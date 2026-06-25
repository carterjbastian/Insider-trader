"""US Senate eFD ingester — the agreement-gated search + e-filed PTR HTML parser.

Source (free, official, terms-of-use gated): efdsearch.senate.gov. Flow:
  1. GET /search/home/  -> CSRF token.
  2. POST /search/home/ with prohibition_agreement=1  -> accept the terms.
  3. POST /search/report/data/ (report_types=[11] = PTR) -> DataTables JSON.
  4. e-filed PTRs -> /search/view/ptr/<uuid>/ : a clean HTML transactions table.
     (paper/scanned PTRs are skipped here — flagged for OCR later.)

⚖️ The eFD agreement prohibits COMMERCIAL use / credit-rating / solicitation. This is a
personal research tool (not reselling the data); we POST-accept the agreement rather
than bypass it. Lands in the SAME Neon filings/transactions tables as the House data
(chamber='senate'), so all downstream analysis just works.

  DATABASE_URL=... uv run python -m insider_trader.senate 2024 2024
"""

from __future__ import annotations

import http.cookiejar
import re
import urllib.parse
import urllib.request
from datetime import date

from . import store
from .house import _date  # reuse the year-sane date parser
from .models import Transaction

BASE = "https://efdsearch.senate.gov"
_UA = "Mozilla/5.0 (X11; Linux x86_64) blackbox-insider-trader/0.1 (research)"
_OWNER = {"self": "SELF", "spouse": "SP", "joint": "JT", "child": "DC", "dependent": "DC"}
_AMT = re.compile(r"\$([\d,]+)(?:\s*-\s*\$?([\d,]+))?")


def _authed():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", _UA)]
    home = op.open(BASE + "/search/home/", timeout=30).read().decode("utf-8", "replace")  # noqa: S310
    tok = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', home).group(1)
    op.open(  # accept the prohibition agreement
        urllib.request.Request(
            BASE + "/search/home/",
            data=urllib.parse.urlencode(
                {"prohibition_agreement": "1", "csrfmiddlewaretoken": tok}
            ).encode(),
            headers={"Referer": BASE + "/search/home/"},
        ),
        timeout=30,
    ).read()
    csrf = next((c.value for c in cj if c.name == "csrftoken"), tok)
    return op, csrf


def _search(op, csrf, start: str, end: str, offset: int, length: int = 100) -> dict:
    import json

    payload = {
        "start": str(offset),
        "length": str(length),
        "report_types": "[11]",
        "filer_types": "[]",
        "submitted_start_date": f"{start} 00:00:00",
        "submitted_end_date": f"{end} 23:59:59",
        "candidate_states": "[]",
        "senator_states": "[]",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "csrfmiddlewaretoken": csrf,
    }
    req = urllib.request.Request(
        BASE + "/search/report/data/",
        data=urllib.parse.urlencode(payload).encode(),
        headers={
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/search/",
        },
    )
    return json.loads(op.open(req, timeout=40).read().decode("utf-8", "replace"))  # noqa: S310


def iter_ptrs(op, csrf, start: str, end: str):
    """Yield (first, last, doc_id, url, disclosure_date) for e-filed PTRs in the window."""
    offset, total = 0, None
    while total is None or offset < total:
        d = _search(op, csrf, start, end, offset)
        total = d.get("recordsTotal", 0)
        rows = d.get("data", [])
        if not rows:
            break
        for r in rows:
            href = re.search(r'href="([^"]+)"', r[3])
            if not href or "/ptr/" not in href.group(1):
                continue  # paper/scanned -> skip (OCR later)
            doc_id = href.group(1).rstrip("/").split("/")[-1]
            yield r[0].strip(), r[1].strip(), doc_id, href.group(1), _date(_us(r[4]))
        offset += len(rows)


def _us(d: str) -> str:
    return d.strip()  # eFD already MM/DD/YYYY


def _amount(s: str):
    m = _AMT.search(s)
    if not m:
        return None, None
    lo = int(m.group(1).replace(",", ""))
    hi = int(m.group(2).replace(",", "")) if m.group(2) else None
    return lo, hi


def _ttype(s: str) -> str:
    s = s.lower()
    return "purchase" if "purchase" in s else "exchange" if "exchange" in s else "sale"


def parse_ptr(op, doc_id: str, url: str, disclosure: date | None) -> list[Transaction]:
    html = op.open(BASE + url, timeout=40).read().decode("utf-8", "replace")  # noqa: S310
    out: list[Transaction] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        cells = [html.unescape(re.sub("<[^>]+>", "", c)).strip() for c in tds]
        # columns: #, txn_date, owner, ticker, asset_name, asset_type, type, amount, comment
        if len(cells) < 8 or not re.match(r"\d\d?/\d\d?/\d{4}", cells[1]):
            continue
        ticker = cells[3].strip()
        lo, hi = _amount(cells[7])
        own = cells[2].strip().lower().split()
        out.append(
            Transaction(
                doc_id=doc_id,
                owner=_OWNER.get(own[0], "SELF") if own else "SELF",
                asset_name=re.sub(r"\s+", " ", cells[4]).strip(),
                ticker=(ticker if ticker and ticker != "--" else None),
                asset_type=cells[5].strip() or None,
                txn_type=_ttype(cells[6]),
                txn_date=_date(cells[1]),
                disclosure_date=disclosure,
                amount_low=lo,
                amount_high=hi,
            )
        )
    return out


_CHAMBER_DDL = "ALTER TABLE filings ADD COLUMN IF NOT EXISTS chamber text"


def _save_filing(conn, doc_id, first, last, disclosure, year, parsed, n):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO filings (doc_id,last,first,suffix,state_dst,filing_type,filing_date,"
            "year,parsed,txn_count,chamber) VALUES (%s,%s,%s,'','','P',%s,%s,%s,%s,'senate') "
            "ON CONFLICT (doc_id) DO UPDATE SET parsed=EXCLUDED.parsed, "
            "txn_count=EXCLUDED.txn_count",
            (doc_id, last, first, disclosure, year, parsed, n),
        )
    conn.commit()


def ingest(start_year: int, end_year: int) -> dict:
    import time

    conn = store.connect()
    with conn.cursor() as cur:
        cur.execute(_CHAMBER_DDL)
    conn.commit()
    op, csrf = _authed()
    grand_f = grand_t = 0
    for year in range(start_year, end_year + 1):
        for q in (("01/01", "03/31"), ("04/01", "06/30"), ("07/01", "09/30"), ("10/01", "12/31")):
            start, end = f"{q[0]}/{year}", f"{q[1]}/{year}"
            ptrs = list(iter_ptrs(op, csrf, start, end))
            for first, last, doc_id, url, disc in ptrs:
                try:
                    txns = parse_ptr(op, doc_id, url, disc)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! {doc_id} {type(e).__name__}: {e}", flush=True)
                    txns = []
                _save_filing(conn, doc_id, first, last, disc, year, bool(txns), len(txns))
                store.replace_transactions(conn, doc_id, txns)
                grand_t += len(txns)
                time.sleep(0.3)  # be a polite gated-source citizen
            grand_f += len(ptrs)
            print(f"  {start}..{end}: {len(ptrs)} PTRs", flush=True)
    conn.close()
    print(f"TOTAL: {grand_f} Senate PTRs -> {grand_t} transactions")
    return {"ptrs": grand_f, "transactions": grand_t}


if __name__ == "__main__":
    import sys

    a = sys.argv[1:]
    ingest(int(a[0]), int(a[1]) if len(a) > 1 else int(a[0]))
