#!/usr/bin/env python3
"""LiveSellSI data pipeline - fetch, normalize, dedupe and store .si domain sales.

Runs on GitHub Actions (see .github/workflows/update-data.yml) and locally.

Data flow:
  1. Load existing records from src/data/sales.json
  2. Merge manual/seed entries from scripts/seeds/seed_sales.json
  3. Run every registered source in SOURCES (see below)
  4. Deduplicate by domain name, sort by date descending,
     rewrite src/data/sales.json

Dedupe rule: first-seen wins, EXCEPT a public record (venue != "Private")
replaces an existing "Private" placeholder for the same domain.

The process exits 0 even when the network is unreachable or a source crashes
(only warnings are logged), so scheduled CI runs never break the build. The
file is only rewritten when content actually changed, which keeps git history
clean and avoids no-op commits.

Dependencies: Python 3.9+ standard library by default. Optional extras from
scripts/requirements.txt:
  - beautifulsoup4: tree-based HTML row extraction (recommended in CI);
    a stdlib regex fallback keeps the script fully functional without it.
  - requests: reserved for future API/RSS source modules.

Adding a new source (RSS feed, API, public archive):
  write a function `def scrape_my_source(max_weeks: int) -> list[dict]`
  returning RAW records {"domain", "price", "date", "venue"} and register
  it in the SOURCES dict. Normalization, dedupe and sorting happen centrally.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "src" / "data" / "sales.json"
SEED_FILE = ROOT / "scripts" / "seeds" / "seed_sales.json"

# --- Optional third-party dependencies ---------------------------------------

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# --- Source configuration ----------------------------------------------------

DNJ_CURRENT_PAGE = "https://www.dnjournal.com/domainsales.htm"
DNJ_ARCHIVE_INDEX = "https://www.dnjournal.com/archive/domainsales-archive.htm"

USER_AGENT = "LiveSellSI-Bot/1.0 (+https://livesellsi.com; .si domain sales data pipeline)"
HTTP_TIMEOUT = 25

# Sanity limits and conversion constants
MAX_PRICE_USD = 10_000_000
REG_FEE_USD = 20.0  # baseline annual .si registration fee (used only for docs)
EUR_TO_USD = 1.12   # fallback rate when a page omits the "= $X" conversion
GBP_TO_USD = 1.28

# --- Parsing helpers ----------------------------------------------------------

DOMAIN_RE = re.compile(r"\b([a-z0-9][a-z0-9-]{0,62})\.si(?![a-z0-9-])", re.I)
DOMAIN_FULL_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})\.si$", re.I)

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
DNJ_HREF_RE = re.compile(r'href="([^"]*domainsales/\d{4}/\d{4,8}\.htm)"', re.I)
YEAR_INDEX_RE = re.compile(r'href="([^"]*domainsales-archive-\d{4}\.htm)"', re.I)

# One matcher for every price representation, with positions preserved so a
# domain can be paired with the price that follows it in the same row.
PRICE_ALT_RE = re.compile(
    r"(=\s*\$[\d,]{3,}(?:\.\d+)?)"   # explicit conversion: "€21,800 = $24,634"
    r"|(\$[\d,]{3,}(?:\.\d+)?)"      # plain "$20,000"
    r"|(€[\d,]{3,}(?:\.\d+)?)"       # bare euro
    r"|(£[\d,]{3,}(?:\.\d+)?)",      # bare pound
)

# DNJournal rows like "HeroCare.com £20,000 = $26,200" carry the original
# currency BEFORE the USD conversion. Positional pairing would grab the £
# token first and apply our fallback rate, shadowing the authoritative "= $Y".
# Rewriting these to a bare "$Y" before price matching fixes that.
CONVERTED_PRICE_RE = re.compile(r"[€£][\d,]{3,}(?:\.\d+)?\s*=\s*(\$[\d,]{3,}(?:\.\d+)?)")

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_DATE_RE = re.compile(r"domainsales/(\d{4})/(\d{8}|\d{4})\.htm", re.I)

_DAY = r"(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat)(?:day)?\.?,?\s+"
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|"
    r"Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)"
)
PERIOD_END_RE = re.compile(rf"{_DAY}({_MONTH})\s+(\d{{1,2}}),\s+(\d{{4}})", re.I)
ENDING_RE = re.compile(rf"ending\s+{_DAY}({_MONTH})\s+(\d{{1,2}}),\s+(\d{{4}})", re.I)
CAPTION_END_RE = re.compile(
    rf"{_DAY}{_MONTH}\s+\d{{1,2}},\s+\d{{4}}\s*[-–—]+\s*{_DAY}({_MONTH})\s+(\d{{1,2}}),\s+(\d{{4}})",
    re.I,
)

_MONTH_ABBR = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "jun": "June", "jul": "July", "aug": "August", "sep": "September",
    "sept": "September", "oct": "October", "nov": "November", "dec": "December",
}

# Known marketplaces, ordered longest-first for unambiguous matching.
VENUES = [
    "DomainMarket",
    "DomainLore.uk",
    "LegalBrandMarketing",
    "TopDomains",
    "DomainName.com",
    "Spaceship",
    "Namecheap",
    "Afternic",
    "GoDaddy",
    "Dan.com",
    "Flippa",
    "Atom.com",
    "Uniregistry",
    "Buy.name",
    "Sedo",
]

# Keyword -> category heuristics (checked in order, lowercase label).
CATEGORY_RULES: List[tuple] = [
    (("ai", "data", "bot", "robot"), "AI Data"),
    (("kripto", "bitcoin", "btc", "crypto", "coin", "nalozb", "invest", "banka", "plac", "capital", "economy"), "Finance & Crypto"),
    (("shop", "kupim", "prodaj", "trgovin", "nakup", "store"), "E-commerce"),
    (("avto", "koles", "moto", "vozil", "auto"), "Automotive"),
    (("nepremicnin", "hisa", "stanovanj", "estate"), "Real Estate"),
    (("vino", "pivo", "kava", "hran", "recept", "kuhin", "pica"), "Food & Drink"),
    (("letalo", "potovan", "hotel", "pocitnic", "turizem", "term", "fly"), "Travel"),
    (("igre", "kviz", "game", "play", "casino"), "Gaming"),
    (("foto", "slika", "video", "photo"), "Photography"),
    (("zavarovan", "insurance"), "Insurance"),
    (("zdrav", "lekarn", "farma", "fitness", "health"), "Health"),
    (("saas", "app", "cloud", "softwar", "splet", "web", "hosting"), "Tech SaaS"),
    (("energi", "solar", "elektr"), "Energy"),
    (("novic", "medij", "radio", "news"), "Media"),
    (("zaposlit", "delo", "job", "hr"), "Jobs"),
]


def clean_text(raw: str) -> str:
    """Strip tags, unescape entities and collapse whitespace."""
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", unescape(raw))).strip()


def extract_rows(html: str) -> List[str]:
    """Return cleaned <tr> texts in document order.

    Uses BeautifulSoup's tree-based extraction when installed (robust against
    malformed markup); falls back to regex otherwise.
    """
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        return [re.sub(r"\s+", " ", tr.get_text(" ", strip=True)).strip() for tr in soup.find_all("tr")]
    return [clean_text(row_html) for row_html in ROW_RE.findall(html)]


def fetch_text(url: str) -> str:
    """Fetch a URL as text. Returns '' on any network/HTTP error."""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logging.warning("Fetch failed for %s: %s", url, exc)
        return ""


def money_to_int(raw: str, rate: float = 1.0) -> Optional[int]:
    value = int(round(float(raw.replace(",", "")) * rate))
    if 0 < value <= MAX_PRICE_USD:
        return value
    return None


def price_match_value(match: "re.Match") -> Optional[int]:
    """Convert a PRICE_ALT_RE match into a USD integer."""
    group_eq, group_usd, group_eur, group_gbp = match.groups()
    if group_eq is not None:
        return money_to_int(group_eq[group_eq.index("$") + 1:])
    if group_usd is not None:
        return money_to_int(group_usd[1:])
    if group_eur is not None:
        return money_to_int(group_eur[1:], EUR_TO_USD)
    return money_to_int(group_gbp[1:], GBP_TO_USD)


def normalize_month(name: str) -> str:
    key = name.lower().rstrip(".")
    return _MONTH_ABBR.get(key, name)


def parse_issue_date(page_url: str) -> Optional[datetime]:
    """Extract the issue date from a DNJournal chart URL (2026/0204.htm or
    2009/20090729.htm). Returns None when the URL carries no date."""
    match = URL_DATE_RE.search(page_url or "")
    if not match:
        return None
    year, token = match.group(1), match.group(2)
    if len(token) == 8:
        year, token = token[:4], token[4:]
    try:
        return datetime(int(year), int(token[:2]), int(token[2:4]))
    except ValueError:
        return None


def parse_period_end(html: str, page_url: str = "") -> Optional[str]:
    """Return the reporting-period end date of a DNJournal page as ISO date.

    Tries in order: chart caption date range ("Mon. Jan 5 - Sun. Jan 18, 2026"),
    prose "ending Sunday, ..." sentence, then the last bare date on the page.
    DNJournal occasionally typo's the year in its prose (e.g. "January 18, 2025"
    on a 2026 issue); when the page URL carries an issue date, results further
    than 45 days before the issue are treated as typos and corrected to
    issue-date minus 15 days (the typical publication lag).
    """
    text = clean_text(html)
    match = CAPTION_END_RE.search(text) or ENDING_RE.search(text)
    if match:
        month, day, year = match.group(1), match.group(2), match.group(3)
    else:
        matches = PERIOD_END_RE.findall(text)
        if not matches:
            return None
        month, day, year = matches[-1]
    try:
        parsed = datetime.strptime(f"{normalize_month(month)} {int(day)} {year}", "%B %d %Y")
    except ValueError:
        return None

    issue_date = parse_issue_date(page_url)
    if issue_date:
        delta = (issue_date - parsed).days
        if delta < 0 or delta > 45:
            corrected = issue_date - timedelta(days=15)
            logging.info("Correcting implausible period end %s -> %s for %s",
                         parsed.strftime("%Y-%m-%d"), corrected.strftime("%Y-%m-%d"), page_url)
            parsed = corrected
    return parsed.strftime("%Y-%m-%d")


def parse_dnj_rows(html: str, date_iso: str) -> List[Dict[str, Any]]:
    """Extract .si sales from DNJournal chart table rows.

    Rows may contain two (domain, price) pairs (YTD charts) or a single pair
    with a venue (weekly charts). Prices are paired with the domain that
    precedes them inside the same row.
    """
    records: List[Dict[str, Any]] = []
    for text in extract_rows(html):
        text = CONVERTED_PRICE_RE.sub(r"\1", text)
        price_matches = list(PRICE_ALT_RE.finditer(text))
        if not price_matches:
            continue
        venue = next((v for v in VENUES if v.lower() in text.lower()), "Private")
        for domain_match in DOMAIN_RE.finditer(text):
            price_match = next(
                (p for p in price_matches if p.start() >= domain_match.end()), None
            )
            if price_match is None:
                continue
            price = price_match_value(price_match)
            if price is None:
                continue
            records.append(
                {
                    "domain": f"{domain_match.group(1)}.si",
                    "price": price,
                    "date": date_iso,
                    "venue": venue,
                }
            )
    return records


def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://www.dnjournal.com{href}"
    return f"https://www.dnjournal.com/archive/{href}"


def discover_dnj_pages(max_weeks: int, skip: int = 0) -> List[str]:
    """List DNJournal chart pages to parse, oldest first, current page last.

    Follows the main archive index AND the per-year index pages (2023, 2024,
    2025, ...) so the whole public archive is reachable. Oldest-first
    processing means each domain keeps the date of its earliest reporting,
    which is closest to the actual sale date.

    `max_weeks` caps the selection to the newest N archive pages. `skip`
    drops the newest `skip` archive pages BEFORE that cap, which enables
    chunked historical backfills (e.g. skip=0 for the newest slice, then
    skip=N to walk further back).
    """
    pages: List[str] = []
    index_html = fetch_text(DNJ_ARCHIVE_INDEX)
    if index_html:
        for href in sorted(set(DNJ_HREF_RE.findall(index_html))):
            pages.append(_abs_url(href))
        # Per-year index pages cover the older archive. Fetch them newest-first
        # and stop once we have a comfortable buffer, so short cron runs never
        # crawl 20+ index pages they don't need. When `skip` is used (chunked
        # backfills) the whole year index must be read to keep slice math exact.
        target = float("inf") if skip > 0 else max(1, max_weeks) * 2
        for year_href in sorted(set(YEAR_INDEX_RE.findall(index_html)), reverse=True):
            if len(pages) >= target:
                break
            year_html = fetch_text(_abs_url(year_href))
            if not year_html:
                continue
            for href in sorted(set(DNJ_HREF_RE.findall(year_html))):
                url = _abs_url(href)
                if url not in pages:
                    pages.append(url)
    pages = sorted(set(pages))
    if skip > 0:
        pages = pages[:-skip] if skip < len(pages) else []
    if len(pages) > max(1, max_weeks - 1):
        pages = pages[-(max_weeks - 1):]
    pages.append(DNJ_CURRENT_PAGE)
    return pages


def scrape_dnjournal(max_weeks: int, skip: int = 0) -> List[Dict[str, Any]]:
    """Fetch DNJournal weekly charts and return raw .si sale records."""
    records: List[Dict[str, Any]] = []
    for page in discover_dnj_pages(max(1, max_weeks), skip):
        html = fetch_text(page)
        if not html:
            continue
        date_iso = parse_period_end(html, page)
        if not date_iso:
            logging.warning("No reporting period found on %s - skipping page", page)
            continue
        rows = parse_dnj_rows(html, date_iso)
        logging.info("%s: %s .si row(s) found (period end %s)", page, len(rows), date_iso)
        records.extend(rows)
    return records


# --- Source registry ----------------------------------------------------------
#
# Each source is a callable taking (max_weeks, skip) and returning RAW record
# dicts with keys: domain, price, date, venue. Normalization, dedupe and
# sorting are handled centrally in main(). A source raising an exception never
# aborts the pipeline - the failure is logged and the remaining sources run.

SOURCES: Dict[str, Callable[[int, int], List[Dict[str, Any]]]] = {
    "dnjournal": scrape_dnjournal,
}


# --- Normalization ------------------------------------------------------------

def slug_for_label(label: str) -> str:
    return f"{label}-si"


def categorize(label: str) -> str:
    if len(label) <= 3:
        return "Short Brandable"
    for keywords, category in CATEGORY_RULES:
        if any(keyword in label for keyword in keywords):
            return category
    return "Keyword"


def normalize(record: Any, source: str) -> Optional[Dict[str, Any]]:
    """Validate and normalize one raw record into the canonical schema."""
    if not isinstance(record, dict):
        return None
    domain = str(record.get("domain", "")).strip().lower()
    match = DOMAIN_FULL_RE.match(domain)
    if not match:
        logging.debug("Skipping invalid domain from %s: %r", source, domain)
        return None
    label = match.group(1)

    try:
        price = int(record.get("price"))
    except (TypeError, ValueError):
        logging.debug("Skipping record with bad price from %s: %r", source, domain)
        return None
    if not (0 < price <= MAX_PRICE_USD):
        logging.debug("Skipping record with out-of-range price from %s: %r", source, domain)
        return None

    date = str(record.get("date", "")).strip()
    if not ISO_DATE_RE.match(date):
        logging.debug("Skipping record with bad date from %s: %r", source, domain)
        return None
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None

    venue = str(record.get("venue", "")).strip()[:40] or "Private"
    category = str(record.get("category", "")).strip()[:40] or categorize(label)
    length = int(record.get("length", len(label)))
    if length <= 0:
        length = len(label)

    return {
        "id": slug_for_label(label),
        "domain": domain,
        "price": price,
        "date": date,
        "venue": venue,
        "category": category,
        "length": length,
    }


# --- Merge / dedupe -----------------------------------------------------------

def merge_records(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe by domain. First-seen wins, except public records replace
    'Private' placeholders for the same domain."""
    by_domain: Dict[str, Dict[str, Any]] = {}
    for record in existing + incoming:
        key = record["domain"]
        previous = by_domain.get(key)
        if previous is None:
            by_domain[key] = record
        elif previous["venue"] == "Private" and record["venue"] != "Private":
            logging.info("Upgrading %s with public record (%s, $%s)",
                         key, record["venue"], record["price"])
            by_domain[key] = record
        else:
            logging.debug("Duplicate skipped: %s", key)
    return list(by_domain.values())


# --- Persistence --------------------------------------------------------------

def load_file(path: Path) -> tuple:
    """Return (raw_text, records). Missing/corrupt files yield ('', [])."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        logging.warning("Could not read %s (treated as empty)", path)
        return "", []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logging.error("Invalid JSON in %s: %s - treating as empty", path, exc)
        return "", []
    return raw, data if isinstance(data, list) else []


def serialize(records: List[Dict[str, Any]]) -> str:
    return json.dumps(records, indent=2, ensure_ascii=False) + "\n"


# --- Main ---------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and merge .si domain sales data")
    parser.add_argument("--dry-run", action="store_true", help="do not write sales.json")
    parser.add_argument("--skip-scrape", action="store_true", help="only merge seed data (no network)")
    parser.add_argument("--max-weeks", type=int, default=12, help="max DNJournal weekly pages to parse (across all years)")
    parser.add_argument("--skip", type=int, default=0, help="skip the newest N archive pages (historical backfill chunking)")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.info("LiveSellSI pipeline started (dry-run=%s, scrape=%s, bs4=%s)",
                 args.dry_run, not args.skip_scrape, HAS_BS4)

    # 1. Load existing + seed data
    raw_existing, existing = load_file(DATA_FILE)
    _, seed = load_file(SEED_FILE)
    records = [record for record in (normalize(r, "existing") for r in existing) if record]
    seeds = [record for record in (normalize(r, "seed") for r in seed) if record]
    before = len(records)
    records = merge_records(records, seeds)
    logging.info("Seed merge: %s existing, %s seed, %s total after dedupe",
                 before, len(seeds), len(records))

    # 2. Run every registered source; one failing source never aborts the run
    if not args.skip_scrape:
        for name, scraper in SOURCES.items():
            try:
                raw_records = scraper(args.max_weeks, args.skip)
            except Exception as exc:
                logging.warning("Source %r raised an unexpected error: %s", name, exc)
                raw_records = []
            scraped = [record for record in (normalize(r, name) for r in raw_records) if record]
            records = merge_records(records, scraped)
            logging.info("Source %r: %s usable record(s), %s total after dedupe",
                         name, len(scraped), len(records))

    # 3. Sort and serialize
    records.sort(key=lambda r: (r["date"], r["price"]), reverse=True)
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)):
        logging.error("Duplicate ids after merge - aborting to protect data integrity")
        return 1
    output = serialize(records)

    # 4. Summary
    volume = sum(r["price"] for r in records)
    logging.info("Summary: %s records, $%s total volume, newest %s",
                 len(records), f"{volume:,}", records[0]["date"] if records else "-")

    # 5. Write
    if args.dry_run:
        logging.info("Dry run - would write %s bytes to %s", len(output), DATA_FILE)
        return 0
    if output == raw_existing:
        logging.info("No changes detected - src/data/sales.json left untouched")
        return 0
    try:
        DATA_FILE.write_text(output, encoding="utf-8")
    except OSError as exc:
        logging.error("Failed to write %s: %s", DATA_FILE, exc)
        return 1
    logging.info("Wrote %s records to %s", len(records), DATA_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
