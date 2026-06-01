"""
pipeline.py — INTEGRATOR PORSCHE (spina wszystkie moduły w jeden przepływ)
===========================================================================
Bot Porsche — jedno wejście, które łączy 6 modułów w pełną ścieżkę decyzyjną:

    reconcile  ->  invariants  ->  smart_money  ->  sizing (DCM)  ->  gates  ->  order_generation

ZASADA: na każdym etapie, gdy coś się nie zgadza lub czegoś nie wiadomo -> STOP albo skip,
nigdy "zgaduj na korzyść wejścia". Przy prawdziwych pieniądzach milczenie > błędny strzał.

WALUTA KOMUNIKATU: złotówki. Pod spodem liczymy w USD (NYSE), ale wynik prezentujemy w zł.

TRYB DRY-RUN: domyślnie ON. W dry-run pipeline liczy wszystko i zwraca plany, ale oznacza je
flagą dry_run=True (mail dostaje [DRY-RUN] w tytule, brak dyrektyw "na ostro").

Ten moduł NIE pobiera danych z sieci — dostaje je gotowe od marketbot.py (ceny, ATR, radar,
kandydaci, stan konta). Dzięki temu jest w 100% testowalny offline.

Uruchomienie testów: python pipeline.py --selftest
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

# moduły pipeline'u
from cash_management import PositionSizer, DCMConfig, SmartMoneyState as CashSMState
from smart_money_engine import SmartMoneyEngine, SmartMoneyState as EngineSMState
from reconcile import Reconciler, PortfolioState
from safety_gates import SafetyGates, Candidate, GatesConfig
from invariants import Invariants, StateSnapshot
from order_generation import OrderGenerator, OrderConfig, OrderPlan

logger = logging.getLogger("porsche.pipeline")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


# mapowanie stanu z silnika smart money na stan używany przez sizer (te same nazwy, różne Enum)
_SM_MAP = {
    EngineSMState.CONFLUENCE: CashSMState.CONFLUENCE,
    EngineSMState.NEUTRAL: CashSMState.NEUTRAL,
    EngineSMState.UNKNOWN: CashSMState.UNKNOWN,
    EngineSMState.HARD_BLOCK: CashSMState.HARD_BLOCK,
}


@dataclass
class CandidateInput:
    """Dane jednego kandydata, które marketbot.py podaje do pipeline'u."""
    ticker: str
    price_usd: float
    atr_pct: Optional[float] = None          # ATR% jako ułamek (0.06 = 6%)
    atr_usd: Optional[float] = None           # ATR w USD (do liczenia stop loss)
    days_to_earnings: Optional[int] = None
    avg_dollar_volume: Optional[float] = None
    is_smallcap: bool = False
    rsi14: Optional[float] = None
    price_vs_sma20: Optional[float] = None
    fractional_enabled: Optional[bool] = None
    spread_pct: Optional[float] = None
    current_exposure_pln: float = 0.0
    smart_money_html: Optional[str] = None    # do testów / cache (zamiast pobierać)


@dataclass
class PipelineResult:
    ok: bool
    dry_run: bool
    halted: bool = False
    halt_reason: str = ""
    cash_pln: float = 0.0
    equity_pln: float = 0.0
    radar_level: int = 0
    accepted_orders: list = field(default_factory=list)   # list[OrderPlan]
    rejected: list = field(default_factory=list)          # list[(ticker, reason)]
    notes: list = field(default_factory=list)
    reconcile_synced: bool = False                        # True = reconcile przyjął nowy stan z XTB
    actual_positions: list = field(default_factory=list)  # list[Position] z eksportu (źródło prawdy)


class PorschePipeline:
    """Główny integrator. Wejście: stan konta + kandydaci + kontekst makro. Wyjście: PipelineResult."""

    def __init__(self, dry_run: bool = True,
                 dcm_config: Optional[DCMConfig] = None,
                 gates_config: Optional[GatesConfig] = None,
                 order_config: Optional[OrderConfig] = None,
                 max_open_positions: int = 5):
        self.dry_run = dry_run
        self.sizer = PositionSizer(dcm_config)
        self.gates = SafetyGates(gates_config)
        self.invariants = Invariants()
        self.order_gen = OrderGenerator(order_config)
        self.sm_engine = SmartMoneyEngine()
        self.reconciler = Reconciler()
        self.max_open_positions = max_open_positions

    def run(
        self,
        actual_state: PortfolioState,
        expected_json: Optional[dict],
        radar_level: int,
        usd_pln_rate: float,
        candidates: list,                       # list[CandidateInput]
        yesterday_cash_pln: Optional[float] = None,
        deposits_today_pln: float = 0.0,
        export_age_hours: float = 0.0,
        minutes_to_session_open: Optional[int] = None,
        has_unresolved_pending: bool = False,
    ) -> PipelineResult:
        res = PipelineResult(ok=True, dry_run=self.dry_run, radar_level=radar_level)

        # ── ETAP 1: RECONCILE (stan z XTB vs portfolio.json) ──────────────────
        rec = self.reconciler.reconcile(actual_state, expected_json)
        if not rec.consistent:
            res.ok = False; res.halted = True; res.halt_reason = f"RECONCILE: {rec.reason}"
            logger.error(res.halt_reason)
            return res
        res.notes.append(f"reconcile OK: {rec.reason}")
        res.reconcile_synced = rec.synced
        res.actual_positions = list(actual_state.positions)

        cash_pln = actual_state.cash_pln
        # Wartość pozycji liczona po AKTUALNEJ cenie rynkowej (nie po cenie wejścia!).
        # Aktualne ceny są w candidates (price_usd ze skanu yfinance). Mapujemy ticker->cena.
        # Fallback: jeśli brak aktualnej ceny dla pozycji, użyj ceny wejścia (open_price).
        price_by_ticker = {c.ticker: c.price_usd for c in (candidates or []) if getattr(c, "price_usd", 0)}
        positions_value_pln = 0.0
        for p in actual_state.positions:
            cur_price = price_by_ticker.get(p.ticker) or p.open_price
            positions_value_pln += p.volume * cur_price * usd_pln_rate
        equity_pln = cash_pln + positions_value_pln
        res.cash_pln = round(cash_pln, 2); res.equity_pln = round(equity_pln, 2)

        # ── ETAP 2: INVARIANTS (twarde reguły zdrowego rozsądku) ──────────────
        snap = StateSnapshot(
            cash_today_pln=cash_pln,
            cash_yesterday_pln=yesterday_cash_pln if yesterday_cash_pln is not None else cash_pln,
            deposits_today_pln=deposits_today_pln,
            positions_value_pln=positions_value_pln,
            n_positions=len(actual_state.positions),
            min_position_volume=min([p.volume for p in actual_state.positions], default=1.0),
            export_age_hours=export_age_hours,
        )
        violations = self.invariants.check_all(snap)
        if violations:
            res.ok = False; res.halted = True
            res.halt_reason = "INWARIANTY: " + "; ".join(f"{v.name}: {v.detail}" for v in violations)
            logger.error(res.halt_reason)
            return res
        res.notes.append("inwarianty OK")

        # ── ETAP 3-6: STAGED FUNNEL ───────────────────────────────────────────
        # Tanie filtry NAJPIERW (sizing wstępny + 12 bezpieczników bez hard_block),
        # smart money (OpenInsider) TYLKO dla tych, co przeszły — oszczędza zapytania
        # i daje czytelny log (nie "UNKNOWN" dla 100 spółek, których nie rozważamy).
        shortlist = []
        for c in candidates:
            # 4. SIZING wstępny — skalar smart money NEUTRAL (jeszcze nie znamy insiderów)
            sizing = self.sizer.size_position(
                ticker=c.ticker, radar_level=radar_level, atr_pct=c.atr_pct,
                smart_money=CashSMState.NEUTRAL, cash_available_pln=cash_pln,
                equity_total_pln=equity_pln if equity_pln > 0 else cash_pln,
                share_price_usd=c.price_usd, usd_pln_rate=usd_pln_rate,
            )
            if not sizing.accepted:
                res.rejected.append((c.ticker, f"sizing: {sizing.reason}"))
                continue

            # 5. BEZPIECZNIKI (bez hard_block — smart money jeszcze nie sprawdzone)
            cand = Candidate(
                ticker=c.ticker, price_usd=c.price_usd,
                position_value_pln=sizing.target_value_pln,
                cash_available_pln=cash_pln, equity_total_pln=equity_pln if equity_pln > 0 else cash_pln,
                usd_pln_rate=usd_pln_rate, days_to_earnings=c.days_to_earnings,
                avg_dollar_volume=c.avg_dollar_volume, is_smallcap=c.is_smallcap,
                rsi14=c.rsi14, price_vs_sma20=c.price_vs_sma20,
                fractional_enabled=c.fractional_enabled, radar_level=radar_level,
                smart_money_hard_block=False,  # sprawdzimy po smart money
                spread_pct=c.spread_pct, has_unresolved_pending=has_unresolved_pending,
                minutes_to_session_open=minutes_to_session_open,
                current_exposure_pln=c.current_exposure_pln,
            )
            report = self.gates.evaluate(cand)
            if not report.all_passed:
                reasons = "; ".join(f.reason for f in report.failures)
                res.rejected.append((c.ticker, f"bezpieczniki: {reasons}"))
                continue
            shortlist.append(c)

        res.notes.append(f"shortlist po tanich filtrach: {len(shortlist)} z {len(candidates)} kandydatów")

        # ── SMART MONEY tylko dla shortlisty + MULTI-POSITION z trackingiem gotówki ──
        # Każda zaakceptowana pozycja KONSUMUJE gotówkę, więc kolejni kandydaci sizują
        # się z malejącego kapitału (inaczej 5 pozycji × 30% = 150% kapitału — błąd).
        # Limit liczby pozycji: MAX_OPEN_POSITIONS (domyślnie 5).
        remaining_cash_pln = cash_pln
        for c in shortlist:
            # limit liczby otwartych pozycji
            if len(res.accepted_orders) >= self.max_open_positions:
                res.rejected.append((c.ticker, f"limit pozycji osiągnięty ({self.max_open_positions})"))
                continue

            sm_res = self.sm_engine.get_signal(c.ticker, html_override=c.smart_money_html)
            sm_state_cash = _SM_MAP.get(sm_res.state, CashSMState.UNKNOWN)
            res.notes.append(f"{c.ticker}: smart money = {sm_res.state.value} ({sm_res.reason})")

            # HARD_BLOCK: klaster sprzedaży C-Suite -> odrzut finalny
            if sm_res.state == EngineSMState.HARD_BLOCK:
                res.rejected.append((c.ticker, f"smart money HARD_BLOCK: {sm_res.reason}"))
                continue

            # FINALNE SIZING z realnym skalarem smart money i POZOSTAŁĄ gotówką
            sizing = self.sizer.size_position(
                ticker=c.ticker, radar_level=radar_level, atr_pct=c.atr_pct,
                smart_money=sm_state_cash, cash_available_pln=remaining_cash_pln,
                equity_total_pln=equity_pln if equity_pln > 0 else cash_pln,
                share_price_usd=c.price_usd, usd_pln_rate=usd_pln_rate,
            )
            if not sizing.accepted:
                res.rejected.append((c.ticker, f"sizing (po smart money): {sizing.reason}"))
                continue

            # 6. ORDER GENERATION (Buy + Sell Stop, po złotówkowemu)
            plan = self.order_gen.build_plan(
                ticker=c.ticker, shares=sizing.shares, entry_price_usd=c.price_usd,
                usd_pln_rate=usd_pln_rate, atr_usd=c.atr_usd,
            )
            res.accepted_orders.append(plan)
            remaining_cash_pln -= plan.value_pln   # KONSUMPCJA gotówki
            logger.info("[%s] ZAAKCEPTOWANE: %.4f akcji, ~%.0f zł, SL %.2f USD | pozostała gotówka: %.0f zł",
                        c.ticker, plan.shares, plan.value_pln, plan.stop_price_usd, remaining_cash_pln)

        return res


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST — pełna ścieżka end-to-end na danych syntetycznych
# ─────────────────────────────────────────────────────────────────────────────
_HTML_CONFLUENCE = """
<table class="tinytable">
<tr><th>X</th><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th>
<th>Title</th><th>Trade Type</th><th>Price</th></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Smith John</td>
<td>CEO</td><td>P - Purchase</td><td>$140</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Doe Jane</td>
<td>CFO</td><td>P - Purchase</td><td>$141</td></tr>
</table>
"""

_HTML_HARDBLOCK = """
<table class="tinytable">
<tr><th>X</th><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th>
<th>Title</th><th>Trade Type</th><th>Price</th></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>BAD</td><td>Boss One</td>
<td>CEO</td><td>S - Sale</td><td>$50</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>BAD</td><td>Boss Two</td>
<td>CFO</td><td>S - Sale</td><td>$51</td></tr>
</table>
"""

# Poprawna strona OpenInsider BEZ transakcji -> NEUTRAL (nie UNKNOWN).
_HTML_NEUTRAL = "<html><title>SEC Form 4 Insider Trading Screener - openinsider</title><body>no rows</body></html>"


def _run_selftest() -> int:
    print("=== SELFTEST pipeline (end-to-end, offline) ===")
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    pipe = PorschePipeline(dry_run=True)
    USD_PLN = 4.0

    # Stan: świeży depozyt 1582 zł, brak pozycji, pierwszy run
    state = PortfolioState(cash_pln=1582.0, positions=[])

    # Kandydat dobry: NVDA, confluence insiderów, wszystkie wskaźniki zdrowe
    good = CandidateInput(
        ticker="NVDA", price_usd=140.0, atr_pct=0.04, atr_usd=3.0,
        days_to_earnings=20, avg_dollar_volume=5e9, is_smallcap=False,
        rsi14=58.0, price_vs_sma20=1.05, fractional_enabled=True, spread_pct=0.0008,
        smart_money_html=_HTML_CONFLUENCE,
    )

    # 1. Pełna ścieżka — kandydat przechodzi
    r = pipe.run(actual_state=state, expected_json=None, radar_level=0,
                 usd_pln_rate=USD_PLN, candidates=[good],
                 yesterday_cash_pln=1582.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("Pipeline nie zatrzymany", not r.halted)
    check("Tryb dry-run aktywny", r.dry_run)
    check("Jeden zaakceptowany order", len(r.accepted_orders) == 1)
    check("Order to NVDA z dyrektywami Buy+SellStop",
          r.accepted_orders and r.accepted_orders[0].ticker == "NVDA"
          and any("SELL STOP" in d for d in r.accepted_orders[0].directives))
    check("Wartość pozycji w zł > 0", r.accepted_orders[0].value_pln > 0)

    # 2. HARD_BLOCK — kandydat odrzucony przez smart money / sizing
    bad = CandidateInput(
        ticker="BAD", price_usd=50.0, atr_pct=0.03, atr_usd=1.5,
        days_to_earnings=20, avg_dollar_volume=5e9, rsi14=55.0, price_vs_sma20=1.0,
        fractional_enabled=True, spread_pct=0.001, smart_money_html=_HTML_HARDBLOCK,
    )
    r = pipe.run(actual_state=state, expected_json=None, radar_level=0,
                 usd_pln_rate=USD_PLN, candidates=[bad],
                 yesterday_cash_pln=1582.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("HARD_BLOCK: zero zaakceptowanych", len(r.accepted_orders) == 0)
    check("HARD_BLOCK: kandydat odrzucony", len(r.rejected) == 1)

    # 3. RECONCILE halt — portfolio.json mówi co innego niż stan z XTB
    expected = PortfolioState(cash_pln=1000.0, positions=[]).to_json()  # bot myślał 1000, w XTB 1582
    r = pipe.run(actual_state=state, expected_json=expected, radar_level=0,
                 usd_pln_rate=USD_PLN, candidates=[good],
                 yesterday_cash_pln=1000.0, export_age_hours=2.0)
    check("Reconcile rozjazd -> HALT", r.halted and "RECONCILE" in r.halt_reason)

    # 4. INVARIANT halt — gotówka zawyżona względem wczoraj bez depozytu
    state_infl = PortfolioState(cash_pln=5000.0, positions=[])
    r = pipe.run(actual_state=state_infl, expected_json=None, radar_level=0,
                 usd_pln_rate=USD_PLN, candidates=[good],
                 yesterday_cash_pln=1582.0, deposits_today_pln=0.0, export_age_hours=2.0)
    check("Zawyżona gotówka -> HALT inwariant", r.halted and "INWARIANT" in r.halt_reason)

    # 5. Radar 3/3 -> wszystko odrzucone (zakaz wejść), ale NIE halt
    r = pipe.run(actual_state=state, expected_json=None, radar_level=3,
                 usd_pln_rate=USD_PLN, candidates=[good],
                 yesterday_cash_pln=1582.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("Radar 3/3: brak akceptacji, bez halt", (not r.halted) and len(r.accepted_orders) == 0)

    # 6. Earnings za 1 dzień -> odrzut przez gates
    soon = CandidateInput(
        ticker="ERN", price_usd=100.0, atr_pct=0.03, atr_usd=2.0,
        days_to_earnings=1, avg_dollar_volume=5e9, rsi14=55.0, price_vs_sma20=1.0,
        fractional_enabled=True, spread_pct=0.001, smart_money_html=_HTML_CONFLUENCE.replace("NVDA","ERN"),
    )
    r = pipe.run(actual_state=state, expected_json=None, radar_level=0,
                 usd_pln_rate=USD_PLN, candidates=[soon],
                 yesterday_cash_pln=1582.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("Earnings T+1 -> odrzut (nie order)", len(r.accepted_orders) == 0 and len(r.rejected) == 1)

    # ── MULTI-POSITION (nowe testy) ───────────────────────────────────────────
    def _good(tk, price=100.0):
        return CandidateInput(
            ticker=tk, price_usd=price, atr_pct=0.03, atr_usd=2.0,
            days_to_earnings=20, avg_dollar_volume=5e9, is_smallcap=False,
            rsi14=55.0, price_vs_sma20=1.0, fractional_enabled=True, spread_pct=0.001,
            smart_money_html=_HTML_NEUTRAL,
        )

    # 7. Multi-accept: 3 zdrowych kandydatów -> wszyscy 3 zaakceptowani (MAX=5)
    state3 = PortfolioState(cash_pln=1582.0, positions=[])
    three = [_good("AAA", 100), _good("BBB", 120), _good("CCC", 90)]
    r = pipe.run(actual_state=state3, expected_json=None, radar_level=0,
                 usd_pln_rate=USD_PLN, candidates=three,
                 yesterday_cash_pln=1582.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("Multi-accept: 3 zdrowych -> 3 zaakceptowanych", len(r.accepted_orders) == 3)
    check("Multi-accept: suma pozycji <= gotówka (tracking działa)",
          sum(o.value_pln for o in r.accepted_orders) <= 1582.0 + 0.01)

    # 8. Cash exhaustion: mała gotówka, kolejni kandydaci dostają "brak gotówki"
    state_small = PortfolioState(cash_pln=300.0, positions=[])
    many = [_good(f"T{i}", 100) for i in range(6)]
    r = pipe.run(actual_state=state_small, expected_json=None, radar_level=0,
                 usd_pln_rate=USD_PLN, candidates=many,
                 yesterday_cash_pln=300.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("Cash exhaustion: nie przekracza dostępnej gotówki",
          sum(o.value_pln for o in r.accepted_orders) <= 300.0 + 0.01)

    # 9. Limit pozycji: MAX_OPEN_POSITIONS=2, pięciu dobrych -> tylko 2 zaakceptowanych
    pipe2 = PorschePipeline(dry_run=True, max_open_positions=2)
    five = [_good(f"P{i}", 100) for i in range(5)]
    r = pipe2.run(actual_state=PortfolioState(cash_pln=1582.0, positions=[]), expected_json=None,
                  radar_level=0, usd_pln_rate=USD_PLN, candidates=five,
                  yesterday_cash_pln=1582.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("Limit pozycji: MAX=2 -> dokładnie 2 zaakceptowanych", len(r.accepted_orders) == 2)

    # 10. REGRESJA "41 pozycji za 11k": dużo dobrych kandydatów NIGDY nie przekracza
    #     ani limitu pozycji (5), ani kapitału (1582 zł). Ten test złapałby stary plik.
    pipe_def = PorschePipeline(dry_run=True)  # domyślny MAX=5
    forty = [_good(f"K{i}", 100) for i in range(40)]
    r = pipe_def.run(actual_state=PortfolioState(cash_pln=1582.0, positions=[]), expected_json=None,
                     radar_level=0, usd_pln_rate=USD_PLN, candidates=forty,
                     yesterday_cash_pln=1582.0, export_age_hours=2.0, minutes_to_session_open=45)
    check("REGRESJA: 40 kandydatów -> max 5 pozycji (limit trzyma)", len(r.accepted_orders) <= 5)
    check("REGRESJA: suma NIGDY > kapitał (nie 11k za 1.5k konta)",
          sum(o.value_pln for o in r.accepted_orders) <= 1582.0 + 0.01)

    # ── REGRESJA EQUITY: liczone po AKTUALNEJ cenie rynkowej, NIE po cenie wejścia ──
    from reconcile import PortfolioState as _PS, Position as _Pos
    eq_state = _PS(cash_pln=55.27, positions=[_Pos("TSM", 0.3319, 423.68)])  # wejście 423.68
    eq_cand = CandidateInput(ticker="TSM", price_usd=460.0, atr_pct=0.03, atr_usd=5.0,
                             days_to_earnings=30, avg_dollar_volume=5e9, rsi14=60.0,
                             price_vs_sma20=1.05, fractional_enabled=True)  # rynek 460
    eq_r = PorschePipeline(dry_run=True).run(
        actual_state=eq_state,
        expected_json={"cash_pln": 55.27, "positions": [{"ticker": "TSM", "volume": 0.3319, "open_price": 423.68}]},
        radar_level=0, usd_pln_rate=3.63, candidates=[eq_cand])
    eq_current = round(0.3319 * 460.0 * 3.63 + 55.27, 2)   # po aktualnej cenie
    eq_entry = round(0.3319 * 423.68 * 3.63 + 55.27, 2)    # po cenie wejścia (stary bug)
    check("EQUITY: liczone po AKTUALNEJ cenie rynkowej (nie wejścia)",
          abs(eq_r.equity_pln - eq_current) < 1.0)
    check("EQUITY: NIE liczy po cenie wejścia (stary bug nie wrócił)",
          abs(eq_r.equity_pln - eq_entry) > 1.0)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== pipeline.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Integrator pipeline")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
