# -*- coding: utf-8 -*-
"""
positions_table.py — PEŁNA tabela pozycji do maila BILANSU (21:00).

Łączy:
  - RDZEŃ (managed_positions z portfolio.json) — władza bota rynkowego,
  - ŁOWCA (otwarte pozycje z lowca_decisions_log.json przez lowca_positions) — read-only, z założeniami (stop/cel).

Dla każdej pozycji liczy: wartość teraz, wpłacone na start, cena kupna, cena teraz,
stop loss, take profit (cel — jeśli jest), % zysk, kwota zysk. Na końcu SUMA.

Ceny "teraz" pobiera fail-safe (yfinance); w testach można wstrzyknąć własne (price_fetch).
Nigdy nie wywala biegu — brak ceny -> wycena po cenie wejścia (P&L = 0).
"""
from __future__ import annotations
import json

try:
    import lowca_positions as lpos
except Exception:
    lpos = None


def _load_core(portfolio_json: str = "portfolio.json") -> list:
    """Pozycje rdzenia z portfolio.json (managed_positions) -> znormalizowane rekordy."""
    try:
        with open(portfolio_json, encoding="utf-8") as f:
            pf = json.load(f)
    except Exception:
        return []
    out = []
    for p in (pf.get("managed_positions") or []):
        try:
            out.append({
                "owner": "rdzeń",
                "ticker": str(p.get("ticker", "?")).upper(),
                "shares": float(p.get("shares", 0) or 0),
                "entry_usd": float(p.get("entry_price_usd", 0) or 0),
                "stop_usd": float(p.get("stop_loss_usd", 0) or 0),
                "target_usd": None,                      # rdzeń = strategia trailing, brak sztywnego TP
            })
        except Exception:
            continue
    return out


def _load_lowca(log_path: str = "lowca_decisions_log.json") -> list:
    """Otwarte pozycje łowcy (status OPEN) -> znormalizowane rekordy (z celem = take profit)."""
    if lpos is None:
        return []
    out = []
    for r in lpos.load_open(log_path):
        try:
            out.append({
                "owner": "łowca",
                "ticker": str(r.get("ticker", "?")).upper(),
                "shares": float(r.get("shares") or 0),
                "entry_usd": float(r.get("entry_usd") or 0),
                "stop_usd": float(r.get("stop_usd") or 0),
                "target_usd": (float(r["target_usd"]) if r.get("target_usd") else None),
            })
        except Exception:
            continue
    return out


def fetch_prices(tickers: list) -> dict:
    """Aktualne ceny (USD) fail-safe przez yfinance (jeden batch). Błąd/brak -> pomija ticker.
    O 21:00 CEST rynek USA jest otwarty -> bierzemy ostatni słupek intraday (5m);
    fallback: ostatni dzienny close."""
    tickers = [t for t in dict.fromkeys(tickers) if t]
    if not tickers:
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}

    def _last(tks, period, interval):
        res = {}
        try:
            data = yf.download(tks, period=period, interval=interval,
                               auto_adjust=True, progress=False, threads=False)
            if data is None or len(data) == 0:
                return res
            close = data["Close"]
            for tk in tks:
                try:
                    s = close[tk] if hasattr(close, "columns") else close
                    s = s.dropna()
                    if len(s):
                        res[tk] = float(s.iloc[-1])
                except Exception:
                    continue
        except Exception:
            pass
        return res

    out = _last(tickers, "1d", "5m")                     # intraday (rynek otwarty o 21:00)
    missing = [t for t in tickers if t not in out]
    if missing:
        out.update(_last(missing, "5d", "1d"))           # fallback: ostatni dzienny close
    return out


def build_detailed_positions(portfolio_json: str = "portfolio.json", fx: float = 4.0,
                             log_path: str = "lowca_decisions_log.json", price_fetch=None) -> dict:
    """Zwraca {'rows': [...], 'total_now','total_init','total_pnl','fx'}.
    price_fetch: funkcja tickers->{ticker:price} (do testów). Domyślnie fetch_prices (yfinance)."""
    rows_in = _load_core(portfolio_json) + _load_lowca(log_path)
    fx = float(fx or 4.0)
    fetch = price_fetch or fetch_prices
    try:
        prices = fetch([r["ticker"] for r in rows_in]) or {}
    except Exception:
        prices = {}
    rows = []
    tot_now = tot_init = tot_pnl = 0.0
    for r in rows_in:
        entry = r["entry_usd"]; sh = r["shares"]
        cur = float(prices.get(r["ticker"]) or 0) or entry      # brak ceny -> cena wejścia (P&L 0)
        init_pln = sh * entry * fx
        now_pln = sh * cur * fx
        pnl_pln = now_pln - init_pln
        pnl_pct = ((cur - entry) / entry * 100) if entry > 0 else 0.0
        rows.append({
            "owner": r["owner"], "ticker": r["ticker"], "shares": sh,
            "entry_usd": entry, "current_usd": cur, "has_price": bool(prices.get(r["ticker"])),
            "stop_usd": r["stop_usd"], "target_usd": r["target_usd"],
            "init_pln": round(init_pln, 0), "now_pln": round(now_pln, 0),
            "pnl_pln": round(pnl_pln, 0), "pnl_pct": round(pnl_pct, 1),
        })
        tot_now += now_pln; tot_init += init_pln; tot_pnl += pnl_pln
    rows.sort(key=lambda x: x["now_pln"], reverse=True)
    return {"rows": rows, "total_now": round(tot_now, 0), "total_init": round(tot_init, 0),
            "total_pnl": round(tot_pnl, 0), "fx": fx}


def _run_selftest() -> int:
    import os
    print("=== SELFTEST positions_table ===")
    P = F = 0

    def ok(n, c):
        nonlocal P, F
        if c: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")

    pf = "portfolio_PTTEST.json"
    log = "lowca_decisions_log_PTTEST.json"
    try:
        with open(pf, "w", encoding="utf-8") as f:
            json.dump({"cash_pln": 55.0, "managed_positions": [
                {"ticker": "SAP", "shares": 1.0, "entry_price_usd": 100.0, "stop_loss_usd": 90.0},
            ]}, f)
        with open(log, "w", encoding="utf-8") as f:
            json.dump([
                {"id": "2026-06-04-AAA", "ticker": "AAA", "status": "OPEN", "shares": 2.0,
                 "entry_usd": 10.0, "stop_usd": 8.0, "target_usd": 15.0},
                {"id": "2026-06-01-OLD", "ticker": "OLD", "status": "WIN", "shares": 1.0, "entry_usd": 5.0},
            ], f)
        # wstrzyknięte ceny: SAP +20%, AAA +50%
        prices = {"SAP": 120.0, "AAA": 15.0}
        res = build_detailed_positions(pf, fx=4.0, log_path=log, price_fetch=lambda tks: prices)
        ok("2 wiersze (rdzeń SAP + łowca AAA, bez WIN)", len(res["rows"]) == 2)
        sap = next(r for r in res["rows"] if r["ticker"] == "SAP")
        aaa = next(r for r in res["rows"] if r["ticker"] == "AAA")
        ok("SAP owner=rdzeń, brak TP (trailing)", sap["owner"] == "rdzeń" and sap["target_usd"] is None)
        ok("AAA owner=łowca, TP=15", aaa["owner"] == "łowca" and aaa["target_usd"] == 15.0)
        ok("SAP +20%: init 400, now 480, pnl 80", sap["init_pln"] == 400 and sap["now_pln"] == 480 and sap["pnl_pln"] == 80)
        ok("AAA +50%: init 80, now 120, pnl 40", aaa["init_pln"] == 80 and aaa["now_pln"] == 120 and aaa["pnl_pln"] == 40)
        ok("SUMA: init 480, now 600, pnl +120", res["total_init"] == 480 and res["total_now"] == 600 and res["total_pnl"] == 120)
        # brak ceny -> wycena po wejściu, P&L 0
        res2 = build_detailed_positions(pf, fx=4.0, log_path=log, price_fetch=lambda tks: {})
        ok("Brak cen -> P&L 0 (wycena po wejściu)", all(r["pnl_pln"] == 0 for r in res2["rows"]))
        ok("Brak pliku portfela -> brak crasha", isinstance(build_detailed_positions("nie_ma.json", 4.0, "nie_ma.json", lambda t: {}), dict))
    finally:
        for p in (pf, log):
            if os.path.exists(p):
                os.remove(p)
    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== positions_table.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_run_selftest() if "--selftest" in sys.argv else 0)
