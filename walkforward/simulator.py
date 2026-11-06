"""
Walk-forward blind simulation engine.

For each quarter T from start to end:
  1. Build snapshot using ONLY data available at T  ← blind constraint
  2. Score with CompositeScorer  → signal + composite_score
  3. Record prediction (signal, price, scores)
  4. Advance to T+1 quarter
  5. Calculate actual 1-quarter and 1-year forward returns
  6. Classify: was the signal correct?
  7. After all periods, run attribution + propose weight adjustments

Accuracy metric: directional correctness
  BUY   → correct if price > 0% over next quarter
  HOLD  → correct if price within ±10%
  SELL  → correct if price < 0% over next quarter
"""
from __future__ import annotations
import sys, os, json
from datetime import date
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.expanduser("~/NSELIB"))

from equity_research.scorer.composite import CompositeScorer
from walkforward.data import build_snapshot

_SCORER = CompositeScorer()


# ── Signal thresholds for "correct" classification ───────────────────────────

def _is_correct(signal: str, fwd_return: float) -> str:
    """Returns 'correct', 'wrong', or 'neutral'."""
    if signal in ("STRONG_BUY", "BUY"):
        if fwd_return > 0.05:
            return "correct"
        elif fwd_return < -0.05:
            return "wrong"
        return "neutral"
    elif signal in ("SELL", "STRONG_SELL"):
        if fwd_return < -0.05:
            return "correct"
        elif fwd_return > 0.05:
            return "wrong"
        return "neutral"
    else:  # HOLD
        if abs(fwd_return) <= 0.10:
            return "correct"
        return "neutral"


# ── Main simulation ───────────────────────────────────────────────────────────

def run_simulation(
    symbol: str,
    name: str,
    quarters: list,          # list of date objects, each is a quarter-end
    price_cache: dict,
    xbrl_records: list,
    annual_xbrl: list,
) -> list:
    """
    Run walk-forward simulation. Returns list of period results.

    Each result dict:
      quarter, signal, composite_score, confidence, price_at_signal,
      price_next_quarter, price_1year, return_1q, return_1y,
      correct_1q, components, flags, snapshot_financials_summary
    """
    results = []
    print(f"\n{'='*80}")
    print(f"  WALK-FORWARD SIMULATION: {symbol} ({name})")
    print(f"  Periods: {quarters[0].isoformat()} → {quarters[-1].isoformat()}")
    print(f"  Total quarters: {len(quarters)}")
    print(f"{'='*80}")

    for i, q_date in enumerate(quarters):
        # Build blind snapshot: ONLY data available at q_date
        snapshot = build_snapshot(
            symbol=symbol,
            name=name,
            as_of=q_date,
            price_cache=price_cache,
            xbrl_records=xbrl_records,
            annual_xbrl=annual_xbrl,
        )

        price_at_signal = snapshot["valuation"].get("price")
        fins = snapshot["financials"]
        n_quarters_avail = len(fins)

        if not fins or not price_at_signal:
            print(f"  [{q_date}] SKIP — insufficient data "
                  f"(quarters={n_quarters_avail}, price={price_at_signal})")
            continue

        # Score
        result = _SCORER.score(snapshot)
        signal     = result["signal"]
        composite  = result["composite_score"]
        confidence = result["confidence"]
        components = {k: round(v["score"], 3) for k, v in result["components"].items()}
        flags      = result["flags"]

        # Forward return: price at next quarter-end
        price_next_q  = None
        price_1y      = None
        return_1q     = None
        return_1y     = None
        correct_1q    = "pending"

        if i + 1 < len(quarters):
            next_q = quarters[i + 1]
            # Find the price on or just before next quarter-end
            price_next_q = _price_at(price_cache, next_q)
            if price_next_q and price_at_signal:
                return_1q = (price_next_q - price_at_signal) / price_at_signal
                correct_1q = _is_correct(signal, return_1q)

        # 1-year forward (4 quarters ahead)
        if i + 4 < len(quarters):
            q_1y = quarters[i + 4]
            price_1y = _price_at(price_cache, q_1y)
            if price_1y and price_at_signal:
                return_1y = (price_1y - price_at_signal) / price_at_signal

        # Latest TTM revenue / PAT for context
        ttm_rev = sum(q.get("revenue", 0) or 0 for q in fins[:4])
        ttm_pat = sum(q.get("pat", 0) or 0 for q in fins[:4])
        latest_eps = fins[0].get("eps") if fins else None

        period_result = {
            "quarter":          q_date.isoformat(),
            "n_q_available":    n_quarters_avail,
            "signal":           signal,
            "composite_score":  composite,
            "confidence":       round(confidence, 3),
            "price_at_signal":  price_at_signal,
            "price_next_q":     price_next_q,
            "price_1y":         price_1y,
            "return_1q":        round(return_1q, 4) if return_1q is not None else None,
            "return_1y":        round(return_1y, 4) if return_1y is not None else None,
            "correct_1q":       correct_1q,
            "components":       components,
            "flags":            flags,
            "pe_at_signal":     snapshot["valuation"].get("pe_ratio"),
            "ttm_rev":          round(ttm_rev, 1),
            "ttm_pat":          round(ttm_pat, 1),
            "latest_eps":       latest_eps,
        }
        results.append(period_result)

        # Print period summary
        r1q_str = f"{return_1q*100:+.1f}%" if return_1q is not None else "pending"
        r1y_str = f"{return_1y*100:+.1f}%" if return_1y is not None else "pending"
        correct_icon = {"correct": "OK", "wrong": "XX", "neutral": "--", "pending": ".."}[correct_1q]
        print(f"  [{q_date}]  {signal:<12} score={composite:.3f}  "
              f"P={price_at_signal:.0f}  1Q={r1q_str:>7}  1Y={r1y_str:>7}  [{correct_icon}]  "
              f"PE={snapshot['valuation'].get('pe_ratio') or 'n/a'}  "
              f"flags={','.join(flags[:2]) or 'none'}")

    return results


# ── Accuracy analysis ─────────────────────────────────────────────────────────

def analyse_results(results: list) -> dict:
    """Compute accuracy stats and attribution across all periods."""
    resolved = [r for r in results if r["correct_1q"] != "pending"]

    total     = len(resolved)
    correct   = sum(1 for r in resolved if r["correct_1q"] == "correct")
    wrong     = sum(1 for r in resolved if r["correct_1q"] == "wrong")
    neutral   = sum(1 for r in resolved if r["correct_1q"] == "neutral")
    accuracy  = correct / total if total else 0

    # By signal
    by_signal: dict = {}
    for r in resolved:
        sig = r["signal"]
        by_signal.setdefault(sig, {"total": 0, "correct": 0, "wrong": 0, "neutral": 0, "returns": []})
        by_signal[sig]["total"]   += 1
        by_signal[sig][r["correct_1q"]] += 1
        if r["return_1q"] is not None:
            by_signal[sig]["returns"].append(r["return_1q"])

    # Average forward returns
    all_returns = [r["return_1q"] for r in resolved if r["return_1q"] is not None]
    buy_returns  = [r["return_1q"] for r in resolved
                   if r["signal"] in ("BUY", "STRONG_BUY") and r["return_1q"] is not None]
    hold_returns = [r["return_1q"] for r in resolved
                   if r["signal"] == "HOLD" and r["return_1q"] is not None]
    sell_returns = [r["return_1q"] for r in resolved
                   if r["signal"] in ("SELL", "STRONG_SELL") and r["return_1q"] is not None]

    def _avg(lst): return sum(lst) / len(lst) if lst else None

    # Attribution: which scorer most influenced wrong calls
    scorer_error: dict = {}
    for r in resolved:
        if r["correct_1q"] == "wrong":
            for scorer, score in r["components"].items():
                # High score on a wrong BUY call = that scorer contributed to error
                if r["signal"] in ("BUY", "STRONG_BUY") and score > 0.6:
                    scorer_error[scorer] = scorer_error.get(scorer, 0) + (score - 0.5)
                elif r["signal"] in ("SELL", "STRONG_SELL") and score < 0.4:
                    scorer_error[scorer] = scorer_error.get(scorer, 0) + (0.5 - score)

    return {
        "total_periods":     total,
        "correct":           correct,
        "wrong":             wrong,
        "neutral":           neutral,
        "accuracy":          round(accuracy, 3),
        "avg_return_all":    round(_avg(all_returns) * 100, 2) if _avg(all_returns) else None,
        "avg_return_buy":    round(_avg(buy_returns) * 100, 2) if _avg(buy_returns) else None,
        "avg_return_hold":   round(_avg(hold_returns) * 100, 2) if _avg(hold_returns) else None,
        "avg_return_sell":   round(_avg(sell_returns) * 100, 2) if _avg(sell_returns) else None,
        "by_signal":         {k: {kk: vv for kk, vv in v.items() if kk != "returns"}
                              for k, v in by_signal.items()},
        "avg_return_by_signal": {k: round(_avg(v["returns"]) * 100, 2)
                                  for k, v in by_signal.items() if v["returns"]},
        "scorer_attribution": {k: round(v, 3) for k, v in
                                sorted(scorer_error.items(), key=lambda x: -x[1])},
    }


def print_summary(results: list, stats: dict, symbol: str):
    print(f"\n\n{'='*80}")
    print(f"  WALK-FORWARD RESULTS: {symbol}")
    print(f"{'='*80}")

    # Signal timeline
    print(f"\n  {'Quarter':<12} {'Signal':<12} {'Score':>6} {'PE':>6} "
          f"{'1Q Ret':>8} {'1Y Ret':>8} {'OK?':>5}  TTM PAT (Cr)")
    print(f"  {'-'*80}")
    for r in results:
        sig      = r["signal"]
        score    = r["composite_score"]
        r1q      = f"{r['return_1q']*100:+.1f}%" if r["return_1q"] is not None else "pend"
        r1y      = f"{r['return_1y']*100:+.1f}%" if r["return_1y"] is not None else "pend"
        ok       = {"correct": " OK", "wrong": " XX", "neutral": " --", "pending": "..."}[r["correct_1q"]]
        pe       = f"{r['pe_at_signal']:.1f}" if r.get("pe_at_signal") else "n/a"
        pat      = f"₹{r['ttm_pat']:.0f}" if r.get("ttm_pat") else "n/a"
        print(f"  {r['quarter']:<12} {sig:<12} {score:>6.3f} {pe:>6} "
              f"{r1q:>8} {r1y:>8} {ok:>5}  {pat}")

    print(f"\n{'='*80}")
    print(f"  ACCURACY SUMMARY")
    print(f"{'='*80}")
    print(f"  Total resolved periods : {stats['total_periods']}")
    print(f"  Correct                : {stats['correct']}  ({stats['correct']}/{stats['total_periods']})")
    print(f"  Wrong                  : {stats['wrong']}")
    print(f"  Neutral                : {stats['neutral']}")
    print(f"  Accuracy (excl neutral): ", end="")
    decisive = stats["correct"] + stats["wrong"]
    acc = stats["correct"] / decisive if decisive else 0
    print(f"{acc:.1%}  ({stats['correct']}/{decisive})")

    print(f"\n  Avg 1Q return by signal:")
    for sig, ret in stats.get("avg_return_by_signal", {}).items():
        count = stats["by_signal"].get(sig, {}).get("total", 0)
        print(f"    {sig:<14} {ret:+.2f}%  (n={count})")

    if stats.get("scorer_attribution"):
        print(f"\n  Scorer attribution on wrong calls (higher = more blame):")
        for scorer, weight in stats["scorer_attribution"].items():
            print(f"    {scorer:<25} {weight:.3f}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _price_at(price_cache: dict, target: date) -> Optional[float]:
    """Return closing price on target date or nearest prior trading day (up to 5 days back)."""
    for offset in range(6):
        from datetime import timedelta
        d = target - timedelta(days=offset)
        ds = d.isoformat()
        if ds in price_cache:
            return price_cache[ds]
    return None
