#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper_engine.py — WIRTUALNA księga 100 000 zł (faza nauki / forward paper-trade).

Oba boty zarządzają jedną wirtualną księgą — BEZ realnego ryzyka, NIEZALEŻNIE od prawdziwego
rachunku XTB (ten zostaje w trybie overlay/bezpiecznym). To pełny forward-test CAŁEGO systemu
(rdzeń + rękaw) z prawdziwym zarządzaniem pozycjami: sizing, gotówka, stopy, cele, trailing, time-stop.

ŹRÓDŁA (reużycie istniejącego kodu, zero duplikacji strategii):
  • RĘKAW  : lowca.decide_all(@capital=100k, selection_enabled=True)  -> picks spekulacyjne (<=15%)
  • RDZEŃ  : feed paper_core_orders.json (bilans dopisuje swoje momentum-picks)   -> rdzeń (~85%)
  • CENY   : scoreboard.fetch_prices_yf / fetch_spy_yf (guarded; offline -> brak ruchu, nie crash)
  • WYJŚCIA: reguły position_manager (stop / cel +20% partial / trailing -12% od szczytu / time-stop 15d)

STAN: paper_portfolio.json   LOGI: paper_equity_log.json, paper_trades_log.json
Nigdy nie wywala biegu (try/except wokół I/O). Idempotentny po dacie (jeden krok = jeden dzień).
"""
from __future__ import annotations
import argparse
import json
import os
from dataclasses import dataclass, field

BOOK_PATH = "paper_portfolio.json"
EQUITY_PATH = "paper_equity_log.json"
TRADES_PATH = "paper_trades_log.json"
CORE_FEED_PATH = "paper_core_orders.json"
START_CAPITAL = 100_000.0

# Reguły wyjścia (lustro position_manager) ----------------------------------------------------
STOP_TRAIL_DD = 0.12       # trailing: stop nie niżej niż szczyt*(1-0.12)
TAKE_PROFIT = 0.20         # +20% -> realizuj część (25%) i podnieś stop do break-even
TP_PARTIAL_FRAC = 0.25
TIME_STOP_DAYS = 15
TIME_STOP_MIN_PROFIT = 0.03
# Barbell / sizing na 100k
CORE_TARGET_PCT = 0.85     # rdzeń dąży do ~85% equity zainwestowane
SLEEVE_PCT = 0.15          # rękaw <= 15%
CORE_MAX_PER_POS = 0.15    # pojedynczy rdzeń <= 15% equity (anti-koncentracja)
CORE_MAX_POSITIONS = 8


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _num(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def new_book(capital=START_CAPITAL, start_date="") -> dict:
    return {"cash_pln": float(capital), "start_capital_pln": float(capital),
            "start_date": start_date, "positions": [], "updated": start_date}


def load_book(path=None, capital=START_CAPITAL, start_date="") -> dict:
    path = path or BOOK_PATH
    b = _load(path, None)
    if not isinstance(b, dict) or "cash_pln" not in b:
        return new_book(capital, start_date)
    b.setdefault("positions", [])
    b.setdefault("start_capital_pln", capital)
    return b


# ── Wycena / equity ─────────────────────────────────────────────────────────────────────────
def position_value_pln(p: dict, price_usd, fx: float) -> float:
    px = _num(price_usd) if price_usd is not None else _num(p.get("entry_usd"))
    return _num(p.get("shares")) * px * fx


def mark_equity(book: dict, prices: dict, fx: float) -> dict:
    prices = {str(k).upper(): v for k, v in (prices or {}).items()}
    invested = 0.0
    for p in book.get("positions", []):
        invested += position_value_pln(p, prices.get(str(p.get("ticker")).upper()), fx)
    equity = book["cash_pln"] + invested
    start = book.get("start_capital_pln", START_CAPITAL) or START_CAPITAL
    return {"equity_pln": round(equity, 2), "cash_pln": round(book["cash_pln"], 2),
            "invested_pln": round(invested, 2), "n_positions": len(book.get("positions", [])),
            "ret_since_start_pct": round((equity / start - 1.0) * 100.0, 2)}


# ── WYJŚCIA (lustro position_manager) ─────────────────────────────────────────────────────────
def evaluate_exit(p: dict, price_usd, today_days_held: int):
    """Zwraca (action, fraction, reason, new_stop). action: HOLD / SELL_ALL / SELL_PARTIAL."""
    px = _num(price_usd)
    entry = _num(p.get("entry_usd"))
    if px <= 0 or entry <= 0:
        return "HOLD", 0.0, "brak ceny — HOLD", _num(p.get("stop_usd"))
    peak = max(_num(p.get("peak_usd", entry)), px)
    ret = px / entry - 1.0
    stop = _num(p.get("stop_usd"))
    # ratchet: stop nie niżej niż szczyt*(1-trail)
    trail_stop = peak * (1.0 - STOP_TRAIL_DD)
    eff_stop = max(stop, trail_stop) if peak > entry else stop
    if px <= eff_stop:
        why = "stop trafiony" if px <= stop else f"trailing -{STOP_TRAIL_DD*100:.0f}% od szczytu"
        return "SELL_ALL", 1.0, f"{why} (zwrot {ret*100:+.1f}%)", eff_stop
    if today_days_held >= TIME_STOP_DAYS and ret < TIME_STOP_MIN_PROFIT:
        return "SELL_ALL", 1.0, f"time-stop {TIME_STOP_DAYS}d, zysk <{TIME_STOP_MIN_PROFIT*100:.0f}% ({ret*100:+.1f}%)", eff_stop
    if ret >= TAKE_PROFIT and not p.get("tp_taken"):
        be = max(eff_stop, entry)  # stop do break-even po realizacji części
        return "SELL_PARTIAL", TP_PARTIAL_FRAC, f"take-profit +{ret*100:.0f}% — realizuj {TP_PARTIAL_FRAC*100:.0f}%, stop->BE", be
    return "HOLD", 0.0, f"trzymam ({ret*100:+.1f}%)", eff_stop


def apply_exits(book: dict, prices: dict, fx: float, today: str, day_index: dict, trades: list):
    prices = {str(k).upper(): v for k, v in (prices or {}).items()}
    keep = []
    for p in book.get("positions", []):
        tk = str(p.get("ticker")).upper()
        px = prices.get(tk)
        days = day_index.get(tk, _num(p.get("days_held")))
        if px is not None:
            p["peak_usd"] = max(_num(p.get("peak_usd", p.get("entry_usd"))), _num(px))
        p["days_held"] = days
        action, frac, reason, new_stop = evaluate_exit(p, px, int(days))
        p["stop_usd"] = round(_num(new_stop), 4)
        if action == "HOLD":
            keep.append(p); continue
        sell_shares = _num(p.get("shares")) * frac
        proceeds = sell_shares * _num(px) * fx
        cost = sell_shares * _num(p.get("entry_usd")) * fx
        book["cash_pln"] += proceeds
        trades.append({"date": today, "ticker": tk, "tag": p.get("tag"), "side": "SELL",
                       "shares": round(sell_shares, 4), "price_usd": round(_num(px), 2),
                       "value_pln": round(proceeds, 2), "pnl_pln": round(proceeds - cost, 2),
                       "reason": reason})
        if action == "SELL_PARTIAL":
            p["shares"] = round(_num(p.get("shares")) - sell_shares, 6)
            p["tp_taken"] = True
            keep.append(p)
        # SELL_ALL -> pozycja znika (nie dodajemy do keep)
    book["positions"] = keep
    return book


# ── WEJŚCIA (sizing barbell + fill) ───────────────────────────────────────────────────────────
def _held_tickers(book: dict) -> set:
    return {str(p.get("ticker")).upper() for p in book.get("positions", [])}


def size_and_buy(book: dict, orders: list, equity: float, fx: float, today: str, prices: dict, trades: list):
    """orders: [{ticker, tag('core'/'sleeve'), entry_usd, stop_usd, target_usd, score, kind}].
    Sizing: rdzeń dąży do CORE_TARGET_PCT equity (<=CORE_MAX_PER_POS/pozycja, max N), rękaw <=SLEEVE_PCT.
    Fill po cenie bieżącej (prices) lub entry_usd. ANTI-DOUBLE-BUY: pomija już trzymane."""
    prices = {str(k).upper(): v for k, v in (prices or {}).items()}
    held = _held_tickers(book)
    core_pos = [p for p in book["positions"] if p.get("tag") == "core"]
    sleeve_invested = sum(position_value_pln(p, prices.get(str(p.get("ticker")).upper()), fx)
                          for p in book["positions"] if p.get("tag") == "sleeve")
    core_invested = sum(position_value_pln(p, prices.get(str(p.get("ticker")).upper()), fx)
                        for p in core_pos)
    core_budget_left = max(0.0, equity * CORE_TARGET_PCT - core_invested)
    sleeve_budget_left = max(0.0, equity * SLEEVE_PCT - sleeve_invested)
    core_per = equity * CORE_MAX_PER_POS

    # rdzeń: najlepsze wg score; rękaw: kolejność z decide_all (już posortowane)
    core_orders = sorted([o for o in orders if o.get("tag") == "core"],
                         key=lambda o: _num(o.get("score")), reverse=True)
    sleeve_orders = [o for o in orders if o.get("tag") == "sleeve"]

    def _fill(o, size_pln):
        tk = str(o.get("ticker")).upper()
        px = _num(prices.get(tk)) or _num(o.get("entry_usd"))
        if px <= 0 or size_pln < 1:
            return 0.0
        size_pln = min(size_pln, book["cash_pln"])
        if size_pln < 1:
            return 0.0
        shares = (size_pln / fx) / px
        book["cash_pln"] -= size_pln
        book["positions"].append({
            "ticker": tk, "tag": o.get("tag"), "shares": round(shares, 6),
            "entry_usd": round(px, 4), "entry_date": today, "peak_usd": round(px, 4),
            "stop_usd": round(_num(o.get("stop_usd")), 4), "target_usd": round(_num(o.get("target_usd")), 4),
            "days_held": 0, "kind": o.get("kind", ""), "score": _num(o.get("score"))})
        trades.append({"date": today, "ticker": tk, "tag": o.get("tag"), "side": "BUY",
                       "shares": round(shares, 4), "price_usd": round(px, 2),
                       "value_pln": round(size_pln, 2), "pnl_pln": 0.0,
                       "reason": f"wejście ({o.get('tag')}, score {_num(o.get('score')):.1f})"})
        held.add(tk)
        return size_pln

    # RDZEŃ
    n_core = len(core_pos)
    for o in core_orders:
        if n_core >= CORE_MAX_POSITIONS or core_budget_left < 1:
            break
        if str(o.get("ticker")).upper() in held:
            continue
        size = min(core_per, core_budget_left)
        spent = _fill(o, size)
        if spent > 0:
            core_budget_left -= spent; n_core += 1
    # RĘKAW (lowca już dał size; tu tylko klatka sleeve_budget + gotówka)
    for o in sleeve_orders:
        if sleeve_budget_left < 1:
            break
        if str(o.get("ticker")).upper() in held:
            continue
        size = min(_num(o.get("size_pln")) or (equity * SLEEVE_PCT / 2), sleeve_budget_left)
        spent = _fill(o, size)
        if spent > 0:
            sleeve_budget_left -= spent
    return book


# ── Generowanie zleceń z OBU botów ────────────────────────────────────────────────────────────
def generate_sleeve_orders(capital: float, fx: float, signals_path=None) -> list:
    """Prawdziwy lowca.decide_all w trybie SELEKCJI (paper) @ capital — pełna logika rękawa."""
    try:
        import lowca_pipeline as L, opportunity_lens as oppl
        try:
            import lowca_sources as lsrc
        except Exception:
            lsrc = None
        signals = oppl.read_opportunity_signals(signals_path) if signals_path else oppl.read_opportunity_signals()
        radar = oppl.build_radar(signals)
        smart = lsrc.build_smart(signals) if lsrc else {"all": []}
        cands = L.build_candidates(radar, smart, L._price_map(signals), L._meta_map(signals))
        # SKALOWANIE: limity max/min_position w LowcaConfig są tuned pod ~1582 zł — przeskaluj
        # proporcjonalnie do kapitału paper, inaczej cap 80 zł dławi sizing na 100k (pozycje po 68 zł).
        factor = max(1.0, capital / 1582.0)
        c = L.LowcaConfig(capital_pln=capital, fx_usd_pln=fx, selection_enabled=True,  # paper = selekcja ON
                          max_position_pln=80.0 * factor, min_position_pln=43.0 * factor,
                          max_open_spec=4)  # większa księga -> więcej dywersyfikacji rękawa
        decs = L.decide_all(cands, c, free_cash_pln=capital * SLEEVE_PCT, fx=fx)
        out = []
        for d in decs:
            if getattr(d, "verdict", "") in ("BUY", "ROTACJA"):
                out.append({"ticker": d.ticker, "tag": "sleeve", "entry_usd": getattr(d, "price_usd", 0),
                            "stop_usd": getattr(d, "stop_price_usd", 0), "target_usd": getattr(d, "target_price_usd", 0),
                            "size_pln": getattr(d, "size_pln", 0), "score": getattr(d, "score", 0),
                            "kind": getattr(d, "kind", "")})
        return out
    except Exception as e:
        print(f"[paper] generowanie rękawa pominięte: {e}")
        return []


def generate_core_orders() -> list:
    """Rdzeń: bilans dopisuje swoje momentum-picks do paper_core_orders.json
    [{ticker, entry_usd, stop_usd, target_usd, score}]. Brak pliku -> brak rdzenia w tym kroku."""
    feed = _load(CORE_FEED_PATH, [])
    out = []
    for o in (feed if isinstance(feed, list) else []):
        if isinstance(o, dict) and o.get("ticker"):
            out.append({"ticker": o["ticker"], "tag": "core", "entry_usd": _num(o.get("entry_usd")),
                        "stop_usd": _num(o.get("stop_usd")), "target_usd": _num(o.get("target_usd")),
                        "score": _num(o.get("score", 5.0)), "kind": "CORE"})
    return out


# ── KROK DNIA ─────────────────────────────────────────────────────────────────────────────────
def step(today: str, fx=4.0, signals_path=None, prices=None, orders=None) -> dict:
    """Jeden dzień: wyjścia -> wejścia -> wycena -> log. Idempotentny po dacie (pomija powtórkę)."""
    book = load_book(start_date=today)
    eqlog = _load(EQUITY_PATH, [])
    if isinstance(eqlog, list) and any(e.get("date") == today for e in eqlog):
        print(f"[paper] {today} już zaksięgowany — pomijam (idempotentnie).")
        return mark_equity(book, prices or {}, fx)
    trades = _load(TRADES_PATH, [])
    if not isinstance(trades, list):
        trades = []

    # ceny: dla trzymanych + kandydatów
    if orders is None:
        orders = generate_sleeve_orders(book.get("start_capital_pln", START_CAPITAL), fx, signals_path) + generate_core_orders()
    tickers = sorted({str(p.get("ticker")).upper() for p in book["positions"]} |
                     {str(o.get("ticker")).upper() for o in (orders or [])})
    if prices is None:
        try:
            import scoreboard as sb
            prices = sb.fetch_prices_yf(tickers) or {}
        except Exception as e:
            print(f"[paper] ceny niedostępne ({e}) — krok bez ruchu cen.")
            prices = {}

    day_index = {str(p.get("ticker")).upper(): _num(p.get("days_held")) + 1 for p in book["positions"]}
    new_trades = []
    apply_exits(book, prices, fx, today, day_index, new_trades)
    eqmid = mark_equity(book, prices, fx)
    size_and_buy(book, orders or [], eqmid["equity_pln"], fx, today, prices, new_trades)
    snap = mark_equity(book, prices, fx)
    snap["date"] = today

    book["updated"] = today
    eqlog.append(snap)
    trades.extend(new_trades)
    _save(BOOK_PATH, book)
    _save(EQUITY_PATH, eqlog)
    _save(TRADES_PATH, trades)
    n_buy = sum(1 for t in new_trades if t["side"] == "BUY")
    n_sell = sum(1 for t in new_trades if t["side"] == "SELL")
    print(f"[paper] {today}: equity {snap['equity_pln']:,.0f} zł ({snap['ret_since_start_pct']:+.1f}% od startu) | "
          f"cash {snap['cash_pln']:,.0f} | pozycji {snap['n_positions']} | dziś +{n_buy} kup / -{n_sell} sprzedaż")
    return snap


def status() -> dict:
    book = load_book()
    eq = _load(EQUITY_PATH, [])
    last = eq[-1] if isinstance(eq, list) and eq else None
    print(f"=== WIRTUALNA KSIĘGA (paper) ===")
    print(f"  start: {book.get('start_capital_pln', START_CAPITAL):,.0f} zł ({book.get('start_date','?')}) | gotówka: {book['cash_pln']:,.0f} zł")
    print(f"  pozycji: {len(book.get('positions', []))}")
    for p in book.get("positions", []):
        print(f"    {p.get('tag','?'):6} {str(p.get('ticker')):6} {p.get('shares')} @ ${p.get('entry_usd')} stop ${p.get('stop_usd')} dni {p.get('days_held')}")
    if last:
        print(f"  ostatnie equity ({last.get('date')}): {last.get('equity_pln'):,.0f} zł ({last.get('ret_since_start_pct'):+.1f}% od startu)")
    return book


# ── SELFTEST (bez sieci — ceny syntetyczne) ───────────────────────────────────────────────────
def _run_selftest() -> int:
    import tempfile
    _cwd = os.getcwd()
    os.chdir(tempfile.mkdtemp())   # izolacja: relatywne ścieżki paper_*.json lądują w temp cwd
    P = [0]; F = [0]
    def ok(name, cond):
        (P if cond else F)[0] += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")

    fx = 4.0
    # Dzień 1: kup 2 (1 core, 1 sleeve) po $100, stop $90, cel $130
    orders = [{"ticker": "COREX", "tag": "core", "entry_usd": 100, "stop_usd": 90, "target_usd": 130, "score": 8},
              {"ticker": "SPECY", "tag": "sleeve", "entry_usd": 100, "stop_usd": 85, "target_usd": 160, "size_pln": 7000, "score": 7}]
    s1 = step("2026-07-01", fx=fx, prices={"COREX": 100, "SPECY": 100}, orders=orders)
    book = load_book()
    ok("Start 100k: equity ~100k po wejściach (mark-to-market = koszt)", abs(s1["equity_pln"] - 100000) < 1.0)
    ok("Kupiono 2 pozycje (core+sleeve)", len(book["positions"]) == 2)
    ok("Gotówka spadła (zainwestowano)", book["cash_pln"] < 100000)
    core = [p for p in book["positions"] if p["tag"] == "core"][0]
    ok("Rdzeń sizing ~15% equity (~15k)", 12000 < (core["shares"] * 100 * fx) < 16000)

    # idempotencja
    step("2026-07-01", fx=fx, prices={"COREX": 100, "SPECY": 100}, orders=orders)
    ok("Idempotencja: ten sam dzień nie dubluje", len(load_book()["positions"]) == 2)

    # Dzień 2: COREX +35% -> take-profit partial; SPECY -16% -> stop trafiony (SELL_ALL)
    step("2026-07-02", fx=fx, prices={"COREX": 135, "SPECY": 84}, orders=[])
    book = load_book()
    tickers = {p["ticker"] for p in book["positions"]}
    ok("SPECY wyleciał na stopie (cena<=stop $85)", "SPECY" not in tickers)
    ok("COREX został (take-profit partial, nie cały)", "COREX" in tickers)
    corex = [p for p in book["positions"] if p["ticker"] == "COREX"][0]
    ok("COREX: część zrealizowana (tp_taken)", corex.get("tp_taken") is True)
    trades = _load(TRADES_PATH, [])
    ok("Trade log: są SELL z pnl", any(t["side"] == "SELL" and "pnl_pln" in t for t in trades))

    # Dzień 3: COREX spada do trailing-stop (szczyt 135 -> -12% = 118.8) -> SELL_ALL
    step("2026-07-03", fx=fx, prices={"COREX": 118}, orders=[])
    ok("COREX wyleciał na trailing -12% od szczytu", "COREX" not in {p["ticker"] for p in load_book()["positions"]})

    # equity log rośnie po 3 dniach (3 wpisy)
    ok("Equity log ma 3 dni", len(_load(EQUITY_PATH, [])) == 3)

    # INVARIANT: gotówka nigdy nie spada poniżej 0
    ok("Gotówka >= 0 (nie ma debetu)", load_book()["cash_pln"] >= -0.01)

    os.chdir(_cwd)
    print(f"\n=== WYNIK: {P[0]} OK, {F[0]} FAIL ===")
    if F[0] == 0:
        print("=== paper_engine.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F[0] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Wirtualna księga 100k — paper trading (faza nauki)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--init", action="store_true", help="utwórz świeżą księgę 100k")
    ap.add_argument("--step", action="store_true", help="jeden dzień (wyjścia->wejścia->log)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--today", default="")
    ap.add_argument("--fx", type=float, default=4.0)
    ap.add_argument("--capital", type=float, default=START_CAPITAL)
    ap.add_argument("--signals", default=None)
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    if args.init:
        _save(BOOK_PATH, new_book(args.capital, args.today))
        print(f"Utworzono wirtualną księgę: {args.capital:,.0f} zł ({args.today}).")
        return 0
    if args.step:
        step(args.today or "today", fx=args.fx, signals_path=args.signals)
        return 0
    status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
