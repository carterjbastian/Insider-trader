"""US House Clerk ingester — annual FD index + PTR transaction parsing.

Source (free, official, ungated): disclosures-clerk.house.gov
  - Annual index ZIP: /public_disc/financial-pdfs/<YEAR>FD.zip  (XML: PTRs = FilingType 'P')
  - PTR documents:    /public_disc/ptr-pdfs/<YEAR>/<DocID>.pdf  (ptr-pdfs, NOT financial-pdfs)

PTR PDFs are e-filed text (a transactions table). pdfplumber's table detection is
inconsistent on them, so we parse the extracted *text* line-by-line, which is more
robust. Older/scanned PTRs yield no parseable lines -> caller flags them for OCR later.
"""

from __future__ import annotations

import io
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime

from .models import Filing, Transaction

BASE = "https://disclosures-clerk.house.gov/public_disc"
_UA = {"User-Agent": "blackbox-insider-trader/0.1 (research)"}

_OWNER = {"SP": "SP", "DC": "DC", "JT": "JT"}
_TTYPE = {"P": "purchase", "S": "sale", "E": "exchange"}

# The transaction CORE is always on one line: "<P|S|E> <date> <date> $lo[ - $hi]".
# Asset name precedes it; the ticker/[type]/high-amount may wrap to the next line.
_TXN_CORE = re.compile(
    r"\b(?P<ttype>[PSE])(?:\s*\(partial\))?\s+"
    r"(?P<tdate>\d\d?/\d\d?/\d{4})\s+(?P<ndate>\d\d?/\d\d?/\d{4})\s+"
    r"\$\s*(?P<lo>[\d,]+)(?:\s*-\s*\$?\s*(?P<hi>[\d,]+))?"
)
_OWNER_PREFIX = re.compile(r"^(SP|JT|DC)\b\s*")
_TICKER = re.compile(r"\((?P<t>[A-Z][A-Z.]{0,5})\)")
_ATYPE = re.compile(r"\[(?P<a>[A-Za-z]{1,3})\]")
_AMT = re.compile(r"\$\s*(?P<v>[\d,]+)")


def _clean_asset(s: str) -> str:
    s = _ATYPE.sub("", _TICKER.sub("", s))
    return re.sub(r"\s+", " ", s).strip(" -")


def _get(url: str, timeout: int = 60) -> bytes:
    return urllib.request.urlopen(  # noqa: S310 (trusted host)
        urllib.request.Request(url, headers=_UA), timeout=timeout
    ).read()


def _date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


# --- index ------------------------------------------------------------------


def fetch_index(year: int) -> list[Filing]:
    """All filings in the year's FD index (use ``filing_type == 'P'`` for PTRs)."""
    raw = _get(f"{BASE}/financial-pdfs/{year}FD.zip")
    xml = zipfile.ZipFile(io.BytesIO(raw)).read(f"{year}FD.xml").decode("utf-8", "replace")
    out: list[Filing] = []
    for m in ET.fromstring(xml).findall("Member"):
        out.append(
            Filing(
                doc_id=(m.findtext("DocID") or "").strip(),
                last=(m.findtext("Last") or "").strip(),
                first=(m.findtext("First") or "").strip(),
                suffix=(m.findtext("Suffix") or "").strip(),
                state_dst=(m.findtext("StateDst") or "").strip(),
                filing_type=(m.findtext("FilingType") or "").strip(),
                filing_date=_date(m.findtext("FilingDate")),
                year=year,
            )
        )
    return out


def ptrs(year: int) -> list[Filing]:
    return [f for f in fetch_index(year) if f.filing_type == "P"]


# --- PTR document parsing ---------------------------------------------------


def ptr_text(year: int, doc_id: str) -> str:
    """Extracted text of a PTR PDF (all pages)."""
    import pdfplumber

    pdf = _get(f"{BASE}/ptr-pdfs/{year}/{doc_id}.pdf")
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        return "\n".join((pg.extract_text() or "") for pg in p.pages)


def parse_transactions(text: str, doc_id: str) -> list[Transaction]:
    """Parse transactions from PTR text, anchored on the transaction core line.

    Handles long asset names whose ticker / [type] / high-amount wrap to the next
    (continuation) line. Non-equity rows (bonds, options with no ticker) are kept
    with ``ticker=None``.
    """
    lines = text.splitlines()
    out: list[Transaction] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        mo = _OWNER_PREFIX.match(line)
        owner = mo.group(1) if mo else "SELF"
        if mo:
            line = line[mo.end() :]
        m = _TXN_CORE.search(line)
        if not m:
            continue
        asset_part = line[: m.start()]
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        cont = nxt if not _TXN_CORE.search(nxt) else ""  # continuation, not a new txn

        ticker = _TICKER.search(asset_part) or (_TICKER.search(cont) if cont else None)
        atype = _ATYPE.search(asset_part) or (_ATYPE.search(cont) if cont else None)
        lo = int(m.group("lo").replace(",", ""))
        hi = int(m.group("hi").replace(",", "")) if m.group("hi") else None
        if hi is None and cont:  # high amount wrapped to the continuation line
            mh = _AMT.search(cont)
            if mh:
                hi = int(mh.group("v").replace(",", ""))
        out.append(
            Transaction(
                doc_id=doc_id,
                owner=_OWNER.get(owner, "SELF"),
                asset_name=_clean_asset(asset_part),
                ticker=ticker.group("t") if ticker else None,
                asset_type=atype.group("a") if atype else None,
                txn_type=_TTYPE[m.group("ttype")],
                txn_date=_date(m.group("tdate")),
                disclosure_date=_date(m.group("ndate")),
                amount_low=lo,
                amount_high=hi,
            )
        )
    return out


def fetch_ptr(filing: Filing) -> list[Transaction]:
    """Download + parse one PTR filing's transactions (empty list if unparseable/scanned)."""
    return parse_transactions(ptr_text(filing.year, filing.doc_id), filing.doc_id)
