"""
DataFetcher — pulls all required data for a symbol using NSELIB.

Returns a normalized `data` dict ready for the CompositeScorer:
{
    "symbol": str,
    "name": str,
    "sector": str,
    "financials": [...],      # 4 quarters of P&L + balance sheet + cash flow
    "valuation": {...},       # PE, PB, D/E, promoter holding, 52w H/L, price
    "management": {...},      # promoter pledge, dividend history
    "price_history": [...],   # OHLCV, last 365 days
    "corporate_actions": [...],
    "shareholding": {...},
    "raw": {...},             # raw NSELIB responses for debugging
}
"""
from __future__ import annotations
import sys
import os
from datetime import date, timedelta
from typing import Optional
import traceback


def _load_nse():
    """Import NSE from NSELIB, handling the path."""
    nselib_path = os.path.expanduser("~/NSELIB")
    if nselib_path not in sys.path:
        sys.path.insert(0, nselib_path)
    from nselib import NSE
    return NSE


class DataFetcher:
    def __init__(self, guard: bool = False):
        NSE = _load_nse()
        # guard=False for cloud (live API blocked; use archive methods)
        self._nse = NSE(guard=guard)

    def fetch(self, symbol: str, periods: int = 4) -> dict:
        """
        Fetch all data for `symbol`. Returns normalized scorer-ready dict.
        `periods` = number of quarters of financial data to fetch.
        """
        sym = symbol.upper()
        result = {
            "symbol": sym,
            "name": "",
            "sector": "",
            "financials": [],
            "valuation": {},
            "management": {},
            "price_history": [],
            "corporate_actions": [],
            "shareholding": {},
            "raw": {},
            "fetch_errors": [],
        }

        # --- Financial results (XBRL parsed) ---
        try:
            detail = self._nse.financial_results_detail(sym, consolidated=True)
            if not detail:
                detail = self._nse.financial_results_detail(sym, consolidated=False)
            result["raw"]["financial_detail"] = detail
            result["financials"] = self._parse_financials(detail, periods)
        except Exception as e:
            result["fetch_errors"].append(f"financial_results_detail: {e}")

        # --- Historical price data (archive-safe) ---
        try:
            to_date = date.today()
            from_date = to_date - timedelta(days=400)
            hist = self._nse.fetch_equity_historical_data(
                sym,
                from_date=from_date,
                to_date=to_date,
            )
            result["raw"]["price_history"] = hist[:5] if hist else []  # sample only in raw
            result["price_history"] = self._parse_price_history(hist)
        except Exception as e:
            result["fetch_errors"].append(f"price_history: {e}")

        # --- Shareholding patterns (archive-safe via .in fallback) ---
        try:
            sh = self._nse.shareholding_patterns(sym)
            result["raw"]["shareholding"] = sh[:2] if sh else []
            result["shareholding"] = self._parse_shareholding(sh)
        except Exception as e:
            result["fetch_errors"].append(f"shareholding: {e}")

        # --- Corporate actions ---
        try:
            actions = self._nse.corporate_actions(sym)
            result["corporate_actions"] = actions[:10] if actions else []
        except Exception as e:
            result["fetch_errors"].append(f"corporate_actions: {e}")

        # --- Equity meta info (PE, PB from live if accessible, else skip) ---
        try:
            meta = self._nse.equity_meta_info(sym)
            result["raw"]["meta"] = meta
            result["name"] = meta.get("company_name") or meta.get("companyName") or sym
            result["sector"] = meta.get("sector") or meta.get("industry", "")
        except Exception as e:
            result["fetch_errors"].append(f"equity_meta_info: {e}")

        # --- Compile valuation dict ---
        result["valuation"] = self._compile_valuation(result)

        # --- Compile management dict ---
        result["management"] = self._compile_management(result)

        return result

    # -----------------------------------------------------------------------
    # Parsers
    # -----------------------------------------------------------------------

    def _parse_financials(self, detail: list, periods: int) -> list:
        """
        Normalize XBRL financial detail into scorer-ready format.
        Each item: {period, revenue, pat, ebitda, cfo, receivables, inventory,
                    capex, roce, eps, debt_equity}
        """
        parsed = []
        for q in (detail or [])[:periods]:
            pl = q.get("profit_loss", {})
            bs = q.get("balance_sheet", {})
            cf = q.get("cash_flow", {})
            meta = q.get("meta", {})

            revenue = pl.get("revenue_from_operations") or pl.get("total_income") or 0
            pat = pl.get("pat") or pl.get("profit_after_tax") or 0
            ebitda = pl.get("ebitda") or 0
            cfo = cf.get("operating_activities") or cf.get("cfo") or None
            capex = cf.get("capex") or cf.get("purchase_of_fixed_assets") or None
            receivables = bs.get("trade_receivables") or bs.get("receivables") or None
            inventory = bs.get("inventories") or bs.get("inventory") or None
            total_debt = (bs.get("borrowings") or bs.get("total_borrowings") or 0)
            equity = (bs.get("equity") or bs.get("shareholders_equity") or 1)
            debt_equity = total_debt / equity if equity else None

            parsed.append({
                "period": q.get("period", meta.get("period_end", "")),
                "revenue": revenue,
                "pat": pat,
                "ebitda": ebitda,
                "cfo": cfo,
                "capex": capex,
                "receivables": receivables,
                "inventory": inventory,
                "debt_equity": debt_equity,
                "eps": pl.get("eps_basic") or pl.get("eps") or None,
                "roce": q.get("roce") or None,
            })
        return parsed

    def _parse_price_history(self, hist: list) -> list:
        """Normalize price history to {date, open, high, low, close, volume}."""
        result = []
        for row in (hist or []):
            result.append({
                "date": row.get("date") or row.get("CH_TIMESTAMP") or "",
                "open": float(row.get("open") or row.get("CH_OPENING_PRICE") or 0),
                "high": float(row.get("high") or row.get("CH_TRADE_HIGH_PRICE") or 0),
                "low": float(row.get("low") or row.get("CH_TRADE_LOW_PRICE") or 0),
                "close": float(row.get("close") or row.get("CH_CLOSING_PRICE") or 0),
                "volume": float(row.get("volume") or row.get("CH_TOT_TRADED_QTY") or 0),
            })
        return sorted(result, key=lambda x: x["date"], reverse=True)

    def _parse_shareholding(self, sh: list) -> dict:
        """Extract latest shareholding breakdown."""
        if not sh:
            return {}
        latest = sh[0] if isinstance(sh, list) else sh
        # NSE shareholding keys vary; try common patterns
        promoter = (latest.get("promoter") or latest.get("promoterAndPromoterGroup") or {})
        if isinstance(promoter, dict):
            promoter_pct = promoter.get("total") or promoter.get("percentOfShareHolding")
        else:
            promoter_pct = promoter
        return {
            "date": latest.get("date", ""),
            "promoter_holding_pct": promoter_pct,
            "pledge_pct": latest.get("pledge") or latest.get("encumbered"),
        }

    def _compile_valuation(self, result: dict) -> dict:
        """Build valuation dict from all available data sources."""
        val = {}
        meta = result["raw"].get("meta", {})
        fins = result["financials"]

        # Price
        val["price"] = meta.get("lastPrice") or meta.get("currentMarketPrice")

        # 52-week H/L
        val["week52_high"] = meta.get("week52HighDate") or meta.get("weekHigh52")
        val["week52_low"] = meta.get("week52LowDate") or meta.get("weekLow52")

        # Try to get numeric 52w values from price history
        ph = result["price_history"]
        if ph and len(ph) >= 10:
            closes = [r["close"] for r in ph[:252] if r["close"] > 0]
            if closes:
                val["week52_high"] = max(closes)
                val["week52_low"] = min(closes)
                val["price"] = val["price"] or closes[0]

        # P/E, P/B from meta (live API if accessible)
        val["pe_ratio"] = meta.get("pdSymbolPe") or meta.get("priceEarningRatio")
        val["pb_ratio"] = meta.get("pbRatio") or meta.get("priceToBook")

        # Debt/equity from latest financials
        if fins:
            val["debt_equity"] = fins[0].get("debt_equity")
            # 3-year PAT growth
            if len(fins) >= 4:
                pat_now = fins[0].get("pat", 0) or 0
                pat_then = fins[3].get("pat", 0) or 1
                if pat_then > 0 and pat_now > 0:
                    val["pat_growth_3y"] = (pat_now / pat_then) ** (4 / 3) - 1

        # Shareholding
        sh = result.get("shareholding", {})
        val["promoter_holding"] = sh.get("promoter_holding_pct")
        val["promoter_pledge"] = sh.get("pledge_pct")

        return val

    def _compile_management(self, result: dict) -> dict:
        """Build management dict."""
        mgmt = {}
        sh = result.get("shareholding", {})
        actions = result.get("corporate_actions", [])

        mgmt["promoter_holding_pct"] = sh.get("promoter_holding_pct")
        mgmt["promoter_pledge_pct"] = sh.get("pledge_pct", 0)

        # Dividend history
        dividends = [a for a in actions if "div" in str(a.get("purpose", "")).lower()]
        mgmt["dividend_consecutive_years"] = min(len(dividends), 10)

        return mgmt
