"""
trend_backtest.py — TREND-FOLLOWING (time-series momentum) na SUROWCACH / CRYPTO
================================================================================
Faza 1 (harness walidacji) + rdzeń Fazy 2 (strategia surowcowa). Odpowiada na pytanie:
czy time-series momentum (trend-following) na koszyku surowców/crypto ma EDGE — ZANIM
dotkniemy złotówki. To NAJLEPIEJ udokumentowana przewaga w finansach (Moskowitz-Ooi-Pedersen
"Time Series Momentum" 2012, AQR Managed Futures) — w przeciwieństwie do momentum akcji,
które u nas wyszło ≈ SPY.

STRATEGIA (świadomie konserwatywna — zgodna z anti-ruiną):
  • Dla każdego aktywa: trend UP jeśli zwrot za `lookback` dni > 0 (bez lookahead — dane <= t).
  • Pozycja LONG/FLAT (NIGDY short, NIGDY dźwignia). Trend down -> gotówka (kapitał chroniony).
  • Równa waga wśród aktywów z trendem UP; reszta w gotówce. Rebalans co `rebalance_days`.
  • Brak dźwigni = sizing jak spot (na XTB surowce/crypto bywają CFD z dźwignią — to ryzyko ruiny,
    NIE używamy; ekspozycja = jak posiadanie spot).

UCZCIWE OGRANICZENIA (czytaj zanim zaufasz):
  • Koszty: backtest liczy opcjonalny koszt rebalansu (spread). NIE modeluje swapu overnight ani
    contango/roll na CFD — na żywo to ZJADA edge; jeśli gramy CFD, odjąć swap (sygnał alarmowy).
  • Survivorship: tickery futures (GC=F itd.) to ciągłe kontrakty — bez delistingu, ale roll-adjusted.
  • Walk-forward: dzielimy próbkę in-sample / OOS — ufamy TYLKO gdy OOS też dodatni.

Uruchomienie:  python trend_backtest.py --selftest        (mechanika, dane syntetyczne)
               python trend_backtest.py --run             (na żywo z yfinance: surowce vs SPY)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

try:
    import numpy as np
    import pandas as pd
except Exception:  # pragma: no cover
    np = None
    pd = None


# domyślny koszyk surowców (yfinance ciągłe kontrakty futures)
DEFAULT_COMMODITIES = ["GC=F", "SI=F", "CL=F", "HG=F", "NG=F"]   # złoto, srebro, ropa, miedź, gaz
DEFAULT_CRYPTO = ["BTC-USD", "ETH-USD"]
BENCH = "SPY"


@dataclass
class TrendConfig:
    lookback_days: int = 252        # okno momentum (12 mies — klasyczne TS-momentum)
    rebalance_days: int = 21        # rebalans ~miesięczny
    max_assets: int = 5             # maks. ile pozycji naraz (reszta gotówka)
    cost_per_turn: float = 0.001    # 0.1% kosztu od obracanej części przy rebalansie (spread)
    ann_factor: float = 252.0       # dni handlowe / rok (Sharpe)


@dataclass
class TrendResult:
    name: str = ""
    years: float = 0.0
    cagr: float = 0.0
    sharpe: float = 0.0
    max_dd: float = 0.0
    time_in_market: float = 0.0     # śr. udział kapitału w rynku (0..1)
    corr_spy: float = 0.0           # korelacja dziennych zwrotów do SPY (wartość dywersyfikacji)
    final_mult: float = 1.0         # ile razy urósł kapitał
    n_days: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# METRYKI (lokalne, czyste)
# ─────────────────────────────────────────────────────────────────────────────
def _cagr(equity, ann_factor):
    if len(equity) < 2 or equity[0] <= 0:
        return 0.0
    yrs = len(equity) / ann_factor
    if yrs <= 0:
        return 0.0
    return float((equity[-1] / equity[0]) ** (1.0 / yrs) - 1.0)


def _sharpe(daily_rets, ann_factor):
    r = np.asarray(daily_rets, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * np.sqrt(ann_factor))


def _max_drawdown(equity):
    if not len(equity):
        return 0.0
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    return float(-dd.min())   # dodatnia liczba (np. 0.28 = -28%)


# ─────────────────────────────────────────────────────────────────────────────
# SILNIK BACKTESTU (no-lookahead)
# ─────────────────────────────────────────────────────────────────────────────
def backtest_trend(close_df, cfg: TrendConfig, bench_col: str = BENCH) -> "tuple":
    """close_df: DataFrame (index=daty, kolumny=tickery; może zawierać kolumnę benchmarku).
    Zwraca (TrendResult, equity_list, daily_port_rets). Bench wyłączony z koszyka, służy do korelacji."""
    assets = [c for c in close_df.columns if c != bench_col]
    px = close_df[assets].astype(float)
    daily_ret = px.pct_change().fillna(0.0)
    n = len(px)

    weights = pd.Series(0.0, index=assets)
    prev_weights = weights.copy()
    equity = [1.0]
    port_rets = [0.0]
    in_market = []
    turnover_cost_total = 0.0

    for i in range(1, n):
        # zwrot dnia i z wag ustalonych na koniec dnia i-1 (bez lookahead)
        day_ret = float((weights * daily_ret.iloc[i]).sum())
        # rebalans: na koniec dnia i, jeśli to dzień rebalansu, przelicz wagi na podstawie danych <= i
        if i % cfg.rebalance_days == 0 or i == 1:
            selected = []
            for a in assets:
                j0 = i - cfg.lookback_days
                if j0 < 0:
                    continue
                base = px[a].iloc[j0]
                cur = px[a].iloc[i]
                if base and base > 0 and (cur / base - 1.0) > 0.0:   # trend UP
                    selected.append(a)
            selected = selected[: cfg.max_assets]
            new_w = pd.Series(0.0, index=assets)
            if selected:
                w = 1.0 / len(selected)
                for a in selected:
                    new_w[a] = w
            # koszt rebalansu = obrót * cost
            turnover = float((new_w - prev_weights).abs().sum())
            cost = turnover * cfg.cost_per_turn
            turnover_cost_total += cost
            day_ret -= cost
            weights = new_w
            prev_weights = new_w.copy()
        equity.append(equity[-1] * (1.0 + day_ret))
        port_rets.append(day_ret)
        in_market.append(float(weights.sum()))

    res = TrendResult(name="trend")
    res.n_days = n
    res.years = n / cfg.ann_factor
    res.cagr = _cagr(equity, cfg.ann_factor)
    res.sharpe = _sharpe(port_rets[1:], cfg.ann_factor)
    res.max_dd = _max_drawdown(equity)
    res.time_in_market = float(np.mean(in_market)) if in_market else 0.0
    res.final_mult = equity[-1] / equity[0]
    if bench_col in close_df.columns:
        b = close_df[bench_col].pct_change().fillna(0.0).values[1:]
        p = np.asarray(port_rets[1:], dtype=float)
        m = min(len(b), len(p))
        if m > 2 and np.std(p[:m]) > 0 and np.std(b[:m]) > 0:
            res.corr_spy = float(np.corrcoef(p[:m], b[:m])[0, 1])
    return res, equity, port_rets


def buy_hold(close_df, col, ann_factor=252.0) -> TrendResult:
    """Kup-i-trzymaj pojedynczej kolumny (benchmark / koszyk EW) do porównania."""
    s = close_df[col].astype(float).dropna()
    rets = s.pct_change().fillna(0.0).values
    eq = (1.0 + pd.Series(rets)).cumprod().values
    r = TrendResult(name=f"buyhold:{col}")
    r.n_days = len(s)
    r.years = len(s) / ann_factor
    r.cagr = _cagr(list(eq), ann_factor)
    r.sharpe = _sharpe(rets[1:], ann_factor)
    r.max_dd = _max_drawdown(eq)
    r.time_in_market = 1.0
    r.final_mult = float(eq[-1] / eq[0]) if len(eq) and eq[0] else 1.0
    return r


def walk_forward(close_df, cfg: TrendConfig, bench_col: str = BENCH) -> "dict":
    """Dzieli próbkę na pół: in-sample (IS) i out-of-sample (OOS). Ufamy TYLKO gdy OOS dodatni."""
    n = len(close_df)
    mid = n // 2
    is_df = close_df.iloc[:mid]
    oos_df = close_df.iloc[mid:]
    is_res, _, _ = backtest_trend(is_df, cfg, bench_col)
    oos_res, _, _ = backtest_trend(oos_df, cfg, bench_col)
    return {"in_sample": is_res, "out_of_sample": oos_res}


# ─────────────────────────────────────────────────────────────────────────────
# URUCHOMIENIE NA ŻYWO (yfinance)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_history(tickers, period="10y"):
    import yfinance as yf
    data = yf.download(tickers, period=period, progress=False)
    close = data["Close"] if "Close" in data else data
    if isinstance(close, pd.Series):
        close = close.to_frame()
    return close.dropna(how="all").ffill().dropna()


def _print(res: TrendResult, tag=""):
    print(f"  {tag:18s} CAGR {res.cagr*100:6.1f}%  Sharpe {res.sharpe:5.2f}  "
          f"MaxDD {res.max_dd*100:5.1f}%  w rynku {res.time_in_market*100:4.0f}%  "
          f"korel.SPY {res.corr_spy:+.2f}  x{res.final_mult:.2f}")


def run_live(asset_set="commodities", period="10y") -> int:
    assets = DEFAULT_COMMODITIES if asset_set == "commodities" else DEFAULT_CRYPTO
    tickers = assets + [BENCH]
    print(f"=== TREND-FOLLOWING backtest: {asset_set} {assets} vs {BENCH} ({period}) ===")
    try:
        close = fetch_history(tickers, period)
    except Exception as e:
        print(f"  Pobranie danych nieudane (yfinance): {e}")
        return 1
    cfg = TrendConfig()
    res, _, _ = backtest_trend(close, cfg)
    print("\nSTRATEGIA (trend-following long/flat, bez dźwigni):")
    _print(res, "trend")
    print("\nPORÓWNANIA:")
    _print(buy_hold(close, BENCH), "kup-trzymaj SPY")
    # koszyk EW kup-i-trzymaj
    bsk = close[assets].astype(float)
    ew = bsk.pct_change().fillna(0.0).mean(axis=1)
    ew_eq = (1.0 + ew).cumprod().values
    bh = TrendResult(name="EW")
    bh.cagr = _cagr(list(ew_eq), cfg.ann_factor); bh.sharpe = _sharpe(ew.values[1:], cfg.ann_factor)
    bh.max_dd = _max_drawdown(ew_eq); bh.time_in_market = 1.0; bh.final_mult = float(ew_eq[-1])
    _print(bh, "kup-trzymaj koszyk")
    print("\nWALK-FORWARD (ufaj TYLKO gdy OOS dodatni):")
    wf = walk_forward(close, cfg)
    _print(wf["in_sample"], "IS (1. polowa)")
    _print(wf["out_of_sample"], "OOS (2. polowa)")
    verdict = "EDGE prawdopodobny" if (wf["out_of_sample"].sharpe > 0.3 and res.max_dd < buy_hold(close, BENCH).max_dd) else "BRAK pewnego edge / ostrożnie"
    print(f"\nWERDYKT: {verdict}. (Pamiętaj: na CFD odjąć swap/contango — to może skasować edge.)")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline, dane syntetyczne — test MECHANIKI, nie wyniku rynkowego)
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST trend_backtest (offline, mechanika) ===")
    if pd is None:
        print("  [FAIL] brak pandas/numpy"); return 1
    P = F = 0

    def ok(name, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {name}")
        else: F += 1; print(f"  [FAIL] {name}")

    N = 800
    idx = pd.RangeIndex(N)
    # aktywo trendujące w gore (geometryczny wzrost), crashujace (spadek), benchmark plaski
    up = pd.Series(100.0 * (1.004) ** np.arange(N), index=idx)
    down = pd.Series(100.0 * (0.997) ** np.arange(N), index=idx)
    flat = pd.Series(100.0 + np.sin(np.arange(N) / 30.0) * 2.0, index=idx)
    cfg = TrendConfig(lookback_days=200, rebalance_days=20, max_assets=5, cost_per_turn=0.0)

    # 1) sam trend UP -> strategia prawie caly czas w rynku, dodatni zwrot
    df_up = pd.DataFrame({"UP": up, "SPY": flat})
    r_up, _, _ = backtest_trend(df_up, cfg)
    ok("Trend UP: dodatni CAGR", r_up.cagr > 0)
    ok("Trend UP: wysoki czas w rynku (>70%)", r_up.time_in_market > 0.7)

    # 2) sam crash -> strategia ucieka w gotowke: MaxDD strategii << kup-i-trzymaj
    df_dn = pd.DataFrame({"DN": down, "SPY": flat})
    r_dn, _, _ = backtest_trend(df_dn, cfg)
    bh_dn = buy_hold(df_dn, "DN")
    ok("Crash: strategia chroni kapital (MaxDD < kup-trzymaj)", r_dn.max_dd < bh_dn.max_dd)
    ok("Crash: niski czas w rynku (<40%)", r_dn.time_in_market < 0.4)

    # 3) koszyk up+down -> trzyma trendera, unika crashera (dodatni wynik, MaxDD maly)
    df_mix = pd.DataFrame({"UP": up, "DN": down, "SPY": flat})
    r_mix, eq_mix, _ = backtest_trend(df_mix, cfg)
    ok("Koszyk: dodatni mnoznik kapitalu", r_mix.final_mult > 1.0)
    ok("Koszyk: MaxDD umiarkowany (<25%)", r_mix.max_dd < 0.25)

    # 4) no-lookahead: pierwsze lookback dni bez pozycji (brak danych) -> equity ~plaskie na starcie
    ok("No-lookahead: equity startuje od 1.0", abs(eq_mix[0] - 1.0) < 1e-9)
    early = eq_mix[: cfg.lookback_days]
    ok("No-lookahead: brak ruchu zanim jest historia momentum", max(early) - min(early) < 0.02)

    # 5) walk-forward zwraca dwa wyniki
    wf = walk_forward(df_mix, cfg)
    ok("Walk-forward: IS i OOS policzone", wf["in_sample"].n_days > 0 and wf["out_of_sample"].n_days > 0)

    # 6) korelacja do SPY policzona (flat SPY -> niska/dowolna, ale liczba)
    ok("Korelacja do SPY policzona (liczba)", isinstance(r_mix.corr_spy, float))

    # 7) metryki sensowne
    ok("Sharpe to liczba skonczona", np.isfinite(r_up.sharpe))
    ok("MaxDD nieujemny", r_dn.max_dd >= 0)

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== trend_backtest.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Trend-following backtest (surowce/crypto)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run", action="store_true", help="na żywo z yfinance")
    ap.add_argument("--assets", default="commodities", choices=["commodities", "crypto"])
    ap.add_argument("--period", default="10y")
    a = ap.parse_args()
    if a.selftest:
        return _run_selftest()
    if a.run:
        return run_live(a.assets, a.period)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
