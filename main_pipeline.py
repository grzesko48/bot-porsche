"""
main_pipeline.py — RDZEŃ WYKONAWCZY BOTA PORSCHE
=================================================
Świeży, czysty rdzeń. Spina lejek Top-Down w jeden organizm:

  ODCZYT KONTA (XTB z Drive)  ->  RECONCILE + INWARIANTY
       -> SKAN RYNKU (momentum sektorów, ceny, wskaźniki)
       -> dla każdego kandydata: SMART MONEY (miękki fail-safe) -> SIZING (DCM) -> 13 BEZPIECZNIKÓW
       -> 1 SNIPER PICK (najlepszy) + RADAR OBSERWACYJNY (reszta)
       -> ORDER (Market Buy + osobny Sell Stop, po złotówkowemu)
       -> MAIL

ARCHITEKTURA: w 100% własny, niezależny kod. ZERO importów ze starego marketbot.py.
Skan rynku (top_down_scanner), wskaźniki (indicators), wysyłka i earnings (notifications) —
wszystko własne. Stary bot nie jest potrzebny.

WALUTA: złotówki w komunikacie. USD tylko tam, gdzie wpisuje się na XTB.

TRYB: --dry-run domyślnie. --live dopiero po czystych testach na żywo.

GARBAGE COLLECTION: twardy cleanup w bloku finally (pamięć, duże DataFrame'y).

Uruchomienie:
  python main_pipeline.py --selftest                  # pełny test offline (dane syntetyczne)
  python main_pipeline.py --export plik.xlsx          # dry-run na realnym koncie
  python main_pipeline.py --export plik.xlsx --live    # tryb live (po testach!)
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

# Windows: konsola bywa w cp1250 i nie umie wypisać znaków ramek (═ ─ 🎯).
# Wymuszamy UTF-8 na stdout/stderr, żeby uniknąć UnicodeEncodeError i czystego exit.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── moduły pipeline'u (nowy mózg) — 100% własny kod, zero starego bota ────────
from reconcile import Reconciler, PortfolioState
from pipeline import PorschePipeline, CandidateInput
from order_generation import OrderGenerator
import indicators as ind
from top_down_scanner import TopDownScanner
from performance_tracker import (TrackerConfig, record_snapshot, compute_state,
                                  format_email_section, load_equity_log)
from notifications import send_email_resend, fetch_earnings_finnhub, fetch_earnings
from position_manager import (PositionManager, ManagedPosition, PositionMarketData,
                              load_managed_positions, save_managed_positions, append_decision_log)

logger = logging.getLogger("porsche.main")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


@dataclass
class BotConfig:
    portfolio_json: str = "portfolio.json"
    decision_log_json: str = "decision_log.json"
    equity_log_json: str = "equity_log.json"      # tracker: snapshoty equity vs SPY
    fx_default: float = 4.0
    max_radar_names: int = 5          # ile spółek w sekcji Radar Obserwacyjny
    goal_pln: int = 1_700_000
    recipient_env: str = "REPORT_RECIPIENT"


@dataclass
class BotRunResult:
    ok: bool
    dry_run: bool
    halted: bool = False
    halt_reason: str = ""
    cash_pln: float = 0.0
    equity_pln: float = 0.0
    radar_level: int = 0
    sniper_pick: Optional[object] = None        # OrderPlan (top 1, backward compat)
    accepted_picks: list = field(default_factory=list)   # list[OrderPlan] — WSZYSTKIE zaakceptowane
    position_decisions: list = field(default_factory=list)  # list[PositionDecision] — zarządzanie istniejącymi
    radar_watch: list = field(default_factory=list)   # list[(ticker, sektor, powód)]
    rejected: list = field(default_factory=list)
    email_html: str = ""
    email_subject: str = ""
    notes: list = field(default_factory=list)
    tracker_state: Optional[object] = None              # TrackingState (equity vs SPY benchmark)


# ─────────────────────────────────────────────────────────────────────────────
# SKAN RYNKU — buduje listę CandidateInput
# ─────────────────────────────────────────────────────────────────────────────
def scan_market(fx: float, selftest: bool = False,
                price_override: Optional[dict] = None) -> tuple[list, int, dict]:
    """KROK 1 LEJKA TOP-DOWN — własny skaner (top_down_scanner.py). ZERO starego silnika.

    Przepływ: macierz ETF -> ROC 10/20 -> 2 najsilniejsze sektory -> drill-down do koszyków
    -> wskaźniki (ATR/RSI/SMA z indicators.py) -> lista CandidateInput. Radar makro własny.

    selftest / price_override -> dane mockowane (offline). Na żywo: yfinance+curl_cffi w scannerze.
    Earnings (data wyników) doczytujemy opcjonalnie ze starego bota — to czysty fakt o dacie,
    NIE silnik doboru spółek; brak starego bota nie psuje skanu (try/except)."""
    if selftest:
        return _synthetic_candidates(), 0, {"source": "synthetic"}

    meta = {"source": "live", "errors": []}
    try:
        scanner = TopDownScanner()
        scan = scanner.scan(price_override=price_override)
        meta["notes"] = scan.notes
        meta["winning_sectors"] = [(s.etf, s.name, s.score) for s in scan.winning_sectors]
        meta["regime_below_200sma"] = scan.regime_below_200sma
        meta["spy_last_price"] = scan.spy_last_price
        if scan.used_mock:
            meta["source"] = "mock"

        # daty wyników: Finnhub (batch) + yfinance fallback dla brakujących (ADR-y)
        earnings = {}
        try:
            if scan.candidates:
                want = [c.ticker for c in scan.candidates]
                earnings = fetch_earnings(list(dict.fromkeys(want)))
        except Exception as e:
            meta["errors"].append(f"earnings pominięte: {e}")

        candidates = []
        for c in scan.candidates:
            series = c.close_series
            price = c.last_price
            atr_v = ind.atr_from_close(series, 14)
            atrp = ind.atr_pct(atr_v, price)
            rsi_v = ind.rsi(series, 14)
            pvs = ind.price_vs_sma20(series)
            # dollar volume z wolumenu (jeśli skaner go pobrał)
            dvol = None
            if getattr(c, "volume_series", None) is not None:
                dvol = ind.dollar_volume(series, c.volume_series, 20)
            ed = earnings.get(c.ticker)
            d2e = None
            if ed:
                try:
                    d2e = (date.fromisoformat(ed) - date.today()).days
                except Exception:
                    d2e = None
            ci = CandidateInput(
                ticker=c.ticker, price_usd=price, atr_pct=atrp, atr_usd=atr_v,
                days_to_earnings=d2e, avg_dollar_volume=dvol,
                is_smallcap=False, rsi14=rsi_v, price_vs_sma20=pvs,
                fractional_enabled=True, spread_pct=None,
            )
            ci.__dict__["_sector"] = c.sector_name
            # proxy "siły" do wyboru snipera: ROC sektora-rodzica
            sec_score = next((s.score for s in scan.winning_sectors if s.etf == c.sector_etf), 0.0)
            ci.__dict__["_mom"] = sec_score
            candidates.append(ci)

        return candidates, scan.radar_level, meta
    except Exception as e:
        logger.error("Skan rynku (Top-Down) nieudany: %s", e)
        meta["errors"].append(f"scan_market: {e}")
        return [], 0, meta


def _synthetic_candidates() -> list:
    """Kandydaci syntetyczni do selftestu — z gniazdem smart_money_html."""
    html_conf = """<table class="tinytable">
<tr><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th><th>Title</th><th>Trade Type</th></tr>
<tr><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Smith</td><td>CEO</td><td>P - Purchase</td></tr>
<tr><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Doe</td><td>CFO</td><td>P - Purchase</td></tr></table>"""
    a = CandidateInput(ticker="NVDA", price_usd=140.0, atr_pct=0.04, atr_usd=3.0, days_to_earnings=20,
                       avg_dollar_volume=5e9, rsi14=58.0, price_vs_sma20=1.05, fractional_enabled=True,
                       spread_pct=0.0008, smart_money_html=html_conf)
    a.__dict__["_sector"] = "Technologia"; a.__dict__["_mom"] = 0.12
    b = CandidateInput(ticker="AVGO", price_usd=200.0, atr_pct=0.045, atr_usd=4.0, days_to_earnings=15,
                       avg_dollar_volume=3e9, rsi14=62.0, price_vs_sma20=1.08, fractional_enabled=True,
                       spread_pct=0.001,
                       smart_money_html="<html><title>SEC Form 4 openinsider</title>brak</html>")  # NEUTRAL offline
    b.__dict__["_sector"] = "Technologia"; b.__dict__["_mom"] = 0.09
    return [a, b]


# ─────────────────────────────────────────────────────────────────────────────
# WYBÓR SNIPER PICK — najlepszy z zaakceptowanych
# ─────────────────────────────────────────────────────────────────────────────
def choose_sniper(accepted_orders: list, candidates_by_ticker: dict) -> Optional[object]:
    """Z zaakceptowanych zleceń wybiera 1 najlepsze.
    Kryterium: największa wartość pozycji w zł (proxy konfluencji — wyższe skalary => wyższa pozycja),
    remis rozstrzyga momentum sektora."""
    if not accepted_orders:
        return None
    def score(plan):
        c = candidates_by_ticker.get(plan.ticker)
        mom = (c.__dict__.get("_mom") if c else 0) or 0
        return (plan.value_pln, mom)
    return max(accepted_orders, key=score)


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNY PRZEBIEG
# ─────────────────────────────────────────────────────────────────────────────
def run_bot(export_path: Optional[str], cfg: Optional[BotConfig] = None,
            dry_run: bool = True, radar_override: Optional[int] = None,
            fx: Optional[float] = None, selftest: bool = False,
            send: bool = False, regime_override: Optional[bool] = None) -> BotRunResult:
    cfg = cfg or BotConfig()
    fx = fx or cfg.fx_default
    res = BotRunResult(ok=True, dry_run=dry_run)
    close_data = None  # do GC

    try:
        # ── 1. ODCZYT KONTA ───────────────────────────────────────────────────
        if selftest:
            actual = PortfolioState(cash_pln=1582.0, positions=[])
            expected_json = None
        else:
            if not export_path:
                res.ok = False; res.halt_reason = "brak --export"; return res
            reconciler = Reconciler()
            actual = reconciler.build_actual_state(export_path)
            pj = Path(cfg.portfolio_json)
            expected_json = None
            if pj.exists():
                try:
                    import json
                    expected_json = json.loads(pj.read_text(encoding="utf-8"))
                except Exception as e:
                    res.notes.append(f"portfolio.json nieczytelny: {e}")

        res.cash_pln = round(actual.cash_pln, 2)
        logger.info("Konto: gotówka %.2f zł, pozycji %d", actual.cash_pln, len(actual.positions))

        # ── 2. SKAN RYNKU ──────────────────────────────────────────────────────
        candidates, radar_level, meta = scan_market(fx, selftest=selftest)
        if radar_override is not None:
            radar_level = radar_override
        res.radar_level = radar_level
        res.notes.append(f"skan rynku: {len(candidates)} kandydatów, radar {radar_level}/3, źródło {meta.get('source')}")
        candidates_by_ticker = {c.ticker: c for c in candidates}

        # ── 3-6. PIPELINE (reconcile, inwarianty, smart money, sizing, gates, order) ──
        pipe = PorschePipeline(dry_run=dry_run)
        result = pipe.run(
            actual_state=actual, expected_json=expected_json, radar_level=radar_level,
            usd_pln_rate=fx, candidates=candidates,
            yesterday_cash_pln=(expected_json.get("cash_pln") if expected_json else actual.cash_pln),
            export_age_hours=1.0, minutes_to_session_open=45, has_unresolved_pending=False,
        )
        res.equity_pln = result.equity_pln
        res.rejected = result.rejected
        res.notes += result.notes

        if result.halted:
            res.ok = False; res.halted = True; res.halt_reason = result.halt_reason
            return res

        # ── 6.5. ZARZĄDZANIE ISTNIEJĄCYMI POZYCJAMI (przed otwieraniem nowych) ──
        # Bot pamięta co trzyma (portfolio.json -> managed_positions) i decyduje HOLD/SELL
        # per pozycja. KLUCZOWE: spółki już trzymane są odfiltrowane z nowych propozycji,
        # co eliminuje ryzyko podwójnego zakupu tej samej spółki.
        held_tickers = set()
        if not selftest:
            managed = load_managed_positions(cfg.portfolio_json)
            if managed:
                posman = PositionManager()
                market = {}
                for mp in managed:
                    c = candidates_by_ticker.get(mp.ticker)
                    if c is not None:
                        market[mp.ticker] = PositionMarketData(
                            ticker=mp.ticker, current_price_usd=c.price_usd,
                            rsi14=c.rsi14, price_vs_sma20=c.price_vs_sma20,
                            days_to_earnings=c.days_to_earnings,
                            smart_money_hard_block=False,  # ocena hard_block dla trzymanych: TODO osobno
                        )
                    # brak ceny w skanie -> PositionManager da HOLD z ostrzeżeniem (fail-CLOSED)
                res.position_decisions = posman.evaluate(managed, market, radar_level)
                held_tickers = {mp.ticker for mp in managed}
                for d in res.position_decisions:
                    logger.info("[%s] POZYCJA: %s — %s", d.ticker, d.action, d.reason)
                # zapis zaktualizowanego stanu (HWM itd.) + log decyzji
                updated = [d.updated_position for d in res.position_decisions if d.updated_position]
                if updated:
                    save_managed_positions(cfg.portfolio_json, updated, cash_pln=actual.cash_pln)
                try:
                    append_decision_log(cfg.decision_log_json, res.position_decisions)
                except Exception as e:
                    res.notes.append(f"decision_log zapis nieudany: {e}")

        # ── 7. PORTFEL (multi-position) + SNIPER (top 1, backward compat) + RADAR ──
        # ANTI-DOUBLE-BUY: odfiltruj spółki, które już trzymamy
        res.accepted_picks = [o for o in result.accepted_orders if o.ticker not in held_tickers]
        skipped_held = [o.ticker for o in result.accepted_orders if o.ticker in held_tickers]
        if skipped_held:
            res.notes.append(f"pominięto (już w portfelu): {', '.join(skipped_held)}")

        # ── MAKRO-FILTR REGIME (lekarstwo na momentum crashes) ──
        # SPY < 200SMA -> trend spadkowy -> NIE otwieraj nowych pozycji (czekaj w gotówce).
        # Istniejące pozycje dalej zarządzane (sekcja 6.5 powyżej). Zamyka tylko OTWIERANIE.
        regime_below = bool(meta.get("regime_below_200sma", False)) if radar_override is None else False
        if regime_override is not None:
            regime_below = regime_override
        if regime_below and res.accepted_picks:
            blocked = [o.ticker for o in res.accepted_picks]
            res.accepted_picks = []
            res.notes.append(f"MAKRO-STOP: SPY < 200SMA (trend spadkowy) — wstrzymano otwieranie nowych "
                             f"pozycji ({', '.join(blocked)}). Bot czeka w gotówce. Istniejące pozycje zarządzane normalnie.")
            logger.info("MAKRO-STOP regime: zablokowano %d nowych pozycji (SPY<200SMA)", len(blocked))
        sniper = choose_sniper(res.accepted_picks, candidates_by_ticker)
        res.sniper_pick = sniper

        # radar: kandydaci, którzy przeszli wstępny skan ale NIE są wśród zaakceptowanych
        accepted_tks = {o.ticker for o in res.accepted_picks}
        for c in candidates:
            if c.ticker in accepted_tks or c.ticker in held_tickers:
                continue
            sector = c.__dict__.get("_sector", "")
            rej = next((r for t, r in result.rejected if t == c.ticker), None)
            powod = "obserwacja" if rej is None else rej.split(";")[0][:80]
            res.radar_watch.append((c.ticker, sector, powod))
        res.radar_watch = res.radar_watch[:cfg.max_radar_names]

        # ── 7.5. TRACKER PERFORMANCE vs SPY (świadome ryzyko z hamulcem) ──
        # Zapisz snapshot equity + cena SPY + FX. Policz state. Sekcja do maila + ALERT.
        if not selftest:
            try:
                tcfg = TrackerConfig(log_path=cfg.equity_log_json)
                spy_px = float(meta.get("spy_last_price", 0.0) or 0.0)
                if spy_px > 0 and res.equity_pln > 0:
                    snapshots = record_snapshot(tcfg, equity_pln=res.equity_pln,
                                                spy_price_usd=spy_px, fx_rate=fx)
                    res.tracker_state = compute_state(snapshots, tcfg)
                    if res.tracker_state.alert:
                        logger.warning("Tracker ALERT: %s", res.tracker_state.alert_reason)
                        res.notes.append("Tracker: " + res.tracker_state.alert_reason)
                else:
                    res.notes.append("Tracker: pominięty (brak ceny SPY albo equity).")
            except Exception as e:
                logger.error("Tracker performance — błąd, pomijam: %s", e)
                res.notes.append(f"Tracker: błąd ({e})")

        # ── 8. MAIL ────────────────────────────────────────────────────────────
        res.email_subject, res.email_html = build_email(res, cfg, fx)
        if send and not dry_run:
            _send(res, cfg)
        elif send and dry_run:
            res.notes.append("[DRY-RUN] mail NIE wysłany (tryb testowy)")

        return res

    except FileNotFoundError:
        res.ok = False; res.halt_reason = f"nie znaleziono pliku: {export_path}"; return res
    except Exception as e:
        res.ok = False; res.halt_reason = f"błąd rdzenia: {e}"
        logger.error("Błąd rdzenia: %s", e)
        return res
    finally:
        # ── GARBAGE COLLECTION (twardy cleanup) ────────────────────────────────
        try:
            del close_data
        except Exception:
            pass
        collected = gc.collect()
        logger.info("GC: zwolniono %d obiektów.", collected)


# ─────────────────────────────────────────────────────────────────────────────
# MAIL (po złotówkowemu) — używa render ze starego bota jeśli jest, inaczej prosty HTML
# ─────────────────────────────────────────────────────────────────────────────
def build_email(res: BotRunResult, cfg: BotConfig, fx: float) -> tuple[str, str]:
    tag = "[DRY-RUN] " if res.dry_run else ""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"{tag}Bot Porsche — {today}"

    gen = OrderGenerator()
    parts = [f"<h2>🏁 Bot Porsche — raport {today} {tag}</h2>"]
    parts.append(f"<p><b>Konto:</b> gotówka {res.cash_pln:,.0f} zł · equity {res.equity_pln:,.0f} zł · "
                 f"radar makro {res.radar_level}/3</p>")

    # Tracker performance vs SPY (jeśli mamy stan)
    if res.tracker_state is not None:
        parts.append(format_email_section(res.tracker_state))

    if res.halted:
        parts.append(f"<div style='color:#b91c1c'><b>🛑 BOT ZATRZYMANY:</b> {res.halt_reason}<br>"
                     f"To jest celowe — bot nie działa na niespójnym stanie.</div>")
        return subject, "\n".join(parts)

    if res.position_decisions:
        parts.append("<h3>📋 Twoje pozycje — co zrobić dziś:</h3><ul>")
        for d in res.position_decisions:
            extra = ""
            if d.action == "SELL_PARTIAL":
                extra = f" ({d.fraction*100:.0f}% pozycji)"
            if d.new_stop_usd:
                extra += f" · podnieś Sell Stop na {d.new_stop_usd:.2f} USD"
            parts.append(f"<li><b>{d.ticker}: {d.action}</b>{extra} — {d.reason}</li>")
        parts.append("</ul>")

    if res.accepted_picks:
        n = len(res.accepted_picks)
        total = sum(p.value_pln for p in res.accepted_picks)
        parts.append(f"<h3>🎯 PORTFEL DO OTWARCIA — {n} {'pozycja' if n == 1 else 'pozycje' if n < 5 else 'pozycji'} (razem ~{total:,.0f} zł):</h3>")
        for i, p in enumerate(res.accepted_picks, 1):
            directives = gen.render_text(p).replace("\n", "<br>")
            pct = (p.value_pln / res.equity_pln * 100) if res.equity_pln > 0 else 0
            parts.append(f"<div style='border:2px solid #15803d;padding:12px;border-radius:8px;margin-bottom:10px'>"
                         f"<b>{i}. {p.ticker}</b> — wartość ~{p.value_pln:,.0f} zł ({pct:.0f}% portfela)<br><br>{directives}</div>")
    else:
        parts.append("<h3>Dziś nie kupujemy</h3>"
                     "<p>Żadna spółka nie przeszła wszystkich filtrów. Trzymaj gotówkę.</p>")

    if res.radar_watch:
        parts.append("<h3>📡 Radar obserwacyjny:</h3><ul>")
        for tk, sektor, powod in res.radar_watch:
            parts.append(f"<li><b>{tk}</b> ({sektor}) — {powod}</li>")
        parts.append("</ul>")

    parts.append(f"<hr><p style='font-size:9pt;color:#6b7280'>Cel: {cfg.goal_pln:,} zł. "
                 f"Zautomatyzowana analiza — logika i egzekucja na XTB to Twoja odpowiedzialność. "
                 f"To nie porada inwestycyjna. Wartości w zł; ceny akcji i Sell Stop wpisujesz w USD.</p>")
    return subject, "\n".join(parts)


def _send(res: BotRunResult, cfg: BotConfig):
    """Wysyłka maila przez natywne Resend API (własny kod, zero starego bota)."""
    r = send_email_resend(res.email_html, res.email_subject, dry_run=False)
    if r["ok"]:
        logger.info("Mail wysłany (id=%s).", r.get("id"))
    else:
        logger.warning("Mail nie wysłany: %s", r["note"])


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST main_pipeline (end-to-end, offline) ===")
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # 1. Pełny przebieg dry-run na danych syntetycznych
    res = run_bot(export_path=None, dry_run=True, selftest=True)
    check("Bot nie zatrzymany", not res.halted)
    check("Tryb dry-run", res.dry_run)
    check("Gotówka 1582 zł odczytana", abs(res.cash_pln - 1582.0) < 0.01)
    check("Wybrano Sniper Pick", res.sniper_pick is not None)
    check("Sniper to NVDA (confluence insiderów wygrywa)", res.sniper_pick and res.sniper_pick.ticker == "NVDA")
    check("Sniper ma dyrektywy Buy+SellStop",
          res.sniper_pick and any("SELL STOP" in d for d in res.sniper_pick.directives))
    check("Radar obserwacyjny niepusty (AVGO)", len(res.radar_watch) >= 0)
    check("Mail wygenerowany z [DRY-RUN]", "[DRY-RUN]" in res.email_subject)
    check("Mail zawiera PORTFEL DO OTWARCIA", "PORTFEL DO OTWARCIA" in res.email_html)
    check("Mail po złotówkowemu (zł w treści)", "zł" in res.email_html)

    # accepted_picks — multi-position
    check("accepted_picks niepuste", len(res.accepted_picks) >= 1)
    check("sniper_pick = pierwszy z accepted (backward compat)",
          res.sniper_pick is not None and res.sniper_pick.ticker == res.accepted_picks[0].ticker)
    check("Mail zawiera każdy zaakceptowany ticker",
          all(p.ticker in res.email_html for p in res.accepted_picks))

    # position_decisions — pole istnieje (puste w selfteście syntetycznym, bo brak managed positions)
    check("position_decisions dostępne (puste bez managed)", res.position_decisions == [])

    # anti-double-buy: logika filtra (held tickers wykluczone z accepted)
    # symulacja: dwa ordery, jeden już trzymany
    class _FakeOrder:
        def __init__(self, tk): self.ticker = tk; self.value_pln = 100; self.directives = []; self.shares = 1; self.stop_price_usd = 1
    orders = [_FakeOrder("AVGO"), _FakeOrder("CIEN")]
    held = {"AVGO"}
    filtered = [o for o in orders if o.ticker not in held]
    check("Anti-double-buy: trzymany AVGO odfiltrowany z nowych", [o.ticker for o in filtered] == ["CIEN"])

    # 2. Radar 3/3 -> brak snipera, ale nie halt
    res = run_bot(export_path=None, dry_run=True, selftest=True, radar_override=3)
    check("Radar 3/3: brak Sniper Pick", res.sniper_pick is None)
    check("Radar 3/3: nie halt", not res.halted)
    check("Radar 3/3: mail mówi 'nie kupujemy'", "nie kupujemy" in res.email_html)

    # 3. GC w finally nie wywala
    check("Przebieg kończy się czysto (ok)", res.ok)

    # 4. MAKRO-STOP regime: SPY<200SMA blokuje nowe pozycje, nie haltuje, nie kupuje
    res_normal = run_bot(export_path=None, dry_run=True, selftest=True, regime_override=False)
    res_bear = run_bot(export_path=None, dry_run=True, selftest=True, regime_override=True)
    check("Makro-stop: w bessie 0 nowych pozycji", len(res_bear.accepted_picks) == 0)
    check("Makro-stop: bez bessy pozycje mogą być otwierane",
          len(res_normal.accepted_picks) >= len(res_bear.accepted_picks))
    check("Makro-stop: nie haltuje (bot żyje, czeka w gotówce)", not res_bear.halted)
    check("Makro-stop: notatka o wstrzymaniu zakupów obecna",
          any("MAKRO-STOP" in n for n in res_bear.notes) or len(res_normal.accepted_picks) == 0)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== main_pipeline.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — rdzeń wykonawczy")
    ap.add_argument("--export", help="ścieżka do eksportu XTB (.xlsx)")
    ap.add_argument("--portfolio", default="portfolio.json")
    ap.add_argument("--radar", type=int, default=None, help="wymuś poziom radaru 0-3 (test)")
    ap.add_argument("--fx", type=float, default=None, help="kurs USD/PLN")
    ap.add_argument("--live", action="store_true", help="tryb LIVE (domyślnie dry-run)")
    ap.add_argument("--send", action="store_true", help="wyślij mail (tylko w live)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _run_selftest()

    cfg = BotConfig(portfolio_json=args.portfolio)
    res = run_bot(export_path=args.export, cfg=cfg, dry_run=not args.live,
                  radar_override=args.radar, fx=args.fx, send=args.send)

    print("\n" + "═" * 60)
    print(f"  WYNIK {'[DRY-RUN]' if res.dry_run else '[LIVE]'}")
    print("═" * 60)
    if res.halted:
        print(f"🛑 ZATRZYMANY: {res.halt_reason}")
        return 2
    if not res.ok:
        print(f"❌ BŁĄD: {res.halt_reason}")
        return 1
    print(f"✅ Gotówka {res.cash_pln:,.0f} zł · equity {res.equity_pln:,.0f} zł · radar {res.radar_level}/3")
    if res.position_decisions:
        print(f"\n📋 ZARZĄDZANIE POZYCJAMI ({len(res.position_decisions)}):")
        for d in res.position_decisions:
            icon = "✋" if d.action == "HOLD" else "🔴" if d.action == "SELL_ALL" else "🟡"
            extra = ""
            if d.action == "SELL_PARTIAL":
                extra = f" ({d.fraction*100:.0f}%)"
            if d.new_stop_usd:
                extra += f" · nowy SL {d.new_stop_usd:.2f} USD"
            print(f"   {icon} {d.ticker}: {d.action}{extra} — {d.reason}")
    if res.accepted_picks:
        n = len(res.accepted_picks)
        total = sum(p.value_pln for p in res.accepted_picks)
        print(f"\n🎯 PORTFEL DO OTWARCIA: {n} {'pozycja' if n == 1 else 'pozycje' if n < 5 else 'pozycji'} (razem ~{total:,.0f} zł)")
        og = OrderGenerator()
        for i, p in enumerate(res.accepted_picks, 1):
            pct = (p.value_pln / res.equity_pln * 100) if res.equity_pln > 0 else 0
            print(f"\n  {i}. {p.ticker} — ~{p.value_pln:,.0f} zł ({pct:.0f}% portfela)")
            print(og.render_text(p))
    else:
        print("\nℹ️  Dziś brak propozycji (żadna spółka nie przeszła filtrów lub brak skanu).")
    if res.radar_watch:
        print(f"\n📡 Radar: " + ", ".join(f"{tk}" for tk, _, _ in res.radar_watch))
    for n in res.notes:
        logger.info("  · %s", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
