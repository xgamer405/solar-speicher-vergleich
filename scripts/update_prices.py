#!/usr/bin/env python3
"""Best-effort price updater for the solar storage comparison.

Uses Idealo links from data.json. Idealo often blocks simple non-browser clients
with HTTP 503; in that case the script keeps the existing price/source and writes
`price_error` + `price_checked_at` so the site stays honest.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36"
PRICE_RE = re.compile(r"(?<!\d)(\d{2,5})(?:[.,](\d{2}))?\s*€")


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def parse_lowest_price(html: str) -> float | None:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    vals: list[float] = []
    for euro, cent in PRICE_RE.findall(text):
        val = int(euro.replace(".", "")) + (int(cent or 0) / 100)
        if 250 <= val <= 10000:
            vals.append(val)
    return min(vals) if vals else None


def main() -> int:
    products = json.loads(DATA.read_text(encoding="utf-8"))
    today = dt.date.today().isoformat()
    changed = False
    for p in products:
        url = p.get("idealo_url")
        if not url:
            continue
        try:
            html = fetch(url)
            price = parse_lowest_price(html)
            p["price_checked_at"] = today
            if price is None:
                p["price_error"] = "Idealo abgefragt, aber kein Preis erkannt; alter Preis beibehalten"
            else:
                old = p.get("current_price_eur")
                p["current_price_eur"] = round(price, 2)
                p["price_source"] = "Idealo Preisvergleich automatisch abgefragt"
                p.pop("price_error", None)
                changed = changed or old != p["current_price_eur"]
        except Exception as e:
            p["price_checked_at"] = today
            p["price_error"] = f"Idealo Autoabfrage fehlgeschlagen ({type(e).__name__}); alter Preis beibehalten"
        time.sleep(2)
    DATA.write_text(json.dumps(products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated {DATA}; price_changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
