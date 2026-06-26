"""Broadcast channels for the daily job.

  Channel 1 — personal Telegram: a short summary every run (pipeline + any signals).
  Channel 2 — friends email: ONLY when there's a buy/sell, to the distribution list, with the
              signal details, instructions, a portfolio overview, and a not-advice disclaimer.

Telegram uses TELEGRAM_BOT_TOKEN + CHAT_ID. Email uses the Loops transactional HTTPS API
(LOOPS_API_KEY + LOOPS_TRANSACTIONAL_ID, reusing forkbeard's Loops account) because the droplet
blocks outbound SMTP; if either is absent the email step logs and skips rather than failing the
run, so it wires up the moment the creds land.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

CHAT_ID = "8551999515"
EMAIL_FROM = "carter.bastian1@gmail.com"
EMAIL_TO = [
    "carter.bastian1@gmail.com",
    "Mdmmmorgan@gmail.com",
    "qsbastian@gmail.com",
    "Huntjames23@gmail.com",
    "tom.demichele1@gmail.com",
]
DISCLAIMER = (
    "DISCLAIMER: This is not investment advice. It is provided for educational purposes only "
    "as a description of an algorithmic stock-trading strategy and its simulated results — not a "
    "recommendation of any security or of a strategy to follow. Do your own research."
)


def telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("[notify] no TELEGRAM_BOT_TOKEN — skipping telegram", flush=True)
        return False
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30):  # noqa: S310
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify] telegram failed: {type(e).__name__}: {e}", flush=True)
        return False


LOOPS_URL = "https://app.loops.so/api/v1/transactional"


def email(subject: str, body: str, to=None) -> bool:
    """Send via Loops' transactional HTTPS API (reuses forkbeard's Loops account; the droplet
    blocks SMTP). Loops needs a pre-made transactional TEMPLATE (LOOPS_TRANSACTIONAL_ID) with
    `subject` + `body` data variables; we send it 1:1 to each recipient. Skips if unconfigured."""
    key = os.environ.get("LOOPS_API_KEY")
    tid = os.environ.get("LOOPS_TRANSACTIONAL_ID")
    to = to or EMAIL_TO
    if not (key and tid):
        miss = "LOOPS_API_KEY" if not key else "LOOPS_TRANSACTIONAL_ID"
        print(f"[notify] no {miss} — would email {len(to)}: {subject!r}", flush=True)
        return False
    ok = 0
    for addr in to:
        payload = json.dumps(
            {
                "transactionalId": tid,
                "email": addr,
                "dataVariables": {"subject": subject, "body": body},
            }
        ).encode()
        req = urllib.request.Request(
            LOOPS_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # Loops sits behind Cloudflare, which 403s (error 1010) on urllib's default
                # User-Agent; a named UA clears the bot-signature block.
                "User-Agent": "blackbox-insider-trader/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                ok += r.status in (200, 201, 202)
        except Exception as e:  # noqa: BLE001
            print(f"[notify] loops failed for {addr}: {type(e).__name__}: {e}", flush=True)
    print(f"[notify] emailed {ok}/{len(to)} via Loops", flush=True)
    return ok > 0


def _tier_label(t):
    return {1: "$50", 2: "$100", 3: "$100 + 1yr ATM calls"}.get(t, "?")


def run_summary(stats: dict, buys: list, sells: list, port: dict | None) -> str:
    """Channel-1 Telegram text: pipeline + signals."""
    lines = [
        f"📊 Insider Trader daily run — {stats.get('date')}",
        f"Pulled: +{stats.get('house', 0)} House, +{stats.get('senate', 0)} Senate trades.",
        f"Pipeline: {stats.get('candidates', 0)} growth+proven candidates scanned → "
        f"{len(buys)} cleared Phase-1 (run-up) and were scored by the LLM.",
    ]
    if buys:
        lines.append("\n🟢 BUY signals:")
        for b in buys:
            lines.append(
                f"  • {b['ticker']} (copy {b['member']}) — signal {b['signal']}, "
                f"tier {b['tier']} → {_tier_label(b['tier'])}, run-up {b['runup'] * 100:+.0f}%"
            )
    if sells:
        lines.append("\n🔴 SELL signals:")
        for s in sells:
            lines.append(f"  • {s['ticker']} (copy {s['member']})")
    if not buys and not sells:
        lines.append("\nNo new buy/sell signals today.")
    if port and port["personal"] > 0:
        lines.append(
            f"\nPaper portfolio: ${port['total']:,.0f} on ${port['personal']:,.0f} in "
            f"({(port['total'] / port['personal'] - 1) * 100:+.0f}%), "
            f"{port['open']} open / {port['closed']} closed."
        )
    elif port:
        lines.append("\nPaper portfolio: empty — no positions opened yet.")
    return "\n".join(lines)


def signal_email_body(buys: list, sells: list, port: dict | None) -> str:
    parts = ["A new congressional-trade signal has fired in the Insider Trader strategy.\n"]
    for b in buys:
        parts.append(
            f"BUY: {b['ticker']}  (mirroring Rep./Sen. {b['member']})\n"
            f"  Why: passed our quantitative filter (growth sector, a member with a proven "
            f"track record, and the stock up {b['runup'] * 100:+.0f}% into the public disclosure), "
            f"then scored {b['signal']}/100 by the model.\n"
            f"  Instruction: {_tier_label(b['tier'])} (Tier {b['tier']}).\n"
        )
    for s in sells:
        parts.append(
            f"SELL: {s['ticker']}  (mirroring {s['member']}) — exit the position "
            "(the member sold, or the 18-month hold elapsed).\n"
        )
    if port and port["personal"] > 0:
        parts.append(
            f"\n— Paper portfolio (for anyone following along) —\n"
            f"Value: ${port['total']:,.2f} on ${port['personal']:,.2f} deployed "
            f"({(port['total'] / port['personal'] - 1) * 100:+.1f}%). "
            f"Open positions: {port['open']}, closed: {port['closed']}.\n"
        )
    parts.append("\n" + DISCLAIMER)
    return "\n".join(parts)
