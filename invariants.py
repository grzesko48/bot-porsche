"""
invariants.py — TWARDE INWARIANTY STANU
========================================
Bot Porsche — ostatnia linia obrony. Zestaw niepodważalnych warunków, które MUSZĄ być
spełnione, zanim bot cokolwiek zaproponuje. Naruszenie któregokolwiek = sys.exit(1).

DLACZEGO: nawet jeśli reconcile i bezpieczniki przejdą, prosty błąd parsowania mógłby
pokazać ZA DUŻO gotówki (np. policzony depozyt dwa razy, pominięta wypłata). Wtedy bot
zaproponowałby zakup bez pokrycia. Inwarianty łapią takie absurdy zanim wyrządzą szkodę.

NAJWAŻNIEJSZY INWARIANT:
  cash_dziś ≤ cash_wczoraj + depozyty_dziś + zrealizowane_zyski_dziś + dywidendy_dziś + ε
  (gotówka nie może wzrosnąć bardziej, niż wynika z realnych wpływów — chroni przed
   zawyżeniem salda przez błąd parsera)

Czysto obliczeniowy. Brak sieci.
Uruchomienie testów: python invariants.py --selftest
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("porsche.invariants")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


EPSILON_PLN = 0.01   # tolerancja zaokrągleń


@dataclass
class StateSnapshot:
    """Stan do weryfikacji inwariantów."""
    cash_today_pln: float
    cash_yesterday_pln: float
    deposits_today_pln: float = 0.0
    realized_pl_today_pln: float = 0.0
    dividends_today_pln: float = 0.0
    withdrawals_today_pln: float = 0.0     # dodatnia liczba (kwota wypłacona)
    positions_value_pln: float = 0.0
    n_positions: int = 0
    min_position_volume: float = 1.0       # najmniejszy wolumen wśród pozycji (>0)
    export_age_hours: float = 0.0


@dataclass
class InvariantViolation:
    name: str
    detail: str


class Invariants:
    def __init__(self, epsilon: float = EPSILON_PLN, max_export_age_hours: float = 24.0):
        self.eps = epsilon
        self.max_age = max_export_age_hours

    # ── pojedyncze inwarianty (zwracają None jeśli OK, albo InvariantViolation) ──
    def inv_cash_upper_bound(self, s: StateSnapshot) -> Optional[InvariantViolation]:
        """Gotówka nie może wzrosnąć ponad realne wpływy (ochrona przed zawyżeniem salda)."""
        max_allowed = (s.cash_yesterday_pln + s.deposits_today_pln
                       + max(0.0, s.realized_pl_today_pln) + s.dividends_today_pln + self.eps)
        if s.cash_today_pln > max_allowed:
            return InvariantViolation(
                "cash_upper_bound",
                f"gotówka dziś {s.cash_today_pln:.2f} > max dopuszczalna {max_allowed:.2f} "
                f"(wczoraj {s.cash_yesterday_pln:.2f} + depozyty {s.deposits_today_pln:.2f} "
                f"+ zyski {max(0.0, s.realized_pl_today_pln):.2f} + dyw {s.dividends_today_pln:.2f}) "
                f"— możliwy błąd parsera zawyżający saldo")
        return None

    def inv_cash_non_negative(self, s: StateSnapshot) -> Optional[InvariantViolation]:
        if s.cash_today_pln < -self.eps:
            return InvariantViolation("cash_non_negative", f"gotówka ujemna: {s.cash_today_pln:.2f}")
        return None

    def inv_positions_value_non_negative(self, s: StateSnapshot) -> Optional[InvariantViolation]:
        if s.positions_value_pln < -self.eps:
            return InvariantViolation("positions_value_non_negative",
                                      f"wartość pozycji ujemna: {s.positions_value_pln:.2f}")
        return None

    def inv_positions_volume_positive(self, s: StateSnapshot) -> Optional[InvariantViolation]:
        if s.n_positions > 0 and s.min_position_volume <= 0:
            return InvariantViolation("positions_volume_positive",
                                      f"pozycja z wolumenem ≤ 0 (min {s.min_position_volume})")
        return None

    def inv_equity_positive(self, s: StateSnapshot) -> Optional[InvariantViolation]:
        equity = s.cash_today_pln + s.positions_value_pln
        if equity <= 0:
            return InvariantViolation("equity_positive",
                                      f"equity ≤ 0: gotówka {s.cash_today_pln:.2f} + pozycje "
                                      f"{s.positions_value_pln:.2f} = {equity:.2f}")
        return None

    def inv_export_fresh(self, s: StateSnapshot) -> Optional[InvariantViolation]:
        if s.export_age_hours > self.max_age:
            return InvariantViolation("export_fresh",
                                      f"eksport sprzed {s.export_age_hours:.1f}h > {self.max_age:.0f}h "
                                      f"— dane nieaktualne")
        return None

    # ── sprawdzenie wszystkich ────────────────────────────────────────────────
    def check_all(self, s: StateSnapshot) -> list:
        """Zwraca listę naruszeń (pustą jeśli OK). NIE woła sys.exit — to robi caller."""
        checks: list[Callable[[StateSnapshot], Optional[InvariantViolation]]] = [
            self.inv_cash_upper_bound, self.inv_cash_non_negative,
            self.inv_positions_value_non_negative, self.inv_positions_volume_positive,
            self.inv_equity_positive, self.inv_export_fresh,
        ]
        violations = [v for v in (chk(s) for chk in checks) if v is not None]
        return violations

    def enforce_or_halt(self, s: StateSnapshot) -> None:
        """Produkcyjna ścieżka: przy jakimkolwiek naruszeniu — sys.exit(1)."""
        violations = self.check_all(s)
        if violations:
            for v in violations:
                logger.error("NARUSZENIE INWARIANTU [%s]: %s", v.name, v.detail)
            logger.error("HARD HALT — bot zatrzymany, brak rekomendacji.")
            sys.exit(1)
        logger.info("Wszystkie inwarianty OK (gotówka %.2f PLN, equity %.2f PLN).",
                    s.cash_today_pln, s.cash_today_pln + s.positions_value_pln)


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline)
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST invariants (offline) ===")
    inv = Invariants()
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # 1. Stan zdrowy -> brak naruszeń
    s = StateSnapshot(cash_today_pln=1260.4, cash_yesterday_pln=1582.0,
                      positions_value_pln=320.0, n_positions=1, min_position_volume=0.55,
                      export_age_hours=2.0)
    check("Zdrowy stan -> brak naruszeń", inv.check_all(s) == [])

    # 2. Gotówka zawyżona (błąd parsera: pokazuje więcej niż wczoraj bez wpływów) -> naruszenie
    s = StateSnapshot(cash_today_pln=3000.0, cash_yesterday_pln=1582.0, export_age_hours=1.0)
    v = inv.check_all(s)
    check("Zawyżona gotówka -> naruszenie cash_upper_bound",
          any(x.name == "cash_upper_bound" for x in v))

    # 3. Gotówka wzrosła, ale ZGODNIE z depozytem -> OK
    s = StateSnapshot(cash_today_pln=3000.0, cash_yesterday_pln=1582.0,
                      deposits_today_pln=1418.0, export_age_hours=1.0)
    check("Wzrost = depozyt -> OK", inv.check_all(s) == [])

    # 4. Gotówka ujemna -> naruszenie
    s = StateSnapshot(cash_today_pln=-5.0, cash_yesterday_pln=1582.0, export_age_hours=1.0)
    check("Gotówka ujemna -> naruszenie", any(x.name == "cash_non_negative" for x in inv.check_all(s)))

    # 5. Equity zerowe -> naruszenie
    s = StateSnapshot(cash_today_pln=0.0, cash_yesterday_pln=0.0, positions_value_pln=0.0,
                      export_age_hours=1.0)
    check("Equity 0 -> naruszenie", any(x.name == "equity_positive" for x in inv.check_all(s)))

    # 6. Pozycja z wolumenem 0 -> naruszenie
    s = StateSnapshot(cash_today_pln=100.0, cash_yesterday_pln=1582.0,
                      positions_value_pln=200.0, n_positions=1, min_position_volume=0.0,
                      export_age_hours=1.0)
    check("Wolumen 0 -> naruszenie", any(x.name == "positions_volume_positive" for x in inv.check_all(s)))

    # 7. Stary eksport -> naruszenie
    s = StateSnapshot(cash_today_pln=1582.0, cash_yesterday_pln=1582.0, export_age_hours=48.0)
    check("Eksport 48h -> naruszenie świeżości", any(x.name == "export_fresh" for x in inv.check_all(s)))

    # 8. Zrealizowana strata nie podnosi limitu gotówki (max liczy tylko dodatnie zyski)
    s = StateSnapshot(cash_today_pln=1700.0, cash_yesterday_pln=1582.0,
                      realized_pl_today_pln=-50.0, export_age_hours=1.0)
    # max = 1582 + 0 + 0 + 0 = 1582; 1700 > 1582 -> naruszenie
    check("Strata nie podnosi limitu -> 1700 odrzucone", any(x.name == "cash_upper_bound" for x in inv.check_all(s)))

    # 9. Wiele naruszeń naraz
    s = StateSnapshot(cash_today_pln=-100.0, cash_yesterday_pln=10.0,
                      positions_value_pln=-5.0, export_age_hours=99.0)
    check("Wiele naruszeń wykrytych razem", len(inv.check_all(s)) >= 3)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== invariants.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Twarde inwarianty")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
