"""Base scorer interface."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScoreResult:
    score: float            # 0–1
    sub_scores: dict        # component breakdowns
    reasoning: str          # one-line explanation
    data_quality: float     # 0–1: how much data was available (1 = full, 0 = no data)
    flags: list[str]        # ["negative_cfo", "receivables_spike", ...]

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "sub_scores": {k: round(v, 4) for k, v in self.sub_scores.items()},
            "reasoning": self.reasoning,
            "data_quality": round(self.data_quality, 4),
            "flags": self.flags,
        }


class BaseScorer:
    name: str = "base"

    def score(self, data: dict) -> ScoreResult:
        raise NotImplementedError

    @staticmethod
    def _clamp(v: float) -> float:
        return max(0.0, min(1.0, v))

    @staticmethod
    def _safe_div(a: float, b: float, default: float = 0.0) -> float:
        if b == 0:
            return default
        return a / b
