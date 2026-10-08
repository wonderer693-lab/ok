export interface Sale {
  id: string;
  domain: string;
  price: number;
  date: string;
  venue: string;
  category: string;
  length: number;
}

/** Baseline annual .si registration fee (USD) used for the multiplier stat. */
export const REG_FEE_USD = 20;

const usdFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

export function formatUSD(value: number): string {
  return usdFormatter.format(value);
}

export function formatDate(iso: string): string {
  const date = new Date(`${iso}T00:00:00Z`);
  return new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(date);
}

export function regFeeMultiplier(price: number): number {
  return Math.max(1, Math.round(price / REG_FEE_USD));
}

export function sortByDateDesc(a: Sale, b: Sale): number {
  return b.date.localeCompare(a.date) || b.price - a.price;
}

export function sortByPriceDesc(a: Sale, b: Sale): number {
  return b.price - a.price || b.date.localeCompare(a.date);
}
