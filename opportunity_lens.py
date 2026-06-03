# opportunity_lens.py — RADAR OKAZJI ULTRA. Wyłapuje spółki PRZED dużym ruchem w górę
# (potencjalne multibaggery): świeże IPO z momentum, nietypowy wolumen/wybicie, oraz
# małe spółki z kontraktem przewyższającym kapitalizację (ruch transformacyjny).
#
# FILOZOFIA (barbell Taleba): rdzeń portfela (~91%) pracuje bezpiecznie w sekcji I maila.
# Radar Okazji to loteryjne zakłady z konweksją — ograniczony downside (akceptujesz -100%
# pozycji ~43 zł), nieograniczony upside. WIĘKSZOŚĆ moonshotów zeruje — to wpisana cena.
#
# KLUCZOWE ZASADY (z analizy 2026-06-03):
# - Radar = OBSERWUJ, nie auto-kup. Bot wyłapuje i prezentuje, CZŁOWIEK decyduje.
# - Nigdy nie sugeruje wejścia PRZED publikacją newsa (to byłby insider trading).
#   Bot łapie pierwsze godziny PO publikacji 8-K/newsa, nie przed.
# - Filtr cap > $300M odsiewa najgorsze pump-and-dump microcap.
# - IPO grać TYLKO momentum + twardy stop (IPO przegrywają długoterminowo: 34% vs 62%).
# - XTB min €10 (~43 zł) — moonshot od 43 zł, nie mniej.
#
# ODPORNOŚĆ: brak danych / pusty plik / błąd -> pusty radar, bot leci dalej. Nigdy nie wywala.
#
# Agent routine wypełnia opportunity_signals.json przez web_search (IPO calendar z Finnhub,
# nietypowy wolumen, kontrakty z SEC 8-K / USAspending). Ten moduł filtruje i ocenia.

from __future__ import annotations
import os
import json
from datetime import datetime, timezone, date
from typing import Optional

SIGNALS_PATH = "opportunity_signals.json"
REQUEST_PATH = "opportunity_request.json"

# ── Progi (z analizy) ──
IPO_MAX_AGE_MONTHS = 18          # "świeże IPO" = listing < 18 miesięcy
VOLUME_SPIKE_MULT = 3.0          # nietypowy wolumen = > 3× średnia 20-dniowa
CONTRACT_RATIO_MIN = 0.50        # kontrakt ≥ 50% market cap = ruch transformacyjny
CAP_FLOOR_USD = 300_000_000      # < $300M = za duże ryzyko pump-and-dump, odsiewamy
CAP_CEIL_USD = 5_000_000_000     # > $5B = kontrakt rzadko jest transformacyjny
NEWS_MAX_AGE_HOURS = 48          # katalizator świeży = publikacja < 48h temu


def write_opportunity_request(path: str = REQUEST_PATH) -> bool:
    """Zapisuje prośbę dla agenta: jakie kategorie okazji przeskanować.
    Agent czyta to i wypełnia opportunity_signals.json przez web_search."""
    try:
        req = {
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scan": [
                {"type": "ipo", "query": "recent IPO listings last 18 months momentum, Finnhub IPO calendar",
                 "want": "ticker, ipo_date, pct_from_ipo, volume_vs_avg"},
                {"type": "volume", "query": "unusual volume stocks today, volume > 3x average, breakout 3-month high",
                 "want": "ticker, volume_mult, pct_today, broke_high"},
                {"type": "contract", "query": "small cap company large government contract SEC 8-K, USAspending defense space nuclear AI",
                 "want": "ticker, contract_usd, market_cap_usd, source, hours_ago"},
                {"type": "theme", "query": "hottest sector leaders momentum 20 days nuclear AI space defense",
                 "want": "ticker, theme, pct_20d"},
                {"type": "lockup", "query": "IPO lockup expiry next 14 days insider supply risk",
                 "want": "ticker, lockup_date, days_until"},
            ],
            "rules": "Filtruj: cap > $300M. Newsy < 48h. Tylko PO publikacji (nie przed). "
                     "Etykieta OBSERWUJ. Nigdy nie sugeruj auto-kupna.",
        }
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(req, ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


def read_opportunity_signals(path: str = SIGNALS_PATH) -> dict:
    """Czyta sygnały okazji wypełnione przez agenta. Brak/błąd -> pusty."""
    try:
        if not os.path.exists(path):
            return {"_empty": True}
        data = json.loads(open(path, encoding="utf-8").read())
        if not isinstance(data, dict):
            return {"_empty": True}
        return data
    except Exception:
        return {"_empty": True}


def _hours_since(iso_or_hours) -> Optional[float]:
    """Akceptuje albo liczbę godzin, albo ISO timestamp. Zwraca godziny temu."""
    if iso_or_hours is None:
        return None
    if isinstance(iso_or_hours, (int, float)):
        return float(iso_or_hours)
    try:
        t = datetime.fromisoformat(str(iso_or_hours).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - t
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


def evaluate_ipo(items: list) -> list:
    """Świeże IPO z dodatnim momentum. Zwraca listę okazji z etykietą i ryzykiem."""
    out = []
    for it in items or []:
        try:
            tk = str(it.get("ticker", "")).strip().upper()
            if not tk:
                continue
            age_m = it.get("age_months")
            pct = float(it.get("pct_from_ipo", 0) or 0)
            volm = float(it.get("volume_mult", 1) or 1)
            # świeże + momentum dodatnie (IPO grać tylko z momentum, twardy stop)
            if age_m is not None and age_m > IPO_MAX_AGE_MONTHS:
                continue
            if pct <= 0:
                continue   # IPO bez momentum = zły zakład długoterminowo
            note = f"{age_m} mies. od IPO, {pct:+.0f}% od debiutu" if age_m else f"{pct:+.0f}% od debiutu"
            if volm >= 1.5:
                note += f", wolumen {volm:.1f}×"
            out.append({"ticker": tk, "kind": "IPO", "note": note,
                        "risk": "WYSOKIE", "label": "OBSERWUJ",
                        "warn": it.get("lockup_warn", "")})
        except Exception:
            continue
    return out


def evaluate_volume(items: list) -> list:
    """Nietypowy wolumen / wybicie ponad szczyt. Wymaga wolumenu > 3× średnia."""
    out = []
    for it in items or []:
        try:
            tk = str(it.get("ticker", "")).strip().upper()
            if not tk:
                continue
            volm = float(it.get("volume_mult", 0) or 0)
            if volm < VOLUME_SPIKE_MULT:
                continue
            pct = float(it.get("pct_today", 0) or 0)
            broke = it.get("broke_high", False)
            note = f"wolumen {volm:.1f}×, {pct:+.0f}% dziś"
            if broke:
                note += ", przebił 3-mies. szczyt"
            out.append({"ticker": tk, "kind": "WOLUMEN", "note": note,
                        "risk": "WYSOKIE", "label": "OBSERWUJ — sprawdź news"})
        except Exception:
            continue
    return out


def evaluate_contract(items: list) -> list:
    """Mała spółka + ogromny kontrakt. Klucz: kontrakt/market_cap ≥ 0.5 = transformacyjny.
    Filtr cap $300M-$5B. Tylko świeże (< 48h) i PO publikacji."""
    out = []
    for it in items or []:
        try:
            tk = str(it.get("ticker", "")).strip().upper()
            if not tk:
                continue
            contract = float(it.get("contract_usd", 0) or 0)
            cap = float(it.get("market_cap_usd", 0) or 0)
            if cap <= 0 or contract <= 0:
                continue
            # filtr kapitalizacji: odsiej pump-and-dump (za małe) i nietransformacyjne (za duże)
            if cap < CAP_FLOOR_USD or cap > CAP_CEIL_USD:
                continue
            ratio = contract / cap
            if ratio < CONTRACT_RATIO_MIN:
                continue
            # świeżość: tylko po publikacji, < 48h
            hrs = _hours_since(it.get("hours_ago") or it.get("published_utc"))
            if hrs is not None and hrs > NEWS_MAX_AGE_HOURS:
                continue
            cap_b = cap / 1e9
            con_m = contract / 1e6
            note = (f"kapitalizacja {cap_b:.1f}B, kontrakt {con_m:.0f}M "
                    f"({ratio*100:.0f}% cap!)")
            src = it.get("source", "")
            if src:
                note += f" · {src}"
            risk = "BARDZO WYSOKIE (chase!)" if (hrs is not None and hrs < 6) else "BARDZO WYSOKIE"
            out.append({"ticker": tk, "kind": "KONTRAKT", "note": note,
                        "risk": risk, "label": "OBSERWUJ PILNIE", "ratio": round(ratio, 2)})
        except Exception:
            continue
    return out


def build_radar(signals: Optional[dict] = None) -> dict:
    """Główna funkcja: buduje radar okazji ze wszystkich kategorii.
    Zwraca dict z listami per kategoria + płaską listą do maila. Fail-safe."""
    if signals is None:
        signals = read_opportunity_signals()
    if not signals or signals.get("_empty"):
        return {"_empty": True, "ipo": [], "volume": [], "contract": [],
                "theme": [], "lockup": [], "all": []}
    try:
        ipo = evaluate_ipo(signals.get("ipo", []))
        vol = evaluate_volume(signals.get("volume", []))
        con = evaluate_contract(signals.get("contract", []))
        theme = signals.get("theme", []) or []
        lockup = signals.get("lockup", []) or []
        # kontrakty na górę (najmocniejszy sygnał), potem wolumen, potem IPO
        all_opps = con + vol + ipo
        return {"_empty": not all_opps, "ipo": ipo, "volume": vol, "contract": con,
                "theme": theme, "lockup": lockup, "all": all_opps}
    except Exception:
        return {"_empty": True, "ipo": [], "volume": [], "contract": [],
                "theme": [], "lockup": [], "all": []}


def radar_summary_text(radar: dict) -> str:
    """Krótkie podsumowanie tekstowe radaru (do logu/notatki)."""
    if not radar or radar.get("_empty"):
        return "Radar Okazji: brak sygnałów w tym cyklu"
    n = len(radar.get("all", []))
    parts = []
    if radar.get("contract"):
        parts.append(f"{len(radar['contract'])} kontrakt(y)")
    if radar.get("volume"):
        parts.append(f"{len(radar['volume'])} wolumen")
    if radar.get("ipo"):
        parts.append(f"{len(radar['ipo'])} IPO")
    return f"Radar Okazji: {n} okazji ({', '.join(parts)}) — OBSERWUJ"


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

    # IPO: świeże + momentum dodatnie przechodzi, stare/bez momentum odpada
    ipo = evaluate_ipo([
        {"ticker": "RDDT", "age_months": 8, "pct_from_ipo": 60, "volume_mult": 2.0},
        {"ticker": "OLD", "age_months": 30, "pct_from_ipo": 50},      # za stare
        {"ticker": "FLAT", "age_months": 5, "pct_from_ipo": -10},     # bez momentum
    ])
    check("IPO: tylko świeże z momentum (1 z 3)", len(ipo) == 1 and ipo[0]["ticker"] == "RDDT")
    check("IPO: etykieta OBSERWUJ", ipo[0]["label"] == "OBSERWUJ")

    # Wolumen: > 3× przechodzi, < 3× odpada
    vol = evaluate_volume([
        {"ticker": "XYZ", "volume_mult": 5.0, "pct_today": 18, "broke_high": True},
        {"ticker": "LOW", "volume_mult": 2.0, "pct_today": 5},        # za mały wolumen
    ])
    check("Wolumen: tylko > 3× (1 z 2)", len(vol) == 1 and vol[0]["ticker"] == "XYZ")
    check("Wolumen: nota o szczycie", "szczyt" in vol[0]["note"])

    # Kontrakt: ratio ≥ 0.5 + cap w zakresie przechodzi
    con = evaluate_contract([
        {"ticker": "OKLO", "contract_usd": 900e6, "market_cap_usd": 1.8e9,
         "source": "SEC 8-K", "hours_ago": 2},                       # ratio 0.5, cap OK -> OK
        {"ticker": "SMALL", "contract_usd": 100e6, "market_cap_usd": 100e6},  # cap < 300M -> odpada
        {"ticker": "BIG", "contract_usd": 1e9, "market_cap_usd": 50e9},       # ratio 0.02 -> odpada
        {"ticker": "OLD", "contract_usd": 500e6, "market_cap_usd": 800e6, "hours_ago": 200},  # za stare
    ])
    check("Kontrakt: tylko transformacyjny świeży (1 z 4)", len(con) == 1 and con[0]["ticker"] == "OKLO")
    check("Kontrakt: ratio policzone", con[0]["ratio"] == 0.5)
    check("Kontrakt: chase risk gdy < 6h", "chase" in con[0]["risk"])

    # build_radar: składa wszystko, kontrakty na górze
    radar = build_radar({
        "ipo": [{"ticker": "RDDT", "age_months": 8, "pct_from_ipo": 60}],
        "volume": [{"ticker": "XYZ", "volume_mult": 5.0, "pct_today": 18}],
        "contract": [{"ticker": "OKLO", "contract_usd": 900e6, "market_cap_usd": 1.8e9, "hours_ago": 2}],
    })
    check("build_radar: 3 okazje", len(radar["all"]) == 3)
    check("build_radar: kontrakt pierwszy (najmocniejszy)", radar["all"][0]["kind"] == "KONTRAKT")
    check("build_radar: nie pusty", not radar["_empty"])

    # pusty wkład -> pusty radar, nie crash
    empty = build_radar({"_empty": True})
    check("Pusty sygnał -> pusty radar", empty["_empty"] and empty["all"] == [])
    check("Pusty radar -> tekst informacyjny", "brak" in radar_summary_text(empty))

    # request się zapisuje
    ok = write_opportunity_request("/tmp/opp_req_test.json")
    check("write_opportunity_request działa", ok and os.path.exists("/tmp/opp_req_test.json"))

    # odporność: śmieci na wejściu nie wywalają
    try:
        build_radar({"ipo": "nie lista", "contract": None, "volume": 123})
        check("Śmieci na wejściu -> nie crash", True)
    except Exception:
        check("Śmieci na wejściu -> nie crash", False)

    print(f"WYNIK opportunity_lens: {passed} OK, {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    r = build_radar()
    print(radar_summary_text(r))
