#!/usr/bin/env python3
"""
Arkham -> Telegram whale alerts (GitHub Actions edition).

Runs ONCE per invocation: checks the last N minutes of transfers,
sends anything new to Telegram, then exits. A scheduled workflow
calls it every 10 minutes, so no server is needed.

Set these as GitHub repo Secrets:
    ARKHAM_API_KEY
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Set these as GitHub repo Variables (all optional):
    MIN_USD             default 10000
    LOOKBACK_MINUTES    default 12   (a bit more than the schedule gap)
    CHAINS              e.g. ethereum,base,solana   (blank = all)
    ONLY_TOKENS         e.g. bitcoin,ethereum,pepe  (blank = all)
    SKIP_STABLES        "true" (default) or "false"
    MAX_ALERTS          default 8
"""

import os
import sys
import html
import time

import requests

ARKHAM_KEY = os.environ.get("ARKHAM_API_KEY")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

missing = [
    n for n, v in [
        ("ARKHAM_API_KEY", ARKHAM_KEY),
        ("TELEGRAM_BOT_TOKEN", TG_TOKEN),
        ("TELEGRAM_CHAT_ID", TG_CHAT),
    ] if not v
]
if missing:
    print("ERROR: missing secrets: " + ", ".join(missing))
    sys.exit(1)

MIN_USD = float(os.environ.get("MIN_USD") or 10000)
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MINUTES") or 130)
CHAINS = (os.environ.get("CHAINS") or "").strip()
ONLY_TOKENS = (os.environ.get("ONLY_TOKENS") or "").strip()
SKIP_STABLES = (os.environ.get("SKIP_STABLES") or "true").lower() != "false"
MAX_ALERTS = int(os.environ.get("MAX_ALERTS") or 8)

STABLES = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDE", "SUSDE", "USDS", "SUSDS",
    "FDUSD", "PYUSD", "USDD", "FRAX", "LUSD", "GUSD", "USDP", "USD1",
    "USDB", "CRVUSD", "GHO", "MIM", "EURC", "EURT", "USDF", "USDY",
    "USDC.E", "USDT.E", "AUSDC", "AUSDT", "CUSDC", "CUSDT", "WETH", "AERO", "VVV",
}

ARKHAM_URL = "https://api.arkm.com/transfers"
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

# File that survives between runs via actions/cache, holding transfer ids
# we've already alerted on so a schedule overlap doesn't double-notify.
STATE_FILE = "seen_ids.txt"
STATE_MAX = 3000


def load_seen():
    try:
        with open(STATE_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def save_seen(ids):
    trimmed = list(ids)[-STATE_MAX:]
    with open(STATE_FILE, "w") as f:
        f.write("\n".join(trimmed))


def transfer_id(t):
    for k in ("id", "transferID", "transferId"):
        if t.get(k):
            return str(t[k])
    tx = t.get("transactionHash") or t.get("txHash") or ""
    return "{}:{}:{}".format(tx, t.get("tokenAddress", ""), t.get("unitValue", ""))


def name_of(side):
    if not isinstance(side, dict):
        return "unknown"
    for key in ("arkhamEntity", "arkhamLabel"):
        obj = side.get(key) or {}
        if obj.get("name"):
            return obj["name"]
    addr = side.get("address") or "unknown"
    return addr[:6] + "…" + addr[-4:] if len(addr) > 12 else addr


def format_alert(t):
    usd = t.get("historicalUSD") or t.get("usd") or 0
    sym = (t.get("tokenSymbol") or t.get("tokenName") or "?").upper()
    amt = t.get("unitValue")
    chain = t.get("chain", "?")
    src = html.escape(str(name_of(t.get("fromAddress"))))
    dst = html.escape(str(name_of(t.get("toAddress"))))
    tx = t.get("transactionHash") or t.get("txHash") or ""

    amt_str = f"{amt:,.4g} " if isinstance(amt, (int, float)) else ""
    out = (
        f"🐋 <b>${usd:,.0f}</b>  {amt_str}{html.escape(sym)}"
        f"  <i>({html.escape(str(chain))})</i>\n{src} → {dst}"
    )
    if tx:
        out += f"\n<code>{html.escape(str(tx)[:24])}…</code>"
    return out


def send(text):
    r = requests.post(
        TG_URL,
        json={
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not r.ok:
        print(f"telegram error {r.status_code}: {r.text[:300]}")


def main():
    since_ms = int((time.time() - LOOKBACK_MIN * 60) * 1000)

    params = {
        "usdGte": MIN_USD,
        "timeGte": since_ms,
        "sortKey": "time",
        "sortDir": "desc",
        "limit": 250,
        "flow": "all",
    }
    if CHAINS:
        params["chains"] = CHAINS
    if ONLY_TOKENS:
        params["tokens"] = ONLY_TOKENS

    r = requests.get(
        ARKHAM_URL, params=params, headers={"API-Key": ARKHAM_KEY}, timeout=45
    )
    if r.status_code in (401, 403):
        print("Arkham rejected the API key.")
        send("⚠️ Arkham rejected the API key — check ARKHAM_API_KEY.")
        sys.exit(1)
    r.raise_for_status()

    data = r.json()
    transfers = data.get("transfers") or data.get("data") or [] if isinstance(data, dict) else (data or [])
    print(f"fetched {len(transfers)} transfers >= ${MIN_USD:,.0f} in last {LOOKBACK_MIN}m")

    seen = load_seen()
    fresh = []
    for t in transfers:
        sym = (t.get("tokenSymbol") or "").upper()
        if SKIP_STABLES and sym in STABLES:
            continue
        tid = transfer_id(t)
        if tid in seen:
            continue
        seen.add(tid)
        fresh.append(t)

    if fresh:
        fresh.sort(key=lambda x: x.get("historicalUSD") or 0, reverse=True)
        print(f"{len(fresh)} new alerts")
        batch = fresh[:MAX_ALERTS]
        body = "\n\n".join(format_alert(t) for t in batch)
        if len(fresh) > MAX_ALERTS:
            body += f"\n\n…plus {len(fresh) - MAX_ALERTS} more in this window."
        send(body)
    else:
        print("nothing new")

    save_seen(seen)


if __name__ == "__main__":
    main()
