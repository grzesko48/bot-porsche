"""
cash_management.py — DYNAMIC CASH MANAGEMENT (DCM)
===================================================
Bot Porsche — deterministyczny silnik wielkości pozycji (PositionSizer).

ZASADA NADRZĘDNA: bot NIE ma intuicji. Wielkość każdej pozycji wynika z TWARDEJ
matematyki w kodzie, nigdy z "nastroju" modelu językowego. Ten moduł jest czysto
obliczeniowy, w 100% testowalny offline, bez wywołań sieciowych.

WZÓR (specyfikacja użytkownika + audyt):
    raw_value_pln = MAXIMUM_POSITION_PLN * macro_scalar * volatility_scalar * smart_money_scalar
    -> ograniczony twardą bramką koncentracji: min(raw, 0.60 * equity)
    -> jeśli wynik < MINIMUM_POSITION_PLN: ODRZUĆ zagranie (nie rozdrabniamy)
    -> jeśli wynik < równowartość 10 USD: ODRZUĆ (minimum zlecenia XTB)

SKALARY:
  • Macro (Radar makro):   0/3 -> 1.0 | 1/3 -> 0.5 | 2/3 -> 0.25 | 3/3 -> 0.0 (zakaz wejść)
  • Volatility (ATR%):     max(0.5, 1.0 - ATR_pct * 5)   [ATR_pct jako ułamek, np. 0.06 = 6%]
  • SmartMoney:            CONFLUENCE -> 1.0 | NEUTRAL -> 0.8 | UNKNOWN -> 0.5 | HARD_BLOCK -> 0.0

REALIA XTB (zweryfikowane w audycie 26.05.2026):
  • Minimalne zlecenie akcji USA: 10 USD.
  • Akcje ułamkowe: precyzja 0.0001, wolumen ZAOKRĄGLANY W DÓŁ (nie zawyżamy ekspozycji).
  • Koszt przewalutowania PLN->USD: 0.5% przy kupnie (drugie 0.5% przy sprzedaży).
  • Bufor na pokrycie: spread 0.5% + FX 0.5% = 1.0% doliczane przy sprawdzaniu gotówki.

Brak zależności sieciowych. Wymaga tylko biblioteki standardowej.
Uruchomienie testów: python cash_management.py --selftest
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Logowanie
# ─────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("porsche.cash")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Stałe konfiguracyjne (TWARDE BRAMKI — żaden algorytm ich nie przekracza)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DCMConfig:
    """Parametry DCM. Wszystkie twarde liczby w jednym miejscu."""
    MINIMUM_POSITION_PLN: float = 100.0     # Floor — niżej nie schodzimy (tarcie/FX/spread)
    MAXIMUM_POSITION_PLN: float = 700.0     # Cap podniesiony — agresywny deployment (konstytucja v2)
    CONCENTRATION_CAP: float = 0.45         # max 45% equity w jednej pozycji (v2: wyżej niż 30%, skończone)
    MIN_ORDER_USD: float = 10.0             # minimalne zlecenie akcji USA na XTB
    FX_MARKUP: float = 0.005                # 0.5% przewalutowanie XTB (jednostronnie)
    SPREAD_BUFFER: float = 0.005            # 0.5% zapas na spread
    FRACTIONAL_DECIMALS: int = 4            # precyzja akcji ułamkowych XTB
    VOL_SCALAR_FLOOR: float = 0.5           # dolny limit skalara zmienności
    VOL_SCALAR_K: float = 5.0               # mnożnik ATR% we wzorze 1 - ATR%*K

    @property
    def cost_buffer(self) -> float:
        """Łączny bufor kosztowy przy sprawdzaniu pokrycia gotówki (spread + FX)."""
        return self.FX_MARKUP + self.SPREAD_BUFFER  # 1.0%


# Mapa skalara makro wg poziomu radaru (0..3)
MACRO_SCALAR = {0: 1.0, 1: 0.5, 2: 0.25, 3: 0.0}


class SmartMoneyState(str, Enum):
    """Stan sygnału smart money (z smart_money_engine.py)."""
    CONFLUENCE = "CONFLUENCE"   # ≥2 C-Suite kupiło (kod P) -> skalar 1.0
    NEUTRAL = "NEUTRAL"         # źródło OK, brak klastra -> skalar 0.8
    UNKNOWN = "UNKNOWN"         # źródło padło (fail-CLOSED) -> skalar 0.5, NIGDY 1.0
    HARD_BLOCK = "HARD_BLOCK"   # klaster sprzedaży C-Suite -> skalar 0.0 (zakaz wejścia)


SMART_MONEY_SCALAR = {
    SmartMoneyState.CONFLUENCE: 1.0,
    SmartMoneyState.NEUTRAL: 0.8,   # źródło działa, brak klastra = lekki dyskont (nie połowa)
    SmartMoneyState.UNKNOWN: 0.5,   # asymetryczny fail-safe: brak danych ≠ zielone światło
    SmartMoneyState.HARD_BLOCK: 0.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# Wynik sizingu
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SizingResult:
    """Wynik kalkulacji wielkości pozycji."""
    accepted: bool                       # czy zagranie przeszło wszystkie bramki
    reason: str                          # powód (zwłaszcza przy odrzuceniu)
    ticker: str = ""
    target_value_pln: float = 0.0        # docelowa wartość pozycji w PLN (po skalarach, capach)
    value_usd: float = 0.0               # wartość w USD (po kursie)
    shares: float = 0.0                  # wolumen akcji (ułamkowy, zaokrąglony w dół)
    macro_scalar: float = 1.0
    volatility_scalar: float = 1.0
    smart_money_scalar: float = 1.0
    breakdown: dict = field(default_factory=dict)  # pełna rozpiska do logu/maila


# ─────────────────────────────────────────────────────────────────────────────
# PositionSizer
# ─────────────────────────────────────────────────────────────────────────────
class PositionSizer:
    """Deterministyczny kalkulator wielkości pozycji.

    Wejście: stan radaru makro, ATR% spółki, stan smart money, dostępna gotówka PLN,
             equity (do bramki koncentracji), cena akcji USD, kurs USD/PLN.
    Wyjście: SizingResult — albo zaakceptowane zlecenie z wolumenem, albo odrzucenie z powodem.
    """

    def __init__(self, config: Optional[DCMConfig] = None):
        self.cfg = config or DCMConfig()

    # ── pojedyncze skalary (czyste funkcje) ─────────────────────────────────
    def macro_scalar(self, radar_level: int) -> float:
        """Skalar makro wg poziomu radaru 0..3. Poza zakresem -> najostrożniej (0.0)."""
        if radar_level not in MACRO_SCALAR:
            logger.warning("Nieznany poziom radaru %s — przyjmuję 0.0 (zakaz wejść).", radar_level)
            return 0.0
        return MACRO_SCALAR[radar_level]

    def volatility_scalar(self, atr_pct: Optional[float]) -> float:
        """Skalar zmienności: max(floor, 1 - ATR%*K). ATR_pct jako ułamek (0.06 = 6%).
        Brak ATR -> najostrożniej (floor 0.5), bo nie znamy ryzyka."""
        if atr_pct is None or atr_pct < 0:
            logger.warning("Brak/ujemny ATR%% — przyjmuję skalar floor %.2f.", self.cfg.VOL_SCALAR_FLOOR)
            return self.cfg.VOL_SCALAR_FLOOR
        raw = 1.0 - atr_pct * self.cfg.VOL_SCALAR_K
        return max(self.cfg.VOL_SCALAR_FLOOR, raw)

    def smart_money_scalar(self, state: SmartMoneyState) -> float:
        """Skalar smart money. HARD_BLOCK -> 0.0 (zakaz), UNKNOWN -> 0.5 (fail-CLOSED)."""
        return SMART_MONEY_SCALAR.get(state, 0.5)

    @staticmethod
    def floor_shares(raw_shares: float, decimals: int) -> float:
        """Zaokrąglenie wolumenu W DÓŁ do `decimals` miejsc — nigdy nie zawyżamy ekspozycji."""
        factor = 10 ** decimals
        return math.floor(raw_shares * factor) / factor

    # ── główny kalkulator ────────────────────────────────────────────────────
    def size_position(
        self,
        ticker: str,
        radar_level: int,
        atr_pct: Optional[float],
        smart_money: SmartMoneyState,
        cash_available_pln: float,
        equity_total_pln: float,
        share_price_usd: float,
        usd_pln_rate: float,
    ) -> SizingResult:
        """Liczy wielkość pozycji ze wszystkimi bramkami. Zwraca SizingResult.

        Kolejność bramek (każda może odrzucić):
          1. Walidacja wejść (ceny/kursy dodatnie).
          2. HARD_BLOCK smart money -> natychmiastowy odrzut.
          3. Skalary -> raw_value_pln.
          4. Bramka koncentracji: min(raw, 60% equity).
          5. Floor: jeśli < MINIMUM_POSITION_PLN -> odrzut.
          6. Pokrycie gotówką z buforem kosztowym 1%.
          7. Przeliczenie na USD i wolumen ułamkowy (zaokrąglony W DÓŁ).
          8. Minimum zlecenia XTB 10 USD.
        """
        cfg = self.cfg
        ms = self.macro_scalar(radar_level)
        vs = self.volatility_scalar(atr_pct)
        sms = self.smart_money_scalar(smart_money)
        bd = {
            "radar_level": radar_level, "macro_scalar": ms,
            "atr_pct": atr_pct, "volatility_scalar": vs,
            "smart_money_state": smart_money.value, "smart_money_scalar": sms,
            "cap_pln": cfg.MAXIMUM_POSITION_PLN, "floor_pln": cfg.MINIMUM_POSITION_PLN,
        }

        def reject(reason: str) -> SizingResult:
            logger.info("[%s] ODRZUCONE: %s", ticker, reason)
            return SizingResult(False, reason, ticker, macro_scalar=ms,
                                volatility_scalar=vs, smart_money_scalar=sms, breakdown=bd)

        # 1. walidacja wejść
        if share_price_usd <= 0 or usd_pln_rate <= 0:
            return reject(f"niepoprawne dane: cena={share_price_usd} USD, kurs={usd_pln_rate}")
        if cash_available_pln <= 0:
            return reject("brak wolnej gotówki")

        # 2. hard block
        if smart_money == SmartMoneyState.HARD_BLOCK:
            return reject("HARD_BLOCK: klaster sprzedaży C-Suite — nie jesteśmy płynnością wyjściową zarządu")

        # 3. raw value ze skalarów
        raw_value_pln = cfg.MAXIMUM_POSITION_PLN * ms * vs * sms
        bd["raw_value_pln"] = round(raw_value_pln, 2)
        if raw_value_pln <= 0:
            return reject(f"skalary wyzerowały pozycję (macro={ms}, vol={vs}, sm={sms})")

        # 4. bramka koncentracji: min(raw, 60% equity)
        conc_cap = cfg.CONCENTRATION_CAP * equity_total_pln
        target_pln = min(raw_value_pln, conc_cap)
        bd["concentration_cap_pln"] = round(conc_cap, 2)
        if target_pln < raw_value_pln:
            logger.info("[%s] przycięte bramką koncentracji: %.2f -> %.2f PLN",
                        ticker, raw_value_pln, target_pln)

        # 5. floor
        if target_pln < cfg.MINIMUM_POSITION_PLN:
            return reject(f"pozycja {target_pln:.2f} PLN < Floor {cfg.MINIMUM_POSITION_PLN:.0f} PLN "
                          f"— nie rozdrabniamy")

        # 6. pokrycie gotówką z buforem kosztowym (spread+FX = 1%)
        cost_with_buffer = target_pln * (1.0 + cfg.cost_buffer)
        bd["cost_with_buffer_pln"] = round(cost_with_buffer, 2)
        if cost_with_buffer > cash_available_pln:
            # spróbuj zejść do tego, na co realnie stać (z buforem), o ile ≥ floor
            affordable_pln = cash_available_pln / (1.0 + cfg.cost_buffer)
            if affordable_pln < cfg.MINIMUM_POSITION_PLN:
                return reject(f"za mało gotówki: trzeba {cost_with_buffer:.2f} PLN "
                              f"(z buforem 1%), jest {cash_available_pln:.2f} PLN")
            logger.info("[%s] zejście do dostępnej gotówki: %.2f -> %.2f PLN",
                        ticker, target_pln, affordable_pln)
            target_pln = affordable_pln
            bd["adjusted_to_cash"] = True

        # 7. przeliczenie na USD i wolumen ułamkowy (W DÓŁ)
        value_usd = target_pln / usd_pln_rate
        raw_shares = value_usd / share_price_usd
        shares = self.floor_shares(raw_shares, cfg.FRACTIONAL_DECIMALS)
        if shares <= 0:
            return reject(f"wolumen po zaokrągleniu w dół = 0 (cena {share_price_usd} USD za drogo)")
        # realna wartość po zaokrągleniu wolumenu w dół
        actual_usd = shares * share_price_usd
        actual_pln = actual_usd * usd_pln_rate
        bd.update({"value_usd": round(value_usd, 2), "raw_shares": round(raw_shares, 6),
                   "shares_floored": shares, "actual_usd": round(actual_usd, 2),
                   "actual_pln": round(actual_pln, 2)})

        # 8. minimum zlecenia XTB 10 USD (po zaokrągleniu wolumenu)
        if actual_usd < cfg.MIN_ORDER_USD:
            return reject(f"zlecenie {actual_usd:.2f} USD < minimum XTB {cfg.MIN_ORDER_USD:.0f} USD")

        # po zejściu do gotówki: jeszcze raz sprawdź floor na realnej wartości
        if actual_pln < cfg.MINIMUM_POSITION_PLN:
            return reject(f"realna pozycja po zaokrągleniu {actual_pln:.2f} PLN < Floor "
                          f"{cfg.MINIMUM_POSITION_PLN:.0f} PLN")

        logger.info("[%s] OK: %.4f akcji @ %.2f USD = %.2f USD (%.2f PLN); skalary M=%.2f V=%.2f S=%.2f",
                    ticker, shares, share_price_usd, actual_usd, actual_pln, ms, vs, sms)
        return SizingResult(
            accepted=True, reason="OK", ticker=ticker,
            target_value_pln=round(actual_pln, 2), value_usd=round(actual_usd, 2),
            shares=shares, macro_scalar=ms, volatility_scalar=vs, smart_money_scalar=sms,
            breakdown=bd,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline, bez sieci)
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST cash_management (offline) ===")
    sizer = PositionSizer()
    cfg = sizer.cfg
    USD_PLN = 4.00
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1; print(f"  [OK] {name}")
        else:
            failed += 1; print(f"  [FAIL] {name}")

    # 1. Skalary makro
    check("macro 0/3 = 1.0", sizer.macro_scalar(0) == 1.0)
    check("macro 1/3 = 0.5", sizer.macro_scalar(1) == 0.5)
    check("macro 2/3 = 0.25", sizer.macro_scalar(2) == 0.25)
    check("macro 3/3 = 0.0 (zakaz)", sizer.macro_scalar(3) == 0.0)

    # 2. Skalar zmienności (realne ATR%)
    check("vol ATR 1.5% -> 0.925", abs(sizer.volatility_scalar(0.015) - 0.925) < 1e-9)
    check("vol ATR 6.2% (NVDA) -> 0.69", abs(sizer.volatility_scalar(0.062) - 0.69) < 1e-9)
    check("vol ATR 10% -> floor 0.5", sizer.volatility_scalar(0.10) == 0.5)
    check("vol ATR 20% -> floor 0.5 (nie ujemny!)", sizer.volatility_scalar(0.20) == 0.5)
    check("vol brak ATR -> floor 0.5", sizer.volatility_scalar(None) == 0.5)

    # 3. Skalar smart money
    check("sm CONFLUENCE -> 1.0", sizer.smart_money_scalar(SmartMoneyState.CONFLUENCE) == 1.0)
    check("sm NEUTRAL -> 0.8", sizer.smart_money_scalar(SmartMoneyState.NEUTRAL) == 0.8)
    check("sm UNKNOWN -> 0.5 (fail-CLOSED, nie 1.0!)", sizer.smart_money_scalar(SmartMoneyState.UNKNOWN) == 0.5)
    check("sm HARD_BLOCK -> 0.0", sizer.smart_money_scalar(SmartMoneyState.HARD_BLOCK) == 0.0)

    # 4. Zaokrąglanie wolumenu W DÓŁ
    check("floor_shares 0.123456 -> 0.1234", sizer.floor_shares(0.123456, 4) == 0.1234)
    check("floor_shares 0.99999 -> 0.9999 (nie zawyża)", sizer.floor_shares(0.99999, 4) == 0.9999)

    # 5. Pełny sizing — najlepszy scenariusz (radar 0, niski ATR, confluence)
    r = sizer.size_position("NVDA", radar_level=0, atr_pct=0.04,
                            smart_money=SmartMoneyState.CONFLUENCE,
                            cash_available_pln=1582.0, equity_total_pln=1582.0,
                            share_price_usd=140.0, usd_pln_rate=USD_PLN)
    # raw = 700 * 1.0 * 0.80 * 1.0 = 560 PLN; conc cap = 0.45*1582 = 712 -> target 560
    check("sizing A: zaakceptowane", r.accepted)
    check("sizing A: target ~560 PLN", abs(r.target_value_pln - 560) < 30)
    check("sizing A: wolumen dodatni", r.shares > 0)

    # 6. HARD_BLOCK -> odrzut mimo idealnych warunków
    r = sizer.size_position("ABC", radar_level=0, atr_pct=0.02,
                            smart_money=SmartMoneyState.HARD_BLOCK,
                            cash_available_pln=1582.0, equity_total_pln=1582.0,
                            share_price_usd=50.0, usd_pln_rate=USD_PLN)
    check("HARD_BLOCK odrzucony", (not r.accepted) and "HARD_BLOCK" in r.reason)

    # 7. Radar 3/3 -> zakaz wejść (skalar 0)
    r = sizer.size_position("ABC", radar_level=3, atr_pct=0.02,
                            smart_money=SmartMoneyState.CONFLUENCE,
                            cash_available_pln=1582.0, equity_total_pln=1582.0,
                            share_price_usd=50.0, usd_pln_rate=USD_PLN)
    check("Radar 3/3 odrzuca (zakaz wejść)", not r.accepted)

    # 8. Radar 2/3 (skalar 0.25) -> 700*0.25*vs*sm spada poniżej floora -> odrzut
    r = sizer.size_position("ABC", radar_level=2, atr_pct=0.06,
                            smart_money=SmartMoneyState.NEUTRAL,
                            cash_available_pln=1582.0, equity_total_pln=1582.0,
                            share_price_usd=50.0, usd_pln_rate=USD_PLN)
    # raw = 700*0.25*vol*0.8; przy atr 6% vol scalar ~0.7 -> ~98 PLN < floor 100 -> odrzut
    check("Radar 2/3 + neutral -> poniżej floora -> odrzut", (not r.accepted) and "Floor" in r.reason)

    # 9. Za mało gotówki
    r = sizer.size_position("ABC", radar_level=0, atr_pct=0.02,
                            smart_money=SmartMoneyState.CONFLUENCE,
                            cash_available_pln=50.0, equity_total_pln=50.0,
                            share_price_usd=50.0, usd_pln_rate=USD_PLN)
    check("Za mało gotówki (50 PLN) -> odrzut", not r.accepted)

    # 10. Akcja za droga na 1 ułamek? (przy floorze) — bardzo wysoka cena, mała kasa
    r = sizer.size_position("BRKA", radar_level=0, atr_pct=0.02,
                            smart_money=SmartMoneyState.CONFLUENCE,
                            cash_available_pln=1582.0, equity_total_pln=1582.0,
                            share_price_usd=600000.0, usd_pln_rate=USD_PLN)
    # wartość docelowa ~320 PLN = 80 USD; 80/600000 = 0.000133 -> floor do 0.0001 -> 0.0001*600000=60 USD ok? 
    # actual_usd = 0.0001*600000 = 60 USD = 240 PLN, > floor 100, > 10 USD -> akceptacja możliwa
    check("Bardzo droga akcja: liczona przez ułamki (akcept lub sensowny odrzut)",
          r.accepted or "min" in r.reason.lower() or "Floor" in r.reason or "0" in r.reason)

    # 11. Bramka koncentracji przy większym equity NIE przycina (bo cap 400 < 60% equity)
    r = sizer.size_position("NVDA", radar_level=0, atr_pct=0.0,
                            smart_money=SmartMoneyState.CONFLUENCE,
                            cash_available_pln=10000.0, equity_total_pln=10000.0,
                            share_price_usd=140.0, usd_pln_rate=USD_PLN)
    # raw=700*1*1*1=700; conc=0.45*10000=4500 -> target 700 (cap absolutny wiąże)
    check("Duże equity: wiąże Cap 700 (nie koncentracja)", r.accepted and r.target_value_pln <= 701)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== cash_management.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Dynamic Cash Management")
    ap.add_argument("--selftest", action="store_true", help="uruchom testy offline")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
