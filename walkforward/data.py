"""
Historical data layer for walk-forward simulation.

Two caches stored under ~/.equity_wf/:
  prices/{SYMBOL}.json   — {date_str: close_price} for all trading days downloaded
  xbrl/{SYMBOL}.json     — [{period_end, revenue, pat, ebitda, eps, ...}] one entry per filing

Both are build-once, read-many. The blind constraint is enforced by the snapshot
builder (data.py:build_snapshot), which slices each cache at as_of_date.
"""
from __future__ import annotations
import io, csv, json, zipfile, sys, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.expanduser("~/NSELIB"))
from nselib import NSE
from nselib.modules.corporate import _parse_xbrl

CACHE_DIR = Path.home() / ".equity_wf"
(CACHE_DIR / "prices").mkdir(parents=True, exist_ok=True)
(CACHE_DIR / "xbrl").mkdir(parents=True, exist_ok=True)

# ── URL helpers ──────────────────────────────────────────────────────────────

def _url_new(d: date) -> str:
    return (f"https://nsearchives.nseindia.com/content/cm"
            f"/BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")

def _url_old(d: date) -> str:
    mon = d.strftime("%b").upper()
    return (f"https://nsearchives.nseindia.com/content/historical/EQUITIES"
            f"/{d.year}/{mon}/cm{d.strftime('%d')}{mon}{d.year}bhav.csv.zip")

# ── Price cache ──────────────────────────────────────────────────────────────

def _load_price_cache(symbol: str) -> dict:
    p = CACHE_DIR / "prices" / f"{symbol}.json"
    return json.loads(p.read_text()) if p.exists() else {}

def _save_price_cache(symbol: str, cache: dict):
    p = CACHE_DIR / "prices" / f"{symbol}.json"
    p.write_text(json.dumps(cache, sort_keys=True))

def _fetch_close_for_date(session, d: date, symbol: str) -> Optional[float]:
    """Fetch closing price for symbol on date d. Try new format then old."""
    for url_fn in [_url_new, _url_old]:
        url = url_fn(d)
        try:
            r = session._s.get(url, timeout=15)
            if r.status_code != 200 or b"DOCTYPE" in r.content[:100]:
                continue
            z = zipfile.ZipFile(io.BytesIO(r.content))
            text = z.open(z.namelist()[0]).read().decode(errors="replace")
            for row in csv.DictReader(text.splitlines()):
                # Handle both old (SYMBOL) and new (TckrSymb) column names
                sym_val = row.get("SYMBOL") or row.get("TckrSymb") or ""
                if sym_val.strip() == symbol:
                    close_val = row.get("CLOSE") or row.get("ClsPric") or ""
                    try:
                        return float(close_val.replace(",", ""))
                    except (ValueError, AttributeError):
                        pass
        except Exception:
            continue
    return None

def build_price_cache(symbol: str, start: date, end: date,
                      nse: NSE, workers: int = 12) -> dict:
    """
    Download bhavcopy for every trading day in [start, end] and cache closes.
    Uses threads for speed. Skips dates already in cache. Returns full cache.
    """
    cache = _load_price_cache(symbol)

    # Enumerate all calendar days (bhavcopy only exists on trading days;
    # missing files are silently skipped)
    all_dates = []
    cur = start
    while cur <= end:
        ds = cur.isoformat()
        if ds not in cache and cur.weekday() < 5:  # skip weekends
            all_dates.append(cur)
        cur += timedelta(days=1)

    if not all_dates:
        print(f"  Price cache up to date for {symbol} ({len(cache)} days)")
        return cache

    print(f"  Downloading {len(all_dates)} bhavcopy files for {symbol} "
          f"[{start} → {end}] ({workers} threads)...")

    session = nse._session

    def _fetch(d: date):
        close = _fetch_close_for_date(session, d, symbol)
        return d.isoformat(), close

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, d): d for d in all_dates}
        for fut in as_completed(futures):
            ds, close = fut.result()
            if close is not None:
                cache[ds] = close
            done += 1
            if done % 100 == 0:
                pct = done / len(all_dates) * 100
                print(f"    {done}/{len(all_dates)} ({pct:.0f}%) ...")

    _save_price_cache(symbol, cache)
    print(f"  Price cache saved: {len(cache)} trading days for {symbol}")
    return cache

# ── XBRL cache ───────────────────────────────────────────────────────────────

def _load_xbrl_cache(symbol: str) -> list:
    p = CACHE_DIR / "xbrl" / f"{symbol}.json"
    return json.loads(p.read_text()) if p.exists() else []

def _save_xbrl_cache(symbol: str, records: list):
    p = CACHE_DIR / "xbrl" / f"{symbol}.json"
    p.write_text(json.dumps(records, indent=2, default=str))

def build_xbrl_cache(symbol: str, nse: NSE) -> list:
    """
    Download and parse all available quarterly XBRL filings for symbol.
    Caches to ~/.equity_wf/xbrl/{SYMBOL}.json. Returns list of parsed records.
    """
    cached = _load_xbrl_cache(symbol)
    cached_periods = {r["period_end"] for r in cached}

    filings = nse.financial_results(symbol, period="Quarterly")
    xbrl_filings = [
        (f.get("toDate"), f.get("xbrl", ""), f.get("consolidated", False))
        for f in filings
        if f.get("xbrl") and not f.get("xbrl", "").endswith("-")
    ]

    new_records = []
    for to_date, xbrl_url, consolidated in xbrl_filings:
        # Normalise date to YYYY-MM-DD
        try:
            d = _parse_nse_date(to_date)
            period_key = d.isoformat()
        except Exception:
            continue

        if period_key in cached_periods:
            continue

        try:
            r = nse._session._s.get(xbrl_url, timeout=20)
            r.raise_for_status()
            xbrl = _parse_xbrl(r.content)
        except Exception as e:
            print(f"  XBRL fetch failed for {to_date}: {e}")
            continue

        revenue = xbrl.get("RevenueFromOperations") or xbrl.get("TotalIncome") or 0
        pat     = xbrl.get("ProfitLossForPeriod") or 0
        ebit    = xbrl.get("ProfitBeforeTax") or 0   # approx if no EBIT tag
        depr    = xbrl.get("DepreciationDepletionAndAmortisation") or 0
        eps_b   = (xbrl.get("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
                   or xbrl.get("EarningsPerShareBasic") or xbrl.get("BasicEPS") or None)
        eps_d   = (xbrl.get("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
                   or xbrl.get("EarningsPerShareDiluted") or xbrl.get("DilutedEPS") or None)

        # Balance sheet (may be absent in quarterly filings)
        equity     = xbrl.get("Equity") or xbrl.get("TotalEquity") or None
        borrow_c   = xbrl.get("BorrowingsCurrent") or 0
        borrow_nc  = xbrl.get("BorrowingsNoncurrent") or 0
        total_bor  = borrow_c + borrow_nc
        receivables = xbrl.get("TradeReceivablesCurrent") or None
        inventories = xbrl.get("Inventories") or None

        # Cash flow (usually absent in quarterly, present in annual)
        cfo   = xbrl.get("CashFlowsFromUsedInOperatingActivities") or None
        capex = xbrl.get("PurchaseOfPropertyPlantAndEquipment") or None

        record = {
            "period_end":    period_key,
            "filing_date":   to_date,
            "consolidated":  consolidated,
            "revenue":       revenue,
            "pat":           pat,
            "ebit":          ebit,
            "depreciation":  depr,
            "ebitda":        (ebit + depr) if ebit else 0,
            "eps":           eps_b or eps_d,
            "equity":        equity,
            "total_borrowings": total_bor,
            "receivables":   receivables,
            "inventories":   inventories,
            "cfo":           cfo,
            "capex":         capex,
            "debt_equity":   (total_bor / equity) if equity and equity > 0 else None,
        }
        new_records.append(record)
        print(f"  XBRL parsed: {period_key}  rev={revenue}  pat={pat}  eps={eps_b or eps_d}")

    if new_records:
        all_records = cached + new_records
    else:
        all_records = cached

    # Deduplicate: per quarter-end, keep the filing with highest revenue (consolidated)
    by_period: dict = {}
    for rec in all_records:
        key = rec["period_end"]
        if key not in by_period or (rec.get("revenue") or 0) > (by_period[key].get("revenue") or 0):
            by_period[key] = rec
    all_records = sorted(by_period.values(), key=lambda x: x["period_end"], reverse=True)

    if new_records:
        _save_xbrl_cache(symbol, all_records)
        print(f"  XBRL cache saved: {len(all_records)} quarters for {symbol}")
        return all_records

    print(f"  XBRL cache up to date for {symbol} ({len(all_records)} quarters)")
    return all_records

# ── Annual XBRL (balance sheet + cash flow) ──────────────────────────────────

def _load_annual_xbrl_cache(symbol: str) -> list:
    p = CACHE_DIR / "xbrl" / f"{symbol}_annual.json"
    return json.loads(p.read_text()) if p.exists() else []

def _save_annual_xbrl_cache(symbol: str, records: list):
    p = CACHE_DIR / "xbrl" / f"{symbol}_annual.json"
    p.write_text(json.dumps(records, indent=2, default=str))

def build_annual_xbrl_cache(symbol: str, nse: NSE) -> list:
    """Download annual XBRL filings (year-end balance sheet + full-year OCF)."""
    cached = _load_annual_xbrl_cache(symbol)
    cached_periods = {r["period_end"] for r in cached}

    filings = nse.financial_results(symbol, period="Annual")
    xbrl_filings = [
        (f.get("toDate"), f.get("xbrl", ""))
        for f in filings
        if f.get("xbrl") and not f.get("xbrl", "").endswith("-")
    ]

    new_records = []
    for to_date, xbrl_url in xbrl_filings:
        try:
            d = _parse_nse_date(to_date)
            period_key = d.isoformat()
        except Exception:
            continue

        if period_key in cached_periods:
            continue

        try:
            r = nse._session._s.get(xbrl_url, timeout=20)
            r.raise_for_status()
            xbrl = _parse_xbrl(r.content)
        except Exception as e:
            print(f"  Annual XBRL fetch failed for {to_date}: {e}")
            continue

        borrow_c  = xbrl.get("BorrowingsCurrent") or 0
        borrow_nc = xbrl.get("BorrowingsNoncurrent") or 0
        total_bor = borrow_c + borrow_nc
        equity    = xbrl.get("Equity") or xbrl.get("TotalEquity") or None

        record = {
            "period_end":     period_key,
            "filing_date":    to_date,
            "total_assets":   xbrl.get("Assets"),
            "cash":           xbrl.get("CashAndCashEquivalents"),
            "equity":         equity,
            "total_borrowings": total_bor,
            "receivables":    xbrl.get("TradeReceivablesCurrent"),
            "inventories":    xbrl.get("Inventories"),
            "cfo":            xbrl.get("CashFlowsFromUsedInOperatingActivities"),
            "capex":          xbrl.get("PurchaseOfPropertyPlantAndEquipment"),
            "debt_equity":    (total_bor / equity) if equity and equity > 0 else 0.0,
            "revenue_annual": xbrl.get("RevenueFromOperations"),
            "pat_annual":     xbrl.get("ProfitLossForPeriod"),
        }
        new_records.append(record)
        print(f"  Annual XBRL parsed: {period_key}  equity={equity}  cfo={record['cfo']}")

    if new_records:
        all_records = cached + new_records
        all_records.sort(key=lambda x: x["period_end"], reverse=True)
        _save_annual_xbrl_cache(symbol, all_records)
        return all_records

    return cached

# ── Snapshot builder (enforces blind constraint) ─────────────────────────────

def build_snapshot(
    symbol: str,
    name: str,
    as_of: date,
    price_cache: dict,
    xbrl_records: list,
    annual_xbrl: list,
    periods: int = 4,
) -> dict:
    """
    Build a scorer-ready dict using ONLY data available at as_of date.
    This is the blind constraint: nothing after as_of leaks in.
    """
    as_of_str = as_of.isoformat()

    # --- Price history (only dates <= as_of) ---
    ph_raw = sorted(
        [(ds, close) for ds, close in price_cache.items() if ds <= as_of_str],
        reverse=True,
    )
    # Keep last 400 trading days
    ph_raw = ph_raw[:400]
    price_history = [{"date": ds, "close": close, "open": close, "high": close,
                      "low": close, "volume": 0} for ds, close in ph_raw]

    current_price = ph_raw[0][1] if ph_raw else None

    # 52-week H/L
    closes = [c for _, c in ph_raw[:252] if c > 0]
    week52_high = max(closes) if closes else None
    week52_low  = min(closes) if closes else None

    # --- Quarterly XBRL (only filings with period_end <= as_of) ---
    available_q = sorted(
        [r for r in xbrl_records if r["period_end"] <= as_of_str],
        key=lambda x: x["period_end"],
        reverse=True,
    )[:periods]

    # --- Annual XBRL (most recent year-end <= as_of) ---
    available_annual = sorted(
        [r for r in annual_xbrl if r["period_end"] <= as_of_str],
        key=lambda x: x["period_end"],
        reverse=True,
    )
    ann = available_annual[0] if available_annual else {}

    # Build financials list
    financials = []
    for q in available_q:
        # Balance sheet from annual (best available at this point in time)
        receivables   = q.get("receivables") or ann.get("receivables")
        inventory     = q.get("inventories") or ann.get("inventories")
        equity        = q.get("equity") or ann.get("equity")
        total_borrow  = q.get("total_borrowings") or ann.get("total_borrowings") or 0
        debt_equity   = q.get("debt_equity")
        if debt_equity is None and equity and equity > 0:
            debt_equity = total_borrow / equity

        # CFO from annual, only on most recent quarter
        cfo   = ann.get("cfo") if q == available_q[0] else None
        capex = ann.get("capex") if q == available_q[0] else None

        financials.append({
            "period":       q["period_end"],
            "revenue":      q["revenue"],
            "pat":          q["pat"],
            "ebitda":       q["ebitda"],
            "cfo":          cfo,
            "capex":        capex,
            "receivables":  receivables,
            "inventory":    inventory,
            "debt_equity":  debt_equity,
            "eps":          q.get("eps"),
            "roce":         None,
        })

    # TTM metrics
    ttm_pat = sum(q.get("pat", 0) or 0 for q in financials[:4])
    eps_vals = [q.get("eps") for q in financials[:4] if q.get("eps") is not None]
    ttm_eps  = sum(eps_vals) if eps_vals else None
    pe_ratio = round(current_price / ttm_eps, 1) if (current_price and ttm_eps and ttm_eps > 0) else None

    valuation = {
        "price":              current_price,
        "week52_high":        week52_high,
        "week52_low":         week52_low,
        "pe_ratio":           pe_ratio,
        "pe_ratio_source":    "computed_ttm",
        "pb_ratio":           None,
        "debt_equity":        financials[0].get("debt_equity") if financials else None,
        "ttm_pat":            ttm_pat,
        "ttm_eps":            ttm_eps,
        "promoter_holding":   None,  # live API blocked
        "promoter_pledge":    None,
    }

    management = {
        "promoter_holding_pct":  None,
        "promoter_pledge_pct":   0,
        "mgmt_tone_score":       0.6,
        "dividend_consecutive_years": 0,
    }

    thesis_check = {
        "bull_case_score":        0.5,
        "bear_case_materialised": 0.3,
        "guidance_momentum":      0.5,
        "catalyst_progress":      0.5,
    }

    return {
        "symbol":           symbol,
        "name":             name,
        "sector":           "IT Services",
        "financials":       financials,
        "valuation":        valuation,
        "management":       management,
        "price_history":    price_history,
        "thesis_check":     thesis_check,
        "corporate_actions": [],
        "shareholding":     {},
        "as_of_date":       as_of_str,
    }

# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_nse_date(s: str) -> date:
    """Parse NSE date strings like '30-Sep-2024' or '2024-09-30'."""
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}")
