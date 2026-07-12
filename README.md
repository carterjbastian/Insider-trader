---
strategy: insider-trader
market: stock
mode: async
horizon: long
autonomy: research
status: research
chamber: house
---
# Insider Trader

Mines **US Congressional stock-trade disclosures** (STOCK Act Periodic Transaction Reports)
for trades that may ride on non-public information, and follows them. A **Black Box** strategy
(async, long-horizon). Full design: vault `Projects/Active/Black Box/Specs/05 — Strategy — Insider Trader`.

> **Money is real.** `research` only. Signals are advisory; no orders without a gated promotion.

## Status — Phase 1: House ingester → Neon DB
- `house.py` — download the House Clerk annual FD ZIP, parse the XML index (PTRs = `FilingType=P`),
  fetch each PTR PDF (`/ptr-pdfs/<year>/<DocID>.pdf`), and parse transactions (owner, ticker,
  buy/sell, transaction date, **disclosure date** + the reporting **delay**, amount bracket).
- `store.py` — Neon schema + upsert (optional/resilient; reads `DATABASE_URL`).
- `backfill.py` — ingest a range of years.

Senate (`efdsearch.senate.gov`) is **Phase 6** — added after House proves out.

### Data notes
- Source is **free + official + ungated** (House). Amounts are **ranges**, not exact $.
  Filings lag (45-day legal limit, often later) — the delay is itself a signal. Older/some PTRs
  are scanned PDFs → flagged `unparsed` for later OCR.

## Develop
```bash
uv sync
uv run pytest
DATABASE_URL=... uv run python -m insider_trader.backfill 2024 2024   # ingest one year
```
