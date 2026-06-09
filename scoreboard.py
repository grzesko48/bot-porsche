"""
scoreboard.py — TABLICA WYNIKÓW (forward-test: mierzymy realny edge łowcy)
=========================================================================
Bot Porsche. Dziennik decyzji (lowca_decisions_log.json) loguje KAŻDĄ rekomendację KUP,
ale dotąd NIC jej nie zamykało ani nie liczyło wyniku. Ten moduł to zamyka:

  • Dla każdej pozycji OPEN: licz zwrot % (cena teraz vs entry), dni trzymania, status.
  • Zamknięcie: cena ≤ stop -> STOP; cena ≥ cel -> WIN; po T+N sesji -> WIN/LOSS wg znaku.
  • ALFA vs SPY: gdy znamy spy_at_entry ORAZ spy_now -> alpha = zwrot_spółki − zwrot_SPY.
  • Agregacja PER TYP SYGNAŁU (KONTRAKT/INSIDER/IPO/...): ile, win-rate, śr. zwrot, śr. alfa.

PO CO: "maksymalizować można tylko to, co się mierzy". Po 2-3 miesiącach widać, KTÓRE
sygnały realnie zarabiają -> skalujemy te, ZABIJAMY te z ujemnym edge (osobna pętla wag).

Czysto obliczeniowy: silnik (score_log/aggregate) NIE rusza sieci ani plików — dane (ceny)
podaje wywołujący. Wrappery I/O (load/save/run) są cienkie i opakowane try/except.
Uruchomienie testów: python scoreboard.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date


# ─────────────────────────────────────────────────────────────────────────────
# POMOCNICZE
# ─────────────────────────────────────────────────────────────────────────────
def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _days_between(d_from, d_to):
    a, b = _parse_date(d_from), _parse_date(d_to)
    if a is None or b is None:
        return None
    return (b - a).days


def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SILNIK (bez sieci/plików — w pełni testowalny)
# ─────────────────────────────────────────────────────────────────────────────
def score_entry(entry: dict, price_now, spy_now, today, horizon_days: int = 30) -> dict:
    """Zwraca KOPIĘ wpisu wzbogaconą o: ret_pct, days_held, alpha_pct (lub None), status.
    Aktualizuje status TYLKO gdy był OPEN (zamknięte zostają zamknięte = idempotentnie)."""
    e = dict(entry)
    entry_px = _num(e.get("entry_usd"))
    px = _num(price_now)
    days = _days_between(e.get("date"), today)
    if days is not None:
        e["days_held"] = days
    if entry_px and entry_px > 0 and px and px > 0:
        ret = (px / entry_px - 1.0) * 100.0
        # GUARD DANYCH: świeży wpis (days<2) z ogromnym ruchem (>30%) = niemal na pewno zły fetch ceny
        # (np. GOOGL +98% w 0 dni). Nie scoruj i wyczyść ew. starą zglitchowaną wartość — przeliczy się
        # następnym razem z dobrą ceną (lub gdy minie ≥2 dni i ruch będzie realny).
        if days is not None and days < 2 and abs(ret) > 30.0:
            e.pop("ret_pct", None); e.pop("alpha_pct", None); e.pop("peak_ret_pct", None)
            return e
        e["ret_pct"] = round(ret, 1)
        # HIGH-WATER MARK zwrotu (do diagnostyki ścinania ogona: ile oddaliśmy od szczytu przy wyjściu)
        prev_peak = _num(e.get("peak_ret_pct"))
        e["peak_ret_pct"] = round(ret if prev_peak is None else max(prev_peak, ret), 1)
        spy0 = _num(e.get("spy_at_entry"))
        spyn = _num(spy_now)
        if spy0 and spy0 > 0 and spyn and spyn > 0:
            spy_ret = (spyn / spy0 - 1.0) * 100.0
            e["alpha_pct"] = round(ret - spy_ret, 1)
        # zamknięcie tylko jeśli wciąż OPEN
        if e.get("status", "OPEN") == "OPEN":
            stop = _num(e.get("stop_usd"))
            tgt = _num(e.get("target_usd"))
            if stop and px <= stop:
                e["status"] = "STOP"
            elif tgt and px >= tgt:
                e["status"] = "WIN"
            elif days is not None and days >= horizon_days:
                e["status"] = "WIN" if ret > 0 else "LOSS"
    return e


def score_log(log, price_map: dict, today, spy_now=None, horizon_days: int = 30, held=None) -> list:
    """Scoruje cały dziennik. price_map: {TICKER: cena_usd_teraz}. held: tickery realnie w portfelu
    (audyt 'kazał vs zrobił' — czy rekomendacja została wykonana). Wpisy bez ceny zostają nietknięte."""
    price_map = {str(k).upper(): v for k, v in (price_map or {}).items()}
    held_set = {str(h).upper() for h in (held or [])}
    out = []
    for e in (log or []):
        tk = str(e.get("ticker", "")).upper()
        s = score_entry(e, price_map.get(tk), spy_now, today, horizon_days)
        if held_set and tk in held_set and not s.get("executed"):
            s["executed"] = True   # widzieliśmy ją w portfelu => rekomendacja wykonana (egzekucja ręczna OK)
        out.append(s)
    return out


def aggregate(scored) -> dict:
    """Agregat globalny + per typ sygnału (kind). Liczy tylko wpisy z policzonym ret_pct."""
    def _blank():
        return {"n": 0, "closed": 0, "wins": 0, "ret_sum": 0.0, "alpha_sum": 0.0, "alpha_n": 0,
                "tail_sum": 0.0, "tail_n": 0}

    by_kind: dict = {}
    overall = _blank()
    for e in (scored or []):
        ret = e.get("ret_pct")
        if ret is None:
            continue
        kind = str(e.get("kind", "?")).upper()
        b = by_kind.setdefault(kind, _blank())
        for tgt in (b, overall):
            tgt["n"] += 1
            tgt["ret_sum"] += ret
            if e.get("alpha_pct") is not None:
                tgt["alpha_sum"] += e["alpha_pct"]
                tgt["alpha_n"] += 1
            if e.get("status") in ("WIN", "LOSS", "STOP"):
                tgt["closed"] += 1
                if e.get("status") == "WIN":
                    tgt["wins"] += 1
                pk = e.get("peak_ret_pct")
                if pk is not None:
                    tgt["tail_sum"] += (pk - ret)   # ile oddaliśmy od szczytu (ścięty ogon)
                    tgt["tail_n"] += 1

    def _finish(b):
        out = {
            "n": b["n"],
            "closed": b["closed"],
            "win_rate": round(b["wins"] / b["closed"] * 100.0, 0) if b["closed"] else None,
            "avg_ret": round(b["ret_sum"] / b["n"], 1) if b["n"] else None,
            "avg_alpha": round(b["alpha_sum"] / b["alpha_n"], 1) if b["alpha_n"] else None,
            "avg_tail_left": round(b["tail_sum"] / b["tail_n"], 1) if b["tail_n"] else None,
        }
        return out

    return {"overall": _finish(overall), "by_kind": {k: _finish(v) for k, v in by_kind.items()}}


# ─────────────────────────────────────────────────────────────────────────────
# PREZENTACJA (karta do maila) — czysty string, bez zależności
# ─────────────────────────────────────────────────────────────────────────────
def build_card_html(stats: dict, scored=None) -> str:
    """Kompaktowa karta 'Tablica wyników' do maila. Bezpieczna przy pustych danych."""
    ov = (stats or {}).get("overall", {})
    by = (stats or {}).get("by_kind", {})
    if not ov or not ov.get("n"):
        return ("<div style='margin:14px 0;padding:12px;border:1px solid #444;border-radius:8px;"
                "background:#1b1b1b;color:#ccc;'><b>📊 Tablica wyników (forward-test)</b><br>"
                "<span style='color:#999;'>Brak wycenionych pozycji — track record zacznie rosnąć, "
                "gdy rekomendacje dostaną aktualne ceny. Mierzymy, żeby skalować to, co działa.</span></div>")
    rows = []
    for kind, s in sorted(by.items(), key=lambda kv: (kv[1].get("avg_alpha") if kv[1].get("avg_alpha") is not None else kv[1].get("avg_ret") or -999), reverse=True):
        wr = f"{s['win_rate']:.0f}%" if s.get("win_rate") is not None else "—"
        ar = f"{s['avg_ret']:+.1f}%" if s.get("avg_ret") is not None else "—"
        aa = f"{s['avg_alpha']:+.1f} pp" if s.get("avg_alpha") is not None else "—"
        rows.append(f"<tr><td style='padding:3px 8px;'>{kind}</td>"
                    f"<td style='padding:3px 8px;text-align:center;'>{s['n']}</td>"
                    f"<td style='padding:3px 8px;text-align:center;'>{wr}</td>"
                    f"<td style='padding:3px 8px;text-align:right;'>{ar}</td>"
                    f"<td style='padding:3px 8px;text-align:right;'>{aa}</td></tr>")
    ov_ar = f"{ov['avg_ret']:+.1f}%" if ov.get("avg_ret") is not None else "—"
    ov_aa = f"{ov['avg_alpha']:+.1f} pp" if ov.get("avg_alpha") is not None else "—"
    ov_tl = f"{ov['avg_tail_left']:.0f} pp" if ov.get("avg_tail_left") is not None else "—"
    n_rec = len(scored or [])
    n_exec = sum(1 for e in (scored or []) if e.get("executed"))
    exec_str = f" · wykonane <b>{n_exec}/{n_rec}</b>" if n_rec else ""
    return (
        "<div style='margin:14px 0;padding:12px;border:1px solid #444;border-radius:8px;background:#1b1b1b;color:#ddd;'>"
        f"<b>📊 Tablica wyników (forward-test)</b> — {ov['n']} rekomendacji, {ov['closed']} zamkniętych · "
        f"śr. zwrot <b>{ov_ar}</b> · alfa vs SPY <b>{ov_aa}</b> · oddany ogon <b>{ov_tl}</b>{exec_str}"
        "<table style='border-collapse:collapse;margin-top:8px;font-size:13px;width:100%;'>"
        "<tr style='color:#aaa;border-bottom:1px solid #333;'>"
        "<th style='padding:3px 8px;text-align:left;'>Sygnał</th>"
        "<th style='padding:3px 8px;'>n</th><th style='padding:3px 8px;'>win</th>"
        "<th style='padding:3px 8px;text-align:right;'>śr. zwrot</th>"
        "<th style='padding:3px 8px;text-align:right;'>alfa</th></tr>"
        + "".join(rows) +
        "</table>"
        "<div style='color:#888;font-size:11px;margin-top:6px;'>Skalujemy sygnały z dodatnią alfą, "
        "wygaszamy z ujemną (po n≥8/typ). Alfa pusta = brak zapisanego SPY z dnia wejścia.</div></div>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# WRAPPERY I/O (cienkie, bezpieczne)
# ─────────────────────────────────────────────────────────────────────────────
def load_log(path="lowca_decisions_log.json") -> list:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_log(log, path="lowca_decisions_log.json") -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def run(path="lowca_decisions_log.json", price_map=None, today=None, spy_now=None,
        horizon_days=30, persist=True) -> dict:
    """Pełny przebieg: wczytaj -> scoruj -> (opcjonalnie zapisz zaktualizowane statusy) -> agreguj.
    Zwraca {scored, stats, card_html}. Nigdy nie wywala biegu (try/except w wrapperach)."""
    log = load_log(path)
    scored = score_log(log, price_map or {}, today, spy_now, horizon_days)
    if persist and scored:
        save_log(scored, path)
    stats = aggregate(scored)
    return {"scored": scored, "stats": stats, "card_html": build_card_html(stats, scored)}


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_spy_yf():
    """Cena SPY teraz (benchmark do alfy). None gdy brak yfinance/sieci (np. lokalnie)."""
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(period="5d")
        if len(h):
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def fetch_prices_yf(tickers):
    """Aktualne ceny dla listy tickerów (po jednym, odpornie). {} gdy brak yfinance."""
    out = {}
    try:
        import yfinance as yf
    except Exception:
        return out
    for t in {str(x).upper() for x in (tickers or []) if x}:
        try:
            h = yf.Ticker(t).history(period="5d")
            if len(h):
                out[t] = float(h["Close"].iloc[-1])
        except Exception:
            continue
    return out


def refresh_card(path="lowca_decisions_log.json", today=None, spy_now=None, held=None) -> str:
    """Pełny refresh dla maila: fetch cen tickerów z dziennika -> scoring -> persist -> karta HTML.
    held: tickery realnie w portfelu (audyt 'kazał vs zrobił').
    W PEŁNI guarded: każdy błąd -> pusty string (mail bez zmian, nigdy nie wywala biegu)."""
    try:
        log = load_log(path)
        if not log:
            return ""
        prices = fetch_prices_yf([e.get("ticker") for e in log])
        if spy_now is None:
            spy_now = fetch_spy_yf()
        scored = score_log(log, prices, today, spy_now, held=held)
        save_log(scored, path)
        return build_card_html(aggregate(scored), scored)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline)
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST scoreboard (offline) ===")
    P = F = 0

    def ok(name, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {name}")
        else: F += 1; print(f"  [FAIL] {name}")

    # wpisy testowe
    log = [
        {"id": "2026-05-01-AAA", "date": "2026-05-01", "ticker": "AAA", "kind": "KONTRAKT",
         "entry_usd": 10.0, "stop_usd": 8.0, "target_usd": 14.0, "spy_at_entry": 500.0, "status": "OPEN"},
        {"id": "2026-05-01-BBB", "date": "2026-05-01", "ticker": "BBB", "kind": "IPO",
         "entry_usd": 20.0, "stop_usd": 15.0, "target_usd": 30.0, "status": "OPEN"},  # bez spy
        {"id": "2026-05-20-CCC", "date": "2026-05-20", "ticker": "CCC", "kind": "KONTRAKT",
         "entry_usd": 50.0, "stop_usd": 40.0, "target_usd": 70.0, "spy_at_entry": 510.0, "status": "OPEN"},
    ]
    today = "2026-06-06"
    prices = {"AAA": 13.0, "BBB": 14.0, "CCC": 55.0}   # AAA +30%, BBB -30% (poniżej stop), CCC +10%
    spy_now = 525.0

    scored = score_log(log, prices, today, spy_now, horizon_days=30)
    by_id = {e["id"]: e for e in scored}

    aaa = by_id["2026-05-01-AAA"]
    ok("AAA: ret_pct +30%", abs(aaa["ret_pct"] - 30.0) < 0.1)
    ok("AAA: alpha policzona (spy +5%) ~ +25pp", aaa.get("alpha_pct") is not None and abs(aaa["alpha_pct"] - 25.0) < 0.1)
    ok("AAA: dni trzymania = 36", aaa.get("days_held") == 36)
    ok("AAA: po T+30 i ret>0 -> WIN", aaa["status"] == "WIN")

    bbb = by_id["2026-05-01-BBB"]
    ok("BBB: cena 14 <= stop 15 -> STOP", bbb["status"] == "STOP")
    ok("BBB: brak spy -> alpha None", bbb.get("alpha_pct") is None)

    ccc = by_id["2026-05-20-CCC"]
    ok("CCC: 17 dni, nie trafił stop/cel -> wciąż OPEN", ccc["status"] == "OPEN")
    ok("CCC: ret_pct +10%", abs(ccc["ret_pct"] - 10.0) < 0.1)

    # idempotencja: drugi przebieg nie zmienia zamkniętych
    scored2 = score_log(scored, prices, today, spy_now, 30)
    ok("Idempotencja: WIN zostaje WIN", by_id["2026-05-01-AAA"]["status"] == "WIN" and
       next(e for e in scored2 if e["id"] == "2026-05-01-AAA")["status"] == "WIN")

    # agregacja
    stats = aggregate(scored)
    ov = stats["overall"]
    ok("Agregat: n=3", ov["n"] == 3)
    ok("Agregat: closed=2 (AAA WIN, BBB STOP)", ov["closed"] == 2)
    ok("Agregat: win_rate 50%", ov["win_rate"] == 50.0)
    ok("Agregat: śr. zwrot (30-30+10)/3 ~ +3.3%", abs(ov["avg_ret"] - 3.3) < 0.2)
    kon = stats["by_kind"].get("KONTRAKT", {})
    ok("Per-kind KONTRAKT: n=2 (AAA,CCC)", kon.get("n") == 2)
    ok("Per-kind KONTRAKT: avg_alpha policzona", kon.get("avg_alpha") is not None)

    # ── DIAGNOSTYKA SCINANIA OGONA (peak sledzony przez biegi; oddany ogon = peak - exit) ──
    tlog = [{"id": "t1", "date": "2026-05-01", "ticker": "TTT", "kind": "IPO",
             "entry_usd": 10.0, "stop_usd": 8.0, "target_usd": 30.0, "status": "OPEN"}]
    s1 = score_log(tlog, {"TTT": 15.0}, "2026-05-20")     # +50% (szczyt), 19 dni -> OPEN
    ok("Peak sledzony: +50%", abs(s1[0]["peak_ret_pct"] - 50.0) < 0.1)
    s2 = score_log(s1, {"TTT": 13.0}, "2026-06-06")       # spadl do +30%, 36 dni -> WIN
    ok("Peak utrzymany mimo spadku ceny (+50%)", abs(s2[0]["peak_ret_pct"] - 50.0) < 0.1)
    ok("Pozycja zamknieta WIN po T+30", s2[0]["status"] == "WIN")
    ipo = aggregate(s2)["by_kind"].get("IPO", {})
    ok("Oddany ogon = peak 50 - exit 30 = 20 pp", ipo.get("avg_tail_left") is not None and abs(ipo["avg_tail_left"] - 20.0) < 0.1)

    # ── AUDYT 'kazal vs zrobil' (rekomendacja w portfelu -> executed) ──
    elog = [{"id": "e1", "date": "2026-05-01", "ticker": "EEE", "kind": "IPO", "entry_usd": 10.0, "status": "OPEN"},
            {"id": "e2", "date": "2026-05-01", "ticker": "FFF", "kind": "IPO", "entry_usd": 10.0, "status": "OPEN"}]
    se = score_log(elog, {"EEE": 11.0, "FFF": 11.0}, "2026-05-10", held=["EEE"])
    ok("Wykonana (EEE w portfelu) -> executed", se[0].get("executed") is True)
    ok("Niewykonana (FFF brak w portfelu) -> nie executed", not se[1].get("executed"))
    ok("Karta pokazuje 'wykonane 1/2'", "wykonane" in build_card_html(aggregate(se), se) and "1/2" in build_card_html(aggregate(se), se))

    # karta HTML
    card = build_card_html(stats, scored)
    ok("Karta HTML: zawiera 'Tablica wyników'", "Tablica wyników" in card)
    ok("Karta HTML: pokazuje typ KONTRAKT", "KONTRAKT" in card)
    ok("Karta pusta: bezpieczna przy braku danych", "Brak wycenionych" in build_card_html({"overall": {}, "by_kind": {}}))

    # bez ceny -> wpis nietknięty (brak ret_pct)
    s_nopx = score_log([{"id": "x", "date": "2026-05-01", "ticker": "ZZZ", "kind": "IPO",
                         "entry_usd": 10.0, "status": "OPEN"}], {}, today)
    ok("Brak ceny -> brak ret_pct, status OPEN", s_nopx[0].get("ret_pct") is None and s_nopx[0]["status"] == "OPEN")

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== scoreboard.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Tablica wyników (forward-test)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
