"""
Walk-forward blind simulation for INFY (Infosys).

What this does:
  - Downloads all 52 INFY quarterly XBRL filings (Sep 2018 → Dec 2024)
  - Downloads INFY closing prices from bhavcopy archives (Sep 2017 → today)
  - At each quarter-end, builds a snapshot with ONLY data available at that date
  - Scores the snapshot → generates a signal
  - Reveals next quarter's data, computes actual return
  - Tracks: was the signal correct?

First run: downloads ~1800 bhavcopy files + 52 XBRL files → ~3–5 minutes.
Subsequent runs: uses cached data → ~30 seconds.

Cache location: ~/.equity_wf/
"""
import sys, os, json
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.expanduser("~/NSELIB"))

from nselib import NSE
from walkforward.data import (
    build_price_cache, build_xbrl_cache, build_annual_xbrl_cache,
    _load_price_cache, _load_xbrl_cache, _load_annual_xbrl_cache,
    _parse_nse_date,
)
from walkforward.simulator import run_simulation, analyse_results, print_summary

SYMBOL = "INFY"
NAME   = "Infosys Limited"

# Quarter-end dates: derived from XBRL filing dates (Sep 2018 → Dec 2024)
# We build this dynamically from the cache.


def get_quarter_ends(xbrl_records: list) -> list:
    """Extract sorted list of quarter-end dates from XBRL cache."""
    dates = []
    for r in xbrl_records:
        try:
            d = date.fromisoformat(r["period_end"])
            dates.append(d)
        except Exception:
            pass
    return sorted(set(dates))


def main():
    print("="*70)
    print("  INFY Walk-Forward Blind Simulation")
    print("  Rules: at each quarter, only data available at that date is used.")
    print("         No future information leaks in.")
    print("="*70)

    nse = NSE(guard=False)

    # ── Step 1: Build / refresh XBRL cache ──────────────────────────────────
    print("\n[1/3] XBRL financials cache...")
    xbrl_records = build_xbrl_cache(SYMBOL, nse)
    annual_xbrl  = build_annual_xbrl_cache(SYMBOL, nse)
    print(f"      {len(xbrl_records)} quarterly + {len(annual_xbrl)} annual filings cached")

    if not xbrl_records:
        print("ERROR: No XBRL data. Cannot proceed.")
        sys.exit(1)

    # ── Step 2: Build / refresh price cache ─────────────────────────────────
    print("\n[2/3] Historical price cache (bhavcopy)...")
    quarter_ends = get_quarter_ends(xbrl_records)
    # Need prices from 1 year before first quarter to 1 quarter after last
    price_start = quarter_ends[0] - timedelta(days=400)
    price_end   = min(date.today(), quarter_ends[-1] + timedelta(days=100))

    price_cache = build_price_cache(SYMBOL, price_start, price_end, nse, workers=15)
    print(f"      {len(price_cache)} trading days cached")

    if len(price_cache) < 50:
        print("WARNING: Very few price data points. Results may be limited.")

    # ── Step 3: Run walk-forward simulation ──────────────────────────────────
    print("\n[3/3] Running walk-forward simulation...")
    results = run_simulation(
        symbol       = SYMBOL,
        name         = NAME,
        quarters     = quarter_ends,
        price_cache  = price_cache,
        xbrl_records = xbrl_records,
        annual_xbrl  = annual_xbrl,
    )

    # ── Analysis ─────────────────────────────────────────────────────────────
    stats = analyse_results(results)
    print_summary(results, stats, SYMBOL)

    # ── Save results ─────────────────────────────────────────────────────────
    out = os.path.join(os.path.dirname(__file__), "walkforward_results_infy.json")
    with open(out, "w") as f:
        json.dump({"results": results, "stats": stats}, f, indent=2, default=str)
    print(f"\nResults saved to: {out}")


if __name__ == "__main__":
    main()
