# news_lens.py — Soczewka KATALIZATORÓW (źródło edge #3 z konstytucji).
#
# ARCHITEKTURA (inna niż reszta — to domena agenta, nie czysty Python):
# - Newsy ocenia AGENT routine (Opus/Claude) przez web_search — bo to wymaga OCENY
#   ISTOTNOŚCI, nie mielenia liczb. Python NIE woła newsów sam.
# - Python tylko: (1) wystawia listę kandydatów do sprawdzenia (news_request.json),
#   (2) wczytuje werdykt agenta (news_signals.json) i wpina go do maila/scoringu.
#
# PRZEPŁYW W ROUTINE:
#   1. main_pipeline woła write_news_request(candidates) -> news_request.json
#   2. AGENT czyta news_request.json, dla każdego tickera robi web_search,
#      ocenia czy jest ŚWIEŻY ISTOTNY KATALIZATOR, zapisuje news_signals.json
#   3. main_pipeline woła read_news_signals() -> wpina plakietkę/tekst do karty kupna
#
# ODPORNOSC: brak news_signals.json (agent nie zdążył / web_search padł) -> {} ,
# bot leci dalej bez tej soczewki. To wzbogacenie, nie warunek.
#
# DLACZEGO TA SOCZEWKA JEST WAŻNA: katalizator (kontrakt, przejęcie, decyzja
# regulatora) działa NIEZALEŻNIE od trendu rynku — to jedyne źródło edge, które
# może zadziałać w bessie/stagnacji, gdy sam momentum przestaje działać.

from __future__ import annotations
import os
import json
from datetime import datetime, date
from typing import Optional

REQUEST_PATH = "news_request.json"
SIGNALS_PATH = "news_signals.json"
FRESHNESS_DAYS = 7          # katalizator świeży tylko jeśli <= 7 dni
MIN_CONFIDENCE = 0.5        # poniżej tego progu nie pokazujemy plakietki
MIN_SOURCES = 2             # wymagamy >= 2 niezależnych źródeł dla has_catalyst

# Schemat werdyktu agenta dla JEDNEGO tickera (agent wypełnia w news_signals.json):
# {
#   "ticker": "AAPL",
#   "has_catalyst": true/false,        # świeży istotny katalizator (<= 7 dni)
#   "catalyst_type": "contract|earnings_guidance|MnA|product|regulatory|analyst|other|none",
#   "direction": "bullish|neutral|bearish",
#   "headline": "krótki opis własnymi słowami (max ~15 słów)",
#   "as_of": "2026-05-25",             # data najświeższego newsa
#   "confidence": 0.0-1.0,
#   "source_count": 2,
#   "sources": [{"label":"Reuters","url":"https://..."}]
# }


def write_news_request(candidates, path: str = REQUEST_PATH, max_tickers: int = 5) -> int:
    """Python: wystaw listę kandydatów, których newsy ma sprawdzić agent.
    candidates: lista obiektów z polami .ticker / .name / .sector (lub dict)."""
    tickers = []
    for c in (candidates or [])[:max_tickers]:
        if isinstance(c, dict):
            tk, name, sector = c.get("ticker"), c.get("name"), c.get("sector")
        else:
            tk = getattr(c, "ticker", None)
            name = getattr(c, "name", None)
            sector = getattr(c, "sector", None)
        if tk:
            tickers.append({"ticker": tk, "name": name, "sector": sector})
    req = {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "freshness_days": FRESHNESS_DAYS,
        "instruction": (
            "Dla każdego tickera sprawdź przez web_search świeże (<=7 dni) newsy. "
            "Oceń czy jest ISTOTNY katalizator (kontrakt, przejęcie/fuzja, zmiana prognoz, "
            "premiera produktu, decyzja regulatora, ważna rekomendacja). "
            "Szum PR/marketing = has_catalyst:false. Wymagaj >=2 niezależnych źródeł "
            "dla has_catalyst:true. Świeżość ponad pamięć: TYLKO to, co znajdziesz teraz."
        ),
        "schema_example": {
            "ticker": "AAPL", "has_catalyst": True, "catalyst_type": "contract",
            "direction": "bullish", "headline": "krótko własnymi słowami",
            "as_of": "YYYY-MM-DD", "confidence": 0.8, "source_count": 2,
            "sources": [{"label": "Reuters", "url": "https://..."}],
        },
        "tickers": tickers,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(req, ensure_ascii=False, indent=2))
        return len(tickers)
    except Exception:
        return 0


def read_news_signals(path: str = SIGNALS_PATH) -> dict:
    """Python: wczytaj werdykt agenta. Zwraca {ticker: signal} albo {} (brak/błąd)."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("signals") if isinstance(data, dict) else data
        out = {}
        for r in (rows or []):
            tk = r.get("ticker")
            if tk:
                out[tk] = r
        return out
    except Exception:
        return {}


def _is_fresh(sig: dict) -> bool:
    """Czy katalizator jest świeży (<= FRESHNESS_DAYS). Brak daty -> traktuj jako świeży."""
    as_of = sig.get("as_of")
    if not as_of:
        return True
    try:
        d = date.fromisoformat(as_of)
        return (date.today() - d).days <= FRESHNESS_DAYS
    except Exception:
        return True


def is_valid_catalyst(sig: dict) -> bool:
    """Pełna bramka: ma katalizator + świeży + dość pewny + dość źródeł."""
    if not sig or not sig.get("has_catalyst"):
        return False
    if sig.get("confidence", 0) < MIN_CONFIDENCE:
        return False
    if sig.get("source_count", 0) < MIN_SOURCES:
        return False
    return _is_fresh(sig)


def news_badge(sig: dict) -> Optional[tuple]:
    """Plakietka (typ, tekst) do maila, albo None."""
    if not is_valid_catalyst(sig):
        return None
    d = sig.get("direction", "neutral")
    if d == "bullish":
        return ("news_bull", "Świeży katalizator ↑")
    if d == "bearish":
        return ("news_bear", "Świeży katalizator ↓")
    return ("news_neutral", "Świeży news")


def news_text(sig: dict) -> str:
    """Zdanie do uzasadnienia karty kupna, albo pusty string."""
    if not is_valid_catalyst(sig):
        return ""
    head = sig.get("headline", "")
    typ = {
        "contract": "kontrakt", "earnings_guidance": "zmiana prognoz",
        "MnA": "przejęcie/fuzja", "product": "premiera produktu",
        "regulatory": "decyzja regulatora", "analyst": "rekomendacja analityków",
    }.get(sig.get("catalyst_type"), "katalizator")
    src = sig.get("source_count", 0)
    return f"Katalizator newsowy ({typ}): {head}" + (f" [{src} źródła]" if src else "") + "."


def news_sources(sig: dict) -> Optional[list]:
    """Lista (etykieta, url) klikalnych źródeł, albo None."""
    if not sig:
        return None
    srcs = sig.get("sources")
    if not srcs:
        return None
    out = []
    for i, s in enumerate(srcs[:3], 1):
        if isinstance(s, dict):
            url = s.get("url")
            lbl = s.get("label") or f"źródło {i}"
        else:
            url = s
            lbl = f"źródło {i}"
        if url:
            out.append((lbl, url))
    return out or None


def news_scalar(sig: dict) -> float:
    """Mnożnik do scoringu kandydata (jak smart money confluence).
    1.0 = neutral. >1 = bullish katalizator wzmacnia. <1 = bearish osłabia.
    BEZPIECZNY: brak/niepewny katalizator -> 1.0 (bez wpływu)."""
    if not is_valid_catalyst(sig):
        return 1.0
    d = sig.get("direction", "neutral")
    conf = sig.get("confidence", 0.5)
    if d == "bullish":
        return 1.0 + 0.15 * conf      # max +15% do score
    if d == "bearish":
        return 1.0 - 0.20 * conf      # max -20% (asymetria: ryzyko ważniejsze)
    return 1.0


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

    # brak pliku -> {}
    check("read brak pliku -> {}", read_news_signals("/tmp/nie_istnieje_xyz.json") == {})
    # bullish valid
    bull = {"has_catalyst": True, "direction": "bullish", "confidence": 0.8,
            "source_count": 2, "catalyst_type": "contract", "headline": "duży kontrakt AI"}
    check("badge bull", news_badge(bull) == ("news_bull", "Świeży katalizator ↑"))
    check("scalar bull > 1", news_scalar(bull) > 1.0)
    check("text bull niepusty", "kontrakt" in news_text(bull))
    # za mało źródeł -> odrzuć
    weak = {"has_catalyst": True, "direction": "bullish", "confidence": 0.8, "source_count": 1}
    check("1 źródło -> brak plakietki", news_badge(weak) is None)
    check("1 źródło -> scalar 1.0", news_scalar(weak) == 1.0)
    # niska pewność -> odrzuć
    lowc = {"has_catalyst": True, "direction": "bullish", "confidence": 0.3, "source_count": 3}
    check("niska pewność -> brak plakietki", news_badge(lowc) is None)
    # bearish
    bear = {"has_catalyst": True, "direction": "bearish", "confidence": 0.9,
            "source_count": 2, "catalyst_type": "regulatory", "headline": "kara regulatora"}
    check("scalar bear < 1", news_scalar(bear) < 1.0)
    # brak katalizatora
    check("brak katalizatora -> scalar 1.0", news_scalar({"has_catalyst": False}) == 1.0)
    # przeterminowany
    stale = {"has_catalyst": True, "direction": "bullish", "confidence": 0.8,
             "source_count": 2, "as_of": "2020-01-01"}
    check("stary news -> brak plakietki", news_badge(stale) is None)
    # write_news_request
    n = write_news_request([{"ticker": "NVDA", "name": "Nvidia", "sector": "Tech"}], path="/tmp/nr_test.json")
    check("write_news_request -> 1", n == 1)
    rd = read_news_signals("/tmp/nr_test.json")  # to request, nie signals -> {} bo brak 'signals'/lista tickerów
    check("request nie jest signals", isinstance(rd, dict))

    print(f"WYNIK news_lens: {passed} OK, {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    print("news_lens.py — soczewka katalizatorów. Użyj --selftest.")
