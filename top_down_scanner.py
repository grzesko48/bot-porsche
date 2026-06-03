"""
top_down_scanner.py — KROK 1 LEJKA TOP-DOWN (samodzielny skaner)
=================================================================
Bot Porsche — własny, czysty silnik doboru spółek. ZERO importów ze starego marketbot.py.

LEJEK TOP-DOWN:
  1. SKANER ETF: pobiera ceny predefiniowanej macierzy ETF-ów sektorowych.
  2. MOMENTUM (ROC): liczy Rate of Change 10 i 20 dni czystym pandas, łączy w wynik.
  3. WYBÓR 2 NAJSILNIEJSZYCH sektorów.
  4. DRILL-DOWN: bierze koszyk spółek tylko z tych 2 wygranych sektorów.
  5. KANDYDACI: te spółki -> dalej do SmartMoney + DCM.
  6. RADAR MAKRO (własny): SPY vs 200 SMA + odchylenie VIX. Deterministyczny, we własnym kodzie.

SIEĆ: yfinance przez curl_cffi (impersonacja Chrome) + retry. Brak sieci / błąd -> mock/degradacja
przez try/except. Kod produkcyjny: działa na maszynie z internetem, nie wywala się bez niego.

Uruchomienie testów: python top_down_scanner.py --selftest
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
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

logger = logging.getLogger("porsche.scanner")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# MACIERZ ETF SEKTOROWYCH (KROK 1) — zadeklarowana w kodzie
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_ETFS = {
    "XLK":  "Technologia (szeroka)",
    "SMH":  "Półprzewodniki",
    "XLF":  "Finanse",
    "XLE":  "Energia",
    "XLV":  "Ochrona zdrowia",
    "XLI":  "Przemysł",
    "XLY":  "Konsument cykliczny",
    "XLP":  "Konsument defensywny",
    "XLB":  "Materiały",
    "XLU":  "Usługi komunalne",
    "XLRE": "Nieruchomości",
    "ITA":  "Zbrojeniówka / Aero",
    "CIBR": "Cyberbezpieczeństwo",
    "XBI":  "Biotechnologia",
    "XLC":  "Usługi komunikacyjne",
}

# MAPOWANIE ETF -> sektor(y) GICS w S&P 500.
# Ranking sektorów liczymy z ETF-ów (cap-weighted proxy przepływów — decyzja 1a),
# ale drill-down robimy do PEŁNEJ listy spółek S&P 500 należących do zmapowanego sektora GICS.
# Niektóre ETF-y to pod-sektory (SMH=półprzewodniki, CIBR=cyber, ITA=aero, XBI=biotech) —
# mapujemy je na nadrzędny sektor GICS, a zawężenie robią pre-filtry i ranking momentum.
ETF_TO_GICS = {
    "XLK":  ["Information Technology"],
    "SMH":  ["Information Technology"],                       # półprzewodniki ⊂ IT
    "XLF":  ["Financials"],
    "XLE":  ["Energy"],
    "XLV":  ["Health Care"],
    "XLI":  ["Industrials"],
    "XLY":  ["Consumer Discretionary"],
    "XLP":  ["Consumer Staples"],
    "XLB":  ["Materials"],
    "XLU":  ["Utilities"],
    "XLRE": ["Real Estate"],
    "ITA":  ["Industrials"],                                  # aero/zbroj. ⊂ Industrials
    "CIBR": ["Information Technology"],                       # cyber ⊂ IT
    "XBI":  ["Health Care"],                                  # biotech ⊂ Health Care
    "XLC":  ["Communication Services"],                       # GOOGL, META, NFLX, DIS, RDDT — wcześniej NIEWIDZIANE
}

# Źródło dynamicznego uniwersum S&P 500 (darmowe, utrzymywane, ticker + sektor GICS).
SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# KURATOROWANE "grube ryby" spoza S&P 500 (decyzja B + ADR-y).
# Dodawane do uniwersum z RĘCZNIE przypisanym sektorem GICS (bo nie ma ich w CSV S&P 500).
# Dedup: jeśli spółka trafi do S&P 500 (np. HOOD), wpis tutaj jest pomijany (CSV ma pierwszeństwo).
# UWAGA: ADR-y zagraniczne (TSM/ASML/NVO/SAP) — OpenInsider ich NIE pokrywa, smart money zawsze NEUTRAL.
CURATED_EXTRA = {
    "TSM":  "Information Technology",     # TSMC (ADR) — chipy dla NVDA/AAPL/AMD
    "ASML": "Information Technology",     # ASML (ADR) — monopol EUV
    "NVO":  "Health Care",                # Novo Nordisk (ADR) — Ozempic/Wegovy
    "SAP":  "Information Technology",     # SAP (ADR) — ERP enterprise
    "MSTR": "Information Technology",     # MicroStrategy — proxy Bitcoin
    "ARM":  "Information Technology",     # ARM Holdings (ADR, IPO 2023)
    "HOOD": "Financials",                 # Robinhood (dedup jeśli już w S&P 500)
    "RDDT": "Communication Services",     # Reddit (IPO 2024)
}

# Zbiór tickerów zagranicznych ADR (do ewentualnego oznaczenia, że OpenInsider ich nie pokrywa).
FOREIGN_ADR_TICKERS = frozenset({"TSM", "ASML", "NVO", "SAP"})

# FALLBACK: wbudowane koszyki używane TYLKO gdy pobranie S&P 500 padnie.
# Dzięki temu bot działa nawet bez sieci do CSV (degradacja, nie crash).
SECTOR_BASKETS_FALLBACK = {
    "XLK":  ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE"],
    "SMH":  ["NVDA", "AVGO", "AMD", "QCOM", "MU", "LRCX", "AMAT", "KLAC"],
    "XLF":  ["JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP"],
    "XLE":  ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "FANG"],
    "XLV":  ["LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "ABT", "AMGN"],
    "XLI":  ["CAT", "GE", "HON", "UNP", "RTX", "BA", "DE", "LMT"],
    "XLY":  ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "BKNG", "SBUX"],
    "XLP":  ["PG", "KO", "PEP", "COST", "WMT", "MDLZ", "CL", "MO"],
    "XLB":  ["LIN", "FCX", "SHW", "APD", "ECL", "NEM", "DOW", "NUE"],
    "XLU":  ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL"],
    "XLRE": ["PLD", "AMT", "EQIX", "WELL", "SPG", "O", "PSA", "CCI"],
    "ITA":  ["RTX", "BA", "LMT", "GD", "NOC", "GE", "HWM", "AXON"],
    "CIBR": ["CRWD", "PANW", "FTNT", "ZS", "NET", "OKTA", "S", "GEN"],
    "XBI":  ["VRTX", "REGN", "MRNA", "AMGN", "GILD", "BIIB", "ALNY", "INCY"],
}


def load_sp500_universe(url: str = SP500_CSV_URL, timeout_s: int = 20,
                        _csv_override: Optional[str] = None) -> dict:
    """Pobiera aktualny skład S&P 500 -> dict {GICS_sector: [tickery]}.
    _csv_override: surowy tekst CSV do testów offline. Brak sieci -> {} (caller użyje fallbacku)."""
    import csv as _csv
    try:
        if _csv_override is not None:
            text = _csv_override
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            text = urllib.request.urlopen(req, timeout=timeout_s).read().decode("utf-8")
        rows = list(_csv.DictReader(io.StringIO(text)))
        out: dict = {}
        for r in rows:
            sym = str(r.get("Symbol", "")).strip().upper().replace(".", "-")  # BRK.B -> BRK-B (yfinance)
            sec = str(r.get("GICS Sector", "")).strip()
            if sym and sec:
                out.setdefault(sec, []).append(sym)
        return out
    except Exception as e:
        logger.warning("Nie pobrano S&P 500 (%s) — caller użyje fallbacku.", e)
        return {}


# Benchmarki dla radaru makro
BENCH_SPY = "SPY"
VIX_TICKER = "^VIX"


@dataclass
class SectorScore:
    etf: str
    name: str
    roc10: float
    roc20: float
    score: float        # łączny wynik momentum
    last_price: float



@dataclass
class StockCandidate:
    """Surowy kandydat z drill-down — przed wskaźnikami i pipeline'em."""
    ticker: str
    sector_etf: str
    sector_name: str
    close_series: pd.Series           # historia cen do liczenia wskaźników
    last_price: float
    volume_series: Optional[pd.Series] = None   # historia wolumenu (do dollar volume / płynności)


@dataclass
class ScanResult:
    radar_level: int
    radar_detail: str
    winning_sectors: list = field(default_factory=list)     # list[SectorScore]
    candidates: list = field(default_factory=list)          # list[StockCandidate]
    notes: list = field(default_factory=list)
    used_mock: bool = False
    regime_below_200sma: bool = False    # TWARDY filtr bessy: True -> bot nie otwiera nowych pozycji
    spy_last_price: float = 0.0          # cena SPY USD do trackera vs benchmark


@dataclass
class ScannerConfig:
    roc_short_days: int = 10
    roc_long_days: int = 20
    n_winning_sectors: int = 2
    impersonate: str = "chrome120"
    request_timeout_s: int = 20
    max_retries: int = 3
    history_period: str = "2y"        # 2 lata: potrzebne na 12-mies momentum + skip miesiąca
    roc_short_weight: float = 0.5     # waga ROC10 w łącznym wyniku (stary ranking)
    roc_long_weight: float = 0.5      # waga ROC20 (stary ranking)
    use_sp500: bool = True            # True: dynamiczny S&P 500; False: wbudowany fallback
    max_drilldown_per_sector: int = 60  # limit spółek na sektor w drill-down (ochrona przed rozjazdem)
    # RANKING MOMENTUM (spójne z backtest): multi-period 3/6/12 mies, skip ostatni miesiąc
    use_multiperiod_momentum: bool = True
    mom_lookbacks: tuple = (63, 126, 252)
    mom_weights: tuple = (0.2, 0.3, 0.5)
    mom_skip_recent_days: int = 21


# ─────────────────────────────────────────────────────────────────────────────
# POBIERANIE CEN (yfinance + curl_cffi, z degradacją)
# ─────────────────────────────────────────────────────────────────────────────
def _make_session(cfg: ScannerConfig):
    """Sesja curl_cffi z impersonacją (anti-ban yfinance). None gdy curl_cffi brak."""
    if cffi_requests is None:
        return None
    try:
        return cffi_requests.Session(impersonate=cfg.impersonate)
    except Exception as e:
        logger.warning("Nie utworzono sesji curl_cffi: %s", e)
        return None


def fetch_prices(tickers: list, cfg: ScannerConfig,
                 price_override: Optional[dict] = None,
                 volume_override: Optional[dict] = None) -> tuple:
    """Pobiera ceny zamknięcia + wolumen dla listy tickerów.
    Zwraca (close_df, volume_df). volume_df może być None (gdy brak danych/mock bez wolumenu).
    price_override / volume_override: dicty {ticker: pd.Series} do testów offline."""
    if price_override is not None:
        close = pd.DataFrame(price_override)
        vol = pd.DataFrame(volume_override) if volume_override is not None else None
        return close, vol

    if yf is None:
        logger.error("yfinance niedostępne — brak cen.")
        return None, None

    @retry(reraise=True, stop=stop_after_attempt(cfg.max_retries),
           wait=wait_exponential(multiplier=2, max=20))
    def _download() -> pd.DataFrame:
        session = _make_session(cfg)
        kwargs = dict(period=cfg.history_period, interval="1d",
                      auto_adjust=True, progress=False, threads=False)
        if session is not None:
            kwargs["session"] = session
        data = yf.download(tickers, **kwargs)
        if data is None or len(data) == 0:
            raise RuntimeError("yfinance zwrócił pusto")
        return data

    def _extract(data, field_name):
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if field_name in data.columns.get_level_values(0):
                    return data.xs(field_name, axis=1, level=0)
                return None
            else:
                if field_name in data.columns:
                    out = data[[field_name]]
                    if len(tickers) == 1:
                        out.columns = [tickers[0]]
                    return out
                return None
        except Exception:
            return None

    try:
        data = _download()
        close = _extract(data, "Close")
        volume = _extract(data, "Volume")
        if close is None:
            logger.error("Brak kolumny Close w danych.")
            return None, None
        close = close.dropna(how="all")
        return close, volume
    except Exception as e:
        logger.error("Pobieranie cen nieudane: %s", e)
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# MOMENTUM (ROC) — czysty pandas
# ─────────────────────────────────────────────────────────────────────────────
def rate_of_change(series: pd.Series, days: int) -> Optional[float]:
    """ROC = (cena_dziś / cena_sprzed_N_dni - 1) * 100. None gdy za mało danych."""
    s = series.dropna()
    if len(s) <= days:
        return None
    return float((s.iloc[-1] / s.iloc[-1 - days] - 1.0) * 100.0)


def multiperiod_momentum(series: pd.Series, lookbacks: tuple, weights: tuple,
                         skip_recent: int) -> Optional[float]:
    """Multi-period momentum (3/6/12 mies) z pominięciem ostatniego miesiąca (skip_recent).
    Klasyczne momentum Jegadeesh-Titman. Zwraca ważony blend (%). None gdy za mało historii.
    Spójne z Backtester._momentum_score."""
    s = series.dropna()
    longest = max(lookbacks)
    if len(s) < longest + skip_recent + 1:
        return None
    ref = s.iloc[-(skip_recent + 1)] if skip_recent > 0 else s.iloc[-1]
    score = 0.0
    for lb, w in zip(lookbacks, weights):
        base = s.iloc[-(lb + skip_recent + 1)]
        if base <= 0:
            return None
        score += w * (ref - base) / base * 100.0
    return float(score)


def score_sectors(close: pd.DataFrame, cfg: ScannerConfig) -> list:
    """Ranking ETF-ów. use_multiperiod_momentum: True -> multi-period 3/6/12 mies (spójne
    z backtest); False -> stary ROC10/20. ROC10/20 zachowane w SectorScore do raportu."""
    scores = []
    for etf, name in SECTOR_ETFS.items():
        if etf not in close.columns:
            continue
        series = close[etf].dropna()
        roc10 = rate_of_change(series, cfg.roc_short_days)
        roc20 = rate_of_change(series, cfg.roc_long_days)
        if cfg.use_multiperiod_momentum:
            combined = multiperiod_momentum(series, cfg.mom_lookbacks, cfg.mom_weights,
                                            cfg.mom_skip_recent_days)
            if combined is None:
                continue
        else:
            if roc10 is None or roc20 is None:
                continue
            combined = cfg.roc_short_weight * roc10 + cfg.roc_long_weight * roc20
        scores.append(SectorScore(
            etf=etf, name=name,
            roc10=round(roc10, 2) if roc10 is not None else 0.0,
            roc20=round(roc20, 2) if roc20 is not None else 0.0,
            score=round(combined, 2), last_price=float(series.iloc[-1]),
        ))
    scores.sort(key=lambda s: s.score, reverse=True)
    return scores


# ─────────────────────────────────────────────────────────────────────────────
# RADAR MAKRO (własny) — SPY vs 200 SMA + VIX
# ─────────────────────────────────────────────────────────────────────────────
def compute_macro_radar(close: pd.DataFrame, cfg: ScannerConfig) -> tuple[int, str, bool]:
    """Deterministyczny radar makro 0-3. Każdy zapalony sygnał = +1:
      • SPY poniżej 200 SMA (trend spadkowy)
      • VIX > 25 (podwyższony strach)
      • VIX > 35 (panika — drugi punkt od VIX)
    Brak danych -> liczy z dostępnych, dokłada notatkę.
    Zwraca też `regime_below_200sma` — TWARDY sygnał bessy do filtra wejścia.
    FILTR v2 (zwalidowany backtestem 2026-06-03): blokuje zakupy NIE przy samym
    SPY<200SMA, ale gdy JEDNOCZEŚNIE rynek jest pod średnią ORAZ panikuje (wysoka
    realized volatility / VIX). Powolna bessa przy spokoju (jak 2022) NIE blokuje —
    bot zostaje i łapie odbicia. Gwałtowny krach (panika) blokuje — ochrona działa.
    To naprawia stratę -15pp z 2022, którą dawał stary binarny filtr v1."""
    level = 0
    signals = []
    regime_below_200sma = False   # domyślnie: nie blokuj (fail-safe gdy brak danych SPY)
    below_sma = False
    panic = False
    spy_rvol = 0.0

    # SPY vs 200 SMA + realized volatility (rdzeń filtra v2)
    if BENCH_SPY in close.columns:
        spy = close[BENCH_SPY].dropna()
        if len(spy) >= 200:
            sma200 = spy.rolling(200).mean().iloc[-1]
            below_sma = spy.iloc[-1] < sma200
            depth = (sma200 - spy.iloc[-1]) / sma200 if below_sma else 0.0
            # realized volatility 20-dniowa (dzienna) — wskaźnik paniki
            rets = spy.pct_change().dropna()
            if len(rets) >= 20:
                spy_rvol = float(rets.iloc[-20:].std())
            vol_thr = 0.025 * (0.7 if depth > 0.05 else 1.0)   # głęboko pod SMA -> niższy próg
            panic = spy_rvol > vol_thr
            if below_sma:
                level += 1
                if panic:
                    regime_below_200sma = True   # v2: blokuj TYLKO przy panice
                    signals.append(f"SPY < 200SMA + panika (rvol {spy_rvol*100:.1f}% > {vol_thr*100:.1f}%) — STOP zakupów")
                else:
                    signals.append(f"SPY < 200SMA, ale spokój (rvol {spy_rvol*100:.1f}%) — powolna bessa, NIE blokuję")
            else:
                signals.append("SPY nad 200SMA (trend wzrostowy)")
        else:
            signals.append("SPY: za mało danych na 200SMA")
    else:
        signals.append("SPY: brak danych")

    # VIX — dodatkowe potwierdzenie paniki (podbija poziom radaru + może wymusić blokadę)
    if VIX_TICKER in close.columns:
        vix = close[VIX_TICKER].dropna()
        if len(vix):
            v = float(vix.iloc[-1])
            if v > 25:
                level += 1; signals.append(f"VIX {v:.1f} > 25 (podwyższony strach)")
            if v > 35:
                level += 1; signals.append(f"VIX {v:.1f} > 35 (panika)")
                # VIX > 35 = ewidentna panika: jeśli pod SMA, wymuś blokadę nawet gdy rvol nie złapał
                if below_sma:
                    regime_below_200sma = True
            if v <= 25:
                signals.append(f"VIX {v:.1f} (spokój)")
    else:
        signals.append("VIX: brak danych")

    level = min(level, 3)
    return level, "; ".join(signals), regime_below_200sma


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNY SKAN
# ─────────────────────────────────────────────────────────────────────────────
class TopDownScanner:
    def __init__(self, config: Optional[ScannerConfig] = None):
        self.cfg = config or ScannerConfig()

    def _build_baskets(self, sp500_override: Optional[str] = None) -> tuple:
        """Buduje koszyki {ETF: [tickery]} z dynamicznego S&P 500 + kuratorowane ADR-y (lub fallback).
        Zwraca (baskets, source_note)."""
        cfg = self.cfg
        if not cfg.use_sp500:
            return dict(SECTOR_BASKETS_FALLBACK), "fallback (use_sp500=False)"
        gics = load_sp500_universe(_csv_override=sp500_override)
        if not gics:
            return dict(SECTOR_BASKETS_FALLBACK), "fallback (S&P 500 niedostępny)"

        # scal kuratorowane "grube ryby" spoza indeksu (dedup: CSV S&P 500 ma pierwszeństwo)
        sp500_tickers = {t for lst in gics.values() for t in lst}
        n_added = 0
        # mapa GICS->kuratorowane (zachowujemy, by wstawić je PRZED listą S&P 500 — przetrwają cap)
        curated_by_sector: dict = {}
        for tk, sec in CURATED_EXTRA.items():
            if tk in sp500_tickers:
                continue  # już w S&P 500 (np. HOOD) — nie dubluj
            curated_by_sector.setdefault(sec, []).append(tk)
            n_added += 1

        baskets = {}
        for etf, gics_sectors in ETF_TO_GICS.items():
            names = []
            for gs in gics_sectors:
                names += curated_by_sector.get(gs, [])   # kuratorowane PRZODEM (gwarancja w cap)
                names += gics.get(gs, [])
            baskets[etf] = list(dict.fromkeys(names))[:cfg.max_drilldown_per_sector]
        total = sum(len(v) for v in baskets.values())
        return baskets, f"S&P 500 dynamiczny + {n_added} ADR/spoza-indeksu ({total} slotów, {len(gics)} sektorów GICS)"

    def scan(self, price_override: Optional[dict] = None,
             volume_override: Optional[dict] = None,
             sp500_override: Optional[str] = None) -> ScanResult:
        """Pełny lejek Top-Down (staged funnel):
          1. Pobierz ceny ETF-ów + benchmarki -> ranking sektorów -> 2 zwycięzców.
          2. Zbuduj koszyki z PEŁNEGO S&P 500 dla wygranych sektorów.
          3. Pobierz ceny tylko tych spółek -> drill-down kandydaci.
        price_override/volume_override/sp500_override do testów offline."""
        cfg = self.cfg
        used_mock = price_override is not None
        notes = []

        # zbuduj koszyki (dynamiczny S&P 500 lub fallback)
        baskets, basket_src = self._build_baskets(sp500_override=sp500_override)
        notes.append(f"uniwersum: {basket_src}")

        # ── ETAP 1: ranking sektorów z ETF-ów ─────────────────────────────────
        # W trybie mock (testy) mamy wszystkie ceny w jednym override — pobieramy raz.
        # Na żywo: pobieramy ETF-y + benchmarki + wszystkie spółki z koszyków jednym batchem
        # (yfinance i tak robi batch; staged-funnel zysk jest głównie w smart money, nie w cenach).
        etf_tickers = list(SECTOR_ETFS.keys()) + [BENCH_SPY, VIX_TICKER]
        all_basket = sorted({t for b in baskets.values() for t in b})
        all_tickers = list(dict.fromkeys(etf_tickers + all_basket))
        notes.append(f"pobieram ceny: {len(all_tickers)} tickerów (14 ETF + 2 bench + {len(all_basket)} spółek)")

        close, volume = fetch_prices(all_tickers, cfg, price_override=price_override,
                                     volume_override=volume_override)
        if close is None or len(close.columns) == 0:
            logger.error("Brak danych cenowych — skan zwraca pusto (bot powie 'brak propozycji').")
            return ScanResult(radar_level=0, radar_detail="brak danych cenowych",
                              notes=notes + ["fetch_prices zwrócił pusto — degradacja"], used_mock=used_mock)

        # KROK 2-3: momentum sektorów -> 2 najsilniejsze
        sector_scores = score_sectors(close, cfg)
        if not sector_scores:
            return ScanResult(radar_level=0, radar_detail="brak ETF do oceny",
                              notes=notes + ["score_sectors pusto"], used_mock=used_mock)
        winners = sector_scores[:cfg.n_winning_sectors]
        notes.append("ranking sektorów: " + ", ".join(f"{s.etf}({s.score:+.1f})" for s in sector_scores[:5]))
        notes.append("wygrane sektory: " + ", ".join(f"{s.etf} {s.name}" for s in winners))

        # RADAR MAKRO (własny)
        radar_level, radar_detail, regime_below = compute_macro_radar(close, cfg)
        notes.append(f"radar makro {radar_level}/3: {radar_detail}")

        # KROK 4-5: drill-down do PEŁNYCH koszyków S&P 500 wygranych sektorów
        candidates = []
        seen = set()
        for sec in winners:
            basket = baskets.get(sec.etf, [])
            for tk in basket:
                if tk in seen or tk not in close.columns:
                    continue
                series = close[tk].dropna()
                if len(series) < 20:
                    continue
                seen.add(tk)
                vol_series = None
                if volume is not None and tk in volume.columns:
                    vol_series = volume[tk].dropna()
                candidates.append(StockCandidate(
                    ticker=tk, sector_etf=sec.etf, sector_name=sec.name,
                    close_series=series, last_price=float(series.iloc[-1]),
                    volume_series=vol_series,
                ))
        notes.append(f"drill-down: {len(candidates)} kandydatów z {len(winners)} sektorów (pełny S&P 500)")

        spy_last = float(close[BENCH_SPY].dropna().iloc[-1]) if BENCH_SPY in close.columns else 0.0
        return ScanResult(radar_level=radar_level, radar_detail=radar_detail,
                          winning_sectors=winners, candidates=candidates,
                          notes=notes, used_mock=used_mock,
                          regime_below_200sma=regime_below,
                          spy_last_price=spy_last)


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline, mock cen)
# ─────────────────────────────────────────────────────────────────────────────
def _mock_prices() -> dict:
    """Syntetyczne ceny: XLK i SMH mocno rosną (wygrają), reszta płasko/spada.
    SPY nad 200SMA, VIX niski -> radar 0/3."""
    n = 400
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    out = {}
    # ETF-y: różne nachylenia
    slopes = {"XLK": 0.45, "SMH": 0.55, "XLF": 0.10, "XLE": -0.05, "XLV": 0.05,
              "XLI": 0.15, "XLY": 0.08, "XLP": 0.02, "XLB": 0.0, "XLU": 0.01,
              "XLRE": -0.02, "ITA": 0.20, "CIBR": 0.30, "XBI": -0.10}
    for etf, sl in slopes.items():
        out[etf] = pd.Series([100 + sl * i for i in range(n)], index=idx)
    # benchmarki
    out[BENCH_SPY] = pd.Series([400 + 0.3 * i for i in range(n)], index=idx)  # rośnie -> nad 200SMA
    out[VIX_TICKER] = pd.Series([15.0 for _ in range(n)], index=idx)          # spokój
    # koszyki — daj ceny spółkom z XLK i SMH (wygrane), żeby był drill-down
    for tk in SECTOR_BASKETS_FALLBACK["XLK"] + SECTOR_BASKETS_FALLBACK["SMH"]:
        base = 50 + (hash(tk) % 200)
        out[tk] = pd.Series([base + 0.2 * i for i in range(n)], index=idx)
    return out


def _mock_volume() -> dict:
    """Syntetyczny wolumen dla spółek z wygranych koszyków (do testu dollar volume)."""
    n = 400
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    out = {}
    for tk in SECTOR_BASKETS_FALLBACK["XLK"] + SECTOR_BASKETS_FALLBACK["SMH"]:
        out[tk] = pd.Series([5_000_000 + (hash(tk) % 1_000_000) for _ in range(n)], index=idx)
    return out


def _run_selftest() -> int:
    print("=== SELFTEST top_down_scanner (offline, mock cen) ===")
    # do testów mocka: uniwersum z fallbacku (deterministyczne, offline), nie z sieci
    scanner = TopDownScanner(ScannerConfig(use_sp500=False))
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # ROC
    s = pd.Series([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110])
    check("ROC 10 dni z 100->110 = +10%", abs(rate_of_change(s, 10) - 10.0) < 0.01)
    check("ROC za mało danych -> None", rate_of_change(pd.Series([1, 2, 3]), 10) is None)

    # pełny skan na mocku (fallback universe)
    res = scanner.scan(price_override=_mock_prices())
    check("Skan zwrócił wynik", res is not None)
    check("Użyto mocka", res.used_mock)
    check("Wybrano 2 sektory", len(res.winning_sectors) == 2)
    # SMH (slope 0.55) i XLK (0.45) powinny wygrać
    winners = [s.etf for s in res.winning_sectors]
    check("Najsilniejsze to SMH i XLK", set(winners) == {"SMH", "XLK"})
    check("Sektory posortowane (SMH przed XLK)", res.winning_sectors[0].etf == "SMH")
    check("Drill-down dał kandydatów", len(res.candidates) > 0)
    check("Kandydaci tylko z wygranych sektorów",
          all(c.sector_etf in {"SMH", "XLK"} for c in res.candidates))
    check("Kandydaci mają historię cen", all(len(c.close_series) >= 20 for c in res.candidates))

    # wolumen: skan z volume_override -> kandydaci mają volume_series
    res_v = scanner.scan(price_override=_mock_prices(), volume_override=_mock_volume())
    has_vol = [c for c in res_v.candidates if c.volume_series is not None]
    check("Wolumen przekazany do kandydatów", len(has_vol) > 0)

    # radar makro: SPY rośnie, VIX 15 -> 0/3
    check("Radar 0/3 przy spokoju", res.radar_level == 0)

    # radar przy panice: VIX 40, SPY spada
    crash = _mock_prices()
    n = len(crash[BENCH_SPY])
    crash[BENCH_SPY] = pd.Series([600 - 0.5 * i for i in range(n)], index=crash[BENCH_SPY].index)  # spada pod 200SMA
    crash[VIX_TICKER] = pd.Series([40.0] * n, index=crash[VIX_TICKER].index)                        # panika
    res2 = scanner.scan(price_override=crash)
    check("Radar podnosi się przy panice (>=2)", res2.radar_level >= 2)

    # brak danych -> degradacja, nie crash
    res3 = scanner.scan(price_override={})
    check("Pusty override -> brak kandydatów, nie crash", res3.candidates == [])

    # ── DYNAMICZNE UNIWERSUM S&P 500 (CSV override, offline) ──────────────────
    fake_csv = ("Symbol,Security,GICS Sector,GICS Sub-Industry\n"
                "AAA,Alpha,Information Technology,Software\n"
                "BBB,Beta,Information Technology,Semiconductors\n"
                "CCC,Gamma,Financials,Banks\n"
                "BRK.B,Berk,Financials,Insurance\n")
    gics = load_sp500_universe(_csv_override=fake_csv)
    check("S&P 500 CSV: parsuje sektory GICS", "Information Technology" in gics and len(gics["Information Technology"]) == 2)
    check("S&P 500 CSV: BRK.B -> BRK-B (format yfinance)", "BRK-B" in gics.get("Financials", []))

    sc = TopDownScanner(ScannerConfig(use_sp500=True))
    baskets, src = sc._build_baskets(sp500_override=fake_csv)
    check("Koszyk XLK z dynamicznego IT (AAA,BBB + kuratorowane)", {"AAA", "BBB"} <= set(baskets.get("XLK", [])))
    check("Koszyk XLF z dynamicznego Financials (CCC,BRK-B)", {"CCC", "BRK-B"} <= set(baskets.get("XLF", [])))

    # XLC: Communication Services mapuje (wcześniej niewidziane GOOGL/META/RDDT)
    fake_csv2 = (fake_csv + "GOOGL,Alphabet,Communication Services,Interactive Media\n"
                            "META,Meta,Communication Services,Interactive Media\n")
    baskets2, _ = sc._build_baskets(sp500_override=fake_csv2)
    check("XLC mapuje Communication Services (GOOGL, META widziane)",
          {"GOOGL", "META"} <= set(baskets2.get("XLC", [])))

    # Kuratorowane ADR-y wchodzą do uniwersum z przypisanym sektorem
    check("ADR TSM w koszyku IT (XLK)", "TSM" in baskets.get("XLK", []))
    check("ADR NVO w koszyku Health Care (XLV)", "NVO" in baskets.get("XLV", []))
    check("RDDT w koszyku Communication Services (XLC)", "RDDT" in baskets.get("XLC", []))

    # Dedup: spółka już w S&P 500 nie jest dublowana z CURATED_EXTRA
    fake_csv_hood = fake_csv + "HOOD,Robinhood,Financials,Investment Banking\n"
    baskets_h, _ = sc._build_baskets(sp500_override=fake_csv_hood)
    check("Dedup: HOOD w S&P 500 -> tylko raz w XLF",
          baskets_h.get("XLF", []).count("HOOD") == 1)

    # brak CSV (pusty) -> fallback
    baskets_fb, src_fb = sc._build_baskets(sp500_override="Symbol,GICS Sector\n")
    check("Pusty CSV -> fallback uniwersum", "fallback" in src_fb)

    # ── FILTR MAKRO v2 produkcyjny: powolna bessa NIE blokuje, panika blokuje ──
    import pandas as _pd, numpy as _np
    _n = 260
    # powolna bessa: pod 200SMA, niska zmienność -> NIE blokuj
    _calm = 100 * _np.cumprod(1 + _np.random.default_rng(1).normal(-0.0004, 0.006, _n))
    # gwałtowny krach: pod 200SMA, wysoka zmienność -> blokuj
    _panic = 100 * _np.cumprod(1 + _np.random.default_rng(2).normal(-0.003, 0.045, _n))
    try:
        for label, series, want_block in [("powolna bessa", _calm, False), ("panika", _panic, True)]:
            df = _pd.DataFrame({BENCH_SPY: series},
                               index=_pd.date_range("2022-01-01", periods=_n, freq="B"))
            _lvl, _sig, _regime = compute_macro_radar(df, cfg if 'cfg' in dir() else None)
            check(f"FILTR v2 prod: {label} -> blokada={want_block}", _regime == want_block)
    except Exception as e:
        check(f"FILTR v2 prod działa ({e})", False)

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== top_down_scanner.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — skaner Top-Down (Krok 1)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
