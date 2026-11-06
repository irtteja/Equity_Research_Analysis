"""
Momentum Scorer.

Price and volume-based momentum signals.

Inputs (from financial_snapshot["price_history"]):
  - List of OHLCV dicts sorted by date descending
  - current_price, week52_high, week52_low

Scoring:
  1. 3M price return vs Nifty (relative momentum)
  2. 6M price return
  3. Position in 52-week range (at high = strong)
  4. Volume trend (rising volume on up moves)
"""
from __future__ import annotations
import statistics
from .base import BaseScorer, ScoreResult


class MomentumScorer(BaseScorer):
    name = "momentum"

    def score(self, data: dict) -> ScoreResult:
        price_hist = data.get("price_history", [])
        snap = data.get("valuation", {})

        sub = {}
        flags = []
        available = 0

        price_hist = sorted(price_hist, key=lambda x: x.get("date", ""), reverse=True)
        prices = [float(x.get("close", x.get("last", 0))) for x in price_hist if x.get("close") or x.get("last")]

        if not prices:
            return ScoreResult(0.5, {}, "No price data", 0.0, ["no_price_data"])

        current_price = prices[0]

        # --- 1. 3-month return (~63 trading days) ---
        if len(prices) >= 63:
            available += 1
            ret_3m = (current_price - prices[62]) / prices[62]
            # Score: >20% = 1.0, >10% = 0.8, >0% = 0.6, >-10% = 0.4, <-20% = 0.1
            if ret_3m >= 0.20:
                sub["ret_3m"] = 1.0
            elif ret_3m >= 0.10:
                sub["ret_3m"] = 0.80
            elif ret_3m >= 0.0:
                sub["ret_3m"] = 0.60
            elif ret_3m >= -0.10:
                sub["ret_3m"] = 0.40
                flags.append("3m_negative")
            else:
                sub["ret_3m"] = 0.15
                flags.append("3m_sharp_decline")
        else:
            sub["ret_3m"] = 0.5

        # --- 2. 6-month return (~126 trading days) ---
        if len(prices) >= 126:
            available += 1
            ret_6m = (current_price - prices[125]) / prices[125]
            if ret_6m >= 0.30:
                sub["ret_6m"] = 1.0
            elif ret_6m >= 0.15:
                sub["ret_6m"] = 0.80
            elif ret_6m >= 0.0:
                sub["ret_6m"] = 0.60
            elif ret_6m >= -0.15:
                sub["ret_6m"] = 0.35
            else:
                sub["ret_6m"] = 0.1
                flags.append("6m_sharp_decline")
        else:
            sub["ret_6m"] = 0.5

        # --- 3. 52-week range position ---
        week52_high = snap.get("week52_high")
        week52_low = snap.get("week52_low")
        if not week52_high and len(prices) >= 252:
            week52_high = max(prices[:252])
            week52_low = min(prices[:252])

        if week52_high and week52_low and week52_high > week52_low:
            available += 1
            position = (current_price - week52_low) / (week52_high - week52_low)
            # 80%+ of range = strong uptrend; near 52w low = potential reversal or falling knife
            if position >= 0.80:
                sub["range_position"] = 0.90
            elif position >= 0.60:
                sub["range_position"] = 0.75
            elif position >= 0.40:
                sub["range_position"] = 0.55
            elif position >= 0.20:
                sub["range_position"] = 0.40
            else:
                sub["range_position"] = 0.25
                flags.append("near_52w_low")
        else:
            sub["range_position"] = 0.5

        # --- 4. Volume trend (optional — check for rising volume on up days) ---
        volumes = [float(x.get("volume", 0)) for x in price_hist[:63] if x.get("volume")]
        if len(volumes) >= 20:
            available += 1
            recent_vol = statistics.mean(volumes[:20])
            older_vol = statistics.mean(volumes[20:min(60, len(volumes))])
            if older_vol > 0:
                vol_ratio = recent_vol / older_vol
                if vol_ratio >= 1.3:
                    sub["volume_trend"] = 0.85
                    flags.append("volume_expansion")
                elif vol_ratio >= 1.0:
                    sub["volume_trend"] = 0.65
                elif vol_ratio >= 0.7:
                    sub["volume_trend"] = 0.50
                else:
                    sub["volume_trend"] = 0.35
                    flags.append("volume_drying_up")
            else:
                sub["volume_trend"] = 0.5
        else:
            sub["volume_trend"] = 0.5

        weights = {
            "ret_3m": 0.35,
            "ret_6m": 0.30,
            "range_position": 0.20,
            "volume_trend": 0.15,
        }
        composite = sum(sub.get(k, 0.5) * w for k, w in weights.items())
        dq = self._clamp(available / 3)

        ret_3m_v = sub.get("ret_3m", 0.5)
        reasoning = f"3M-ret-score={ret_3m_v:.2f}, range={sub.get('range_position', 0):.2f}"
        if flags:
            reasoning += f" [{', '.join(flags[:2])}]"

        return ScoreResult(
            score=self._clamp(composite),
            sub_scores=sub,
            reasoning=reasoning,
            data_quality=dq,
            flags=flags,
        )
