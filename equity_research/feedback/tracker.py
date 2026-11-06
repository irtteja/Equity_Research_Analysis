"""
FeedbackTracker — the learning loop.

Stores predictions → measures outcomes → attributes errors → proposes weight updates.

Flow:
  make_prediction(kb, signal, scores, price) → stores Prediction in KB
  resolve_prediction(kb, prediction_id, outcome_price, outcome_signal) → measures error
  propose_weight_update(kb) → analyzes patterns → returns proposed weight changes
  apply_weight_update(proposed, approved=True) → writes new weights.json (after human approval)

Error attribution:
  Which scorer was most responsible for the prediction error?
  If earnings_quality was bullish but valuation was expensive and stock dropped:
    → valuation error was larger contributor to miss
  This is tracked across predictions to identify systematic biases.
"""
from __future__ import annotations
import json
import statistics
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from ..kb.schema import CompanyKB, Prediction


_CONFIG_PATH = Path(__file__).parents[2] / "config" / "weights.json"


class FeedbackTracker:
    def __init__(self, config_path: Optional[str] = None):
        self._config_path = Path(config_path) if config_path else _CONFIG_PATH

    def make_prediction(
        self,
        kb: CompanyKB,
        signal: str,
        score_snapshot: dict,
        price: float,
        horizon: str = "6M",
        target_price: Optional[float] = None,
        thesis: str = "",
        confidence: float = 0.5,
    ) -> Prediction:
        """Record a prediction in the KB."""
        p = kb.add_prediction(
            horizon=horizon,
            thesis=thesis or f"Signal: {signal} at ₹{price:.2f}",
            signal=signal,
            confidence=confidence,
            score_snapshot=score_snapshot,
            price_at_prediction=price,
            target_price=target_price,
        )
        return p

    def resolve_prediction(
        self,
        kb: CompanyKB,
        prediction_id: str,
        outcome_price: float,
        outcome_signal: Optional[str] = None,
    ) -> Optional[Prediction]:
        """
        Mark a prediction as resolved and compute prediction error.
        Error is defined as:
          prediction_error = (expected_return - actual_return)
          where expected_return is derived from signal + confidence
        """
        pred = next((p for p in kb.predictions if p.id == prediction_id), None)
        if not pred:
            return None
        if pred.price_at_prediction and pred.price_at_prediction > 0:
            actual_return = (outcome_price - pred.price_at_prediction) / pred.price_at_prediction

            # Expected return from signal
            signal_expected = {
                "STRONG_BUY": 0.25,
                "BUY": 0.15,
                "HOLD": 0.0,
                "SELL": -0.10,
                "STRONG_SELL": -0.20,
            }
            expected_return = signal_expected.get(pred.signal, 0.0)
            prediction_error = expected_return - actual_return  # positive = overestimated

            # Attribute error to scorers
            factor_attributions = self._attribute_error(pred.score_snapshot, prediction_error)

            pred.outcome_date = datetime.utcnow().isoformat()
            pred.outcome_price = outcome_price
            pred.outcome_signal = outcome_signal
            pred.prediction_error = prediction_error
            pred.factor_attributions = factor_attributions

        return pred

    def _attribute_error(self, score_snapshot: dict, total_error: float) -> dict:
        """
        Assign fraction of prediction error to each scorer.
        Simple heuristic: scorers that were most extreme (far from 0.5)
        get the most attribution when there's a large error.
        High positive score + negative outcome → that scorer was overconfident.
        """
        components = score_snapshot.get("components", {})
        if not components:
            return {}

        attributions = {}
        total_weight = 0
        for name, comp_dict in components.items():
            score = comp_dict.get("score", 0.5)
            extremity = abs(score - 0.5) * 2  # 0 = neutral, 1 = fully extreme
            # Direction: if score was bullish (>0.5) and error is positive (overestimated), attribution is positive
            direction = 1.0 if score > 0.5 else -1.0
            attributions[name] = extremity * direction
            total_weight += extremity

        if total_weight > 0:
            # Normalize and multiply by total error
            for name in attributions:
                attributions[name] = (attributions[name] / total_weight) * total_error

        return {k: round(v, 4) for k, v in attributions.items()}

    def propose_weight_update(self, all_resolved_predictions: list[Prediction]) -> Optional[dict]:
        """
        Analyze resolved predictions and propose weight adjustments.

        Rules:
        - Need ≥10 resolved predictions to propose weight changes
        - Scorer with consistently positive attribution (overconfident) gets weight reduced
        - Scorer with consistently negative attribution (underconfident) gets weight increased
        - Maximum change per scorer: ±0.05 per update
        - Weights must still sum to 1.0

        Returns proposed weights dict or None if insufficient data.
        """
        resolved = [p for p in all_resolved_predictions if p.is_resolved]
        if len(resolved) < 10:
            return None

        # Load current weights
        try:
            current = json.loads(self._config_path.read_text())["weights"]
        except Exception:
            from ..scorer.composite import _DEFAULT_WEIGHTS
            current = _DEFAULT_WEIGHTS.copy()

        scorer_names = list(current.keys())
        # Collect attribution per scorer
        attr_by_scorer: dict[str, list[float]] = {k: [] for k in scorer_names}
        for pred in resolved:
            for name, val in (pred.factor_attributions or {}).items():
                if name in attr_by_scorer:
                    attr_by_scorer[name].append(val)

        proposed = dict(current)
        changes = {}
        for name in scorer_names:
            vals = attr_by_scorer[name]
            if len(vals) < 5:
                continue
            mean_attr = statistics.mean(vals)
            # If mean_attr > 0: scorer was systematically overconfident → reduce weight
            # If mean_attr < 0: scorer was underconfident → increase weight
            adjustment = -mean_attr * 0.5   # scale down adjustment
            adjustment = max(-0.05, min(0.05, adjustment))  # cap at ±0.05
            proposed[name] = max(0.05, proposed[name] + adjustment)
            if abs(adjustment) > 0.005:
                changes[name] = {"from": current[name], "to": proposed[name], "reason": f"mean_attr={mean_attr:.3f}"}

        # Renormalize to sum to 1.0
        total = sum(proposed.values())
        proposed = {k: round(v / total, 4) for k, v in proposed.items()}

        if not changes:
            return None

        return {
            "proposed_weights": proposed,
            "changes": changes,
            "based_on_n_predictions": len(resolved),
            "proposed_at": datetime.utcnow().isoformat(),
            "status": "pending_human_approval",
        }

    def apply_weight_update(self, proposal: dict) -> bool:
        """Write approved weight update to config. Must be called after human approval."""
        try:
            config = json.loads(self._config_path.read_text())
            config["weights"] = proposal["proposed_weights"]
            config["_version"] = config.get("_version", 1) + 1
            config["feedback_history"].append({
                "applied_at": datetime.utcnow().isoformat(),
                "changes": proposal["changes"],
                "based_on": proposal["based_on_n_predictions"],
            })
            self._config_path.write_text(json.dumps(config, indent=2))
            return True
        except Exception:
            return False
