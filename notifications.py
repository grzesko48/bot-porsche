"""
notifications.py — WYSYŁKA MAILA (Resend) + EARNINGS (Finnhub)
================================================================
Bot Porsche — 100% własny kod, ZERO importów ze starego marketbot.py.

Dwie niezależne funkcje, obie z biblioteki standardowej (urllib) — bez dodatkowych zależności:
  • send_email_resend(): wysyłka przez natywne Resend API (HTTP POST).
  • fetch_earnings_finnhub(): daty najbliższych wyników kwartalnych z darmowego Finnhub.

Klucze z ENV: RESEND_API_KEY, REPORT_SENDER, REPORT_RECIPIENT, FINNHUB_API_KEY.
Brak klucza / błąd sieci -> log + bezpieczna degradacja (nie wywala bota).

Uruchomienie testów: python notifications.py --selftest
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("porsche.notify")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


RESEND_ENDPOINT = "https://api.resend.com/emails"
FINNHUB_EARNINGS_ENDPOINT = "https://finnhub.io/api/v1/calendar/earnings"


# ─────────────────────────────────────────────────────────────────────────────
# WYSYŁKA MAILA — natywne Resend API (urllib, bez zależności)
# ─────────────────────────────────────────────────────────────────────────────
def send_email_resend(html: str, subject: str,
                      sender: Optional[str] = None,
                      recipient: Optional[str] = None,
                      api_key: Optional[str] = None,
                      dry_run: bool = False) -> dict:
    """Wysyła maila przez Resend. Zwraca {'ok':bool,'note':str,'id':str|None}.
    Brak klucza / dry_run -> nie wysyła, zwraca ok=False z notatką (bot leci dalej)."""
    api_key = api_key or os.environ.get("RESEND_API_KEY", "")
    sender = sender or os.environ.get("REPORT_SENDER", "Market Bot <onboarding@resend.dev>")
    recipient = recipient or os.environ.get("REPORT_RECIPIENT", "")

    if dry_run:
        logger.info("[DRY-RUN] mail NIE wysłany (tryb testowy).")
        return {"ok": False, "note": "dry_run — nie wysłano", "id": None}
    if not api_key:
        logger.warning("Brak RESEND_API_KEY — mail nie wysłany.")
        return {"ok": False, "note": "brak RESEND_API_KEY", "id": None}
    if not recipient:
        logger.warning("Brak REPORT_RECIPIENT — mail nie wysłany.")
        return {"ok": False, "note": "brak REPORT_RECIPIENT", "id": None}

    payload = json.dumps({
        "from": sender, "to": [recipient], "subject": subject, "html": html,
    }).encode("utf-8")
    req = urllib.request.Request(RESEND_ENDPOINT, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "curl/8.5.0")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            mid = data.get("id")
            logger.info("Mail wysłany przez Resend (id=%s).", mid)
            return {"ok": True, "note": f"HTTP {resp.status}", "id": mid}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        logger.error("Resend HTTP %s: %s", e.code, detail)
        return {"ok": False, "note": f"HTTP {e.code}: {detail}", "id": None}
    except Exception as e:
        logger.error("Resend błąd: %s", e)
        return {"ok": False, "note": f"błąd: {e}", "id": None}


# ─────────────────────────────────────────────────────────────────────────────
# EARNINGS — darmowy Finnhub /calendar/earnings
# ─────────────────────────────────────────────────────────────────────────────
def fetch_earnings_finnhub(tickers: list,
                           api_key: Optional[str] = None,
                           horizon_days: int = 14,
                           _mock_response: Optional[dict] = None) -> dict:
    """Zwraca {ticker: 'YYYY-MM-DD'} z najbliższą datą wyników w horyzoncie.
    _mock_response do testów offline. Brak klucza/sieci -> {} (bezpieczna degradacja)."""
    if not tickers:
        return {}
    api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")

    today = date.today()
    to = today + timedelta(days=horizon_days)

    def _parse(data: dict) -> dict:
        out = {}
        for item in data.get("earningsCalendar", []):
            sym = str(item.get("symbol", "")).upper()
            d = item.get("date")
            if sym and d:
                # zachowaj najbliższą datę dla tickera
                if sym not in out or d < out[sym]:
                    out[sym] = d
        # zostaw tylko interesujące nas tickery
        return {tk: out[tk.upper()] for tk in tickers if tk.upper() in out}

    if _mock_response is not None:
        return _parse(_mock_response)

    if not api_key:
        logger.warning("Brak FINNHUB_API_KEY — earnings pominięte (bezpieczna degradacja).")
        return {}

    url = (f"{FINNHUB_EARNINGS_ENDPOINT}?from={today.isoformat()}&to={to.isoformat()}"
           f"&token={api_key}")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse(data)
    except Exception as e:
        logger.error("Finnhub earnings błąd: %s — pomijam (degradacja).", e)
        return {}


def fetch_earnings_yfinance(tickers: list, _calendar_override: Optional[dict] = None) -> dict:
    """FALLBACK: pobiera daty wyników z yfinance dla tickerów, których Finnhub nie pokrył
    (głównie ADR-y: TSM/ASML/NVO/SAP). Zwraca {ticker: 'YYYY-MM-DD'}.
    To DRUGIE ŹRÓDŁO realnych dat — NIE obejście bezpiecznika. Brak danych -> ticker pominięty.
    _calendar_override: {ticker: date_or_None} do testów offline (omija yfinance)."""
    if not tickers:
        return {}

    def _extract_date(cal) -> Optional[str]:
        """yfinance.calendar bywa dict {'Earnings Date': [date,...]} albo DataFrame."""
        try:
            if cal is None:
                return None
            # nowy yfinance: dict
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date") or cal.get("earningsDate")
                if isinstance(ed, (list, tuple)) and ed:
                    d0 = ed[0]
                else:
                    d0 = ed
                if d0 is None:
                    return None
                if hasattr(d0, "isoformat"):
                    return d0.isoformat()[:10]
                return str(d0)[:10]
            # starszy yfinance: DataFrame z wierszem 'Earnings Date'
            if hasattr(cal, "loc"):
                try:
                    val = cal.loc["Earnings Date"][0]
                    if hasattr(val, "isoformat"):
                        return val.isoformat()[:10]
                    return str(val)[:10]
                except Exception:
                    return None
        except Exception:
            return None
        return None

    if _calendar_override is not None:
        out = {}
        for tk in tickers:
            d = _extract_date(_calendar_override.get(tk))
            if d:
                out[tk] = d
        return out

    try:
        import yfinance as yf
    except Exception:
        logger.warning("yfinance niedostępne — fallback earnings pominięty.")
        return {}

    out = {}
    for tk in tickers:
        try:
            cal = yf.Ticker(tk).calendar
            d = _extract_date(cal)
            if d:
                out[tk] = d
        except Exception as e:
            logger.info("yfinance earnings dla %s nieudane (%s) — pomijam.", tk, e)
    return out


def fetch_earnings(tickers: list, api_key: Optional[str] = None, horizon_days: int = 14,
                   use_yfinance_fallback: bool = True,
                   _finnhub_mock: Optional[dict] = None,
                   _yf_override: Optional[dict] = None) -> dict:
    """Łączy oba źródła: najpierw Finnhub (szybkie, batch), potem yfinance dla BRAKUJĄCYCH
    tickerów (ADR-y itd.). Zwraca {ticker: 'YYYY-MM-DD'}.
    To rozwiązuje problem 'nieznana data wyników' dla spółek spoza pokrycia Finnhub free."""
    result = fetch_earnings_finnhub(tickers, api_key=api_key, horizon_days=horizon_days,
                                    _mock_response=_finnhub_mock)
    if not use_yfinance_fallback:
        return result
    missing = [tk for tk in tickers if tk not in result and tk.upper() not in result]
    if missing:
        yf_dates = fetch_earnings_yfinance(missing, _calendar_override=_yf_override)
        for tk, d in yf_dates.items():
            result[tk] = d
    return result


def days_to_earnings(earnings_map: dict, ticker: str) -> Optional[int]:
    """Ile dni do najbliższych wyników (None gdy brak danych)."""
    d = earnings_map.get(ticker) or earnings_map.get(ticker.upper())
    if not d:
        return None
    try:
        return (date.fromisoformat(d) - date.today()).days
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline)
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST notifications (offline) ===")
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # 1. dry_run nie wysyła
    r = send_email_resend("<p>test</p>", "Temat", dry_run=True)
    check("dry_run nie wysyła", not r["ok"] and "dry_run" in r["note"])

    # 2. brak klucza -> bezpieczna degradacja
    r = send_email_resend("<p>x</p>", "T", api_key="", recipient="x@y.pl")
    check("brak klucza -> ok=False, nie crash", not r["ok"])

    # 3. earnings parse z mocka
    today = date.today()
    soon = (today + timedelta(days=1)).isoformat()
    later = (today + timedelta(days=10)).isoformat()
    mock = {"earningsCalendar": [
        {"symbol": "NVDA", "date": soon},
        {"symbol": "AVGO", "date": later},
        {"symbol": "ZZZ", "date": soon},
    ]}
    em = fetch_earnings_finnhub(["NVDA", "AVGO"], _mock_response=mock)
    check("earnings: NVDA i AVGO znalezione", "NVDA" in em and "AVGO" in em)
    check("earnings: ZZZ pominięty (nie pytaliśmy)", "ZZZ" not in em)

    # 4. days_to_earnings
    check("NVDA wyniki za 1 dzień", days_to_earnings(em, "NVDA") == 1)
    check("AVGO wyniki za 10 dni", days_to_earnings(em, "AVGO") == 10)
    check("brak tickera -> None", days_to_earnings(em, "TSLA") is None)

    # 5. pusta lista tickerów -> {}
    check("pusta lista -> {}", fetch_earnings_finnhub([], _mock_response=mock) == {})

    # 6. brak klucza earnings -> {} (degradacja)
    check("brak FINNHUB_API_KEY -> {}", fetch_earnings_finnhub(["NVDA"], api_key="") == {})

    # 7. yfinance fallback — parsuje datę z dict calendar (ADR-y)
    import datetime as _dt
    tsm_date = today + timedelta(days=8)
    yf_over = {"TSM": {"Earnings Date": [tsm_date]}, "ASML": {"Earnings Date": []}}
    yf_em = fetch_earnings_yfinance(["TSM", "ASML"], _calendar_override=yf_over)
    check("yfinance fallback: TSM data sparsowana", yf_em.get("TSM") == tsm_date.isoformat())
    check("yfinance fallback: ASML bez daty -> pominięty", "ASML" not in yf_em)

    # 8. fetch_earnings łączy oba: Finnhub dla NVDA, yfinance dla TSM (brakującego)
    combined = fetch_earnings(["NVDA", "AVGO", "TSM"], _finnhub_mock=mock,
                              _yf_override={"TSM": {"Earnings Date": [tsm_date]}})
    check("fetch_earnings: NVDA z Finnhub", combined.get("NVDA") == soon)
    check("fetch_earnings: TSM dociągnięty z yfinance (fallback)", combined.get("TSM") == tsm_date.isoformat())
    check("fetch_earnings: 3 tickery pokryte (Finnhub+yfinance)", len([t for t in ["NVDA","AVGO","TSM"] if t in combined]) == 3)

    # 9. fallback wyłączalny
    only_finnhub = fetch_earnings(["NVDA", "TSM"], _finnhub_mock=mock, use_yfinance_fallback=False,
                                  _yf_override={"TSM": {"Earnings Date": [tsm_date]}})
    check("fetch_earnings: fallback OFF -> TSM nieobecny", "TSM" not in only_finnhub)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== notifications.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — wysyłka maila + earnings")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
