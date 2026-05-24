#!/usr/bin/env python3
"""Fetch US stock market data for the daily closing report.

The script pulls the most recent trading-session snapshot from public data
sources (primarily Yahoo Finance via the yfinance library) and writes a single
JSON file that downstream consumers (e.g. the LLM report generator) can read.

Design goals
------------
* **No fabrication.** Every field that cannot be retrieved is written as
  ``null`` rather than guessed. The downstream prompt instructs the LLM to
  render ``null`` fields as "暂无可靠数据".
* **Resilient.** A failure for one ticker must not break the entire run.
* **Self-contained.** Aside from yfinance / pandas / requests / numpy, the
  script needs no extra services to run. FRED is supported when an API key is
  available, otherwise it gracefully degrades.

Usage
-----
::

    python scripts/fetch_market_data.py --output data/latest.json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError as exc:  # pragma: no cover - guard for missing dependency
    sys.stderr.write(
        "yfinance is required. Install it via `pip install -r requirements.txt`.\n"
    )
    raise

logger = logging.getLogger("meigu.fetch")

# --------------------------------------------------------------------------- #
# Universe definitions
# --------------------------------------------------------------------------- #

INDICES: Dict[str, str] = {
    "^DJI": "Dow Jones Industrial Average",
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq Composite",
    "^NDX": "Nasdaq 100",
    "^RUT": "Russell 2000",
    "^SOX": "PHLX Semiconductor (SOX)",
    "^VIX": "CBOE Volatility Index (VIX)",
    "^MOVE": "ICE BofA MOVE Index",
}

TREASURY_YIELDS: Dict[str, str] = {
    # yfinance uses CBOE-quoted yield indices. Values are quoted at 1/10 of pct
    # for ^IRX/^FVX/^TNX/^TYX, e.g. ^TNX = 45.6 means 4.56%. We normalise below.
    "^IRX": "13 Week T-Bill",
    "^FVX": "5Y Treasury Yield",
    "^TNX": "10Y Treasury Yield",
    "^TYX": "30Y Treasury Yield",
}

COMMODITIES_FX: Dict[str, str] = {
    "DX-Y.NYB": "US Dollar Index (DXY)",
    "GC=F": "Gold Futures",
    "SI=F": "Silver Futures",
    "CL=F": "WTI Crude Oil",
    "BZ=F": "Brent Crude Oil",
    "NG=F": "Natural Gas Futures",
    "BTC-USD": "Bitcoin (USD)",
    "ETH-USD": "Ethereum (USD)",
}

SECTOR_ETFS: Dict[str, Dict[str, str]] = {
    "XLK": {"name": "Technology Select Sector SPDR", "sector": "Information Technology"},
    "XLC": {"name": "Communication Services SPDR", "sector": "Communication Services"},
    "XLY": {"name": "Consumer Discretionary SPDR", "sector": "Consumer Discretionary"},
    "XLF": {"name": "Financial Select Sector SPDR", "sector": "Financials"},
    "XLI": {"name": "Industrial Select Sector SPDR", "sector": "Industrials"},
    "XLV": {"name": "Health Care Select Sector SPDR", "sector": "Health Care"},
    "XLP": {"name": "Consumer Staples SPDR", "sector": "Consumer Staples"},
    "XLE": {"name": "Energy Select Sector SPDR", "sector": "Energy"},
    "XLU": {"name": "Utilities Select Sector SPDR", "sector": "Utilities"},
    "XLB": {"name": "Materials Select Sector SPDR", "sector": "Materials"},
    "XLRE": {"name": "Real Estate Select Sector SPDR", "sector": "Real Estate"},
}

THEME_ETFS: Dict[str, Dict[str, str]] = {
    "SPY": {"name": "SPDR S&P 500 ETF", "theme": "Broad market"},
    "QQQ": {"name": "Invesco QQQ", "theme": "Mega-cap growth"},
    "IWM": {"name": "iShares Russell 2000", "theme": "Small caps"},
    "RSP": {"name": "Invesco S&P 500 Equal Weight", "theme": "Equal-weight"},
    "SCHG": {"name": "Schwab US Large-Cap Growth", "theme": "Large growth"},
    "VTV": {"name": "Vanguard Value ETF", "theme": "Large value"},
    "IWO": {"name": "iShares Russell 2000 Growth", "theme": "Small growth"},
    "IWN": {"name": "iShares Russell 2000 Value", "theme": "Small value"},
    "SMH": {"name": "VanEck Semiconductor", "theme": "Semis"},
    "SOXX": {"name": "iShares Semiconductor", "theme": "Semis"},
    "IGV": {"name": "iShares Expanded Tech-Software", "theme": "Software"},
    "CIBR": {"name": "First Trust Cybersecurity", "theme": "Cybersecurity"},
    "HACK": {"name": "Amplify Cybersecurity", "theme": "Cybersecurity"},
    "CLOU": {"name": "Global X Cloud Computing", "theme": "Cloud"},
    "WCLD": {"name": "WisdomTree Cloud Computing", "theme": "Cloud / SaaS"},
    "BOTZ": {"name": "Global X Robotics & AI", "theme": "AI / Robotics"},
    "AIQ": {"name": "Global X AI & Technology", "theme": "AI"},
}

# Stock universe -- grouped to reflect the prompt's section structure.
STOCK_UNIVERSE: Dict[str, List[str]] = {
    "magnificent_7": ["NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA"],
    "ai_hardware_semis": [
        "AMD", "AVGO", "MRVL", "MU", "TSM", "ASML", "ARM", "INTC", "QCOM",
        "SMCI", "DELL", "HPE", "ANET", "CLS", "VRT", "COHR", "LITE", "AAOI",
    ],
    "software_saas": [
        "CRM", "NOW", "SNOW", "ORCL", "ADBE", "PANW", "CRWD", "DDOG", "NET",
        "MDB", "PLTR", "APP", "TEAM", "WDAY", "INTU", "SHOP",
    ],
    "ai_power_datacenter": [
        "CEG", "VST", "NRG", "ETN", "PWR", "GEV", "FLNC", "OKLO", "SMR",
        "BE", "NEE", "SO", "DUK", "APLD", "IREN", "CORZ",
    ],
}

# FRED series -- only attempted when FRED_API_KEY is set.
FRED_SERIES: Dict[str, str] = {
    "DGS2": "2-Year Treasury Constant Maturity",
    "DGS10": "10-Year Treasury Constant Maturity",
    "DGS30": "30-Year Treasury Constant Maturity",
    "T10Y2Y": "10Y-2Y Treasury Spread",
    "DFF": "Effective Federal Funds Rate",
    "CPIAUCSL": "CPI for All Urban Consumers (NSA)",
    "PCEPI": "PCE Price Index",
    "UNRATE": "Unemployment Rate",
    "PAYEMS": "All Employees: Total Nonfarm",
    "UMCSENT": "U. of Michigan Consumer Sentiment",
}


# --------------------------------------------------------------------------- #
# Utility helpers
# --------------------------------------------------------------------------- #


def _round(value: Any, ndigits: int = 4) -> Optional[float]:
    """Round float-ish values defensively, returning ``None`` for NaN/inf."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits)


def _safe_pct(numer: Any, denom: Any) -> Optional[float]:
    """Return percentage change rounded to 4 decimals, or ``None``."""
    n = _round(numer, 6)
    d = _round(denom, 6)
    if n is None or d is None or d == 0:
        return None
    return round(((n / d) - 1.0) * 100.0, 4)


def _compute_rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder's RSI on the closing series. Requires period+1 observations."""
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    if delta.empty:
        return None
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1] if avg_loss.iloc[-1] != 0 else np.inf
    if math.isinf(rs):
        return 100.0
    return _round(100.0 - (100.0 / (1.0 + rs)), 2)


def _ma(closes: pd.Series, window: int) -> Optional[float]:
    if closes is None or len(closes) < window:
        return None
    return _round(closes.rolling(window=window).mean().iloc[-1], 4)


def _pct_over_window(closes: pd.Series, window_days: int) -> Optional[float]:
    if closes is None or len(closes) <= window_days:
        return None
    last = closes.iloc[-1]
    base = closes.iloc[-1 - window_days]
    return _safe_pct(last, base)


def _pct_ytd(history: pd.DataFrame) -> Optional[float]:
    """Percent change from the first close of the current calendar year."""
    if history is None or history.empty:
        return None
    closes = history["Close"].dropna()
    if closes.empty:
        return None
    last_idx = closes.index[-1]
    year_start_year = last_idx.year
    year_mask = closes.index.year == year_start_year
    if not year_mask.any():
        return None
    first_close = closes[year_mask].iloc[0]
    return _safe_pct(closes.iloc[-1], first_close)


def _ts_to_iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    try:
        if isinstance(ts, pd.Timestamp):
            return ts.tz_convert("UTC").isoformat() if ts.tzinfo else ts.isoformat()
        return pd.Timestamp(ts).isoformat()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Snapshot extraction
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    successes: List[str] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)


def _extract_snapshot(symbol: str, history: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Build a per-ticker snapshot dict from a DataFrame returned by yfinance."""
    if history is None or history.empty:
        return None

    closes = history["Close"].dropna()
    if closes.empty:
        return None

    # Use the last two distinct dates that have a close. If the most recent row
    # is intra-day with NaN close, dropna handles that already.
    if len(closes) < 2:
        last_close = float(closes.iloc[-1])
        prev_close = None
    else:
        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])

    last_date = closes.index[-1]
    last_high = history.loc[last_date, "High"] if "High" in history.columns else None
    last_low = history.loc[last_date, "Low"] if "Low" in history.columns else None
    last_volume = history.loc[last_date, "Volume"] if "Volume" in history.columns else None

    # 20-day average volume (excluding the latest bar).
    avg_vol_20 = None
    if "Volume" in history.columns and len(history) >= 21:
        avg_vol_20 = float(history["Volume"].iloc[-21:-1].mean())

    snapshot: Dict[str, Any] = {
        "symbol": symbol,
        "last_close": _round(last_close, 4),
        "prev_close": _round(prev_close, 4),
        "change_abs": _round(
            (last_close - prev_close) if prev_close is not None else None, 4
        ),
        "change_pct": _safe_pct(last_close, prev_close),
        "day_high": _round(last_high, 4),
        "day_low": _round(last_low, 4),
        "volume": _round(last_volume, 0),
        "avg_volume_20d": _round(avg_vol_20, 0),
        "volume_vs_avg_pct": _safe_pct(last_volume, avg_vol_20),
        "ma20": _ma(closes, 20),
        "ma50": _ma(closes, 50),
        "ma100": _ma(closes, 100),
        "ma200": _ma(closes, 200),
        "rsi_14": _compute_rsi(closes, 14),
        "pct_5d": _pct_over_window(closes, 5),
        "pct_1m": _pct_over_window(closes, 21),
        "pct_3m": _pct_over_window(closes, 63),
        "pct_ytd": _pct_ytd(history),
        "52w_high": _round(closes.iloc[-252:].max() if len(closes) >= 1 else None, 4),
        "52w_low": _round(closes.iloc[-252:].min() if len(closes) >= 1 else None, 4),
        "last_close_date": _ts_to_iso(last_date),
    }
    return snapshot


def _download_history(
    symbols: Iterable[str], period: str = "1y", interval: str = "1d"
) -> Dict[str, pd.DataFrame]:
    """Download daily history for many symbols in one call where possible."""
    syms = list(symbols)
    if not syms:
        return {}

    histories: Dict[str, pd.DataFrame] = {}
    try:
        df = yf.download(
            tickers=" ".join(syms),
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bulk download failed for %s: %s", syms, exc)
        df = None

    if df is not None and not df.empty:
        # When a single symbol is requested, yfinance returns flat columns.
        if len(syms) == 1:
            histories[syms[0]] = df.dropna(how="all")
            return histories
        for sym in syms:
            try:
                sub = df[sym].dropna(how="all")
                if not sub.empty:
                    histories[sym] = sub
            except KeyError:
                logger.debug("Symbol %s missing from bulk frame", sym)

    # Retry per-symbol for any that didn't come back.
    missing = [s for s in syms if s not in histories]
    for sym in missing:
        try:
            sub = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False)
            if sub is not None and not sub.empty:
                histories[sym] = sub.dropna(how="all")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Per-symbol fetch failed for %s: %s", sym, exc)

    return histories


def _build_group(
    symbols_meta: Dict[str, Any],
    histories: Dict[str, pd.DataFrame],
    *,
    yield_normalize: bool = False,
) -> Dict[str, Any]:
    """Wrap each symbol's snapshot under a {symbol: {...meta..., **snapshot}} dict."""
    out: Dict[str, Any] = {}
    for sym, meta in symbols_meta.items():
        history = histories.get(sym)
        snap = _extract_snapshot(sym, history) if history is not None else None
        if snap is None:
            out[sym] = {
                "symbol": sym,
                "name": meta if isinstance(meta, str) else meta.get("name"),
                "error": "no_data",
            }
            continue

        # CBOE yield indices like ^TNX are quoted as basis_points / 10. Normalise.
        if yield_normalize:
            for key in ("last_close", "prev_close", "day_high", "day_low",
                        "ma20", "ma50", "ma100", "ma200",
                        "52w_high", "52w_low"):
                if snap.get(key) is not None:
                    snap[key] = _round(snap[key] / 10.0 if snap[key] > 25 else snap[key], 4)
            # change_abs needs to be re-derived (in pct points) once normalised.
            if snap.get("last_close") is not None and snap.get("prev_close") is not None:
                snap["change_abs"] = _round(
                    snap["last_close"] - snap["prev_close"], 4
                )
                snap["change_pct"] = _safe_pct(snap["last_close"], snap["prev_close"])

        if isinstance(meta, str):
            snap["name"] = meta
        else:
            snap.update({k: v for k, v in meta.items()})
        out[sym] = snap
    return out


# --------------------------------------------------------------------------- #
# FRED (optional)
# --------------------------------------------------------------------------- #


def fetch_fred(series_id: str, api_key: str) -> Optional[Dict[str, Any]]:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "limit": 5,
        "sort_order": "desc",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("FRED fetch failed for %s: %s", series_id, exc)
        return None

    obs = data.get("observations") or []
    cleaned = []
    for o in obs:
        try:
            value = float(o.get("value"))
        except (TypeError, ValueError):
            continue
        cleaned.append({"date": o.get("date"), "value": value})
    if not cleaned:
        return None
    return {"latest": cleaned[0], "previous": cleaned[1] if len(cleaned) > 1 else None}


def fetch_fred_bundle(api_key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not api_key:
        return None
    out: Dict[str, Any] = {}
    for sid, name in FRED_SERIES.items():
        result = fetch_fred(sid, api_key)
        out[sid] = {"name": name, "data": result}
        time.sleep(0.1)  # courtesy delay
    return out


# --------------------------------------------------------------------------- #
# CME FedWatch (best-effort)
# --------------------------------------------------------------------------- #


def fetch_fed_watch() -> Optional[Dict[str, Any]]:
    """Attempt to fetch FedWatch probabilities from CME's public JSON endpoint.

    CME does not offer a stable public API. We make a best-effort call to the
    documented endpoint, but if it fails we return ``None`` so the LLM marks the
    section as 暂无可靠数据.
    """
    url = "https://www.cmegroup.com/services/cme-watch-tool-fedwatch.json"
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; meigu-daily-report/1.0; "
                    "+https://github.com/ProWD888/meigu)"
                ),
                "Accept": "application/json",
            },
        )
        if r.status_code != 200:
            logger.info("FedWatch endpoint returned %s", r.status_code)
            return None
        try:
            return r.json()
        except ValueError:
            logger.info("FedWatch endpoint did not return JSON")
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("FedWatch fetch failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Top-level run
# --------------------------------------------------------------------------- #


def determine_report_date(indices: Dict[str, Any]) -> Optional[str]:
    """Use the most recent S&P 500 close date as the canonical report date."""
    spx = indices.get("^GSPC") or {}
    iso = spx.get("last_close_date")
    if not iso:
        return None
    return iso.split("T")[0]


def build_payload(fred_api_key: Optional[str]) -> Dict[str, Any]:
    started = datetime.now(timezone.utc)
    logger.info("Fetch started at %s", started.isoformat())

    # 1. Indices, treasuries, commodities -- request together for speed.
    base_universe: List[str] = (
        list(INDICES.keys())
        + list(TREASURY_YIELDS.keys())
        + list(COMMODITIES_FX.keys())
        + list(SECTOR_ETFS.keys())
        + list(THEME_ETFS.keys())
    )
    # All stocks (flatten groups, deduped).
    stock_symbols: List[str] = []
    for group in STOCK_UNIVERSE.values():
        stock_symbols.extend(group)
    stock_symbols = list(dict.fromkeys(stock_symbols))

    all_symbols = list(dict.fromkeys(base_universe + stock_symbols))
    logger.info("Downloading %d symbols", len(all_symbols))
    histories = _download_history(all_symbols, period="2y", interval="1d")

    indices_block = _build_group(
        {sym: name for sym, name in INDICES.items()}, histories
    )
    yields_block = _build_group(
        {sym: name for sym, name in TREASURY_YIELDS.items()}, histories,
        yield_normalize=True,
    )
    commodities_block = _build_group(
        {sym: name for sym, name in COMMODITIES_FX.items()}, histories
    )
    sectors_block = _build_group(SECTOR_ETFS, histories)
    themes_block = _build_group(THEME_ETFS, histories)

    stocks_block: Dict[str, Dict[str, Any]] = {}
    for group_name, syms in STOCK_UNIVERSE.items():
        stocks_block[group_name] = _build_group(
            {sym: sym for sym in syms}, histories
        )

    # Yield curve spreads (in percentage points) -- defensive math.
    def _y(sym: str) -> Optional[float]:
        return yields_block.get(sym, {}).get("last_close")

    yield_spreads = {
        "2y_5y": (
            _round(_y("^FVX") - _y("^IRX"), 4) if _y("^FVX") and _y("^IRX") else None
        ),
        "5y_10y": (
            _round(_y("^TNX") - _y("^FVX"), 4) if _y("^TNX") and _y("^FVX") else None
        ),
        "10y_30y": (
            _round(_y("^TYX") - _y("^TNX"), 4) if _y("^TYX") and _y("^TNX") else None
        ),
    }

    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": started.isoformat(),
        "generated_at_beijing": (
            started.astimezone(timezone(timedelta(hours=8))).isoformat()
        ),
        "report_date": determine_report_date(indices_block),
        "indices": indices_block,
        "treasury_yields": yields_block,
        "yield_spreads_pct_points": yield_spreads,
        "commodities_fx": commodities_block,
        "sector_etfs": sectors_block,
        "theme_etfs": themes_block,
        "stocks": stocks_block,
        "fred_data": None,
        "fed_watch": None,
        "economic_calendar": None,  # left null on purpose; LLM marks unavailable
        "sources": [
            {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/"},
            {"name": "CNBC Markets", "url": "https://www.cnbc.com/markets/"},
            {"name": "Reuters Markets", "url": "https://www.reuters.com/markets/us/"},
            {"name": "MarketWatch", "url": "https://www.marketwatch.com/"},
            {"name": "FRED", "url": "https://fred.stlouisfed.org/"},
            {"name": "U.S. Treasury", "url": "https://home.treasury.gov/"},
            {"name": "CME FedWatch", "url": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"},
            {"name": "EIA", "url": "https://www.eia.gov/"},
        ],
        "notes": [],
    }

    # 2. FRED (optional).
    try:
        payload["fred_data"] = fetch_fred_bundle(fred_api_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("FRED bundle fetch failed: %s", exc)
        payload["notes"].append(f"fred_fetch_error: {exc}")

    # 3. FedWatch (best-effort).
    try:
        payload["fed_watch"] = fetch_fed_watch()
    except Exception as exc:  # noqa: BLE001
        logger.warning("FedWatch fetch failed: %s", exc)
        payload["notes"].append(f"fed_watch_error: {exc}")

    return payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch US market data snapshot")
    parser.add_argument(
        "--output", "-o", default="data/latest.json",
        help="Where to write the JSON snapshot (default: data/latest.json)",
    )
    parser.add_argument(
        "--log-level", default=os.getenv("LOG_LEVEL", "INFO"),
        help="Python logging level (default: INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        payload = build_payload(fred_api_key=os.getenv("FRED_API_KEY"))
    except Exception:
        logger.error("Fatal error during fetch:\n%s", traceback.format_exc())
        return 2

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)

    logger.info(
        "Wrote %s (report_date=%s, indices=%d, stocks=%d)",
        out_path,
        payload.get("report_date"),
        len(payload.get("indices", {})),
        sum(len(g) for g in payload.get("stocks", {}).values()),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
