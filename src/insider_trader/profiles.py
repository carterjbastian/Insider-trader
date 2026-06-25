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

_WIKI_API = "https://en.wikipedia.org/w/api.php"
_UA = "blackbox-insider-trader/0.1 (personal research)"
PROFILE_MODEL = "claude-opus-4-8"

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


def _committee_names() -> dict[str, str]:
    """thomas_id -> full name, including subcommittees (keyed parent+sub, e.g. 'SSAF13')."""
    out: dict[str, str] = {}
    for c in _get("committees-current.json"):
        tid = c.get("thomas_id", "")
        if tid:
            out[tid] = c["name"]
        for sub in c.get("subcommittees", []):
            out[tid + sub.get("thomas_id", "")] = f"{c['name']} — {sub.get('name', '')}"
    return out


def _committee_history() -> dict[str, list[dict]]:
    """bioguide -> [{code, name, rank, title}] from current membership (current-only;
    historical committee tenure isn't in this free source — flagged as a point-in-time gap)."""
    membership = _get("committee-membership-current.json")
    names = _committee_names()
    out: dict[str, list[dict]] = {}
    for code, members in membership.items():
        name = names.get(code, code)
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


def wikipedia_text(title: str | None, max_chars: int = 24000, retries: int = 4) -> str | None:
    """Plain-text extract of a member's Wikipedia article. Retries with backoff on 429/transient
    errors and returns None on persistent failure (never raises — a missing narrative just yields
    a spine-only profile rather than killing a batch run)."""
    import time
    import urllib.error

    if not title:
        return None
    q = urllib.parse.urlencode(
        {
            "format": "json",
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "redirects": "1",
            "titles": title,
        }
    )
    req = urllib.request.Request(_WIKI_API + "?" + q, headers={"User-Agent": _UA})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (trusted host)
                data = json.load(r)
            for p in data.get("query", {}).get("pages", {}).values():
                ext = p.get("extract")
                if ext:
                    return ext[:max_chars]
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt >= retries:
                return None
            time.sleep(1.5 * (attempt + 1))  # back off (Wikipedia 429s under concurrency)
    return None


_PROFILE_SYS = """You are building a concise BIOGRAPHICAL dossier on a member of the US \
Congress, to help judge whether their stock trades might ride on professional or personal \
informational advantages a random investor wouldn't have.

From the structured career timeline and the Wikipedia text provided, extract the DURABLE, \
career-defining facts — NOT recent news. Focus on what gives them an information edge:
- Profession(s) and industries BEFORE Congress (law, business, medicine, finance, energy, \
real estate, military, tech, agriculture, etc.) and any companies/firms founded or led.
- Education and professional training.
- Business interests, board memberships, major asset/wealth sources, family business ties.
- Personal/professional relationships tying them to specific industries or companies.
- The economic base of the state/district they represent.

RULES:
- Prefer time-stable facts (pre-Congress career, education, founding a company) over dated \
recent events, so the profile stays valid across the years we analyze.
- Be factual and specific; if something isn't supported by the provided text, omit it.
- `access_synthesis`: 1-2 sentences naming the sectors/industries where this person most \
plausibly has a non-public information edge, given their background + roles."""


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


def _spine_brief(spine: dict) -> str:
    """Compact, dated rendering of the structured spine for the summarizer / dossier."""
    tl = "; ".join(
        f"{t['start'][:4]}–{(t['end'] or '')[:4]} {t['chamber']} {t['state']}"
        f"{('-' + str(t['district'])) if t.get('district') else ''} ({t['party']})"
        for t in spine.get("timeline", [])
    )
    lead = "; ".join(
        f"{r['role']} ({(r['start'] or '')[:4]}–{(r['end'] or '')[:4]})"
        for r in spine.get("leadership_roles", [])
    )
    coms = "; ".join(
        f"{c['name']}{(' [' + c['title'] + ']') if c.get('title') else ''}"
        for c in spine.get("committees_current", [])
    )
    return "\n".join(
        [
            f"Name: {spine.get('full_name')}  (born {spine.get('birthday') or '?'})",
            f"In office: {spine.get('entered_office')} → {spine.get('left_office')} "
            f"({spine.get('n_terms')} terms)",
            f"Service timeline: {tl or 'n/a'}",
            f"Leadership roles (dated): {lead or 'none'}",
            f"Current committees: {coms or 'none on record'}",
        ]
    )


def summarize_profile(spine: dict, wiki_text: str | None, model: str = PROFILE_MODEL):
    """Opus-summarize the spine + Wikipedia into a structured biographical profile."""

    import anthropic
    from pydantic import BaseModel

    class BioProfile(BaseModel):
        pre_congress_career: str  # professions/industries + firms founded/led
        education: str
        business_interests: str  # boards, holdings, wealth sources, family business
        industry_ties: str  # personal/professional relationships to industries/companies
        state_economy: str  # economic base of their state/district
        notable: str  # other durable, edge-relevant facts
        access_synthesis: str  # 1-2 sentences: where they most plausibly have an info edge
        edge_sectors: list[str]  # sectors/industries flagged as plausible info-edge areas

    user = (
        "STRUCTURED CAREER TIMELINE:\n"
        + _spine_brief(spine)
        + "\n\nWIKIPEDIA:\n"
        + (wiki_text or "(no Wikipedia text available)")
    )
    client = anthropic.Anthropic()
    resp = client.messages.parse(
        model=model,
        max_tokens=4000,
        system=_PROFILE_SYS,
        messages=[{"role": "user", "content": user}],
        output_format=BioProfile,
        thinking={"type": "adaptive"},
    )
    if resp.stop_reason == "refusal":
        return None
    return resp.parsed_output


def profile_markdown(spine: dict, bio) -> str:
    """Render the full biographical profile (spine + narrative) for the trade dossier."""
    lines = ["**Career timeline (dated, point-in-time):**", _spine_brief(spine), ""]
    if bio:
        lines += [
            f"**Pre-Congress career:** {bio.pre_congress_career}",
            f"**Education:** {bio.education}",
            f"**Business interests:** {bio.business_interests}",
            f"**Industry ties:** {bio.industry_ties}",
            f"**State/district economy:** {bio.state_economy}",
            f"**Notable:** {bio.notable}",
            f"**Plausible information edge:** {bio.access_synthesis} "
            f"(sectors: {', '.join(bio.edge_sectors) or 'none'})",
        ]
    return "\n".join(lines)


def compute_profile(
    bioguide: str, idx: dict, chist: dict, model: str = PROFILE_MODEL
) -> dict | None:
    """Network-only profile build (spine + Wikipedia + Opus summary). No DB — thread-safe, so a
    batch runner can fan these out concurrently and persist on the main thread afterwards."""
    leg = idx.get(bioguide)
    if not leg:
        return None
    spine = build_spine(leg, chist.get(bioguide, []))
    wiki = wikipedia_text(spine.get("wikipedia"))
    bio = summarize_profile(spine, wiki, model) if wiki else None
    md = profile_markdown(spine, bio)
    return {
        "bioguide": bioguide,
        "spine": spine,
        "wikipedia": spine.get("wikipedia"),
        "profile_md": md,
        "sources": {"wikipedia": spine.get("wikipedia"), "had_wiki": bool(wiki)},
        "model": model,
        "name": spine.get("full_name"),
    }


def build_profile(
    conn, bioguide: str, model: str = PROFILE_MODEL, idx=None, chist=None
) -> dict | None:
    """Full profile for one member: spine + Wikipedia + Opus summary → persist. Returns the row."""
    idx = idx if idx is not None else legislator_index()
    chist = chist if chist is not None else _committee_history()
    p = compute_profile(bioguide, idx, chist, model)
    if not p:
        return None
    save_profile(conn, bioguide, p["spine"], p["wikipedia"], p["profile_md"], p["sources"], model)
    return {
        "bioguide": bioguide,
        "name": p["name"],
        "had_wiki": p["sources"]["had_wiki"],
        "md": p["profile_md"],
    }


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
