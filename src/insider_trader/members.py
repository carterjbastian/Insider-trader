"""Match filers to legislators + attach committee assignments (the 'access' layer).

Source: the unitedstates/congress-legislators project (free, CC0, current) — JSON
mirrors at unitedstates.github.io. We match each House filer (last/first/state from
the filing) to a bioguide ID, then attach committee seats.

⚠️ Point-in-time caveat: committee membership here is **current-congress only** (the
free membership file is current). For a *recent* trade that's accurate; for an old
trade it's a proxy (see vault spec 05 §5.5). Flagged, not silently assumed.

  uv run python -m insider_trader.members
"""

from __future__ import annotations

import json
import re
import urllib.request

from . import store

_BASE = "https://unitedstates.github.io/congress-legislators/"


def _get(name: str) -> object:
    req = urllib.request.Request(_BASE + name, headers={"User-Agent": "blackbox-insider/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 (trusted host)
        return json.load(r)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _last_norm(last: str) -> str:
    """Normalize a last name, dropping name suffixes the eFD packs into the last-name
    field (e.g. 'McConnell, Jr.' -> 'mcconnell'). Multi-word last names are joined."""
    parts = [p for p in re.split(r"[,\s]+", (last or "").strip()) if p]
    kept = [p for p in parts if _norm(p) and _norm(p) not in _SUFFIXES]
    return _norm("".join(kept))


# --- roster + committee index ----------------------------------------------


def build_rosters() -> tuple[dict, dict]:
    """Return (name_index, member_meta).

    name_index: last_norm -> list of candidate dicts (bioguide, first_norms, states, …).
    member_meta: bioguide -> {last, first, full_name, party, chamber, state, committees}.
    """
    cur_legs = _get("legislators-current.json")
    legislators = [(leg, True) for leg in cur_legs] + [
        (leg, False) for leg in _get("legislators-historical.json")
    ]
    membership = _get("committee-membership-current.json")
    committees = {
        c.get("thomas_id") or c.get("type", "") + c.get("name", ""): c["name"]
        for c in _get("committees-current.json")
    }

    # bioguide -> [{code, name, title}]
    by_member: dict[str, list[dict]] = {}
    for code, members in membership.items():
        cname = committees.get(code, code)
        for m in members:
            by_member.setdefault(m["bioguide"], []).append(
                {"code": code, "name": cname, "title": m.get("title")}
            )

    name_index: dict[str, list[dict]] = {}
    member_meta: dict[str, dict] = {}
    for leg, is_current in legislators:
        nm, ids, terms = leg["name"], leg["id"], leg.get("terms", [])
        if not terms:
            continue
        last = nm.get("last", "")
        firsts = {
            _norm(nm.get("first", "")),
            _norm(nm.get("nickname", "")),
            _norm((nm.get("official_full", "").split() or [""])[0]),
        }
        firsts.discard("")
        states = {t.get("state") for t in terms if t.get("state")}
        chambers = {"senate" if t.get("type") == "sen" else "house" for t in terms if t.get("type")}
        last_term = terms[-1]
        bio = ids["bioguide"]
        name_index.setdefault(_last_norm(last), []).append(
            {
                "bioguide": bio,
                "first_norms": firsts,
                "states": states,
                "chambers": chambers,
                "current": is_current,
            }
        )
        member_meta[bio] = {
            "last": last,
            "first": nm.get("first", ""),
            "full_name": nm.get("official_full") or f"{nm.get('first', '')} {last}".strip(),
            "party": last_term.get("party"),
            "chamber": "house" if last_term.get("type") == "rep" else "senate",
            "state": last_term.get("state"),
            "committees": by_member.get(bio, []),
        }
    return name_index, member_meta


def match(
    last: str, first: str, state: str, name_index: dict, chamber: str | None = None
) -> str | None:
    """Best bioguide for a filer. Disambiguate by state (when known), then the filing's
    chamber, then first name, then prefer a currently-serving legislator. Senate filers lack
    a state and the eFD uses nicknames/legal names inconsistently, so chamber + current-term
    preference does the heavy lifting there."""
    cands = name_index.get(_last_norm(last))
    if not cands:
        return None
    pool = [c for c in cands if state in c["states"]] or cands
    if chamber:  # a Senate filing should match a senator, not a same-named representative
        pool = [c for c in pool if chamber in c["chambers"]] or pool
    if len(pool) == 1:
        return pool[0]["bioguide"]
    # disambiguate by first name (the eFD often gives "A. Mitchell" — try each token)
    toks = [_norm(t) for t in first.split() if _norm(t)]
    narrowed = [c for c in pool if any(t in c["first_norms"] for t in toks)]
    cand = narrowed or pool
    if len(cand) == 1:
        return cand[0]["bioguide"]
    currents = [c for c in cand if c["current"]]  # the filer is in office now -> prefer current
    if len(currents) == 1:
        return currents[0]["bioguide"]
    return None  # still ambiguous -> leave unmatched


# --- persistence ------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    bioguide  text PRIMARY KEY,
    last      text, first text, full_name text,
    party     text, chamber text, state text,
    committees jsonb
);
ALTER TABLE filings ADD COLUMN IF NOT EXISTS bioguide text;
"""


def enrich(conn) -> dict:
    """Match every distinct filer, store members, and stamp filings.bioguide."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
    conn.commit()
    name_index, meta = build_rosters()

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT last, first, state_dst, chamber FROM filings")
        filers = cur.fetchall()

    matched_bios: set[str] = set()
    n_matched = 0
    with conn.cursor() as cur:
        for last, first, state_dst, chamber in filers:
            m_st = re.match(r"[A-Za-z]{2}", state_dst or "")
            state = m_st.group(0).upper() if m_st else ""
            bio = match(last or "", first or "", state, name_index, chamber)
            if not bio:
                continue
            n_matched += 1
            matched_bios.add(bio)
            cur.execute(
                "UPDATE filings SET bioguide=%s WHERE last=%s AND first=%s AND state_dst=%s "
                "AND chamber IS NOT DISTINCT FROM %s",
                (bio, last, first, state_dst, chamber),
            )
        for bio in matched_bios:
            m = meta[bio]
            cur.execute(
                "INSERT INTO members (bioguide,last,first,full_name,party,chamber,state,"
                "committees) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (bioguide) DO UPDATE SET "
                "committees=EXCLUDED.committees, party=EXCLUDED.party",
                (
                    bio,
                    m["last"],
                    m["first"],
                    m["full_name"],
                    m["party"],
                    m["chamber"],
                    m["state"],
                    json.dumps(m["committees"]),
                ),
            )
    conn.commit()
    return {"filers": len(filers), "matched": n_matched, "members": len(matched_bios)}


if __name__ == "__main__":
    conn = store.connect()
    if conn:
        print(enrich(conn))
        conn.close()
