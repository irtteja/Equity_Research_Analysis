"""
Backtest: run the equity research scorer on multiple companies and validate
whether signals correlate with actual 6-month price returns in the data.

Companies chosen for coverage diversity:
  NESTLEIND  — FMCG quality compounder, premium valuation
  DEEPAKNTR  — Specialty chemicals, high-growth cyclical
  TATAMOTORS — Auto, high D/E, JLR cyclical
  SUNPHARMA  — Pharma defensive, steady
  ZOMATO     — Hyper-growth, historically loss-making
  TDPOWERSYS — Capital goods, our baseline
"""
from __future__ import annotations
import sys
import os
import json
import traceback

sys.path.insert(0, os.path.expanduser("~/NSELIB"))
sys.path.insert(0, os.path.dirname(__file__))

from equity_research.pipeline.fetcher import DataFetcher
from equity_research.scorer.composite import CompositeScorer

COMPANIES = [
    ("NESTLEIND",  "Nestle India",          "FMCG"),
    ("DEEPAKNTR",  "Deepak Nitrite",        "Chemicals"),
    ("TATAMOTORS", "Tata Motors",           "Auto"),
    ("SUNPHARMA",  "Sun Pharmaceutical",    "Pharma"),
    ("ZOMATO",     "Zomato",                "Consumer Tech"),
    ("TDPOWERSYS", "TD Power Systems",      "Capital Goods"),
]

SEPARATOR = "-" * 100


def _six_month_return(price_history: list) -> float | None:
    """
    Compute return from 6 months ago to today using price history.
    price_history is sorted newest-first.
    6M ≈ 126 trading days.
    """
    closes = [r["close"] for r in price_history if r.get("close", 0) > 0]
    if len(closes) < 60:
        return None
    idx_6m = min(126, len(closes) - 1)
    price_now  = closes[0]
    price_6m   = closes[idx_6m]
    if price_6m <= 0:
        return None
    return (price_now - price_6m) / price_6m


def _one_year_return(price_history: list) -> float | None:
    closes = [r["close"] for r in price_history if r.get("close", 0) > 0]
    if len(closes) < 100:
        return None
    idx_1y = min(252, len(closes) - 1)
    price_now  = closes[0]
    price_1y   = closes[idx_1y]
    if price_1y <= 0:
        return None
    return (price_now - price_1y) / price_1y


def run_one(symbol: str, name: str, sector: str, fetcher: DataFetcher, scorer: CompositeScorer) -> dict:
    print(f"\n{'='*60}")
    print(f"  {symbol} — {name} ({sector})")
    print(f"{'='*60}")

    try:
        data = fetcher.fetch(symbol)
    except Exception as e:
        print(f"  FETCH ERROR: {e}")
        return {"symbol": symbol, "error": str(e)}

    if data.get("fetch_errors"):
        print(f"  Fetch warnings: {data['fetch_errors']}")

    # Inject minimal analyst stubs (Claude CLI may not be available)
    data.setdefault("thesis_check", {
        "bull_case_score": 0.5,
        "bear_case_materialised": 0.3,
        "guidance_momentum": 0.5,
        "catalyst_progress": 0.5,
    })
    data.setdefault("management", {}).setdefault("mgmt_tone_score", 0.6)

    try:
        result = scorer.score(data)
    except Exception as e:
        print(f"  SCORE ERROR: {e}")
        traceback.print_exc()
        return {"symbol": symbol, "error": str(e)}

    val    = data.get("valuation", {})
    fins   = data.get("financials", [])
    ph     = data.get("price_history", [])
    r6m    = _six_month_return(ph)
    r1y    = _one_year_return(ph)

    ttm_rev = sum(q.get("revenue", 0) or 0 for q in fins[:4])
    ttm_pat = val.get("ttm_pat") or sum(q.get("pat", 0) or 0 for q in fins[:4])

    # result is a plain dict
    comp_scores = {k: v["score"] for k, v in result["components"].items()}
    comp_dq     = {k: v["data_quality"] for k, v in result["components"].items()}

    row = {
        "symbol":     symbol,
        "name":       name,
        "signal":     result["signal"],
        "composite":  round(result["composite_score"], 3),
        "confidence": round(result["confidence"], 3),
        "price":      val.get("price"),
        "pe":         val.get("pe_ratio"),
        "de":         val.get("debt_equity"),
        "promoter":   val.get("promoter_holding"),
        "ttm_rev_cr": round(ttm_rev, 0) if ttm_rev else None,
        "ttm_pat_cr": round(ttm_pat, 0) if ttm_pat else None,
        "r6m_pct":    round(r6m * 100, 1) if r6m is not None else None,
        "r1y_pct":    round(r1y * 100, 1) if r1y is not None else None,
        "flags":      result["flags"],
        "components": comp_scores,
        "data_quality": comp_dq,
        "fetch_errors": data.get("fetch_errors", []),
    }

    print(f"  Signal:      {result['signal']}  (composite={result['composite_score']:.3f}, confidence={result['confidence']:.1%})")
    print(f"  Price:       ₹{val.get('price')}  |  PE={val.get('pe_ratio')}x  |  D/E={val.get('debt_equity')}")
    print(f"  TTM Rev:     ₹{row['ttm_rev_cr']} Cr  |  TTM PAT: ₹{row['ttm_pat_cr']} Cr")
    print(f"  Returns:     6M={row['r6m_pct']}%  |  1Y={row['r1y_pct']}%")
    print(f"  Flags:       {result['flags'] or 'none'}")
    print(f"  Components:")
    for k, v in comp_scores.items():
        bar = "█" * int(v * 20)
        print(f"    {k:<22} {v:.3f}  {bar}")
    return row


def print_summary(rows: list):
    print(f"\n\n{'='*100}")
    print("  BACKTEST SUMMARY")
    print(f"{'='*100}")
    header = f"{'Symbol':<14} {'Signal':<12} {'Score':>6} {'Conf':>6} {'PE':>6} {'D/E':>5} {'6M%':>7} {'1Y%':>7}  Flags"
    print(header)
    print("-" * 100)

    # Sort by composite score descending
    for row in sorted(rows, key=lambda r: r.get("composite", 0), reverse=True):
        if "error" in row:
            print(f"{row['symbol']:<14} ERROR: {row['error']}")
            continue
        sym  = row["symbol"]
        sig  = row["signal"]
        comp = f"{row['composite']:.3f}"
        conf = f"{row['confidence']:.1%}"
        pe   = f"{row['pe']:.1f}" if row.get("pe") else "n/a"
        de   = f"{row['de']:.2f}" if row.get("de") is not None else "n/a"
        r6m  = f"{row['r6m_pct']:+.1f}%" if row.get("r6m_pct") is not None else "n/a"
        r1y  = f"{row['r1y_pct']:+.1f}%" if row.get("r1y_pct") is not None else "n/a"
        flags = ",".join(row.get("flags", [])[:3])
        print(f"{sym:<14} {sig:<12} {comp:>6} {conf:>6} {pe:>6} {de:>5} {r6m:>7} {r1y:>7}  {flags}")

    print(f"\n{'='*100}")
    print("VALIDATION: Does signal rank correlate with 6-month return?")
    print(f"{'='*100}")

    ranked = [r for r in sorted(rows, key=lambda r: r.get("composite", 0), reverse=True) if "error" not in r]
    for i, row in enumerate(ranked):
        r6m = row.get("r6m_pct")
        signal = row["signal"]
        verdict = ""
        if r6m is not None:
            if signal in ("STRONG_BUY", "BUY") and r6m > 5:
                verdict = "CORRECT — positive return for BUY"
            elif signal in ("STRONG_BUY", "BUY") and r6m < -5:
                verdict = "WRONG — BUY signal but price declined"
            elif signal in ("SELL", "STRONG_SELL") and r6m < -5:
                verdict = "CORRECT — negative return for SELL"
            elif signal in ("SELL", "STRONG_SELL") and r6m > 5:
                verdict = "WRONG — SELL signal but price rose"
            elif signal == "HOLD":
                verdict = f"HOLD — return was {r6m:+.1f}% (no strong call)"
            else:
                verdict = f"MIXED — {r6m:+.1f}%"
            print(f"  #{i+1} {row['symbol']:<14} {signal:<12} 6M={r6m:+.1f}% --> {verdict}")
        else:
            print(f"  #{i+1} {row['symbol']:<14} {signal:<12} 6M=n/a")


if __name__ == "__main__":
    print("Equity Research Backtest Runner")
    print("Fetching data for 6 companies (archive-safe, cloud-compatible)...")
    print(SEPARATOR)

    fetcher = DataFetcher(guard=False)
    scorer  = CompositeScorer()
    rows = []

    for symbol, name, sector in COMPANIES:
        try:
            row = run_one(symbol, name, sector, fetcher, scorer)
            rows.append(row)
        except Exception as e:
            print(f"  FATAL for {symbol}: {e}")
            traceback.print_exc()
            rows.append({"symbol": symbol, "error": str(e)})

    print_summary(rows)

    # Save raw results
    out = "/home/user/Equity_Research_Analysis/backtest_results.json"
    with open(out, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nFull results saved to: {out}")
