# pead_lens.py — Soczewka PEAD TEKSTOWY (źródło edge #2 z konstytucji).
#
# ROLA: klasyczny PEAD (sam "beat o X%") spadł do ~0, ale TEKSTOWY (interpretacja
# tonu zarządu, guidance, surprise drivers z transkryptu) wciąż żyje (research 2025/26).
# To naturalna przewaga modelu językowego — czyta transkrypt jak analityk.
#
# ARCHITEKTURA: Gemini = tani long-context summarizer (czyta cały transkrypt do 1M
# tokenów, zwraca esencję w JSON). Werdykt/scoring po stronie bota.
#
# ZASADA ODPORNOSCI: brak GEMINI_API_KEY / brak FMP / płatny endpoint / błąd -> None.
# Bot MUSI działać dalej bez PEAD (to wzbogacenie, nie warunek).
#
# KOSZT: gemini flash ~$1.50/M wej. Transkrypt ~80k tokenów = ~$0.16/spółkę.
# Wołamy TYLKO dla spółek z portfela/kandydatów ze świeżymi wynikami (<= ~7 dni).

from __future__ import annotations
import os
import json
from typing import Optional

GEMINI_MODEL = "gemini-2.0-flash"   # tani, szybki; zmień jeśli masz dostęp do nowszego
FMP_TRANSCRIPT_URL = "https://financialmodelingprep.com/stable/earnings-transcript"
MIN_CONFIDENCE = 0.4
MAX_TRANSCRIPT_CHARS = 600_000      # bezpiecznik kosztowy

PEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment_score": {"type": "number"},
        "guidance_change": {"type": "string", "enum": ["raised", "maintained", "lowered", "none"]},
        "surprise_drivers": {"type": "array", "items": {"type": "string"}},
        "management_tone": {"type": "string", "enum": ["confident", "neutral", "cautious", "defensive"]},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "pead_signal": {"type": "string", "enum": ["bullish_drift", "neutral", "bearish_drift"]},
        "confidence": {"type": "number"},
    },
    "required": ["sentiment_score", "guidance_change", "management_tone", "pead_signal", "confidence"],
}

PROMPT = """Jesteś analitykiem finansowym. Przeczytaj transkrypt konferencji wynikowej spółki {ticker}.
Oceń WYŁĄCZNIE na podstawie tekstu (nie zgaduj, nie używaj wiedzy zewnętrznej):
- ton zarządu (pewny vs defensywny),
- czy prognozy (guidance) podniesione / utrzymane / obniżone,
- główne czynniki zaskoczenia i sygnały ryzyka,
- kierunek spodziewanego dryfu po wynikach (PEAD).
Reguła: pozytywny pewny ton + podniesione prognozy = bullish_drift;
defensywny ton + obniżone prognozy = bearish_drift.
Niejasny/niepełny tekst -> confidence nisko, pead_signal=neutral.

TRANSKRYPT:
{transcript}"""


def fetch_transcript(ticker: str, fmp_key: Optional[str] = None,
                     year: Optional[int] = None, quarter: Optional[int] = None) -> Optional[str]:
    """Pobiera najnowszy transkrypt z FMP. None gdy brak klucza/płatny/brak/błąd.
    ODPORNOSC: każdy błąd -> None, bot leci dalej."""
    fmp_key = fmp_key or os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        return None
    try:
        import requests
        params = {"symbol": ticker, "apikey": fmp_key}
        if year:
            params["year"] = year
        if quarter:
            params["quarter"] = quarter
        r = requests.get(FMP_TRANSCRIPT_URL, params=params, timeout=25)
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        rec = data[0]
        return rec.get("content") or rec.get("transcript") or None
    except Exception:
        return None


def analyze_transcript(ticker: str, transcript: Optional[str],
                       api_key: Optional[str] = None,
                       model: str = GEMINI_MODEL) -> Optional[dict]:
    """Mieli transkrypt przez Gemini -> dict wg PEAD_SCHEMA. None gdy brak klucza/pusty.
    Błąd Gemini -> {'_error':...} (sygnalizuje brak, nie wywala bota)."""
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not transcript or len(transcript.strip()) < 200:
        return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        txt = transcript[:MAX_TRANSCRIPT_CHARS]
        resp = client.models.generate_content(
            model=model,
            contents=PROMPT.format(ticker=ticker, transcript=txt),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PEAD_SCHEMA,
                temperature=0.0,
            ),
        )
        data = json.loads(resp.text)
        data["_ticker"] = ticker
        data["_model"] = model
        return data
    except Exception as e:
        return {"_error": str(e), "_ticker": ticker}


def pead_badge(pead: Optional[dict]) -> Optional[tuple]:
    """Plakietka (typ, tekst) do maila, albo None."""
    if not pead or pead.get("_error") or "pead_signal" not in pead:
        return None
    sig = pead["pead_signal"]
    conf = pead.get("confidence", 0)
    if conf < MIN_CONFIDENCE:
        return None
    if sig == "bullish_drift":
        return ("pead_bull", f"PEAD: dryf ↑ ({conf*100:.0f}%)")
    if sig == "bearish_drift":
        return ("pead_bear", f"PEAD: dryf ↓ ({conf*100:.0f}%)")
    return None


def pead_text(pead: Optional[dict]) -> str:
    """Zdanie do uzasadnienia, albo pusty string."""
    if not pead or pead.get("_error") or "pead_signal" not in pead:
        return ""
    if pead.get("confidence", 0) < MIN_CONFIDENCE:
        return ""
    tone = {"confident": "pewny", "neutral": "neutralny", "cautious": "ostrożny",
            "defensive": "defensywny"}.get(pead.get("management_tone"), "")
    guid = {"raised": "podniósł prognozy", "lowered": "obniżył prognozy",
            "maintained": "utrzymał prognozy", "none": ""}.get(pead.get("guidance_change"), "")
    drift = {"bullish_drift": "spodziewany dryf w górę", "bearish_drift": "spodziewany dryf w dół",
             "neutral": "neutralny dryf"}.get(pead.get("pead_signal"), "")
    bits = [b for b in (f"ton {tone}" if tone else "", guid, drift) if b]
    return "PEAD (transkrypt): " + ", ".join(bits) + "." if bits else ""


def pead_scalar(pead: Optional[dict]) -> float:
    """Mnożnik do scoringu. 1.0 = neutral. BEZPIECZNY: brak/błąd/niepewne -> 1.0."""
    if not pead or pead.get("_error") or "pead_signal" not in pead:
        return 1.0
    conf = pead.get("confidence", 0)
    if conf < MIN_CONFIDENCE:
        return 1.0
    sig = pead["pead_signal"]
    if sig == "bullish_drift":
        return 1.0 + 0.15 * conf
    if sig == "bearish_drift":
        return 1.0 - 0.20 * conf
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

    # brak klucza -> None (odporność)
    check("brak klucza -> None", analyze_transcript("TEST", "x" * 300, api_key="") is None)
    check("pusty transkrypt -> None", analyze_transcript("TEST", "", api_key="fake") is None)
    # plakietki
    check("badge bull", pead_badge({"pead_signal": "bullish_drift", "confidence": 0.8})[0] == "pead_bull")
    check("badge bear", pead_badge({"pead_signal": "bearish_drift", "confidence": 0.7})[0] == "pead_bear")
    check("badge low-conf -> None", pead_badge({"pead_signal": "bullish_drift", "confidence": 0.2}) is None)
    check("badge error -> None", pead_badge({"_error": "x", "_ticker": "T"}) is None)
    # scalary
    check("scalar bull > 1", pead_scalar({"pead_signal": "bullish_drift", "confidence": 0.8}) > 1.0)
    check("scalar bear < 1", pead_scalar({"pead_signal": "bearish_drift", "confidence": 0.8}) < 1.0)
    check("scalar error -> 1.0", pead_scalar({"_error": "x"}) == 1.0)
    check("scalar None -> 1.0", pead_scalar(None) == 1.0)
    # tekst
    t = pead_text({"pead_signal": "bullish_drift", "confidence": 0.8,
                   "management_tone": "confident", "guidance_change": "raised"})
    check("text bull niepusty", "dryf w górę" in t)
    check("schema wymagane pola", set(PEAD_SCHEMA["required"]) >= {"pead_signal", "confidence"})

    print(f"WYNIK pead_lens: {passed} OK, {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    print("pead_lens.py — soczewka PEAD tekstowy. Użyj --selftest.")
