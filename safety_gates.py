"""
safety_gates.py — BEZPIECZNIKI PRZED-TRANSAKCYJNE
==================================================
Bot Porsche — komplet twardych bramek, które MUSZĄ przejść zanim propozycja kupna
trafi do maila. Każda bramka zwraca PASS/FAIL z powodem. Jeden FAIL = odrzucenie zagrania.

KLUCZOWE (wzmocnione przez audyt Gemini):
  Earnings blocker NIE jest tylko "nie kupuj przed wynikami". Zlecenia Sell Stop na XTB
  wypełniają się po cenie RYNKOWEJ i przy luce spadkowej (gap) IGNORUJĄ wpisaną cenę.
  Wynik kwartalny = ryzyko luki, która przeskoczy nasz Sell Stop -> realna strata > zakładana.
  Dlatego twardo odrzucamy spółki z earnings w oknie T+0..T+2.

13 BRAMEK:
  1. earnings_gap_blocker   — brak wyników w T+0..T+2 (ochrona przed luką pomijającą Sell Stop)
  2. liquidity             — dolarowy obrót wystarczający (brak morderczego spreadu)
  3. rsi_not_overbought    — RSI(14) < 75 (nie kupujemy szczytu)
  4. not_parabolic         — cena < SMA20 * 1.15 (nie gonimy paraboli)
  5. cash_sufficiency      — stać na pozycję + bufor kosztowy
  6. min_order_usd         — wartość ≥ 10 USD (minimum XTB)
  7. fractional_enabled    — ticker dostępny jako akcja ułamkowa na XTB
  8. macro_allows          — radar makro nie jest 3/3 (zakaz wejść)
  9. no_hard_block         — smart money nie zwrócił HARD_BLOCK (klaster sprzedaży)
 10. spread_sanity         — spread (ask-bid)/mid w normie
 11. no_pending_unresolved — brak nierozliczonej poprzedniej propozycji
 12. session_timing        — wysyłka odpowiednio przed otwarciem sesji USA
 13. concentration_ok      — pozycja + obecna ekspozycja na ticker ≤ cap koncentracji

Czysto obliczeniowy (dane wejściowe podaje pipeline). Brak sieci.
Uruchomienie testów: python safety_gates.py --selftest
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("porsche.gates")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class GatesConfig:
    earnings_block_days: int = 2          # blokuj jeśli wyniki w T+0..T+2
    min_dollar_volume_midcap: float = 20_000_000.0
    min_dollar_volume_smallcap: float = 5_000_000.0
    rsi_max: float = 75.0
    parabolic_sma_mult: float = 1.15          # cena > SMA20*1.15 = parabola
    parabolic_sma_mult_override: float = 1.30 # FURTKA: ze strong_catalyst luźniej do 1.30× (mocne przesłanki, że jeszcze nie max)
    min_order_usd: float = 10.0
    cost_buffer: float = 0.01             # 1% (spread+FX)
    spread_max_bluechip: float = 0.005    # 0.5%
    spread_max_smallcap: float = 0.015    # 1.5%
    session_lead_minutes: int = 30        # wysyłka min. 30 min przed otwarciem
    concentration_cap: float = 0.60


@dataclass
class Candidate:
    """Dane kandydata do oceny przez bezpieczniki."""
    ticker: str
    price_usd: float
    position_value_pln: float
    cash_available_pln: float
    equity_total_pln: float
    usd_pln_rate: float = 4.0
    # sygnały/wskaźniki (None = nieznane -> bezpiecznik traktuje konserwatywnie)
    days_to_earnings: Optional[int] = None      # ile dni do najbliższych wyników (None=nieznane)
    avg_dollar_volume: Optional[float] = None
    is_smallcap: bool = False
    rsi14: Optional[float] = None
    price_vs_sma20: Optional[float] = None       # cena/SMA20 (np. 1.10 = 10% nad średnią)
    fractional_enabled: Optional[bool] = None
    radar_level: int = 0
    smart_money_hard_block: bool = False
    spread_pct: Optional[float] = None
    has_unresolved_pending: bool = False
    minutes_to_session_open: Optional[int] = None
    current_exposure_pln: float = 0.0            # ile już mamy w tym tickerze
    strong_catalyst: bool = False                # FURTKA: świeży transformacyjny katalizator (kontrakt≫kap, smart-money) -> luźniejsza parabola


@dataclass
class GateResult:
    name: str
    passed: bool
    reason: str


@dataclass
class GatesReport:
    ticker: str
    all_passed: bool
    results: list = field(default_factory=list)   # list[GateResult]

    @property
    def failures(self) -> list:
        return [r for r in self.results if not r.passed]


class SafetyGates:
    def __init__(self, config: Optional[GatesConfig] = None):
        self.cfg = config or GatesConfig()

    # ── pojedyncze bramki ─────────────────────────────────────────────────────
    def g_earnings(self, c: Candidate) -> GateResult:
        # KONSERWATYWNIE: nieznana data wyników = traktuj jak ryzyko (FAIL).
        if c.days_to_earnings is None:
            return GateResult("earnings_gap_blocker", False,
                              "nieznana data wyników — konserwatywnie odrzucam (ryzyko luki na Sell Stop)")
        if 0 <= c.days_to_earnings <= self.cfg.earnings_block_days:
            return GateResult("earnings_gap_blocker", False,
                              f"wyniki za {c.days_to_earnings} dni — luka może przeskoczyć Sell Stop")
        return GateResult("earnings_gap_blocker", True, f"wyniki za {c.days_to_earnings} dni — OK")

    def g_liquidity(self, c: Candidate) -> GateResult:
        if c.avg_dollar_volume is None:
            return GateResult("liquidity", False, "nieznany obrót — konserwatywnie odrzucam")
        thr = self.cfg.min_dollar_volume_smallcap if c.is_smallcap else self.cfg.min_dollar_volume_midcap
        if c.avg_dollar_volume < thr:
            return GateResult("liquidity", False,
                              f"obrót {c.avg_dollar_volume:,.0f} < próg {thr:,.0f} USD")
        return GateResult("liquidity", True, f"obrót {c.avg_dollar_volume:,.0f} USD OK")

    def g_rsi(self, c: Candidate) -> GateResult:
        if c.rsi14 is None:
            return GateResult("rsi_not_overbought", True, "RSI nieznane — pomijam (nieblokujące)")
        if c.rsi14 >= self.cfg.rsi_max:
            return GateResult("rsi_not_overbought", False, f"RSI {c.rsi14:.0f} ≥ {self.cfg.rsi_max:.0f} (wykupienie)")
        return GateResult("rsi_not_overbought", True, f"RSI {c.rsi14:.0f} OK")

    def g_parabolic(self, c: Candidate) -> GateResult:
        if c.price_vs_sma20 is None:
            return GateResult("not_parabolic", True, "brak SMA20 — pomijam")
        # FURTKA: mocny katalizator -> luźniejszy próg (spółka może być wyżej, jeśli jeszcze nie osiągnęła maxa)
        sc = getattr(c, "strong_catalyst", False)
        thr = self.cfg.parabolic_sma_mult_override if sc else self.cfg.parabolic_sma_mult
        if c.price_vs_sma20 > thr:
            extra = " (mimo katalizatora — za gorące)" if sc else ""
            return GateResult("not_parabolic", False,
                              f"cena {c.price_vs_sma20:.2f}× SMA20 > {thr} (parabola){extra}")
        note = " [furtka: katalizator]" if sc and c.price_vs_sma20 > self.cfg.parabolic_sma_mult else ""
        return GateResult("not_parabolic", True, f"cena {c.price_vs_sma20:.2f}× SMA20 OK{note}")

    def g_cash(self, c: Candidate) -> GateResult:
        need = c.position_value_pln * (1.0 + self.cfg.cost_buffer)
        if need > c.cash_available_pln:
            return GateResult("cash_sufficiency", False,
                              f"trzeba {need:.2f} PLN (z buforem), jest {c.cash_available_pln:.2f}")
        return GateResult("cash_sufficiency", True, f"pokrycie OK ({need:.2f} ≤ {c.cash_available_pln:.2f})")

    def g_min_order(self, c: Candidate) -> GateResult:
        usd = c.position_value_pln / c.usd_pln_rate if c.usd_pln_rate > 0 else 0
        if usd < self.cfg.min_order_usd:
            return GateResult("min_order_usd", False, f"{usd:.2f} USD < min {self.cfg.min_order_usd} USD")
        return GateResult("min_order_usd", True, f"{usd:.2f} USD ≥ min OK")

    def g_fractional(self, c: Candidate) -> GateResult:
        if c.fractional_enabled is None:
            return GateResult("fractional_enabled", False, "nieznany status fractional — konserwatywnie odrzucam")
        if not c.fractional_enabled:
            return GateResult("fractional_enabled", False, "ticker NIE jest akcją ułamkową na XTB")
        return GateResult("fractional_enabled", True, "fractional OK")

    def g_macro(self, c: Candidate) -> GateResult:
        if c.radar_level >= 3:
            return GateResult("macro_allows", False, "Radar 3/3 — zakaz nowych wejść")
        return GateResult("macro_allows", True, f"Radar {c.radar_level}/3 OK")

    def g_hard_block(self, c: Candidate) -> GateResult:
        if c.smart_money_hard_block:
            return GateResult("no_hard_block", False, "HARD_BLOCK: klaster sprzedaży C-Suite")
        return GateResult("no_hard_block", True, "brak hard block")

    def g_spread(self, c: Candidate) -> GateResult:
        if c.spread_pct is None:
            return GateResult("spread_sanity", True, "spread nieznany — pomijam (live quote w routine)")
        thr = self.cfg.spread_max_smallcap if c.is_smallcap else self.cfg.spread_max_bluechip
        if c.spread_pct > thr:
            return GateResult("spread_sanity", False, f"spread {c.spread_pct:.3%} > {thr:.3%}")
        return GateResult("spread_sanity", True, f"spread {c.spread_pct:.3%} OK")

    def g_pending(self, c: Candidate) -> GateResult:
        if c.has_unresolved_pending:
            return GateResult("no_pending_unresolved", False,
                              "poprzednia propozycja nierozliczona — czekam na potwierdzenie wykonania")
        return GateResult("no_pending_unresolved", True, "brak zaległych propozycji")

    def g_session(self, c: Candidate) -> GateResult:
        if c.minutes_to_session_open is None:
            return GateResult("session_timing", True, "czas sesji nieznany — pomijam")
        if c.minutes_to_session_open < self.cfg.session_lead_minutes:
            return GateResult("session_timing", False,
                              f"do otwarcia {c.minutes_to_session_open} min < {self.cfg.session_lead_minutes}")
        return GateResult("session_timing", True, f"do otwarcia {c.minutes_to_session_open} min OK")

    def g_concentration(self, c: Candidate) -> GateResult:
        after = c.current_exposure_pln + c.position_value_pln
        cap = self.cfg.concentration_cap * c.equity_total_pln
        if after > cap + 1e-6:
            return GateResult("concentration_ok", False,
                              f"ekspozycja po zakupie {after:.2f} > cap {cap:.2f} ({self.cfg.concentration_cap:.0%} equity)")
        return GateResult("concentration_ok", True, f"koncentracja {after:.2f} ≤ {cap:.2f} OK")

    # ── uruchomienie wszystkich bramek ────────────────────────────────────────
    def evaluate(self, c: Candidate) -> GatesReport:
        gates: list[Callable[[Candidate], GateResult]] = [
            self.g_earnings, self.g_liquidity, self.g_rsi, self.g_parabolic,
            self.g_cash, self.g_min_order, self.g_fractional, self.g_macro,
            self.g_hard_block, self.g_spread, self.g_pending, self.g_session,
            self.g_concentration,
        ]
        results = [g(c) for g in gates]
        all_ok = all(r.passed for r in results)
        report = GatesReport(c.ticker, all_ok, results)
        if not all_ok:
            logger.info("[%s] ODRZUCONE przez bezpieczniki: %s",
                        c.ticker, "; ".join(r.reason for r in report.failures))
        else:
            logger.info("[%s] wszystkie 13 bezpieczników OK", c.ticker)
        return report


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline)
# ─────────────────────────────────────────────────────────────────────────────
def _good_candidate() -> Candidate:
    """Kandydat, który powinien przejść wszystkie bramki."""
    return Candidate(
        ticker="NVDA", price_usd=140.0, position_value_pln=320.0,
        cash_available_pln=1582.0, equity_total_pln=1582.0, usd_pln_rate=4.0,
        days_to_earnings=20, avg_dollar_volume=5_000_000_000.0, is_smallcap=False,
        rsi14=58.0, price_vs_sma20=1.05, fractional_enabled=True, radar_level=0,
        smart_money_hard_block=False, spread_pct=0.0008, has_unresolved_pending=False,
        minutes_to_session_open=45, current_exposure_pln=0.0,
    )


def _run_selftest() -> int:
    print("=== SELFTEST safety_gates (offline) ===")
    sg = SafetyGates()
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # 1. Dobry kandydat -> wszystkie 13 OK
    rep = sg.evaluate(_good_candidate())
    check("Dobry kandydat: wszystkie 13 bezpieczników PASS", rep.all_passed and len(rep.results) == 13)

    # 2. Earnings za 1 dzień -> FAIL
    c = _good_candidate(); c.days_to_earnings = 1
    rep = sg.evaluate(c)
    check("Earnings T+1 -> odrzut (luka na Sell Stop)", not rep.all_passed and
          any(f.name == "earnings_gap_blocker" for f in rep.failures))

    # 3. Nieznana data wyników -> konserwatywnie FAIL
    c = _good_candidate(); c.days_to_earnings = None
    rep = sg.evaluate(c)
    check("Nieznane earnings -> konserwatywny odrzut", not rep.all_passed)

    # 4. Niska płynność -> FAIL
    c = _good_candidate(); c.avg_dollar_volume = 1_000_000.0
    rep = sg.evaluate(c)
    check("Niska płynność -> odrzut", any(f.name == "liquidity" for f in rep.failures))

    # 5. RSI wykupienie -> FAIL
    c = _good_candidate(); c.rsi14 = 82.0
    rep = sg.evaluate(c)
    check("RSI 82 -> odrzut", any(f.name == "rsi_not_overbought" for f in rep.failures))

    # 6. Parabola -> FAIL
    c = _good_candidate(); c.price_vs_sma20 = 1.30
    rep = sg.evaluate(c)
    check("Cena 1.30× SMA20 -> odrzut paraboli", any(f.name == "not_parabolic" for f in rep.failures))

    # 7. Za mało gotówki -> FAIL
    c = _good_candidate(); c.cash_available_pln = 100.0
    rep = sg.evaluate(c)
    check("Za mało gotówki -> odrzut", any(f.name == "cash_sufficiency" for f in rep.failures))

    # 8. HARD_BLOCK -> FAIL
    c = _good_candidate(); c.smart_money_hard_block = True
    rep = sg.evaluate(c)
    check("HARD_BLOCK -> odrzut", any(f.name == "no_hard_block" for f in rep.failures))

    # 9. Radar 3/3 -> FAIL
    c = _good_candidate(); c.radar_level = 3
    rep = sg.evaluate(c)
    check("Radar 3/3 -> odrzut", any(f.name == "macro_allows" for f in rep.failures))

    # 10. Fractional wyłączone -> FAIL
    c = _good_candidate(); c.fractional_enabled = False
    rep = sg.evaluate(c)
    check("Brak fractional -> odrzut", any(f.name == "fractional_enabled" for f in rep.failures))

    # 11. Nierozliczona poprzednia propozycja -> FAIL
    c = _good_candidate(); c.has_unresolved_pending = True
    rep = sg.evaluate(c)
    check("Zaległa propozycja -> odrzut", any(f.name == "no_pending_unresolved" for f in rep.failures))

    # 12. Koncentracja przekroczona -> FAIL
    c = _good_candidate(); c.current_exposure_pln = 900.0  # 900+320=1220 > 0.6*1582=949
    rep = sg.evaluate(c)
    check("Koncentracja > 60% equity -> odrzut", any(f.name == "concentration_ok" for f in rep.failures))

    # 13. Spread za szeroki -> FAIL
    c = _good_candidate(); c.spread_pct = 0.02
    rep = sg.evaluate(c)
    check("Spread 2% -> odrzut", any(f.name == "spread_sanity" for f in rep.failures))

    # 14. Min order: pozycja 30 PLN = 7.5 USD < 10 -> FAIL
    c = _good_candidate(); c.position_value_pln = 30.0
    rep = sg.evaluate(c)
    check("Pozycja 7.5 USD < 10 -> odrzut", any(f.name == "min_order_usd" for f in rep.failures))

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== safety_gates.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Safety Gates")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
