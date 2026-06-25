"""Biographical profiles for congresspeople — the Phase-2 'who are they' dossier.

Built **just-in-case**, one per member (bioguide), persisted in `member_profiles` and
reused across all of that member's trades. Two layers:

  1. STRUCTURED SPINE (here, offline) — from unitedstates/congress-legislators: a dated
     career timeline (terms, party/state/chamber over time, tenure, leadership roles,
     committee assignments) + external IDs (wikipedia/wikidata). Fully point-in-time
     because every term/role is dated.
  2. NARRATIVE (added later) — Wikipedia/Wikidata career, education, pre-Congress
     profession, and industry relationships, summarized by Opus into a profile. The
     leakage-prone layer; the scorer is told to use only pre-trade-date facts.

  DATABASE_URL=... uv run python -m insider_trader.profiles <bioguide>
"""

from __future__ import annotations

import json

from . import store
from .members import _get  # cached congress-legislators fetch

_SCHEMA = """
CREATE TABLE IF NOT EXISTS member_profiles (
    bioguide    text PRIMARY KEY,
    full_name   text,
    spine       jsonb,
    wikipedia   text,
    profile_md  text,
    sources     jsonb,
    model       text,
    created     timestamptz DEFAULT now()
);
"""


def legislator_index() -> dict[str, dict]:
    """bioguide -> raw congress-legislators record (current + historical)."""
    legs = _get("legislators-current.json") + _get("legislators-historical.json")
    return {leg["id"]["bioguide"]: leg for leg in legs if leg.get("id", {}).get("bioguide")}


def _committee_history() -> dict[str, list[dict]]:
    """bioguide -> [{thomas_id, name, rank, title}] from current membership (current-only;
    historical committee tenure isn't in this free source — flagged as a point-in-time gap)."""
    membership = _get("committee-membership-current.json")
    committees = {
        c.get("thomas_id") or (c.get("type", "") + c.get("name", "")): c["name"]
        for c in _get("committees-current.json")
    }
    out: dict[str, list[dict]] = {}
    for code, members in membership.items():
        name = committees.get(code, code)
        for m in members:
            out.setdefault(m["bioguide"], []).append(
                {"code": code, "name": name, "rank": m.get("rank"), "title": m.get("title")}
            )
    return out


def build_spine(leg: dict, committees: list[dict] | None = None) -> dict:
    """A dated, point-in-time-safe career timeline from a congress-legislators record."""
    nm, bio, ids = leg.get("name", {}), leg.get("bio", {}), leg.get("id", {})
    terms = leg.get("terms", [])
    timeline = [
        {
            "start": t.get("start"),
            "end": t.get("end"),
            "chamber": "Senate" if t.get("type") == "sen" else "House",
            "state": t.get("state"),
            "district": t.get("district"),
            "party": t.get("party"),
        }
        for t in terms
    ]
    leadership = [
        {"role": r.get("title"), "start": r.get("start"), "end": r.get("end")}
        for r in leg.get("leadership_roles", [])
    ]
    return {
        "full_name": nm.get("official_full")
        or f"{nm.get('first', '')} {nm.get('last', '')}".strip(),
        "birthday": bio.get("birthday"),
        "gender": bio.get("gender"),
        "entered_office": min((t.get("start") for t in terms if t.get("start")), default=None),
        "left_office": max((t.get("end") for t in terms if t.get("end")), default=None),
        "n_terms": len(terms),
        "timeline": timeline,
        "leadership_roles": leadership,
        "committees_current": committees or [],
        "wikipedia": ids.get("wikipedia"),
        "wikidata": ids.get("wikidata"),
    }


def save_profile(conn, bioguide, spine, wikipedia=None, profile_md=None, sources=None, model=None):
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
        cur.execute(
            "INSERT INTO member_profiles (bioguide, full_name, spine, wikipedia, profile_md, "
            "sources, model) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (bioguide) DO UPDATE SET "
            "full_name=EXCLUDED.full_name, spine=EXCLUDED.spine, wikipedia=EXCLUDED.wikipedia, "
            "profile_md=COALESCE(EXCLUDED.profile_md, member_profiles.profile_md), "
            "sources=EXCLUDED.sources, model=EXCLUDED.model, created=now()",
            (
                bioguide,
                spine.get("full_name"),
                json.dumps(spine),
                wikipedia,
                profile_md,
                json.dumps(sources or {}),
                model,
            ),
        )
    conn.commit()


def build_spines(conn, bioguides: list[str] | None = None) -> dict:
    """Build + persist the structured spine for the given members (default: all that traded)."""
    idx = legislator_index()
    chist = _committee_history()
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
        if bioguides is None:
            cur.execute(
                "SELECT DISTINCT f.bioguide FROM filings f JOIN transactions t USING(doc_id) "
                "WHERE f.bioguide IS NOT NULL"
            )
            bioguides = [r[0] for r in cur.fetchall()]
    built = missing = 0
    for bio in bioguides:
        leg = idx.get(bio)
        if not leg:
            missing += 1
            continue
        spine = build_spine(leg, chist.get(bio, []))
        save_profile(conn, bio, spine, wikipedia=spine.get("wikipedia"))
        built += 1
    return {"requested": len(bioguides), "built": built, "missing": missing}


if __name__ == "__main__":
    import sys

    conn = store.connect()
    if conn:
        if len(sys.argv) > 1:
            idx = legislator_index()
            leg = idx.get(sys.argv[1])
            print(json.dumps(build_spine(leg, _committee_history().get(sys.argv[1], [])), indent=2))
        else:
            print(build_spines(conn))
        conn.close()
