"""
Earnings Quality Scorer.

Scores how "real" the earnings are — cash-backed, not accounting tricks.

Inputs (from financial_snapshot["financials"]):
  - 4 quarters of: revenue, pat, cfo (cash from operations), receivables, inventory
  - Optionally: capex, gross_margin

Score components:
  1. cfo_pat_ratio     — CFO / PAT; >1 is excellent; <0.5 is red flag
  2. receivables_trend — receivables growing faster than revenue = bad sign
  3. inventory_trend   — same logic for manufacturing businesses
  4. pat_consistency   — variance in PAT margins over 4 quarters
  5. revenue_growth    — 4-quarter CAGR
"""
from __future__ import annotations
import statistics
from .base import BaseScorer, ScoreResult


class EarningsQualityScorer(BaseScorer):
    name = "earnings_quality"

    def score(self, data: dict) -> ScoreResult:
        quarters = data.get("financials", [])
        # Need at least 2 quarters, ideally 4
        if not quarters:
            return ScoreResult(0.5, {}, "No financial data", 0.0, ["no_data"])

        quarters = sorted(quarters, key=lambda q: q.get("period", ""), reverse=True)[:4]
        n = len(quarters)

        sub = {}
        flags = []

        # --- 1. CFO / PAT ratio ---
        cfo_pat_scores = []
        for q in quarters:
            pat = q.get("pat", 0) or 0
            cfo = q.get("cfo", q.get("operating_cash_flow", None))
            if cfo is not None and pat != 0:
                ratio = cfo / pat
                # Score: 1.5+ = 1.0, 1.0 = 0.8, 0.5 = 0.5, 0 = 0.2, negative = 0
                s = self._clamp(0.2 + 0.5 * min(ratio, 2.0))
                cfo_pat_scores.append(s)
                if ratio < 0.3:
                    flags.append("low_cfo_pat")
                if ratio < 0:
                    flags.append("negative_cfo")
        sub["cfo_pat"] = statistics.mean(cfo_pat_scores) if cfo_pat_scores else 0.5
        cfo_data_quality = len(cfo_pat_scores) / n

        # --- 2. Receivables trend ---
        rev_values = [q.get("revenue", 0) or 0 for q in quarters]
        rec_values = [q.get("receivables", q.get("trade_receivables", None)) for q in quarters]
        rec_available = [v for v in rec_values if v is not None]

        if len(rec_available) >= 2 and rev_values[0] > 0:
            # Receivables as % of revenue
            rec_pcts = []
            for i, q in enumerate(quarters):
                rev = q.get("revenue", 0) or 0
                rec = rec_values[i]
                if rev > 0 and rec is not None:
                    rec_pcts.append(rec / rev)
            if len(rec_pcts) >= 2:
                # Trend: is receivables_pct increasing? (bad)
                trend = rec_pcts[0] - rec_pcts[-1]  # positive = getting worse (latest > oldest)
                # Score: flat or improving = 0.8, mild increase = 0.6, spike = 0.3
                if trend > 0.1:
                    sub["receivables_trend"] = 0.3
                    flags.append("receivables_spike")
                elif trend > 0.05:
                    sub["receivables_trend"] = 0.55
                    flags.append("receivables_rising")
                elif trend < -0.05:
                    sub["receivables_trend"] = 0.9   # improving
                else:
                    sub["receivables_trend"] = 0.75  # stable
            else:
                sub["receivables_trend"] = 0.6
        else:
            sub["receivables_trend"] = 0.6   # neutral if no data

        # --- 3. Inventory trend (manufacturing) ---
        inv_values = [q.get("inventory", q.get("inventories", None)) for q in quarters]
        inv_available = [v for v in inv_values if v is not None]

        if len(inv_available) >= 2 and rev_values[0] > 0:
            inv_pcts = []
            for i, q in enumerate(quarters):
                rev = q.get("revenue", 0) or 0
                inv = inv_values[i]
                if rev > 0 and inv is not None:
                    inv_pcts.append(inv / rev)
            if len(inv_pcts) >= 2:
                trend = inv_pcts[0] - inv_pcts[-1]
                if trend > 0.1:
                    sub["inventory_trend"] = 0.4
                    flags.append("inventory_buildup")
                elif trend > 0.05:
                    sub["inventory_trend"] = 0.6
                elif trend < -0.05:
                    sub["inventory_trend"] = 0.85
                else:
                    sub["inventory_trend"] = 0.75
            else:
                sub["inventory_trend"] = 0.65
        else:
            sub["inventory_trend"] = 0.65  # neutral; not all businesses carry inventory

        # --- 4. PAT margin consistency ---
        pat_margins = []
        for q in quarters:
            rev = q.get("revenue", 0) or 0
            pat = q.get("pat", 0) or 0
            if rev > 0:
                pat_margins.append(pat / rev)
        if len(pat_margins) >= 2:
            avg_margin = statistics.mean(pat_margins)
            stdev = statistics.stdev(pat_margins)
            cv = stdev / avg_margin if avg_margin > 0 else 1.0  # coefficient of variation
            # Low CV = consistent; high CV = erratic
            consistency = self._clamp(1.0 - cv)
            sub["pat_consistency"] = consistency
            if avg_margin < 0:
                flags.append("loss_making")
            elif avg_margin < 0.03:
                flags.append("thin_margins")
        else:
            sub["pat_consistency"] = 0.5

        # --- 5. Revenue growth (latest vs 4 quarters ago) ---
        rev_latest = quarters[0].get("revenue", 0) or 0
        rev_oldest = quarters[-1].get("revenue", 0) or 0
        if rev_oldest > 0 and n >= 2:
            growth_rate = (rev_latest - rev_oldest) / rev_oldest  # over n-1 quarters
            # Annualise roughly: for 4Q it's annual, for 2Q it's 6M
            periods = n - 1
            annual_growth = (1 + growth_rate) ** (4 / periods) - 1
            # Score: 20%+ growth = 1.0, 10% = 0.75, 0% = 0.5, negative = 0.2
            if annual_growth >= 0.20:
                sub["revenue_growth"] = 1.0
            elif annual_growth >= 0.10:
                sub["revenue_growth"] = 0.75
            elif annual_growth >= 0.0:
                sub["revenue_growth"] = 0.5
            elif annual_growth >= -0.10:
                sub["revenue_growth"] = 0.3
                flags.append("revenue_decline")
            else:
                sub["revenue_growth"] = 0.1
                flags.append("severe_revenue_decline")
        else:
            sub["revenue_growth"] = 0.5

        # --- 6. PAT sequential trend (QoQ direction over last 3 quarters) ---
        # Catches businesses whose earnings are deteriorating even if absolute level looks ok.
        pat_values = [q.get("pat", 0) or 0 for q in quarters[:4]]
        if len(pat_values) >= 3 and all(p != 0 for p in pat_values[:3]):
            # Count how many consecutive QoQ moves are declines
            declines = sum(
                1 for i in range(len(pat_values) - 1)
                if pat_values[i] < pat_values[i + 1]  # quarters sorted newest-first → decline = newer < older
            )
            if declines >= 3:
                sub["pat_trend"] = 0.2
                flags.append("pat_declining_trend")
            elif declines == 2:
                sub["pat_trend"] = 0.4
                flags.append("pat_weakening")
            elif declines == 0:
                sub["pat_trend"] = 1.0   # all quarters improving
                flags.append("pat_accelerating")
            else:
                sub["pat_trend"] = 0.65  # mixed
        else:
            sub["pat_trend"] = 0.5

        # --- Composite ---
        weights = {
            "cfo_pat": 0.30,
            "receivables_trend": 0.15,
            "inventory_trend": 0.10,
            "pat_consistency": 0.15,
            "revenue_growth": 0.15,
            "pat_trend": 0.15,
        }
        composite = sum(sub.get(k, 0.5) * w for k, w in weights.items())

        # Data quality = fraction of quarters with CFO data (most critical)
        dq = max(cfo_data_quality, 0.3)  # floor: revenue data always available

        # Build reasoning string
        cfo_str = f"CFO/PAT={sub.get('cfo_pat', 0):.2f}"
        rec_str = f"receivables={sub.get('receivables_trend', 0):.2f}"
        reasoning = f"{cfo_str}, {rec_str}, growth={sub.get('revenue_growth', 0):.2f}"
        if flags:
            reasoning += f" [{', '.join(flags[:3])}]"

        return ScoreResult(
            score=self._clamp(composite),
            sub_scores=sub,
            reasoning=reasoning,
            data_quality=dq,
            flags=flags,
        )
