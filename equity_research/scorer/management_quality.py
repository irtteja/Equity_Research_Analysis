"""
Management Quality Scorer.

Scores management quality from structured data where possible,
relying on Claude's extracted scores for qualitative signals.

Quantitative inputs (from financial_snapshot):
  - roce_trend: 3-year ROCE trend (improving / stable / declining)
  - promoter_holding_pct
  - promoter_pledge_pct
  - capex_discipline: capex/revenue over 3 years vs industry
  - dividend_consistency: years of consistent/growing dividends

Qualitative inputs (Claude-extracted, 0–1 scores):
  - mgmt_tone_score: tone in concall (confident, specific vs vague, defensive)
  - guidance_accuracy: past guidance accuracy from prediction log
  - related_party_risk: related party transactions as % of revenue
"""
from __future__ import annotations
from .base import BaseScorer, ScoreResult


class ManagementQualityScorer(BaseScorer):
    name = "management_quality"

    def score(self, data: dict) -> ScoreResult:
        snap = data.get("valuation", {})
        mgmt = data.get("management", {})
        fins = data.get("financials", [])

        sub = {}
        flags = []
        available = 0

        # --- 1. Promoter holding ---
        promoter_pct = snap.get("promoter_holding") or mgmt.get("promoter_holding_pct")
        if promoter_pct is not None:
            available += 1
            p = float(promoter_pct)
            if p >= 60:
                sub["promoter_holding"] = 0.90
            elif p >= 50:
                sub["promoter_holding"] = 0.80
            elif p >= 35:
                sub["promoter_holding"] = 0.65
            elif p >= 25:
                sub["promoter_holding"] = 0.50
            else:
                sub["promoter_holding"] = 0.30
                flags.append("low_promoter_holding")
        else:
            sub["promoter_holding"] = 0.6

        # --- 2. Promoter pledge ---
        pledge_pct = snap.get("promoter_pledge") or mgmt.get("promoter_pledge_pct", 0)
        available += 1
        pledge = float(pledge_pct or 0)
        if pledge == 0:
            sub["promoter_pledge"] = 1.0
        elif pledge < 10:
            sub["promoter_pledge"] = 0.75
        elif pledge < 25:
            sub["promoter_pledge"] = 0.50
            flags.append("partial_pledge")
        elif pledge < 50:
            sub["promoter_pledge"] = 0.25
            flags.append("high_pledge")
        else:
            sub["promoter_pledge"] = 0.05
            flags.append("very_high_pledge")

        # --- 3. ROCE trend ---
        roce_values = mgmt.get("roce_history", [])  # list of annual ROCE values, oldest first
        if not roce_values and fins:
            # Try to compute from financials if ebit + capital_employed available
            roce_vals = []
            for q in sorted(fins, key=lambda x: x.get("period", ""))[:4]:
                roce = q.get("roce")
                if roce is not None:
                    roce_vals.append(roce)
            roce_values = roce_vals

        if len(roce_values) >= 2:
            available += 1
            trend = roce_values[-1] - roce_values[0]
            avg_roce = sum(roce_values) / len(roce_values)
            # Score on absolute level + trend
            if avg_roce >= 0.20:
                base_roce = 0.90
            elif avg_roce >= 0.15:
                base_roce = 0.75
            elif avg_roce >= 0.10:
                base_roce = 0.55
            elif avg_roce >= 0.05:
                base_roce = 0.35
            else:
                base_roce = 0.15
                flags.append("low_roce")
            # Adjust for trend
            trend_adj = 0.1 if trend > 0.02 else (-0.1 if trend < -0.02 else 0)
            sub["roce_trend"] = self._clamp(base_roce + trend_adj)
        else:
            sub["roce_trend"] = 0.6

        # --- 4. Mgmt tone (Claude-extracted, 0–1) ---
        tone_score = mgmt.get("mgmt_tone_score")
        if tone_score is not None:
            available += 1
            sub["mgmt_tone"] = float(tone_score)
            if tone_score < 0.4:
                flags.append("defensive_mgmt_tone")
        else:
            sub["mgmt_tone"] = 0.6  # slight positive default

        # --- 5. Guidance accuracy (from prediction log) ---
        guidance_acc = mgmt.get("guidance_accuracy")  # 0–1; set by feedback loop
        if guidance_acc is not None:
            available += 1
            sub["guidance_accuracy"] = float(guidance_acc)
            if guidance_acc < 0.4:
                flags.append("poor_guidance_track_record")
        else:
            sub["guidance_accuracy"] = 0.6  # benefit of the doubt initially

        # --- 6. Capital allocation (dividend consistency + capex discipline) ---
        div_years = mgmt.get("dividend_consecutive_years", 0)
        if div_years >= 5:
            sub["capital_allocation"] = 0.85
        elif div_years >= 3:
            sub["capital_allocation"] = 0.70
        elif div_years >= 1:
            sub["capital_allocation"] = 0.55
        else:
            sub["capital_allocation"] = 0.5  # neutral — could be growth co.

        weights = {
            "promoter_holding": 0.20,
            "promoter_pledge": 0.20,
            "roce_trend": 0.25,
            "mgmt_tone": 0.15,
            "guidance_accuracy": 0.10,
            "capital_allocation": 0.10,
        }
        composite = sum(sub.get(k, 0.6) * w for k, w in weights.items())
        dq = self._clamp(available / 4)

        promo_str = f"promoter={promoter_pct:.0f}%" if promoter_pct else "promoter=n/a"
        pledge_str = f"pledge={pledge:.0f}%"
        reasoning = f"{promo_str}, {pledge_str}, roce={sub.get('roce_trend', 0):.2f}"
        if flags:
            reasoning += f" [{', '.join(flags[:3])}]"

        return ScoreResult(
            score=self._clamp(composite),
            sub_scores=sub,
            reasoning=reasoning,
            data_quality=dq,
            flags=flags,
        )
