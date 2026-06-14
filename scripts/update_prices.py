#!/usr/bin/env python3
"""Slow, best-effort price crawler for the solar storage comparison.

Design goals:
- Be polite: crawl few pages, with long jittered delays in GitHub Actions.
- Prefer product/vendor pages over Idealo; Idealo often blocks bots.
- Never invent prices: if no believable EUR price is found, keep the old price
  and write a transparent `price_error`.
- Keep a small `price_history` when a price actually changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data.json"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/125.0 Safari/537.36"
)

PRICE_AFTER_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[.\s]\d{3})*|\d{2,5})(?:[,.](\d{2}))?\s*(?:€|EUR)",
    re.I,
)
PRICE_BEFORE_RE = re.compile(
    r"(?:€|EUR)\s*(\d{1,3}(?:[.\s]\d{3})*|\d{2,5})(?:[,.](\d{2}))?",
    re.I,
)
JSON_PRICE_RE = re.compile(r'"(?:price|amount|salePrice|currentPrice)"\s*:\s*"?(\d{2,7})(?:[,.](\d{2}))?"?', re.I)
META_PRICE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:price:amount|product:price:amount|twitter:data1)["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)


@dataclass
class SourceResult:
    source: str
    url: str
    price: float | None
    candidates: list[float]
    error: str | None = None


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=35) as r:
        return r.read().decode("utf-8", "ignore")


def normalize_price(euros: str, cents: str | None = None) -> float | None:
    try:
        val = int(re.sub(r"[.\s]", "", euros)) + (int(cents or 0) / 100)
    except ValueError:
        return None
    # Shopify sometimes stores cents as integer, e.g. 99900.
    if val >= 20000 and cents in (None, ""):
        val = val / 100
    return round(val, 2)


def html_to_text(raw: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text))


def extract_candidates(raw: str, source: dict[str, Any]) -> list[float]:
    candidates: list[float] = []
    text = html_to_text(raw)
    head_end = raw.lower().find("</head>")
    head_raw = raw[:head_end] if head_end >= 0 else raw[:100_000]
    # Include head attributes too: several Shopify pages put the actual product
    # price in title/meta/early product JSON. Do not scan full raw HTML because
    # recommendation widgets include unrelated product prices.
    scan_text = text + " " + html_lib.unescape(head_raw)

    for euro, cent in PRICE_AFTER_RE.findall(scan_text) + PRICE_BEFORE_RE.findall(scan_text):
        val = normalize_price(euro, cent)
        if val is not None:
            candidates.append(val)

    # Do not scan arbitrary JSON blobs for prices: Shopify recommendation/cross-sell
    # scripts contain unrelated product prices. Visible text + head/meta prices are
    # safer for this static comparison.

    for m in META_PRICE_RE.findall(raw):
        clean = m.strip().replace(".", "").replace(",", ".") if "," in m else m.strip()
        try:
            candidates.append(round(float(clean), 2))
        except ValueError:
            pass

    min_price = float(source.get("min_price_eur", 50))
    max_price = float(source.get("max_price_eur", 20000))
    filtered = sorted({p for p in candidates if min_price <= p <= max_price})
    return filtered


def crawl_source(source: dict[str, Any]) -> SourceResult:
    url = source["url"]
    name = source.get("name", url)
    try:
        raw = fetch(url)
        visible_text = html_to_text(raw).lower()
        blocked = any(
            marker in visible_text
            for marker in [
                "sorry! something has gone wrong",
                "access denied",
                "unusual traffic",
                "verify you are human",
            ]
        )
        if blocked:
            return SourceResult(name, url, None, [], "blocked")
        candidates = extract_candidates(raw, source)
        price = candidates[0] if candidates else None
        return SourceResult(name, url, price, candidates[:20], None if price is not None else "no_price")
    except Exception as e:  # noqa: BLE001 - keep crawler resilient
        return SourceResult(name, url, None, [], f"{type(e).__name__}: {e}")


def sleep_between_sources(fast: bool) -> None:
    if fast:
        return
    seconds = random.randint(60, 180)
    print(f"sleeping {seconds}s before next source")
    time.sleep(seconds)


def update_product(product: dict[str, Any], today: str, fast: bool) -> bool:
    sources = product.get("crawler_sources") or []
    if not sources:
        return False

    results: list[SourceResult] = []
    for i, source in enumerate(sources):
        if i:
            sleep_between_sources(fast)
        res = crawl_source(source)
        results.append(res)
        print(f"{product['produkt']}: {res.source}: price={res.price} error={res.error}")
        if res.price is not None:
            break

    product["price_checked_at"] = today
    product["price_crawl_results"] = [r.__dict__ for r in results]
    best = next((r for r in results if r.price is not None), None)
    if best is None:
        product["price_error"] = "; ".join(f"{r.source}: {r.error or 'kein Preis'}" for r in results)
        return False

    old = product.get("current_price_eur")
    new = round(float(best.price), 2)
    product["current_price_eur"] = new
    product["price_source"] = f"{best.source} automatisch langsam gecrawlt"
    product["price_url"] = best.url
    product.pop("price_error", None)

    changed = old != new
    if changed:
        history = product.setdefault("price_history", [])
        history.append({"date": today, "price_eur": new, "source": best.source, "url": best.url})
        del history[:-20]
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-products", type=int, default=0, help="test helper: crawl only first N products with sources")
    args = parser.parse_args()

    fast = os.environ.get("PRICE_CRAWLER_FAST") == "1"
    products = json.loads(DATA.read_text(encoding="utf-8"))
    today = dt.date.today().isoformat()
    changed = False
    seen = 0

    for p in products:
        if not p.get("crawler_sources"):
            continue
        if args.max_products and seen >= args.max_products:
            break
        if seen:
            sleep_between_sources(fast)
        seen += 1
        changed = update_product(p, today, fast) or changed

    DATA.write_text(json.dumps(products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated {DATA}; crawled_products={seen}; price_changed={changed}; fast={fast}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
