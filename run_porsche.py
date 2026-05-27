"""
run_porsche.py — JEDNO WEJŚCIE DO CAŁEGO PIPELINE'U
====================================================
Bot Porsche — odpalasz JEDNĄ komendą. Czyta Twój eksport XTB (.xlsx), przepuszcza przez
cały pipeline (reconcile -> invariants -> smart money -> sizing -> bezpieczniki -> zlecenia)
i wypisuje czytelny raport PO ZŁOTÓWKOWEMU.

TRYB DOMYŚLNY: --dry-run (bot NICZEGO nie zaleca "na ostro", tylko pokazuje co BY zrobił).
Żeby zobaczyć realne dyrektywy: dodaj --live (ale dopiero gdy ścieżka przejdzie testy!).

UŻYCIE:
    python run_porsche.py --export sciezka/do/pliku.xlsx
    python run_porsche.py --export plik.xlsx --radar 0 --fx 4.02
    python run_porsche.py --selftest         # test bez pliku (dane syntetyczne)

WAŻNE: ten skrypt to SZKIELET integracyjny. Kandydatów (jakie spółki rozważyć) i ich
wskaźniki (ATR, RSI, płynność, earnings) w pełnym bocie dostarcza marketbot.py / yfinance.
Tutaj, dla testu na żywo stanu konta, możesz podać kandydatów ręcznie w sekcji CANDIDATES
albo zostawić pusto — wtedy skrypt tylko zweryfikuje odczyt konta + reconcile + inwarianty.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from reconcile import Reconciler, PortfolioState
from pipeline import PorschePipeline, CandidateInput

logger = logging.getLogger("porsche.run")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


def _print_header(title: str):
    print("\n" + "═" * 60)
    print(f"  {title}")
    print("═" * 60)


def run(export_path: str, portfolio_json: str = "portfolio.json",
        radar_level: int = 0, fx: float = 4.0, dry_run: bool = True,
        candidates: Optional[list] = None) -> int:
    _print_header("BOT PORSCHE — URUCHOMIENIE" + (" [DRY-RUN]" if dry_run else " [LIVE]"))

    # ── 1. Odczyt stanu z eksportu XTB ────────────────────────────────────────
    reconciler = Reconciler()
    try:
        actual = reconciler.build_actual_state(export_path)
    except FileNotFoundError:
        print(f"\n❌ BŁĄD: nie znaleziono pliku eksportu: {export_path}")
        print("   Sprawdź ścieżkę albo pobierz świeży eksport z XTB i wskaż go przez --export.")
        return 1
    except Exception as e:
        print(f"\n❌ BŁĄD odczytu pliku XTB: {e}")
        print("   Możliwe: uszkodzony plik / zły format. Pobierz eksport ponownie z platformy WEB.")
        return 1

    print(f"\n📂 Odczytano eksport: {export_path}")
    print(f"   • Gotówka: {actual.cash_pln:,.2f} zł")
    print(f"   • Otwarte pozycje: {len(actual.positions)}")
    for p in actual.positions:
        print(f"       - {p.ticker}: {p.volume:.4f} szt @ {p.open_price:.2f} USD")

    # ── 2. Wczytanie portfolio.json (cache) jeśli istnieje ────────────────────
    expected_json = None
    pj = Path(portfolio_json)
    if pj.exists():
        try:
            expected_json = json.loads(pj.read_text(encoding="utf-8"))
            print(f"\n💾 Wczytano poprzedni stan z {portfolio_json} (do porównania).")
        except Exception as e:
            print(f"\n⚠️  portfolio.json nieczytelny ({e}) — traktuję jak pierwszy run.")
    else:
        print(f"\n💾 Brak {portfolio_json} — pierwszy run, przyjmuję stan z XTB jako bazę.")

    # ── 3. Pipeline ───────────────────────────────────────────────────────────
    pipe = PorschePipeline(dry_run=dry_run)
    yest = expected_json.get("cash_pln") if expected_json else actual.cash_pln
    result = pipe.run(
        actual_state=actual, expected_json=expected_json, radar_level=radar_level,
        usd_pln_rate=fx, candidates=candidates or [],
        yesterday_cash_pln=yest, export_age_hours=1.0, minutes_to_session_open=45,
    )

    # ── 4. Raport ──────────────────────────────────────────────────────────────
    _print_header("WYNIK PIPELINE")
    if result.halted:
        print(f"\n🛑 BOT ZATRZYMANY (Hard Halt):")
        print(f"   {result.halt_reason}")
        print(f"\n   To jest CELOWE — bot nie działa na niespójnym stanie.")
        print(f"   Sprawdź eksport XTB i portfolio.json, popraw rozjazd, uruchom ponownie.")
        return 2

    print(f"\n✅ Stan spójny. Gotówka {result.cash_pln:,.2f} zł, equity {result.equity_pln:,.2f} zł, "
          f"radar {result.radar_level}/3.")

    if not candidates:
        print("\nℹ️  Nie podano kandydatów do analizy — to był test odczytu konta + reconcile + inwariantów.")
        print("   Wszystko przeszło. W pełnym bocie kandydatów dostarcza marketbot.py (skan rynku).")
        _maybe_save_portfolio(actual, portfolio_json, dry_run)
        return 0

    if result.accepted_orders:
        _print_header(f"PROPOZYCJE ({len(result.accepted_orders)})" + (" — [DRY-RUN, nie wykonuj]" if dry_run else ""))
        from order_generation import OrderGenerator
        gen = OrderGenerator()
        for plan in result.accepted_orders:
            print(f"\n{'─'*60}")
            print(gen.render_text(plan))
    else:
        print("\nℹ️  Żadna spółka nie przeszła wszystkich filtrów dziś. Bot nie proponuje zakupu.")

    if result.rejected:
        _print_header(f"ODRZUCONE ({len(result.rejected)})")
        for tk, reason in result.rejected:
            print(f"   • {tk}: {reason}")

    _maybe_save_portfolio(actual, portfolio_json, dry_run)
    return 0


def _maybe_save_portfolio(state: PortfolioState, path: str, dry_run: bool):
    """Zapisuje aktualny stan do portfolio.json (snapshot OBSERWOWANY z XTB)."""
    if dry_run:
        print(f"\n💾 [DRY-RUN] NIE zapisuję portfolio.json (tryb testowy).")
        return
    try:
        Path(path).write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n💾 Zapisano aktualny stan do {path}.")
    except Exception as e:
        print(f"\n⚠️  Nie udało się zapisać {path}: {e}")


def _selftest() -> int:
    """Test bez pliku — syntetyczny stan."""
    _print_header("SELFTEST run_porsche (bez pliku, dane syntetyczne)")
    pipe = PorschePipeline(dry_run=True)
    state = PortfolioState(cash_pln=1582.0, positions=[])
    cand = CandidateInput(
        ticker="NVDA", price_usd=140.0, atr_pct=0.04, atr_usd=3.0, days_to_earnings=20,
        avg_dollar_volume=5e9, rsi14=58.0, price_vs_sma20=1.05, fractional_enabled=True,
        spread_pct=0.0008,
        smart_money_html="""<table class="tinytable">
<tr><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th><th>Title</th><th>Trade Type</th></tr>
<tr><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Smith</td><td>CEO</td><td>P - Purchase</td></tr>
<tr><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Doe</td><td>CFO</td><td>P - Purchase</td></tr>
</table>""",
    )
    r = pipe.run(actual_state=state, expected_json=None, radar_level=0, usd_pln_rate=4.0,
                 candidates=[cand], yesterday_cash_pln=1582.0, export_age_hours=1.0,
                 minutes_to_session_open=45)
    ok = (not r.halted) and len(r.accepted_orders) == 1
    print(f"\n{'✅ SELFTEST OK' if ok else '❌ SELFTEST FAIL'}: "
          f"halted={r.halted}, zaakceptowane={len(r.accepted_orders)}")
    if r.accepted_orders:
        from order_generation import OrderGenerator
        print("\nPrzykładowa dyrektywa:")
        print(OrderGenerator().render_text(r.accepted_orders[0]))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — jedno wejście do pipeline'u")
    ap.add_argument("--export", help="ścieżka do eksportu XTB (.xlsx)")
    ap.add_argument("--portfolio", default="portfolio.json", help="plik stanu (cache)")
    ap.add_argument("--radar", type=int, default=0, help="poziom radaru makro 0-3")
    ap.add_argument("--fx", type=float, default=4.0, help="kurs USD/PLN")
    ap.add_argument("--live", action="store_true", help="tryb LIVE (domyślnie dry-run)")
    ap.add_argument("--selftest", action="store_true", help="test bez pliku")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.export:
        ap.print_help()
        print("\n⚠️  Podaj --export sciezka/do/pliku.xlsx (albo --selftest do testu bez pliku).")
        return 1
    return run(args.export, args.portfolio, args.radar, args.fx, dry_run=not args.live)


if __name__ == "__main__":
    raise SystemExit(main())
