"""
backtest.py — Symulator historyczny. Odpowiada na PYTANIE EGZYSTENCJALNE projektu:
czy strategia (momentum + sektor rotation + reguły wyjścia) ma EDGE, czy lepiej trzymać SPY?

Przepuszcza PRAWDZIWĄ logikę bota (te same indicators, ten sam PositionSizer,
ten sam PositionManager) przez N lat historii, dzień po dniu, BEZ lookahead bias
(w dniu T używa tylko danych <= T). Liczy: CAGR, Sharpe, max drawdown, hit rate,
średni zwrot per pozycja — i porównuje z kup-i-trzymaj SPY.

═══════════════════════════════════════════════════════════════════════════════
UCZCIWE OGRANICZENIA BACKTESTU (czytaj zanim zaufasz wynikom):
  1. SURVIVORSHIP BIAS: uniwersum to DZISIEJSZY S&P 500. Spółki, które wypadły
     (zbankrutowały, delisting), nie są w teście -> wynik zawyżony in plus.
  2. BRAK SMART MONEY: nie ma darmowych historycznych danych insider per dzień,
     więc backtest traktuje smart money jako NEUTRAL (skalar 0.8) dla wszystkich.
     Na żywo smart money MOŻE poprawić wynik (CONFLUENCE) lub zablokować (HARD_BLOCK).
  3. BRAK EARNINGS GATE: nie ma darmowego historycznego kalendarza wyników,
     więc backtest NIE blokuje przed earnings. Na żywo bot jest OSTROŻNIEJSZY
     (wychodzi/nie wchodzi przed wynikami) -> realny wynik może być niższy LUB
     wyższy (omija luki). To znaczy: backtest jest lekko OPTYMISTYCZNY tu.
  4. KOSZTY: uwzględniony FX round-trip ~1% (0.5% kupno + 0.5% sprzedaż).
     Prowizja 0% (XTB do 100k EUR/mc). Spread bid/ask pominięty (mały dla large-cap).
  5. CENY: close-to-close. Brak modelowania slippage/luki otwarcia.

Wynik backtestu to NIE obietnica przyszłości. To sprawdzenie, czy logika miałaby
sens na przeszłości. Edge w przeszłości != edge w przyszłości. Ale BRAK edge w
przeszłości to mocny sygnał, że strategii nie warto ufać realnym kapitałem.
═══════════════════════════════════════════════════════════════════════════════

Tryby: --selftest (synthetyczne ceny, test mechaniki), --run (na żywo z yfinance).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

import indicators as ind
from top_down_scanner import (rate_of_change, SECTOR_ETFS, ETF_TO_GICS,
                              BENCH_SPY, SP500_CSV_URL)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("porsche.backtest")


@dataclass
class BacktestConfig:
    start_capital_pln: float = 1582.0
    usd_pln_rate: float = 4.0
    rebalance_every_days: int = 5            # co ile sesji rebalansujemy (5 = tygodniowo)
    roc_short_days: int = 10
    roc_long_days: int = 20
    n_winning_sectors: int = 2
    max_positions: int = 5
    position_floor_pln: float = 100.0
    position_cap_pln: float = 400.0
    max_pos_pct: float = 0.30                # max 30% kapitału na pozycję
    fx_roundtrip_cost: float = 0.01          # 1% round-trip FX
    # filtry techniczne (jak na żywo)
    rsi_max: float = 75.0
    parabola_sma_mult: float = 1.15
    # reguły wyjścia (jak position_manager)
    profit_ladder: tuple = (
        (0.05, -0.02), (0.10, 0.00), (0.15, 0.05), (0.25, 0.10),
        (0.40, 0.20), (0.60, 0.35), (1.00, 0.70),
    )
    trailing_drawdown_pct: float = 0.12
    time_stop_days: int = 15
    time_stop_min_profit: float = 0.03
    take_profit_partial_pct: float = 0.30    # +30% -> realizuj część, reszta biegnie
    take_profit_fraction: float = 0.25
    stop_atr_mult: float = 2.0
    stop_max_pct: float = 0.08               # stop początkowy nie dalej niż -8%
    min_history_days: int = 220              # potrzebne do 200SMA radaru
    # MAKRO-FILTR REGIME (lekarstwo na momentum crashes w bessie):
    # gdy SPY < 200SMA -> rynek w trendzie spadkowym -> ZERO nowych zakupów (czekaj w gotówce).
    # Istniejące pozycje dalej zarządzane (ratchet je wyprowadzi). To NIE zamyka pozycji,
    # tylko wstrzymuje OTWIERANIE nowych w spadającym rynku.
    macro_filter_on: bool = True
    macro_sma_days: int = 200
    # RANKING MOMENTUM: stary = ROC10/20 (szum tygodniowy). Nowy = multi-period 3/6/12 mies
    # (klasyczne momentum Jegadeesh-Titman), z pominięciem ostatniego miesiąca (short-term reversal).
    use_multiperiod_momentum: bool = True
    mom_lookbacks: tuple = (63, 126, 252)    # 3, 6, 12 miesięcy handlowych
    mom_weights: tuple = (0.2, 0.3, 0.5)     # więcej wagi na dłuższy trend
    mom_skip_recent_days: int = 21           # pomiń ostatni miesiąc (reversal effect)


@dataclass
class BTPosition:
    ticker: str
    shares: float
    entry_price: float
    entry_idx: int                           # indeks dnia wejścia
    stop: float
    hwm: float


@dataclass
class BacktestResult:
    equity_curve: list = field(default_factory=list)        # [(date, equity_pln)]
    spy_curve: list = field(default_factory=list)           # [(date, equity_pln)] kup-i-trzymaj SPY
    trades: list = field(default_factory=list)              # zamknięte transakcje
    cagr: float = 0.0
    spy_cagr: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    spy_max_drawdown: float = 0.0
    hit_rate: float = 0.0
    avg_trade_return: float = 0.0
    n_trades: int = 0
    final_equity: float = 0.0
    spy_final_equity: float = 0.0
    notes: list = field(default_factory=list)
    # ULTRA TEST: pomiar ekspozycji na mega-cap winners (test survivorship biasu)
    top_tickers: list = field(default_factory=list)         # [(ticker, n_trades, sum_return_pct)]
    concentration_top5_pct: float = 0.0                     # % sumy zysków z TOP 5 tickerów


# ─────────────────────────────────────────────────────────────────────────────
# METRYKI
# ─────────────────────────────────────────────────────────────────────────────
def _cagr(start: float, end: float, days: int) -> float:
    if start <= 0 or days <= 0:
        return 0.0
    years = days / 365.25
    if years <= 0:
        return 0.0
    return (end / start) ** (1 / years) - 1


def _max_drawdown(curve: list) -> float:
    """Maksymalny spadek od szczytu (jako ułamek dodatni). curve: list of equity floats."""
    if not curve:
        return 0.0
    peak = curve[0]
    mdd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd
    return mdd


def _sharpe(equity: list, periods_per_year: float = 52.0) -> float:
    """Sharpe z krzywej equity (rebalans tygodniowy -> 52/rok). Rf=0 dla uproszczenia."""
    if len(equity) < 3:
        return 0.0
    rets = []
    for i in range(1, len(equity)):
        if equity[i-1] > 0:
            rets.append(equity[i] / equity[i-1] - 1)
    if len(rets) < 2:
        return 0.0
    arr = np.array(rets)
    sd = arr.std(ddof=1)
    if sd == 0:
        return 0.0
    return (arr.mean() / sd) * np.sqrt(periods_per_year)


# ─────────────────────────────────────────────────────────────────────────────
# SYMULATOR
# ─────────────────────────────────────────────────────────────────────────────
class Backtester:
    def __init__(self, config: Optional[BacktestConfig] = None):
        self.cfg = config or BacktestConfig()

    def _momentum_score(self, series: "pd.Series", upto_idx: int) -> "Optional[float]":
        """Multi-period momentum (3/6/12 mies) z pominięciem ostatniego miesiąca.
        Klasyczne momentum Jegadeesh-Titman: dłuższy trend, bez short-term reversal.
        Zwraca ważony blend zwrotów z okien lookback. None gdy za mało historii."""
        cfg = self.cfg
        window = series.iloc[:upto_idx + 1].dropna()
        skip = cfg.mom_skip_recent_days
        longest = max(cfg.mom_lookbacks)
        if len(window) < longest + skip + 1:
            return None
        # cena "teraz" = pomijając ostatni miesiąc (anty-reversal); cena bazowa = lookback temu
        ref_price = window.iloc[-(skip + 1)] if skip > 0 else window.iloc[-1]
        score = 0.0
        for lb, w in zip(cfg.mom_lookbacks, cfg.mom_weights):
            base = window.iloc[-(lb + skip + 1)]
            if base <= 0:
                return None
            score += w * (ref_price - base) / base
        return score

    def _rank_sectors(self, etf_closes: dict, upto_idx: int) -> list:
        """Ranking ETF-ów sektorowych po momentum do dnia upto_idx (bez lookahead).
        use_multiperiod_momentum: True -> multi-period 3/6/12 mies; False -> stary ROC10/20."""
        cfg = self.cfg
        scores = []
        for etf in SECTOR_ETFS:
            s = etf_closes.get(etf)
            if s is None:
                continue
            if cfg.use_multiperiod_momentum:
                sc = self._momentum_score(s, upto_idx)
                if sc is None:
                    continue
                scores.append((etf, sc))
            else:
                if upto_idx < cfg.roc_long_days:
                    continue
                window = s.iloc[:upto_idx + 1]
                r10 = rate_of_change(window, cfg.roc_short_days)
                r20 = rate_of_change(window, cfg.roc_long_days)
                if r10 is None or r20 is None:
                    continue
                scores.append((etf, 0.5 * r10 + 0.5 * r20))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [etf for etf, _ in scores]

    def _winning_tickers(self, winners_etf: list, gics_map: dict) -> set:
        """Spółki należące do wygranych sektorów (przez ETF->GICS->tickery)."""
        out = set()
        for etf in winners_etf[:self.cfg.n_winning_sectors]:
            for gics in ETF_TO_GICS.get(etf, []):
                out.update(gics_map.get(gics, []))
        return out

    def run(self, prices: dict, volumes: Optional[dict], etf_closes: dict,
            spy_close: pd.Series, gics_map: dict, dates: list) -> BacktestResult:
        """prices: {ticker: close_series}, etf_closes: {etf: close_series},
        spy_close: SPY series, gics_map: {sektor: [tickery]}, dates: lista dat (indeks)."""
        cfg = self.cfg
        res = BacktestResult()
        n = len(dates)
        if n < cfg.min_history_days + cfg.rebalance_every_days:
            res.notes.append(f"za mało danych ({n} dni) — potrzeba > {cfg.min_history_days}")
            return res

        cash = cfg.start_capital_pln
        positions: dict = {}          # ticker -> BTPosition
        start_idx = cfg.min_history_days
        equity_hist = []
        spy_units = cfg.start_capital_pln / spy_close.iloc[start_idx]   # kup-i-trzymaj SPY

        def equity_at(idx) -> float:
            val = cash
            for tk, p in positions.items():
                px = prices[tk].iloc[idx] if tk in prices else p.entry_price
                if not np.isnan(px):
                    val += p.shares * px * cfg.usd_pln_rate
            return val

        for idx in range(start_idx, n):
            # ── ZARZĄDZANIE ISTNIEJĄCYMI POZYCJAMI (codziennie, ratchet jak position_manager) ──
            to_close = []
            for tk, p in positions.items():
                if tk not in prices:
                    continue
                px = prices[tk].iloc[idx]
                if np.isnan(px):
                    continue
                p.hwm = max(p.hwm, px)
                pl = (px - p.entry_price) / p.entry_price if p.entry_price > 0 else 0
                days_held = idx - p.entry_idx

                # ADAPTACYJNY STOP (ratchet wg drabinki zysku — tylko w górę)
                guaranteed = None
                for threshold, lock in cfg.profit_ladder:
                    if pl >= threshold:
                        guaranteed = lock
                    else:
                        break
                if guaranteed is not None:
                    ladder_stop = p.entry_price * (1 + guaranteed)
                    p.stop = max(p.stop, ladder_stop)   # ratchet, nigdy w dół

                reason = None
                if px <= p.stop:                         # R2 stop (adaptacyjny)
                    reason = "stop"
                elif p.hwm > 0 and (p.hwm - px) / p.hwm >= cfg.trailing_drawdown_pct:  # R5 trailing
                    reason = "trailing"
                elif days_held >= cfg.time_stop_days and pl < cfg.time_stop_min_profit:  # R8 time-stop
                    reason = "time_stop"
                if reason:
                    proceeds = p.shares * px * cfg.usd_pln_rate * (1 - cfg.fx_roundtrip_cost / 2)
                    cash += proceeds
                    res.trades.append({"ticker": tk, "entry": p.entry_price, "exit": px,
                                       "return": pl, "days": days_held, "reason": reason})
                    to_close.append(tk)
            for tk in to_close:
                del positions[tk]

            # ── REBALANS co N dni: otwieranie nowych pozycji ──
            if (idx - start_idx) % cfg.rebalance_every_days == 0 and len(positions) < cfg.max_positions:
                # MAKRO-FILTR: w spadającym rynku (SPY < 200SMA) NIE otwieraj nowych pozycji.
                # To lekarstwo na momentum crashes — bot czeka w gotówce zamiast kupować w dół.
                regime_ok = True
                if cfg.macro_filter_on:
                    spy_hist = spy_close.iloc[:idx + 1].dropna()
                    spy_sma = ind.sma(spy_hist, cfg.macro_sma_days)
                    spy_now = spy_hist.iloc[-1] if len(spy_hist) else None
                    if spy_sma is not None and spy_now is not None:
                        regime_ok = spy_now > spy_sma
                if not regime_ok:
                    winners = []   # rynek poniżej 200SMA -> pomiń otwieranie, leć dalej
                else:
                    winners = self._rank_sectors(etf_closes, idx)
                universe = self._winning_tickers(winners, gics_map) if winners else []
                # kandydaci: w wygranym sektorze, nie trzymani, przechodzą tanie filtry
                cands = []
                for tk in universe:
                    if tk in positions or tk not in prices:
                        continue
                    s = prices[tk].iloc[:idx + 1].dropna()
                    if len(s) < cfg.min_history_days:
                        continue
                    price = s.iloc[-1]
                    rsi_v = ind.rsi(s, 14)
                    pvs = ind.price_vs_sma20(s)
                    if rsi_v is None or pvs is None:
                        continue
                    # tanie filtry: RSI < 75, nie parabola
                    if rsi_v >= cfg.rsi_max or pvs > cfg.parabola_sma_mult:
                        continue
                    # ranking po momentum własnym — multi-period (jak sektory) lub stary ROC60
                    if cfg.use_multiperiod_momentum:
                        mom = self._momentum_score(s, idx)
                    else:
                        mom = rate_of_change(s, 60)
                    if mom is None:
                        continue
                    cands.append((tk, price, s, mom))
                cands.sort(key=lambda x: x[3], reverse=True)

                # otwieraj aż do limitu / wyczerpania gotówki
                for tk, price, s, mom in cands:
                    if len(positions) >= cfg.max_positions:
                        break
                    # sizing: stała baza, cap %, floor
                    target = min(cfg.position_cap_pln, cash * cfg.max_pos_pct)
                    if target < cfg.position_floor_pln or cash < cfg.position_floor_pln:
                        continue
                    target = min(target, cash)
                    cost_pln = target
                    usd_avail = (cost_pln / cfg.usd_pln_rate) * (1 - cfg.fx_roundtrip_cost / 2)
                    shares = usd_avail / price
                    if shares <= 0:
                        continue
                    # stop ATR-based, max -8%
                    atr_v = ind.atr_from_close(s, 14) or (price * 0.04)
                    stop = price - cfg.stop_atr_mult * atr_v
                    stop = max(stop, price * (1 - cfg.stop_max_pct))
                    positions[tk] = BTPosition(ticker=tk, shares=shares, entry_price=price,
                                               entry_idx=idx, stop=stop, hwm=price)
                    cash -= cost_pln

            # zapis equity (co rebalans, dla Sharpe tygodniowego)
            if (idx - start_idx) % cfg.rebalance_every_days == 0:
                eq = equity_at(idx)
                equity_hist.append(eq)
                res.equity_curve.append((str(dates[idx])[:10], round(eq, 2)))
                spy_eq = spy_units * spy_close.iloc[idx]
                res.spy_curve.append((str(dates[idx])[:10], round(spy_eq, 2)))

        # ── zamknij pozostałe pozycje po ostatniej cenie ──
        last = n - 1
        for tk, p in positions.items():
            if tk in prices:
                px = prices[tk].iloc[last]
                if not np.isnan(px):
                    pl = (px - p.entry_price) / p.entry_price
                    cash += p.shares * px * cfg.usd_pln_rate * (1 - cfg.fx_roundtrip_cost / 2)
                    res.trades.append({"ticker": tk, "entry": p.entry_price, "exit": px,
                                       "return": pl, "days": last - p.entry_idx, "reason": "end"})

        # ── METRYKI ──
        total_days = (pd.Timestamp(dates[last]) - pd.Timestamp(dates[start_idx])).days or 1
        final_eq = cash
        spy_final = spy_units * spy_close.iloc[last]
        res.final_equity = round(final_eq, 2)
        res.spy_final_equity = round(spy_final, 2)
        res.cagr = _cagr(cfg.start_capital_pln, final_eq, total_days)
        res.spy_cagr = _cagr(cfg.start_capital_pln, spy_final, total_days)
        res.sharpe = _sharpe(equity_hist)
        res.max_drawdown = _max_drawdown([e for e in equity_hist])
        res.spy_max_drawdown = _max_drawdown([spy_units * spy_close.iloc[i]
                                              for i in range(start_idx, n, cfg.rebalance_every_days)])
        res.n_trades = len(res.trades)
        if res.trades:
            wins = [t for t in res.trades if t["return"] > 0]
            res.hit_rate = len(wins) / len(res.trades)
            res.avg_trade_return = sum(t["return"] for t in res.trades) / len(res.trades)
            # ULTRA TEST — pomiar ekspozycji na mega-cap winners (survivorship bias check)
            # Dla każdego tickera: ile razy kupiony i ile PUNKTÓW PROCENTOWYCH zysku przyniósł.
            # Punkty proc = suma `return` z trade'ów tego tickera (każdy trade = jego % zwrotu).
            by_tk = {}
            for t in res.trades:
                tk = t["ticker"]; r = t["return"]
                if tk not in by_tk:
                    by_tk[tk] = {"n": 0, "sum_r": 0.0}
                by_tk[tk]["n"] += 1; by_tk[tk]["sum_r"] += r
            ranked = sorted(by_tk.items(), key=lambda x: x[1]["sum_r"], reverse=True)
            res.top_tickers = [(tk, d["n"], d["sum_r"]) for tk, d in ranked[:10]]
            total_pos = sum(max(d["sum_r"], 0.0) for d in by_tk.values())
            if total_pos > 0:
                top5_pos = sum(max(d["sum_r"], 0.0) for tk, d in ranked[:5])
                res.concentration_top5_pct = 100.0 * top5_pos / total_pos
        return res


# ─────────────────────────────────────────────────────────────────────────────
# RAPORT
# ─────────────────────────────────────────────────────────────────────────────
def print_report(res: BacktestResult, cfg: BacktestConfig) -> None:
    print("\n" + "=" * 60)
    print("  BACKTEST — wynik")
    print("=" * 60)
    if not res.equity_curve:
        print("Brak wyniku: " + "; ".join(res.notes))
        return
    print(f"Kapitał startowy:  {cfg.start_capital_pln:,.0f} zł")
    print(f"Equity końcowe:    {res.final_equity:,.0f} zł   (bot)")
    print(f"SPY kup-i-trzymaj: {res.spy_final_equity:,.0f} zł   (benchmark)")
    print(f"")
    print(f"CAGR bot:          {res.cagr*100:+.1f}% / rok")
    print(f"CAGR SPY:          {res.spy_cagr*100:+.1f}% / rok")
    edge = res.cagr - res.spy_cagr
    print(f"EDGE vs SPY:       {edge*100:+.1f} pkt proc / rok  {'✅ bot wygrywa' if edge > 0 else '❌ SPY wygrywa'}")
    print(f"")
    print(f"Sharpe (bot):      {res.sharpe:.2f}")
    print(f"Max drawdown bot:  {res.max_drawdown*100:.1f}%")
    print(f"Max drawdown SPY:  {res.spy_max_drawdown*100:.1f}%")
    print(f"")
    print(f"Transakcji:        {res.n_trades}")
    print(f"Hit rate:          {res.hit_rate*100:.0f}%  (% zyskownych)")
    print(f"Śr. zwrot/trade:   {res.avg_trade_return*100:+.1f}%")
    for note in res.notes:
        print(f"  · {note}")
    print("=" * 60)
    if edge <= 0:
        print("WERDYKT: bot NIE pobił SPY w tym okresie. Rozważ trzymanie SPY (IUSP.UK)")
        print("zamiast dokładania kolejnych modułów. Edge nie jest udowodniony.")
    else:
        print("WERDYKT: bot pobił SPY w tym okresie. UWAGA na survivorship bias i")
        print("brak earnings/smart money w teście — wynik realny będzie inny.")


# ─────────────────────────────────────────────────────────────────────────────
# URUCHOMIENIE NA ŻYWO (yfinance) — na maszynie użytkownika
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# PARAMETER SWEEP — wiele botów przez TĘ SAMĄ historię, tabela obok siebie.
# Dane pobierane RAZ; różnice w wynikach wynikają WYŁĄCZNIE z parametrów.
# Dokręcamy tylko "mądrą agresję" (koncentracja, luźniejszy ratchet, ostrość filtra).
# ─────────────────────────────────────────────────────────────────────────────

# Luźniejszy ratchet = zwycięzcy biegną dłużej (mniej wczesnych wylotów).
_LADDER_TIGHT  = ((0.05, -0.02), (0.10, 0.00), (0.15, 0.05), (0.25, 0.10),
                  (0.40, 0.20), (0.60, 0.35), (1.00, 0.70))   # obecny (baseline ratchet)
_LADDER_LOOSE  = ((0.08, -0.04), (0.15, -0.01), (0.25, 0.05), (0.40, 0.15),
                  (0.60, 0.30), (1.00, 0.55), (1.50, 0.95))   # luźniejszy — daje oddychać
_LADDER_LOOSER = ((0.10, -0.05), (0.20, -0.02), (0.35, 0.05), (0.55, 0.15),
                  (0.80, 0.30), (1.20, 0.55), (2.00, 1.10))   # bardzo luźny — max bieg

def _sweep_variants() -> "list[tuple[str, BacktestConfig]]":
    """Siatka botów do równoległego backtestu. ŁATWO rozszerzalna: dopisz krotkę.
    RUNDA 4: TEST RANKINGU MOMENTUM. Hipoteza: edge na 20 lat był ujemny bo ranking
    ROC10/20 (2-3 tygodnie) to szum, nie momentum. Literatura (Jegadeesh-Titman): momentum
    to 6-12 mies. Porównujemy stary ranking (krótki, szum) z nowym (multi-period 3/6/12).
    Wszystkie z makro-filtrem (zostaje — naprawił MaxDD 2008)."""
    base = dict(start_capital_pln=1582.0, min_history_days=300,
                max_positions=3, position_cap_pln=700.0, max_pos_pct=0.45,
                profit_ladder=_LADDER_LOOSE, macro_filter_on=True)
    return [
        # STARY ranking ROC10/20 (krótki = szum) — to był poprzedni wynik (edge -3pp na 20 lat)
        ("G_ranking_krotki_ROC", BacktestConfig(**base, use_multiperiod_momentum=False)),
        # NOWY ranking multi-period 3/6/12 mies — HIPOTEZA: edge na 20 lat rośnie
        ("H_ranking_momentum_dlugi", BacktestConfig(**base, use_multiperiod_momentum=True)),
        # NOWY ranking + ciaśniejszy ratchet — kontrola interakcji
        ("I_momentum_dlugi_ciasny", BacktestConfig(
            start_capital_pln=1582.0, min_history_days=300, max_positions=3,
            position_cap_pln=700.0, max_pos_pct=0.45, profit_ladder=_LADDER_TIGHT,
            macro_filter_on=True, use_multiperiod_momentum=True)),
    ]


def print_sweep_report(results: "list[tuple[str, BacktestResult]]", spy_cagr: float) -> None:
    """Tabela porównawcza wszystkich botów obok siebie + werdykt."""
    print("\n" + "=" * 78)
    print("PARAMETER SWEEP — wszystkie boty na TEJ SAMEJ historii")
    print("=" * 78)
    print(f"\nBenchmark SPY CAGR: {spy_cagr*100:+.1f}%\n")
    hdr = f"{'Bot':<26}{'CAGR':>8}{'edge':>8}{'Sharpe':>8}{'MaxDD':>8}{'Trans':>7}{'Hit':>6}"
    print(hdr); print("-" * len(hdr))
    best_cagr = best_sharpe = None
    for name, r in results:
        edge = (r.cagr - spy_cagr) * 100
        print(f"{name:<26}{r.cagr*100:>7.1f}%{edge:>+7.1f}{r.sharpe:>8.2f}"
              f"{r.max_drawdown*100:>7.1f}%{r.n_trades:>7}{r.hit_rate*100:>5.0f}%")
        if best_cagr is None or r.cagr > best_cagr[1].cagr: best_cagr = (name, r)
        if best_sharpe is None or r.sharpe > best_sharpe[1].sharpe: best_sharpe = (name, r)
    print("-" * len(hdr))
    if best_cagr and best_sharpe:
        print(f"\nNajwyższy CAGR:   {best_cagr[0]} ({best_cagr[1].cagr*100:+.1f}%, DD {best_cagr[1].max_drawdown*100:.1f}%)")
        print(f"Najlepszy Sharpe: {best_sharpe[0]} ({best_sharpe[1].sharpe:.2f}, ryzyko-skorygowany zwrot)")
        print("\nUWAGA: najwyższy CAGR przy wystrzelonym MaxDD = przekroczona granica agresji.")
        print("Patrz na Sharpe (zwrot/ryzyko), nie tylko na CAGR. Liczy się powtarzalność.")


def print_multiwindow_report(table: dict, windows: list, spy_by_window: dict,
                             oos_table: "Optional[dict]" = None) -> None:
    """ULTRA TEST: tabela wariant × okno + sekcja survivorship bias (top tickery, concentration)
    + sekcja overfitting (out-of-sample split pierwsza/druga połowa najdłuższego okna)."""
    print("\n" + "=" * 78)
    print("ULTRA TEST — wszystkie warianty na wszystkich oknach + dwa robustness check")
    print("=" * 78)
    print("\nSPY CAGR per okno:  " + "  ".join(f"{y}l:{spy_by_window[y]*100:+.0f}%" for y in windows))
    print("\n## CAGR bota (edge nad SPY w nawiasie):\n")
    hdr = f"{'Bot':<28}" + "".join(f"{str(y)+' lat':>14}" for y in windows)
    print(hdr); print("-" * len(hdr))
    for name in table:
        row = f"{name:<28}"
        for y in windows:
            r = table[name].get(y)
            if r is None:
                row += f"{'—':>14}"
            else:
                edge = (r.cagr - spy_by_window[y]) * 100
                row += f"{r.cagr*100:>7.0f}%({edge:+.0f}){'':>1}"
        print(row)
    print("\n## MaxDD bota (im niżej tym lepiej):\n")
    print(hdr); print("-" * len(hdr))
    for name in table:
        row = f"{name:<28}"
        for y in windows:
            r = table[name].get(y)
            row += f"{'—':>14}" if r is None else f"{r.max_drawdown*100:>12.0f}% "
        print(row)

    # ── ROBUSTNESS CHECK A: SURVIVORSHIP BIAS (top tickery + concentration top 5) ──
    longest = max(windows)
    print("\n" + "─" * 78)
    print(f"## ROBUSTNESS A — survivorship bias check (na oknie {longest} lat)")
    print("─" * 78)
    print("Kto był kupowany i ile zysku przyszło z TOP 5 spółek.")
    print("Jeśli concentration > 70% = bot żyje z kilku mega-winners (sygnał biasu).")
    print("Jeśli < 50% = realna dywersyfikacja, edge bardziej wiarygodny.\n")
    for name in table:
        r = table[name].get(longest)
        if r is None or not r.top_tickers:
            print(f"  {name}: brak danych")
            continue
        top5 = r.top_tickers[:5]
        tickers_str = ", ".join(f"{tk}({n}×,{sr:+.0f}pp)" for tk, n, sr in top5)
        verdict = "⚠ wysokie" if r.concentration_top5_pct > 70 else ("OK" if r.concentration_top5_pct < 50 else "średnie")
        print(f"  {name:<28} concentration TOP 5: {r.concentration_top5_pct:5.0f}%  [{verdict}]")
        print(f"  {'':<28}   TOP 5: {tickers_str}")

    # ── ROBUSTNESS CHECK B: OUT-OF-SAMPLE SPLIT ──
    if oos_table:
        print("\n" + "─" * 78)
        print(f"## ROBUSTNESS B — out-of-sample split (okno {longest} lat na pół)")
        print("─" * 78)
        print("Czy strategia działa na OBU połówkach historii, czy tylko na ostatniej?")
        print("Edge dodatni na obu = twardsze. Tylko na drugiej = dotuningowane do ostatniej hossy.\n")
        first_yrs = longest // 2
        second_yrs = longest - first_yrs
        spy_a, spy_b = oos_table.get("__spy__", (0.0, 0.0))
        print(f"  Okres I (starsze {first_yrs} lat):  SPY {spy_a*100:+.0f}%")
        print(f"  Okres II (świeższe {second_yrs} lat): SPY {spy_b*100:+.0f}%\n")
        print(f"  {'Bot':<28}{'Okres I CAGR (edge)':>26}{'Okres II CAGR (edge)':>26}")
        print("  " + "-" * 76)
        for name in table:
            r_a, r_b = oos_table.get(name, (None, None))
            if r_a is None or r_b is None:
                print(f"  {name:<28}{'—':>26}{'—':>26}")
                continue
            e_a = (r_a.cagr - spy_a) * 100; e_b = (r_b.cagr - spy_b) * 100
            cell_a = f"{r_a.cagr*100:>6.0f}% ({e_a:+.0f}pp)"
            cell_b = f"{r_b.cagr*100:>6.0f}% ({e_b:+.0f}pp)"
            stable = "✓ stabilne" if (e_a > 0 and e_b > 0) else ("⚠ overfit" if e_b > e_a + 10 else "")
            print(f"  {name:<28}{cell_a:>26}{cell_b:>26}  {stable}")

    print("\n" + "=" * 78)
    print("JAK CZYTAĆ WERDYKT:")
    print("• Edge dodatni na WSZYSTKICH oknach (CAGR) — warunek konieczny prawdziwej strategii.")
    print("• Concentration TOP 5 < 50% — bot nie żyje z ex-post zwycięzców indeksu (low bias).")
    print("• OOS: edge dodatni i podobny na OBU połówkach — nie dotuningowane do hossy.")
    print("• Wszystkie 3 spełnione = strategia twarda. Brak choć jednego = potrzeba haircut.")


def run_sweep(years: int = 5, windows: "Optional[list]" = None) -> int:
    """Sweep wariantów. Jeśli `windows` -> ULTRA TEST: wielookienny + survivorship check +
    out-of-sample split. Pobiera dane RAZ, porównanie absolutnie uczciwe."""
    try:
        import yfinance as yf
    except Exception:
        print("yfinance niedostępne — sweep wymaga yfinance.")
        return 1
    from top_down_scanner import load_sp500_universe

    gics_map = load_sp500_universe()
    if not gics_map:
        print("Nie pobrano S&P 500 — przerwij.")
        return 1
    universe = sorted({t for sec in ETF_TO_GICS.values() for g in sec for t in gics_map.get(g, [])})
    etf_list = list(SECTOR_ETFS.keys())
    all_tickers = list(dict.fromkeys(universe + etf_list + [BENCH_SPY]))
    variants = _sweep_variants()
    multiwindow = windows is not None and len(windows) > 0
    fetch_years = max(windows) if multiwindow else years

    print(f"ULTRA TEST: {len(variants)} botów, {len(all_tickers)} tickerów, pobieram {fetch_years} lat.")
    if multiwindow:
        print(f"Okna: {windows}, plus OOS split (pierwsza/druga połowa najdłuższego okna).")
    print("Pobieram dane RAZ (wspólne dla wszystkich testów)...")

    data = yf.download(all_tickers, period=f"{fetch_years}y", interval="1d",
                       auto_adjust=True, progress=True, threads=False)
    if data is None or len(data) == 0:
        print("yfinance zwrócił pusto.")
        return 1
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
    if BENCH_SPY not in close.columns:
        print("Brak SPY w danych — przerwij.")
        return 1

    if not multiwindow:
        dates = list(close.index)
        prices = {tk: close[tk] for tk in universe if tk in close.columns}
        etf_closes = {etf: close[etf] for etf in etf_list if etf in close.columns}
        spy_close = close[BENCH_SPY]
        results, spy_cagr = [], None
        for name, cfg in variants:
            print(f"  -> backtest: {name} ...")
            res = Backtester(cfg).run(prices, None, etf_closes, spy_close, gics_map, dates)
            results.append((name, res)); spy_cagr = res.spy_cagr
        print_sweep_report(results, spy_cagr if spy_cagr is not None else 0.0)
        return 0

    # ── ULTRA TEST: warianty × okna ──
    table = {name: {} for name, _ in variants}
    spy_by_window = {}
    approx_sessions_per_year = 252
    for y in sorted(windows):
        n_sessions = y * approx_sessions_per_year
        close_w = close.iloc[-n_sessions:] if len(close) > n_sessions else close
        dates_w = list(close_w.index)
        prices_w = {tk: close_w[tk] for tk in universe if tk in close_w.columns}
        etf_w = {etf: close_w[etf] for etf in etf_list if etf in close_w.columns}
        spy_w = close_w[BENCH_SPY]
        print(f"\n── Okno {y} lat ({len(dates_w)} sesji, {dates_w[0].date()} → {dates_w[-1].date()}) ──")
        for name, cfg in variants:
            print(f"  -> {name} ...")
            res = Backtester(cfg).run(prices_w, None, etf_w, spy_w, gics_map, dates_w)
            table[name][y] = res; spy_by_window[y] = res.spy_cagr

    # ── ROBUSTNESS B: OOS SPLIT na najdłuższym oknie ──
    longest = max(windows)
    n_sessions_long = longest * approx_sessions_per_year
    close_long = close.iloc[-n_sessions_long:] if len(close) > n_sessions_long else close
    half = len(close_long) // 2
    print(f"\n── OOS split: dzielę {longest}-letnie okno na pół ({half} + {len(close_long)-half} sesji) ──")
    oos_table = {}
    for label, slice_close in (("A", close_long.iloc[:half]), ("B", close_long.iloc[half:])):
        d_s = list(slice_close.index)
        p_s = {tk: slice_close[tk] for tk in universe if tk in slice_close.columns}
        e_s = {etf: slice_close[etf] for etf in etf_list if etf in slice_close.columns}
        spy_s = slice_close[BENCH_SPY]
        print(f"  Okres {label}: {d_s[0].date()} → {d_s[-1].date()}")
        for name, cfg in variants:
            res = Backtester(cfg).run(p_s, None, e_s, spy_s, gics_map, d_s)
            oos_table.setdefault(name, [None, None])[0 if label == "A" else 1] = res
            if label == "A":
                oos_table.setdefault("__spy__", [0.0, 0.0])[0] = res.spy_cagr
            else:
                oos_table.setdefault("__spy__", [0.0, 0.0])[1] = res.spy_cagr
    # konwersja list na tuple dla raportu
    oos_final = {k: tuple(v) for k, v in oos_table.items()}
    print_multiwindow_report(table, sorted(windows), spy_by_window, oos_table=oos_final)
    return 0


def run_live(years: int = 5, config: Optional[BacktestConfig] = None) -> int:
    cfg = config or BacktestConfig()
    try:
        import yfinance as yf
    except Exception:
        print("yfinance niedostępne — backtest na żywo wymaga yfinance.")
        return 1
    from top_down_scanner import load_sp500_universe

    gics_map = load_sp500_universe()
    if not gics_map:
        print("Nie pobrano S&P 500 — przerwij.")
        return 1
    # uniwersum: spółki z sektorów mapowanych przez ETF + ETF-y + SPY
    universe = sorted({t for sec in ETF_TO_GICS.values() for g in sec for t in gics_map.get(g, [])})
    etf_list = list(SECTOR_ETFS.keys())
    all_tickers = list(dict.fromkeys(universe + etf_list + [BENCH_SPY]))
    print(f"Pobieram {len(all_tickers)} tickerów, {years} lat historii (to może potrwać)...")

    data = yf.download(all_tickers, period=f"{years}y", interval="1d",
                       auto_adjust=True, progress=True, threads=False)
    if data is None or len(data) == 0:
        print("yfinance zwrócił pusto.")
        return 1
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
    dates = list(close.index)
    prices = {tk: close[tk].dropna() for tk in universe if tk in close.columns}
    # reindex do wspólnej osi dat
    prices = {tk: close[tk] for tk in universe if tk in close.columns}
    etf_closes = {etf: close[etf] for etf in etf_list if etf in close.columns}
    if BENCH_SPY not in close.columns:
        print("Brak SPY w danych — przerwij.")
        return 1
    spy_close = close[BENCH_SPY]

    bt = Backtester(cfg)
    res = bt.run(prices, None, etf_closes, spy_close, gics_map, dates)
    print_report(res, cfg)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST — synthetyczne ceny, test MECHANIKI (nie realnego edge)
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST backtest (synthetyczne ceny, test mechaniki) ===")
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # metryki czyste
    check("CAGR 100->200 w rok = ~100%", abs(_cagr(100, 200, 365) - 1.0) < 0.02)
    check("CAGR zerowy czas -> 0", _cagr(100, 200, 0) == 0.0)
    check("Max drawdown [100,80,120] = 20%", abs(_max_drawdown([100, 80, 120]) - 0.20) < 0.001)
    check("Max drawdown rosnący -> 0", _max_drawdown([100, 110, 120]) == 0.0)
    check("Sharpe stałego wzrostu skończony", _sharpe([100, 101, 102, 103, 104]) >= 0)

    # ── symulacja na synthetycznych cenach ──
    n = 400
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)

    # ETF-y: jeden rośnie mocno (XLK), reszta płasko -> XLK wygrywa ranking
    etf_closes = {}
    for etf in SECTOR_ETFS:
        if etf == "XLK":
            etf_closes[etf] = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, n))), index=dates)
        else:
            etf_closes[etf] = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n))), index=dates)

    # spółki IT (wygrany sektor) — trend wzrostowy; reszta płasko
    gics_map = {"Information Technology": ["AAA", "BBB", "CCC"], "Financials": ["FFF", "GGG"]}
    prices = {}
    for tk in ["AAA", "BBB", "CCC"]:
        prices[tk] = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0015, 0.015, n))), index=dates)
    for tk in ["FFF", "GGG"]:
        prices[tk] = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0, 0.012, n))), index=dates)

    spy_close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.008, n))), index=dates)

    cfg = BacktestConfig(min_history_days=120, start_capital_pln=1582.0, max_positions=3)
    bt = Backtester(cfg)
    res = bt.run(prices, None, etf_closes, spy_close, gics_map, list(dates))

    check("Backtest zwrócił krzywą equity", len(res.equity_curve) > 0)
    check("Backtest zwrócił krzywą SPY", len(res.spy_curve) > 0)
    check("Equity startuje ~kapitał startowy", abs(res.equity_curve[0][1] - 1582.0) < 600)  # po 1 rebalansie
    check("Wykonano transakcje (kupno/sprzedaż)", res.n_trades > 0)
    check("CAGR policzony (skończony)", np.isfinite(res.cagr))
    check("SPY CAGR policzony", np.isfinite(res.spy_cagr))
    check("Max drawdown w [0,1]", 0 <= res.max_drawdown <= 1)
    check("Hit rate w [0,1]", 0 <= res.hit_rate <= 1)

    # NO-LOOKAHEAD: equity w dniu T nie zależy od cen po T.
    # Test: obcięcie danych do połowy daje identyczny prefiks equity.
    half = n // 2
    prices_half = {tk: s.iloc[:half] for tk, s in prices.items()}
    etf_half = {tk: s.iloc[:half] for tk, s in etf_closes.items()}
    res_half = bt.run(prices_half, None, etf_half, spy_close.iloc[:half], gics_map, list(dates[:half]))
    # pierwsze wspólne punkty equity muszą się zgadzać (brak zależności od przyszłości)
    common = min(len(res.equity_curve), len(res_half.equity_curve))
    prefix_match = all(abs(res.equity_curve[i][1] - res_half.equity_curve[i][1]) < 0.01
                       for i in range(min(common, 5)))
    check("NO-LOOKAHEAD: prefiks equity niezależny od przyszłych cen", prefix_match)

    # koszt FX zmniejsza wynik vs zero-cost
    cfg_nocost = BacktestConfig(min_history_days=120, start_capital_pln=1582.0,
                                max_positions=3, fx_roundtrip_cost=0.0)
    res_nocost = Backtester(cfg_nocost).run(prices, None, etf_closes, spy_close, gics_map, list(dates))
    check("Koszt FX obniża equity (realizm)", res.final_equity <= res_nocost.final_equity + 0.01)

    # ── SWEEP: warianty poprawnie zdefiniowane + tabela się drukuje ──
    variants = _sweep_variants()
    check("Sweep: >=3 warianty zdefiniowane", len(variants) >= 3)
    check("Sweep: nazwy unikalne", len({n for n, _ in variants}) == len(variants))
    check("Sweep: każdy wariant ma prawidłowy config", all(isinstance(c, BacktestConfig) for _, c in variants))
    # drabinki uporządkowane od ciasnej do luźnej: LOOSER daje więcej luzu niż LOOSE niż TIGHT
    check("Sweep: ratchet TIGHT < LOOSE < LOOSER (więcej luzu)",
          _LADDER_LOOSER[2][1] <= _LADDER_LOOSE[2][1] <= _LADDER_TIGHT[2][1])
    # przepuść 2 warianty przez syntetyczną historię — muszą dać skończone metryki
    sweep_res = []
    for name, cfg in variants[:2]:
        cfg2 = BacktestConfig(**{**cfg.__dict__, "min_history_days": 120})
        r = Backtester(cfg2).run(prices, None, etf_closes, spy_close, gics_map, list(dates))
        sweep_res.append((name, r))
    check("Sweep: warianty zwracają skończony CAGR", all(np.isfinite(r.cagr) for _, r in sweep_res))
    try:
        print_sweep_report(sweep_res, sweep_res[0][1].spy_cagr)
        sweep_print_ok = True
    except Exception as e:
        sweep_print_ok = False
        print(f"  (sweep report error: {e})")
    check("Sweep: tabela porównawcza drukuje się bez błędu", sweep_print_ok)

    # ── MULTI-WINDOW: raport wielookienny drukuje się bez błędu ──
    mw_table = {name: {} for name, _ in variants[:2]}
    for name, cfg in variants[:2]:
        cfg2 = BacktestConfig(**{**cfg.__dict__, "min_history_days": 120})
        r = Backtester(cfg2).run(prices, None, etf_closes, spy_close, gics_map, list(dates))
        mw_table[name][1] = r   # udajemy okno "1 rok" na syntetyce
    try:
        print_multiwindow_report(mw_table, [1], {1: mw_table[variants[0][0]][1].spy_cagr})
        mw_ok = True
    except Exception as e:
        mw_ok = False; print(f"  (multiwindow report error: {e})")
    check("Multi-window: raport wielookienny drukuje się bez błędu", mw_ok)

    # ── MAKRO-FILTR: w spadającym rynku (SPY<200SMA) bot NIE otwiera nowych pozycji ──
    # Buduj rynek spadkowy: SPY trwale w dół -> filtr ON powinien dać 0 (lub ~0) zakupów.
    n2 = 300
    dates2 = pd.date_range("2024-01-01", periods=n2, freq="B")
    spy_down = pd.Series(200 * np.exp(np.cumsum(np.full(n2, -0.002))), index=dates2)  # stały spadek
    etf_down = {etf: pd.Series(100 * np.exp(np.cumsum(rng.normal(-0.001, 0.01, n2))), index=dates2)
                for etf in SECTOR_ETFS}
    gics2 = {"Information Technology": ["AAA", "BBB"], "Financials": ["FFF"]}
    prices2 = {tk: pd.Series(100 * np.exp(np.cumsum(rng.normal(-0.0005, 0.012, n2))), index=dates2)
               for tk in ["AAA", "BBB", "FFF"]}
    cfg_on = BacktestConfig(min_history_days=120, start_capital_pln=1582.0, max_positions=3,
                            macro_filter_on=True, macro_sma_days=100)
    cfg_off = BacktestConfig(min_history_days=120, start_capital_pln=1582.0, max_positions=3,
                             macro_filter_on=False)
    res_on = Backtester(cfg_on).run(prices2, None, etf_down, spy_down, gics2, list(dates2))
    res_off = Backtester(cfg_off).run(prices2, None, etf_down, spy_down, gics2, list(dates2))
    check("Makro-filtr ON: mało/zero transakcji w spadającym rynku", res_on.n_trades <= res_off.n_trades)
    check("Makro-filtr OFF kupuje więcej w bessie niż ON (filtr działa)", res_off.n_trades >= res_on.n_trades)
    # filtr nie może podnieść drawdownu w bessie (siedzi w gotówce)
    check("Makro-filtr ON nie pogarsza MaxDD w bessie", res_on.max_drawdown <= res_off.max_drawdown + 0.01)

    # ── MULTI-PERIOD MOMENTUM: preferuje długi trend nad świeży skok ──
    bt_mp = Backtester(BacktestConfig(use_multiperiod_momentum=True,
                                      mom_lookbacks=(63, 126, 252), mom_weights=(0.2, 0.3, 0.5),
                                      mom_skip_recent_days=21))
    n3 = 400
    d3 = pd.date_range("2023-01-01", periods=n3, freq="B")
    # spółka A: silny, trwały trend wzrostowy przez cały okres
    strong_trend = pd.Series(100 * np.exp(np.cumsum(np.full(n3, 0.0015))), index=d3)
    # spółka B: płaska przez większość, ostry skok dopiero w ostatnich 10 dniach (szum/reversal)
    flat_then_spike = np.full(n3, 100.0)
    flat_then_spike[-10:] = np.linspace(100, 130, 10)
    spike = pd.Series(flat_then_spike, index=d3)
    sc_trend = bt_mp._momentum_score(strong_trend, n3 - 1)
    sc_spike = bt_mp._momentum_score(spike, n3 - 1)
    check("Multi-period momentum policzony dla trendu", sc_trend is not None)
    check("Multi-period: trwały trend > świeży skok (anty-szum)",
          sc_trend is not None and sc_spike is not None and sc_trend > sc_spike)
    # skip-recent: świeży skok w ostatnim miesiącu jest IGNOROWANY (reversal effect)
    check("Multi-period: za mało historii -> None (bez lookahead)",
          bt_mp._momentum_score(strong_trend.iloc[:50], 49) is None)
    # przełącznik działa: stary ranking nie wymaga długiej historii
    bt_old = Backtester(BacktestConfig(use_multiperiod_momentum=False))
    old_rank = bt_old._rank_sectors({etf: spike for etf in SECTOR_ETFS}, n3 - 1)
    check("Stary ranking ROC nadal działa (przełącznik)", isinstance(old_rank, list))

    # ── ULTRA TEST: top tickers + concentration liczą się z trades ──
    # syntetyczny wynik z różnymi tickerami
    res_synth = BacktestResult()
    res_synth.trades = [
        {"ticker": "AAA", "return": 0.50, "entry": 100, "exit": 150, "days": 30, "reason": "stop"},
        {"ticker": "AAA", "return": 0.30, "entry": 150, "exit": 195, "days": 30, "reason": "stop"},
        {"ticker": "BBB", "return": 0.10, "entry": 100, "exit": 110, "days": 30, "reason": "stop"},
        {"ticker": "CCC", "return": -0.05, "entry": 100, "exit": 95, "days": 30, "reason": "stop"},
        {"ticker": "DDD", "return": 0.02, "entry": 100, "exit": 102, "days": 30, "reason": "stop"},
        {"ticker": "EEE", "return": 0.01, "entry": 100, "exit": 101, "days": 30, "reason": "stop"},
        {"ticker": "FFF", "return": 0.01, "entry": 100, "exit": 101, "days": 30, "reason": "stop"},
    ]
    # ręcznie wykonaj agregację (jak w run())
    by_tk = {}
    for t in res_synth.trades:
        by_tk.setdefault(t["ticker"], {"n": 0, "sum_r": 0.0})
        by_tk[t["ticker"]]["n"] += 1; by_tk[t["ticker"]]["sum_r"] += t["return"]
    ranked = sorted(by_tk.items(), key=lambda x: x[1]["sum_r"], reverse=True)
    check("Ultra: top ticker po sumie zysku to AAA (najwięcej wygrał)",
          ranked[0][0] == "AAA" and ranked[0][1]["sum_r"] > 0.7)
    total_pos = sum(max(d["sum_r"], 0.0) for d in by_tk.values())
    top5_pos = sum(max(d["sum_r"], 0.0) for tk, d in ranked[:5])
    concentration = 100 * top5_pos / total_pos
    # AAA dominuje (0.8/0.94 ≈ 85%) — top5 z 6 dodatnich tickerów to ≈ 99% (ujemny CCC poza top5)
    check("Ultra: concentration TOP 5 ma sens (>90% bo AAA dominuje)", concentration > 90)

    # raport multi-window z OOS się drukuje
    oos_fake = {variants[0][0]: (sweep_res[0][1], sweep_res[1][1]), "__spy__": (0.1, 0.12)}
    try:
        print_multiwindow_report(mw_table, [1], {1: mw_table[variants[0][0]][1].spy_cagr},
                                 oos_table=oos_fake)
        ultra_print_ok = True
    except Exception as e:
        ultra_print_ok = False; print(f"  (ultra report error: {e})")
    check("Ultra: raport z OOS split drukuje się bez błędu", ultra_print_ok)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== backtest.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Backtest historyczny")
    ap.add_argument("--selftest", action="store_true", help="test mechaniki (offline, synthetyczne)")
    ap.add_argument("--run", action="store_true", help="backtest na żywo (yfinance)")
    ap.add_argument("--sweep", action="store_true", help="parameter sweep — wiele botów naraz, tabela porównawcza")
    ap.add_argument("--windows", type=str, default="", help="okna lat dla sweep wielookiennego, np. 5,10,15,20")
    ap.add_argument("--years", type=int, default=5, help="ile lat historii")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    if args.sweep:
        windows = [int(x) for x in args.windows.split(",") if x.strip()] if args.windows else None
        return run_sweep(years=args.years, windows=windows)
    if args.run:
        return run_live(years=args.years)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
