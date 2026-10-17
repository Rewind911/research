#!/usr/bin/env python3
"""Fetch Papertrade's daily numbers and write the site's data files.

Sources: DefiLlama's feed of Papertrade's on-chain revenue, staker payouts and money on platform
(the same source the 14 Oct 2026 model was built from), and, since 17 Oct 2026, PaperDash's public
indexer of the live contract (payout cap, pool, queue, supply, staked, the mint curve's high-water
mark) so the page can mark its own calls hit or missed. Papertrade has no public stats API.

Writes, relative to the repo root:
  data/papertrade.json   the full daily series (complete UTC days only) plus "today so far"
  data/snapshots.csv     one row per run: what the feed said on that day (an audit trail, since
                         DefiLlama revises past days)
Runs daily from .github/workflows/update-data.yml at 06:15 UTC (10:15 Dubai). Standard library only.
Fails closed: if the DefiLlama feed is missing or malformed, nothing is written and the exit code is 1.
The contract read is additive: if PaperDash is down, the previous contract history is kept, the
latest snapshot is marked stale, and nothing is invented.
"""
import csv, json, os, sys, datetime as dt, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SRC = {
    "revenue": "https://api.llama.fi/summary/fees/papertrade?dataType=dailyRevenue",
    "holders": "https://api.llama.fi/summary/fees/papertrade?dataType=dailyHoldersRevenue",
    "tvl": "https://api.llama.fi/protocol/papertrade",
    "contract": "https://paperdash-api.onrender.com/api/overview",
}
WEI = 10 ** 18
CONTRACT_KEEP = 400  # days of contract snapshots kept in the data file
FIELDS = ("revenue", "holders", "tvl")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "research.2rewind.com daily data fetch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def day(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat()


def num(v):
    return v if isinstance(v, (int, float)) and v >= 0 else None


def contract_snapshot(now):
    """Read the live contract through PaperDash. Returns a snapshot dict or None (never raises)."""
    try:
        o = get(SRC["contract"])
        p = (o.get("summary") or {}).get("paper") or {}
        b = (o.get("summary") or {}).get("balances") or {}
        def wei(x):
            return int(x) / WEI if x is not None else None
        tail = wei(p.get("tailProgress"))
        decay = wei(p.get("decay")) or 120e6
        rate = wei(p.get("rate")) or 100.0
        snap = {
            "asof": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": now.date().isoformat(),
            "cap": wei(p.get("cap")), "lp": wei(p.get("trackedLp")), "queue": wei(b.get("queue")),
            "supply": wei(p.get("supply")), "staked": wei(p.get("staked")), "tail": tail,
            "mint_rate": (rate * (decay / (decay + tail)) ** 2) if tail is not None else None,
            "margin": wei(b.get("margin")), "stale": False,
        }
        if snap["cap"] is None or snap["supply"] is None:
            return None
        return snap
    except Exception as e:  # noqa: BLE001 - any failure means "no fresh read", never a guess
        print(f"contract read failed: {e}", file=sys.stderr)
        return None


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    rev, hold, prot = get(SRC["revenue"]), get(SRC["holders"]), get(SRC["tvl"])
    previous = {}
    prev_path = os.path.join(DATA, "papertrade.json")
    if os.path.exists(prev_path):
        try:
            with open(prev_path) as f:
                previous = json.load(f)
        except Exception:
            previous = {}
    history = [h for h in (previous.get("contract_history") or []) if isinstance(h, dict) and h.get("date")]
    snap = contract_snapshot(now)
    if snap:
        history = [h for h in history if h["date"] != today] + [snap]
        contract = snap
    else:
        contract = dict(history[-1], stale=True) if history else None
    history = history[-CONTRACT_KEEP:]

    days = {}
    for t, v in rev.get("totalDataChart") or []:
        days.setdefault(day(t), {})["revenue"] = num(v)
    for t, v in hold.get("totalDataChart") or []:
        days.setdefault(day(t), {})["holders"] = num(v)
    for x in prot.get("tvl") or []:  # several points per day; the last one wins
        days.setdefault(day(x["date"]), {})["tvl"] = num(x.get("totalLiquidityUSD"))

    complete = [d for d in sorted(days) if d < today and days[d].get("revenue") is not None]
    if not complete:
        sys.exit("fetch failed: no complete day with revenue in the feed; nothing written")
    partial = days.get(today)

    series = [{"date": d, **{k: days[d].get(k) for k in FIELDS}} for d in complete]
    last7 = [x["revenue"] for x in series[-7:]]
    chain = prot.get("currentChainTvls") or {}
    out = {
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_through": complete[-1],
        "daily": series,
        "today_so_far": ({"date": today, **{k: partial.get(k) for k in FIELDS}} if partial else None),
        "totals": {
            "revenue_all_time": num(rev.get("totalAllTime")),
            "holders_all_time": num(hold.get("totalAllTime")),
            "revenue_24h": num(rev.get("total24h")),
            "holders_24h": num(hold.get("total24h")),
            "tvl_now": num(sum(v for v in chain.values() if isinstance(v, (int, float)))) if chain else None,
        },
        "sources": SRC,
        "contract": contract,
        "contract_history": history,
    }

    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "papertrade.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
        f.write("\n")

    # Snapshot row for this run; a re-run on the same UTC day replaces that day's row.
    path = os.path.join(DATA, "snapshots.csv")
    cols = ["fetched_at_utc", "data_through", "last_day_revenue", "last_day_holders", "last_day_tvl",
            "revenue_7d_avg", "revenue_all_time", "holders_all_time", "tvl_now"]
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if not r["fetched_at_utc"].startswith(today)]
    last = series[-1]
    rows.append({
        "fetched_at_utc": out["fetched_at"], "data_through": out["data_through"],
        "last_day_revenue": last["revenue"], "last_day_holders": last["holders"], "last_day_tvl": last["tvl"],
        "revenue_7d_avg": round(sum(last7) / len(last7)),
        "revenue_all_time": out["totals"]["revenue_all_time"], "holders_all_time": out["totals"]["holders_all_time"],
        "tvl_now": out["totals"]["tvl_now"],
    })
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"ok: {len(series)} complete days through {out['data_through']}; last day revenue {last['revenue']:,}; "
          f"{len(rows)} snapshot rows; contract {'fresh' if snap else ('stale' if contract else 'none')}"
          + (f" (cap {contract['cap']:,.0f}, supply {contract['supply']/1e9:.2f}B, mint rate {contract['mint_rate']:.1f})" if contract else ""))


if __name__ == "__main__":
    main()
