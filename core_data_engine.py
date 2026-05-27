"""
core_data_engine.py — MODUŁ 1: Fundamenty i Ingestia (Data Pipeline Base)
========================================================================
Bot Porsche — architektura lejka Top-Down. Warstwa pozyskiwania danych.

Ten moduł NIE podejmuje decyzji inwestycyjnych. Realizuje wyłącznie:
  • Anti-Ban Shield      — sesja curl_cffi (impersonacja Chrome) wstrzyknięta do yfinance,
                            batching + pacing, retry z exponential backoff (tenacity),
                            cache EOD na dysk (parquet).
  • MarketDataFetcher    — odporne pobieranie paczek OHLCV i fast_info.
  • PortfolioScreener    — KROK 1 (Compass): momentum 10D/20D dla macierzy ETF-ów → TOP-3 sektory.
  • LiquidityFilter      — KROK 4: odrzut po market cap (<500 mln USD) i dolarowym obrocie (<10 mln USD/dzień).

Zasada nadrzędna: GRACEFUL DEGRADATION. Padnięcie jednego źródła nie wywala pipeline'u —
zwracamy to, co mamy, z flagą ostrzegawczą. Nigdy nie wyłączamy weryfikacji SSL (dane finansowe).

Zależności (requirements):
    yfinance>=0.2.40
    curl_cffi>=0.7.0
    tenacity>=8.2.0
    pandas>=2.0
    pyarrow>=14.0          # backend parquet
Uruchomienie selftestu offline (bez sieci):
    python core_data_engine.py --selftest
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Importy opcjonalne — pipeline ma działać też w środowisku testowym bez sieci.
# Brak biblioteki NIE wywala modułu; degradujemy się miękko i logujemy.
# ─────────────────────────────────────────────────────────────────────────────
try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

try:
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover
    cffi_requests = None

try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    _HAS_TENACITY = True
except Exception:  # pragma: no cover
    _HAS_TENACITY = False

    # Lekki zamiennik dekoratora @retry, gdy tenacity nie jest zainstalowane.
    def retry(*dargs: Any, **dkwargs: Any):  # type: ignore
        def _wrap(fn):
            return fn
        return _wrap

    def stop_after_attempt(*a: Any, **k: Any):  # type: ignore
        return None

    def wait_exponential(*a: Any, **k: Any):  # type: ignore
        return None

    def retry_if_exception_type(*a: Any, **k: Any):  # type: ignore
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Logowanie
# ─────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("porsche.core")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Konfiguracja
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EngineConfig:
    """Parametry warstwy ingestii. Wszystko w jednym miejscu, łatwe do strojenia."""
    # Anti-ban
    chunk_size: int = 45                 # max tickerów na jedno wywołanie yfinance
    pace_seconds: float = 2.0            # przerwa między chunkami (s)
    impersonate: str = "chrome120"       # profil TLS dla curl_cffi
    max_retries: int = 3
    backoff_min_s: float = 2.0
    backoff_max_s: float = 30.0
    request_timeout_s: int = 30
    # Cache
    cache_dir: Path = field(default=Path("data_cache"))
    cache_ttl_hours: float = 12.0        # świeży cache z tego samego dnia jest reużywany
    # KROK 1 — momentum
    momentum_period_days: int = 90       # ile dni historii pobieramy (na 10D/20D z zapasem)
    mom_short_days: int = 10
    mom_long_days: int = 20
    top_sectors: int = 3
    # KROK 4 — płynność
    min_market_cap_usd: float = 500_000_000.0
    min_dollar_volume_usd: float = 10_000_000.0
    dollar_volume_lookback: int = 10


# Macierz ETF-ów (KROK 1 — Compass). Sektor SPDR + tematyczne. Mapowanie ticker→etykieta.
DEFAULT_ETF_MATRIX: dict[str, str] = {
    # SPDR Select Sector
    "XLK": "Technologia", "XLF": "Finanse", "XLE": "Energia", "XLV": "Ochrona zdrowia",
    "XLI": "Przemysł", "XLP": "Dobra podst.", "XLY": "Dobra luks.", "XLB": "Materiały",
    "XLU": "Usługi komun.", "XLRE": "Nieruchomości", "XLC": "Komunikacja",
    # Tematyczne / strategiczne
    "SMH": "Półprzewodniki", "SOXX": "Półprzewodniki (iShares)", "ITA": "Obronność",
    "KRE": "Banki regionalne", "XBI": "Biotech", "IBB": "Biotech (iShares)",
    "BOTZ": "AI / Robotyka", "ROBO": "Robotyka", "URA": "Uran",
    "ESPO": "Gaming / Esport", "NERD": "Gaming (Roundhill)", "HERO": "Gaming (Global X)",
    "IGV": "Software", "CIBR": "Cyberbezpieczeństwo", "HACK": "Cyber (ETFMG)",
    "TAN": "Solar", "LIT": "Lit / Baterie", "XOP": "Ropa / Gaz E&P",
    "ARKK": "Innowacje (ARK)", "KWEB": "China Internet", "JETS": "Linie lotnicze",
    "XHB": "Budownictwo mieszk.",
}


# ─────────────────────────────────────────────────────────────────────────────
# Struktury wynikowe
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FetchResult:
    """Wynik pobrania OHLCV dla paczki tickerów, z flagami degradacji."""
    data: dict[str, pd.DataFrame]                  # ticker → DataFrame OHLCV
    ok_tickers: list[str] = field(default_factory=list)
    failed_tickers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "yfinance"

    @property
    def degraded(self) -> bool:
        return bool(self.failed_tickers) or bool(self.warnings)


@dataclass
class SectorMomentum:
    """Momentum pojedynczego ETF-u (sektora)."""
    ticker: str
    label: str
    mom_short: Optional[float]      # zwrot za mom_short_days (np. 10D), ułamek (0.05 = +5%)
    mom_long: Optional[float]       # zwrot za mom_long_days (np. 20D)
    score: Optional[float]          # złożony wynik rankingowy
    last_close: Optional[float]


# ─────────────────────────────────────────────────────────────────────────────
# Anti-Ban Shield — sesja sieciowa
# ─────────────────────────────────────────────────────────────────────────────
def build_impersonated_session(cfg: EngineConfig):
    """Tworzy sesję curl_cffi imitującą TLS przeglądarki Chrome (omija fingerprinting chmury).
    Zwraca None, gdy curl_cffi niedostępne — wtedy yfinance użyje własnej sesji (mniej odporne)."""
    if cffi_requests is None:
        logger.warning("curl_cffi niedostępne — yfinance użyje domyślnej sesji (większe ryzyko 429).")
        return None
    try:
        sess = cffi_requests.Session(impersonate=cfg.impersonate, timeout=cfg.request_timeout_s)
        logger.info("Anti-Ban Shield: sesja curl_cffi (%s) gotowa.", cfg.impersonate)
        return sess
    except Exception as e:  # pragma: no cover
        logger.warning("Nie udało się zbudować sesji curl_cffi: %s — fallback na domyślną.", e)
        return None


def _chunked(seq: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ─────────────────────────────────────────────────────────────────────────────
# MarketDataFetcher — pobieranie OHLCV + fast_info, z cache i retry
# ─────────────────────────────────────────────────────────────────────────────
class MarketDataFetcher:
    """Odporne pobieranie danych rynkowych z yfinance.

    Cechy:
      • Anti-Ban Shield (curl_cffi),
      • batching (chunk_size) + pacing (sleep),
      • retry z exponential backoff na poziomie pojedynczego chunku,
      • cache parquet per dzień (ponowne uruchomienie tego samego dnia czyta z dysku),
      • graceful degradation: tickery, których nie udało się pobrać, lądują w failed_tickers.
    """

    def __init__(self, cfg: Optional[EngineConfig] = None):
        self.cfg = cfg or EngineConfig()
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = build_impersonated_session(self.cfg)

    # ── cache ────────────────────────────────────────────────────────────────
    def _cache_path(self, key: str) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        safe = key.replace("/", "_").replace(" ", "")
        return self.cfg.cache_dir / f"{safe}_{day}.parquet"

    def _read_cache(self, key: str) -> Optional[pd.DataFrame]:
        p = self._cache_path(key)
        if not p.exists():
            return None
        age_h = (time.time() - p.stat().st_mtime) / 3600.0
        if age_h > self.cfg.cache_ttl_hours:
            return None
        try:
            df = pd.read_parquet(p)
            logger.info("Cache HIT %s (wiek %.1f h, %d wierszy).", p.name, age_h, len(df))
            return df
        except Exception as e:  # pragma: no cover
            logger.warning("Cache %s nieczytelny (%s) — pobieram na nowo.", p.name, e)
            return None

    def _write_cache(self, key: str, df: pd.DataFrame) -> None:
        try:
            self._cache_path(key).parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(self._cache_path(key))
        except Exception as e:  # pragma: no cover
            logger.warning("Nie udało się zapisać cache %s: %s", key, e)

    # ── pobieranie pojedynczego chunku (z retry) ───────────────────────────────
    def _download_chunk(self, tickers: list[str], period: str) -> pd.DataFrame:
        """Pobiera jeden chunk tickerów. Opakowane w retry — rzuca wyjątek po wyczerpaniu prób."""
        if yf is None:
            raise RuntimeError("yfinance niedostępne w tym środowisku")

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.cfg.max_retries),
            wait=wait_exponential(multiplier=self.cfg.backoff_min_s, max=self.cfg.backoff_max_s),
        )
        def _do() -> pd.DataFrame:
            kwargs: dict[str, Any] = dict(
                tickers=tickers, period=period, interval="1d",
                group_by="ticker", auto_adjust=False, threads=True, progress=False,
            )
            if self._session is not None:
                kwargs["session"] = self._session
            df = yf.download(**kwargs)
            if df is None or len(df) == 0:
                raise RuntimeError(f"yfinance zwrócił pusto dla {len(tickers)} tickerów")
            return df

        return _do()

    # ── publiczne API ──────────────────────────────────────────────────────────
    def fetch_ohlcv(self, tickers: list[str], period: Optional[str] = None) -> FetchResult:
        """Pobiera OHLCV dla listy tickerów. Dzieli na chunki, pacing, cache, degradacja.
        Zwraca FetchResult: {ticker: DataFrame[Open,High,Low,Close,Adj Close,Volume]}."""
        period = period or f"{self.cfg.momentum_period_days}d"
        result = FetchResult(data={})
        tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
        if not tickers:
            result.warnings.append("pusta lista tickerów")
            return result

        chunks = list(_chunked(tickers, self.cfg.chunk_size))
        logger.info("Pobieram %d tickerów w %d chunkach (po ≤%d).",
                    len(tickers), len(chunks), self.cfg.chunk_size)

        for idx, chunk in enumerate(chunks):
            cache_key = f"ohlcv_{period}_{'_'.join(chunk[:3])}_{len(chunk)}"
            cached = self._read_cache(cache_key)
            if cached is not None:
                self._split_into_result(cached, chunk, result)
                continue
            try:
                raw = self._download_chunk(chunk, period)
                self._write_cache(cache_key, raw)
                self._split_into_result(raw, chunk, result)
            except Exception as e:
                logger.error("Chunk %d/%d padł po retry: %s", idx + 1, len(chunks), e)
                result.failed_tickers.extend(chunk)
                result.warnings.append(f"chunk {idx + 1} nieudany: {e}")
            if idx < len(chunks) - 1:
                time.sleep(self.cfg.pace_seconds)  # pacing — nie wal w API z prędkością światła

        logger.info("Pobrano OK: %d, nieudane: %d.", len(result.ok_tickers), len(result.failed_tickers))
        return result

    def _split_into_result(self, raw: pd.DataFrame, chunk: list[str], result: FetchResult) -> None:
        """Rozbija ramkę yfinance (multi-index lub pojedynczą) na {ticker: DataFrame}."""
        cols = raw.columns
        is_multi = isinstance(cols, pd.MultiIndex)
        for tk in chunk:
            try:
                if is_multi:
                    if tk not in cols.get_level_values(0):
                        result.failed_tickers.append(tk)
                        continue
                    sub = raw[tk].dropna(how="all")
                else:
                    sub = raw.dropna(how="all")  # pojedynczy ticker → płaskie kolumny
                if sub is None or len(sub) == 0:
                    result.failed_tickers.append(tk)
                    continue
                result.data[tk] = sub
                result.ok_tickers.append(tk)
            except Exception as e:  # pragma: no cover
                logger.warning("Rozbijanie %s nieudane: %s", tk, e)
                result.failed_tickers.append(tk)

    def fetch_market_cap(self, ticker: str) -> Optional[float]:
        """Kapitalizacja przez fast_info (lekkie, mniejsze ryzyko 429 niż .info). None gdy brak."""
        if yf is None:
            return None
        try:
            t = yf.Ticker(ticker, session=self._session) if self._session else yf.Ticker(ticker)
            fi = getattr(t, "fast_info", None)
            if fi is not None:
                for attr in ("market_cap", "marketCap"):
                    val = None
                    try:
                        val = fi[attr] if isinstance(fi, dict) else getattr(fi, attr, None)
                    except Exception:
                        val = None
                    if val:
                        return float(val)
            return None
        except Exception as e:  # pragma: no cover
            logger.warning("fast_info market cap %s nieudane: %s", ticker, e)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# PortfolioScreener — KROK 1 (Compass)
# ─────────────────────────────────────────────────────────────────────────────
class PortfolioScreener:
    """KROK 1: liczy momentum 10D/20D dla macierzy ETF-ów i wskazuje TOP-N gorących sektorów."""

    def __init__(self, fetcher: MarketDataFetcher, etf_matrix: Optional[dict[str, str]] = None,
                 cfg: Optional[EngineConfig] = None):
        self.fetcher = fetcher
        self.cfg = cfg or fetcher.cfg
        self.etf_matrix = etf_matrix or DEFAULT_ETF_MATRIX

    @staticmethod
    def _trailing_return(close: pd.Series, days: int) -> Optional[float]:
        """Zwrot za ostatnie `days` sesji jako ułamek. None gdy za mało danych."""
        s = close.dropna()
        if len(s) < days + 1:
            return None
        try:
            return float(s.iloc[-1] / s.iloc[-1 - days] - 1.0)
        except Exception:
            return None

    def compute_momentum(self) -> tuple[list[SectorMomentum], list[str]]:
        """Liczy momentum dla całej macierzy. Zwraca (lista SectorMomentum posortowana malejąco, warnings)."""
        tickers = list(self.etf_matrix.keys())
        fetched = self.fetcher.fetch_ohlcv(tickers, period=f"{self.cfg.momentum_period_days}d")
        out: list[SectorMomentum] = []
        for tk in tickers:
            df = fetched.data.get(tk)
            if df is None or "Close" not in df.columns:
                out.append(SectorMomentum(tk, self.etf_matrix[tk], None, None, None, None))
                continue
            close = df["Close"]
            ms = self._trailing_return(close, self.cfg.mom_short_days)
            ml = self._trailing_return(close, self.cfg.mom_long_days)
            score = self._score(ms, ml)
            last = float(close.dropna().iloc[-1]) if len(close.dropna()) else None
            out.append(SectorMomentum(tk, self.etf_matrix[tk], ms, ml, score, last))
        out.sort(key=lambda s: (s.score is not None, s.score or float("-inf")), reverse=True)
        return out, fetched.warnings

    @staticmethod
    def _score(mom_short: Optional[float], mom_long: Optional[float]) -> Optional[float]:
        """Złożony wynik: waży trend krótki i długi. Brak danych → None (ląduje na końcu rankingu).
        Waga 0.4*short + 0.6*long — preferujemy trwałość nad chwilowy skok (anty-MAX-effect)."""
        if mom_short is None and mom_long is None:
            return None
        s = mom_short if mom_short is not None else 0.0
        l = mom_long if mom_long is not None else 0.0
        return 0.4 * s + 0.6 * l

    def top_sectors(self) -> dict[str, Any]:
        """Zwraca TOP-N sektorów + pełny ranking + flagi degradacji (do JSON / raportu)."""
        ranked, warnings = self.compute_momentum()
        valid = [s for s in ranked if s.score is not None]
        top = valid[: self.cfg.top_sectors]
        dead = [s for s in valid if s.score is not None and s.score < 0][-3:]  # najsłabsze (do tarczy)
        return {
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "top_sectors": [self._fmt(s) for s in top],
            "dead_sectors": [self._fmt(s) for s in dead],
            "full_ranking": [self._fmt(s) for s in ranked],
            "warnings": warnings,
            "degraded": bool(warnings) or len(valid) < len(ranked),
        }

    @staticmethod
    def _fmt(s: SectorMomentum) -> dict[str, Any]:
        return {
            "ticker": s.ticker, "label": s.label,
            "mom_short": None if s.mom_short is None else round(s.mom_short, 4),
            "mom_long": None if s.mom_long is None else round(s.mom_long, 4),
            "score": None if s.score is None else round(s.score, 4),
            "last_close": None if s.last_close is None else round(s.last_close, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# LiquidityFilter — KROK 4
# ─────────────────────────────────────────────────────────────────────────────
class LiquidityFilter:
    """KROK 4: odrzuca spółki o zbyt małej kapitalizacji lub zbyt niskim dolarowym obrocie.
    Tarcza przed morderczymi spreadami na XTB. Liczy z już pobranych OHLCV (oszczędza zapytania)."""

    def __init__(self, fetcher: MarketDataFetcher, cfg: Optional[EngineConfig] = None):
        self.fetcher = fetcher
        self.cfg = cfg or fetcher.cfg

    def avg_dollar_volume(self, df: pd.DataFrame) -> Optional[float]:
        """Średni dzienny dolarowy obrót (Close*Volume) z ostatnich N sesji. None gdy brak danych."""
        if df is None or "Close" not in df.columns or "Volume" not in df.columns:
            return None
        tail = df.dropna(subset=["Close", "Volume"]).tail(self.cfg.dollar_volume_lookback)
        if len(tail) == 0:
            return None
        try:
            return float((tail["Close"] * tail["Volume"]).mean())
        except Exception:
            return None

    def screen(self, candidates: list[str]) -> dict[str, Any]:
        """Filtruje listę tickerów-kandydatów. Zwraca przeszłe + odrzucone (z powodem) + warnings.
        GRACEFUL: jeśli brak market cap (np. 429), NIE odrzucamy na ślepo — oznaczamy 'unknown' i przepuszczamy
        po samym dolarowym obrocie, z flagą do weryfikacji."""
        candidates = list(dict.fromkeys(t.strip().upper() for t in candidates if t and t.strip()))
        fetched = self.fetcher.fetch_ohlcv(candidates, period=f"{max(self.cfg.dollar_volume_lookback + 5, 30)}d")
        passed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        warnings: list[str] = list(fetched.warnings)

        for tk in candidates:
            df = fetched.data.get(tk)
            adv = self.avg_dollar_volume(df)
            mcap = self.fetcher.fetch_market_cap(tk)

            if adv is None:
                rejected.append({"ticker": tk, "reason": "brak danych o obrocie (źródło padło?)",
                                 "market_cap": mcap, "avg_dollar_volume": None})
                continue
            if adv < self.cfg.min_dollar_volume_usd:
                rejected.append({"ticker": tk, "reason": f"obrót {adv:,.0f} < próg {self.cfg.min_dollar_volume_usd:,.0f}",
                                 "market_cap": mcap, "avg_dollar_volume": adv})
                continue
            if mcap is None:
                # Graceful: nie znamy kapitalizacji, ale obrót wystarczający → przepuść z flagą.
                warnings.append(f"{tk}: brak market cap — przepuszczony po obrocie, zweryfikuj ręcznie")
                passed.append({"ticker": tk, "market_cap": None, "avg_dollar_volume": adv,
                               "flag": "market_cap_unknown"})
                continue
            if mcap < self.cfg.min_market_cap_usd:
                rejected.append({"ticker": tk, "reason": f"kapitalizacja {mcap:,.0f} < próg {self.cfg.min_market_cap_usd:,.0f}",
                                 "market_cap": mcap, "avg_dollar_volume": adv})
                continue
            passed.append({"ticker": tk, "market_cap": mcap, "avg_dollar_volume": adv, "flag": None})

        return {
            "passed": passed, "rejected": rejected, "warnings": warnings,
            "n_in": len(candidates), "n_passed": len(passed), "n_rejected": len(rejected),
            "degraded": bool(warnings) or bool(fetched.failed_tickers),
        }


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline, bez sieci) — dane syntetyczne
# ─────────────────────────────────────────────────────────────────────────────
def _synthetic_ohlcv(days: int = 60, start: float = 100.0, drift: float = 0.003,
                      vol: float = 1_000_000.0) -> pd.DataFrame:
    """Buduje syntetyczną ramkę OHLCV (rosnący trend) do testów bez sieci."""
    idx = pd.date_range(end=datetime.now(timezone.utc).date(), periods=days, freq="B")
    close = [start * (1 + drift) ** i for i in range(days)]
    return pd.DataFrame({
        "Open": close, "High": [c * 1.01 for c in close], "Low": [c * 0.99 for c in close],
        "Close": close, "Adj Close": close, "Volume": [vol] * days,
    }, index=idx)


def _run_selftest() -> int:
    logger.info("=== SELFTEST core_data_engine (offline) ===")
    cfg = EngineConfig(cache_dir=Path("/tmp/porsche_cache_selftest"))

    # 1) PortfolioScreener.momentum + ranking — wstrzykujemy fetcher z syntetycznymi danymi
    class _FakeFetcher(MarketDataFetcher):
        def __init__(self, c):
            self.cfg = c
            self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
            self._session = None
        def fetch_ohlcv(self, tickers, period=None):
            res = FetchResult(data={})
            for i, tk in enumerate(tickers):
                # różne drifty, by ranking miał sens; jeden ticker "martwy" (brak danych)
                if tk == "DEAD":
                    res.failed_tickers.append(tk); res.warnings.append("DEAD: brak danych"); continue
                res.data[tk] = _synthetic_ohlcv(drift=0.002 + (i % 5) * 0.001,
                                                vol=5_000_000 if tk != "TINY" else 100_000)
                res.ok_tickers.append(tk)
            return res
        def fetch_market_cap(self, ticker):
            return {"BIG": 9e10, "TINY": 3e8}.get(ticker, 2e9)  # TINY < 500M → odrzut

    ff = _FakeFetcher(cfg)
    scr = PortfolioScreener(ff, etf_matrix={"XLK": "Tech", "XLE": "Energia", "URA": "Uran", "DEAD": "Pusty"}, cfg=cfg)
    top = scr.top_sectors()
    assert top["top_sectors"], "ranking pusty!"
    assert top["degraded"], "DEAD powinien zaznaczyć degradację"
    print("  [OK] PortfolioScreener: TOP =", [s["ticker"] for s in top["top_sectors"]],
          "| degraded =", top["degraded"])

    # 2) LiquidityFilter — TINY odpada (mcap), low-volume odpada
    lf = LiquidityFilter(ff, cfg=cfg)
    screen = lf.screen(["BIG", "TINY", "DEAD"])
    passed = [p["ticker"] for p in screen["passed"]]
    rejected = [r["ticker"] for r in screen["rejected"]]
    print("  [OK] LiquidityFilter: passed =", passed, "| rejected =", rejected)
    assert "BIG" in passed, "BIG (duża kapitalizacja+obrót) powinien przejść"
    assert "TINY" in rejected, "TINY (kapitalizacja <500M) powinien odpaść"

    # 3) avg_dollar_volume liczy się poprawnie
    adv = lf.avg_dollar_volume(_synthetic_ohlcv(vol=2_000_000))
    assert adv and adv > 0, "dolarowy obrót powinien być dodatni"
    print(f"  [OK] avg_dollar_volume = {adv:,.0f} USD")

    # 4) trailing return
    r = PortfolioScreener._trailing_return(_synthetic_ohlcv(days=30, drift=0.01)["Close"], 20)
    print(f"  [OK] trailing_return(20D) = {r:+.2%}")
    assert r is not None and r > 0

    print("\n=== SELFTEST OK — Moduł 1 gotowy ===")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Moduł 1: Data Pipeline Base")
    ap.add_argument("--selftest", action="store_true", help="uruchom testy offline (bez sieci)")
    ap.add_argument("--scan", action="store_true", help="realny skan macierzy ETF (wymaga sieci + yfinance)")
    args = ap.parse_args()

    if args.selftest:
        return _run_selftest()
    if args.scan:
        cfg = EngineConfig()
        fetcher = MarketDataFetcher(cfg)
        screener = PortfolioScreener(fetcher, cfg=cfg)
        result = screener.top_sectors()
        import json
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
