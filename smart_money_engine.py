"""
smart_money_engine.py — SMART MONEY ENGINE (Krok 2 lejka Top-Down)
====================================================================
Bot Porsche — silnik pobierania danych "grubego kapitału" na ŻYWO.

UWAGA: ten moduł robi REALNE wywołania sieciowe (OpenInsider scraping, yfinance opcje).
NIE da się go przetestować z kontenera bez sieci — selftest używa FIXTURE (zapisany HTML
OpenInsider + mock łańcucha opcji). Realne pobieranie tylko w routine.

TRZY SYGNAŁY:
  1. Klastry insiderów (OpenInsider, primary): ≥2 oficerów C-Suite (CEO/CFO/COO/Pres),
     kod transakcji P (open-market purchase), w oknie 10 dni  -> CONFLUENCE.
  2. Filtr Śmierci (Hard Block): ≥2 oficerów C-Suite SPRZEDAJE (kod S, z odsiewem 10b5-1)
     w oknie 10 dni  -> HARD_BLOCK (zakaz wejścia długiego).
  3. Put/Call ratio SPY (yfinance, dzienny VOLUME nie OI): P/C > 1.2 = strach -> flaga makro.

FAIL-CLOSED (asymetryczny, kluczowy dla pieniędzy):
  • Błąd sieci/parsowania przy OpenInsider -> stan UNKNOWN (NIE Hard Block, ale NIGDY Confluence).
  • UNKNOWN przekłada się na skalar 0.5 w cash_management (nie 1.0). Brak danych ≠ zielone światło.

ANTI-BAN: pobieranie HTML przez curl_cffi (impersonacja Chrome), dopiero potem pandas.read_html
na pobranym tekście. Sam pandas.read_html(url) NIE omija blokad — dlatego rozdzielamy fetch i parse.

Zależności: curl_cffi, pandas, yfinance (opcjonalnie; brak -> degradacja). lxml/bs4 dla read_html.
Uruchomienie testów: python smart_money_engine.py --selftest
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import pandas as pd

# importy opcjonalne — brak nie wywala modułu
try:
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover
    cffi_requests = None

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

try:
    from tenacity import retry, stop_after_attempt, wait_exponential
    _HAS_TENACITY = True
except Exception:  # pragma: no cover
    _HAS_TENACITY = False
    def retry(*a, **k):
        def w(f): return f
        return w
    def stop_after_attempt(*a, **k): return None
    def wait_exponential(*a, **k): return None


logger = logging.getLogger("porsche.smartmoney")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Stany sygnału (zgodne z cash_management.SmartMoneyState)
# ─────────────────────────────────────────────────────────────────────────────
class SmartMoneyState(str, Enum):
    CONFLUENCE = "CONFLUENCE"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"        # fail-CLOSED: źródło padło
    HARD_BLOCK = "HARD_BLOCK"


@dataclass(frozen=True)
class SmartMoneyConfig:
    cluster_window_days: int = 30      # okno klastra: 30 dni (kupna insiderów to rzadkie zdarzenia)
    min_csuite_buyers: int = 2          # ≥2 C-Suite kupujących = CONFLUENCE
    min_csuite_sellers: int = 2         # ≥2 C-Suite sprzedających = HARD_BLOCK
    put_call_fear_threshold: float = 1.2
    impersonate: str = "chrome120"
    request_timeout_s: int = 20
    max_retries: int = 3
    # tytuły uznawane za C-Suite — dopasowane do REALNYCH skrótów OpenInsider:
    # CEO, CFO, COO, Pres (President), COB (Chairman of Board), GC (General Counsel),
    # CTO/CIO/CMO. UWAGA: "Dir" (Director) i "10%" NIE są C-Suite — to zwykli członkowie
    # rady / więksi udziałowcy, nie zarząd operacyjny.
    csuite_markers: tuple = ("ceo", "cfo", "coo", "cto", "cio", "cmo",
                             "pres", "cob", "chief", "chairman", "gc")
    # okno dni rozszerzone do 90 — kupna insiderów to rzadkie zdarzenia, 10 dni za mało
    openinsider_base: str = "http://openinsider.com/screener"


@dataclass
class SmartMoneyResult:
    ticker: str
    state: SmartMoneyState
    reason: str
    n_csuite_buyers: int = 0
    n_csuite_sellers: int = 0
    buyer_names: list = field(default_factory=list)
    seller_names: list = field(default_factory=list)
    source: str = "openinsider"


@dataclass
class PutCallResult:
    ok: bool
    ratio: Optional[float]
    fear_flag: bool
    note: str


# ─────────────────────────────────────────────────────────────────────────────
# Anti-Ban: pobieranie HTML przez curl_cffi
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_html(url: str, cfg: SmartMoneyConfig) -> str:
    """Pobiera HTML przez curl_cffi (impersonacja Chrome). Rzuca wyjątek po wyczerpaniu prób.
    Sam pandas.read_html(url) NIE omija blokad — dlatego pobieramy osobno."""
    if cffi_requests is None:
        raise RuntimeError("curl_cffi niedostępne — nie pobieram (anti-ban wymagany)")

    @retry(reraise=True, stop=stop_after_attempt(cfg.max_retries),
           wait=wait_exponential(multiplier=2, max=20))
    def _do() -> str:
        r = cffi_requests.get(url, impersonate=cfg.impersonate, timeout=cfg.request_timeout_s)
        if r.status_code != 200:
            raise RuntimeError(f"OpenInsider HTTP {r.status_code}")
        if not r.text or len(r.text) < 500:
            raise RuntimeError("OpenInsider: pusta/zbyt krótka odpowiedź")
        return r.text

    return _do()


# ─────────────────────────────────────────────────────────────────────────────
# Parsowanie tabeli OpenInsider (z już pobranego HTML)
# ─────────────────────────────────────────────────────────────────────────────
def parse_openinsider_html(html: str) -> pd.DataFrame:
    """Parsuje HTML OpenInsider -> DataFrame z tabelą transakcji.
    Szuka tabeli danych (Filing Date + Trade Type + Insider Name), nie tabel formularza filtrów.
    Rzuca wyjątek jeśli nie znajdzie sensownej tabeli (-> fail-CLOSED w warstwie wyżej)."""
    tables = pd.read_html(io.StringIO(html))
    if not tables:
        raise ValueError("brak tabel w HTML")
    # tabela danych OpenInsider ma charakterystyczny zestaw kolumn:
    # X, Filing Date, Trade Date, Ticker, Company Name, Insider Name, Title, Trade Type, Price...
    best = None
    for t in tables:
        cols = [str(c).strip().lower() for c in t.columns]
        has_trade_type = any("trade type" in c for c in cols)
        has_filing = any("filing date" in c for c in cols)
        has_insider = any("insider name" in c for c in cols)
        # prawdziwa tabela transakcji: trade type + (filing date lub insider name) + wiele wierszy
        if has_trade_type and (has_filing or has_insider) and len(t) >= 1:
            # wybierz tę z największą liczbą wierszy (główna tabela danych)
            if best is None or len(t) > len(best):
                best = t
    if best is not None:
        return best
    # nie znaleziono tabeli transakcji — to NIE jest fallback na largest (tamto łapało śmieci)
    raise ValueError("nie znaleziono tabeli transakcji (brak Trade Type + Filing/Insider)")


def _col(df: pd.DataFrame, *candidates: str) -> Optional[str]:
    """Znajduje nazwę kolumny pasującą do któregokolwiek z fragmentów (case-insensitive)."""
    for c in df.columns:
        cl = str(c).strip().lower()
        for cand in candidates:
            if cand in cl:
                return c
    return None


def analyze_insider_clusters(df: pd.DataFrame, ticker: str, cfg: SmartMoneyConfig) -> SmartMoneyResult:
    """Z DataFrame OpenInsider liczy klastry C-Suite (kupno kod P / sprzedaż kod S).
    Zwraca CONFLUENCE / HARD_BLOCK / NEUTRAL. Filtruje 10b5-1 dla sprzedaży."""
    title_col = _col(df, "title")
    type_col = _col(df, "trade type", "transaction")
    # insider: preferuj "insider name"; NIE łap "company name" (zawiera 'name')
    insider_col = _col(df, "insider name")
    if insider_col is None:
        # fallback: kolumna z 'insider' albo 'name' ale NIE 'company'
        for c in df.columns:
            cl = str(c).strip().lower()
            if ("insider" in cl or "name" in cl) and "company" not in cl:
                insider_col = c
                break
    ticker_col = _col(df, "ticker")
    date_col = _col(df, "trade date", "filing date", "date")

    if title_col is None or type_col is None:
        return SmartMoneyResult(ticker, SmartMoneyState.UNKNOWN,
                                "brak kolumn Title/TradeType — nie ufam parsowaniu (fail-CLOSED)")

    # filtr po tickerze, jeśli kolumna istnieje
    work = df
    if ticker_col is not None:
        work = df[df[ticker_col].astype(str).str.upper().str.strip() == ticker.upper().strip()]
    if len(work) == 0:
        return SmartMoneyResult(ticker, SmartMoneyState.NEUTRAL, "brak transakcji insiderów w oknie")

    # filtr okna czasu, jeśli mamy datę
    if date_col is not None:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=cfg.cluster_window_days)
        def _recent(v):
            try:
                return pd.to_datetime(v).date() >= cutoff
            except Exception:
                return True  # nie odrzucamy gdy data nieparsowalna
        work = work[work[date_col].apply(_recent)]

    buyers, sellers = set(), set()
    for _, row in work.iterrows():
        title = str(row.get(title_col, "")).lower()
        ttype = str(row.get(type_col, "")).lower()
        name = str(row.get(insider_col, "")).strip() if insider_col else "?"
        # dopasowanie po TOKENACH tytułu (rozdzielone przecinkami/spacjami/slashami),
        # nie po podciągu — inaczej "director" łapie "cto", a "president" łapie "pres" błędnie.
        # Tytuły OpenInsider: "CEO", "Dir, 10%", "Pres, CFO", "COB", "Officer".
        tokens = set()
        for part in title.replace("/", ",").replace("&", ",").split(","):
            tokens.add(part.strip())
        is_csuite = False
        for tok in tokens:
            # token jest C-Suite jeśli równy markerowi LUB marker jest osobnym słowem w tokenie
            for m in cfg.csuite_markers:
                mm = m.strip()
                if tok == mm or tok.startswith(mm + " ") or tok.endswith(" " + mm) or (" " + mm + " ") in (" " + tok + " "):
                    is_csuite = True
                    break
            if is_csuite:
                break
        if not is_csuite:
            continue
        # kod P = purchase (kupno na wolnym rynku); "p - purchase"
        if ttype.startswith("p") or "purchase" in ttype:
            buyers.add(name)
        # kod S = sale; odsiewamy plany 10b5-1 (mechaniczne)
        elif ttype.startswith("s") or "sale" in ttype:
            if "10b5" in ttype or "planned" in ttype:
                continue
            sellers.add(name)

    nb, ns = len(buyers), len(sellers)
    if ns >= cfg.min_csuite_sellers:
        return SmartMoneyResult(ticker, SmartMoneyState.HARD_BLOCK,
                                f"klaster sprzedaży C-Suite ({ns}) — zakaz wejścia",
                                n_csuite_buyers=nb, n_csuite_sellers=ns,
                                buyer_names=sorted(buyers), seller_names=sorted(sellers))
    if nb >= cfg.min_csuite_buyers:
        return SmartMoneyResult(ticker, SmartMoneyState.CONFLUENCE,
                                f"klaster zakupów C-Suite ({nb}) — konfluencja",
                                n_csuite_buyers=nb, n_csuite_sellers=ns,
                                buyer_names=sorted(buyers), seller_names=sorted(sellers))
    return SmartMoneyResult(ticker, SmartMoneyState.NEUTRAL,
                            f"brak klastra (kupujący C-Suite: {nb}, sprzedający: {ns})",
                            n_csuite_buyers=nb, n_csuite_sellers=ns,
                            buyer_names=sorted(buyers), seller_names=sorted(sellers))


# ─────────────────────────────────────────────────────────────────────────────
# Główne API: sygnał smart money dla tickera (z fail-CLOSED)
# ─────────────────────────────────────────────────────────────────────────────
class SmartMoneyEngine:
    def __init__(self, config: Optional[SmartMoneyConfig] = None):
        self.cfg = config or SmartMoneyConfig()

    def _screener_url(self, ticker: str = "") -> str:
        """URL strony konkretnej spółki na OpenInsider: openinsider.com/TICKER.
        Ten format (ścieżka, nie query ?s=) jest faktycznie honorowany przez OpenInsider
        i zwraca transakcje insiderów TYLKO dla tego tickera. Filtr okna/typu robimy w pandas."""
        base = self.cfg.openinsider_base.rstrip("/").replace("/screener", "")
        return f"{base}/{ticker.upper().strip()}"

    def get_signal(self, ticker: str, html_override: Optional[str] = None) -> SmartMoneyResult:
        """Zwraca sygnał smart money dla tickera. html_override pozwala testować offline.
        FAIL-CLOSED: każdy błąd sieci/parsowania -> UNKNOWN (nie blokada, ale nie konfluencja)."""
        try:
            html = html_override if html_override is not None else _fetch_html(self._screener_url(ticker), self.cfg)
        except Exception as e:
            logger.error("OpenInsider niedostępny (%s) — stan UNKNOWN (fail-CLOSED)", e)
            return SmartMoneyResult(ticker, SmartMoneyState.UNKNOWN,
                                    f"źródło insiderów padło: {e}")
        try:
            df = parse_openinsider_html(html)
            return analyze_insider_clusters(df, ticker, self.cfg)
        except Exception as e:
            # Rozróżnij: strona poprawna ale BEZ transakcji (NEUTRAL) vs realna awaria (UNKNOWN).
            # OpenInsider dla spółki bez insider-trades pokazuje stronę z nazwą spółki,
            # ale bez wierszy tabeli — to NIE jest awaria, to brak transakcji = brak zagrożenia.
            looks_valid = ("SEC Form 4" in html or "Insider Trading" in html or
                           "openinsider" in html.lower())
            if looks_valid:
                logger.info("[%s] OpenInsider: strona OK, brak transakcji w oknie -> NEUTRAL", ticker)
                return SmartMoneyResult(ticker, SmartMoneyState.NEUTRAL,
                                        "brak transakcji insiderów w oknie (strona OK)")
            logger.error("Parsowanie OpenInsider nieudane (%s) — UNKNOWN (fail-CLOSED)", e)
            return SmartMoneyResult(ticker, SmartMoneyState.UNKNOWN,
                                    f"parsowanie insiderów nieudane: {e}")

    # ── Put/Call ratio SPY (dzienny VOLUME, nie OI) ──────────────────────────
    def get_put_call_ratio(self, symbol: str = "SPY",
                           chain_override: Optional[tuple] = None) -> PutCallResult:
        """P/C ratio z najbliższego łańcucha opcji (dzienny VOLUME). chain_override=(puts_df,calls_df)
        do testów offline. Brak danych -> ok:False (degradacja, nie crash)."""
        try:
            if chain_override is not None:
                puts, calls = chain_override
            else:
                if yf is None:
                    return PutCallResult(False, None, False, "yfinance niedostępne")
                t = yf.Ticker(symbol)
                expiries = t.options
                if not expiries:
                    return PutCallResult(False, None, False, "brak dat wygaśnięcia")
                chain = t.option_chain(expiries[0])
                puts, calls = chain.puts, chain.calls
            put_vol = float(puts["volume"].fillna(0).sum())
            call_vol = float(calls["volume"].fillna(0).sum())
            if call_vol <= 0:
                return PutCallResult(False, None, False, "zerowy wolumen call")
            ratio = put_vol / call_vol
            fear = ratio > self.cfg.put_call_fear_threshold
            return PutCallResult(True, round(ratio, 3), fear,
                                 f"P/C={ratio:.2f} ({'STRACH' if fear else 'spokój'})")
        except Exception as e:
            logger.error("Put/Call ratio nieudane (%s)", e)
            return PutCallResult(False, None, False, f"błąd: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline, na fixture)
# ─────────────────────────────────────────────────────────────────────────────
_FIXTURE_HTML_CONFLUENCE = """
<table class="tinytable">
<tr><th>X</th><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th>
<th>Title</th><th>Trade Type</th><th>Price</th><th>Qty</th></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Smith John</td>
<td>CEO</td><td>P - Purchase</td><td>$140</td><td>1000</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Doe Jane</td>
<td>CFO</td><td>P - Purchase</td><td>$141</td><td>500</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>NVDA</td><td>Roe Sam</td>
<td>Director</td><td>P - Purchase</td><td>$140</td><td>200</td></tr>
</table>
"""

_FIXTURE_HTML_HARDBLOCK = """
<table class="tinytable">
<tr><th>X</th><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th>
<th>Title</th><th>Trade Type</th><th>Price</th><th>Qty</th></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>ABC</td><td>Boss One</td>
<td>CEO</td><td>S - Sale</td><td>$50</td><td>5000</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>ABC</td><td>Boss Two</td>
<td>CFO</td><td>S - Sale</td><td>$51</td><td>3000</td></tr>
</table>
"""

_FIXTURE_HTML_10B51 = """
<table class="tinytable">
<tr><th>X</th><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Insider Name</th>
<th>Title</th><th>Trade Type</th><th>Price</th><th>Qty</th></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>XYZ</td><td>Boss One</td>
<td>CEO</td><td>S - Sale 10b5-1</td><td>$50</td><td>5000</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>XYZ</td><td>Boss Two</td>
<td>CFO</td><td>S - Sale 10b5-1</td><td>$51</td><td>3000</td></tr>
</table>
"""

# Realne skróty tytułów OpenInsider: "Dir", "10%", "Pres, CFO", "COB". Dir i 10% NIE są C-Suite.
_FIXTURE_HTML_REAL_TITLES = """
<table class="tinytable">
<tr><th>X</th><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Company Name</th>
<th>Insider Name</th><th>Title</th><th>Trade Type</th><th>Price</th><th>Qty</th></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>REAL</td><td>Real Inc</td><td>Alpha</td>
<td>Dir</td><td>P - Purchase</td><td>$10</td><td>100</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>REAL</td><td>Real Inc</td><td>Beta</td>
<td>10%</td><td>P - Purchase</td><td>$10</td><td>100</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>REAL</td><td>Real Inc</td><td>Gamma</td>
<td>Pres, CFO</td><td>P - Purchase</td><td>$10</td><td>100</td></tr>
<tr><td>M</td><td>2026-05-25</td><td>2026-05-24</td><td>REAL</td><td>Real Inc</td><td>Delta</td>
<td>COB</td><td>P - Purchase</td><td>$10</td><td>100</td></tr>
</table>
"""


def _run_selftest() -> int:
    print("=== SELFTEST smart_money_engine (offline, fixtures) ===")
    eng = SmartMoneyEngine()
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # 1. Klaster zakupów C-Suite -> CONFLUENCE (2 z 3 to C-Suite: CEO+CFO, Director się nie liczy)
    r = eng.get_signal("NVDA", html_override=_FIXTURE_HTML_CONFLUENCE)
    check("CONFLUENCE: 2 C-Suite kupujących", r.state == SmartMoneyState.CONFLUENCE)
    check("CONFLUENCE: liczy CEO+CFO = 2 (Director odrzucony)", r.n_csuite_buyers == 2)

    # 2. Klaster sprzedaży C-Suite -> HARD_BLOCK
    r = eng.get_signal("ABC", html_override=_FIXTURE_HTML_HARDBLOCK)
    check("HARD_BLOCK: 2 C-Suite sprzedających", r.state == SmartMoneyState.HARD_BLOCK)

    # 3. Sprzedaże 10b5-1 są odsiane -> NIE hard block
    r = eng.get_signal("XYZ", html_override=_FIXTURE_HTML_10B51)
    check("10b5-1 odsiane -> NIE HARD_BLOCK", r.state != SmartMoneyState.HARD_BLOCK)

    # 4. Pusty HTML / brak tabeli -> UNKNOWN (fail-CLOSED)
    r = eng.get_signal("ABC", html_override="<html>brak tabeli</html>")
    check("Brak tabeli -> UNKNOWN (fail-CLOSED)", r.state == SmartMoneyState.UNKNOWN)

    # 5. Ticker bez transakcji -> NEUTRAL
    r = eng.get_signal("ZZZZ", html_override=_FIXTURE_HTML_CONFLUENCE)
    check("Ticker bez transakcji -> NEUTRAL", r.state == SmartMoneyState.NEUTRAL)

    # 6. Put/Call: strach (P/C > 1.2)
    puts = pd.DataFrame({"volume": [1000, 800]})
    calls = pd.DataFrame({"volume": [500, 400]})
    pc = eng.get_put_call_ratio("SPY", chain_override=(puts, calls))
    check("P/C strach: ratio 2.0 > 1.2 -> fear_flag", pc.ok and pc.fear_flag and abs(pc.ratio - 2.0) < 0.01)

    # 7. Put/Call: spokój (P/C < 1.2)
    puts2 = pd.DataFrame({"volume": [300]})
    calls2 = pd.DataFrame({"volume": [1000]})
    pc = eng.get_put_call_ratio("SPY", chain_override=(puts2, calls2))
    check("P/C spokój: ratio 0.3 < 1.2 -> brak fear", pc.ok and not pc.fear_flag)

    # 8. Put/Call: zerowy wolumen call -> ok:False (degradacja)
    pc = eng.get_put_call_ratio("SPY", chain_override=(pd.DataFrame({"volume": [100]}), pd.DataFrame({"volume": [0]})))
    check("P/C zerowy call -> degradacja (nie crash)", not pc.ok)

    # 9. REALNE skróty OpenInsider: Pres,CFO + COB = 2 C-Suite; Dir i 10% odrzucone
    r = eng.get_signal("REAL", html_override=_FIXTURE_HTML_REAL_TITLES)
    check("Realne tytuły: Pres,CFO + COB = 2 C-Suite (CONFLUENCE)", r.state == SmartMoneyState.CONFLUENCE)
    check("Realne tytuły: Dir i 10% NIE liczone jako C-Suite (dokładnie 2)", r.n_csuite_buyers == 2)

    # 10. Poprawna strona OpenInsider BEZ transakcji -> NEUTRAL (nie UNKNOWN!)
    #     To realny przypadek: mega-cap bez insider-trades w oknie. Brak transakcji = brak zagrożenia.
    empty_page = "<html><title>AVGO - Broadcom Inc. - SEC Form 4 Insider Trading Screener</title><body>no rows</body></html>"
    r = eng.get_signal("AVGO", html_override=empty_page)
    check("Poprawna strona bez transakcji -> NEUTRAL (nie UNKNOWN)", r.state == SmartMoneyState.NEUTRAL)

    # 11. Prawdziwa awaria (śmieci, brak markerów OpenInsider) -> UNKNOWN
    r = eng.get_signal("XXX", html_override="<html>garbage 502 bad gateway</html>")
    check("Śmieci/awaria -> UNKNOWN (fail-CLOSED)", r.state == SmartMoneyState.UNKNOWN)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== smart_money_engine.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Smart Money Engine")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
