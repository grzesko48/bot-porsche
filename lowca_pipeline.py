# -*- coding: utf-8 -*-
"""
lowca_pipeline.py — BOT ŁOWCA (osobny od bota bilansowego main_pipeline.py).

Bieg PRZED OTWARCIEM: bierze okazje ocenione przez opportunity_lens (świeże IPO /
nietypowy wolumen / kontrakt >=50% kapitalizacji) i NAKŁADA WARSTWĘ DECYZYJNĄ:
każda okazja -> KUP albo PASS, z KONKRETNYM zleceniem (ile akcji, cena wejścia,
Sell Stop w USD, cel) i klatką bezpieczeństwa (barbell: sleeve <=15%, min ~43 zł).

CO ROBI (v3):
- SAM czyta z repo: equity + kurs USD/PLN (equity_log.json), wolną gotówkę i
  OBECNE POZYCJE (portfolio.json -> cash_pln, managed_positions). Nic nie modyfikuje.
- Decyzje sizing'owane wg min(sleeve, wolna gotówka). NIE poleca spółki, którą już masz.
- Liczy KONKRETY: liczba akcji, wartość zł, cena wejścia (USD), Sell Stop (USD),
  cel take-profit (USD), ryzyko w zł (ile tracisz jeśli stop).
- Mail HTML w stylu bota bilansowego (import palety/komponentów z email_render),
  z sekcją "Twoje obecne pozycje" (oba boty widzą ten sam stan z repo).

Łowca NIE rusza rdzenia (85% w bilansie). Łowca NIE składa zleceń — podaje gotowe do XTB.

UŻYCIE:
    python lowca_pipeline.py --selftest
    python lowca_pipeline.py --signals opportunity_signals.json --today 2026-06-03 --send
    (equity/gotówka/kurs/pozycje czytane z repo; można nadpisać --capital --cash --fx)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from dataclasses import dataclass, field

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
    tp_contract: float = 0.40          # cel take-profit per typ (orientacyjny)
    tp_volume: float = 0.30
    tp_ipo: float = 0.60


def _clamp(x, lo, hi): return max(lo, min(hi, x))


# ─────────────────────────────────────────────────────────────────────────────
# ODCZYT STANU KONTA Z REPO (oba boty widzą ten sam stan; łowca tylko CZYTA)
# ─────────────────────────────────────────────────────────────────────────────
def read_account(portfolio_path="portfolio.json", equity_path="equity_log.json") -> dict:
    """Czyta equity + kurs (equity_log.json) oraz gotówkę + pozycje (portfolio.json).
    Nic nie zapisuje. Brak pliku / błąd -> bezpieczne fallbacki."""
    acc = {"equity_pln": None, "cash_pln": None, "fx_usd_pln": None, "held": []}
    try:
        with open(equity_path, encoding="utf-8") as f:
            log = json.load(f)
        if isinstance(log, list) and log:
            last = log[-1]
            acc["equity_pln"] = float(last.get("equity_pln")) if last.get("equity_pln") is not None else None
            if last.get("fx_rate"):
                acc["fx_usd_pln"] = float(last["fx_rate"])
    except Exception:
        pass
    try:
        with open(portfolio_path, encoding="utf-8") as f:
            pf = json.load(f)
        if pf.get("cash_pln") is not None:
            acc["cash_pln"] = float(pf["cash_pln"])
        for p in (pf.get("managed_positions") or []):
            acc["held"].append({
                "ticker": p.get("ticker", "?"),
                "shares": float(p.get("shares", 0) or 0),
                "entry_usd": float(p.get("entry_price_usd", 0) or 0),
                "stop_usd": float(p.get("stop_loss_usd", 0) or 0),
            })
    except Exception:
        pass
    return acc


def _price_map(signals: dict) -> dict:
    """{ticker: price_usd} z surowych sygnałów (price_usd opcjonalny)."""
    out = {}
    if not isinstance(signals, dict):
        return out
    for key in ("contract", "volume", "ipo", "theme", "lockup"):
        for s in (signals.get(key) or []):
            if isinstance(s, dict) and s.get("ticker") and s.get("price_usd"):
                try:
                    out[str(s["ticker"]).upper()] = float(s["price_usd"])
                except Exception:
                    pass
    return out


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


def _tp_pct(kind: str, c: LowcaConfig) -> float:
    return {"KONTRAKT": c.tp_contract, "WOLUMEN": c.tp_volume, "IPO": c.tp_ipo}.get(kind, 0.40)


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
    # konkrety (gdy znana cena)
    price_usd: float = 0.0
    stop_price_usd: float = 0.0
    target_price_usd: float = 0.0
    shares: float = 0.0
    risk_pln: float = 0.0


def _shares(size_pln, price_usd, fx):
    if not price_usd or price_usd <= 0 or not fx or fx <= 0:
        return 0.0
    return round((size_pln / fx) / price_usd, 4)


def decide_all(radar: dict, c: LowcaConfig, free_cash_pln=None, price_map=None,
               held=None, fx=None, sleeve_used_pln: float = 0.0,
               open_spec: int = 0) -> "list[LowcaDecision]":
    """Decyzje KUP/PASS + sizing w klatce min(sleeve, gotówka), z KONKRETAMI (akcje/ceny).
    Pomija spółki już posiadane (held)."""
    price_map = price_map or {}
    held_set = {str(t).upper() for t in (held or [])}
    fx = fx or c.fx_usd_pln
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
        if d.ticker.upper() in held_set:
            d.verdict = "PASS"; d.reason = "już masz tę spółkę w portfelu (bilans)"; continue
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
        # KONKRETY (gdy znana cena)
        price = price_map.get(d.ticker.upper(), 0.0)
        if price and price > 0:
            d.price_usd = round(price, 2)
            d.stop_price_usd = round(price * (1 - d.stop_pct), 2)
            d.target_price_usd = round(price * (1 + _tp_pct(d.kind, c)), 2)
            d.shares = _shares(size, price, fx)
        d.risk_pln = round(size * d.stop_pct, 0)
        used += size; n += 1
    return cand


def deployable_budget(c: LowcaConfig, free_cash_pln=None, sleeve_used_pln: float = 0.0):
    """min(wolny sleeve, wolna gotówka). Zwraca (budget, sleeve_cap, cash_limited?)."""
    sleeve_cap = c.capital_pln * c.sleeve_pct
    avail_sleeve = max(0.0, sleeve_cap - sleeve_used_pln)
    if free_cash_pln is None:
        return avail_sleeve, sleeve_cap, False
    budget = max(0.0, min(avail_sleeve, float(free_cash_pln)))
    cash_limited = float(free_cash_pln) < avail_sleeve
    return budget, sleeve_cap, cash_limited


# ─────────────────────────────────────────────────────────────────────────────
# RENDER TEKSTOWY (konsola / fallback)
# ─────────────────────────────────────────────────────────────────────────────
def render_text(decisions, c: LowcaConfig, equity_pln=None, free_cash_pln=None,
                fx=None, held=None, sleeve_used_pln: float = 0.0) -> str:
    buys = [d for d in decisions if d.verdict == "BUY"]
    passes = [d for d in decisions if d.verdict == "PASS"]
    budget, sleeve_cap, cash_limited = deployable_budget(c, free_cash_pln, sleeve_used_pln)
    L = ["=" * 64, "  BOT LOWCA — DECYZJE PRZED OTWARCIEM (SPEKULACJA)", "=" * 64]
    L.append(f"  Equity: {(equity_pln or c.capital_pln):.0f} zl  |  Wolna gotowka: "
             f"{('%.0f zl' % free_cash_pln) if free_cash_pln is not None else '—'}  |  "
             f"Do wydania dzis: {budget:.0f} zl  |  Kurs USD/PLN: {fx or c.fx_usd_pln:.2f}")
    if held:
        L.append("  Masz juz: " + ", ".join(f"{h['ticker']} ({h['shares']:.3f} akc.)" for h in held))
    if buys:
        L.append("\n  KUPUJEMY (klik na XTB o 15:30):")
        for i, d in enumerate(buys, 1):
            if d.price_usd:
                L.append(f"   {i}. {d.ticker} [{d.kind}] — KUP {d.shares:.4f} akc. za {d.size_pln:.0f} zl "
                         f"· wejscie ~${d.price_usd:.2f} · Sell Stop ${d.stop_price_usd:.2f} "
                         f"· cel ${d.target_price_usd:.2f} · ryzyko {d.risk_pln:.0f} zl · score {d.score:.1f}")
            else:
                L.append(f"   {i}. {d.ticker} [{d.kind}] — KUP za {d.size_pln:.0f} zl · stop -{d.stop_pct*100:.0f}% "
                         f"(brak ceny) · ryzyko {d.risk_pln:.0f} zl · score {d.score:.1f}")
            if d.note:
                L.append(f"        {d.note}")
    else:
        L.append("\n  KUPUJEMY: dzis zadna okazja nie przeszla (prog / gotowka / juz w portfelu).")
    if passes:
        L.append("\n  PASS:")
        for d in passes[:8]:
            L.append(f"   - {d.ticker} [{d.kind}] — {d.reason}")
    if cash_limited:
        L.append(f"\n  UWAGA: wolna gotowka ({free_cash_pln:.0f} zl) < sleeve ({sleeve_cap:.0f} zl) — "
                 f"to gotowka ogranicza zakupy.")
    L.append("=" * 64)
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# RENDER HTML — styl bota bilansowego (import palety/komponentow z email_render)
# ─────────────────────────────────────────────────────────────────────────────
def render_html(decisions, c: LowcaConfig, equity_pln=None, free_cash_pln=None,
                fx=None, held=None, sleeve_used_pln: float = 0.0, today: str = "") -> str:
    try:
        from email_render import (BG, DARK, GOLD, GOLD_DK, GREEN, GREEN_LT, RED, RED_LT,
                                  GREY, MUTED, MUTED2, TEXT, LINE, LINE2, SERIF, SANS,
                                  _card_open, _action_block)
    except Exception:
        return "<pre style='font-family:Courier New,monospace;font-size:12px'>" + \
               render_text(decisions, c, equity_pln, free_cash_pln, fx, held, sleeve_used_pln) + "</pre>"

    buys = [d for d in decisions if d.verdict == "BUY"]
    passes = [d for d in decisions if d.verdict == "PASS"]
    eq = equity_pln if equity_pln is not None else c.capital_pln
    fxv = fx or c.fx_usd_pln
    budget, sleeve_cap, cash_limited = deployable_budget(c, free_cash_pln, sleeve_used_pln)
    spent = sum(d.size_pln for d in buys)
    risk_total = sum(d.risk_pln for d in buys)
    cash_txt = (f"{free_cash_pln:,.0f} zł" if free_cash_pln is not None else "—")
    kind_label = {"KONTRAKT": "Mała spółka + duży kontrakt", "WOLUMEN": "Nietypowy wolumen / wybicie",
                  "IPO": "Świeże IPO z momentum"}

    P = [f"<div style='{SANS}background:{BG};padding:22px;'><div style='max-width:700px;margin:0 auto;'>"]

    # ── HEADER ──
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
        f"Do wydania dziś {budget:,.0f} zł · USD/PLN {fxv:.2f}</div></div>"
    )

    # ── TWOJE PIENIĄDZE ──
    if cash_limited:
        money_note = (f"Masz <b>{cash_txt}</b> wolnej gotówki — mniej niż budżet sleeve "
                      f"({sleeve_cap:,.0f} zł). Decyzje poniżej zmieściłem w Twojej gotówce.")
        money_bar = RED
    elif free_cash_pln is not None:
        money_note = (f"Wolna gotówka <b>{cash_txt}</b> pokrywa cały budżet sleeve "
                      f"({sleeve_cap:,.0f} zł). Limitem jest strategia (max 15%), nie gotówka.")
        money_bar = GREEN
    else:
        money_note = "Brak danych o wolnej gotówce — sizing wg samego sleeve (15% equity)."
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
        f"<tr><td style='padding:7px 0;'>Łączne ryzyko sleeve (jeśli wszystkie stopy)</td>"
        f"<td style='padding:7px 0;text-align:right;font-weight:bold;color:{RED};'>{risk_total:,.0f} zł</td></tr>"
        f"</table></div>"
    )

    # ── TWOJE OBECNE POZYCJE (oba boty widzą ten sam stan z repo) ──
    if held:
        rows = ""
        for h in held:
            rows += (f"<tr><td style='padding:10px 8px 10px 0;border-bottom:1px solid {LINE2};{SANS}font-size:12.5pt;'>"
                     f"<b style='color:{DARK};'>{h['ticker']}</b></td>"
                     f"<td style='padding:10px 8px;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{TEXT};'>{h['shares']:.4f} akc.</td>"
                     f"<td style='padding:10px 8px;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{MUTED};'>wejście ${h['entry_usd']:.2f}</td>"
                     f"<td style='padding:10px 0;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{TEXT};'>Stop ${h['stop_usd']:.2f}</td></tr>")
        P.append(
            _card_open("Twoje obecne pozycje (rdzeń — z bilansu)",
                       "Ten sam stan widzi bot bilansowy. Łowca tego NIE rusza i nie poleca duplikatów.") +
            f"<table style='width:100%;border-collapse:collapse;'>{rows}</table></div>"
        )

    # ── DECYZJE KUP (KONKRETY) ──
    if buys:
        P.append(_card_open("Decyzje KUP — gotowe zlecenia na XTB",
                            "Wykonaj o 15:30 w xStation. Ceny w USD wpisujesz w xStation; kwoty w zł."))
        for d in buys:
            if d.price_usd:
                do_html = (f"Kup <b>{d.shares:.4f} akcji {d.ticker}</b> (≈ <b>{d.size_pln:,.0f} zł</b>) "
                           f"po cenie rynkowej (~<b>${d.price_usd:.2f}</b>).<br>"
                           f"Ustaw <b>Sell Stop ${d.stop_price_usd:.2f}</b>. "
                           f"Cel orientacyjny <b>${d.target_price_usd:.2f}</b> (lub trailing — zysk bez capa).")
            else:
                do_html = (f"Kup <b>{d.ticker}</b> za <b>{d.size_pln:,.0f} zł</b> po cenie rynkowej. "
                           f"Ustaw Sell Stop −{d.stop_pct*100:.0f}% (brak ceny w sygnale).")
            why_html = (f"{d.note or 'Okazja spekulacyjna.'} "
                        f"<br><span style='color:{RED};font-weight:bold;'>Ryzyko jeśli stop: {d.risk_pln:,.0f} zł.</span> "
                        f"<span style='color:{MUTED};'>Typ: {kind_label.get(d.kind, d.kind)} · "
                        f"score {d.score:.1f}/10 · {d.risk or 'WYSOKIE'}.</span>")
            P.append(_action_block(
                company=d.ticker, order="Zlecenie: KUP po cenie rynkowej + Sell Stop",
                order_color=GREEN, bar_color=GREEN, do_html=do_html, why_html=why_html))
        P.append("</div>")
    else:
        P.append(_card_open("Decyzje KUP — gotowe zlecenia na XTB") +
                 f"<div style='{SANS}font-size:13pt;color:{TEXT};line-height:1.6;'>"
                 f"<b>Dziś nie kupujemy.</b> Żadna okazja nie przeszła progu (score &lt; 6), "
                 f"zabrakło gotówki albo już masz tę spółkę. Łowca woli czekać niż wymuszać.</div></div>")

    # ── PASS ──
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
        f"• Każda pozycja ma <b>konkretny Sell Stop</b>; kwota nigdy nie przekracza <b>wolnej gotówki</b>.<br>"
        f"• Max {c.max_open_spec} spekulacje naraz; próg wejścia ~43 zł (10 EUR na XTB).<br>"
        f"• <b>Egzekucja = Twój klik na XTB.</b> Łowca niczego nie kupuje — podaje gotowe liczby.<br>"
        f"• Konto IKE pozostaje nietykalne.</div></div>"
    )
    P.append(
        f"<div style='{SANS}font-size:10pt;color:{MUTED2};text-align:center;padding:20px 0;line-height:1.6;'>"
        f"Zautomatyzowana synteza okazji — spekulacja wysokiego ryzyka, NIE porada inwestycyjna. "
        f"Ceny wejścia to ostatni kurs (orientacyjnie); realnie kupujesz po cenie rynkowej.<br>"
        f"<em>Kwoty w zł; ceny i Sell Stop wpisujesz w xStation w USD.</em></div>"
    )

    P.append("</div></div>")
    return "".join(P)


def run(signals_path=None, capital=None, cash=None, fx=None, send=False,
        sleeve_used=0.0, open_spec=0, today="") -> int:
    acc = read_account()
    capital = capital if capital is not None else (acc["equity_pln"] or 1582.0)
    cash = cash if cash is not None else acc["cash_pln"]
    fx = fx if fx is not None else (acc["fx_usd_pln"] or 4.0)
    held = acc["held"]
    held_tickers = [h["ticker"] for h in held]

    c = LowcaConfig(capital_pln=capital, fx_usd_pln=fx)
    signals = oppl.read_opportunity_signals(signals_path) if signals_path else oppl.read_opportunity_signals()
    radar = oppl.build_radar(signals)
    pmap = _price_map(signals)
    decisions = decide_all(radar, c, free_cash_pln=cash, price_map=pmap, held=held_tickers,
                           fx=fx, sleeve_used_pln=sleeve_used, open_spec=open_spec)
    print(render_text(decisions, c, equity_pln=capital, free_cash_pln=cash, fx=fx, held=held,
                      sleeve_used_pln=sleeve_used))
    if send:
        try:
            from notifications import send_email_resend
            html = render_html(decisions, c, equity_pln=capital, free_cash_pln=cash, fx=fx,
                               held=held, sleeve_used_pln=sleeve_used, today=today)
            r = send_email_resend(html, "Bot Łowca — okazje przed otwarciem", dry_run=False)
            print("Mail:", "OK" if r.get("ok") else r.get("note"), "id=", r.get("id"))
        except Exception as e:
            print(f"Mail pominięty: {e}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST — offline
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST lowca_pipeline (konkrety + pozycje + gotowka + HTML) ===")
    P = F = 0
    def ok(n, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")
    c = LowcaConfig(capital_pln=1648.0, fx_usd_pln=3.64)

    signals = {
        "contract": [{"ticker": "OKLO", "contract_usd": 900e6, "market_cap_usd": 1.8e9, "hours_ago": 2, "source": "SEC 8-K", "price_usd": 22.50}],
        "volume": [{"ticker": "XYZ", "volume_mult": 5, "pct_today": 18, "broke_high": True, "price_usd": 8.0}],
        "ipo": [{"ticker": "RDDT", "age_months": 8, "pct_from_ipo": 60, "volume_mult": 2, "price_usd": 140.0}],
    }
    radar = oppl.build_radar(signals)
    pmap = _price_map(signals)
    ok("price_map z sygnalow", pmap.get("OKLO") == 22.50 and pmap.get("RDDT") == 140.0)
    ok("build_radar 3 okazje", len(radar.get("all", [])) == 3)

    # z gotowka 55 i cena -> konkrety
    dec = decide_all(radar, c, free_cash_pln=55.0, price_map=pmap, fx=3.64)
    buys = [d for d in dec if d.verdict == "BUY"]
    ok("Gotowka 55: suma <= 55", sum(d.size_pln for d in buys) <= 55 + 0.5)
    ok("BUY ma liczbe akcji > 0", all(d.shares > 0 for d in buys))
    ok("BUY ma cene wejscia", all(d.price_usd > 0 for d in buys))
    ok("BUY ma Sell Stop < wejscie", all(0 < d.stop_price_usd < d.price_usd for d in buys))
    ok("BUY ma cel > wejscie", all(d.target_price_usd > d.price_usd for d in buys))
    ok("BUY ma ryzyko w zl > 0", all(d.risk_pln > 0 for d in buys))
    # weryfikacja matematyki akcji dla pierwszego BUY
    if buys:
        b = buys[0]
        exp_shares = round((b.size_pln / 3.64) / b.price_usd, 4)
        ok("Liczba akcji = (zl/fx)/cena", abs(b.shares - exp_shares) < 1e-6)
        ok("Stop = cena*(1-stop%)", abs(b.stop_price_usd - round(b.price_usd * (1 - b.stop_pct), 2)) < 0.01)

    # held -> pomija spolke
    dec_h = decide_all(radar, c, free_cash_pln=200.0, price_map=pmap, held=["RDDT"], fx=3.64)
    ok("Held RDDT -> PASS 'juz masz'", any(d.ticker == "RDDT" and d.verdict == "PASS" and "już masz" in d.reason for d in dec_h))
    ok("Held RDDT -> nie ma RDDT w BUY", not any(d.ticker == "RDDT" and d.verdict == "BUY" for d in dec_h))

    # brak ceny -> BUY bez konkretow, ale z ryzykiem
    sig2 = {"ipo": [{"ticker": "NOPRICE", "age_months": 5, "pct_from_ipo": 40, "volume_mult": 2}]}
    r2 = oppl.build_radar(sig2)
    d2 = decide_all(r2, c, free_cash_pln=200.0, price_map=_price_map(sig2), fx=3.64)
    b2 = [d for d in d2 if d.verdict == "BUY"]
    ok("Brak ceny -> nadal BUY (size>0)", b2 and b2[0].size_pln > 0)
    ok("Brak ceny -> shares=0 (nie zmysla)", b2 and b2[0].shares == 0)
    ok("Brak ceny -> ryzyko nadal liczone", b2 and b2[0].risk_pln > 0)

    # read_account fallbacki (brak plikow w tym katalogu testowym jest OK)
    acc = read_account("brak_portfolio.json", "brak_equity.json")
    ok("read_account brak plikow -> fallback dict", isinstance(acc, dict) and acc["equity_pln"] is None)

    # budzet min(sleeve, gotowka)
    b1, cap1, lim1 = deployable_budget(c, 55.0)
    ok("Budzet = min(sleeve, gotowka)", abs(b1 - 55.0) < 0.01 and lim1)

    # pusty radar
    ok("Pusty radar -> 0 decyzji", len(decide_all(oppl.build_radar({"_empty": True}), c, free_cash_pln=55.0)) == 0)
    ok("Sygnaly None -> brak crasha", isinstance(oppl.build_radar(None), dict))

    held = [{"ticker": "SAP", "shares": 0.799, "entry_usd": 176.37, "stop_usd": 176.37}]
    # render_text
    try:
        render_text(dec, c, equity_pln=1648, free_cash_pln=55.0, fx=3.64, held=held); ok("render_text bez bledu", True)
    except Exception as e:
        ok(f"render_text bez bledu ({e})", False)
    # render_html z konkretami + pozycjami
    try:
        html = render_html(dec, c, equity_pln=1648, free_cash_pln=55.0, fx=3.64, held=held, today="2026-06-03")
        ok("render_html bez bledu", True)
        ok("HTML: liczba akcji", "akcji" in html or "akc." in html)
        ok("HTML: Sell Stop $", "Sell Stop $" in html)
        ok("HTML: cel $", "Cel orientacyjny" in html)
        ok("HTML: ryzyko zl", "Ryzyko jeśli stop" in html)
        ok("HTML: obecne pozycje (SAP)", "SAP" in html and "obecne pozycje" in html)
        ok("HTML: USD/PLN", "USD/PLN" in html)
        ok("HTML: IKE", "IKE" in html)
    except Exception as e:
        ok(f"render_html bez bledu ({e})", False)

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== lowca_pipeline.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Łowca — okazje przed otwarciem")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--signals", default=None, help="ścieżka do opportunity_signals.json")
    ap.add_argument("--capital", type=float, default=None, help="equity w zł (domyślnie z equity_log.json)")
    ap.add_argument("--cash", type=float, default=None, help="wolna gotówka w zł (domyślnie z portfolio.json)")
    ap.add_argument("--fx", type=float, default=None, help="kurs USD/PLN (domyślnie z equity_log.json)")
    ap.add_argument("--sleeve-used", type=float, default=0.0)
    ap.add_argument("--open-spec", type=int, default=0)
    ap.add_argument("--today", default="", help="data do nagłówka maila (YYYY-MM-DD)")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _run_selftest()
    return run(signals_path=a.signals, capital=a.capital, cash=a.cash, fx=a.fx, send=a.send,
               sleeve_used=a.sleeve_used, open_spec=a.open_spec, today=a.today)


if __name__ == "__main__":
    raise SystemExit(main())
