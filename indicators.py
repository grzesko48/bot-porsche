"""
indicators.py — WSKAŹNIKI TECHNICZNE
=====================================
Bot Porsche — czysta matematyka wskaźników potrzebnych do sizingu i bezpieczników.
Liczone z serii cen (pandas Series/DataFrame). Brak sieci. W 100% testowalne offline.

Wskaźniki:
  • ATR(14) i ATR% — zmienność (do skalara zmienności DCM i do stop loss)
  • RSI(14) — wykupienie (bezpiecznik anty-szczyt)
  • SMA(20) i cena/SMA20 — wykrywanie paraboli
  • dollar volume — płynność (cena × wolumen)

ATR liczymy z High/Low/Close gdy są dostępne; gdy mamy tylko Close (np. stooq),
przybliżamy True Range jako |Close_t - Close_t-1| (mniej dokładne, ale działa jako fallback).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("porsche.indicators")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> Optional[float]:
    """ATR(period) w jednostkach ceny (USD). Zwraca ostatnią wartość lub None."""
    try:
        h, l, c = high.astype(float), low.astype(float), close.astype(float)
        prev_close = c.shift(1)
        tr = pd.concat([(h - l).abs(), (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
        a = tr.rolling(window=period, min_periods=max(2, period // 2)).mean()
        val = a.dropna()
        return float(val.iloc[-1]) if len(val) else None
    except Exception as e:
        logger.warning("ATR błąd: %s", e)
        return None


def atr_from_close(close: pd.Series, period: int = 14) -> Optional[float]:
    """ATR przybliżony tylko z Close (fallback gdy brak High/Low). |ΔClose| jako TR."""
    try:
        c = close.astype(float)
        tr = c.diff().abs()
        a = tr.rolling(window=period, min_periods=max(2, period // 2)).mean()
        val = a.dropna()
        return float(val.iloc[-1]) if len(val) else None
    except Exception as e:
        logger.warning("ATR(close) błąd: %s", e)
        return None


def atr_pct(atr_value: Optional[float], price: float) -> Optional[float]:
    """ATR jako ułamek ceny (0.06 = 6%). Wejście do skalara zmienności DCM."""
    if atr_value is None or price <= 0:
        return None
    return atr_value / price


def rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    """RSI(period) metodą Wildera. Zwraca ostatnią wartość 0-100 lub None."""
    try:
        c = close.astype(float)
        delta = c.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        # ostatnie wartości avg_gain/avg_loss (po okresie rozbiegu)
        ag = avg_gain.dropna()
        al = avg_loss.dropna()
        if not len(ag) or not len(al):
            return None
        g, l = float(ag.iloc[-1]), float(al.iloc[-1])
        if l <= 0:                      # brak strat -> RSI = 100 (skrajne wykupienie)
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100 - (100 / (1 + rs))
    except Exception as e:
        logger.warning("RSI błąd: %s", e)
        return None


def sma(close: pd.Series, period: int = 20) -> Optional[float]:
    try:
        c = close.astype(float)
        s = c.rolling(window=period, min_periods=max(2, period // 2)).mean().dropna()
        return float(s.iloc[-1]) if len(s) else None
    except Exception as e:
        logger.warning("SMA błąd: %s", e)
        return None


def price_vs_sma20(close: pd.Series) -> Optional[float]:
    """Cena / SMA20 (np. 1.10 = 10% nad średnią). Do wykrywania paraboli."""
    s = sma(close, 20)
    try:
        last = float(close.astype(float).dropna().iloc[-1])
    except Exception:
        return None
    if s is None or s <= 0:
        return None
    return last / s


def dollar_volume(close: pd.Series, volume: pd.Series, window: int = 20) -> Optional[float]:
    """Średni dzienny obrót dolarowy (cena × wolumen) z ostatnich `window` sesji."""
    try:
        c = close.astype(float)
        v = volume.astype(float)
        dv = (c * v).rolling(window=window, min_periods=max(2, window // 2)).mean().dropna()
        return float(dv.iloc[-1]) if len(dv) else None
    except Exception as e:
        logger.warning("dollar_volume błąd: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST indicators (offline) ===")
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # seria wzrostowa
    n = 60
    close = pd.Series([100 + i * 0.5 for i in range(n)])
    high = close + 1.0
    low = close - 1.0
    vol = pd.Series([1_000_000] * n)

    a = atr(high, low, close, 14)
    check("ATR liczone (dodatnie)", a is not None and a > 0)
    ap = atr_pct(a, float(close.iloc[-1]))
    check("ATR% w sensownym zakresie (0-0.5)", ap is not None and 0 < ap < 0.5)

    r = rsi(close, 14)
    check("RSI trendu wzrostowego wysokie (>70)", r is not None and r > 70)

    # seria spadkowa -> RSI niskie
    close_dn = pd.Series([100 - i * 0.5 for i in range(n)])
    r2 = rsi(close_dn, 14)
    check("RSI trendu spadkowego niskie (<30)", r2 is not None and r2 < 30)

    s = sma(close, 20)
    check("SMA20 liczone", s is not None and s > 0)

    pv = price_vs_sma20(close)
    check("cena/SMA20 > 1 dla trendu wzrostowego", pv is not None and pv > 1.0)

    dv = dollar_volume(close, vol, 20)
    check("dollar volume ~ cena*1M", dv is not None and dv > 100_000_000)

    # fallback ATR z close
    af = atr_from_close(close, 14)
    check("ATR(close) fallback działa", af is not None and af > 0)

    # odporność na śmieci
    check("ATR pustej serii -> None", atr(pd.Series([], dtype=float), pd.Series([], dtype=float), pd.Series([], dtype=float)) is None)
    check("RSI krótkiej serii -> None", rsi(pd.Series([1.0, 2.0]), 14) is None)
    check("atr_pct przy cenie 0 -> None", atr_pct(5.0, 0) is None)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== indicators.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — wskaźniki techniczne")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
