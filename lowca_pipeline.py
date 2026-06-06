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
try:
    import lowca_misses as lmiss
except Exception:
    lmiss = None
try:
    import scoreboard as sb
except Exception:
    sb = None
try:
    import wall_street as ws
except Exception:
    ws = None


@dataclass
class LowcaConfig:
    capital_pln: float = 1582.0
    sleeve_pct: float = 0.15
    min_position_pln: float = 43.0
    max_position_pln: float = 80.0     # E: mniejszy cap (sleeve ~237 zł — 3×120 to fikcja); mniej ekspozycji na nieudowodniony edge
    max_open_spec: int = 2             # E: max 2 spekulacje naraz (mały sleeve — nie rozcieńczaj w szum)
    risk_per_trade_pct: float = 0.008  # G: risk-parity — każda spekulacja ryzykuje 0,8% kapitału (równy złotówkowy risk)
    min_rr_enter: float = 1.8          # ASYMETRIA: poniżej tego R:R nie wchodzimy (za słaby zakład)
    min_rr_full: float = 2.2           # R:R >= tego = pełna pozycja (presuj najlepszą asymetrię); między = ×mult
    rr_marginal_mult: float = 0.85     # słabsza asymetria (między enter a full) -> mniejsza pozycja
    buy_threshold: float = 6.0
    alert_threshold: float = 8.0       # score >= -> PILNY alert
    extension_warn_pct: float = 0.25   # już +25% w ~mies -> rozgrzane: CZEKAJ, CHYBA ŻE mocne przesłanki dalej (furtka)
    extension_hard_pct: float = 0.50   # już +50% -> parabola: ZAWSZE czekaj (furtka nie działa)
    override_size_mult: float = 0.6    # furtka = kupno mimo rozgrzania -> mniejsza pozycja (wyższe ryzyko)
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


def _meta_map(signals: dict) -> dict:
    """Ticker -> {pct_1m, catalyst}: ruch ~1-mies (bezpiecznik 'nie kupuj górki') +
    tag katalizatora (polityczny/Trump/kontrakt rządowy) do podświetlenia w mailu.
    Łapie kategorie 'policy'/'trump' (zagrywki polityczne) ORAZ pole catalyst w każdej innej."""
    out = {}
    if not isinstance(signals, dict):
        return out
    for key in ("policy", "trump", "contract", "volume", "ipo", "theme", "lockup", "insider", "fund13f", "congress"):
        for s in (signals.get(key) or []):
            if not (isinstance(s, dict) and s.get("ticker")):
                continue
            tk = str(s["ticker"]).upper()
            m = out.setdefault(tk, {})
            for f in ("pct_1m", "pct_1mo", "month_pct", "run_pct"):
                if s.get(f) is not None and "pct_1m" not in m:
                    try:
                        m["pct_1m"] = float(s[f])
                    except Exception:
                        pass
            cat = s.get("catalyst", "")
            if not cat and key in ("policy", "trump"):
                cat = s.get("source") or "Trump/polityka"
            if cat and not m.get("catalyst"):
                m["catalyst"] = str(cat)
            # FURTKA: mocne przesłanki, że spółka jeszcze nie osiągnęła szczytu (świeży katalizator ≫ cena)
            if s.get("still_upside") and not m.get("still_upside"):
                m["still_upside"] = True
                m["upside_reason"] = str(s.get("upside_reason") or s.get("still_upside_reason") or "mocne przesłanki dalszego wzrostu")
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


def build_candidates(radar: dict, smart: dict, price_map=None, meta_map=None) -> "list[dict]":
    """Scala radar lensa + smart-money, deduplikuje po tickerze, liczy KONFLUENCJĘ.
    Konfluencja (N źródeł na spółkę) podbija score o +0.6*(N-1), max +1.5.
    Dokłada pct_1m (ruch ~1-mies) i catalyst (Trump/polityka) z meta_map."""
    price_map = price_map or {}
    meta_map = meta_map or {}
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
        meta = meta_map.get(tk, {})
        catalyst = meta.get("catalyst", "") or best.get("catalyst", "")
        if catalyst:
            note = ("🇺🇸 " + catalyst + " · " + note) if note else ("🇺🇸 " + catalyst)
        out.append({"ticker": tk, "kind": best.get("kind", "?"), "kinds": kinds,
                    "confluence": len(kinds), "note": note, "risk": best.get("risk", ""),
                    "price_usd": round(price, 2), "score": final,
                    "pct_1m": float(meta.get("pct_1m", 0) or 0), "catalyst": catalyst,
                    "still_upside": bool(meta.get("still_upside", False)),
                    "upside_reason": str(meta.get("upside_reason", ""))})
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
    pct_1m: float = 0.0          # ruch ~1-mies (do bezpiecznika "nie kupuj górki")
    catalyst: str = ""           # tag katalizatora (np. Trump/polityka/kontrakt rządowy)
    still_upside: bool = False   # FURTKA: mocne przesłanki dalszego wzrostu (świeży katalizator ≫ cena)
    upside_reason: str = ""      # uzasadnienie furtki
    rr: float = 0.0              # ASYMETRIA: potencjał/ryzyko (target%/stop%) — wyższa = lepszy zakład; presuj najlepsze
    ev_pct: float = None         # opcjonalny EV od agenta (scenariusze bull/base/bear); <=0 => nie wchodź


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
                          confluence=int(o.get("confluence", 1)), price_usd=float(o.get("price_usd", 0) or 0),
                          pct_1m=float(o.get("pct_1m", 0) or 0), catalyst=o.get("catalyst", ""),
                          still_upside=bool(o.get("still_upside", False)), upside_reason=str(o.get("upside_reason", "")),
                          ev_pct=(float(o["ev_pct"]) if o.get("ev_pct") is not None else None))
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
        # ── BEZPIECZNIK "NIE KUPUJ GÓRKI" + FURTKA (Wniosek #4) ──
        override = False
        if d.pct_1m >= c.extension_hard_pct * 100:          # >= +50% = parabola: ZAWSZE czekaj
            d.verdict = "CZEKAJ"
            d.reason = f"już +{d.pct_1m:.0f}% (parabola) — czekaj na głębsze cofnięcie, za gorące nawet z katalizatorem"
            continue
        if d.pct_1m >= c.extension_warn_pct * 100:          # +25..50% = rozgrzane
            if d.still_upside or d.confluence >= 3:         # FURTKA: mocne przesłanki dalej LUB silna konfluencja
                override = True
            else:
                d.verdict = "CZEKAJ"
                d.reason = f"już +{d.pct_1m:.0f}% w ~mies — czekaj na cofnięcie (brak mocnych przesłanek dalszego wzrostu)"
                continue
        # ── ASYMETRIA (R:R) + EV ── presuj zakłady, gdzie potencjał ≫ ryzyko; blokuj ujemny EV.
        stop_pct = _stop_pct(d.kind, c)
        tp_pct = _tp_pct(d.kind, c)
        d.rr = round(tp_pct / stop_pct, 2) if stop_pct > 0 else 0.0
        if d.ev_pct is not None and d.ev_pct <= 0:
            d.verdict = "CZEKAJ"; d.reason = f"EV {d.ev_pct:+.0f}% ≤ 0 — brak dodatniej asymetrii, czekam"; continue
        if d.rr < c.min_rr_enter:
            d.verdict = "CZEKAJ"; d.reason = f"R:R {d.rr:.1f} < {c.min_rr_enter:.1f} — za słaba asymetria, czekam"; continue
        # G: RISK-PARITY — każda spekulacja ryzykuje TYLE SAMO zł (anti-ruina), niezależnie od
        # (nieudowodnionego) score. size = budżet_ryzyka / stop%. Szerszy stop -> mniejsza pozycja.
        risk_budget = c.risk_per_trade_pct * c.capital_pln
        size = risk_budget / stop_pct if stop_pct > 0 else c.min_position_pln
        size = _clamp(size, c.min_position_pln, c.max_position_pln)
        if d.rr < c.min_rr_full:
            size *= c.rr_marginal_mult          # słabsza asymetria -> mniejsza pozycja (presuj najlepsze)
        if override:
            size *= c.override_size_mult        # furtka = mniejsza pozycja (wyższe ryzyko)
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
        rr_tag = f" · R:R {d.rr:.1f}" + (" (pełna)" if d.rr >= c.min_rr_full else " (mniejsza)")
        if override:
            d.reason = f"KUPNO MIMO +{d.pct_1m:.0f}% — {d.upside_reason or ('konfluencja ' + str(d.confluence))} (ryzyko ↑, pozycja ↓){rr_tag}"
        else:
            d.reason = f"score {d.score:.1f} >= {c.buy_threshold:.0f}{rr_tag}"
        if d.price_usd and d.price_usd > 0:
            d.stop_price_usd = round(d.price_usd * (1 - d.stop_pct), 2)
            d.target_price_usd = round(d.price_usd * (1 + _tp_pct(d.kind, c)), 2)
            d.shares = _shares(size, d.price_usd, fx)
        d.risk_pln = round(size * d.stop_pct, 0)
        used += size; n += 1
    return cand


def best_score(decisions) -> float:
    return max([d.score for d in decisions], default=0.0)


def select_ultra_pick(decisions, c: "LowcaConfig"):
    """ULTRA-PICK — najwyższe przekonanie. Gating oparty na ANALIZIE HISTORYCZNEJ:
    - score >= próg alertu (silny sygnał),
    - konfluencja >= 2 (wiele źródeł = REALNY katalizator, nie pojedynczy hype/wolumen —
      to właśnie pojedyncze, niepotwierdzone skoki najczęściej oddają zysk najszybciej).
    Zwraca JEDNĄ pozycję (najwyższy score, potem konfluencja) albo None.
    "Jak najbardziej pewna, ale możliwa" = NIE pewniak, tylko maks. przekonanie przy kontroli ryzyka."""
    cands = [d for d in (decisions or [])
             if d.verdict == "BUY" and d.score >= c.alert_threshold and d.confluence >= 2]
    if not cands:
        return None
    return max(cands, key=lambda d: (d.score, d.confluence))


# ─────────────────────────────────────────────────────────────────────────────
# DZIENNIK DECYZJI (forward-test) — zapis rekomendacji do późniejszego pomiaru edge
# ─────────────────────────────────────────────────────────────────────────────
def _load_decisions_log(path="lowca_decisions_log.json") -> list:
    """Wczytuje dziennik decyzji (lista rekordów). Brak/uszkodzony plik -> []."""
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
        return log if isinstance(log, list) else []
    except Exception:
        return []


def log_decisions(decisions, today, path="lowca_decisions_log.json", spy_usd=None) -> dict:
    """FORWARD-TEST: dopisuje dzisiejsze decyzje KUP do dziennika (append-only, idempotentnie po id).
    Dzięki temu można PÓŹNIEJ zmierzyć realny edge rekomendacji łowcy vs SPY (track record na żywo).
    Loguje tylko BUY (faktyczne rekomendacje). Nigdy nie wywala biegu (bezpieczne try/except)."""
    log = _load_decisions_log(path)
    buys = [d for d in (decisions or []) if d.verdict == "BUY"]
    if not today or not buys:
        return {"added": 0, "total": len(log)}
    seen = {r.get("id") for r in log}
    added = 0
    for d in buys:
        rid = f"{today}-{d.ticker.upper()}"
        if rid in seen:
            continue
        log.append({
            "id": rid,
            "date": today,
            "ticker": d.ticker.upper(),
            "kind": d.kind,
            "kinds": d.kinds,
            "confluence": d.confluence,
            "score": round(d.score, 2),
            "entry_usd": round(d.price_usd, 2) if d.price_usd else None,
            "stop_usd": round(d.stop_price_usd, 2) if d.stop_price_usd else None,
            "target_usd": round(d.target_price_usd, 2) if d.target_price_usd else None,
            "shares": round(d.shares, 4) if d.shares else None,
            "size_pln": round(d.size_pln, 0),
            "risk_pln": round(d.risk_pln, 0),
            "rr": d.rr,                          # asymetria (do walidacji EV agenta przez scoreboard)
            "ev_pct": d.ev_pct,                  # EV od agenta (jeśli podał) — scoreboard sprawdzi korelację z wynikiem
            "spy_at_entry": round(float(spy_usd), 2) if spy_usd else None,  # benchmark do ALFY (scoreboard.py)
            "status": "OPEN",   # scoring T+30/+90 robi scoreboard.py: OPEN -> WIN / LOSS / STOP
        })
        seen.add(rid)
        added += 1
    if added:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Dziennik decyzji — zapis pominięty: {e}")
    return {"added": added, "total": len(log)}


# ─────────────────────────────────────────────────────────────────────────────
# RENDER TEKSTOWY
# ─────────────────────────────────────────────────────────────────────────────
def render_text(decisions, c, equity_pln=None, free_cash_pln=None, fx=None, held=None, sleeve_used_pln=0.0, learn=None):
    buys = [d for d in decisions if d.verdict == "BUY"]
    waits = [d for d in decisions if d.verdict == "CZEKAJ"]
    passes = [d for d in decisions if d.verdict == "PASS"]
    budget, sleeve_cap, cash_limited = deployable_budget(c, free_cash_pln, sleeve_used_pln)
    hot = best_score(buys) >= c.alert_threshold
    L = ["=" * 66, ("  MOCNY SYGNAL — " if hot else "  ") + "BOT LOWCA — DECYZJE PRZED OTWARCIEM", "=" * 66]
    L.append(f"  Equity: {(equity_pln or c.capital_pln):.0f} zl | Wolna gotowka: "
             f"{('%.0f zl' % free_cash_pln) if free_cash_pln is not None else '—'} | "
             f"Do wydania: {budget:.0f} zl | USD/PLN: {fx or c.fx_usd_pln:.2f}")
    if held:
        L.append("  Masz juz: " + ", ".join(f"{h['ticker']} ({h['shares']:.3f})" for h in held))
    if learn and learn.get("misses_today"):
        L.append("\n  POMINIETE WCZORAJ (lekcje zapisane):")
        for m in learn["misses_today"][:5]:
            L.append(f"   - {m['ticker']} +{m['pct']:.0f}% — {m.get('lesson','')}")
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
    if waits:
        L.append("\n  CZEKAM na cofniecie (zlapane, ale za gorace — nie kupuj gorki):")
        for d in waits[:6]:
            tag = (" " + d.catalyst) if d.catalyst else ""
            L.append(f"   ~ {d.ticker}{tag} — {d.reason}")
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
                sleeve_used_pln=0.0, today="", learn=None, wsreview=None, track=None,
                open_positions=None, ultra=None, scoreboard_card="") -> str:
    try:
        from email_render import (BG, DARK, GOLD, GOLD_DK, GREEN, GREEN_LT, RED, RED_LT,
                                  GREY, MUTED, MUTED2, TEXT, LINE, LINE2, SERIF, SANS,
                                  _card_open, _action_block)
    except Exception:
        return "<pre style='font-family:Courier New,monospace;font-size:12px'>" + \
               render_text(decisions, c, equity_pln, free_cash_pln, fx, held, sleeve_used_pln) + "</pre>"

    buys = [d for d in decisions if d.verdict == "BUY"]
    waits = [d for d in decisions if d.verdict == "CZEKAJ"]
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

    # BANER WYSOKIEGO PRZEKONANIA (stoicki — sygnał, nie nakaz; bez paniki, spokojny ton)
    if hot:
        P.append(
            f"<div style='background:{DARK};border-left:5px solid {GOLD};border-radius:12px;padding:16px 20px;margin-bottom:18px;"
            f"{SANS}font-size:13pt;color:#ffffff;'>"
            f"<b style='color:{GOLD};'>WYSOKIE PRZEKONANIE</b> — najwyższy score {best_score(buys):.1f}/10. "
            f"Spokojnie sprawdź poniżej. To <b>sygnał, nie nakaz</b> — decyzja Twoja, plan i Sell Stop gotowe.</div>"
        )

    # ★ ULTRA-PICK — najwyższe przekonanie (ramka z gwiazdkami; uczciwa, oparta na analizie historycznej)
    if ultra is not None:
        u = ultra
        gap = max(0.0, u.size_pln - (free_cash_pln if free_cash_pln is not None else 0.0))
        deposit = (f" <b>(dopłać {gap:,.0f} zł)</b>" if gap > 0 else "")
        price_line = (f"Wejście ~<b>${u.price_usd:.2f}</b> · Sell Stop <b>${u.stop_price_usd:.2f}</b> · "
                      f"Cel <b>${u.target_price_usd:.2f}</b> · ryzyko {u.risk_pln:,.0f} zł"
                      if u.price_usd else f"Stop −{u.stop_pct*100:.0f}% (brak ceny w sygnale)")
        srcs = " + ".join(u.kinds) if u.kinds else u.kind
        P.append(
            f"<div style='background:{DARK};border:2px solid {GOLD};border-radius:14px;padding:22px 24px;margin-bottom:18px;'>"
            f"<div style='{SANS}font-size:12pt;letter-spacing:3px;color:{GOLD};font-weight:bold;text-align:center;'>"
            f"★ ★ ★&nbsp; NAJWYŻSZE PRZEKONANIE &nbsp;★ ★ ★</div>"
            f"<div style='{SERIF}font-size:23pt;color:#ffffff;text-align:center;margin:8px 0 4px;font-weight:bold;'>{u.ticker}</div>"
            f"<div style='{SANS}font-size:11pt;color:{MUTED2};text-align:center;margin-bottom:14px;'>"
            f"score {u.score:.1f}/10 · konfluencja: {srcs}</div>"
            f"<div style='background:#0b1220;border-radius:10px;padding:14px 16px;{SANS}font-size:12pt;color:#e5e7eb;line-height:1.6;'>"
            f"<b style='color:{GOLD};'>Teza:</b> {u.note or 'Realny katalizator potwierdzony z wielu źródeł.'}<br>"
            f"<b style='color:{GREEN};'>→ Rozważ:</b> kup <b>{u.shares:.4f}</b> akc. za <b>{u.size_pln:,.0f} zł</b>{deposit} · {price_line}"
            f"</div>"
            f"<div style='{SANS}font-size:10.5pt;color:{MUTED2};line-height:1.6;margin-top:12px;'>"
            f"⚠ <b>To NIE pewniak</b> — najwyższe przekonanie przy kontrolowanym ryzyku. "
            f"Historia podobnych setupów: większość gwałtownych ruchów <b>oddaje</b> znaczną część w dni/tygodnie → "
            f"realizuj część zysku w sile i trzymaj twardy stop. "
            f"Sprawdź, czy kontrakt jest <b>FIRM/committed</b>, a nie tylko IDIQ ceiling (max ≠ przychód)."
            f"</div></div>"
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

    # TWOJE OTWARTE POZYCJE (ŁOWCA) — wyłącznie własne pozycje spekulacyjne, NIE rdzeń
    if open_positions:
        rows = ""
        for p in open_positions:
            e = f"${p['entry_usd']:.2f}" if p.get("entry_usd") else "—"
            s = f"${p['stop_usd']:.2f}" if p.get("stop_usd") else "—"
            t = f"${p['target_usd']:.2f}" if p.get("target_usd") else "—"
            rows += (f"<tr><td style='padding:10px 8px 10px 0;border-bottom:1px solid {LINE2};{SANS}font-size:12.5pt;'>"
                     f"<b style='color:{DARK};'>{p.get('ticker','')}</b> <span style='color:{MUTED};font-size:10pt;'>[{p.get('kind','')}]</span></td>"
                     f"<td style='padding:10px 8px;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{TEXT};'>{(p.get('shares') or 0):.4f} akc.</td>"
                     f"<td style='padding:10px 8px;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{MUTED};'>wejście {e}</td>"
                     f"<td style='padding:10px 8px;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{RED};'>Stop {s}</td>"
                     f"<td style='padding:10px 0;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:12pt;color:{GREEN};'>Cel {t}</td></tr>")
        P.append(_card_open(f"Twoje otwarte pozycje (łowca) — {len(open_positions)}",
                            "Pozycje spekulacyjne, którymi steruje łowca. Pełny rachunek (rdzeń + łowca) widzisz w mailu bilansu o 21:00.") +
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

    # CZEKAM na cofnięcie — złapane zagrywki (Trump/polityka/kontrakt), ale już za gorące
    if waits:
        rows = ""
        for d in waits[:6]:
            cat = (f"<span style='color:#166534;font-weight:bold;'>{d.catalyst}</span> · " if d.catalyst else "")
            rows += (f"<tr><td style='padding:10px 8px 10px 0;border-bottom:1px solid {LINE2};{SANS}font-size:12.5pt;white-space:nowrap;'>"
                     f"<b style='color:{DARK};'>{d.ticker}</b></td>"
                     f"<td style='padding:10px 0;border-bottom:1px solid {LINE2};color:{TEXT};{SANS}font-size:11.5pt;'>{cat}{d.reason}</td></tr>")
        P.append(_card_open("Czekam na cofnięcie 🇺🇸",
                            "Złapane zagrywki (Trump / polityka / kontrakt rządowy), ale już rozgrzane — wejście dopiero na korekcie, nie na górce.") +
                 f"<table style='width:100%;border-collapse:collapse;'>{rows}</table></div>")

    # PASS
    if passes:
        rows = ""
        for d in passes[:8]:
            rows += (f"<tr><td style='padding:10px 8px 10px 0;border-bottom:1px solid {LINE2};{SANS}font-size:12.5pt;'>"
                     f"<b style='color:{DARK};'>{d.ticker}</b> <span style='color:{MUTED};font-size:11pt;'>[{d.kind}]</span></td>"
                     f"<td style='padding:10px 0;border-bottom:1px solid {LINE2};color:{MUTED};{SANS}font-size:11.5pt;'>{d.reason}</td></tr>")
        P.append(_card_open("Pominięte (PASS)", "Okazje, które dziś nie kwalifikują się do zakupu.") +
                 f"<table style='width:100%;border-collapse:collapse;'>{rows}</table></div>")

    # DZIENNIK DECYZJI — forward-test track record (rekomendacje łowcy w czasie)
    if track:
        recent = list(reversed(track))[:8]            # najnowsze u góry
        th = f"{SANS}font-size:9.5pt;text-transform:uppercase;letter-spacing:.6px;color:{MUTED};padding:0 8px 6px 0;font-weight:bold;"
        td = f"padding:9px 8px 9px 0;border-bottom:1px solid {LINE2};{SANS}font-size:11pt;white-space:nowrap;"
        rows_t = (f"<tr><td style='{th}'>Data</td><td style='{th}'>Spółka</td>"
                  f"<td style='{th}text-align:right;'>Wejście</td><td style='{th}text-align:right;'>Stop</td>"
                  f"<td style='{th}text-align:right;'>Cel</td><td style='{th}text-align:right;'>Score</td></tr>")
        for r in recent:
            e = f"${r['entry_usd']:.2f}" if r.get("entry_usd") else "—"
            s = f"${r['stop_usd']:.2f}" if r.get("stop_usd") else "—"
            t = f"${r['target_usd']:.2f}" if r.get("target_usd") else "—"
            rows_t += (f"<tr><td style='{td}color:{MUTED};'>{r.get('date','')}</td>"
                       f"<td style='{td}'><b style='color:{DARK};'>{r.get('ticker','')}</b> "
                       f"<span style='color:{MUTED};font-size:10pt;'>[{r.get('kind','')}]</span></td>"
                       f"<td style='{td}text-align:right;color:{TEXT};'>{e}</td>"
                       f"<td style='{td}text-align:right;color:{RED};'>{s}</td>"
                       f"<td style='{td}text-align:right;color:{GREEN};'>{t}</td>"
                       f"<td style='{td}text-align:right;color:{DARK};font-weight:bold;'>{r.get('score','')}</td></tr>")
        more = (f"Pokazane {len(recent)} z {len(track)}. " if len(track) > len(recent) else "")
        P.append(_card_open(f"Dziennik decyzji łowcy — {len(track)} rekomendacji",
                            "Track record na żywo: każda decyzja KUP zapisana, by uczciwie zmierzyć skuteczność.") +
                 f"<table style='width:100%;border-collapse:collapse;'>{rows_t}</table>"
                 f"<div style='{SANS}font-size:10pt;color:{MUTED};margin-top:10px;'>{more}"
                 f"Wynik (WIN/LOSS) dojdzie po T+30/+90 dni — wtedy zmierzymy realny edge vs SPY.</div></div>")

    # TABLICA WYNIKÓW (scoreboard) — per-typ-sygnału zwrot + alfa vs SPY
    if scoreboard_card:
        P.append(scoreboard_card)

    # ZASADY / STOPKA
    # NAUKA — pominięcia + dziennik lekcji
    if learn and lmiss:
        try:
            P.append(lmiss.render_lessons_html(learn))
        except Exception:
            pass
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
    candidates = build_candidates(radar, smart, _price_map(signals), _meta_map(signals))
    decisions = decide_all(candidates, c, free_cash_pln=cash, held=held_tickers, fx=fx,
                           sleeve_used_pln=sleeve_used, open_spec=open_spec)
    # FORWARD-TEST: zapisz dzisiejsze rekomendacje do dziennika (append-only) — fundament pomiaru edge na żywo
    track = None
    spy_now = sb.fetch_spy_yf() if sb else None   # benchmark do ALFY (stempel wejścia + scoring scoreboardu)
    if today:
        try:
            res_log = log_decisions(decisions, today, spy_usd=spy_now)
            track = _load_decisions_log()
            print(f"Dziennik decyzji: +{res_log['added']} (łącznie {res_log['total']}).")
        except Exception as e:
            print(f"Dziennik decyzji pominięty: {e}")
    # PĘTLA UCZENIA: zapisz dziś rozważane, znajdź wczorajsze pominięcia (lowca_movers.json), dopisz lekcje
    learn = None
    if lmiss and today:
        try:
            learn = lmiss.process(today, [str(cc.get("ticker")) for cc in candidates])
        except Exception as e:
            print(f"Nauka pominieta: {e}")
    # POZYCJE ŁOWCY (własne, status OPEN) + ULTRA-PICK (najwyższe przekonanie)
    open_positions = [r for r in (track or []) if str(r.get("status", "OPEN")).upper() == "OPEN"]
    ultra = select_ultra_pick(decisions, c)
    if ultra is not None:
        print(f"ULTRA-PICK: {ultra.ticker} (score {ultra.score:.1f}, konfluencja {ultra.confluence}).")
    print(render_text(decisions, c, equity_pln=capital, free_cash_pln=cash, fx=fx, held=held,
                      sleeve_used_pln=sleeve_used, learn=learn))

    buys = [d for d in decisions if d.verdict == "BUY"]
    hot = best_score(buys) >= c.alert_threshold
    if alert_only and not hot:
        print(f"[alert-only] brak okazji score>={c.alert_threshold:.0f} — mail POMINIETY (cicho).")
        return 0
    if send:
        try:
            from notifications import send_email_resend
            sb_card = sb.refresh_card(today=today, spy_now=spy_now, held=held_tickers) if (sb and today) else ""
            html = render_html(decisions, c, equity_pln=capital, free_cash_pln=cash, fx=fx,
                               held=held, sleeve_used_pln=sleeve_used, today=today, learn=learn,
                               track=track, open_positions=open_positions, ultra=ultra,
                               scoreboard_card=sb_card)
            subj = (f"Bot Łowca — mocny sygnał (score {best_score(buys):.1f})" if hot
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
        my_pos = [{"ticker": "ZZZ", "kind": "KONTRAKT", "entry_usd": 10.0, "stop_usd": 8.0,
                   "target_usd": 14.0, "shares": 2.0, "size_pln": 50.0, "status": "OPEN"}]
        ultra = select_ultra_pick(decb, c)
        html = render_html(decb, c, equity_pln=1648, free_cash_pln=200.0, fx=3.64, held=held,
                           today="2026-06-04", open_positions=my_pos, ultra=ultra)
        ok("render_html bez bledu", True)
        ok("HTML: baner WYSOKIE PRZEKONANIE przy score>=8", "WYSOKIE PRZEKONANIE" in html)
        ok("HTML: badge KONFLUENCJA", "KONFLUENCJA" in html)
        ok("HTML: konkretny Sell Stop $", "Sell Stop $" in html)
        ok("ULTRA-PICK wybrany (score>=8 i konfluencja>=2)", ultra is not None and ultra.ticker == "ZZZ")
        ok("HTML: ramka NAJWYZSZE PRZEKONANIE", "NAJWYŻSZE PRZEKONANIE" in html)
        ok("HTML: wlasne pozycje lowcy, NIE rdzen", ("otwarte pozycje (łowca)" in html) and ("obecne pozycje (rdzeń" not in html))
    except Exception as e:
        ok(f"render_html bez bledu ({e})", False)
    try:
        render_text(decb, c, equity_pln=1648, free_cash_pln=200.0, fx=3.64, held=held); ok("render_text bez bledu", True)
    except Exception as e:
        ok(f"render_text bez bledu ({e})", False)

    # ── DZIENNIK DECYZJI (forward-test log): append-only + idempotentny + karta w mailu ──
    test_log = "lowca_decisions_log_TEST.json"
    try:
        if os.path.exists(test_log):
            os.remove(test_log)
        r1 = log_decisions(decb, "2026-06-04", path=test_log)
        ok("Dziennik: dopisal decyzje BUY", r1["added"] >= 1)
        r2 = log_decisions(decb, "2026-06-04", path=test_log)   # ten sam dzien -> brak duplikatow
        ok("Dziennik: idempotentny (id=data-ticker)", r2["added"] == 0 and r2["total"] == r1["total"])
        loaded = _load_decisions_log(test_log)
        ok("Dziennik: rekord ma id+date+entry+status", bool(loaded) and all(
            ("id" in x and "date" in x and "status" in x) for x in loaded))
        html_t = render_html(decb, c, equity_pln=1648, free_cash_pln=200.0, fx=3.64,
                             held=held, today="2026-06-04", track=loaded)
        ok("HTML: karta Dziennik decyzji obecna", "Dziennik decyzji" in html_t)
        ok("Dziennik: brak crasha gdy brak BUY/daty", log_decisions([], "", path=test_log)["added"] == 0)
    except Exception as e:
        ok(f"Dziennik decyzji bez bledu ({e})", False)
    finally:
        if os.path.exists(test_log):
            os.remove(test_log)

    # ── SKANER POLITYKA/TRUMP (tag katalizatora) + BEZPIECZNIK "NIE KUPUJ GORKI" ──
    hot_sig = {"contract": [{"ticker": "DRON", "contract_usd": 600e6, "market_cap_usd": 700e6, "hours_ago": 2,
                             "price_usd": 20.0, "pct_1m": 85.0, "catalyst": "Trump/DoD kontrakt"}]}
    mm = _meta_map(hot_sig)
    ok("Meta: pct_1m zlapany (+85%)", abs(mm.get("DRON", {}).get("pct_1m", 0) - 85.0) < 0.1)
    ok("Meta: catalyst (Trump) zlapany", "Trump" in mm.get("DRON", {}).get("catalyst", ""))
    pc = build_candidates(oppl.build_radar(hot_sig), {"all": []}, _price_map(hot_sig), mm)
    dron = next((m for m in pc if m["ticker"] == "DRON"), None)
    ok("Kandydat ma badge polityczny (flaga US)", dron and "\U0001F1FA\U0001F1F8" in dron.get("note", ""))
    ok("Kandydat niesie pct_1m=85", dron and abs(dron.get("pct_1m", 0) - 85.0) < 0.1)
    pdec = decide_all(pc, c, free_cash_pln=300.0, fx=3.64)
    dron_d = next((d for d in pdec if d.ticker == "DRON"), None)
    ok("BEZPIECZNIK: +85% w miesiac -> CZEKAJ (nie kupuj gorki)", dron_d and dron_d.verdict == "CZEKAJ")
    cool_sig = {"contract": [{"ticker": "DRON", "contract_usd": 600e6, "market_cap_usd": 700e6, "hours_ago": 2,
                              "price_usd": 20.0, "pct_1m": 5.0}]}
    cdec = decide_all(build_candidates(oppl.build_radar(cool_sig), {"all": []}, _price_map(cool_sig), _meta_map(cool_sig)),
                      c, free_cash_pln=300.0, fx=3.64)
    cool_d = next((d for d in cdec if d.ticker == "DRON"), None)
    ok("Niewystrzelona (+5%) NIE blokowana przez bezpiecznik", cool_d and cool_d.verdict != "CZEKAJ")

    # ── ASYMETRIA R:R / EV ── presuj najlepsze, blokuj ujemny EV (test bezpośredni decide_all)
    ev_neg = decide_all([{"ticker": "EVN", "kind": "KONTRAKT", "score": 8.0, "confluence": 2,
                          "price_usd": 20.0, "ev_pct": -10.0}], c, free_cash_pln=300.0, fx=3.64)
    ok("EV ujemny -> CZEKAJ (brak asymetrii)", ev_neg[0].verdict == "CZEKAJ")
    rr_ipo = decide_all([{"ticker": "RRP", "kind": "IPO", "score": 8.0, "confluence": 2,
                          "price_usd": 20.0}], c, free_cash_pln=300.0, fx=3.64)
    ok("IPO R:R 2.4 -> BUY pełna (rr>=2.2)", rr_ipo[0].verdict == "BUY" and rr_ipo[0].rr >= 2.2)
    rr_kon = decide_all([{"ticker": "KKN", "kind": "KONTRAKT", "score": 8.0, "confluence": 2,
                          "price_usd": 20.0}], c, free_cash_pln=300.0, fx=3.64)
    ok("KONTRAKT R:R 2.0 -> BUY mniejsza (rr<2.2)", rr_kon[0].verdict == "BUY" and abs(rr_kon[0].rr - 2.0) < 0.01)
    full_kon = _clamp(c.risk_per_trade_pct * c.capital_pln / _stop_pct("KONTRAKT", c), c.min_position_pln, c.max_position_pln)
    ok("Asymetria: KONTRAKT (rr<2.2) dostaje mniejszą pozycję niż pełny risk-parity (×0.85)", rr_kon[0].size_pln < round(full_kon, 0))
    try:
        ok("HTML: karta Czekam na cofniecie", "Czekam na cofnięcie" in
           render_html(pdec, c, equity_pln=1648, free_cash_pln=300.0, fx=3.64, today="2026-06-05"))
    except Exception as e:
        ok(f"HTML czekam ({e})", False)

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
