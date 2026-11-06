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
            # Fetch annual XBRL for balance sheet + cash flow (not in quarterly XBRL)
            annual_bs_cf = self._fetch_annual_bs_cf(sym)
            result["raw"]["annual_bs_cf"] = annual_bs_cf
            result["financials"] = self._parse_financials(detail, periods, annual_bs_cf)
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

    def _fetch_annual_bs_cf(self, sym: str) -> dict:
        """
        Fetch the most recent annual XBRL filing and extract balance sheet + cash flow.
        Annual XBRL (Q4 year-end) has the full balance sheet and full-year cash flows
        that are absent from quarterly integrated-filing XBRL.
        """
        try:
            import sys, os
            nselib_path = os.path.expanduser("~/NSELIB")
            if nselib_path not in sys.path:
                sys.path.insert(0, nselib_path)
            from nselib.modules.corporate import _parse_xbrl

            annual_filings = self._nse.financial_results(sym, period="Annual")
            if not annual_filings:
                return {}

            # Try the first two (consolidated then standalone both filed)
            for filing in annual_filings[:4]:
                xbrl_url = filing.get("xbrl", "")
                if not xbrl_url or xbrl_url.endswith("-"):
                    continue
                try:
                    r = self._nse._session._s.get(xbrl_url, timeout=15)
                    r.raise_for_status()
                    xbrl = _parse_xbrl(r.content)

                    # Balance sheet
                    borrowings_current = xbrl.get("BorrowingsCurrent") or 0
                    borrowings_noncurrent = xbrl.get("BorrowingsNoncurrent") or 0
                    total_borrowings = (borrowings_current or 0) + (borrowings_noncurrent or 0)
                    equity = xbrl.get("Equity") or xbrl.get("TotalEquity") or None

                    bs = {
                        "total_assets":       xbrl.get("Assets"),
                        "current_assets":     xbrl.get("CurrentAssets"),
                        "noncurrent_assets":  xbrl.get("NoncurrentAssets"),
                        "cash":               xbrl.get("CashAndCashEquivalents"),
                        "trade_receivables":  xbrl.get("TradeReceivablesCurrent"),
                        "inventories":        xbrl.get("Inventories"),
                        "ppe":                xbrl.get("PropertyPlantAndEquipment"),
                        "equity":             equity,
                        "equity_share_capital": xbrl.get("EquityShareCapital"),
                        "borrowings_current": borrowings_current,
                        "borrowings_noncurrent": borrowings_noncurrent,
                        "total_borrowings":   total_borrowings,
                        "trade_payables":     xbrl.get("TradePayablesCurrent"),
                    }

                    # Cash flow (year-to-date cumulative in Q4 = full year)
                    cf = {
                        "cfo":      xbrl.get("CashFlowsFromUsedInOperatingActivities"),
                        "cfi":      xbrl.get("CashFlowsFromUsedInInvestingActivities"),
                        "cff":      xbrl.get("CashFlowsFromUsedInFinancingActivities"),
                        "capex":    xbrl.get("PurchaseOfPropertyPlantAndEquipment"),
                        "dividends_paid": xbrl.get("DividendsPaid"),
                    }

                    # Only return if we got meaningful data
                    if bs.get("total_assets") or cf.get("cfo"):
                        return {
                            "period_end": xbrl.get("DateOfEndOfReportingPeriod", filing.get("toDate")),
                            "balance_sheet": bs,
                            "cash_flow": cf,
                            "debt_equity": total_borrowings / equity if equity and equity > 0 else 0.0,
                            "revenue_annual": xbrl.get("RevenueFromOperations"),
                            "pat_annual": xbrl.get("ProfitLossForPeriod"),
                        }
                except Exception:
                    continue
        except Exception:
            pass
        return {}

    def _parse_financials(self, detail: list, periods: int, annual_bs_cf: dict = None) -> list:
        """
        Normalize XBRL financial detail into scorer-ready format.

        P&L fields are FLAT on the top-level dict in financial_results_detail output
        (not nested under 'profit_loss'). Balance sheet and cash flow are always None
        in quarterly XBRL — they come from the separate annual_bs_cf fetch.

        Each item: {period, revenue, pat, ebitda, cfo, receivables, inventory,
                    capex, roce, eps, debt_equity}
        """
        annual_bs = (annual_bs_cf or {}).get("balance_sheet", {})
        annual_cf = (annual_bs_cf or {}).get("cash_flow", {})
        annual_de = (annual_bs_cf or {}).get("debt_equity")

        parsed = []
        for i, q in enumerate((detail or [])[:periods]):
            # P&L fields are flat on q (not nested under 'profit_loss')
            revenue = q.get("revenue_from_operations") or q.get("total_income") or 0
            pat     = q.get("pat") or 0
            ebit    = q.get("ebit") or 0
            pbt     = q.get("pbt") or 0
            # EBITDA = EBIT + depreciation
            ebitda  = ebit + (q.get("depreciation") or 0) if ebit else 0

            # Balance sheet: not in quarterly XBRL — use annual for most recent quarter
            # For older quarters, carry forward annual values (best available)
            receivables = annual_bs.get("trade_receivables")
            inventory   = annual_bs.get("inventories")
            equity      = annual_bs.get("equity")
            total_borrowings = annual_bs.get("total_borrowings", 0) or 0
            debt_equity = annual_de if annual_de is not None else (
                total_borrowings / equity if equity and equity > 0 else None
            )

            # Cash flow: annual filing gives full-year OCF — attach to most recent quarter only
            cfo   = annual_cf.get("cfo") if i == 0 else None
            capex = annual_cf.get("capex") if i == 0 else None

            parsed.append({
                "period":       q.get("period_end") or q.get("period") or "",
                "revenue":      revenue,
                "pat":          pat,
                "ebitda":       ebitda,
                "cfo":          cfo,
                "capex":        capex,
                "receivables":  receivables,
                "inventory":    inventory,
                "debt_equity":  debt_equity,
                "eps":          q.get("eps_basic") or q.get("eps_diluted") or None,
                "roce":         None,  # not in XBRL, computed below if data allows
            })

        # Compute trailing 12M (TTM) ROCE for the latest quarter if we have enough data
        if parsed and equity and equity > 0:
            ttm_ebit = sum(q.get("ebitda", 0) or 0 for q in parsed[:4])
            # Rough capital employed = equity + total_borrowings
            cap_employed = equity + total_borrowings
            if cap_employed > 0 and ttm_ebit > 0:
                parsed[0]["roce"] = round(ttm_ebit / cap_employed, 4)

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

        # P/E from meta (live API if accessible), fallback to computed TTM PE
        val["pe_ratio"] = meta.get("pdSymbolPe") or meta.get("priceEarningRatio")
        val["pb_ratio"] = meta.get("pbRatio") or meta.get("priceToBook")

        # Debt/equity and computed valuations from financials
        if fins:
            val["debt_equity"] = fins[0].get("debt_equity")

            # TTM (trailing 12M) PAT = sum of last 4 quarters
            ttm_pat = sum(q.get("pat", 0) or 0 for q in fins[:4])

            # TTM EPS = sum of quarterly EPS
            eps_values = [q.get("eps") for q in fins[:4] if q.get("eps") is not None]
            ttm_eps = sum(eps_values) if eps_values else None

            # Compute PE from price / TTM EPS if live PE not available
            price = val.get("price")
            if not val["pe_ratio"] and price and ttm_eps and ttm_eps > 0:
                val["pe_ratio"] = round(price / ttm_eps, 1)
                val["pe_ratio_source"] = "computed_ttm"

            # PAT growth: compare most recent quarter vs same quarter a year ago (Q5)
            if len(fins) >= 5:
                pat_now = fins[0].get("pat", 0) or 0
                pat_year_ago = fins[4].get("pat", 0) or 1
                if pat_year_ago > 0 and pat_now > 0:
                    val["pat_growth_yoy"] = (pat_now - pat_year_ago) / pat_year_ago
            # 3-year approximation from 4 quarters of data
            if len(fins) >= 4:
                pat_now = fins[0].get("pat", 0) or 0
                pat_then = fins[3].get("pat", 0) or 1
                if pat_then > 0 and pat_now > 0:
                    val["pat_growth_3y"] = (pat_now / pat_then) ** (4 / 3) - 1

            val["ttm_pat"] = ttm_pat
            val["ttm_eps"] = ttm_eps

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
