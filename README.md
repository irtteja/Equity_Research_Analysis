# Equity Research System

A personal equity research system for Indian markets (NSE/BSE).

**Claude** is the reasoning engine. **Python** handles scoring and decisions. **Git** persists the knowledge base.

## Architecture

```
Data Sources (NSELIB → NSE/BSE APIs)
        ↓
Data Fetcher (equity_research/pipeline/fetcher.py)
        ↓
Claude Analysis (equity_research/pipeline/analyst.py)
        ↓
Mathematical Scoring Layer (equity_research/scorer/)
        ↓
Knowledge Base (equity_research/kb/companies/{SYMBOL}.json)
        ↓
Signal Output (BUY/SELL/HOLD/WATCH + confidence + reasoning)
        ↓
Feedback Loop (equity_research/feedback/tracker.py)
```

## Scoring Formula

```python
thesis_score = (
    earnings_quality_score   * 0.30 +
    management_quality_score * 0.25 +
    valuation_score          * 0.20 +
    momentum_score           * 0.10 +
    thesis_alignment_score   * 0.15
)
```

Weights are dynamic — stored in `config/weights.json`, updated via feedback loop (Claude proposes, human approves).

## Quick Start

```bash
# TD Power Systems prototype (first company)
python run_tdpowersys.py

# View KB
cat equity_research/kb/companies/TDPOWERSYS.json | python3 -m json.tool | head -60
```

## Directory Structure

```
equity_research/
  kb/
    schema.py         — CompanyKB, Fact, Prediction, Entity, Thesis dataclasses
    store.py          — KBStore: load/save/list JSON files
    companies/        — one .json per company (TDPOWERSYS.json, etc.)
  scorer/
    base.py           — ScoreResult + BaseScorer
    earnings_quality.py  — CFO/PAT ratio, receivables, inventory, revenue growth
    valuation.py      — PE/sector, PEG, P/B vs ROE, D/E, EV/EBITDA
    momentum.py       — 3M/6M price return, 52w range position, volume trend
    management_quality.py — promoter holding/pledge, ROCE trend, mgmt tone
    thesis_alignment.py  — bull/bear case scores, guidance momentum (Claude-fed)
    composite.py      — weighted composite + signal + confidence
  pipeline/
    fetcher.py        — DataFetcher: wraps NSELIB, returns scorer-ready dict
    analyst.py        — Analyst: calls Claude CLI for qualitative scoring
  feedback/
    tracker.py        — FeedbackTracker: predictions → outcomes → weight proposals
config/
  weights.json        — scorer weights (dynamic, versioned)
run_tdpowersys.py     — end-to-end prototype for TD Power Systems
```

## Knowledge Base Design

Each company KB file contains:
- **Thesis**: bull case, bear case, moat, key risks
- **Facts**: timestamped evidence with source, impact score, lifecycle tracking
- **Predictions**: past signals with actual outcomes (for feedback loop)
- **Entities**: competitors, suppliers, customers, mgmt persons
- **Open questions**: what the system is actively looking for

Facts decay over time and are pruned when contradicted. The KB grows incrementally — run the same script multiple times and it adds new facts without replacing old ones.

## Feedback Loop

```
Prediction made → stored in KB
Outcome happens → resolve_prediction(kb, pred_id, outcome_price)
Gap measured    → error attributed to specific scorer
Weight adjusted → propose_weight_update() → human approves → apply_weight_update()
```

Minimum 10 resolved predictions before weight updates are proposed.

## Status (November 2026)

- [x] One company prototype working end-to-end (TDPOWERSYS)
- [ ] Shareholding pattern integration (live API blocked from cloud)
- [ ] Concall transcript ingestion
- [ ] Cross-company signal extraction
- [ ] Graph links between companies (competitors, suppliers)
- [ ] Weight updates from real prediction errors (needs 10+ resolved predictions)
