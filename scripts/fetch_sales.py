#!/usr/bin/env python3
"""LiveSellSI data pipeline - fetch, normalize, dedupe and store .si domain sales.

Runs on GitHub Actions (see .github/workflows/update-data.yml) and locally.
Uses ONLY the Python standard library - no pip dependencies, zero cost.

Data flow:
  1. Load existing records from src/data/sales.json
  2. Merge manual/seed entries from scripts/seeds/seed_sales.json
  3. Scrape publicly reported sales from DNJournal weekly charts
  4. Deduplicate by domain name, sort by date descending,
     rewrite src/data/sales.json

Dedupe rule: first-seen wins, EXCEPT a public record (venue != "Private")
replaces an existing "Private" placeholder for the same domain.

The process exits 0 even when the network is unreachable (only warnings are
logged), so scheduled CI runs never break the build. The file is only
rewritten when content actually changed, which keeps git history clean.

Requirements: Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "src" / "data" / "sales.json"
SEED_FILE = ROOT / "scripts" / "seeds" / "seed_sales.json"

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
DNJ_HREF_RE = re.compile(r'href="([^"]*domainsales/\d{4}/\d{4}\.htm)"', re.I)

# One matcher for every price representation, with positions preserved so a
# domain can be paired with the price that follows it in the same row.
PRICE_ALT_RE = re.compile(
    r"(=\s*\$[\d,]{3,}(?:\.\d+)?)"   # explicit conversion: "€21,800 = $24,634"
    r"|(\$[\d,]{3,}(?:\.\d+)?)"      # plain "$20,000"
    r"|(€[\d,]{3,}(?:\.\d+)?)"       # bare euro
    r"|(£[\d,]{3,}(?:\.\d+)?)",      # bare pound
)

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DAY = r"(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat)(?:day)?\.?,?\s+"
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|"
    r"Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)"
)
PERIOD_END_RE = re.compile(rf"{_DAY}({_MONTH})\s+(\d{{1,2}}),\s+(\d{{4}})", re.I)
ENDING_RE = re.compile(rf"ending\s+{_DAY}({_MONTH})\s+(\d{{1,2}}),\s+(\d{{4}})", re.I)

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


def parse_period_end(html: str) -> Optional[str]:
    """Return the reporting-period end date of a DNJournal page as ISO date."""
    text = clean_text(html)
    match = ENDING_RE.search(text)
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
    return parsed.strftime("%Y-%m-%d")


def parse_dnj_rows(html: str, date_iso: str) -> List[Dict[str, Any]]:
    """Extract .si sales from DNJournal chart table rows.

    Rows may contain two (domain, price) pairs (YTD charts) or a single pair
    with a venue (weekly charts). Prices are paired with the domain that
    precedes them inside the same row.
    """
    records: List[Dict[str, Any]] = []
    for row_html in ROW_RE.findall(html):
        text = clean_text(row_html)
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


def discover_dnj_pages(max_weeks: int) -> List[str]:
    """List DNJournal chart pages to parse, oldest first, current page last.

    Oldest-first processing means each domain keeps the date of its earliest
    reporting, which is closest to the actual sale date.
    """
    pages: List[str] = []
    index_html = fetch_text(DNJ_ARCHIVE_INDEX)
    if index_html:
        hrefs = sorted(set(DNJ_HREF_RE.findall(index_html)))
        for href in hrefs:
            url = href if href.startswith("http") else f"https://www.dnjournal.com/archive/{href}"
            pages.append(url)
    if len(pages) > max(1, max_weeks - 1):
        pages = pages[-(max_weeks - 1):]
    pages.append(DNJ_CURRENT_PAGE)
    return pages


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
    parser.add_argument("--max-weeks", type=int, default=6, help="max DNJournal weekly pages to parse")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.info("LiveSellSI pipeline started (dry-run=%s, scrape=%s)",
                 args.dry_run, not args.skip_scrape)

    # 1. Load existing + seed data
    raw_existing, existing = load_file(DATA_FILE)
    _, seed = load_file(SEED_FILE)
    records = [record for record in (normalize(r, "existing") for r in existing) if record]
    seeds = [record for record in (normalize(r, "seed") for r in seed) if record]
    before = len(records)
    records = merge_records(records, seeds)
    logging.info("Seed merge: %s existing, %s seed, %s total after dedupe",
                 before, len(seeds), len(records))

    # 2. Scrape public sources (oldest pages first so earliest reporting wins)
    if not args.skip_scrape:
        for page in discover_dnj_pages(max(1, args.max_weeks)):
            html = fetch_text(page)
            if not html:
                continue
            date_iso = parse_period_end(html)
            if not date_iso:
                logging.warning("No reporting period found on %s - skipping page", page)
                continue
            rows = parse_dnj_rows(html, date_iso)
            logging.info("%s: %s .si row(s) found (period end %s)", page, len(rows), date_iso)
            scraped = [record for record in (normalize(r, "dnjournal") for r in rows) if record]
            records = merge_records(records, scraped)

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
