# -*- coding: utf-8 -*-
"""
lowca_positions.py — WSPÓLNY odczyt OTWARTYCH pozycji łowcy z lowca_decisions_log.json.

Łowca "POSIADA" wpisy ze statusem OPEN (to jego pozycje spekulacyjne, sleeve ≤15%).
Czytają to dwa boty:
  - Łowca (14:30): pokazuje TYLKO swoje pozycje (władza nad nimi).
  - Bilans (21:00): pokazuje je w pełnej tabeli read-only (z założeniami: stop, cel).

Czysto odczytowy, bez sieci, fail-safe (każdy błąd -> pusta lista).
"""
from __future__ import annotations
import json


def load_open(path: str = "lowca_decisions_log.json") -> list:
    """Lista OTWARTYCH pozycji łowcy (status OPEN). Każdy rekord ma:
    id, date, ticker, kind, score, entry_usd, stop_usd, target_usd, shares, size_pln, risk_pln, status."""
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
        if not isinstance(log, list):
            return []
    except Exception:
        return []
    out = []
    for r in log:
        if isinstance(r, dict) and str(r.get("status", "OPEN")).upper() == "OPEN":
            out.append(r)
    return out


def open_tickers(path: str = "lowca_decisions_log.json") -> list:
    """Same tickery otwartych pozycji łowcy (UPPER)."""
    return [str(r.get("ticker", "")).upper() for r in load_open(path) if r.get("ticker")]


def _run_selftest() -> int:
    import os
    print("=== SELFTEST lowca_positions ===")
    P = F = 0

    def ok(n, c):
        nonlocal P, F
        if c: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")

    tmp = "lowca_decisions_log_POSTEST.json"
    try:
        data = [
            {"id": "2026-06-04-AAA", "date": "2026-06-04", "ticker": "AAA", "status": "OPEN",
             "entry_usd": 10.0, "stop_usd": 8.0, "target_usd": 15.0, "shares": 5.0, "size_pln": 50.0},
            {"id": "2026-06-03-BBB", "date": "2026-06-03", "ticker": "BBB", "status": "WIN",
             "entry_usd": 20.0, "stop_usd": 16.0},
            {"id": "2026-06-02-CCC", "date": "2026-06-02", "ticker": "ccc", "status": "OPEN", "entry_usd": 5.0},
        ]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        opn = load_open(tmp)
        ok("Tylko OPEN (2 z 3, bez WIN)", len(opn) == 2)
        ok("Tickery OPEN = AAA, CCC (upper)", set(open_tickers(tmp)) == {"AAA", "CCC"})
        ok("Brak pliku -> []", load_open("nie_ma_takiego.json") == [])
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("to nie json")
        ok("Uszkodzony plik -> []", load_open(tmp) == [])
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== lowca_positions.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_run_selftest() if "--selftest" in sys.argv else 0)
