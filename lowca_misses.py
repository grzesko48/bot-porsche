# -*- coding: utf-8 -*-
"""
lowca_misses.py — PĘTLA UCZENIA SIĘ Bota Łowcy (analiza pominięć + dziennik).
Osobny plik (lowca_*), nie rusza opportunity_lens.py.

Idea: codziennie sprawdzamy, która spółka MOCNO urosła wczoraj w jeden dzień,
a łowca jej NIE miał na radarze (przeoczył). Dla każdego pominięcia notujemy:
- jaki PRE-SYGNAŁ istniał dzień wcześniej (insider / wolumen / kontrakt / IPO / FDA…),
- LEKCJĘ: jak złapać podobny wzorzec następnym razem.
Lekcje trafiają do trwałego dziennika (lowca_lessons.json) — łowca czyta je przy
kolejnym polowaniu, żeby nie powtarzać błędów.

Pliki (w repo):
- lowca_flagged_log.json : [{date, flagged:[tickery rozważane danego dnia]}]
- lowca_lessons.json     : [{date, ticker, pct, presignal, lesson}]

Agent dostarcza lowca_movers.json: [{ticker, pct, day, presignal, lesson}] —
największe jednodniowe wzrosty z wczoraj (web_search) + jego analiza pre-sygnału.
"""
from __future__ import annotations
import json


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d
    except Exception:
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def record_flagged(date, tickers, path="lowca_flagged_log.json", keep=45):
    """Zapisuje tickery, które łowca dziś ROZWAŻAŁ (do późniejszej analizy pominięć).
    Zastępuje wpis z danego dnia (idempotentne). Trzyma ostatnie `keep` dni."""
    log = _load_json(path, [])
    if not isinstance(log, list):
        log = []
    log = [e for e in log if e.get("date") != date]
    log.append({"date": date, "flagged": sorted({str(t).upper() for t in (tickers or []) if t})})
    log = log[-keep:]
    _save_json(path, log)
    return log


def flagged_set(log, days=None):
    """Zbiór wszystkich tickerów rozważanych (cały log lub ostatnie `days` wpisów)."""
    if not isinstance(log, list):
        return set()
    entries = log[-days:] if days else log
    out = set()
    for e in entries:
        for t in (e.get("flagged") or []):
            out.add(str(t).upper())
    return out


BOT_START = "2026-05-28"   # PIERWSZY DZIEN PRACY bota. Nie analizuj ruchow sprzed — bot wtedy nie istnial
                            # (zeby nie pisac "byla okazja 1000% w marcu"). To NIE byly jego okazje.

_MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
           "sty":1,"lut":2,"mar":3,"kwi":4,"maj":5,"cze":6,"lip":7,"sie":8,"wrz":9,"paz":10,"lis":11,"gru":12}


def _parse_day(s):
    """Loose parser daty ruchu: ISO 'YYYY-MM-DD' albo 'June 5 2026'/'March 24'. None gdy nie wiadomo."""
    import re, datetime
    s = str(s or "").strip().lower()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try: return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception: return None
    mm = re.search(r"([a-z]{3,})\.?\s+(\d{1,2})(?:[,\s]+(\d{4}))?", s)
    if mm:
        mon = _MONTHS.get(mm.group(1)[:3])
        if mon:
            try: return datetime.date(int(mm.group(3) or 2026), mon, int(mm.group(2)))
            except Exception: return None
    return None


def find_misses(movers, flagged, min_pct=15.0, start=BOT_START):
    """Duże jednodniowe wzrosty (>=min_pct%), których łowca NIE miał na radarze.
    FILTR DNIA STARTU: pomija ruchy sprzed pierwszego dnia pracy bota (nie były jego okazjami)."""
    flag = {str(t).upper() for t in (flagged or [])}
    out = []
    for m in (movers or []):
        if not isinstance(m, dict) or not m.get("ticker"):
            continue
        tk = str(m["ticker"]).upper()
        try:
            pct = float(m.get("pct", 0) or 0)
        except Exception:
            pct = 0.0
        day = _parse_day(m.get("day", ""))
        if day is not None and day.isoformat() < start:
            continue  # ruch sprzed startu bota — nie nasza okazja
        if pct >= min_pct and tk not in flag:
            out.append({"ticker": tk, "pct": round(pct, 1),
                        "day": m.get("day", ""), "presignal": m.get("presignal", "(nieznany)"),
                        "lesson": m.get("lesson", "(do uzupełnienia)")})
    out.sort(key=lambda m: m["pct"], reverse=True)
    return out


def append_lessons(misses, date, path="lowca_lessons.json", keep=300):
    """Dopisuje lekcje z dzisiejszych pominięć do dziennika (dedup po date+ticker)."""
    lessons = _load_json(path, [])
    if not isinstance(lessons, list):
        lessons = []
    have = {(l.get("date"), l.get("ticker")) for l in lessons}
    added = []
    for m in (misses or []):
        e = {"date": date, "ticker": str(m.get("ticker", "?")).upper(),
             "pct": round(float(m.get("pct", 0) or 0), 1),
             "presignal": m.get("presignal", "(nieznany)"),
             "lesson": m.get("lesson", "(do uzupełnienia)")}
        k = (e["date"], e["ticker"])
        if k not in have:
            lessons.append(e); have.add(k); added.append(e)
    lessons = lessons[-keep:]
    _save_json(path, lessons)
    return lessons, added


def recent(lessons, n=8):
    return (lessons or [])[-n:]


def patterns_summary(lessons, top=5):
    """Najczęstsze typy pre-sygnałów w pominięciach — czego łowca najczęściej nie łapie."""
    cats = {"insider": ["insider", "10b5", "form 4"], "kontrakt": ["kontrakt", "contract", "8-k", "umowa"],
            "wolumen": ["wolumen", "volume", "breakout", "wybicie"], "ipo": ["ipo", "debiut"],
            "FDA/biotech": ["fda", "trial", "phase", "approval", "biotech"], "earnings": ["earnings", "wyniki", "guidance"],
            "przejęcie": ["przejęcie", "buyout", "merger", "acqui"], "short-squeeze": ["squeeze", "short interest"]}
    counts = {}
    for l in (lessons or []):
        ps = (str(l.get("presignal", "")) + " " + str(l.get("lesson", ""))).lower()
        for cat, keys in cats.items():
            if any(k in ps for k in keys):
                counts[cat] = counts.get(cat, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]


def process(today, flagged_tickers, movers_path="lowca_movers.json",
            lessons_path="lowca_lessons.json", flaglog_path="lowca_flagged_log.json",
            min_pct=15.0):
    """Pełny krok nauki: zapisz dzisiejsze rozważane, znajdź wczorajsze pominięcia
    (z lowca_movers.json), dopisz lekcje. Zwraca dict gotowy do renderu."""
    log = record_flagged(today, flagged_tickers, flaglog_path)
    movers = _load_json(movers_path, [])
    if not isinstance(movers, list):
        movers = []
    misses = find_misses(movers, flagged_set(log), min_pct=min_pct)
    lessons, added = append_lessons(misses, today, lessons_path)
    return {"misses_today": misses, "added": added, "lessons": lessons,
            "recent": recent(lessons, 8), "patterns": patterns_summary(lessons)}


def render_lessons_html(info: dict) -> str:
    """Karta do maila: 'Czego się uczę' — dzisiejsze pominięcia + dziennik + wzorce.
    Styl bilansu (import z email_render); fallback prosty."""
    try:
        from email_render import (GOLD_DK, GREEN, RED, MUTED, TEXT, DARK, LINE2, SANS, _card_open)
    except Exception:
        DARK = "#0f172a"; TEXT = "#334155"; MUTED = "#64748b"; RED = "#b91c1c"; GREEN = "#15803d"
        GOLD_DK = "#b8860b"; LINE2 = "#eef1f5"
        SANS = "font-family:-apple-system,Arial,Helvetica,sans-serif;"
        def _card_open(t, s="", **k): return f"<div style='background:#fff;border-radius:14px;padding:24px 26px;margin-bottom:18px;border-top:5px solid #d4af37;'><div style='{SANS}font-weight:bold;text-transform:uppercase;color:{MUTED};margin-bottom:8px;'>{t}</div><div style='{SANS}color:{MUTED};margin-bottom:12px;'>{s}</div>"

    misses = info.get("misses_today") or []
    recent_l = info.get("recent") or []
    pats = info.get("patterns") or []
    H = [_card_open("Czego się uczę — pominięcia i lekcje",
                    "Łowca sprawdza, co mocno urosło wczoraj, a co przeoczył — i notuje lekcje na przyszłość.")]
    if misses:
        H.append(f"<div style='{SANS}font-size:11.5pt;color:{RED};font-weight:bold;margin-bottom:8px;'>"
                 f"Wczoraj przeoczone (duży 1-dniowy ruch):</div>")
        for m in misses[:5]:
            H.append(f"<div style='{SANS}font-size:12pt;color:{TEXT};line-height:1.55;border-left:3px solid {RED};"
                     f"padding:8px 12px;margin-bottom:8px;background:#fff7f7;border-radius:6px;'>"
                     f"<b style='color:{DARK};'>{m['ticker']}</b> <b style='color:{GREEN};'>+{m['pct']:.0f}%</b>"
                     f" — pre-sygnał: {m.get('presignal','?')}<br>"
                     f"<span style='color:{GOLD_DK};'>Lekcja:</span> {m.get('lesson','?')}</div>")
    else:
        H.append(f"<div style='{SANS}font-size:12pt;color:{GREEN};margin-bottom:8px;'>"
                 f"Wczoraj brak dużych ruchów, które bym przeoczył (albo dane niedostępne). Dobra robota.</div>")
    if recent_l:
        H.append(f"<div style='{SANS}font-size:10.5pt;text-transform:uppercase;letter-spacing:1px;color:{MUTED};"
                 f"font-weight:bold;margin:14px 0 8px;'>Dziennik lekcji (ostatnie):</div>")
        rows = ""
        for l in reversed(recent_l):
            rows += (f"<tr><td style='padding:7px 8px 7px 0;border-bottom:1px solid {LINE2};{SANS}font-size:11pt;color:{MUTED};white-space:nowrap;'>{l.get('date','')}</td>"
                     f"<td style='padding:7px 8px;border-bottom:1px solid {LINE2};{SANS}font-size:11.5pt;'><b style='color:{DARK};'>{l.get('ticker','')}</b> +{l.get('pct',0):.0f}%</td>"
                     f"<td style='padding:7px 0;border-bottom:1px solid {LINE2};{SANS}font-size:11pt;color:{TEXT};'>{l.get('lesson','')}</td></tr>")
        H.append(f"<table style='width:100%;border-collapse:collapse;'>{rows}</table>")
    if pats:
        txt = ", ".join(f"{c} ({n}×)" for c, n in pats)
        H.append(f"<div style='{SANS}font-size:11pt;color:{MUTED};margin-top:12px;border-top:1px solid {LINE2};padding-top:10px;'>"
                 f"<b>Najczęściej mi umyka:</b> {txt}. Na to zwracam większą uwagę przy polowaniu.</div>")
    H.append("</div>")
    return "".join(H)


# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    import sys, os, tempfile
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=== SELFTEST lowca_misses (pętla uczenia) ===")
    P = F = 0
    def ok(n, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")

    d = tempfile.mkdtemp()
    flog = os.path.join(d, "flag.json"); less = os.path.join(d, "less.json"); mov = os.path.join(d, "mov.json")

    # record_flagged
    log = record_flagged("2026-06-03", ["aaa", "BBB"], flog)
    ok("record_flagged zapisuje", len(log) == 1 and "AAA" in log[0]["flagged"])
    log = record_flagged("2026-06-03", ["AAA", "CCC"], flog)  # ten sam dzień -> zastąp
    ok("record_flagged zastępuje wpis z dnia", len(log) == 1 and "CCC" in log[0]["flagged"])
    log = record_flagged("2026-06-04", ["DDD"], flog)
    ok("record_flagged dokłada nowy dzień", len(log) == 2)
    ok("flagged_set łączy dni", flagged_set(log) == {"AAA", "CCC", "DDD"})

    # find_misses: NVDA urosło +30%, nie było flagowane -> miss; CCC było flagowane -> nie miss; XYZ +5% -> za mało
    movers = [{"ticker": "NVDA", "pct": 30, "presignal": "insider 2d", "lesson": "śledź insiderów half-cap"},
              {"ticker": "ccc", "pct": 40, "presignal": "x", "lesson": "y"},
              {"ticker": "XYZ", "pct": 5, "presignal": "z", "lesson": "w"}]
    misses = find_misses(movers, flagged_set(log), min_pct=15)
    ok("find_misses łapie NVDA (przeoczone +30%)", any(m["ticker"] == "NVDA" for m in misses))
    ok("find_misses pomija CCC (było flagowane)", not any(m["ticker"] == "CCC" for m in misses))
    ok("find_misses pomija mały ruch XYZ", not any(m["ticker"] == "XYZ" for m in misses))

    # append_lessons + dedup
    lessons, added = append_lessons(misses, "2026-06-04", less)
    ok("append_lessons dodaje lekcję", any(l["ticker"] == "NVDA" for l in lessons) and len(added) == 1)
    lessons2, added2 = append_lessons(misses, "2026-06-04", less)  # ten sam dzień+ticker -> brak duplikatu
    ok("append_lessons dedup (brak duplikatu)", len(added2) == 0 and len(lessons2) == len(lessons))

    # patterns_summary
    pats = patterns_summary(lessons)
    ok("patterns_summary wykrywa kategorię insider", any(c == "insider" for c, _ in pats))

    # process end-to-end
    _save_json(mov, [{"ticker": "TSLA", "pct": 22, "presignal": "kontrakt 8-K", "lesson": "small-cap kontrakt >50% mcap"},
                     {"ticker": "DDD", "pct": 25, "presignal": "x", "lesson": "y"}])  # DDD było flagowane -> nie miss
    info = process("2026-06-05", ["EEE"], movers_path=mov, lessons_path=less, flaglog_path=flog)
    ok("process: TSLA jako pominięcie", any(m["ticker"] == "TSLA" for m in info["misses_today"]))
    ok("process: DDD nie jest pominięciem (było flagowane wcześniej)", not any(m["ticker"] == "DDD" for m in info["misses_today"]))
    ok("process zwraca recent + patterns", isinstance(info["recent"], list) and isinstance(info["patterns"], list))

    # brak pliku movers -> 0 pominięć, brak crasha
    info2 = process("2026-06-06", ["FFF"], movers_path=os.path.join(d, "brak.json"), lessons_path=less, flaglog_path=flog)
    ok("Brak movers -> 0 pominięć, brak crasha", info2["misses_today"] == [])

    # render_lessons_html
    try:
        html = render_lessons_html(info)
        ok("render_lessons_html bez błędu", True)
        ok("HTML ma sekcję nauki", "Czego się uczę" in html)
        ok("HTML pokazuje pominięcie TSLA", "TSLA" in html)
    except Exception as e:
        ok(f"render_lessons_html bez błędu ({e})", False)

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== lowca_misses.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_run_selftest() if "--selftest" in sys.argv else 0)
