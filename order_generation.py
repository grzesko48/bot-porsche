"""
order_generation.py — GENERATOR ZLECEŃ (dwustopniowa egzekucja XTB OMI)
========================================================================
Bot Porsche — zamienia zaakceptowaną pozycję w KONKRETNE dyrektywy dla użytkownika.

REALIA XTB (akcje rzeczywiste / OMI):
  • Stop Loss NIE dokleja się do pozycji — to OSOBNE zlecenie oczekujące "Sell Stop".
  • Take Profit = osobne "Sell Limit".
  • Trailing Stop NIE istnieje dla akcji — ZAKAZANY w tym module.
  • Zlecenia wpisuje się w cenie instrumentu (USD dla akcji USA), ale KOMUNIKAT do użytkownika
    prowadzimy w ZŁOTÓWKACH (prościej), z ceną USD tylko tam, gdzie trzeba ją wpisać na platformie.

WALUTA: użytkownik handluje na koncie PLN i myśli w złotówkach. Dlatego:
  • Wartości pozycji, stop loss, take profit — pokazujemy GŁÓWNIE w PLN.
  • Cena akcji i cena aktywacji Sell Stop — w USD (bo tak wpisuje się na XTB) + ekwiwalent PLN poglądowo.

STOP LOSS: liczony z ATR (k×ATR poniżej entry), z twardym ograniczeniem max -8% od entry.
Brak ATR -> fallback procentowy -7%.

Czysto obliczeniowy. Brak sieci.
Uruchomienie testów: python order_generation.py --selftest
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("porsche.orders")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class OrderConfig:
    atr_stop_mult: float = 2.0          # k w SL = entry - k*ATR
    max_stop_pct: float = 0.08          # SL nigdy dalej niż -8% od entry
    fallback_stop_pct: float = 0.07     # gdy brak ATR -> -7%
    take_profit_pct: float = 0.0        # 0 = bez TP (opcjonalne); >0 wlicza Sell Limit


@dataclass
class OrderPlan:
    """Komplet dyrektyw dla jednej pozycji — gotowy do wstawienia do maila."""
    ticker: str
    shares: float                       # wolumen ułamkowy
    entry_price_usd: float
    value_pln: float                    # wartość pozycji w PLN
    value_usd: float
    stop_price_usd: float
    stop_value_pln: float               # poglądowa wartość SL w PLN (po kursie)
    stop_pct: float                     # o ile % poniżej entry
    usd_pln_rate: float
    take_profit_usd: Optional[float] = None
    take_profit_pln: Optional[float] = None
    directives: list = field(default_factory=list)   # lista kroków tekstowych (PL)


class OrderGenerator:
    def __init__(self, config: Optional[OrderConfig] = None):
        self.cfg = config or OrderConfig()

    def _stop_price(self, entry_usd: float, atr_usd: Optional[float]) -> tuple[float, float]:
        """Zwraca (cena_stop_usd, procent_ponizej_entry). ATR-based z ograniczeniem -8%."""
        cfg = self.cfg
        if atr_usd and atr_usd > 0:
            raw_stop = entry_usd - cfg.atr_stop_mult * atr_usd
            # nie dalej niż -8% (czyli stop nie może być niżej niż entry*(1-0.08))
            floor_stop = entry_usd * (1.0 - cfg.max_stop_pct)
            stop = max(raw_stop, floor_stop)   # bierzemy CIASNIEJSZY z dwóch (wyższa cena = mniejsza strata)
        else:
            stop = entry_usd * (1.0 - cfg.fallback_stop_pct)
        pct = (entry_usd - stop) / entry_usd
        return round(stop, 2), pct

    def build_plan(
        self,
        ticker: str,
        shares: float,
        entry_price_usd: float,
        usd_pln_rate: float,
        atr_usd: Optional[float] = None,
    ) -> OrderPlan:
        """Buduje OrderPlan z dyrektywami po polsku (złotówki jako główna waluta)."""
        cfg = self.cfg
        value_usd = shares * entry_price_usd
        value_pln = value_usd * usd_pln_rate
        stop_usd, stop_pct = self._stop_price(entry_price_usd, atr_usd)
        stop_value_pln = shares * stop_usd * usd_pln_rate

        tp_usd = tp_pln = None
        if cfg.take_profit_pct > 0:
            tp_usd = round(entry_price_usd * (1.0 + cfg.take_profit_pct), 2)
            tp_pln = shares * tp_usd * usd_pln_rate

        # dyrektywy PO POLSKU, złotówki główną walutą, USD tam gdzie trzeba wpisać
        directives = [
            f"KROK 1 — KUP (zlecenie rynkowe):",
            f"   • Spółka: {ticker}",
            f"   • Wolumen: {shares:.4f} akcji",
            f"   • Szacowana wartość: ~{value_pln:,.0f} zł (cena akcji ≈ {entry_price_usd:.2f} USD)",
            f"   • Typ zlecenia: Kup / Market (po cenie rynkowej)",
            f"",
            f"KROK 2 — USTAW STOP LOSS (OSOBNE zlecenie oczekujące):",
            f"   • W panelu zleceń oczekujących wybierz typ: SELL STOP",
            f"     (UWAGA: to NIE jest przycisk 'Sprzedaj'! Szukaj 'Sell Stop' / 'Sprzedaj Stop')",
            f"   • Wolumen: {shares:.4f} (cała pozycja)",
            f"   • Cena aktywacji: {stop_usd:.2f} USD  (≈ {stop_value_pln:,.0f} zł wartości pozycji; -{stop_pct*100:.1f}% od wejścia)",
            f"   • To Twoja ostatnia linia obrony — strata ograniczona do ~{stop_pct*100:.1f}%.",
        ]
        if tp_usd:
            directives += [
                f"",
                f"KROK 3 (opcjonalnie) — TAKE PROFIT:",
                f"   • Typ: SELL LIMIT, wolumen {shares:.4f}, cena {tp_usd:.2f} USD (≈ {tp_pln:,.0f} zł)",
            ]
        directives += [
            f"",
            f"Trailing Stop: NIE używamy (XTB nie obsługuje go dla akcji rzeczywistych).",
            f"Uwaga walutowa: akcja kwotowana w USD, więc ceny wpisujesz w USD; złotówki to przeliczenie "
            f"po kursie {usd_pln_rate:.3f} i mają charakter poglądowy.",
        ]

        return OrderPlan(
            ticker=ticker, shares=shares, entry_price_usd=round(entry_price_usd, 2),
            value_pln=round(value_pln, 2), value_usd=round(value_usd, 2),
            stop_price_usd=stop_usd, stop_value_pln=round(stop_value_pln, 2),
            stop_pct=round(stop_pct, 4), usd_pln_rate=usd_pln_rate,
            take_profit_usd=tp_usd, take_profit_pln=(round(tp_pln, 2) if tp_pln else None),
            directives=directives,
        )

    def render_text(self, plan: OrderPlan) -> str:
        return "\n".join(plan.directives)


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST order_generation (offline) ===")
    gen = OrderGenerator()
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # 1. Plan z ATR
    p = gen.build_plan("NVDA", shares=0.5500, entry_price_usd=140.0, usd_pln_rate=4.0, atr_usd=3.0)
    check("Wartość pozycji 0.55*140*4 = 308 zł", abs(p.value_pln - 308.0) < 0.5)
    # SL ATR: 140 - 2*3 = 134; -8% floor = 128.8; max(134,128.8)=134 -> -4.3%
    check("Stop ATR = 134 USD", abs(p.stop_price_usd - 134.0) < 0.01)
    check("Stop ~-4.3%", abs(p.stop_pct - 0.0429) < 0.001)

    # 2. ATR za duże -> ograniczenie do -8%
    p = gen.build_plan("WILD", shares=1.0, entry_price_usd=100.0, usd_pln_rate=4.0, atr_usd=10.0)
    # raw = 100-20=80 (-20%); floor = 92 (-8%); bierzemy max=92
    check("Duże ATR -> stop ograniczony do -8% (92 USD)", abs(p.stop_price_usd - 92.0) < 0.01)
    check("Stop pct = 8%", abs(p.stop_pct - 0.08) < 0.001)

    # 3. Brak ATR -> fallback -7%
    p = gen.build_plan("ABC", shares=1.0, entry_price_usd=100.0, usd_pln_rate=4.0, atr_usd=None)
    check("Brak ATR -> stop -7% (93 USD)", abs(p.stop_price_usd - 93.0) < 0.01)

    # 4. Dyrektywy zawierają dwa kroki i ostrzeżenie o Sell Stop
    p = gen.build_plan("NVDA", shares=0.55, entry_price_usd=140.0, usd_pln_rate=4.0, atr_usd=3.0)
    txt = gen.render_text(p)
    check("Dyrektywy mają KROK 1 (Kup)", "KROK 1" in txt and "Market" in txt)
    check("Dyrektywy mają KROK 2 (Sell Stop)", "KROK 2" in txt and "SELL STOP" in txt)
    check("Ostrzeżenie: to nie 'Sprzedaj'", "NIE jest przycisk" in txt)
    check("Zakaz trailing stop wspomniany", "Trailing Stop: NIE" in txt)
    check("Złotówki jako główna waluta (zł w treści)", "zł" in txt)

    # 5. Take profit gdy włączony
    gen_tp = OrderGenerator(OrderConfig(take_profit_pct=0.10))
    p = gen_tp.build_plan("NVDA", shares=0.5, entry_price_usd=140.0, usd_pln_rate=4.0, atr_usd=3.0)
    check("Take profit 154 USD przy +10%", p.take_profit_usd is not None and abs(p.take_profit_usd - 154.0) < 0.01)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== order_generation.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Generator zleceń (Buy + Sell Stop)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
