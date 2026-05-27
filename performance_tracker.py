"""performance_tracker.py — pomiar realnego zwrotu bota vs benchmark SPY i automatyczny ALERT
gdy bot underperformuje przez zbyt długi okres.

POWÓD: ultra test pokazał, że strategia ma EDGE CYKLICZNY (działa w bull markets), nie permanentny.
Świadomy live z większym kapitałem WYMAGA wbudowanej reguły wyjścia, bo inaczej zostajemy
w strategii, która już przestała działać. Ten moduł to ta reguła:

1. Co cykl zapisuje SNAPSHOT: data, equity bota, cena SPY (benchmark), kurs USD/PLN.
2. Liczy: ile zarobiło 1582 zł w bocie vs ile zarobiłoby w SPY od daty startu.
3. ALERT gdy bot jest >= ALERT_THRESHOLD_PP punktów pod SPY przez >= ALERT_CONSECUTIVE_DAYS dni.

Alert NIE blokuje bota technicznie — mailuje JASNY komunikat, że bot wlecze się pod rynkiem
i sugeruje wyjście do SPY. Decyzja zawsze należy do człowieka. Bot nie handluje sam.

NIE używa zewnętrznego API (cena SPY przychodzi z istniejącego skanera). Plik JSON, append-only.
"""
from __future__ import annotations
import json, os, logging
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# DOMYŚLNE PROGI — świadome ryzyko ale skończone, do strojenia świadomie.
DEFAULT_ALERT_THRESHOLD_PP: float = 10.0   # bot >=10pp pod SPY
DEFAULT_ALERT_CONSECUTIVE_DAYS: int = 56   # przez 8 tygodni z rzędu
DEFAULT_HARD_STOP_THRESHOLD_PP: float = 20.0  # bot >=20pp pod SPY = HARD ALERT (krytyczne)


@dataclass
class EquitySnapshot:
    """Pojedynczy punkt pomiarowy. JSON-serializowalny."""
    date: str                       # ISO YYYY-MM-DD
    equity_pln: float               # equity bota (cash + pozycje przeliczone na PLN)
    spy_price_usd: float            # cena SPY tego dnia
    fx_rate: float                  # USD/PLN
    note: str = ""                  # opcjonalna notatka (np. "start", "po pierwszej tranzakcji")


@dataclass
class TrackerConfig:
    log_path: str = "equity_log.json"
    alert_threshold_pp: float = DEFAULT_ALERT_THRESHOLD_PP
    alert_consecutive_days: int = DEFAULT_ALERT_CONSECUTIVE_DAYS
    hard_stop_threshold_pp: float = DEFAULT_HARD_STOP_THRESHOLD_PP


@dataclass
class TrackingState:
    """Aktualny stan vs benchmark — zwracany do mailera."""
    n_snapshots: int = 0
    start_date: Optional[str] = None
    start_equity_pln: float = 0.0
    start_spy_price: float = 0.0
    start_fx: float = 0.0
    current_equity_pln: float = 0.0
    spy_equity_pln: float = 0.0     # ile bylo 1582 zł trzymając SPY od startu
    bot_pl_pct: float = 0.0         # zwrot bota od startu (%)
    spy_pl_pct: float = 0.0         # zwrot SPY od startu (%)
    edge_pp: float = 0.0            # różnica pp
    alert: bool = False             # czy próg miękki przekroczony
    hard_alert: bool = False        # czy próg twardy przekroczony
    days_below_threshold: int = 0
    alert_reason: str = ""


def load_equity_log(path: str) -> list:
    """Ładuje historię snapshotów. Brak pliku -> []. Korupcja -> [] + log warn."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("equity_log: zły format (oczekiwano listy), startuję od pustej.")
            return []
        return data
    except Exception as e:
        logger.warning("equity_log: błąd odczytu %s — startuję od pustej.", e)
        return []


def save_equity_log(path: str, snapshots: list) -> None:
    """Zapis atomowy: tmp + rename, żeby się nie ułamać w połowie zapisu."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshots, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def record_snapshot(cfg: TrackerConfig, equity_pln: float, spy_price_usd: float,
                    fx_rate: float, note: str = "",
                    today: Optional[date] = None) -> "list[dict]":
    """Dopisuje snapshot do logu. Jeśli już jest snapshot z dziś -> nadpisuje (idempotent)."""
    today = today or date.today()
    snap = EquitySnapshot(date=today.isoformat(), equity_pln=round(equity_pln, 2),
                          spy_price_usd=round(spy_price_usd, 4), fx_rate=round(fx_rate, 4),
                          note=note)
    snapshots = load_equity_log(cfg.log_path)
    # idempotent: jeśli już dziś jest, zastąp
    snapshots = [s for s in snapshots if s.get("date") != snap.date]
    snapshots.append(asdict(snap))
    snapshots.sort(key=lambda s: s["date"])
    save_equity_log(cfg.log_path, snapshots)
    return snapshots


def _days_between(a_iso: str, b_iso: str) -> int:
    return abs((date.fromisoformat(b_iso) - date.fromisoformat(a_iso)).days)


def compute_state(snapshots: list, cfg: TrackerConfig) -> TrackingState:
    """Liczy zwrot bota vs SPY od pierwszego snapshotu i sprawdza warunki alertu."""
    st = TrackingState()
    st.n_snapshots = len(snapshots)
    if not snapshots:
        return st
    first = snapshots[0]; last = snapshots[-1]
    st.start_date = first["date"]
    st.start_equity_pln = float(first["equity_pln"])
    st.start_spy_price = float(first["spy_price_usd"])
    st.start_fx = float(first["fx_rate"])
    st.current_equity_pln = float(last["equity_pln"])
    # SPY benchmark: ile zł zrobi 1582 zł trzymając SPY od startu, z aktualnym kursem
    if st.start_spy_price > 0 and st.start_fx > 0:
        spy_units = st.start_equity_pln / (st.start_spy_price * st.start_fx)
        st.spy_equity_pln = spy_units * float(last["spy_price_usd"]) * float(last["fx_rate"])
    # zwroty %
    if st.start_equity_pln > 0:
        st.bot_pl_pct = 100.0 * (st.current_equity_pln - st.start_equity_pln) / st.start_equity_pln
    if st.spy_equity_pln > 0 and st.start_equity_pln > 0:
        st.spy_pl_pct = 100.0 * (st.spy_equity_pln - st.start_equity_pln) / st.start_equity_pln
    st.edge_pp = st.bot_pl_pct - st.spy_pl_pct

    # ── ALERT: ile DNI Z RZĘDU bot jest >= threshold_pp pod SPY ──
    # idź wstecz od ostatniego snapshotu; przerwij na pierwszym dniu, który NIE spełnia warunku.
    days_below = 0
    breach_start = None
    for s in reversed(snapshots):
        spy_units = st.start_equity_pln / (st.start_spy_price * st.start_fx) if st.start_spy_price > 0 else 0
        spy_eq = spy_units * float(s["spy_price_usd"]) * float(s["fx_rate"]) if spy_units else st.start_equity_pln
        bot_pl = (float(s["equity_pln"]) - st.start_equity_pln) / st.start_equity_pln * 100
        spy_pl = (spy_eq - st.start_equity_pln) / st.start_equity_pln * 100
        edge_here = bot_pl - spy_pl
        if edge_here <= -cfg.alert_threshold_pp:
            breach_start = s["date"]
        else:
            break
    if breach_start:
        days_below = _days_between(breach_start, last["date"])
    st.days_below_threshold = days_below

    if st.edge_pp <= -cfg.hard_stop_threshold_pp:
        st.hard_alert = True
        st.alert = True
        st.alert_reason = (f"HARD ALERT: bot {st.edge_pp:+.1f}pp pod SPY "
                           f"(próg twardy: -{cfg.hard_stop_threshold_pp:.0f}pp). "
                           f"Strategia wyraźnie nie działa — ROZWAŻ wyjście do SPY.")
    elif days_below >= cfg.alert_consecutive_days and st.edge_pp <= -cfg.alert_threshold_pp:
        st.alert = True
        st.alert_reason = (f"ALERT: bot {st.edge_pp:+.1f}pp pod SPY przez "
                           f"{days_below} dni z rzędu (próg: -{cfg.alert_threshold_pp:.0f}pp / "
                           f"{cfg.alert_consecutive_days} dni). Ultra test mówił o cyklicznym edge — "
                           f"możliwe, że cykl się odwrócił. ROZWAŻ wyjście do SPY.")
    return st


def format_email_section(state: TrackingState) -> str:
    """HTML sekcja do maila bota: equity vs SPY + ewentualny alert."""
    if state.n_snapshots == 0:
        return "<p><b>Tracker:</b> pierwszy cykl, zapisuję snapshot startowy.</p>"
    color = "#c0392b" if state.hard_alert else ("#e67e22" if state.alert else "#27ae60")
    badge = ""
    if state.hard_alert:
        badge = f"<div style='background:#fdecea;border:1px solid #f5b7b1;padding:10px;margin:8px 0;border-radius:6px;color:#922b21;'><b>🛑 HARD ALERT</b><br>{state.alert_reason}</div>"
    elif state.alert:
        badge = f"<div style='background:#fdf2e9;border:1px solid #f5cba7;padding:10px;margin:8px 0;border-radius:6px;color:#a04000;'><b>⚠ ALERT</b><br>{state.alert_reason}</div>"
    return (
        f"<h3 style='margin-top:18px;'>Performance vs SPY</h3>"
        f"<table style='border-collapse:collapse;margin:4px 0;'>"
        f"<tr><td style='padding:2px 12px 2px 0;'>Start:</td><td>{state.start_date} "
        f"({state.start_equity_pln:.2f} zł)</td></tr>"
        f"<tr><td style='padding:2px 12px 2px 0;'>Bot dziś:</td><td>{state.current_equity_pln:.2f} zł "
        f"({state.bot_pl_pct:+.1f}%)</td></tr>"
        f"<tr><td style='padding:2px 12px 2px 0;'>SPY benchmark:</td><td>{state.spy_equity_pln:.2f} zł "
        f"({state.spy_pl_pct:+.1f}%)</td></tr>"
        f"<tr><td style='padding:2px 12px 2px 0;'><b>Edge:</b></td>"
        f"<td style='color:{color};'><b>{state.edge_pp:+.1f} pp</b> "
        f"(snapshotów: {state.n_snapshots})</td></tr>"
        f"</table>"
        f"{badge}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    import tempfile
    print("=== SELFTEST performance_tracker ===")
    passed = failed = 0
    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "eq.json")
        cfg = TrackerConfig(log_path=path, alert_threshold_pp=10.0,
                            alert_consecutive_days=2, hard_stop_threshold_pp=20.0)

        # 1. brak pliku -> pusta lista
        check("Brak pliku -> []", load_equity_log(path) == [])

        # 2. record + idempotent
        s1 = record_snapshot(cfg, equity_pln=1582, spy_price_usd=580, fx_rate=4.0,
                             note="start", today=date(2026, 5, 27))
        check("Po pierwszym snapshocie 1 wpis", len(s1) == 1)
        s1b = record_snapshot(cfg, equity_pln=1600, spy_price_usd=580, fx_rate=4.0,
                              today=date(2026, 5, 27))  # ten sam dzień
        check("Idempotent: ten sam dzień nadpisuje", len(s1b) == 1 and s1b[0]["equity_pln"] == 1600)

        # 3. compute_state — bot dorównuje SPY
        s = [{"date":"2026-05-27","equity_pln":1582,"spy_price_usd":580,"fx_rate":4.0,"note":""},
             {"date":"2026-06-27","equity_pln":1660,"spy_price_usd":609,"fx_rate":4.0,"note":""}]
        st = compute_state(s, cfg)
        # SPY 580->609 = +5%; bot 1582->1660 = +4.93%; edge ~0
        check("Compute: edge ~0 gdy bot dorównuje SPY", abs(st.edge_pp) < 0.5)
        check("Compute: brak alertu gdy edge ~0", not st.alert)

        # 4. bot 15pp pod SPY przez 2 dni z rzędu -> ALERT (próg miękki)
        s_under = [
            {"date":"2026-05-27","equity_pln":1582,"spy_price_usd":580,"fx_rate":4.0,"note":""},
            {"date":"2026-06-25","equity_pln":1500,"spy_price_usd":609,"fx_rate":4.0,"note":""},
            {"date":"2026-06-26","equity_pln":1500,"spy_price_usd":609,"fx_rate":4.0,"note":""},
            {"date":"2026-06-27","equity_pln":1500,"spy_price_usd":609,"fx_rate":4.0,"note":""},
        ]
        st_u = compute_state(s_under, cfg)
        check("Underperformance: edge ujemny", st_u.edge_pp < -5)
        check("Underperformance: ALERT po >=2 dniach pod progiem", st_u.alert)

        # 5. -25pp -> HARD ALERT niezależnie od dni
        s_hard = [
            {"date":"2026-05-27","equity_pln":1582,"spy_price_usd":580,"fx_rate":4.0,"note":""},
            {"date":"2026-06-27","equity_pln":1200,"spy_price_usd":609,"fx_rate":4.0,"note":""},
        ]
        st_h = compute_state(s_hard, cfg)
        check("HARD ALERT przy edge <= -20pp", st_h.hard_alert and st_h.alert)

        # 6. format_email_section produkuje HTML, alert badge widoczny
        html = format_email_section(st_h)
        check("Mail: HARD ALERT widoczny w HTML", "HARD ALERT" in html)
        html_ok = format_email_section(st)
        check("Mail: brak alertu gdy edge OK", "ALERT" not in html_ok)

        # 7. atomowy zapis: po save plik istnieje i parsuje
        save_equity_log(path, s_under)
        check("Save: plik istnieje", os.path.exists(path))
        check("Save: parsuje się z powrotem", len(load_equity_log(path)) == 4)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== performance_tracker.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_run_selftest())
    print("performance_tracker.py: brak akcji. Użyj --selftest.")
