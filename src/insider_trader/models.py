"""Data model for House disclosures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Filing:
    """One row of the House annual FD XML index (a PTR when filing_type == 'P')."""

    doc_id: str
    last: str
    first: str
    suffix: str
    state_dst: str
    filing_type: str
    filing_date: date | None
    year: int


@dataclass(frozen=True)
class Transaction:
    """One parsed transaction line from a PTR PDF."""

    doc_id: str
    owner: str  # SELF | SP (spouse) | DC (dependent child) | JT (joint)
    asset_name: str
    ticker: str | None
    asset_type: str | None  # e.g. ST (stock), OP (option), …
    txn_type: str  # purchase | sale | exchange
    txn_date: date | None
    disclosure_date: date | None
    amount_low: int | None
    amount_high: int | None  # None = open-ended ("over $X")

    @property
    def delay_days(self) -> int | None:
        if self.txn_date and self.disclosure_date:
            return (self.disclosure_date - self.txn_date).days
        return None
