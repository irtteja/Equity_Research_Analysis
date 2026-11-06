"""
Composite Scorer — combines all five scorers using dynamic weights.

thesis_score = (
    earnings_quality_score   * w1 +
    management_quality_score * w2 +
    valuation_score          * w3 +
    momentum_score           * w4 +
    thesis_alignment_score   * w5
)

Weights are loaded from config/weights.json and can be updated via feedback loop.
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base import ScoreResult
from .earnings_quality import EarningsQualityScorer
from .valuation import ValuationScorer
from .momentum import MomentumScorer
from .management_quality import ManagementQualityScorer
from .thesis_alignment import ThesisAlignmentScorer


_DEFAULT_WEIGHTS = {
    "earnings_quality": 0.30,
    "management_quality": 0.25,
    "valuation": 0.20,
    "momentum": 0.10,
    "thesis_alignment": 0.15,
}

_THRESHOLDS = {
    "STRONG_BUY": 0.75,
    "BUY": 0.60,
    "HOLD": 0.45,
    "SELL": 0.30,
    "STRONG_SELL": 0.0,
}


def _load_weights(config_path: Optional[Path] = None) -> dict:
    if config_path is None:
        config_path = Path(__file__).parents[2] / "config" / "weights.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())["weights"]
        except Exception:
            pass
    return _DEFAULT_WEIGHTS.copy()


def _signal_from_score(score: float) -> str:
    for signal, threshold in sorted(_THRESHOLDS.items(), key=lambda x: -x[1]):
        if score >= threshold:
            return signal
    return "STRONG_SELL"


class CompositeScorer:
    def __init__(self, config_path: Optional[str] = None):
        self._scorers = {
            "earnings_quality": EarningsQualityScorer(),
            "management_quality": ManagementQualityScorer(),
            "valuation": ValuationScorer(),
            "momentum": MomentumScorer(),
            "thesis_alignment": ThesisAlignmentScorer(),
        }
        self._config_path = Path(config_path) if config_path else None
        self._weights = _load_weights(self._config_path)

    def score(self, data: dict) -> dict:
        """
        Run all scorers on `data`, return composite result.

        Args:
            data: dict with keys: financials, valuation, management,
                  price_history, thesis_check, kb_snapshot

        Returns:
            {
                "composite_score": float,
                "signal": str,
                "confidence": float,
                "components": {scorer_name: ScoreResult.to_dict()},
                "weights": dict,
                "flags": list[str],
                "scored_at": str,
                "reasoning": str,
            }
        """
        components = {}
        all_flags = []

        for name, scorer in self._scorers.items():
            try:
                result = scorer.score(data)
            except Exception as e:
                result = ScoreResult(0.5, {}, f"scorer error: {e}", 0.0, ["scorer_error"])
            components[name] = result
            all_flags.extend(result.flags)

        weights = self._weights
        weighted_score = sum(
            components[name].score * weights.get(name, 0.2)
            for name in self._scorers
        )

        # Data quality average → used to dampen confidence
        avg_dq = sum(components[n].data_quality for n in self._scorers) / len(self._scorers)
        confidence = weighted_score * avg_dq  # lower data quality = lower confidence

        signal = _signal_from_score(weighted_score)

        # Composite reasoning
        lines = []
        for name, result in components.items():
            w = weights.get(name, 0.2)
            lines.append(f"{name}={result.score:.2f}(w={w:.2f}): {result.reasoning}")
        reasoning = " | ".join(lines)

        return {
            "composite_score": round(weighted_score, 4),
            "signal": signal,
            "confidence": round(confidence, 4),
            "components": {name: r.to_dict() for name, r in components.items()},
            "weights": weights,
            "flags": list(set(all_flags)),
            "scored_at": datetime.utcnow().isoformat(),
            "reasoning": reasoning,
        }
