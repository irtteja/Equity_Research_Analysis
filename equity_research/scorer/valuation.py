"""
Valuation Scorer.

Lower valuation relative to growth and peers = higher score.

Inputs (from financial_snapshot):
  - pe_ratio, pb_ratio, ev_ebitda
  - sector_pe (sector median P/E — provided or estimated)
  - pat_growth_3y (3-year PAT CAGR)
  - roce (return on capital employed)
  - debt_equity

Scoring:
  1. PEG ratio (P/E ÷ growth rate) — below 1 = attractive
  2. P/E vs sector — discount = good
  3. P/B vs ROE — P/B justified by ROE?
  4. Balance sheet health — D/E ratio
"""
from __future__ import annotations
import math
from .base import BaseScorer, ScoreResult


class ValuationScorer(BaseScorer):
    name = "valuation"

    # Sector-agnostic fallback benchmarks (use sector_pe from data if available)
    _DEFAULT_SECTOR_PE = 25.0
    _DEFAULT_SECTOR_PB = 3.5

    def score(self, data: dict) -> ScoreResult:
        snap = data.get("valuation", {})
        fins = data.get("financials", [])

        sub = {}
        flags = []
        available_components = 0

        pe = snap.get("pe_ratio") or snap.get("pe")
        pb = snap.get("pb_ratio") or snap.get("pb")
        ev_ebitda = snap.get("ev_ebitda")
        sector_pe = snap.get("sector_pe", self._DEFAULT_SECTOR_PE)
        pat_growth_3y = snap.get("pat_growth_3y")  # e.g. 0.25 for 25%
        roe = snap.get("roe")
        debt_equity = snap.get("debt_equity", 0)
        price = snap.get("price") or snap.get("last_price")

        # --- 1. P/E vs sector ---
        if pe and pe > 0:
            available_components += 1
            pe_discount = (sector_pe - pe) / sector_pe  # positive = discount to sector
            # Score: 30%+ discount = 1.0, at par = 0.5, 30% premium = 0.2
            pe_score = self._clamp(0.5 + pe_discount)
            sub["pe_vs_sector"] = pe_score
            if pe > sector_pe * 1.5:
                flags.append("expensive_pe")
            elif pe < sector_pe * 0.6:
                flags.append("cheap_pe")
        else:
            sub["pe_vs_sector"] = 0.5  # neutral

        # --- 2. PEG ratio ---
        if pe and pe > 0 and pat_growth_3y and pat_growth_3y > 0:
            available_components += 1
            growth_pct = pat_growth_3y * 100  # convert to percentage points
            peg = pe / growth_pct
            # PEG < 1 = attractive (paying less than growth rate), > 2 = expensive
            if peg < 0.5:
                sub["peg"] = 1.0
            elif peg < 1.0:
                sub["peg"] = 0.8
            elif peg < 1.5:
                sub["peg"] = 0.6
            elif peg < 2.0:
                sub["peg"] = 0.4
            else:
                sub["peg"] = 0.2
                flags.append("high_peg")
        else:
            sub["peg"] = 0.5

        # --- 3. P/B vs ROE (justified P/B) ---
        if pb and pb > 0:
            available_components += 1
            if roe and roe > 0:
                # Justified P/B ≈ ROE / cost_of_equity; assume CoE = 12%
                justified_pb = roe / 0.12
                pb_ratio = pb / justified_pb
                # Score: P/B below justified = good
                if pb_ratio < 0.7:
                    sub["pb_justified"] = 0.9
                elif pb_ratio < 1.0:
                    sub["pb_justified"] = 0.75
                elif pb_ratio < 1.5:
                    sub["pb_justified"] = 0.5
                else:
                    sub["pb_justified"] = 0.3
                    flags.append("expensive_pb")
            else:
                # Simple P/B benchmark
                pb_benchmark = self._DEFAULT_SECTOR_PB
                pb_score = self._clamp(0.5 + (pb_benchmark - pb) / pb_benchmark * 0.5)
                sub["pb_justified"] = pb_score
        else:
            sub["pb_justified"] = 0.5

        # --- 4. Balance sheet health (D/E) ---
        available_components += 1
        if debt_equity is not None:
            de = float(debt_equity)
            if de < 0.3:
                sub["balance_sheet"] = 1.0
            elif de < 0.75:
                sub["balance_sheet"] = 0.8
            elif de < 1.5:
                sub["balance_sheet"] = 0.55
            elif de < 3.0:
                sub["balance_sheet"] = 0.3
                flags.append("high_debt")
            else:
                sub["balance_sheet"] = 0.1
                flags.append("dangerously_high_debt")
        else:
            sub["balance_sheet"] = 0.6  # slight positive if no debt info (assume ok)

        # --- 5. EV/EBITDA ---
        if ev_ebitda and ev_ebitda > 0:
            available_components += 1
            # < 10x = cheap, 10–20x = fair, > 20x = expensive
            if ev_ebitda < 8:
                sub["ev_ebitda"] = 0.9
            elif ev_ebitda < 15:
                sub["ev_ebitda"] = 0.7
            elif ev_ebitda < 25:
                sub["ev_ebitda"] = 0.4
            else:
                sub["ev_ebitda"] = 0.2
                flags.append("high_ev_ebitda")
        else:
            sub["ev_ebitda"] = 0.5

        # --- Composite ---
        weights = {
            "pe_vs_sector": 0.25,
            "peg": 0.25,
            "pb_justified": 0.20,
            "balance_sheet": 0.20,
            "ev_ebitda": 0.10,
        }
        composite = sum(sub.get(k, 0.5) * w for k, w in weights.items())
        dq = self._clamp(available_components / 4)

        pe_str = f"PE={pe:.1f}" if pe else "PE=n/a"
        pb_str = f"PB={pb:.1f}" if pb else "PB=n/a"
        de_str = f"D/E={debt_equity:.2f}" if debt_equity is not None else "D/E=n/a"
        reasoning = f"{pe_str}, {pb_str}, {de_str}"
        if pat_growth_3y:
            reasoning += f", 3y-growth={pat_growth_3y:.0%}"
        if flags:
            reasoning += f" [{', '.join(flags[:3])}]"

        return ScoreResult(
            score=self._clamp(composite),
            sub_scores=sub,
            reasoning=reasoning,
            data_quality=dq,
            flags=flags,
        )
