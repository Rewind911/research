#!/usr/bin/env python3
"""Fetch Papertrade's daily numbers and write the site's data files.

Source: DefiLlama's feed of Papertrade's on-chain revenue, staker payouts and money on platform
(the same source the 14 Oct 2026 model was built from). Papertrade has no public stats API.

Writes, relative to the repo root:
  data/papertrade.json   the full daily series (complete UTC days only) plus "today so far"
  data/snapshots.csv     one row per run: what the feed said on that day (an audit trail, since
                         DefiLlama revises past days)
Runs daily from .github/workflows/update-data.yml at 06:15 UTC (10:15 Dubai). Standard library only.
Fails closed: if the feed is missing or malformed, nothing is written and the exit code is 1.
"""
import csv, json, os, sys, datetime as dt, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SRC = {
    "revenue": "https://api.llama.fi/summary/fees/papertrade?dataType=dailyRevenue",
    "holders": "https://api.llama.fi/summary/fees/papertrade?dataType=dailyHoldersRevenue",
    "tvl": "https://api.llama.fi/protocol/papertrade",
}
FIELDS = ("revenue", "holders", "tvl")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "research.2rewind.com daily data fetch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def day(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat()


def num(v):
    return v if isinstance(v, (int, float)) and v >= 0 else None


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    rev, hold, prot = get(SRC["revenue"]), get(SRC["holders"]), get(SRC["tvl"])

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
          f"{len(rows)} snapshot rows")


if __name__ == "__main__":
    main()
