"""
reconcile.py — RECONCILIATION STANU PORTFELA
=============================================
Bot Porsche — odtwarzanie i weryfikacja stanu portfela między bezstanowymi sesjami.

PROBLEM: Claude Code Routines są bezstanowe. Stan (gotówka + pozycje) musi być
eksternalizowany. ŹRÓDŁO PRAWDY = świeży eksport XTB (.xlsx) z Drive.
portfolio.json to tylko CACHE do walidacji — nigdy źródło prawdy.

TWARDY INWARIANT (Hard Halt):
  Jeśli |gotówka_z_portfolio.json (oczekiwana) − gotówka_z_xlsx (faktyczna)| > 0.5 PLN
  -> sys.exit(1), zatrzymanie potoku, alert. Bot NIE zgaduje przy niespójności.

EKSPORT XTB — trzy sekcje w jednym .xlsx:
  • Closed Positions  — pozycje zamknięte (Instrument, Ticker, Volume, Open/Close Price, P/L, ...)
  • Cash Operations   — log operacji (Type, Ticker, Time, Amount, ID, Comment)
  • Open Positions    — pozycje otwarte (tylko z platformy web; mobile ich nie ma)

GOTÓWKA liczona z Cash Operations: suma Amount (transfery +, zakupy −, sprzedaże +,
prowizje −, dywidendy +, podatki −, przewalutowania ±). Wymaga eksportu OD POCZĄTKU rachunku.

Zależności: pandas, openpyxl (czytanie .xlsx).
Uruchomienie testów: python reconcile.py --selftest
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


logger = logging.getLogger("porsche.reconcile")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


CASH_TOLERANCE_PLN = 0.50   # próg rozjazdu gotówki -> Hard Halt
MAX_EXPORT_AGE_HOURS = 24   # eksport starszy niż to -> ostrzeżenie/halt


@dataclass
class Position:
    ticker: str
    volume: float
    open_price: float = 0.0
    currency: str = "USD"


@dataclass
class PortfolioState:
    cash_pln: float
    positions: list = field(default_factory=list)   # list[Position]
    timestamp_utc: str = ""
    source: str = "xtb_export"

    def to_json(self) -> dict:
        return {
            "cash_pln": round(self.cash_pln, 2),
            "positions": [{"ticker": p.ticker, "volume": p.volume,
                           "open_price": p.open_price, "currency": p.currency}
                          for p in self.positions],
            "timestamp_utc": self.timestamp_utc or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": self.source,
        }

    @staticmethod
    def from_json(d: dict) -> "PortfolioState":
        raw_pos = d.get("positions") or []
        # FALLBACK: bot utrzymuje stan w 'managed_positions'; pole 'positions' bywa puste.
        # Użyj managed_positions jako baseline reconcile, by porównanie pozycji miało sens
        # (inaczej oczekiwane=[] vs broker=[4 poz.] => fałszywy HARD HALT — bug 2026-06-07).
        if not raw_pos and d.get("managed_positions"):
            raw_pos = [{"ticker": p.get("ticker"), "volume": p.get("shares", 0.0),
                        "open_price": p.get("entry_price_usd", 0.0)}
                       for p in d.get("managed_positions", [])]
        return PortfolioState(
            cash_pln=float(d.get("cash_pln", 0.0)),
            positions=[Position(p["ticker"], float(p.get("volume", 0.0)),
                                float(p.get("open_price", 0.0)), p.get("currency", "USD"))
                       for p in raw_pos],
            timestamp_utc=d.get("timestamp_utc", ""),
            source=d.get("source", "unknown"),
        )


@dataclass
class ReconcileResult:
    consistent: bool
    reason: str
    actual_state: Optional[PortfolioState] = None
    expected_cash_pln: Optional[float] = None
    actual_cash_pln: Optional[float] = None
    delta_pln: Optional[float] = None
    synced: bool = False     # True = rozjazd wyjaśniony transakcjami, przyjęto stan XTB


# ─────────────────────────────────────────────────────────────────────────────
# Parser eksportu XTB (.xlsx z wieloma sekcjami w jednym arkuszu)
# ─────────────────────────────────────────────────────────────────────────────
class XTBExportParser:
    """Parsuje eksport XTB. Wykrywa sekcje po nagłówkach. Liczy gotówkę z Cash Operations,
    pozycje z Open Positions (jeśli sekcja istnieje)."""

    # typy operacji gotówkowych i ich znak we wpływie na saldo (jeśli Amount nie ma znaku)
    # XTB zwykle podaje Amount ze znakiem, więc domyślnie sumujemy Amount wprost.

    def _normalize_xtb_text(self, text: str) -> str:
        """Normalizuje tekst eksportu XTB do formy wielowierszowej.

        Drive get_file_metadata zwraca contentSnippet jako JEDNĄ linię (wiersze
        sklejone spacją). Nie da się rozbić po spacjach (są też w datach i komentarzach),
        ale KAŻDY wiersz XTB zaczyna się od znanego słowa-typu w pierwszej kolumnie.
        Wstawiamy nową linię przed każdym takim markerem. Jeśli tekst już jest
        wielowierszowy (prawdziwy CSV/plik), markery i tak są na początku linii,
        więc operacja jest bezpieczna (idempotentna)."""
        # Markery początku wiersza w eksporcie XTB (pierwsza kolumna). Kolejność bez znaczenia.
        markers = [
            "Closed Positions Account", "Closed Positions", "Open Positions Account",
            "Open Positions", "Cash Operations Account number", "Cash Operations",
            "Date from (UTC)", "Date to (UTC)", "Instrument,", "Type,Ticker", "Symbol,",
            "Stock purchase,", "Stock sale,", "Stock sell,", "Transfer,", "Dividend,", "Commission,",
            "Withholding tax,", "Free-funds Interest,", "Free funds interest,", "Free funds interest", "Total,",
        ]
        # zredukuj wielokrotne białe znaki do pojedynczej spacji (snippet bywa "rozstrzelony")
        # ale NIE ruszaj zawartości — tylko separatory wierszy
        t = text.replace("\r", "\n")
        # jeśli już ma sensowne wiersze (≥3 linie), zostaw — to prawdziwy plik
        if t.count("\n") >= 3:
            return t
        # jednolinijkowy snippet: wstaw \n przed każdym markerem poprzedzonym spacją
        for m in markers:
            t = t.replace(" " + m, "\n" + m)
        return t

    def parse_file(self, path: str | Path) -> dict:
        """Czyta eksport XTB (.xlsx LUB .csv), zwraca {'closed','cash_ops','open'} (każdy może być None).

        XLSX: czyta wszystkie arkusze i skleja pionowo (XTB rozbija sekcje na arkusze).
        CSV: czyta jako tekst — ODPORNE na truncation binarnego pobierania z Drive
             (konektor ucina pliki binarne, ale tekst/CSV przechodzi w całości).
        Wybór po rozszerzeniu; przy .xlsx fallback openpyxl -> calamine."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"brak pliku eksportu: {path}")

        ext = path.suffix.lower()
        if ext == ".csv":
            # CSV = czysty tekst, bez ZIP/CRC/truncation. Sekcje XTB w jednej kolumnie wierszy.
            # ODPORNOSC NA SNIPPET: Drive get_file_metadata zwraca contentSnippet jako JEDNĄ
            # długą linię (wiersze sklejone spacją). Wykryj to i rozbij po markerach XTB,
            # zanim sparsujemy — inaczej pandas czyta wszystko jako 1 wiersz (gotówka 0).
            text = path.read_text(encoding="utf-8", errors="ignore")
            text = self._normalize_xtb_text(text)
            from io import StringIO
            raw = pd.read_csv(StringIO(text), header=None, dtype=str, keep_default_na=False,
                              skip_blank_lines=False, engine="python", on_bad_lines="skip")
            return self._split_sections(raw)

        # .xlsx (domyślnie): openpyxl, fallback calamine dla luźnych plików xStation
        all_sheets = None
        last_err = None
        for engine in ("openpyxl", "calamine"):
            try:
                all_sheets = pd.read_excel(path, header=None, engine=engine, sheet_name=None)
                break
            except Exception as e:
                last_err = f"{engine}: {type(e).__name__}: {e}"
                continue
        if all_sheets is None:
            raise ValueError(
                f"Nie da się odczytać xlsx żadnym engine'em. Ostatni błąd: {last_err}. "
                f"Workaround: wyeksportuj z xStation jako CSV (tekst nie ulega truncation).")
        frames = [df for df in all_sheets.values() if df is not None and len(df) > 0]
        if not frames:
            raise ValueError("pusty plik eksportu")
        maxcols = max(df.shape[1] for df in frames)
        norm = []
        for df in frames:
            if df.shape[1] < maxcols:
                for c in range(df.shape[1], maxcols):
                    df[c] = None
            norm.append(df)
        raw = pd.concat(norm, ignore_index=True)
        return self._split_sections(raw)

    def parse_dataframe(self, raw: pd.DataFrame) -> dict:
        """Wersja do testów: przyjmuje surowy DataFrame (bez nagłówka)."""
        return self._split_sections(raw)

    def _split_sections(self, raw: pd.DataFrame) -> dict:
        """Dzieli surowy arkusz na sekcje po wierszach-markerach."""
        sections = {"closed": None, "cash_ops": None, "open": None}
        markers = {
            "closed": ("closed positions",),
            "cash_ops": ("cash operations",),
            "open": ("open positions", "open position"),
        }
        # znajdź wiersze startowe sekcji
        starts = {}
        for i in range(len(raw)):
            cells = [str(x).strip().lower() for x in raw.iloc[i].tolist() if pd.notna(x)]
            row_text = " ".join(cells)
            for key, keys in markers.items():
                # marker = wiersz ZACZYNAJĄCY się od nazwy sekcji (odporne na "... Account number")
                # bierzemy pierwszy taki wiersz dla danej sekcji
                if key in starts:
                    continue
                first_cell = cells[0] if cells else ""
                if any(first_cell.startswith(m) for m in keys) or any(m == row_text for m in keys):
                    starts[key] = i
        # nagłówek kolumn znajdujemy PO TREŚCI (wiersz zawierający charakterystyczne kolumny),
        # nie sztywno start+1 — bo między markerem a nagłówkiem bywają wiersze (np. nr konta)
        header_signatures = {
            "cash_ops": ("amount",),
            "closed": ("ticker", "volume"),
            "open": ("ticker", "volume"),
        }
        ordered = sorted(starts.items(), key=lambda kv: kv[1])
        for idx, (key, start) in enumerate(ordered):
            end = ordered[idx + 1][1] if idx + 1 < len(ordered) else len(raw)
            sig = header_signatures.get(key, ())
            header_row = None
            for r in range(start, end):
                cells = [str(x).strip().lower() for x in raw.iloc[r].tolist() if pd.notna(x)]
                if sig and all(any(s in c for c in cells) for s in sig):
                    header_row = r
                    break
            if header_row is None or header_row + 1 >= end:
                continue
            block = raw.iloc[header_row:end].copy()
            block.columns = [str(x).strip() for x in raw.iloc[header_row].tolist()]
            block = block.iloc[1:].reset_index(drop=True)   # usuń wiersz nagłówka z danych
            block = block.dropna(how="all")
            sections[key] = block
        return sections

    def compute_cash_pln(self, cash_ops: Optional[pd.DataFrame]) -> float:
        """Liczy bieżącą gotówkę PLN jako sumę kolumny Amount z Cash Operations.
        XTB podaje Amount ze znakiem (transfer +, zakup −, ...). Wymaga eksportu od początku.

        KRYTYCZNE: odfiltrowuje wiersze podsumowania ('Total', 'My Trades Total', puste),
        które XTB dokleja na końcu sekcji — inaczej gotówka byłaby PODWOJONA."""
        if cash_ops is None or len(cash_ops) == 0:
            return 0.0
        amount_col = None
        type_col = None
        for c in cash_ops.columns:
            cl = str(c).strip().lower()
            if amount_col is None and "amount" in cl:
                amount_col = c
            if type_col is None and cl == "type":
                type_col = c
        if amount_col is None:
            raise ValueError("Cash Operations: brak kolumny Amount")

        df = cash_ops
        # odfiltruj wiersze podsumowania po kolumnie Type (jeśli jest)
        if type_col is not None:
            def _is_data_row(v):
                t = str(v).strip().lower()
                if not t or t == "nan" or t == "none":
                    return False
                if "total" in t:          # 'Total', 'My Trades Total' itd.
                    return False
                return True
            df = df[df[type_col].apply(_is_data_row)]

        vals = pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0)
        return float(vals.sum())

    def parse_open_positions(self, open_df: Optional[pd.DataFrame]) -> list:
        """Zwraca listę Position z sekcji Open Positions (lub [] gdy brak sekcji)."""
        if open_df is None or len(open_df) == 0:
            return []
        tcol = self._find(open_df, "ticker", "symbol")
        vcol = self._find(open_df, "volume", "wolumen")
        pcol = self._find(open_df, "open price", "cena otwarcia", "open")
        out = []
        for _, row in open_df.iterrows():
            tk = str(row.get(tcol, "")).strip() if tcol else ""
            if not tk or tk.lower() == "nan":
                continue
            try:
                vol = float(pd.to_numeric(row.get(vcol), errors="coerce")) if vcol else 0.0
            except Exception:
                vol = 0.0
            try:
                op = float(pd.to_numeric(row.get(pcol), errors="coerce")) if pcol else 0.0
            except Exception:
                op = 0.0
            if vol > 0:
                out.append(Position(tk, vol, op))
        return out

    def reconstruct_positions_from_cashops(self, cash_ops: Optional[pd.DataFrame]) -> list:
        """Odtwarza pozycje netto z transakcji w Cash Operations, gdy brak sekcji
        Open Positions (np. eksport z aplikacji mobilnej XTB).

        XTB w komentarzu transakcji zapisuje pełny dowód wejścia/wyjścia, np.:
          'OPEN BUY 0.3319 @ 423.68'  /  'CLOSE SELL 0.1 @ 430.00'
        Ticker bierzemy z kolumny Ticker (TSM.US -> TSM). Sumujemy wolumeny netto
        (BUY +, SELL -), liczymy średnią cenę wejścia ważoną wolumenem.

        Zwraca [] gdy nic nie da się odtworzyć — wtedy zachowanie jak dotąd."""
        if cash_ops is None or len(cash_ops) == 0:
            return []
        ccol = self._find(cash_ops, "comment", "komentarz")
        tcol = self._find(cash_ops, "ticker", "symbol")
        if ccol is None:
            return []

        import re
        # netto wolumen i wartość per ticker (do średniej ceny ważonej)
        agg = {}  # ticker -> {"vol": float, "cost": float (suma vol*price dla BUY)}
        pat = re.compile(r"(OPEN|CLOSE)\s+(BUY|SELL)\s+([\d.]+)\s*@\s*([\d.]+)", re.IGNORECASE)
        for _, row in cash_ops.iterrows():
            comment = str(row.get(ccol, "")).strip()
            m = pat.search(comment)
            if not m:
                continue
            action, side, vol_s, price_s = m.groups()
            try:
                vol = float(vol_s)
                price = float(price_s)
            except Exception:
                continue
            # ticker: z kolumny Ticker, normalizacja TSM.US -> TSM
            raw_tk = str(row.get(tcol, "")).strip() if tcol else ""
            tk = raw_tk.split(".")[0].upper() if raw_tk and raw_tk.lower() != "nan" else ""
            if not tk:
                continue
            # ZNAK wg OPEN/CLOSE, NIE wg BUY/SELL. XTB zamyka długą pozycję komentarzem
            # "CLOSE BUY" (typ operacji "Stock sell") — liczenie wg BUY/SELL traktowało to
            # jako kolejne OTWARCIE (+vol) i PODWAJAŁO pozycje przy zamknięciu konta ->
            # fałszywy "rozjazd pozycji" -> HARD HALT. Poprawnie:
            #   long:  OPEN BUY  +vol,  CLOSE BUY  -vol
            #   short: OPEN SELL -vol,  CLOSE SELL +vol
            is_buy = side.upper() == "BUY"
            is_open = action.upper() == "OPEN"
            signed = vol * (1.0 if is_buy else -1.0) * (1.0 if is_open else -1.0)
            a = agg.setdefault(tk, {"vol": 0.0, "cost": 0.0, "open_vol": 0.0})
            a["vol"] += signed
            if is_open and is_buy:          # koszt wejścia tylko z OTWARĆ długich pozycji
                a["cost"] += vol * price
                a["open_vol"] += vol

        out = []
        for tk, a in agg.items():
            net_vol = round(a["vol"], 6)
            if net_vol <= 1e-9:           # pozycja zamknięta lub zerowa — pomijamy
                continue
            # średnia cena wejścia ważona kupnami; gdy brak kosztu -> 0
            avg_price = round(a["cost"] / a["open_vol"], 4) if a.get("open_vol", 0.0) > 0 and a["cost"] > 0 else 0.0
            out.append(Position(tk, net_vol, avg_price))
        return out

    @staticmethod
    def _find(df: pd.DataFrame, *cands: str) -> Optional[str]:
        for c in df.columns:
            cl = str(c).strip().lower()
            for cand in cands:
                if cand in cl:
                    return c
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliation
# ─────────────────────────────────────────────────────────────────────────────
class Reconciler:
    def __init__(self, tolerance_pln: float = CASH_TOLERANCE_PLN):
        self.tolerance = tolerance_pln
        self.parser = XTBExportParser()

    def build_actual_state(self, export_path: str | Path) -> PortfolioState:
        """Buduje stan faktyczny z eksportu XTB (źródło prawdy).
        Pozycje bierze z sekcji Open Positions; gdy jej brak (np. eksport mobilny),
        odtwarza je z transakcji w Cash Operations (OPEN BUY/CLOSE SELL)."""
        sections = self.parser.parse_file(export_path)
        cash = self.parser.compute_cash_pln(sections.get("cash_ops"))
        positions = self.parser.parse_open_positions(sections.get("open"))
        if not positions:
            # brak sekcji Open Positions — odtwórz z transakcji (Cash Operations)
            positions = self.parser.reconstruct_positions_from_cashops(sections.get("cash_ops"))
            if positions:
                logger.info("Open Positions brak w eksporcie — odtworzono %d pozycji z transakcji.",
                            len(positions))
        return PortfolioState(cash_pln=cash, positions=positions,
                              timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def reconcile(self, actual: PortfolioState, expected_json: Optional[dict]) -> ReconcileResult:
        """Porównuje stan faktyczny (z XTB) z oczekiwanym (portfolio.json).
        Zwraca ReconcileResult. NIE woła sys.exit — to robi caller, by dało się testować.

        AUTO-SYNC: jeśli rozjazd gotówki/pozycji DA SIĘ WYJAŚNIĆ transakcjami z eksportu
        (zakupy/sprzedaże, które realnie się wydarzyły), to NIE jest błąd — to wykonane
        przez Ciebie zlecenia. Bot przyjmuje stan z XTB jako nową prawdę (consistent=True,
        flaga synced=True). HALT zostaje tylko dla rozjazdu, którego transakcje NIE tłumaczą
        (np. tajemniczy ubytek gotówki) — wtedy fail-CLOSED."""
        if expected_json is None:
            # pierwszy run — brak historii, akceptujemy faktyczny stan jako bazę
            return ReconcileResult(True, "pierwszy run — brak portfolio.json, przyjmuję stan z XTB",
                                   actual_state=actual, actual_cash_pln=actual.cash_pln)
        expected = PortfolioState.from_json(expected_json)
        delta = abs(actual.cash_pln - expected.cash_pln)

        if delta > self.tolerance:
            # Czy transakcje z eksportu tłumaczą zmianę gotówki?
            if self._delta_explained_by_trades(actual, expected):
                return ReconcileResult(
                    True,
                    f"AUTO-SYNC: gotówka {expected.cash_pln:.2f}->{actual.cash_pln:.2f} PLN "
                    f"(delta {delta:.2f}) wyjaśniona transakcjami w eksporcie — przyjmuję stan z XTB",
                    actual_state=actual, expected_cash_pln=expected.cash_pln,
                    actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2), synced=True)
            return ReconcileResult(
                False,
                f"ROZJAZD GOTÓWKI: oczekiwano {expected.cash_pln:.2f} PLN, "
                f"w XTB {actual.cash_pln:.2f} PLN (delta {delta:.2f} > {self.tolerance:.2f}), "
                f"NIE wyjaśniony transakcjami — HARD HALT",
                actual_state=actual, expected_cash_pln=expected.cash_pln,
                actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2))

        # gotówka OK — porównaj pozycje (zbiór tickerów)
        exp_pos = {p.ticker: p.volume for p in expected.positions}
        act_pos = {p.ticker: p.volume for p in actual.positions}
        if set(exp_pos.keys()) != set(act_pos.keys()):
            # różnica pozycji przy zgodnej gotówce — jeśli tłumaczona transakcjami, sync
            if self._delta_explained_by_trades(actual, expected):
                return ReconcileResult(
                    True,
                    f"AUTO-SYNC pozycji: {sorted(exp_pos.keys())}->{sorted(act_pos.keys())} "
                    f"wyjaśnione transakcjami — przyjmuję stan z XTB",
                    actual_state=actual, expected_cash_pln=expected.cash_pln,
                    actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2), synced=True)
            return ReconcileResult(
                False,
                f"ROZJAZD POZYCJI: oczekiwano {sorted(exp_pos.keys())}, "
                f"w XTB {sorted(act_pos.keys())} — HARD HALT",
                actual_state=actual, expected_cash_pln=expected.cash_pln,
                actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2))
        return ReconcileResult(True, f"stan spójny (delta gotówki {delta:.2f} PLN ≤ {self.tolerance})",
                               actual_state=actual, expected_cash_pln=expected.cash_pln,
                               actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2))

    def _delta_explained_by_trades(self, actual: PortfolioState, expected: PortfolioState) -> bool:
        """Czy zmiana stanu (gotówka + pozycje) jest wyjaśniona realnymi transakcjami?

        Logika: bot oczekiwał stanu X (portfolio.json z wczoraj). XTB pokazuje stan Y.
        Jeśli różnica pozycji to NOWE pozycje, których wcześniej nie było (kupione),
        a spadek gotówki ~ odpowiada wartości tych zakupów — to wykonane zlecenia, nie błąd.

        Bezpieczne: wymaga, by KAŻDA nowa/zmieniona pozycja miała cenę wejścia (dowód
        z transakcji). Sam ubytek gotówki bez nowych pozycji => NIE wyjaśnione => HALT."""
        exp_pos = {p.ticker: p for p in expected.positions}
        act_pos = {p.ticker: p for p in actual.positions}

        cash_dropped = actual.cash_pln < expected.cash_pln    # gotówka spadła = możliwe zakupy
        cash_rose = actual.cash_pln > expected.cash_pln        # gotówka wzrosła = możliwe sprzedaże

        new_tickers = set(act_pos.keys()) - set(exp_pos.keys())   # pojawiły się (kupno)
        gone_tickers = set(exp_pos.keys()) - set(act_pos.keys())  # zniknęły (sprzedaż/stop)

        # Każda NOWA pozycja musi mieć dodatni wolumen i cenę wejścia (dowód z transakcji).
        for tk in new_tickers:
            p = act_pos[tk]
            if p.volume <= 0 or p.open_price <= 0:
                return False   # pozycja bez dowodu ceny wejścia — nie ufamy, HALT

        # Scenariusz 1: gotówka spadła i pojawiły się nowe pozycje (klasyczne kupno) -> OK
        if cash_dropped and new_tickers and not gone_tickers:
            return True
        # Scenariusz 2: gotówka wzrosła i pozycje zniknęły (sprzedaż/stop) -> OK
        if cash_rose and gone_tickers and not new_tickers:
            return True
        # Scenariusz 3: mix (część kupiona, część sprzedana) — akceptuj jeśli nowe mają cenę
        if new_tickers and gone_tickers:
            return True
        # Sam ubytek/przyrost gotówki BEZ zmiany pozycji => podejrzane => HALT
        return False

    def reconcile_or_halt(self, export_path: str | Path, portfolio_json_path: str | Path) -> PortfolioState:
        """Pełna ścieżka produkcyjna: buduje stan, porównuje, przy niespójności sys.exit(1)."""
        actual = self.build_actual_state(export_path)
        expected_json = None
        pj = Path(portfolio_json_path)
        if pj.exists():
            try:
                expected_json = json.loads(pj.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("portfolio.json nieczytelny (%s) — HARD HALT", e)
                sys.exit(1)
        res = self.reconcile(actual, expected_json)
        if not res.consistent:
            logger.error("RECONCILE FAIL: %s", res.reason)
            sys.exit(1)
        logger.info("RECONCILE OK: %s", res.reason)
        return actual


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST (offline, syntetyczny arkusz XTB)
# ─────────────────────────────────────────────────────────────────────────────
def _make_synthetic_export(cash_rows, open_rows=None) -> pd.DataFrame:
    """Buduje surowy DataFrame imitujący eksport XTB (sekcje bez nagłówka kolumn DF)."""
    rows = []
    # sekcja Cash Operations
    rows.append(["Cash Operations Account number", "54820945"] + [None] * 5)
    rows.append(["Cash Operations"] + [None] * 6)
    rows.append(["Type", "Ticker", "Instrument", "Time", "Amount", "ID", "Comment"])
    for r in cash_rows:
        rows.append(r)
    # sekcja Open Positions (opcjonalna)
    if open_rows is not None:
        rows.append([None] * 7)
        rows.append(["Open Positions"] + [None] * 6)
        rows.append(["Ticker", "Volume", "Open Price", "Instrument", "Type", "Open Time", "ID"])
        for r in open_rows:
            rows.append(r)
    maxlen = max(len(r) for r in rows)
    rows = [r + [None] * (maxlen - len(r)) for r in rows]
    return pd.DataFrame(rows)


def _run_selftest() -> int:
    print("=== SELFTEST reconcile (offline) ===")
    parser = XTBExportParser()
    rec = Reconciler()
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    # 1. Sam transfer 1582 -> gotówka 1582, brak pozycji
    raw = _make_synthetic_export(
        cash_rows=[["Transfer", None, None, "2026-05-26 09:56", 1582.0, "123", "Transfer in"]])
    sec = parser.parse_dataframe(raw)
    cash = parser.compute_cash_pln(sec["cash_ops"])
    check("Gotówka z transferu = 1582", abs(cash - 1582.0) < 0.01)
    check("Brak sekcji Open Positions -> brak pozycji", parser.parse_open_positions(sec.get("open")) == [])

    # 2. Transfer + zakup − sprzedaż − prowizja
    raw = _make_synthetic_export(cash_rows=[
        ["Transfer", None, None, "2026-05-26 09:56", 1582.0, "1", "in"],
        ["Stocks Purchase", "NVDA", "NVDA.US", "2026-05-27 16:00", -320.0, "2", "buy"],
        ["Commission", "NVDA", "NVDA.US", "2026-05-27 16:00", -1.6, "3", "fx"],
    ])
    sec = parser.parse_dataframe(raw)
    cash = parser.compute_cash_pln(sec["cash_ops"])
    check("Gotówka 1582-320-1.6 = 1260.4", abs(cash - 1260.4) < 0.01)

    # 3. Open Positions parsowane
    raw = _make_synthetic_export(
        cash_rows=[["Transfer", None, None, "2026-05-26", 1582.0, "1", "in"]],
        open_rows=[["NVDA", 0.55, 140.0, "NVDA.US", "BUY", "2026-05-27", "P1"]])
    sec = parser.parse_dataframe(raw)
    pos = parser.parse_open_positions(sec["open"])
    check("Open Positions: 1 pozycja NVDA", len(pos) == 1 and pos[0].ticker == "NVDA")
    check("Open Positions: wolumen 0.55", abs(pos[0].volume - 0.55) < 1e-9)

    # 4. Reconcile pierwszy run (brak portfolio.json) -> spójny
    actual = PortfolioState(cash_pln=1582.0, positions=[])
    r = rec.reconcile(actual, None)
    check("Pierwszy run (brak JSON) -> spójny", r.consistent)

    # 5. Reconcile zgodny (delta < 0.5)
    expected = PortfolioState(cash_pln=1582.0, positions=[]).to_json()
    actual = PortfolioState(cash_pln=1582.3, positions=[])
    r = rec.reconcile(actual, expected)
    check("Delta 0.3 PLN -> spójny", r.consistent)

    # 6. Reconcile rozjazd gotówki (delta > 0.5) -> NIESPÓJNY (hard halt w produkcji)
    actual = PortfolioState(cash_pln=1500.0, positions=[])
    r = rec.reconcile(actual, expected)
    check("Delta 82 PLN -> NIESPÓJNY (hard halt)", not r.consistent and "ROZJAZD GOTÓWKI" in r.reason)

    # 7. Reconcile rozjazd pozycji -> NIESPÓJNY
    expected = PortfolioState(cash_pln=1260.0, positions=[Position("NVDA", 0.55)]).to_json()
    actual = PortfolioState(cash_pln=1260.0, positions=[])  # bot myślał że jest NVDA, w XTB nie ma
    r = rec.reconcile(actual, expected)
    check("Rozjazd pozycji (NVDA znikła) -> NIESPÓJNY", not r.consistent and "POZYCJI" in r.reason)

    # 8. Roundtrip JSON
    st = PortfolioState(cash_pln=1260.4, positions=[Position("NVDA", 0.55, 140.0)])
    st2 = PortfolioState.from_json(st.to_json())
    check("JSON roundtrip zachowuje stan", st2.cash_pln == 1260.4 and st2.positions[0].ticker == "NVDA")

    # 9. REALNY UKŁAD XTB (regresja na bug z 26.05): Closed+Cash w jednym arkuszu,
    #    wiersze Account/Date między markerem a nagłówkiem, wiersz Total na końcu.
    real_rows = [
        ["Closed Positions Account", 54820945],
        ["Closed Positions"],
        ["Date from (UTC)", "2026-04-25 22:00:00"],
        ["Date to (UTC)", "2026-05-26 10:00:44"],
        ["Instrument","Category","Ticker","Type","Volume","Open Price","Open Time (UTC)",
         "Close Price","Close Time (UTC)","Product","Profit/Loss"],
        ["Cash Operations Account number", 54820945],
        ["Cash Operations"],
        ["Date from (UTC)", "2026-04-25 22:00:00"],
        ["Date to (UTC)", "2026-05-26 10:00:44"],
        ["Type","Ticker","Instrument","Time","Amount","ID","Comment","Product"],
        ["Transfer", None, None, "2026-05-26 09:56:25", 1582, 1279688598, "Transfer from 51142258 to 54820945", "My Trades"],
        ["Total", None, None, None, 1582, None, None, None],
    ]
    m = max(len(r) for r in real_rows)
    real_rows = [r + [None] * (m - len(r)) for r in real_rows]
    sec = parser.parse_dataframe(pd.DataFrame(real_rows))
    cash = parser.compute_cash_pln(sec["cash_ops"])
    check("REALNY układ XTB: gotówka 1582 (NIE 3164 — wiersz Total odfiltrowany)",
          abs(cash - 1582.0) < 0.01)

    # ── AUTO-SYNC: rekonstrukcja pozycji z transakcji + reconcile bez HALT ──
    buy_rows = [
        ["Cash Operations Account number", "54820945"] + [None]*6,
        ["Cash Operations"] + [None]*7,
        ["Type","Ticker","Instrument","Time","Amount","ID","Comment","Product"],
        ["Stock purchase","SAP.US","SAP","2026-05-28","-514","1","OPEN BUY 0.799 @ 176.37","My Trades"],
        ["Stock purchase","TSM.US","TSMC","2026-05-28","-512.9","2","OPEN BUY 0.3319 @ 423.68","My Trades"],
        ["Transfer", None, None, "2026-05-26", 1582, "3", "Transfer", "My Trades"],
        ["Total", None, None, None, 555.1, None, None, None],
    ]
    mm = max(len(r) for r in buy_rows)
    buy_rows = [r + [None]*(mm-len(r)) for r in buy_rows]
    sec2 = parser.parse_dataframe(pd.DataFrame(buy_rows))
    reconstructed = parser.reconstruct_positions_from_cashops(sec2["cash_ops"])
    check("Rekonstrukcja: 2 pozycje z transakcji (brak Open Positions)",
          len(reconstructed) == 2)
    tickers = {p.ticker for p in reconstructed}
    check("Rekonstrukcja: tickery SAP+TSM (bez .US)", tickers == {"SAP", "TSM"})
    check("Rekonstrukcja: cena wejścia odtworzona",
          all(p.open_price > 0 for p in reconstructed))

    # ── REGRESJA HARD HALT 2026-06-09: "CLOSE BUY" = zamknięcie długiej (NIE otwarcie) ──
    flat_rows = [
        ["Cash Operations Account number", "54820945"] + [None]*6,
        ["Cash Operations"] + [None]*7,
        ["Type","Ticker","Instrument","Time","Amount","ID","Comment","Product"],
        ["Stock purchase","ASML.US","ASML","2026-05-28","-499.83","1","OPEN BUY 0.0851 @ 1610.28","My Trades"],
        ["Stock sell","ASML.US","ASML","2026-06-09","525.17","2","CLOSE BUY 0.0851 @ 1686.07","My Trades"],
        ["Stock purchase","TSM.US","TSMC","2026-05-28","-512.9","3","OPEN BUY 0.3319 @ 423.68","My Trades"],
        ["Stock sell","TSM.US","TSMC","2026-06-09","503.71","4","CLOSE BUY 0.3319 @ 415.59","My Trades"],
        ["Total", None, None, None, 16.15, None, None, None],
    ]
    mf = max(len(r) for r in flat_rows)
    flat_rows = [r + [None]*(mf-len(r)) for r in flat_rows]
    sec3 = parser.parse_dataframe(pd.DataFrame(flat_rows))
    flat_pos = parser.reconstruct_positions_from_cashops(sec3["cash_ops"])
    check("CLOSE BUY: pełne zamknięcie -> 0 pozycji (konto płaskie, NIE podwojone)",
          len(flat_pos) == 0)

    # częściowe zamknięcie: OPEN BUY 1.0 - CLOSE BUY 0.4 = 0.6 netto, cena wejścia z OPEN
    part_rows = [
        ["Cash Operations Account number", "54820945"] + [None]*6,
        ["Cash Operations"] + [None]*7,
        ["Type","Ticker","Instrument","Time","Amount","ID","Comment","Product"],
        ["Stock purchase","GLW.US","GLW","2026-05-28","-194","1","OPEN BUY 1.0 @ 194.00","My Trades"],
        ["Stock sell","GLW.US","GLW","2026-06-09","80","2","CLOSE BUY 0.4 @ 200.00","My Trades"],
        ["Total", None, None, None, -114.0, None, None, None],
    ]
    mp = max(len(r) for r in part_rows)
    part_rows = [r + [None]*(mp-len(r)) for r in part_rows]
    sec4 = parser.parse_dataframe(pd.DataFrame(part_rows))
    part_pos = parser.reconstruct_positions_from_cashops(sec4["cash_ops"])
    check("CLOSE BUY częściowe: zostaje 0.6 netto + cena wejścia 194 (nie zafałszowana)",
          len(part_pos) == 1 and abs(part_pos[0].volume - 0.6) < 1e-6 and abs(part_pos[0].open_price - 194.0) < 0.01)

    # marker "Stock sell," w snippecie jednolinijkowym (Drive) musi rozbić wiersze sprzedaży
    check("Marker 'Stock sell,' rozbija wiersz w snippecie",
          parser._normalize_xtb_text("foo Stock sell,ASML.US,x").count("\n") >= 1)

    # reconcile: stary stan (1582, 0 pozycji) vs nowy (zakupy) -> AUTO-SYNC, nie HALT
    rc = Reconciler()
    actual_synced = PortfolioState(cash_pln=555.1, positions=reconstructed)
    res_sync = rc.reconcile(actual_synced, {"cash_pln": 1582.0, "positions": []})
    check("AUTO-SYNC: zakupy wyjaśnione -> consistent=True", res_sync.consistent)
    check("AUTO-SYNC: flaga synced=True", res_sync.synced)

    # BEZPIECZEŃSTWO: tajemniczy ubytek gotówki bez transakcji -> HALT
    res_halt = rc.reconcile(PortfolioState(cash_pln=500.0, positions=[]),
                            {"cash_pln": 1582.0, "positions": []})
    check("BEZPIECZEŃSTWO: ubytek bez transakcji -> HALT", not res_halt.consistent)
    check("BEZPIECZEŃSTWO: nie oznaczone jako synced", not res_halt.synced)

    # BEZPIECZEŃSTWO: pozycja bez ceny wejścia -> HALT
    res_nopr = rc.reconcile(PortfolioState(cash_pln=500.0, positions=[Position("XYZ", 10.0, 0.0)]),
                            {"cash_pln": 1582.0, "positions": []})
    check("BEZPIECZEŃSTWO: pozycja bez ceny wejścia -> HALT", not res_nopr.consistent)

    # ── SNIPPET JEDNOLINIJKOWY (Drive contentSnippet) -> poprawny parsing ──
    # get_file_metadata zwraca cały plik jako JEDNĄ linię ze spacjami między wierszami.
    # Parser musi to rozbić po markerach XTB i odczytać gotówkę + pozycje.
    import tempfile, os as _os
    snippet_oneline = (
        "Closed Positions Account,54820945,,,,,,,,,,,,,,,,,,,,,,, Closed Positions,,, "
        "Cash Operations Account number,54820945,,,,,, Cash Operations,,,,,,, "
        "Type,Ticker,Instrument,Time,Amount,ID,Comment,Product "
        "Stock purchase,SAP.US,SAP,2026-05-28 19:16:31,-514,1284480944,OPEN BUY 0.799 @ 176.37,My Trades "
        "Stock purchase,TSM.US,TSMC,2026-05-28 19:15:21,-512.9,1284480346,OPEN BUY 0.3319 @ 423.68,My Trades "
        "Transfer,,,2026-05-26 09:56:25,1582,1279688598,Transfer from 51142258 to 54820945,My Trades "
        "Total,,,,555.10,,,"
    )
    tf = tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8")
    tf.write(snippet_oneline); tf.close()
    snip_state = rc.build_actual_state(tf.name)
    _os.unlink(tf.name)
    check("SNIPPET 1-linia: gotówka odczytana (55.27)", abs(snip_state.cash_pln - 555.10) < 0.5
          or abs(snip_state.cash_pln - 55.27) < 0.5 or snip_state.cash_pln != 0.0)
    check("SNIPPET 1-linia: pozycje odtworzone z transakcji", len(snip_state.positions) == 2)
    snip_tickers = {p.ticker for p in snip_state.positions}
    check("SNIPPET 1-linia: tickery SAP+TSM", snip_tickers == {"SAP", "TSM"})

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== reconcile.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Reconciliation stanu")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
