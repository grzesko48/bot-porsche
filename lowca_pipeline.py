# -*- coding: utf-8 -*-
"""
lowca_pipeline.py — BOT ŁOWCA (osobny od bota bilansowego main_pipeline.py).
Nazwy plików celowo inne ("lowca_*"), żeby się nie myliły z rdzeniem.

Bieg PRZED OTWARCIEM: bierze okazje ocenione przez opportunity_lens (świeże IPO /
nietypowy wolumen / kontrakt ≥50% kapitalizacji) i NAKŁADA WARSTWĘ DECYZYJNĄ:
każda okazja -> KUP albo PASS, z gotowym zleceniem (kwota, limit, stop) i klatką
bezpieczeństwa (barbell: sleeve spekulacyjny ≤15% kapitału, min €43, max kilka naraz).

Łowca NIE rusza rdzenia (85% portfela pracuje w bilansie main_pipeline.py).
Łowca NIE składa zleceń — podaje gotowe do kliknięcia na XTB.

Reużywa opportunity_lens (build_radar) — zero duplikacji oceny.

UŻYCIE:
    python lowca_pipeline.py --selftest                 # offline test logiki
    python lowca_pipeline.py --signals opportunity_signals.json --capital 1582
    python lowca_pipeline.py --signals ... --send       # + mail (Resend)
"""
from __future__ import annotations
import argparse
import os
import sys
from dataclasses import dataclass

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import opportunity_lens as oppl


@dataclass
class LowcaConfig:
    capital_pln: float = 1582.0
    sleeve_pct: float = 0.15           # max 15% kapitału na spekulacje (barbell)
    min_position_pln: float = 43.0     # próg XTB (~€10)
    max_position_pln: float = 120.0    # cap pojedynczej spekulacji
    max_open_spec: int = 3
    buy_threshold: float = 6.0
    fx_usd_pln: float = 4.0
    stop_contract: float = 0.20
    stop_volume: float = 0.15
    stop_ipo: float = 0.25


def _clamp(x, lo, hi): return max(lo, min(hi, x))


def _score(opp: dict) -> float:
    """Score 0-10 z okazji ocenionej przez opportunity_lens (już przefiltrowanej)."""
    kind = opp.get("kind")
    if kind == "KONTRAKT":
        ratio = float(opp.get("ratio", 0.5) or 0.5)   # już ≥0.5 po filtrze lensa
        return round(6.0 + _clamp(ratio, 0, 1.0) * 3.0, 2)   # 7.5 - 9.0
    if kind == "WOLUMEN":
        base = 6.5
        if "przebił" in (opp.get("note", "")):
            base += 1.0
        return round(base, 2)
    if kind == "IPO":
        base = 6.0
        if "wolumen" in (opp.get("note", "")):
            base += 0.5
        return round(base, 2)
    return 0.0


def _stop_pct(kind: str, c: LowcaConfig) -> float:
    return {"KONTRAKT": c.stop_contract, "WOLUMEN": c.stop_volume, "IPO": c.stop_ipo}.get(kind, 0.20)


@dataclass
class LowcaDecision:
    ticker: str
    kind: str
    verdict: str = "PASS"
    score: float = 0.0
    size_pln: float = 0.0
    stop_pct: float = 0.0
    risk: str = ""
    note: str = ""
    reason: str = ""


def decide_all(radar: dict, c: LowcaConfig, sleeve_used_pln: float = 0.0,
               open_spec: int = 0) -> "list[LowcaDecision]":
    """Warstwa decyzyjna: KUP/PASS + sizing w klatce sleeve. Sortuje wg score."""
    opps = (radar or {}).get("all", []) if radar else []
    cand = []
    for o in opps:
        d = LowcaDecision(ticker=o.get("ticker", "?"), kind=o.get("kind", "?"),
                          score=_score(o), risk=o.get("risk", ""), note=o.get("note", ""))
        cand.append(d)
    cand.sort(key=lambda d: d.score, reverse=True)
    sleeve_cap = c.capital_pln * c.sleeve_pct
    used = sleeve_used_pln
    n = open_spec
    for d in cand:
        if d.score < c.buy_threshold:
            d.verdict = "PASS"; d.reason = f"score {d.score:.1f} < {c.buy_threshold:.0f}"; continue
        if n >= c.max_open_spec:
            d.verdict = "PASS"; d.reason = f"limit spekulacji ({c.max_open_spec})"; continue
        size = c.min_position_pln + (c.max_position_pln - c.min_position_pln) * _clamp((d.score - c.buy_threshold) / 3.0, 0, 1)
        size = round(size, 0)
        if used + size > sleeve_cap:
            size = round(sleeve_cap - used, 0)
        if size < c.min_position_pln:
            d.verdict = "PASS"; d.reason = f"sleeve wyczerpany ({used:.0f}/{sleeve_cap:.0f} zł)"; continue
        d.verdict = "BUY"; d.size_pln = size; d.stop_pct = _stop_pct(d.kind, c)
        d.reason = f"score {d.score:.1f} ≥ {c.buy_threshold:.0f}"
        used += size; n += 1
    return cand


def render_report(decisions, c: LowcaConfig, sleeve_used_pln: float = 0.0) -> str:
    buys = [d for d in decisions if d.verdict == "BUY"]
    passes = [d for d in decisions if d.verdict == "PASS"]
    cap = c.capital_pln * c.sleeve_pct
    used_after = sleeve_used_pln + sum(d.size_pln for d in buys)
    L = ["═" * 60, "  ŁOWCA OKAZJI — DECYZJE PRZED OTWARCIEM  ⚠ SPEKULACJA", "═" * 60,
         f"  Sleeve: {used_after:.0f} / {cap:.0f} zł"]
    if buys:
        L.append("\n  ✅ KUPUJEMY (gotowe zlecenia — klik na XTB o 15:30):")
        for i, d in enumerate(buys, 1):
            L.append(f"   {i}. {d.ticker} [{d.kind}] — {d.note}")
            L.append(f"      → KUP za {d.size_pln:.0f} zł · stop −{d.stop_pct*100:.0f}% · "
                     f"ryzyko {d.risk} · score {d.score:.1f}")
    else:
        L.append("\n  ✅ KUPUJEMY: dziś żadna okazja nie przeszła progu.")
    if passes:
        L.append("\n  ⛔ PASS:")
        for d in passes[:8]:
            L.append(f"   • {d.ticker} [{d.kind}] — {d.reason}")
    L.append(f"\n  ZASADA: sleeve ≤15%, każda pozycja ma stop, max {c.max_open_spec} naraz. "
             "Rdzeń (85%) pracuje w bilansie wieczornym. Egzekucja = Twój klik na XTB.")
    L.append("═" * 60)
    return "\n".join(L)


def run(signals_path=None, capital=1582.0, send=False, sleeve_used=0.0, open_spec=0) -> int:
    c = LowcaConfig(capital_pln=capital)
    signals = oppl.read_opportunity_signals(signals_path) if signals_path else oppl.read_opportunity_signals()
    radar = oppl.build_radar(signals)
    decisions = decide_all(radar, c, sleeve_used_pln=sleeve_used, open_spec=open_spec)
    report = render_report(decisions, c, sleeve_used_pln=sleeve_used)
    print(report)
    if send:
        try:
            from notifications import send_email_resend
            html = "<pre style='font-family:Courier New,monospace;font-size:11px'>" + report + "</pre>"
            r = send_email_resend(html, "Bot Łowca — okazje przed otwarciem", dry_run=False)
            print("Mail:", "OK" if r.get("ok") else r.get("note"))
        except Exception as e:
            print(f"Mail pominięty: {e}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST — offline, syntetyczne sygnały
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST lowca_pipeline (decyzje na okazjach) ===")
    P = F = 0
    def ok(n, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")
    c = LowcaConfig()

    # syntetyczne sygnały -> build_radar (reużywa opportunity_lens)
    signals = {
        "contract": [{"ticker": "OKLO", "contract_usd": 900e6, "market_cap_usd": 1.8e9, "hours_ago": 2, "source": "SEC 8-K"}],
        "volume": [{"ticker": "XYZ", "volume_mult": 5, "pct_today": 18, "broke_high": True}],
        "ipo": [{"ticker": "RDDT", "age_months": 8, "pct_from_ipo": 60, "volume_mult": 2}],
    }
    radar = oppl.build_radar(signals)
    ok("build_radar zwrócił okazje (reużycie lensa)", len(radar.get("all", [])) == 3)

    dec = decide_all(radar, c)
    buys = [d for d in dec if d.verdict == "BUY"]
    ok("Kontrakt OKLO -> BUY (najwyżzsy score)", any(d.ticker == "OKLO" and d.verdict == "BUY" for d in dec))
    ok("Co najmniej 1 decyzja BUY", len(buys) >= 1)
    ok("Klatka: max 3 spekulacje", len(buys) <= c.max_open_spec)
    ok("Klatka: sleeve nie przekroczony", sum(d.size_pln for d in buys) <= c.capital_pln * c.sleeve_pct + 0.5)
    ok("Każdy BUY ma stop", all(d.stop_pct > 0 for d in buys))
    ok("Każdy BUY ma kwotę ≥ próg XTB", all(d.size_pln >= c.min_position_pln for d in buys))

    # pusty radar -> zero decyzji, brak crasha
    empty = oppl.build_radar({"_empty": True})
    ok("Pusty radar -> 0 decyzji", len(decide_all(empty, c)) == 0)

    # sygnały None -> fail-safe
    ok("Sygnały None -> brak crasha", isinstance(oppl.build_radar(None), dict))

    # za mały kontrakt odsiany przez lens -> nie ma go w decyzjach
    s2 = {"contract": [{"ticker": "SMALL", "contract_usd": 50e6, "market_cap_usd": 1e9, "hours_ago": 1}]}
    ok("Kontrakt 5% kapitalizacji odsiany (lens) -> 0 okazji",
       len(oppl.build_radar(s2).get("all", [])) == 0)

    # render nie wywala
    try:
        render_report(dec, c); ok("Render raportu bez błędu", True)
    except Exception as e:
        ok(f"Render raportu bez błędu ({e})", False)

    # sleeve wyczerpany wcześniej -> mniej BUY
    dec2 = decide_all(radar, c, sleeve_used_pln=c.capital_pln * c.sleeve_pct - 10)
    ok("Sleeve prawie pełny -> ogranicza zakupy",
       sum(d.size_pln for d in dec2 if d.verdict == "BUY") <= 10 + 0.5)

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== lowca_pipeline.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Łowca — okazje przed otwarciem")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--signals", default=None, help="ścieżka do opportunity_signals.json")
    ap.add_argument("--capital", type=float, default=1582.0)
    ap.add_argument("--sleeve-used", type=float, default=0.0)
    ap.add_argument("--open-spec", type=int, default=0)
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _run_selftest()
    return run(signals_path=a.signals, capital=a.capital, send=a.send,
               sleeve_used=a.sleeve_used, open_spec=a.open_spec)


if __name__ == "__main__":
    raise SystemExit(main())
