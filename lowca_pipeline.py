# -*- coding: utf-8 -*-
"""
lowca_pipeline.py — BOT ŁOWCA (osobny od bota bilansowego main_pipeline.py).

Bieg PRZED OTWARCIEM: bierze okazje z opportunity_lens (IPO / wolumen / kontrakt)
ORAZ smart-money z lowca_sources (insiderzy / fundusze 13F / kongresmeni), scala je,
liczy KONFLUENCJĘ (ta sama spółka w wielu źródłach = wyższy score) i NAKŁADA
WARSTWĘ DECYZYJNĄ: KUP/PASS z konkretnym zleceniem (ile akcji, cena wejścia,
Sell Stop USD, cel, ryzyko zł) w klatce bezpieczeństwa (sleeve <=15%, min ~43 zł).

CO ROBI (v4):
- SAM czyta z repo: equity, kurs USD/PLN, wolną gotówkę i OBECNE POZYCJE
  (equity_log.json + portfolio.json). Nic nie modyfikuje. Nie poleca spółki, którą masz.
- Scala radar lensa + smart-money; KONFLUENCJA podbija score (max +1.5).
- Tryb --alert-only: bieg MILCZY, chyba że najlepszy score >= progu alertu (domyślnie 8)
  -> wtedy wysyła "PILNA OKAZJA". Do częstszych biegów w środku sesji.
- Mail HTML w stylu bilansu (import z email_render), z sekcją pozycji i konkretami.

Łowca NIE składa zleceń — podaje gotowe do XTB. Konto IKE nietykalne.

UŻYCIE:
    python lowca_pipeline.py --selftest
    python lowca_pipeline.py --signals opportunity_signals.json --today 2026-06-04 --send
    python lowca_pipeline.py --signals ... --alert-only --send   # cichy, alert tylko gdy score>=8
"""
from __future__ import annotations
import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dataclasses import dataclass, field
import opportunity_lens as oppl
try:
    import lowca_sources as lsrc
except Exception:
    lsrc = None


@dataclass
class LowcaConfig:
    capital_pln: float = 1582.0
    sleeve_pct: float = 0.15
    min_position_pln: float = 43.0
    max_position_pln: float = 120.0
    max_open_spec: int = 3
    buy_threshold: float = 6.0
    alert_threshold: float = 8.0       # score >= -> PILNY alert
    fx_usd_pln: float = 4.0
    stop_contract: float = 0.20
    stop_volume: float = 0.15
    stop_ipo: float = 0.25
    stop_insider: float = 0.18
    stop_fund13f: float = 0.20
    stop_congress: float = 0.20
    tp_contract: float = 0.40
    tp_volume: float = 0.30
    tp_ipo: float = 0.60
    tp_insider: float = 0.40
    tp_fund13f: float = 0.40
    tp_congress: float = 0.40


def _clamp(x, lo, hi): return max(lo, min(hi, x))


def read_account(portfolio_path="portfolio.json", equity_path="equity_log.json") -> dict:
    """Czyta equity+kurs (equity_log.json) i gotówkę+pozycje (portfolio.json). Tylko ODCZYT."""
    acc = {"equity_pln": None, "cash_pln": None, "fx_usd_pln": None, "held": []}
    try:
        with open(equity_path, encoding="utf-8") as f:
            log = json.load(f)
        if isinstance(log, list) and log:
            last = log[-1]
            if last.get("equity_pln") is not None:
                acc["equity_pln"] = float(last["equity_pln"])
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
            acc["held"].append({"ticker": p.get("ticker", "?"),
                                "shares": float(p.get("shares", 0) or 0),
                                "entry_usd": float(p.get("entry_price_usd", 0) or 0),
                                "stop_usd": float(p.get("stop_loss_usd", 0) or 0)})
    except Exception:
        pass
    return acc


def _price_map(signals: dict) -> dict:
    out = {}
    if not isinstance(signals, dict):
        return out
    for key in ("contract", "volume", "ipo", "theme", "lockup", "insider", "fund13f", "congress"):
        for s in (signals.get(key) or []):
            if isinstance(s, dict) and s.get("ticker") and s.get("price_usd"):
                try:
                    out[str(s["ticker"]).upper()] = float(s["price_usd"])
                except Exception:
                    pass
    return out


def _score_lens(opp: dict) -> float:
    """Score okazji z opportunity_lens (IPO/WOLUMEN/KONTRAKT)."""
    kind = opp.get("kind")
    if kind == "KONTRAKT":
        ratio = float(opp.get("ratio", 0.5) or 0.5)
        return round(6.0 + _clamp(ratio, 0, 1.0) * 3.0, 2)
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


def _opp_score(o: dict) -> float:
    """Score okazji: base_score (smart-money) albo wyliczony z lensa."""
    bs = o.get("base_score")
    return float(bs) if bs is not None else _score_lens(o)


def _stop_pct(kind: str, c: LowcaConfig) -> float:
    return {"KONTRAKT": c.stop_contract, "WOLUMEN": c.stop_volume, "IPO": c.stop_ipo,
            "INSIDER": c.stop_insider, "FUND13F": c.stop_fund13f, "CONGRESS": c.stop_congress}.get(kind, 0.20)


def _tp_pct(kind: str, c: LowcaConfig) -> float:
    return {"KONTRAKT": c.tp_contract, "WOLUMEN": c.tp_volume, "IPO": c.tp_ipo,
            "INSIDER": c.tp_insider, "FUND13F": c.tp_fund13f, "CONGRESS": c.tp_congress}.get(kind, 0.40)


def build_candidates(radar: dict, smart: dict, price_map=None) -> "list[dict]":
    """Scala radar lensa + smart-money, deduplikuje po tickerze, liczy KONFLUENCJĘ.
    Konfluencja (N źródeł na spółkę) podbija score o +0.6*(N-1), max +1.5."""
    price_map = price_map or {}
    lens_all = (radar or {}).get("all", []) if radar else []
    smart_all = (smart or {}).get("all", []) if smart else []
    by_ticker = {}
    for o in (lens_all + smart_all):
        tk = str(o.get("ticker", "?")).upper()
        by_ticker.setdefault(tk, []).append(o)
    out = []
    for tk, opps in by_ticker.items():
        kinds = []
        for o in opps:
            k = o.get("kind", "?")
            if k not in kinds:
                kinds.append(k)
        best = max(opps, key=_opp_score)
        base = _opp_score(best)
        conf_bonus = min(1.5, 0.6 * (len(kinds) - 1))
        final = round(min(10.0, base + conf_bonus), 2)
        price = 0.0
        for o in opps:
            if o.get("price_usd"):
                price = float(o["price_usd"]); break
        if not price:
            price = float(price_map.get(tk, 0) or 0)
        note = best.get("note", "")
        if len(kinds) > 1:
            note = "KONFLUENCJA " + "+".join(kinds) + " · " + note
        out.append({"ticker": tk, "kind": best.get("kind", "?"), "kinds": kinds,
                    "confluence": len(kinds), "note": note, "risk": best.get("risk", ""),
                    "price_usd": round(price, 2), "score": final})
    out.sort(key=lambda m: m["score"], reverse=True)
    return out


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
    kinds: list = field(default_factory=list)
    confluence: int = 1
    price_usd: float = 0.0
    stop_price_usd: float = 0.0
    target_price_usd: float = 0.0
    shares: float = 0.0
    risk_pln: float = 0.0


def _shares(size_pln, price_usd, fx):
    if not price_usd or price_usd <= 0 or not fx or fx <= 0:
        return 0.0
    return round((size_pln / fx) / price_usd, 4)


def deployable_budget(c: LowcaConfig, free_cash_pln=None, sleeve_used_pln: float = 0.0):
    sleeve_cap = c.capital_pln * c.sleeve_pct
    avail_sleeve = max(0.0, sleeve_cap - sleeve_used_pln)
    if free_cash_pln is None:
        return avail_sleeve, sleeve_cap, False
    budget = max(0.0, min(avail_sleeve, float(free_cash_pln)))
    return budget, sleeve_cap, float(free_cash_pln) < avail_sleeve


def decide_all(candidates, c: LowcaConfig, free_cash_pln=None, held=None, fx=None,
               sleeve_used_pln: float = 0.0, open_spec: int = 0) -> "list[LowcaDecision]":
    """KUP/PASS + sizing w klatce min(sleeve, gotówka), z konkretami i konfluencją.
    Pomija spółki już posiadane."""
    held_set = {str(t).upper() for t in (held or [])}
    fx = fx or c.fx_usd_pln
    cand = []
    for o in (candidates or []):
        d = LowcaDecision(ticker=str(o.get("ticker", "?")), kind=o.get("kind", "?"),
                          score=float(o.get("score", 0)), risk=o.get("risk", ""),
                          note=o.get("note", ""), kinds=list(o.get("kinds", [])) or [o.get("kind", "?")],
                          confluence=int(o.get("confluence", 1)), price_usd=float(o.get("price_usd", 0) or 0))
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
        if d.price_usd and d.price_usd > 0:
            d.stop_price_usd = round(d.price_usd * (1 - d.stop_pct), 2)
            d.target_price_usd = round(d.price_usd * (1 + _tp_pct(d.kind, c)), 2)
            d.shares = _shares(size, d.price_usd, fx)
        d.risk_pln = round(size * d.stop_pct, 0)
        used += size; n += 1
    return cand


def best_score(decisions) -> float:
    return max([d.score for d in decisions], default=0.0)


# ─────────────────────────────────────────────────────────────────────────────
# RENDER TEKSTOWY
# ─────────────────────────────────────────────────────────────────────────────
def render_text(decisions, c, equity_pln=None, free_cash_pln=None, fx=None, held=None, sleeve_used_pln=0.0):
    buys = [d for d in decisions if d.verdict == "BUY"]
    passes = [d for d in decisions if d.verdict == "PASS"]
    budget, sleeve_cap, cash_limited = deployable_budget(c, free_cash_pln, sleeve_used_pln)
    hot = best_score(buys) >= c.alert_threshold
    L = ["=" * 66, ("  PILNA OKAZJA! " if hot else "  ") + "BOT LOWCA — DECYZJE PRZED OTWARCIEM", "=" * 66]
    L.append(f"  Equity: {(equity_pln or c.capital_pln):.0f} zl | Wolna gotowka: "
             f"{('%.0f zl' % free_cash_pln) if free_cash_pln is not None else '—'} | "
             f"Do wydania: {budget:.0f} zl | USD/PLN: {fx or c.fx_usd_pln:.2f}")
    if held:
        L.append("  Masz juz: " + ", ".join(f"{h['ticker']} ({h['shares']:.3f})" for h in held))
    if buys:
        L.append("\n  KUPUJEMY:")
        for i, d in enumerate(buys, 1):
            conf = f" [KONFLUENCJA {'+'.join(d.kinds)}]" if d.confluence > 1 else f" [{d.kind}]"
            if d.price_usd:
                L.append(f"   {i}. {d.ticker}{conf} — {d.shares:.4f} akc. za {d.size_pln:.0f} zl · "
                         f"wejscie ${d.price_usd:.2f} · Stop ${d.stop_price_usd:.2f} · cel ${d.target_price_usd:.2f} "
                         f"· ryzyko {d.risk_pln:.0f} zl · score {d.score:.1f}")
            else:
                L.append(f"   {i}. {d.ticker}{conf} — KUP za {d.size_pln:.0f} zl · stop -{d.stop_pct*100:.0f}% "
                         f"· ryzyko {d.risk_pln:.0f} zl · score {d.score:.1f}")
            if d.note:
                L.append(f"        {d.note}")
    else:
        L.append("\n  KUPUJEMY: dzis nic (prog / gotowka / juz w portfelu).")
    if passes:
        L.append("\n  PASS:")
        for d in passes[:8]:
            L.append(f"   - {d.ticker} [{d.kind}] — {d.reason}")
    L.append("=" * 66)
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# RENDER HTML — styl bilansu
# ─────────────────────────────────────────────────────────────────────────────
def render_html(decisions, c, equity_pln=None, free_cash_pln=None, fx=None, held=None,
                sleeve_used_pln=0.0, today="") -> str:
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
    hot = best_score(buys) >= c.alert_threshold
    cash_txt = (f"{free_cash_pln:,.0f} zł" if free_cash_pln is not None else "—")
    kind_label = {"KONTRAKT": "Mała spółka + duży kontrakt", "WOLUMEN": "Nietypowy wolumen / wybicie",
                  "IPO": "Świeże IPO z momentum", "INSIDER": "Zakupy insiderów",
                  "FUND13F": "Ruch funduszu (13F)", "CONGRESS": "Zakup kongresmena"}

    P = [f"<div style='{SANS}background:{BG};padding:22px;'><div style='max-width:700px;margin:0 auto;'>"]

    # HEADER
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
        f"Do wydania {budget:,.0f} zł · USD/PLN {fxv:.2f}</div></div>"
    )

    # PILNY BANER
    if hot:
        P.append(
            f"<div style='background:{RED};border-radius:12px;padding:16px 20px;margin-bottom:18px;"
            f"{SANS}font-size:14pt;color:#ffffff;font-weight:bold;'>PILNA OKAZJA — "
            f"najwyższy score {best_score(buys):.1f}/10 (próg alertu {c.alert_threshold:.0f}). Patrz niżej.</div>"
        )

    # TWOJE PIENIĄDZE
    if cash_limited:
        money_note = f"Masz <b>{cash_txt}</b> wolnej gotówki — mniej niż budżet sleeve ({sleeve_cap:,.0f} zł). Decyzje zmieściłem w gotówce."
        money_bar = RED
    elif free_cash_pln is not None:
        money_note = f"Wolna gotówka <b>{cash_txt}</b> pokrywa cały budżet sleeve ({sleeve_cap:,.0f} zł). Limitem jest strategia (15%)."
        money_bar = GREEN
    else:
        money_note = "Brak danych o gotówce — sizing wg sleeve (15% equity)."
        money_bar = GOLD_DK
    P.append(
        _card_open("Twoje pieniądze dziś", "Tyle realnie możesz dziś wydać na spekulację.") +
        f"<div style='border-left:5px solid {money_bar};background:#f8fafc;border-radius:8px;padding:14px 16px;"
        f"{SANS}font-size:12.5pt;color:{TEXT};line-height:1.6;margin-bottom:14px;'>{money_note}</div>"
        f"<table style='width:100%;border-collapse:collapse;{SANS}font-size:12.5pt;color:{TEXT};'>"
        f"<tr><td style='padding:7px 0;'>Equity (cały rachunek)</td><td style='padding:7px 0;text-align:right;font-weight:bold;color:{DARK};'>{eq:,.0f} zł</td></tr>"
        f"<tr><td style='padding:7px 0;border-top:1px solid {LINE2};'>Wolna gotówka</td><td style='padding:7px 0;text-align:right;border-top:1px solid {LINE2};font-weight:bold;color:{DARK};'>{cash_txt}</td></tr>"
        f"<tr><td style='padding:7px 0;border-top:1px solid {LINE2};'>Sleeve (max 15%)</td><td style='padding:7px 0;text-align:right;border-top:1px solid {LINE2};color:{TEXT};'>{sleeve_cap:,.0f} zł</td></tr>"
        f"<tr><td style='padding:7px 0;border-top:2px solid {LINE};'><b>Do wydania dziś</b></td><td style='padding:7px 0;text-align:right;border-top:2px solid {LINE};font-weight:bold;color:{GREEN};font-size:14pt;'>{budget:,.0f} zł</td></tr>"
        f"<tr><td style='padding:7px 0;'>Łączne ryzyko sleeve (jeśli stopy)</td><td style='padding:7px 0;text-align:right;font-weight:bold;color:{RED};'>{risk_total:,.0f} zł</td></tr>"
        f"</table></div>"
    )

    # TWOJE OBECNE POZYCJE
    if held:
        rows = ""
        for h in held:
            rows += (f"<tr><td style='padding:10px 8px 10px 0;border-bottom:1px solid {LINE2};{SANS}font-size:12.5pt;'><b style='color:{DARK};'>{h['ticker']}</b></td>"
                     f"<td style='padding:10px 8px;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{TEXT};'>{h['shares']:.4f} akc.</td>"
                     f"<td style='padding:10px 8px;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{MUTED};'>wejście ${h['entry_usd']:.2f}</td>"
                     f"<td style='padding:10px 0;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{TEXT};'>Stop ${h['stop_usd']:.2f}</td></tr>")
        P.append(_card_open("Twoje obecne pozycje (rdzeń — z bilansu)",
                            "Ten sam stan widzi bot bilansowy. Łowca tego NIE rusza i nie poleca duplikatów.") +
                 f"<table style='width:100%;border-collapse:collapse;'>{rows}</table></div>")

    # DECYZJE KUP
    if buys:
        P.append(_card_open("Decyzje KUP — gotowe zlecenia na XTB",
                            "Wykonaj o 15:30 w xStation. Ceny w USD; kwoty w zł."))
        for d in buys:
            conf_badge = ""
            if d.confluence > 1:
                conf_badge = (f"<span style='display:inline-block;{SANS}font-size:10pt;font-weight:bold;color:#166534;"
                              f"background:{GREEN_LT};border:1px solid #86efac;padding:4px 9px;border-radius:5px;margin-bottom:6px;'>"
                              f"KONFLUENCJA: {' + '.join(d.kinds)}</span><br>")
            if d.price_usd:
                do_html = (f"Kup <b>{d.shares:.4f} akcji {d.ticker}</b> (≈ <b>{d.size_pln:,.0f} zł</b>) po cenie rynkowej "
                           f"(~<b>${d.price_usd:.2f}</b>).<br>Ustaw <b>Sell Stop ${d.stop_price_usd:.2f}</b>. "
                           f"Cel orientacyjny <b>${d.target_price_usd:.2f}</b> (lub trailing — zysk bez capa).")
            else:
                do_html = (f"Kup <b>{d.ticker}</b> za <b>{d.size_pln:,.0f} zł</b> po cenie rynkowej. "
                           f"Ustaw Sell Stop −{d.stop_pct*100:.0f}% (brak ceny w sygnale).")
            why_html = (f"{conf_badge}{d.note or 'Okazja spekulacyjna.'} "
                        f"<br><span style='color:{RED};font-weight:bold;'>Ryzyko jeśli stop: {d.risk_pln:,.0f} zł.</span> "
                        f"<span style='color:{MUTED};'>Typ: {kind_label.get(d.kind, d.kind)} · score {d.score:.1f}/10 · {d.risk or 'WYSOKIE'}.</span>")
            P.append(_action_block(company=d.ticker, order="Zlecenie: KUP po cenie rynkowej + Sell Stop",
                                   order_color=GREEN, bar_color=GREEN, do_html=do_html, why_html=why_html))
        P.append("</div>")
    else:
        P.append(_card_open("Decyzje KUP — gotowe zlecenia na XTB") +
                 f"<div style='{SANS}font-size:13pt;color:{TEXT};line-height:1.6;'>"
                 f"<b>Dziś nie kupujemy.</b> Żadna okazja nie przeszła progu, zabrakło gotówki albo już masz tę spółkę.</div></div>")

    # PASS
    if passes:
        rows = ""
        for d in passes[:8]:
            rows += (f"<tr><td style='padding:10px 8px 10px 0;border-bottom:1px solid {LINE2};{SANS}font-size:12.5pt;'>"
                     f"<b style='color:{DARK};'>{d.ticker}</b> <span style='color:{MUTED};font-size:11pt;'>[{d.kind}]</span></td>"
                     f"<td style='padding:10px 0;border-bottom:1px solid {LINE2};color:{MUTED};{SANS}font-size:11.5pt;'>{d.reason}</td></tr>")
        P.append(_card_open("Pominięte (PASS)", "Okazje, które dziś nie kwalifikują się do zakupu.") +
                 f"<table style='width:100%;border-collapse:collapse;'>{rows}</table></div>")

    # ZASADY / STOPKA
    P.append(_card_open("Zasady łowcy") +
             f"<div style='{SANS}font-size:11.5pt;color:{TEXT};line-height:1.7;'>"
             f"• Źródła: IPO / wolumen / kontrakt + smart-money (insiderzy, fundusze 13F, kongres). "
             f"<b>Konfluencja</b> (wiele źródeł na spółkę) podbija score.<br>"
             f"• Sleeve <b>max 15%</b> equity — rdzeń (85%) niezależnie w bilansie. Każda pozycja ma konkretny Sell Stop.<br>"
             f"• Kwota nigdy nie przekracza wolnej gotówki. Max {c.max_open_spec} spekulacje naraz.<br>"
             f"• <b>Egzekucja = Twój klik na XTB.</b> Konto IKE nietykalne.</div></div>")
    P.append(f"<div style='{SANS}font-size:10pt;color:{MUTED2};text-align:center;padding:20px 0;line-height:1.6;'>"
             f"Spekulacja wysokiego ryzyka, NIE porada inwestycyjna. Ceny wejścia to ostatni kurs (orientacyjnie).<br>"
             f"<em>Kwoty w zł; ceny i Sell Stop wpisujesz w xStation w USD.</em></div>")
    P.append("</div></div>")
    return "".join(P)


def run(signals_path=None, capital=None, cash=None, fx=None, send=False,
        sleeve_used=0.0, open_spec=0, today="", alert_only=False) -> int:
    acc = read_account()
    capital = capital if capital is not None else (acc["equity_pln"] or 1582.0)
    cash = cash if cash is not None else acc["cash_pln"]
    fx = fx if fx is not None else (acc["fx_usd_pln"] or 4.0)
    held = acc["held"]
    held_tickers = [h["ticker"] for h in held]

    c = LowcaConfig(capital_pln=capital, fx_usd_pln=fx)
    signals = oppl.read_opportunity_signals(signals_path) if signals_path else oppl.read_opportunity_signals()
    radar = oppl.build_radar(signals)
    smart = lsrc.build_smart(signals) if lsrc else {"all": []}
    candidates = build_candidates(radar, smart, _price_map(signals))
    decisions = decide_all(candidates, c, free_cash_pln=cash, held=held_tickers, fx=fx,
                           sleeve_used_pln=sleeve_used, open_spec=open_spec)
    print(render_text(decisions, c, equity_pln=capital, free_cash_pln=cash, fx=fx, held=held, sleeve_used_pln=sleeve_used))

    buys = [d for d in decisions if d.verdict == "BUY"]
    hot = best_score(buys) >= c.alert_threshold
    if alert_only and not hot:
        print(f"[alert-only] brak okazji score>={c.alert_threshold:.0f} — mail POMINIETY (cicho).")
        return 0
    if send:
        try:
            from notifications import send_email_resend
            html = render_html(decisions, c, equity_pln=capital, free_cash_pln=cash, fx=fx,
                               held=held, sleeve_used_pln=sleeve_used, today=today)
            subj = (f"Bot Łowca — PILNA OKAZJA (score {best_score(buys):.1f})" if hot
                    else "Bot Łowca — okazje przed otwarciem")
            r = send_email_resend(html, subj, dry_run=False)
            print("Mail:", "OK" if r.get("ok") else r.get("note"), "id=", r.get("id"))
        except Exception as e:
            print(f"Mail pominięty: {e}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST lowca_pipeline (smart money + konfluencja + alert) ===")
    P = F = 0
    def ok(n, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")
    c = LowcaConfig(capital_pln=1648.0, fx_usd_pln=3.64)

    signals = {
        "contract": [{"ticker": "OKLO", "contract_usd": 900e6, "market_cap_usd": 1.8e9, "hours_ago": 2, "price_usd": 22.5}],
        "ipo": [{"ticker": "RDDT", "age_months": 8, "pct_from_ipo": 60, "volume_mult": 2, "price_usd": 140.0}],
        "insider": [{"ticker": "RDDT", "buyers": 3, "usd_total": 1_500_000, "days_ago": 2, "price_usd": 140.0}],
        "congress": [{"ticker": "RDDT", "member": "X", "amount_usd": 200_000, "days_ago": 6, "price_usd": 140.0}],
    }
    radar = oppl.build_radar(signals)
    smart = lsrc.build_smart(signals)
    ok("smart-money: 2 okazje (insider+congress)", len(smart["all"]) == 2)
    cands = build_candidates(radar, smart, _price_map(signals))

    rddt = next((m for m in cands if m["ticker"] == "RDDT"), None)
    ok("RDDT scalony z wielu zrodel", rddt and rddt["confluence"] >= 3)
    ok("RDDT konfluencja podbila score (>= IPO bazowy)", rddt and rddt["score"] > 6.5)
    # IPO bazowo 6.5 (wolumen w nocie) -> +konfluencja (3 zrodla) +1.2 -> ~7.7+; insider base 6.9
    ok("RDDT score >= 7.5 (konfluencja dziala)", rddt and rddt["score"] >= 7.5)
    ok("Brak duplikatow tickera w kandydatach", len({m["ticker"] for m in cands}) == len(cands))

    dec = decide_all(cands, c, free_cash_pln=200.0, fx=3.64)
    buys = [d for d in dec if d.verdict == "BUY"]
    rddt_d = next((d for d in buys if d.ticker == "RDDT"), None)
    ok("RDDT -> BUY", rddt_d is not None)
    ok("BUY ma konkretne akcje/stop/cel", rddt_d and rddt_d.shares > 0 and rddt_d.stop_price_usd > 0 and rddt_d.target_price_usd > rddt_d.price_usd)
    ok("BUY niesie liste kinds (konfluencja)", rddt_d and len(rddt_d.kinds) >= 2)

    # ALERT: spotka silna konfluencja -> score>=8
    big = {
        "contract": [{"ticker": "ZZZ", "contract_usd": 2e9, "market_cap_usd": 2e9, "hours_ago": 1, "price_usd": 10}],
        "insider": [{"ticker": "ZZZ", "buyers": 4, "usd_total": 3e6, "days_ago": 1, "price_usd": 10}],
        "congress": [{"ticker": "ZZZ", "member": "Y", "amount_usd": 500_000, "days_ago": 2, "committee_relevant": True, "price_usd": 10}],
    }
    cb = build_candidates(oppl.build_radar(big), lsrc.build_smart(big), _price_map(big))
    zzz = next((m for m in cb if m["ticker"] == "ZZZ"), None)
    ok("Silna konfluencja ZZZ -> score >= 8 (alert)", zzz and zzz["score"] >= 8.0)
    decb = decide_all(cb, c, free_cash_pln=200.0, fx=3.64)
    ok("best_score >= prog alertu", best_score([d for d in decb if d.verdict == "BUY"]) >= c.alert_threshold)

    # held -> pomija
    dec_h = decide_all(cands, c, free_cash_pln=200.0, held=["RDDT"], fx=3.64)
    ok("Held RDDT -> PASS", any(d.ticker == "RDDT" and d.verdict == "PASS" for d in dec_h))

    # gotowka 0 -> 0 zakupow
    ok("Gotowka 0 -> 0 BUY", sum(1 for d in decide_all(cands, c, free_cash_pln=0.0) if d.verdict == "BUY") == 0)
    # brak smart (None) -> dziala na samym lensie
    ok("Bez smart -> dziala (sam lens)", isinstance(build_candidates(radar, {"all": []}, _price_map(signals)), list))
    # pusty
    ok("Pusto -> 0 kandydatow", build_candidates(oppl.build_radar({"_empty": True}), {"all": []}) == [])
    ok("Sygnaly None -> brak crasha", isinstance(lsrc.build_smart(None), dict))

    # render bez bledu + alert baner
    held = [{"ticker": "SAP", "shares": 0.799, "entry_usd": 176.37, "stop_usd": 176.37}]
    try:
        html = render_html(decb, c, equity_pln=1648, free_cash_pln=200.0, fx=3.64, held=held, today="2026-06-04")
        ok("render_html bez bledu", True)
        ok("HTML: baner PILNA OKAZJA przy score>=8", "PILNA OKAZJA" in html)
        ok("HTML: badge KONFLUENCJA", "KONFLUENCJA" in html)
        ok("HTML: konkretny Sell Stop $", "Sell Stop $" in html)
        ok("HTML: obecne pozycje SAP", "SAP" in html and "obecne pozycje" in html)
    except Exception as e:
        ok(f"render_html bez bledu ({e})", False)
    try:
        render_text(decb, c, equity_pln=1648, free_cash_pln=200.0, fx=3.64, held=held); ok("render_text bez bledu", True)
    except Exception as e:
        ok(f"render_text bez bledu ({e})", False)

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== lowca_pipeline.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Łowca — okazje przed otwarciem (smart money + alerty)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--signals", default=None)
    ap.add_argument("--capital", type=float, default=None, help="equity w zł (domyślnie z equity_log.json)")
    ap.add_argument("--cash", type=float, default=None, help="wolna gotówka w zł (domyślnie z portfolio.json)")
    ap.add_argument("--fx", type=float, default=None, help="kurs USD/PLN (domyślnie z equity_log.json)")
    ap.add_argument("--sleeve-used", type=float, default=0.0)
    ap.add_argument("--open-spec", type=int, default=0)
    ap.add_argument("--today", default="")
    ap.add_argument("--alert-only", action="store_true", help="mail tylko gdy score >= prog alertu")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _run_selftest()
    return run(signals_path=a.signals, capital=a.capital, cash=a.cash, fx=a.fx, send=a.send,
               sleeve_used=a.sleeve_used, open_spec=a.open_spec, today=a.today, alert_only=a.alert_only)


if __name__ == "__main__":
    raise SystemExit(main())
