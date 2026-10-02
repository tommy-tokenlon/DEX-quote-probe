"""
Weekly large-quote competitiveness overview from quotes_log_v3.csv.

Run quote_probe.py at random points through the week; on report day run this to roll the samples
up into one row per pair (both directions pooled), one row per direction, and a total row.

A "sample" = one probe run x one direction x one size. Tokenlon v5 wins a sample when its net USD
received >= the best competitor's (CoW Swap, ParaSwap, Uniswap v3, 1inch). Winners are recomputed
from each venue's net_usd, so older log rows written under different "best" rules are judged the same way.
Samples where v5 returned no quote are left out of the win rate and counted in v5_no_quote.
prefee_* columns judge v5's amount before its own fee (pre_fee_net_usd) against the same best competitor:
the gap between net and pre-fee = fee effect; the pre-fee gap = route / MM pricing effect.

Usage:
  python3 report.py                  # latest ISO week in the log
  python3 report.py --week 2026-W40
  python3 report.py --list-weeks
Writes data/reports/quote_overview_<week>.csv
"""
import argparse
import csv
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime

from quote_probe import DATA_DIR, EXCLUDE_FROM_BEST, LOG_FILE, PAIRS, SIZES_USD, TOKENLON_VENUES

TOKENLON = "Tokenlon v5"
CORE_SIZE = 50_000
TARGET_WIN_PCT = 60.0
STATUS_BANDS = [(60.0, "green"), (50.0, "yellow"), (0.0, "red")]   # by $50K win rate
REPORT_DIR = os.path.join(DATA_DIR, "reports")

CANONICAL = {f"{a}_{b}": f"{a}/{b}" for a, b in PAIRS} | {f"{b}_{a}": f"{a}/{b}" for a, b in PAIRS}


def iso_week(ts):
    y, w, _ = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").isocalendar()
    return f"{y}-W{w:02d}"


def load_samples(include_mock=False):
    """{week: [sample, ...]} where a sample is one (run, direction, size) judged v5 vs best competitor."""
    groups = defaultdict(dict)
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["pair"] not in CANONICAL or (r.get("mock") == "1" and not include_mock):
                continue
            net = float(r["net_usd"]) if r["net_usd"] else None
            pre = float(r["pre_fee_net_usd"]) if r.get("pre_fee_net_usd") else None
            groups[(r["ts_utc"], r["pair"], int(r["size_usd"]))][r["venue"]] = (net, pre)

    by_week = defaultdict(list)
    for (ts, direction, size), venues in groups.items():
        comps = {v: n for v, (n, _) in venues.items()
                 if v not in TOKENLON_VENUES and v not in EXCLUDE_FROM_BEST and n is not None}
        if not comps:
            continue                                   # nothing to compare against
        best_venue = max(comps, key=comps.get)
        best = comps[best_venue]
        v5, v5_pre = venues.get(TOKENLON, (None, None))
        gap = lambda x: round((x - best) / best * 1e4, 2) if x is not None else None
        by_week[iso_week(ts)].append({"ts": ts, "direction": direction, "pair": CANONICAL[direction],
                                      "size": size, "best_comp": best_venue,
                                      "gap_bps": gap(v5), "prefee_gap_bps": gap(v5_pre) if v5 is not None else None})
    return by_week


def pct(samples, key):
    vals = [s[key] for s in samples if s[key] is not None]
    return round(100 * sum(v >= 0 for v in vals) / len(vals), 1) if vals else ""


def summarise(samples, week, scope, label):
    row = {"week": week, "scope": scope, "pair": label, "runs": len({s["ts"] for s in samples})}
    quoted = [s for s in samples if s["gap_bps"] is not None]
    for size in SIZES_USD:
        at = [s for s in quoted if s["size"] == size]
        k = f"{size // 1000}k"
        row[f"n_{k}"] = len(at)
        row[f"win_{k}_pct"] = pct(at, "gap_bps")
        row[f"prefee_win_{k}_pct"] = pct(at, "prefee_gap_bps")
    losses = Counter(s["best_comp"] for s in quoted if s["gap_bps"] < 0)
    if losses:
        venue, n = losses.most_common(1)[0]
        row["main_loss_to"], row["main_loss_share_pct"] = venue, round(100 * n / sum(losses.values()), 1)
    else:
        row["main_loss_to"], row["main_loss_share_pct"] = "", ""
    core = [s["gap_bps"] for s in quoted if s["size"] == CORE_SIZE]
    row["avg_gap_50k_bps"] = round(statistics.mean(core), 2) if core else ""
    row["median_gap_50k_bps"] = round(statistics.median(core), 2) if core else ""
    core_pre = [s["prefee_gap_bps"] for s in quoted if s["size"] == CORE_SIZE and s["prefee_gap_bps"] is not None]
    row["avg_prefee_gap_50k_bps"] = round(statistics.mean(core_pre), 2) if core_pre else ""
    win = row[f"win_{CORE_SIZE // 1000}k_pct"]
    row["status"] = next(s for floor, s in STATUS_BANDS if win >= floor) if win != "" else "n/a"
    row["v5_no_quote"] = len(samples) - len(quoted)
    return row


def build(week, samples):
    rows = []
    for a, b in PAIRS:
        label = f"{a}/{b}"
        rows.append(summarise([s for s in samples if s["pair"] == label], week, "pair", label))
    rows.append(summarise(samples, week, "total", "All pairs"))
    for a, b in PAIRS:
        for d in (f"{a}_{b}", f"{b}_{a}"):
            rows.append(summarise([s for s in samples if s["direction"] == d], week, "direction", d))
    y, w = week.split("-W")
    for r in rows:
        r["period_start"] = date.fromisocalendar(int(y), int(w), 1).isoformat()
        r["period_end"] = date.fromisocalendar(int(y), int(w), 7).isoformat()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", help="ISO week, e.g. 2026-W40 (default: latest in the log)")
    ap.add_argument("--list-weeks", action="store_true")
    ap.add_argument("--include-mock", action="store_true", help="include --mock rows (testing only)")
    args = ap.parse_args()

    if not os.path.exists(LOG_FILE):
        raise SystemExit(f"no log yet: {LOG_FILE} (run quote_probe.py first)")
    by_week = load_samples(args.include_mock)
    if not by_week:
        raise SystemExit("no usable samples in the log")
    if args.list_weeks:
        for wk in sorted(by_week):
            print(f"{wk}  runs={len({s['ts'] for s in by_week[wk]})}  samples={len(by_week[wk])}")
        return
    week = args.week or max(by_week)
    if week not in by_week:
        raise SystemExit(f"no samples for {week}; have {', '.join(sorted(by_week))}")

    rows = build(week, by_week[week])
    os.makedirs(REPORT_DIR, exist_ok=True)
    out = os.path.join(REPORT_DIR, f"quote_overview_{week}.csv")
    cols = ["week", "period_start", "period_end", "scope", "pair", "runs"] + \
           [f"{c}_{s // 1000}k{p}" for s in SIZES_USD for c, p in (("n", ""), ("win", "_pct"), ("prefee_win", "_pct"))] + \
           ["main_loss_to", "main_loss_share_pct", "avg_gap_50k_bps", "median_gap_50k_bps", "avg_prefee_gap_50k_bps",
            "status", "v5_no_quote"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    total = next(r for r in rows if r["scope"] == "total")
    win50 = total["win_50k_pct"]
    print(f"{week} ({total['period_start']} to {total['period_end']} UTC)  runs={total['runs']}  "
          f"Tokenlon v5 $50K win rate={win50}%  target={TARGET_WIN_PCT:.0f}%"
          + (f"  gap={win50 - TARGET_WIN_PCT:+.1f}pp" if win50 != "" else "")
          + f"  (pre-fee {total['prefee_win_50k_pct']}%)")
    print("\nwin rate % shown as net (pre-fee)")
    print(f"{'pair':<11} {'$10K':>13} {'$50K':>13} {'$100K':>13}  {'main loss to':<20} {'$50K gap net (pre)':>22}  status")
    for r in rows:
        if r["scope"] == "direction":
            continue
        loss = f"{r['main_loss_to']} ({r['main_loss_share_pct']}%)" if r["main_loss_to"] else "-"
        gap = (f"{r['avg_gap_50k_bps']:+.1f} ({r['avg_prefee_gap_50k_bps']:+.1f}) bps"
               if r["avg_prefee_gap_50k_bps"] != "" else "-")
        wins = [f"{r[f'win_{k}_pct']} ({r[f'prefee_win_{k}_pct']})" for k in ("10k", "50k", "100k")]
        print(f"{r['pair']:<11} {wins[0]:>13} {wins[1]:>13} {wins[2]:>13}  {loss:<20} {gap:>22}  {r['status']}")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
