"""
Weekly large-quote competitiveness report from data/quotes_log_v3.csv.

Default window: rolling last 7 days ending now (UTC). `--week 2026-W40` still gives an ISO week.

Section 1 (overview, quote_overview_<label>.csv)
  One row per pair (both directions pooled), an "All pairs" total, and one row per direction.
  A sample = one run-slot x one direction x one size. Tokenlon v5 wins a sample when its net USD received
  >= the best competitor's (venues outside TOKENLON_VENUES / EXCLUDE_FROM_BEST). prefee_* judges v5's amount
  before its own fee against the same competitor: net vs pre-fee difference = fee effect; pre-fee gap =
  route / liquidity effect. Status is given twice (net and pre-fee) using the template bands
  (green >= 60%, yellow 50-60%, red < 50%, on the $50K win rate). low_n flags $50K cells with < MIN_N samples.

Section 2 (price impact, price_impact_<label>.csv)
  Per venue, how much worse execution gets from $10K to $50K / $100K, in bps (positive = worse).
  Measured within the same run and direction as gross_bps(10K) - gross_bps(size), where
  gross_bps = (net_usd + gas_usd - in_usd) / in_usd. Gas is added back so falling gas share at larger sizes
  does not look like better pricing; the reference-price bias cancels because both sizes share one run.

De-duplication: runs closer together than --slot-hours (default 3h, same UTC slot) count once; the latest
sample per (slot, direction, size) is kept.

Usage:
  python3 report.py                     # rolling 7 days to now
  python3 report.py --days 7 --end "2026-10-09 23:59"
  python3 report.py --week 2026-W40
  python3 report.py --list-weeks
Writes data/reports/quote_overview_<label>.csv, price_impact_<label>.csv, quote_summary_<label>.json
"""
import argparse
import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from quote_probe import DATA_DIR, EXCLUDE_FROM_BEST, LOG_FILE, PAIRS, SIZES_USD, TOKENLON_VENUES

TOKENLON = "Tokenlon v5"
BASE_SIZE = 10_000
CORE_SIZE = 50_000
TARGET_WIN_PCT = 60.0
STATUS_BANDS = [(60.0, "green"), (50.0, "yellow"), (0.0, "red")]   # by $50K win rate
MIN_N = 10
IMPACT_VENUES = ["Tokenlon v5", "Tokenlon v6", "CoW Swap", "ParaSwap", "1inch", "Uniswap v3"]
REPORT_DIR = os.path.join(DATA_DIR, "reports")
FMT = "%Y-%m-%d %H:%M:%S"

CANONICAL = {f"{a}_{b}": f"{a}/{b}" for a, b in PAIRS} | {f"{b}_{a}": f"{a}/{b}" for a, b in PAIRS}


def parse_ts(ts):
    return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)


def iso_week(ts):
    y, w, _ = parse_ts(ts).isocalendar()
    return f"{y}-W{w:02d}"


def fnum(x):
    return float(x) if x not in ("", None) else None


def load_rows(include_mock=False):
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    mock = sum(1 for r in rows if r.get("mock") == "1")
    if not include_mock:
        rows = [r for r in rows if r.get("mock") != "1"]
    return [r for r in rows if r["pair"] in CANONICAL], mock


def build_samples(rows, slot_hours):
    """De-duplicate by slot, then judge each (slot, direction, size) sample."""
    slot_sec = slot_hours * 3600
    cells = defaultdict(lambda: defaultdict(dict))        # (slot, dir, size) -> ts -> venue -> row
    for r in rows:
        slot = int(parse_ts(r["ts_utc"]).timestamp() // slot_sec)
        cells[(slot, r["pair"], int(r["size_usd"]))][r["ts_utc"]][r["venue"]] = r

    samples = []
    for (slot, direction, size), by_ts in cells.items():
        ts = max(by_ts)                                    # latest run in the slot
        venues = by_ts[ts]
        net = {v: fnum(r["net_usd"]) for v, r in venues.items()}
        comps = {v: n for v, n in net.items()
                 if v not in TOKENLON_VENUES and v not in EXCLUDE_FROM_BEST and n is not None}
        if not comps:
            continue
        best_venue = max(comps, key=comps.get)
        best = comps[best_venue]
        gap = lambda x: round((x - best) / best * 1e4, 2) if x is not None else None
        v5 = venues.get(TOKENLON)
        v5_net = fnum(v5["net_usd"]) if v5 else None
        v5_pre = fnum(v5["pre_fee_net_usd"]) if v5 else None
        gross = {}
        for v, r in venues.items():
            n, g, i = fnum(r["net_usd"]), fnum(r["gas_usd"]) or 0.0, fnum(r["in_usd"])
            if n is not None and i:
                gross[v] = (n + g - i) / i * 1e4
        samples.append({"slot": slot, "ts": ts, "direction": direction, "pair": CANONICAL[direction], "size": size,
                        "best_comp": best_venue, "gap_bps": gap(v5_net),
                        "prefee_gap_bps": gap(v5_pre) if v5_net is not None else None, "gross": gross})
    return samples


def pct(samples, key):
    vals = [s[key] for s in samples if s[key] is not None]
    return round(100 * sum(v >= 0 for v in vals) / len(vals), 1) if vals else ""


def status(win):
    return next(s for floor, s in STATUS_BANDS if win >= floor) if win != "" else "n/a"


def summarise(samples, scope, label):
    row = {"scope": scope, "pair": label, "runs": len({s["slot"] for s in samples})}
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
    core_pre = [s["prefee_gap_bps"] for s in quoted if s["size"] == CORE_SIZE and s["prefee_gap_bps"] is not None]
    row["avg_gap_50k_bps"] = round(statistics.mean(core), 2) if core else ""
    row["median_gap_50k_bps"] = round(statistics.median(core), 2) if core else ""
    row["avg_prefee_gap_50k_bps"] = round(statistics.mean(core_pre), 2) if core_pre else ""
    row["status_net"] = status(row["win_50k_pct"])
    row["status_prefee"] = status(row["prefee_win_50k_pct"])
    row["low_n"] = int(row["n_50k"] < MIN_N)
    row["v5_no_quote"] = len(samples) - len(quoted)
    return row


def impact_rows(samples, scope, label):
    """gross_bps(10K) - gross_bps(size) per venue, paired within (slot, direction)."""
    by_key = defaultdict(dict)
    for s in samples:
        by_key[(s["slot"], s["direction"])][s["size"]] = s["gross"]
    out = []
    for v in IMPACT_VENUES:
        row = {"scope": scope, "pair": label, "venue": v}
        for size in SIZES_USD:
            if size == BASE_SIZE:
                continue
            diffs = [g[BASE_SIZE][v] - g[size][v] for g in by_key.values()
                     if BASE_SIZE in g and size in g and v in g[BASE_SIZE] and v in g[size]]
            k = f"{size // 1000}k"
            row[f"n_{k}"] = len(diffs)
            row[f"impact_{k}_bps"] = round(statistics.mean(diffs), 2) if diffs else ""
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="rolling window length (default 7)")
    ap.add_argument("--end", help='window end, UTC "YYYY-MM-DD HH:MM" (default: now)')
    ap.add_argument("--week", help="ISO week instead of rolling window, e.g. 2026-W40")
    ap.add_argument("--list-weeks", action="store_true")
    ap.add_argument("--slot-hours", type=float, default=3, help="runs within the same slot count once (default 3)")
    ap.add_argument("--include-mock", action="store_true", help="include --mock rows (testing only)")
    args = ap.parse_args()

    if not os.path.exists(LOG_FILE):
        raise SystemExit(f"no log yet: {LOG_FILE} (run quote_probe.py first)")
    rows, mock_rows = load_rows(args.include_mock)

    if args.list_weeks:
        for wk, n in sorted(Counter(iso_week(r["ts_utc"]) for r in rows).items()):
            print(f"{wk}  runs={len({r['ts_utc'] for r in rows if iso_week(r['ts_utc']) == wk})}  rows={n}")
        return

    if args.week:
        y, w = args.week.split("-W")
        start = datetime.combine(date.fromisocalendar(int(y), int(w), 1), datetime.min.time(), timezone.utc)
        end = start + timedelta(days=7) - timedelta(seconds=1)
        label = args.week
    else:
        end = (datetime.strptime(args.end, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
               if args.end else datetime.now(timezone.utc))
        start = end - timedelta(days=args.days)
        label = f"{start:%Y%m%d}-{end:%Y%m%d}"

    win_rows = [r for r in rows if start <= parse_ts(r["ts_utc"]) <= end]
    if not win_rows:
        raise SystemExit(f"no live rows between {start:%Y-%m-%d %H:%M} and {end:%Y-%m-%d %H:%M} UTC")
    samples = build_samples(win_rows, args.slot_hours)

    overview = [summarise([s for s in samples if s["pair"] == f"{a}/{b}"], "pair", f"{a}/{b}") for a, b in PAIRS]
    overview.append(summarise(samples, "total", "All pairs"))
    for a, b in PAIRS:
        for d in (f"{a}_{b}", f"{b}_{a}"):
            overview.append(summarise([s for s in samples if s["direction"] == d], "direction", d))

    impact = []
    for a, b in PAIRS:
        impact += impact_rows([s for s in samples if s["pair"] == f"{a}/{b}"], "pair", f"{a}/{b}")
    impact += impact_rows(samples, "total", "All pairs")
    for a, b in PAIRS:
        for d in (f"{a}_{b}", f"{b}_{a}"):
            impact += impact_rows([s for s in samples if s["direction"] == d], "direction", d)

    raw_runs = sorted({r["ts_utc"] for r in win_rows})
    errors = Counter((r["venue"], r["error"][:60]) for r in win_rows if r["error"])
    summary = {
        "label": label, "window_start_utc": f"{start:%Y-%m-%d %H:%M}", "window_end_utc": f"{end:%Y-%m-%d %H:%M}",
        "raw_runs": len(raw_runs), "run_timestamps": raw_runs, "runs_after_dedupe": len({s["slot"] for s in samples}),
        "slot_hours": args.slot_hours, "samples": len(samples), "mock_rows_in_log_excluded": mock_rows,
        "min_n": MIN_N, "target_win_pct": TARGET_WIN_PCT, "competitors_excluded_from_best": sorted(EXCLUDE_FROM_BEST),
        "errors": [{"venue": v, "error": e, "count": n} for (v, e), n in errors.most_common()],
    }

    os.makedirs(REPORT_DIR, exist_ok=True)
    ov_cols = ["scope", "pair", "runs"] + \
        [f"{c}_{s // 1000}k{p}" for s in SIZES_USD for c, p in (("n", ""), ("win", "_pct"), ("prefee_win", "_pct"))] + \
        ["main_loss_to", "main_loss_share_pct", "avg_gap_50k_bps", "median_gap_50k_bps", "avg_prefee_gap_50k_bps",
         "status_net", "status_prefee", "low_n", "v5_no_quote"]
    paths = {}
    for name, data, cols in (("quote_overview", overview, ov_cols),
                             ("price_impact", impact, list(impact[0].keys()))):
        paths[name] = os.path.join(REPORT_DIR, f"{name}_{label}.csv")
        with open(paths[name], "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(data)
    paths["summary"] = os.path.join(REPORT_DIR, f"quote_summary_{label}.json")
    with open(paths["summary"], "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    # ---- console
    total = next(r for r in overview if r["scope"] == "total")
    print(f"{label}  {summary['window_start_utc']} to {summary['window_end_utc']} UTC  "
          f"runs={summary['raw_runs']} (after {args.slot_hours:g}h de-dup: {summary['runs_after_dedupe']})  "
          f"samples={summary['samples']}")
    print(f"Tokenlon v5 $50K win rate: net {total['win_50k_pct']}% / pre-fee {total['prefee_win_50k_pct']}%  "
          f"(target {TARGET_WIN_PCT:.0f}%)")
    print("\n[1] overview   win % net (pre-fee)   * = n < MIN_N at $50K")
    print(f"{'pair':<11} {'$10K':>13} {'$50K':>13} {'$100K':>13}  {'main loss to':<20} {'$50K gap net (pre)':>22}  status net/pre")
    for r in overview:
        if r["scope"] == "direction":
            continue
        loss = f"{r['main_loss_to']} ({r['main_loss_share_pct']}%)" if r["main_loss_to"] else "-"
        gap = (f"{r['avg_gap_50k_bps']:+.1f} ({r['avg_prefee_gap_50k_bps']:+.1f}) bps"
               if r["avg_prefee_gap_50k_bps"] != "" else "-")
        wins = [f"{r[f'win_{k}_pct']} ({r[f'prefee_win_{k}_pct']})" for k in ("10k", "50k", "100k")]
        flag = "*" if r["low_n"] else ""
        print(f"{r['pair'] + flag:<11} {wins[0]:>13} {wins[1]:>13} {wins[2]:>13}  {loss:<20} {gap:>22}  "
              f"{r['status_net']}/{r['status_prefee']}")
    print("\n[2] price impact, bps worse than $10K (all pairs)")
    for r in impact:
        if r["scope"] == "total":
            print(f"  {r['venue']:<12} $50K {r['impact_50k_bps']!s:>7} (n={r['n_50k']})   "
                  f"$100K {r['impact_100k_bps']!s:>7} (n={r['n_100k']})")
    if errors:
        print("\nerrors in window:")
        for (v, e), n in errors.most_common():
            print(f"  {n:>3}  {v}: {e}")
    for p in paths.values():
        print(f"-> {p}")


if __name__ == "__main__":
    main()
