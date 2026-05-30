# lens_evaluate.py — OCENA skuteczności soczewek na podstawie rejestru shadow.
#
# Czyta lens_shadow_log.json (zbierany codziennie przez bota), dla każdego wpisu
# pobiera forward return (co spółka zrobiła PO dacie wpisu) i liczy:
#   - średni forward return zestawu BASELINE (sam momentum)
#   - średni forward return zestawu LENS (momentum × soczewki)
#   - różnicę (edge soczewek w pp)
#   - hit-rate: jak często zestaw lens pobił baseline
#
# To jest TWARDY dowód: jeśli po N tygodniach lens bije baseline -> włączamy LENS_MODE=live.
# Jeśli nie -> soczewki zostają informacyjne. Decyzja na danych, nie na wierze.
#
# Uruchamiasz RĘCZNIE co tydzień/dwa:  python lens_evaluate.py --horizon 10
# (horizon = ile dni handlowych po wpisie mierzymy forward return)

from __future__ import annotations
import os
import json
import sys
from datetime import datetime, date, timedelta
from typing import Optional

SHADOW_LOG_PATH = "lens_shadow_log.json"
DEFAULT_HORIZON_DAYS = 10        # dni handlowych do pomiaru forward return
MIN_ENTRIES_FOR_VERDICT = 8      # minimum wpisów z divergencją na sensowny werdykt


def _fetch_forward_return(ticker: str, from_date: str, horizon_days: int) -> Optional[float]:
    """Forward return spółki od from_date przez horizon_days dni handlowych.
    Zwraca ułamek (0.05 = +5%) albo None gdy brak danych. BEZPIECZNE: błąd -> None."""
    try:
        import yfinance as yf
        start = date.fromisoformat(from_date)
        # pobierz ~horizon*2 dni kalendarzowych + bufor, żeby złapać dni handlowe
        end = start + timedelta(days=horizon_days * 2 + 10)
        df = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 2:
            return None
        closes = df["Close"].dropna()
        if len(closes) < 2:
            return None
        entry_px = float(closes.iloc[0])
        # forward o horizon dni handlowych (lub ostatni dostępny, jeśli krócej)
        idx = min(horizon_days, len(closes) - 1)
        exit_px = float(closes.iloc[idx])
        if entry_px <= 0:
            return None
        return exit_px / entry_px - 1.0
    except Exception:
        return None


def _avg_return(tickers: list, from_date: str, horizon: int) -> Optional[float]:
    """Średni forward return koszyka tickerów. None gdy żaden nie ma danych."""
    rets = []
    for tk in tickers:
        r = _fetch_forward_return(tk, from_date, horizon)
        if r is not None:
            rets.append(r)
    if not rets:
        return None
    return sum(rets) / len(rets)


def evaluate(log_path: str = SHADOW_LOG_PATH, horizon: int = DEFAULT_HORIZON_DAYS,
             offline_entries: Optional[list] = None) -> dict:
    """Główna ocena. Zwraca raport: ile wpisów, edge soczewek w pp, hit-rate, werdykt.
    offline_entries: do testów — lista wpisów z gotowym '_baseline_ret'/'_lens_ret'."""
    if offline_entries is not None:
        entries = offline_entries
    else:
        if not os.path.exists(log_path):
            return {"error": f"brak rejestru {log_path} — bot jeszcze nie logował"}
        try:
            entries = json.loads(open(log_path, encoding="utf-8").read())
        except Exception as e:
            return {"error": f"rejestr nieczytelny: {e}"}

    today = date.today()
    measured = []        # wpisy z policzonym forward return obu zestawów
    for e in entries:
        # tylko wpisy starsze niż horizon (inaczej forward return niepełny)
        try:
            d = date.fromisoformat(e["date"])
        except Exception:
            continue
        if offline_entries is None and (today - d).days < horizon:
            continue   # za świeży — jeszcze nie ma pełnego okna forward
        # różnica zestawów ma sens tylko gdy była divergencja
        base_top = e.get("baseline_top", [])
        lens_top = e.get("lens_top", [])
        if set(base_top) == set(lens_top):
            continue   # soczewki nic nie zmieniły — pomijamy w pomiarze edge

        if offline_entries is not None:
            base_ret = e.get("_baseline_ret")
            lens_ret = e.get("_lens_ret")
        else:
            base_ret = _avg_return(base_top, e["date"], horizon)
            lens_ret = _avg_return(lens_top, e["date"], horizon)
        if base_ret is None or lens_ret is None:
            continue
        measured.append({"date": e["date"], "baseline_ret": base_ret,
                         "lens_ret": lens_ret, "delta": lens_ret - base_ret})

    n = len(measured)
    if n == 0:
        return {"measured": 0,
                "note": "brak wpisów z divergencją gotowych do oceny (za świeże lub soczewki "
                        "nic nie zmieniły). Zbieraj dalej."}
    avg_base = sum(m["baseline_ret"] for m in measured) / n
    avg_lens = sum(m["lens_ret"] for m in measured) / n
    edge_pp = (avg_lens - avg_base) * 100
    wins = sum(1 for m in measured if m["delta"] > 0)
    hit_rate = wins / n

    if n < MIN_ENTRIES_FOR_VERDICT:
        verdict = f"ZA MAŁO DANYCH ({n}/{MIN_ENTRIES_FOR_VERDICT}) — zbieraj dalej, nie włączaj live"
    elif edge_pp > 1.0 and hit_rate >= 0.55:
        verdict = "SOCZEWKI BIJĄ BASELINE — rozważ LENS_MODE=live (edge dodatni, hit-rate OK)"
    elif edge_pp < -1.0:
        verdict = "SOCZEWKI SZKODZĄ — zostają informacyjne, NIE włączaj live"
    else:
        verdict = "BRAK WYRAŹNEGO EDGE — zostaw shadow, zbieraj więcej danych"

    return {
        "measured": n, "horizon_days": horizon,
        "avg_baseline_return_pct": round(avg_base * 100, 2),
        "avg_lens_return_pct": round(avg_lens * 100, 2),
        "lens_edge_pp": round(edge_pp, 2),
        "hit_rate": round(hit_rate, 2),
        "verdict": verdict,
        "detail": measured,
    }


def format_report(rep: dict) -> str:
    """Czytelny raport tekstowy."""
    if "error" in rep:
        return f"BŁĄD: {rep['error']}"
    if rep.get("measured", 0) == 0:
        return rep.get("note", "Brak danych do oceny.")
    return (
        f"OCENA SOCZEWEK (forward test, horyzont {rep['horizon_days']} dni handl.)\n"
        f"  Zmierzonych cykli z divergencją: {rep['measured']}\n"
        f"  Średni zwrot BASELINE (sam momentum): {rep['avg_baseline_return_pct']:+.2f}%\n"
        f"  Średni zwrot LENS (z soczewkami):     {rep['avg_lens_return_pct']:+.2f}%\n"
        f"  EDGE soczewek: {rep['lens_edge_pp']:+.2f} pp\n"
        f"  Hit-rate (lens > baseline): {rep['hit_rate']*100:.0f}%\n"
        f"  WERDYKT: {rep['verdict']}"
    )


def _run_selftest() -> int:
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {name}")

    # offline: soczewki wyraźnie biją baseline
    good = [
        {"date": "2026-01-01", "baseline_top": ["A"], "lens_top": ["B"],
         "_baseline_ret": 0.02, "_lens_ret": 0.05},
        {"date": "2026-01-02", "baseline_top": ["A"], "lens_top": ["B"],
         "_baseline_ret": 0.01, "_lens_ret": 0.04},
        {"date": "2026-01-03", "baseline_top": ["A"], "lens_top": ["B"],
         "_baseline_ret": -0.01, "_lens_ret": 0.02},
    ]
    rep = evaluate(offline_entries=good, horizon=10)
    check("edge dodatni gdy lens lepszy", rep["lens_edge_pp"] > 0)
    check("hit-rate 100% gdy zawsze lepszy", rep["hit_rate"] == 1.0)
    check("za mało danych -> werdykt ostrożny", "ZA MAŁO" in rep["verdict"])

    # offline: soczewki szkodzą
    bad = [{"date": f"2026-01-{i:02d}", "baseline_top": ["A"], "lens_top": ["B"],
            "_baseline_ret": 0.05, "_lens_ret": 0.01} for i in range(1, 11)]
    rep2 = evaluate(offline_entries=bad, horizon=10)
    check("edge ujemny gdy lens gorszy", rep2["lens_edge_pp"] < 0)
    check("werdykt: szkodzą", "SZKODZĄ" in rep2["verdict"])

    # offline: soczewki biją, dość danych -> werdykt live
    win = [{"date": f"2026-02-{i:02d}", "baseline_top": ["A"], "lens_top": ["B"],
            "_baseline_ret": 0.01, "_lens_ret": 0.03} for i in range(1, 11)]
    rep3 = evaluate(offline_entries=win, horizon=10)
    check("dość danych + edge -> werdykt live", "live" in rep3["verdict"].lower())

    # brak divergencji -> pomijane
    nodiv = [{"date": "2026-01-01", "baseline_top": ["A"], "lens_top": ["A"],
              "_baseline_ret": 0.02, "_lens_ret": 0.02}]
    rep4 = evaluate(offline_entries=nodiv, horizon=10)
    check("brak divergencji -> 0 zmierzonych", rep4["measured"] == 0)

    check("format_report działa", "EDGE" in format_report(rep))

    print(f"WYNIK lens_evaluate: {passed} OK, {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    # tryb produkcyjny: oceń rejestr
    horizon = DEFAULT_HORIZON_DAYS
    for i, a in enumerate(sys.argv):
        if a == "--horizon" and i + 1 < len(sys.argv):
            try:
                horizon = int(sys.argv[i + 1])
            except Exception:
                pass
    rep = evaluate(horizon=horizon)
    print(format_report(rep))
