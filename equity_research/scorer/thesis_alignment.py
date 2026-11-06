"""
Thesis Alignment Scorer.

Measures how well current evidence supports or contradicts the stored thesis.

This is the most qualitative scorer — it relies on Claude-extracted scores
rather than pure math. Its inputs come from the analyst.py pipeline.

Inputs (from data["thesis_check"]):
  - bull_case_score: 0–1 (how much current evidence supports bull case)
  - bear_case_materialised: 0–1 (how much bear case risks have materialised)
  - guidance_momentum: 0–1 (is guidance getting better or worse)
  - catalyst_progress: 0–1 (key catalysts on track / achieved)
  - open_questions_resolved: fraction of open questions now answered
"""
from __future__ import annotations
from .base import BaseScorer, ScoreResult


class ThesisAlignmentScorer(BaseScorer):
    name = "thesis_alignment"

    def score(self, data: dict) -> ScoreResult:
        tc = data.get("thesis_check", {})
        kb_data = data.get("kb_snapshot", {})

        sub = {}
        flags = []
        available = 0

        # --- 1. Bull case support ---
        bull = tc.get("bull_case_score")
        if bull is not None:
            available += 1
            sub["bull_case"] = float(bull)
            if bull < 0.3:
                flags.append("thesis_weakening")
        else:
            sub["bull_case"] = 0.5

        # --- 2. Bear case materialisation (inverted: low = good) ---
        bear = tc.get("bear_case_materialised")
        if bear is not None:
            available += 1
            sub["bear_case_clear"] = 1.0 - float(bear)  # invert
            if bear > 0.6:
                flags.append("bear_case_materialising")
        else:
            sub["bear_case_clear"] = 0.6

        # --- 3. Guidance momentum ---
        guidance = tc.get("guidance_momentum")
        if guidance is not None:
            available += 1
            sub["guidance_momentum"] = float(guidance)
            if guidance > 0.7:
                flags.append("guidance_upgrade")
            elif guidance < 0.3:
                flags.append("guidance_cut")
        else:
            sub["guidance_momentum"] = 0.5

        # --- 4. Catalyst progress ---
        catalyst = tc.get("catalyst_progress")
        if catalyst is not None:
            available += 1
            sub["catalyst_progress"] = float(catalyst)
            if catalyst < 0.3:
                flags.append("catalyst_delayed")
        else:
            sub["catalyst_progress"] = 0.5

        # --- 5. Open questions resolved ---
        open_q_total = kb_data.get("open_questions_total", 0)
        open_q_remaining = kb_data.get("open_questions_open", 0)
        if open_q_total > 0:
            resolved_frac = 1.0 - (open_q_remaining / open_q_total)
            sub["questions_resolved"] = resolved_frac
        else:
            sub["questions_resolved"] = 0.6  # neutral

        weights = {
            "bull_case": 0.35,
            "bear_case_clear": 0.25,
            "guidance_momentum": 0.20,
            "catalyst_progress": 0.15,
            "questions_resolved": 0.05,
        }
        composite = sum(sub.get(k, 0.5) * w for k, w in weights.items())
        dq = self._clamp(available / 4)

        bull_str = f"bull={sub.get('bull_case', 0):.2f}"
        bear_str = f"bear_clear={sub.get('bear_case_clear', 0):.2f}"
        reasoning = f"{bull_str}, {bear_str}, guidance={sub.get('guidance_momentum', 0):.2f}"
        if flags:
            reasoning += f" [{', '.join(flags[:3])}]"

        return ScoreResult(
            score=self._clamp(composite),
            sub_scores=sub,
            reasoning=reasoning,
            data_quality=dq,
            flags=flags,
        )
