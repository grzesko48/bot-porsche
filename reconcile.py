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
    # runtime-only (NIE zapisywane do portfolio.json): czy eksport był kompletny i spójny
    export_trustworthy: bool = True
    integrity_note: str = ""

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
        return PortfolioState(
            cash_pln=float(d.get("cash_pln", 0.0)),
            positions=[Position(p["ticker"], float(p["volume"]),
                                float(p.get("open_price", 0.0)), p.get("currency", "USD"))
                       for p in d.get("positions", [])],
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


# ─────────────────────────────────────────────────────────────────────────────
# Parser eksportu XTB (.xlsx z wieloma sekcjami w jednym arkuszu)
# ─────────────────────────────────────────────────────────────────────────────
class XTBExportParser:
    """Parsuje eksport XTB. Wykrywa sekcje po nagłówkach. Liczy gotówkę z Cash Operations,
    pozycje z Open Positions (jeśli sekcja istnieje)."""

    # typy operacji gotówkowych i ich znak we wpływie na saldo (jeśli Amount nie ma znaku)
    # XTB zwykle podaje Amount ze znakiem, więc domyślnie sumujemy Amount wprost.

    def parse_file(self, path: str | Path) -> dict:
        """Czyta .xlsx, zwraca {'closed': df, 'cash_ops': df, 'open': df}.
        Próbuje openpyxl → calamine → czytelny komunikat błędu.
        xStation generuje luźny xlsx który openpyxl czasem odrzuca."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"brak pliku eksportu: {path}")
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
                f"Nie da się odczytać xlsx żadnym z engine'ów. Ostatni błąd: {last_err}. "
                f"Workaround: otwórz plik w Google Sheets i pobierz jako .xlsx."
            )
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
        # KLUCZOWE: zapamiętaj, które sekcje (markery) w ogóle wystąpiły w pliku.
        # Pozwala odróżnić "sekcja Open Positions istniała i była pusta" (= naprawdę 0 pozycji)
        # od "sekcji Open Positions w ogóle nie było" (= eksport niekompletny, NIE wolno zakładać 0).
        sections["_markers_present"] = set(starts.keys())
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

    def purchased_tickers(self, cash_ops: Optional[pd.DataFrame]) -> set:
        """Zwraca zbiór tickerów, dla których w Cash Operations widać ZAKUP akcji
        (Type ~ 'Stocks Purchase'/'Buy' i ujemny Amount). Służy do kontroli spójności:
        jeśli był zakup, to MUSI być odpowiadająca pozycja w Open Positions (albo zamknięcie)."""
        if cash_ops is None or len(cash_ops) == 0:
            return set()
        type_col = self._find(cash_ops, "type")
        tkr_col = self._find(cash_ops, "ticker", "symbol")
        amt_col = self._find(cash_ops, "amount")
        if not (type_col and tkr_col):
            return set()
        bought = set()
        for _, row in cash_ops.iterrows():
            t = str(row.get(type_col, "")).strip().lower()
            tk = str(row.get(tkr_col, "")).strip()
            if not tk or tk.lower() in ("nan", "none"):
                continue
            amt = pd.to_numeric(row.get(amt_col), errors="coerce") if amt_col else None
            is_buy = ("purchase" in t) or ("buy" in t) or ("stock" in t and amt is not None and amt < 0)
            if is_buy:
                bought.add(tk.split(".")[0].upper())   # 'SAP.US' -> 'SAP'
        return bought

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
        Dokłada KONTROLĘ INTEGRALNOŚCI: jeśli w Cash Operations są zakupy akcji,
        a brak sekcji Open Positions (albo brak w niej kupionych tickerów),
        eksport jest NIEKOMPLETNY i stan oznaczany jest jako niewiarygodny.
        Dzięki temu auto-sync NIGDY nie wyzeruje pozycji na podstawie dziurawego pliku."""
        sections = self.parser.parse_file(export_path)
        cash = self.parser.compute_cash_pln(sections.get("cash_ops"))
        positions = self.parser.parse_open_positions(sections.get("open"))
        st = PortfolioState(cash_pln=cash, positions=positions,
                            timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        markers = sections.get("_markers_present", set())
        bought = self.parser.purchased_tickers(sections.get("cash_ops"))
        held = {p.ticker.split(".")[0].upper() for p in positions}
        missing = bought - held   # kupione, ale niewidoczne wśród pozycji otwartych

        if "open" not in markers and bought:
            st.export_trustworthy = False
            st.integrity_note = (
                f"EKSPORT NIEKOMPLETNY: brak sekcji 'Open Positions', a w Cash Operations "
                f"są zakupy {sorted(bought)}. Nie mogę odróżnić '0 pozycji' od 'sekcji nie dołączono'. "
                f"Wygeneruj eksport z zaznaczoną sekcją Open Positions.")
        elif missing:
            st.export_trustworthy = False
            st.integrity_note = (
                f"EKSPORT NIESPÓJNY: kupiono {sorted(bought)}, ale wśród pozycji otwartych brak "
                f"{sorted(missing)}. Albo eksport jest częściowy, albo te pozycje zamknięto poza botem. "
                f"Sprawdź ręcznie zanim bot przyjmie ten stan.")
        return st

    def reconcile(self, actual: PortfolioState, expected_json: Optional[dict]) -> ReconcileResult:
        """Porównuje stan faktyczny (z XTB) z oczekiwanym (portfolio.json).
        Zwraca ReconcileResult. NIE woła sys.exit — to robi caller, by dało się testować."""
        if expected_json is None:
            # pierwszy run — brak historii, akceptujemy faktyczny stan jako bazę
            return ReconcileResult(True, "pierwszy run — brak portfolio.json, przyjmuję stan z XTB",
                                   actual_state=actual, actual_cash_pln=actual.cash_pln)
        expected = PortfolioState.from_json(expected_json)
        delta = abs(actual.cash_pln - expected.cash_pln)
        if delta > self.tolerance:
            return ReconcileResult(
                False,
                f"ROZJAZD GOTÓWKI: oczekiwano {expected.cash_pln:.2f} PLN, "
                f"w XTB {actual.cash_pln:.2f} PLN (delta {delta:.2f} > {self.tolerance:.2f}) — HARD HALT",
                actual_state=actual, expected_cash_pln=expected.cash_pln,
                actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2))
        # gotówka OK — porównaj pozycje (zbiór tickerów + wolumeny)
        exp_pos = {p.ticker: p.volume for p in expected.positions}
        act_pos = {p.ticker: p.volume for p in actual.positions}
        if set(exp_pos.keys()) != set(act_pos.keys()):
            return ReconcileResult(
                False,
                f"ROZJAZD POZYCJI: oczekiwano {sorted(exp_pos.keys())}, "
                f"w XTB {sorted(act_pos.keys())} — HARD HALT",
                actual_state=actual, expected_cash_pln=expected.cash_pln,
                actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2))
        return ReconcileResult(True, f"stan spójny (delta gotówki {delta:.2f} PLN ≤ {self.tolerance})",
                               actual_state=actual, expected_cash_pln=expected.cash_pln,
                               actual_cash_pln=actual.cash_pln, delta_pln=round(delta, 2))

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

    def sync_or_halt(self, export_path: str | Path, portfolio_json_path: str | Path,
                     default_stop_pct: float = 0.08, today: Optional[str] = None,
                     write: bool = True) -> dict:
        """AUTO-SYNC: czyni eksport XTB źródłem prawdy i zapisuje portfolio.json z niego.
        — Eksport KOMPLETNY i SPÓJNY  -> aktualizuje gotówkę + pozycje, zwraca ok=True.
        — Eksport NIEKOMPLETNY/USZKODZONY -> NIC nie zapisuje, zwraca halt=True (alert).

        Zachowuje metadane już zarządzanych pozycji (ratchetowany stop_loss, HWM, entry) —
        NIE resetuje ich. Nowe pozycje dostają placeholder-stop (default_stop_pct niżej),
        który PositionManager przeliczy/podniesie na żywo. Sprzedane (znikłe z eksportu)
        pozycje są usuwane. Bez dotykania konta IKE."""
        today = today or datetime.now(timezone.utc).date().isoformat()
        actual = self.build_actual_state(export_path)

        # 1) BRAMKA INTEGRALNOŚCI — nigdy nie syncuj z dziurawego pliku
        if not actual.export_trustworthy:
            return {"ok": False, "halt": True, "reason": actual.integrity_note,
                    "cash_pln": round(actual.cash_pln, 2), "wrote": False}

        # 2) Wczytaj istniejący portfolio.json (zachowamy stopy/HWM zarządzanych pozycji)
        pj = Path(portfolio_json_path)
        existing = {}
        existing_managed = {}
        if pj.exists():
            try:
                existing = json.loads(pj.read_text(encoding="utf-8"))
                for m in existing.get("managed_positions", []):
                    existing_managed[str(m["ticker"]).split(".")[0].upper()] = m
            except Exception as e:
                return {"ok": False, "halt": True,
                        "reason": f"portfolio.json nieczytelny ({e}) — nie nadpisuję", "wrote": False}

        # 3) Zbuduj nowe managed_positions z pozycji z eksportu
        new_managed, seeded, kept, dropped = [], [], [], []
        for p in actual.positions:
            base = p.ticker.split(".")[0].upper()
            if base in existing_managed:                       # ZACHOWAJ ratchet
                m = dict(existing_managed[base])
                m["shares"] = round(p.volume, 6)               # aktualizuj wolumen (możliwa częściowa sprzedaż)
                new_managed.append(m); kept.append(base)
            else:                                              # NOWA pozycja -> seed
                entry = round(p.open_price, 4)
                if entry <= 0:
                    return {"ok": False, "halt": True,
                            "reason": f"Pozycja {base} bez ceny otwarcia w eksporcie — nie syncuję (brak bazy do stopu).",
                            "wrote": False}
                new_managed.append({
                    "ticker": base, "shares": round(p.volume, 6),
                    "entry_price_usd": entry, "entry_date": today,
                    "stop_loss_usd": round(entry * (1 - default_stop_pct), 4),
                    "high_water_mark_usd": entry, "days_held": 0,
                })
                seeded.append(base)
        dropped = [t for t in existing_managed if t not in {p.ticker.split(".")[0].upper() for p in actual.positions}]

        # 4) Złóż nowy stan
        out = {
            "cash_pln": round(actual.cash_pln, 2),
            "positions": [{"ticker": p.ticker, "volume": round(p.volume, 6),
                           "open_price": round(p.open_price, 4), "currency": p.currency}
                          for p in actual.positions],
            "managed_positions": new_managed,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "xtb_export_autosync",
        }
        if write:
            tmp = pj.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(pj)   # atomowy zapis

        return {"ok": True, "halt": False,
                "reason": f"sync OK: gotówka {out['cash_pln']:.2f} PLN, pozycje {[p.ticker for p in actual.positions]}",
                "cash_pln": out["cash_pln"], "seeded": seeded, "kept": kept, "dropped": dropped,
                "wrote": bool(write), "state": out}


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

    # ── NOWE: kontrola integralności + auto-sync ──────────────────────────────
    import tempfile, os
    tmpd = tempfile.mkdtemp(prefix="porsche_sync_")

    def _write_xlsx(raw_df, name):
        path = os.path.join(tmpd, name)
        raw_df.to_excel(path, header=False, index=False, engine="openpyxl")
        return path

    # 10. EKSPORT NIEKOMPLETNY (repro bug 29.05): zakupy w Cash Ops, BRAK sekcji Open Positions
    raw_incomplete = _make_synthetic_export(cash_rows=[
        ["Transfer", None, None, "2026-05-26 09:56", 1582.0, "1", "in"],
        ["Stocks Purchase", "SAP",  "SAP.US",  "2026-05-28 16:00", -514.00, "10", "buy"],
        ["Stocks Purchase", "ASML", "ASML.US", "2026-05-28 16:00", -499.83, "11", "buy"],
        ["Stocks Purchase", "TSM",  "TSM.US",  "2026-05-28 16:00", -512.90, "12", "buy"],
    ], open_rows=None)   # <- sekcji Open Positions NIE dołączono
    p_inc = _write_xlsx(raw_incomplete, "incomplete.xlsx")
    st_inc = rec.build_actual_state(p_inc)
    check("Niekompletny eksport (zakupy bez Open Positions) -> untrustworthy",
          st_inc.export_trustworthy is False and "NIEKOMPLETNY" in st_inc.integrity_note)

    # 11. sync_or_halt na niekompletnym -> HALT, NIE nadpisuje portfolio.json
    pj_path = os.path.join(tmpd, "portfolio.json")
    r = rec.sync_or_halt(p_inc, pj_path)
    check("Auto-sync niekompletny -> HALT bez zapisu",
          r["halt"] is True and r["wrote"] is False and not os.path.exists(pj_path))

    # 12. EKSPORT KOMPLETNY: zakupy + odpowiadające Open Positions -> trustworthy
    raw_ok = _make_synthetic_export(cash_rows=[
        ["Transfer", None, None, "2026-05-26 09:56", 1582.0, "1", "in"],
        ["Stocks Purchase", "SAP",  "SAP.US",  "2026-05-28 16:00", -514.00, "10", "buy"],
        ["Stocks Purchase", "ASML", "ASML.US", "2026-05-28 16:00", -499.83, "11", "buy"],
        ["Stocks Purchase", "TSM",  "TSM.US",  "2026-05-28 16:00", -512.90, "12", "buy"],
    ], open_rows=[
        ["SAP",  0.7990, 176.37,  "SAP.US",  "BUY", "2026-05-28", "P1"],
        ["ASML", 0.0851, 1610.28, "ASML.US", "BUY", "2026-05-28", "P2"],
        ["TSM",  0.3319, 423.68,  "TSM.US",  "BUY", "2026-05-28", "P3"],
    ])
    p_ok = _write_xlsx(raw_ok, "complete.xlsx")
    st_ok = rec.build_actual_state(p_ok)
    check("Kompletny eksport -> trustworthy + 3 pozycje",
          st_ok.export_trustworthy is True and len(st_ok.positions) == 3)

    # 13. sync_or_halt na kompletnym -> zapis portfolio.json, seed 3 pozycji
    r = rec.sync_or_halt(p_ok, pj_path)
    saved = json.loads(Path(pj_path).read_text(encoding="utf-8")) if os.path.exists(pj_path) else {}
    check("Auto-sync kompletny -> zapisany, 3 managed_positions, gotówka 55.27",
          r["ok"] is True and len(saved.get("managed_positions", [])) == 3
          and abs(saved.get("cash_pln", 0) - 55.27) < 0.01 and sorted(r["seeded"]) == ["ASML","SAP","TSM"])

    # 14. RATCHET zachowany: istniejący stop NIE jest resetowany przy ponownym sync
    saved["managed_positions"] = [m for m in saved["managed_positions"]]
    for m in saved["managed_positions"]:
        if m["ticker"] == "TSM":
            m["stop_loss_usd"] = 415.00   # udajemy podniesiony stop
            m["high_water_mark_usd"] = 440.00
    Path(pj_path).write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    r2 = rec.sync_or_halt(p_ok, pj_path)
    saved2 = json.loads(Path(pj_path).read_text(encoding="utf-8"))
    tsm = next(m for m in saved2["managed_positions"] if m["ticker"] == "TSM")
    check("Re-sync zachowuje ratchetowany stop TSM (415, nie reset)",
          abs(tsm["stop_loss_usd"] - 415.00) < 1e-6 and abs(tsm["high_water_mark_usd"] - 440.0) < 1e-6
          and "TSM" in r2["kept"])

    # 15. SPRZEDAŻ: ticker znika z eksportu -> usunięty z managed_positions
    raw_sold = _make_synthetic_export(cash_rows=[
        ["Transfer", None, None, "2026-05-26 09:56", 1582.0, "1", "in"],
        ["Stocks Purchase", "SAP", "SAP.US", "2026-05-28 16:00", -514.00, "10", "buy"],
    ], open_rows=[["SAP", 0.7990, 176.37, "SAP.US", "BUY", "2026-05-28", "P1"]])
    p_sold = _write_xlsx(raw_sold, "after_sell.xlsx")
    r3 = rec.sync_or_halt(p_sold, pj_path)
    saved3 = json.loads(Path(pj_path).read_text(encoding="utf-8"))
    tickers3 = {m["ticker"] for m in saved3["managed_positions"]}
    check("Sprzedaż TSM/ASML -> zostaje tylko SAP, reszta dropped",
          tickers3 == {"SAP"} and set(r3["dropped"]) == {"ASML", "TSM"})

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
