# -*- coding: utf-8 -*-
"""
lowca_pipeline.py — BOT ŁOWCA (osobny od bota bilansowego main_pipeline.py).
Nazwy plików celowo inne ("lowca_*"), żeby się nie myliły z rdzeniem.

Bieg PRZED OTWARCIEM: bierze okazje ocenione przez opportunity_lens (świeże IPO /
nietypowy wolumen / kontrakt >=50% kapitalizacji) i NAKŁADA WARSTWĘ DECYZYJNĄ:
każda okazja -> KUP albo PASS, z gotowym zleceniem (kwota, stop) i klatką
bezpieczeństwa (barbell: sleeve spekulacyjny <=15% kapitału, min ~43 zł).

NOWE:
- Sizing wg REALNEJ WOLNEJ GOTÓWKI na rachunku (--cash) — decyzje nigdy nie
  przekraczają tego, co faktycznie masz do wydania (min(sleeve, gotówka)).
- Mail HTML w stylu bota bilansowego (import palety/komponentów z email_render).

Łowca NIE rusza rdzenia (85% portfela pracuje w bilansie main_pipeline.py).
Łowca NIE składa zleceń — podaje gotowe do kliknięcia na XTB.

Reużywa opportunity_lens (build_radar) — zero duplikacji oceny.

UŻYCIE:
    python lowca_pipeline.py --selftest
    python lowca_pipeline.py --signals opportunity_signals.json --capital 1648 --cash 55
    python lowca_pipeline.py --signals ... --cash 55 --send       # + mail (Resend)
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
    min_position_pln: float = 43.0     # próg XTB (~10 EUR)
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
        ratio = float(opp.get("ratio", 0.5) or 0.5)   # już >=0.5 po filtrze lensa
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


def deployable_budget(c: LowcaConfig, free_cash_pln=None, sleeve_used_pln: float = 0.0):
    """Ile realnie można dziś wydać = min(wolny sleeve, wolna gotówka na rachunku).
    Zwraca (budget, sleeve_cap, cash_limited?)."""
    sleeve_cap = c.capital_pln * c.sleeve_pct
    avail_sleeve = max(0.0, sleeve_cap - sleeve_used_pln)
    if free_cash_pln is None:
        return avail_sleeve, sleeve_cap, False
    budget = max(0.0, min(avail_sleeve, float(free_cash_pln)))
    cash_limited = float(free_cash_pln) < avail_sleeve
    return budget, sleeve_cap, cash_limited


def decide_all(radar: dict, c: LowcaConfig, free_cash_pln=None,
               sleeve_used_pln: float = 0.0, open_spec: int = 0) -> "list[LowcaDecision]":
    """Warstwa decyzyjna: KUP/PASS + sizing w klatce min(sleeve, wolna gotówka). Sort wg score."""
    opps = (radar or {}).get("all", []) if radar else []
    cand = []
    for o in opps:
        d = LowcaDecision(ticker=o.get("ticker", "?"), kind=o.get("kind", "?"),
                          score=_score(o), risk=o.get("risk", ""), note=o.get("note", ""))
        cand.append(d)
    cand.sort(key=lambda d: d.score, reverse=True)
    budget, sleeve_cap, cash_limited = deployable_budget(c, free_cash_pln, sleeve_used_pln)
    used = 0.0
    n = open_spec
    for d in cand:
        if d.score < c.buy_threshold:
            d.verdict = "PASS"; d.reason = f"score {d.score:.1f} < {c.buy_threshold:.0f}"; continue
        if n >= c.max_open_spec:
            d.verdict = "PASS"; d.reason = f"limit spekulacji ({c.max_open_spec})"; continue
        size = c.min_position_pln + (c.max_position_pln - c.min_position_pln) * _clamp((d.score - c.buy_threshold) / 3.0, 0, 1)
        size = round(size, 0)
        remaining = budget - used
        if size > remaining:
            size = round(remaining, 0)
        if size < c.min_position_pln:
            if free_cash_pln is not None and cash_limited:
                d.verdict = "PASS"; d.reason = f"za mało wolnej gotówki (zostało {max(0.0, remaining):.0f} zł)"
            else:
                d.verdict = "PASS"; d.reason = f"sleeve wyczerpany ({used:.0f}/{budget:.0f} zł)"
            continue
        d.verdict = "BUY"; d.size_pln = size; d.stop_pct = _stop_pct(d.kind, c)
        d.reason = f"score {d.score:.1f} >= {c.buy_threshold:.0f}"
        used += size; n += 1
    return cand


# ─────────────────────────────────────────────────────────────────────────────
# RENDER TEKSTOWY (konsola / fallback)
# ─────────────────────────────────────────────────────────────────────────────
def render_text(decisions, c: LowcaConfig, equity_pln=None, free_cash_pln=None,
                sleeve_used_pln: float = 0.0) -> str:
    buys = [d for d in decisions if d.verdict == "BUY"]
    passes = [d for d in decisions if d.verdict == "PASS"]
    budget, sleeve_cap, cash_limited = deployable_budget(c, free_cash_pln, sleeve_used_pln)
    spent = sum(d.size_pln for d in buys)
    L = ["=" * 60, "  BOT LOWCA — DECYZJE PRZED OTWARCIEM (SPEKULACJA)", "=" * 60]
    L.append(f"  Equity: {(equity_pln or c.capital_pln):.0f} zl  |  Wolna gotowka: "
             f"{('%.0f zl' % free_cash_pln) if free_cash_pln is not None else '—'}  |  "
             f"Sleeve 15%: {sleeve_cap:.0f} zl  |  Do wydania dzis: {budget:.0f} zl")
    if buys:
        L.append("\n  KUPUJEMY (klik na XTB o 15:30):")
        for i, d in enumerate(buys, 1):
            L.append(f"   {i}. {d.ticker} [{d.kind}] — KUP za {d.size_pln:.0f} zl · stop -{d.stop_pct*100:.0f}% "
                     f"· score {d.score:.1f}")
            if d.note:
                L.append(f"        {d.note}")
    else:
        L.append("\n  KUPUJEMY: dzis zadna okazja nie przeszla (prog lub brak gotowki).")
    if passes:
        L.append("\n  PASS:")
        for d in passes[:8]:
            L.append(f"   - {d.ticker} [{d.kind}] — {d.reason}")
    if cash_limited:
        L.append(f"\n  UWAGA: wolna gotowka ({free_cash_pln:.0f} zl) < sleeve ({sleeve_cap:.0f} zl) — "
                 f"to gotowka ogranicza zakupy, nie strategia.")
    L.append("=" * 60)
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# RENDER HTML — styl bota bilansowego (import palety/komponentow z email_render)
# ─────────────────────────────────────────────────────────────────────────────
def render_html(decisions, c: LowcaConfig, equity_pln=None, free_cash_pln=None,
                sleeve_used_pln: float = 0.0, today: str = "") -> str:
    try:
        from email_render import (BG, DARK, GOLD, GOLD_DK, GREEN, GREEN_LT, RED, RED_LT,
                                  GREY, MUTED, MUTED2, TEXT, LINE, LINE2, SERIF, SANS,
                                  _card_open, _action_block)
    except Exception:
        return "<pre style='font-family:Courier New,monospace;font-size:12px'>" + \
               render_text(decisions, c, equity_pln, free_cash_pln, sleeve_used_pln) + "</pre>"

    buys = [d for d in decisions if d.verdict == "BUY"]
    passes = [d for d in decisions if d.verdict == "PASS"]
    eq = equity_pln if equity_pln is not None else c.capital_pln
    budget, sleeve_cap, cash_limited = deployable_budget(c, free_cash_pln, sleeve_used_pln)
    spent = sum(d.size_pln for d in buys)
    cash_txt = (f"{free_cash_pln:,.0f} zł" if free_cash_pln is not None else "—")
    kind_label = {"KONTRAKT": "Mała spółka + duży kontrakt", "WOLUMEN": "Nietypowy wolumen / wybicie",
                  "IPO": "Świeże IPO z momentum"}

    P = [f"<div style='{SANS}background:{BG};padding:22px;'><div style='max-width:700px;margin:0 auto;'>"]

    # ── HEADER (granat + złoto, jak bilans) ──
    P.append(
        f"<div style='background:{DARK};border-radius:14px;padding:28px 30px;margin-bottom:18px;'>"
        f"<div style='{SANS}font-size:10.5pt;text-transform:uppercase;letter-spacing:2.5px;"
        f"color:{GOLD};font-weight:bold;'>Łowca okazji · przed otwarciem</div>"
        f"<div style='{SERIF}font-size:25pt;color:#ffffff;margin:10px 0 6px;font-weight:bold;'>"
        f"Polowanie na okazje — decyzje na dziś</div>"
        f"<div style='{SANS}font-size:11.5pt;color:{MUTED2};'>{today} · przed otwarciem sesji USA (15:30)</div>"
        f"<div style='border-top:1px solid #334155;margin:16px 0 12px;'></div>"
        f"<div style='{SANS}font-size:10.5pt;text-transform:uppercase;letter-spacing:1px;"
        f"color:{GOLD};font-weight:bold;'>Wolna gotówka {cash_txt} · Equity {eq:,.0f} zł · "
        f"Do wydania dziś {budget:,.0f} zł</div></div>"
    )

    # ── TWOJE PIENIĄDZE (powiązanie z realnym rachunkiem) ──
    if cash_limited:
        money_note = (f"Masz <b>{cash_txt}</b> wolnej gotówki — mniej niż budżet sleeve "
                      f"({sleeve_cap:,.0f} zł). Decyzje poniżej zmieściłem w Twojej gotówce, "
                      f"nie w teoretycznym limicie.")
        money_bar = RED
    elif free_cash_pln is not None:
        money_note = (f"Wolna gotówka <b>{cash_txt}</b> pokrywa cały budżet spekulacyjny sleeve "
                      f"({sleeve_cap:,.0f} zł). Limitem jest strategia (max 15%), nie gotówka.")
        money_bar = GREEN
    else:
        money_note = ("Brak danych o wolnej gotówce — sizing wg samego sleeve (15% equity). "
                      "Sprawdź na XTB, czy masz tyle wolnych środków.")
        money_bar = GOLD_DK

    P.append(
        _card_open("Twoje pieniądze dziś", "Tyle realnie możesz dziś wydać na spekulację.") +
        f"<div style='border-left:5px solid {money_bar};background:#f8fafc;border-radius:8px;"
        f"padding:14px 16px;{SANS}font-size:12.5pt;color:{TEXT};line-height:1.6;margin-bottom:14px;'>"
        f"{money_note}</div>"
        f"<table style='width:100%;border-collapse:collapse;{SANS}font-size:12.5pt;color:{TEXT};'>"
        f"<tr><td style='padding:7px 0;'>Equity (cały rachunek)</td>"
        f"<td style='padding:7px 0;text-align:right;font-weight:bold;color:{DARK};'>{eq:,.0f} zł</td></tr>"
        f"<tr><td style='padding:7px 0;border-top:1px solid {LINE2};'>Wolna gotówka na rachunku</td>"
        f"<td style='padding:7px 0;text-align:right;border-top:1px solid {LINE2};font-weight:bold;color:{DARK};'>{cash_txt}</td></tr>"
        f"<tr><td style='padding:7px 0;border-top:1px solid {LINE2};'>Sleeve spekulacyjny (max 15%)</td>"
        f"<td style='padding:7px 0;text-align:right;border-top:1px solid {LINE2};color:{TEXT};'>{sleeve_cap:,.0f} zł</td></tr>"
        f"<tr><td style='padding:7px 0;border-top:2px solid {LINE};'><b>Do wydania dziś (mniejsze z dwóch)</b></td>"
        f"<td style='padding:7px 0;text-align:right;border-top:2px solid {LINE};font-weight:bold;color:{GREEN};font-size:14pt;'>{budget:,.0f} zł</td></tr>"
        f"<tr><td style='padding:7px 0;'>Wykorzystane decyzjami poniżej</td>"
        f"<td style='padding:7px 0;text-align:right;font-weight:bold;color:{DARK};'>{spent:,.0f} zł</td></tr>"
        f"</table></div>"
    )

    # ── DECYZJE KUP ──
    if buys:
        P.append(_card_open("Decyzje KUP — gotowe zlecenia na XTB",
                            "Wykonaj o 15:30 w xStation. Kwota mieści się w Twojej wolnej gotówce. "
                            "Każda pozycja ma stop."))
        for d in buys:
            do_html = (f"Kup <b>{d.ticker}</b> za <b>{d.size_pln:,.0f} zł</b> po cenie rynkowej, "
                       f"potem ustaw <b>Sell Stop -{d.stop_pct*100:.0f}%</b> od ceny wejścia.")
            why_html = (f"{d.note or 'Okazja spekulacyjna.'} "
                        f"<br><span style='color:{MUTED};'>Typ: {kind_label.get(d.kind, d.kind)} · "
                        f"score {d.score:.1f}/10 · ryzyko {d.risk or 'WYSOKIE'}.</span>")
            P.append(_action_block(
                company=d.ticker, order="Zlecenie: KUP po cenie rynkowej + Sell Stop",
                order_color=GREEN, bar_color=GREEN, do_html=do_html, why_html=why_html))
        P.append("</div>")
    else:
        P.append(_card_open("Decyzje KUP — gotowe zlecenia na XTB") +
                 f"<div style='{SANS}font-size:13pt;color:{TEXT};line-height:1.6;'>"
                 f"<b>Dziś nie kupujemy.</b> Żadna okazja nie przeszła progu (score &lt; 6) "
                 f"albo zabrakło wolnej gotówki. To normalne — łowca woli czekać niż wymuszać "
                 f"słaby zakład.</div></div>")

    # ── PASS (kompaktowo) ──
    if passes:
        rows = ""
        for d in passes[:8]:
            rows += (f"<tr><td style='padding:10px 8px 10px 0;border-bottom:1px solid {LINE2};"
                     f"{SANS}font-size:12.5pt;'><b style='color:{DARK};'>{d.ticker}</b> "
                     f"<span style='color:{MUTED};font-size:11pt;'>[{d.kind}]</span></td>"
                     f"<td style='padding:10px 0;border-bottom:1px solid {LINE2};color:{MUTED};"
                     f"{SANS}font-size:11.5pt;'>{d.reason}</td></tr>")
        P.append(
            _card_open("Pominięte (PASS)", "Okazje, które dziś nie kwalifikują się do zakupu.") +
            f"<table style='width:100%;border-collapse:collapse;'>{rows}</table></div>"
        )

    # ── ZASADY / STOPKA ──
    P.append(
        _card_open("Zasady łowcy") +
        f"<div style='{SANS}font-size:11.5pt;color:{TEXT};line-height:1.7;'>"
        f"• Sleeve spekulacyjny <b>max 15%</b> equity — rdzeń (85%) pracuje niezależnie w bilansie wieczornym.<br>"
        f"• Każda pozycja ma <b>stop</b>; kwota nigdy nie przekracza <b>wolnej gotówki</b> na rachunku.<br>"
        f"• Max {c.max_open_spec} spekulacje naraz; próg wejścia ~43 zł (10 EUR na XTB).<br>"
        f"• <b>Egzekucja = Twój klik na XTB.</b> Łowca niczego nie kupuje, tylko podaje gotowe decyzje.<br>"
        f"• Konto IKE pozostaje nietykalne.</div></div>"
    )
    P.append(
        f"<div style='{SANS}font-size:10pt;color:{MUTED2};text-align:center;padding:20px 0;line-height:1.6;'>"
        f"Zautomatyzowana synteza okazji — to spekulacja wysokiego ryzyka i NIE porada inwestycyjna. "
        f"Decyzja i egzekucja na XTB to Twoja odpowiedzialność.<br>"
        f"<em>Kwoty w zł; ceny i Sell Stop wpisujesz w xStation w USD.</em></div>"
    )

    P.append("</div></div>")
    return "".join(P)


def run(signals_path=None, capital=1582.0, cash=None, send=False,
        sleeve_used=0.0, open_spec=0, today="") -> int:
    c = LowcaConfig(capital_pln=capital)
    signals = oppl.read_opportunity_signals(signals_path) if signals_path else oppl.read_opportunity_signals()
    radar = oppl.build_radar(signals)
    decisions = decide_all(radar, c, free_cash_pln=cash, sleeve_used_pln=sleeve_used, open_spec=open_spec)
    print(render_text(decisions, c, equity_pln=capital, free_cash_pln=cash, sleeve_used_pln=sleeve_used))
    if send:
        try:
            from notifications import send_email_resend
            html = render_html(decisions, c, equity_pln=capital, free_cash_pln=cash,
                               sleeve_used_pln=sleeve_used, today=today)
            r = send_email_resend(html, "Bot Łowca — okazje przed otwarciem", dry_run=False)
            print("Mail:", "OK" if r.get("ok") else r.get("note"), "id=", r.get("id"))
        except Exception as e:
            print(f"Mail pominięty: {e}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST — offline, syntetyczne sygnały
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST lowca_pipeline (decyzje + gotowka + HTML) ===")
    P = F = 0
    def ok(n, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")
    c = LowcaConfig(capital_pln=1648.0)

    signals = {
        "contract": [{"ticker": "OKLO", "contract_usd": 900e6, "market_cap_usd": 1.8e9, "hours_ago": 2, "source": "SEC 8-K"}],
        "volume": [{"ticker": "XYZ", "volume_mult": 5, "pct_today": 18, "broke_high": True}],
        "ipo": [{"ticker": "RDDT", "age_months": 8, "pct_from_ipo": 60, "volume_mult": 2}],
    }
    radar = oppl.build_radar(signals)
    ok("build_radar zwrócił okazje (reużycie lensa)", len(radar.get("all", [])) == 3)

    # bez limitu gotówki -> sleeve decyduje
    dec = decide_all(radar, c)
    buys = [d for d in dec if d.verdict == "BUY"]
    ok("Bez limitu gotówki: >=1 BUY", len(buys) >= 1)
    ok("Każdy BUY ma stop", all(d.stop_pct > 0 for d in buys))
    ok("Każdy BUY >= próg XTB", all(d.size_pln >= c.min_position_pln for d in buys))
    ok("Sleeve nie przekroczony", sum(d.size_pln for d in buys) <= c.capital_pln * c.sleeve_pct + 0.5)

    # MAŁA wolna gotówka (55 zł) -> suma zakupów nie przekracza 55
    dec_cash = decide_all(radar, c, free_cash_pln=55.0)
    buys_cash = [d for d in dec_cash if d.verdict == "BUY"]
    ok("Gotówka 55 zł: suma zakupów <= 55", sum(d.size_pln for d in buys_cash) <= 55 + 0.5)
    ok("Gotówka 55 zł: co najwyżej 1 pozycja", len(buys_cash) <= 1)
    ok("Gotówka 55 zł: reszta PASS z powodu gotówki",
       any(d.verdict == "PASS" and "gotówk" in d.reason for d in dec_cash))

    # gotówka 0 -> zero zakupów
    dec_zero = decide_all(radar, c, free_cash_pln=0.0)
    ok("Gotówka 0 zł -> 0 zakupów", sum(1 for d in dec_zero if d.verdict == "BUY") == 0)

    # gotówka większa niż sleeve -> sleeve znów wiąże
    dec_big = decide_all(radar, c, free_cash_pln=10000.0)
    ok("Gotówka 10k: sleeve znów ogranicza",
       sum(d.size_pln for d in dec_big if d.verdict == "BUY") <= c.capital_pln * c.sleeve_pct + 0.5)

    # budżet: min(sleeve, gotówka)
    b1, cap1, lim1 = deployable_budget(c, 55.0)
    ok("Budżet = min(sleeve, gotówka) gdy gotówka mała", abs(b1 - 55.0) < 0.01 and lim1)
    b2, cap2, lim2 = deployable_budget(c, 10000.0)
    ok("Budżet = sleeve gdy gotówka duża", abs(b2 - cap2) < 0.01 and not lim2)

    # pusty radar -> 0 decyzji
    empty = oppl.build_radar({"_empty": True})
    ok("Pusty radar -> 0 decyzji", len(decide_all(empty, c, free_cash_pln=55.0)) == 0)

    # sygnały None -> fail-safe
    ok("Sygnały None -> brak crasha", isinstance(oppl.build_radar(None), dict))

    # render_text nie wywala
    try:
        render_text(dec_cash, c, equity_pln=1648, free_cash_pln=55.0); ok("render_text bez błędu", True)
    except Exception as e:
        ok(f"render_text bez błędu ({e})", False)

    # render_html nie wywala i zawiera realną gotówkę
    try:
        html = render_html(dec_cash, c, equity_pln=1648, free_cash_pln=55.0, today="2026-06-03")
        ok("render_html bez błędu", True)
        ok("HTML pokazuje wolną gotówkę", "55 zł" in html or "55&nbsp;zł" in html or "Wolna gotówka" in html)
        ok("HTML ma sekcję pieniędzy", "Twoje pieniądze" in html)
        ok("HTML ma stopkę/zasady", "IKE" in html)
    except Exception as e:
        ok(f"render_html bez błędu ({e})", False)

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== lowca_pipeline.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Łowca — okazje przed otwarciem")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--signals", default=None, help="ścieżka do opportunity_signals.json")
    ap.add_argument("--capital", type=float, default=1582.0, help="equity (cały rachunek) w zł")
    ap.add_argument("--cash", type=float, default=None, help="wolna gotówka na rachunku w zł")
    ap.add_argument("--sleeve-used", type=float, default=0.0)
    ap.add_argument("--open-spec", type=int, default=0)
    ap.add_argument("--today", default="", help="data do nagłówka maila (YYYY-MM-DD)")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _run_selftest()
    return run(signals_path=a.signals, capital=a.capital, cash=a.cash, send=a.send,
               sleeve_used=a.sleeve_used, open_spec=a.open_spec, today=a.today)


if __name__ == "__main__":
    raise SystemExit(main())
