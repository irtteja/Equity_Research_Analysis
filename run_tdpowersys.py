"""
TD Power Systems — end-to-end prototype run.

Demonstrates the full pipeline:
  1. Fetch real data from NSE (via NSELIB)
  2. Claude analyses the data, extracts thesis + scores
  3. Mathematical model scores all five dimensions
  4. Result stored in KB as TDPOWERSYS.json
  5. Signal and reasoning printed

Run from the Equity_Research_Analysis directory:
  python run_tdpowersys.py
"""
import sys
import os
import json
from pathlib import Path
from datetime import datetime

# Add parent directories
here = Path(__file__).parent
sys.path.insert(0, str(here))
sys.path.insert(0, os.path.expanduser("~/NSELIB"))

from equity_research.kb.store import KBStore
from equity_research.kb.schema import CompanyKB, Thesis
from equity_research.scorer.composite import CompositeScorer
from equity_research.pipeline.fetcher import DataFetcher
from equity_research.pipeline.analyst import Analyst
from equity_research.feedback.tracker import FeedbackTracker


SYMBOL = "TDPOWERSYS"
NAME = "TD Power Systems"
SECTOR = "Industrials"
INDUSTRY = "Capital Goods — Electrical Equipment"


def _banner(msg: str):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print('='*60)


def run():
    _banner(f"Equity Research: {NAME} ({SYMBOL})")

    # --- Step 1: KB ---
    kb_dir = here / "equity_research" / "kb" / "companies"
    store = KBStore(kb_dir=str(kb_dir))

    # Load or create KB entry
    kb = store.load_or_create(
        SYMBOL,
        name=NAME,
        sector=SECTOR,
        industry=INDUSTRY,
    )
    print(f"\n[KB] {'Loaded existing' if kb.facts else 'Created new'} KB for {SYMBOL}")

    # --- Step 2: Fetch data ---
    _banner("Step 1: Fetching data from NSE")
    fetcher = DataFetcher(guard=False)
    print(f"  Fetching financials, price history, shareholding...")
    try:
        data = fetcher.fetch(SYMBOL)
    except Exception as e:
        print(f"  ERROR in fetcher: {e}")
        import traceback; traceback.print_exc()
        data = {
            "symbol": SYMBOL, "name": NAME, "sector": SECTOR,
            "financials": [], "valuation": {}, "management": {},
            "price_history": [], "corporate_actions": [], "shareholding": {},
            "raw": {}, "fetch_errors": [str(e)],
        }

    if data.get("fetch_errors"):
        print(f"  Fetch warnings: {data['fetch_errors']}")

    n_quarters = len(data.get("financials", []))
    n_prices = len(data.get("price_history", []))
    print(f"  Quarters of financials: {n_quarters}")
    print(f"  Price history rows: {n_prices}")
    print(f"  Promoter holding: {data.get('valuation', {}).get('promoter_holding', 'n/a')}%")
    print(f"  Current price: ₹{data.get('valuation', {}).get('price', 'n/a')}")
    print(f"  P/E: {data.get('valuation', {}).get('pe_ratio', 'n/a')}")

    # --- Step 3: Claude analysis ---
    _banner("Step 2: Claude analysis (qualitative reasoning)")
    analyst = Analyst()
    print("  Running Claude analysis...")
    analysis = analyst.analyse(
        symbol=SYMBOL,
        name=NAME,
        sector=SECTOR,
        fetched_data=data,
        existing_kb=kb if kb.facts else None,
    )

    print(f"\n  One-line summary: {analysis.get('one_line_summary', 'n/a')}")
    thesis = analysis.get("suggested_thesis", {})
    print(f"\n  Bull case: {thesis.get('bull_case', 'n/a')}")
    print(f"\n  Bear case: {thesis.get('bear_case', 'n/a')}")
    print(f"\n  Moat: {thesis.get('moat', 'n/a')}")

    # Merge analysis into data for scoring
    data["thesis_check"] = analysis["thesis_check"]
    data["management"].update(analysis.get("management", {}))
    data["kb_snapshot"] = {
        "open_questions_total": len(kb.open_questions),
        "open_questions_open": len(kb.open_questions_list()),
    }

    # --- Step 4: Score ---
    _banner("Step 3: Mathematical scoring")
    scorer = CompositeScorer()
    scores = scorer.score(data)

    print(f"\n  {'Scorer':<25} {'Score':>7}  {'Data Quality':>14}  Flags")
    print(f"  {'-'*65}")
    for name, comp in scores["components"].items():
        flags_str = ", ".join(comp["flags"][:2]) if comp["flags"] else "—"
        print(f"  {name:<25} {comp['score']:>7.3f}  {comp['data_quality']:>14.2f}  {flags_str}")

    print(f"\n  {'COMPOSITE SCORE':<25} {scores['composite_score']:>7.3f}")
    print(f"  {'SIGNAL':<25} {scores['signal']}")
    print(f"  {'CONFIDENCE':<25} {scores['confidence']:>7.3f}")

    if scores.get("flags"):
        print(f"\n  Top flags: {', '.join(scores['flags'][:5])}")

    # --- Step 5: Update KB ---
    _banner("Step 4: Storing to KB")

    # Update thesis
    kb.thesis = Thesis(
        bull_case=thesis.get("bull_case", kb.thesis.bull_case),
        bear_case=thesis.get("bear_case", kb.thesis.bear_case),
        moat=thesis.get("moat", kb.thesis.moat),
        key_risks=thesis.get("key_risks", kb.thesis.key_risks),
    )
    kb.name = data.get("name") or NAME
    kb.sector = SECTOR
    kb.industry = INDUSTRY

    # Add extracted facts
    for fact_dict in analysis.get("suggested_facts", []):
        kb.add_fact(
            text=fact_dict.get("text", ""),
            source=fact_dict.get("source", "automated_analysis"),
            tags=fact_dict.get("tags", []),
            impact=fact_dict.get("impact", 0.5),
        )

    # Add open questions
    for q in analysis.get("suggested_questions", []):
        # Don't duplicate
        existing = [oq.question for oq in kb.open_questions_list()]
        if q not in existing:
            kb.add_question(question=q, priority="medium")

    # Update scores
    kb.latest_scores = scores
    kb.latest_signal = scores["signal"]
    kb.latest_confidence = scores["confidence"]
    kb.latest_scored_at = scores["scored_at"]

    # Store financial snapshot summary
    kb.financial_snapshot = {
        "fetched_at": datetime.utcnow().isoformat(),
        "quarters": n_quarters,
        "price": data.get("valuation", {}).get("price"),
        "pe_ratio": data.get("valuation", {}).get("pe_ratio"),
        "promoter_holding": data.get("valuation", {}).get("promoter_holding"),
        "debt_equity": data.get("valuation", {}).get("debt_equity"),
    }

    store.save(kb)
    kb_path = kb_dir / f"{SYMBOL}.json"
    print(f"  KB saved to: {kb_path}")
    print(f"  Facts: {len(kb.facts)}")
    print(f"  Open questions: {len(kb.open_questions_list())}")

    # --- Step 6: Record initial prediction ---
    _banner("Step 5: Recording initial prediction")
    tracker = FeedbackTracker()
    price = data.get("valuation", {}).get("price")
    if price:
        pred = tracker.make_prediction(
            kb=kb,
            signal=scores["signal"],
            score_snapshot=scores,
            price=float(price),
            horizon="6M",
            confidence=scores["confidence"],
            thesis=analysis.get("one_line_summary", ""),
        )
        store.save(kb)
        print(f"  Prediction recorded: {pred.id}")
        print(f"  Signal: {pred.signal} @ ₹{pred.price_at_prediction:.2f}")
        print(f"  Horizon: {pred.horizon}")
    else:
        print("  No live price available — prediction not recorded (run from non-cloud IP)")

    # --- Final summary ---
    _banner("DONE")
    print(f"""
  Company:    {kb.name} ({kb.symbol})
  Signal:     {kb.latest_signal}
  Confidence: {kb.latest_confidence:.1%}

  Thesis snapshot:
    Bull: {kb.thesis.bull_case[:100]}...
    Bear: {kb.thesis.bear_case[:100]}...

  KB file:    {kb_dir}/{SYMBOL}.json
  Run again to update (new data will add to, not replace, existing facts)
""")

    return kb, scores


if __name__ == "__main__":
    kb, scores = run()
