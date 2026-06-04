# -*- coding: utf-8 -*-
"""
wall_street.py — INSTYTUCJONALNY NADZÓR PORTFELA (zasady Wall Street).
Osobny plik. Czysto obliczeniowy, testowalny. NIE rusza botów — dostarcza
AUDYT + konkretne AKCJE rebalansu (które wykonujesz ręcznie na XTB).

Zasady (twarde limity zarządzania ryzykiem, jak u profesjonalistów):
- Pojedyncza spółka:        max 25% equity   (concentration limit)
- Sektor:                   max 35% equity
- Klaster skorelowany:      max 45% (np. wszystkie półprzewodniki = jeden zakład)
- Rezerwa gotówki:          min 15% equity   (finansuje sleeve spekulacyjny; dry powder)
- Portfolio heat:           max 6% equity pod ryzykiem (suma dystansów do stopów)
- Sizing wg RYZYKA:         1.5% equity na nową pozycję (value = risk_budget / dystans_do_stopu)

Wejście: pozycje [{ticker, shares, entry_usd, stop_usd}], gotówka PLN, equity PLN, kurs.
Wyjście: review() -> dict z naruszeniami + akcjami; render_html() -> karta do maila.
"""
from __future__ import annotations


# ── KONFIG (twarde limity) ──
SINGLE_NAME_CAP = 0.25
SECTOR_CAP = 0.35
CLUSTER_CAP = 0.45
CASH_RESERVE = 0.15
MAX_PORTFOLIO_HEAT = 0.06
RISK_PER_TRADE = 0.015

# Mapa sektorów (wbudowana dla popularnych tickerów; reszta -> "Inne").
SECTOR_MAP = {
    "ASML": "Półprzewodniki", "TSM": "Półprzewodniki", "NVDA": "Półprzewodniki",
    "AMD": "Półprzewodniki", "AVGO": "Półprzewodniki", "MU": "Półprzewodniki",
    "INTC": "Półprzewodniki", "ARM": "Półprzewodniki", "SMCI": "Półprzewodniki",
    "TXN": "Półprzewodniki", "QCOM": "Półprzewodniki", "LRCX": "Półprzewodniki", "KLAC": "Półprzewodniki",
    "SAP": "Software", "MSFT": "Software", "ORCL": "Software", "CRM": "Software",
    "ADBE": "Software", "NOW": "Software", "PLTR": "Software", "SNOW": "Software", "CRWD": "Software",
    "AAPL": "Hardware/Konsument", "GOOGL": "Internet", "META": "Internet", "AMZN": "Internet", "NFLX": "Internet",
    "TSLA": "Auto/EV", "JPM": "Banki", "BAC": "Banki", "GS": "Banki", "V": "Płatności", "MA": "Płatności",
    "LLY": "Pharma", "NVO": "Pharma", "UNH": "Zdrowie", "XOM": "Energia", "CVX": "Energia",
    "CRWV": "AI/Infra", "CRCL": "Krypto/Fintech", "COIN": "Krypto/Fintech",
}

# Klastry skorelowane (traktowane jak jeden zakład przy limicie 45%).
CLUSTERS = {
    "Półprzewodniki + AI-Infra": {"Półprzewodniki", "AI/Infra"},
}


def sector_of(ticker, sector_map=None):
    m = sector_map or SECTOR_MAP
    return m.get(str(ticker).upper(), "Inne")


def _pos_value_pln(p, fx):
    """Wartość pozycji w PLN wg ceny wejścia (stabilna do audytu alokacji)."""
    return float(p.get("shares", 0) or 0) * float(p.get("entry_usd", 0) or 0) * fx


def _pos_risk_pln(p, fx):
    """Ryzyko w PLN = ile tracisz od wejścia do stopu (shares*(entry-stop)*fx)."""
    entry = float(p.get("entry_usd", 0) or 0)
    stop = float(p.get("stop_usd", 0) or 0)
    sh = float(p.get("shares", 0) or 0)
    if entry <= 0 or stop <= 0 or stop >= entry:
        return 0.0
    return sh * (entry - stop) * fx


def risk_based_value_pln(equity_pln, entry_usd, stop_usd, risk_per_trade=RISK_PER_TRADE):
    """SIZING WG RYZYKA (Wall Street): wielkość pozycji tak, by strata na stopie = risk_per_trade*equity.
    value = (risk_budget) / (dystans_do_stopu). Ciasny stop -> większa pozycja; szeroki -> mniejsza."""
    if entry_usd <= 0 or stop_usd <= 0 or stop_usd >= entry_usd or equity_pln <= 0:
        return 0.0
    stop_dist = (entry_usd - stop_usd) / entry_usd
    risk_budget = equity_pln * risk_per_trade
    return round(risk_budget / stop_dist, 0)


def sector_exposure(positions, equity_pln, fx, sector_map=None):
    out = {}
    for p in (positions or []):
        s = sector_of(p.get("ticker", "?"), sector_map)
        out[s] = out.get(s, 0.0) + _pos_value_pln(p, fx)
    return {s: {"pln": round(v, 0), "pct": (v / equity_pln if equity_pln else 0)} for s, v in out.items()}


def cluster_exposure(sector_exp, equity_pln):
    """Łączna ekspozycja klastrów skorelowanych."""
    out = {}
    for name, sects in CLUSTERS.items():
        pln = sum(sector_exp.get(s, {}).get("pln", 0) for s in sects)
        if pln > 0:
            out[name] = {"pln": round(pln, 0), "pct": (pln / equity_pln if equity_pln else 0)}
    return out


def portfolio_heat(positions, equity_pln, fx):
    """Suma ryzyka wszystkich pozycji / equity (ile % kapitału pod ryzykiem na stopach)."""
    total = sum(_pos_risk_pln(p, fx) for p in (positions or []))
    return {"risk_pln": round(total, 0), "pct": (total / equity_pln if equity_pln else 0)}


def review(positions, cash_pln, equity_pln, fx, sector_map=None):
    """Pełny audyt instytucjonalny + konkretne akcje rebalansu."""
    positions = positions or []
    eq = equity_pln if equity_pln and equity_pln > 0 else 1.0
    names = []
    for p in positions:
        v = _pos_value_pln(p, fx)
        names.append({"ticker": str(p.get("ticker", "?")).upper(), "pln": round(v, 0), "pct": v / eq})
    names.sort(key=lambda x: x["pct"], reverse=True)
    sec = sector_exposure(positions, eq, fx, sector_map)
    clusters = cluster_exposure(sec, eq)
    heat = portfolio_heat(positions, eq, fx)
    cash_pct = (cash_pln / eq) if eq else 0
    reserve_gap_pln = max(0.0, CASH_RESERVE * eq - cash_pln)

    actions = []
    breaches = 0

    # 1. Rezerwa gotówki (finansuje sleeve)
    if cash_pct < CASH_RESERVE:
        breaches += 1
        actions.append(("rezerwa", f"Gotówka {cash_pct*100:.0f}% < wymagane {CASH_RESERVE*100:.0f}%. "
                        f"Podnieś gotówkę o ~{reserve_gap_pln:.0f} zł (sprzedaj część największej pozycji), "
                        f"żeby sfinansować sleeve spekulacyjny ({CASH_RESERVE*eq:.0f} zł dry powder)."))

    # 2. Limit sektorowy
    for s, d in sorted(sec.items(), key=lambda kv: kv[1]["pct"], reverse=True):
        if s != "Inne" and d["pct"] > SECTOR_CAP:
            breaches += 1
            trim = (d["pct"] - SECTOR_CAP) * eq
            actions.append(("sektor", f"Sektor {s} = {d['pct']*100:.0f}% > limit {SECTOR_CAP*100:.0f}%. "
                            f"Przytnij ~{trim:.0f} zł w tym sektorze (zmniejsz najmocniej skorelowaną pozycję)."))

    # 3. Klaster skorelowany
    for name, d in clusters.items():
        if d["pct"] > CLUSTER_CAP:
            breaches += 1
            trim = (d["pct"] - CLUSTER_CAP) * eq
            actions.append(("klaster", f"Klaster {name}: {d['pct']*100:.0f}% > {CLUSTER_CAP*100:.0f}% "
                            f"(skorelowane = jeden zakład). Przytnij ~{trim:.0f} zł."))

    # 4. Limit pojedynczej spółki
    for n in names:
        if n["pct"] > SINGLE_NAME_CAP:
            breaches += 1
            trim = (n["pct"] - SINGLE_NAME_CAP) * eq
            actions.append(("spółka", f"{n['ticker']} = {n['pct']*100:.0f}% > limit {SINGLE_NAME_CAP*100:.0f}%. "
                            f"Przytnij ~{trim:.0f} zł do {SINGLE_NAME_CAP*100:.0f}%."))

    # 5. Portfolio heat
    if heat["pct"] > MAX_PORTFOLIO_HEAT:
        breaches += 1
        actions.append(("heat", f"Łączne ryzyko (heat) {heat['pct']*100:.1f}% > {MAX_PORTFOLIO_HEAT*100:.0f}%. "
                        f"Zacieśnij stopy lub zmniejsz pozycje."))

    if not actions:
        actions.append(("ok", "Portfel w ramach wszystkich limitów Wall Street. Trzymaj kurs."))

    grade = "A" if breaches == 0 else ("B" if breaches <= 2 else "C")
    return {
        "names": names, "sectors": sec, "clusters": clusters, "heat": heat,
        "cash_pct": cash_pct, "reserve_gap_pln": round(reserve_gap_pln, 0),
        "reserve_ok": cash_pct >= CASH_RESERVE, "heat_ok": heat["pct"] <= MAX_PORTFOLIO_HEAT,
        "breaches": breaches, "grade": grade, "actions": actions, "equity_pln": round(eq, 0),
    }


def render_html(rv: dict) -> str:
    """Karta 'Audyt Wall Street' do maila (styl bilansu; fallback prosty)."""
    try:
        from email_render import (DARK, GREEN, RED, GOLD_DK, MUTED, TEXT, LINE2, SANS, _card_open)
    except Exception:
        DARK = "#0f172a"; TEXT = "#334155"; MUTED = "#64748b"; RED = "#b91c1c"; GREEN = "#15803d"
        GOLD_DK = "#b8860b"; LINE2 = "#eef1f5"
        SANS = "font-family:-apple-system,Arial,Helvetica,sans-serif;"
        def _card_open(t, s="", **k): return f"<div style='background:#fff;border-radius:14px;padding:24px 26px;margin-bottom:18px;border-top:5px solid #d4af37;'><div style='{SANS}font-weight:bold;text-transform:uppercase;color:{MUTED};margin-bottom:8px;'>{t}</div><div style='{SANS}color:{MUTED};margin-bottom:12px;'>{s}</div>"
    g = rv.get("grade", "?")
    gcol = GREEN if g == "A" else (GOLD_DK if g == "B" else RED)
    H = [_card_open("Audyt Wall Street — portfel rdzenia",
                   "Instytucjonalne limity ryzyka i konkretne akcje rebalansu (wykonujesz ręcznie na XTB).")]
    H.append(f"<div style='{SANS}font-size:13pt;margin-bottom:12px;'>Ocena portfela: "
             f"<b style='color:{gcol};font-size:18pt;'>{g}</b> "
             f"<span style='color:{MUTED};'>({rv.get('breaches',0)} naruszeń limitów)</span></div>")
    # metryki
    heat = rv.get("heat", {}); cash_pct = rv.get("cash_pct", 0)
    rcol = GREEN if rv.get("reserve_ok") else RED
    hcol = GREEN if rv.get("heat_ok") else RED
    H.append(f"<table style='width:100%;border-collapse:collapse;{SANS}font-size:12pt;color:{TEXT};margin-bottom:14px;'>"
             f"<tr><td style='padding:6px 0;'>Rezerwa gotówki (cel ≥15%)</td>"
             f"<td style='padding:6px 0;text-align:right;font-weight:bold;color:{rcol};'>{cash_pct*100:.0f}%</td></tr>"
             f"<tr><td style='padding:6px 0;border-top:1px solid {LINE2};'>Portfolio heat (cel ≤6%)</td>"
             f"<td style='padding:6px 0;text-align:right;border-top:1px solid {LINE2};font-weight:bold;color:{hcol};'>{heat.get('pct',0)*100:.1f}% ({heat.get('risk_pln',0):.0f} zł pod ryzykiem)</td></tr>"
             f"</table>")
    # sektory
    sec = rv.get("sectors", {})
    if sec:
        rows = ""
        for s, d in sorted(sec.items(), key=lambda kv: kv[1]["pct"], reverse=True):
            over = d["pct"] > SECTOR_CAP and s != "Inne"
            col = RED if over else TEXT
            rows += (f"<tr><td style='padding:6px 8px 6px 0;border-bottom:1px solid {LINE2};{SANS}font-size:11.5pt;color:{col};'>{s}{' ⚠' if over else ''}</td>"
                     f"<td style='padding:6px 0;border-bottom:1px solid {LINE2};text-align:right;{SANS}font-size:11.5pt;color:{col};font-weight:bold;'>{d['pct']*100:.0f}% ({d['pln']:.0f} zł)</td></tr>")
        H.append(f"<div style='{SANS}font-size:10.5pt;text-transform:uppercase;letter-spacing:1px;color:{MUTED};font-weight:bold;margin-bottom:6px;'>Ekspozycja sektorowa (limit 35%):</div>"
                 f"<table style='width:100%;border-collapse:collapse;margin-bottom:14px;'>{rows}</table>")
    # akcje
    acts = rv.get("actions", [])
    H.append(f"<div style='{SANS}font-size:10.5pt;text-transform:uppercase;letter-spacing:1px;color:{MUTED};font-weight:bold;margin-bottom:6px;'>Akcje rebalansu:</div>")
    for tag, txt in acts:
        bar = GREEN if tag == "ok" else GOLD_DK
        H.append(f"<div style='{SANS}font-size:12pt;color:{TEXT};line-height:1.55;border-left:3px solid {bar};"
                 f"padding:8px 12px;margin-bottom:6px;background:#f8fafc;border-radius:6px;'>{txt}</div>")
    H.append("</div>")
    return "".join(H)


# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=== SELFTEST wall_street (instytucjonalny nadzór) ===")
    P = F = 0
    def ok(n, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")

    # sizing wg ryzyka: 1.5% z 1648 = 24.7 zł; stop -8% -> 309 zł; stop -4% -> 618 zł
    v8 = risk_based_value_pln(1648, 100.0, 92.0)   # stop -8%
    v4 = risk_based_value_pln(1648, 100.0, 96.0)   # stop -4%
    ok("Sizing wg ryzyka: ciasny stop -> większa pozycja", v4 > v8)
    ok("Sizing -8%: ~309 zł", abs(v8 - 309) < 5)
    ok("Sizing -4%: ~618 zł", abs(v4 - 618) < 5)
    ok("Sizing: zły stop -> 0", risk_based_value_pln(1648, 100, 100) == 0)

    # realny portfel: SAP/ASML/TSM (z danych użytkownika)
    fx = 3.64; eq = 1678.0
    pos = [
        {"ticker": "SAP", "shares": 0.799, "entry_usd": 176.37, "stop_usd": 176.37},
        {"ticker": "ASML", "shares": 0.0851, "entry_usd": 1610.28, "stop_usd": 1578.07},
        {"ticker": "TSM", "shares": 0.3319, "entry_usd": 423.68, "stop_usd": 415.21},
    ]
    sec = sector_exposure(pos, eq, fx)
    ok("Sektor: półprzewodniki wykryte (ASML+TSM)", "Półprzewodniki" in sec)
    ok("Sektor: półprzewodniki ~60%", sec["Półprzewodniki"]["pct"] > 0.5)
    heat = portfolio_heat(pos, eq, fx)
    ok("Heat liczony (małe ryzyko bo ciasne stopy)", 0 <= heat["pct"] < 0.05)

    rv = review(pos, cash_pln=55.27, equity_pln=eq, fx=fx)
    ok("review: ocena nie A (są naruszenia)", rv["grade"] != "A")
    ok("review: akcja o rezerwie gotówki", any(t == "rezerwa" for t, _ in rv["actions"]))
    ok("review: akcja o sektorze (półprzewodniki >35%)", any(t in ("sektor", "klaster") for t, _ in rv["actions"]))
    ok("review: reserve_ok = False (gotówka 3%)", rv["reserve_ok"] is False)
    ok("review: heat_ok = True (ciasne stopy)", rv["heat_ok"] is True)

    # zdrowy portfel -> A, brak akcji poza 'ok'
    healthy = [
        {"ticker": "MSFT", "shares": 1, "entry_usd": 100, "stop_usd": 95},
        {"ticker": "LLY", "shares": 1, "entry_usd": 100, "stop_usd": 95},
        {"ticker": "JPM", "shares": 1, "entry_usd": 100, "stop_usd": 95},
        {"ticker": "XOM", "shares": 1, "entry_usd": 100, "stop_usd": 95},
    ]  # 4 sektory po 25%, equity tak by każda ~20%
    rvh = review(healthy, cash_pln=400, equity_pln=2000, fx=1.0)
    ok("Zdrowy portfel: ocena A", rvh["grade"] == "A")
    ok("Zdrowy portfel: tylko akcja 'ok'", any(t == "ok" for t, _ in rvh["actions"]))

    # render bez błędu
    try:
        html = render_html(rv)
        ok("render_html bez błędu", True)
        ok("HTML: Audyt Wall Street", "Audyt Wall Street" in html)
        ok("HTML: ocena + sektory", "Ocena portfela" in html and "Ekspozycja sektorowa" in html)
    except Exception as e:
        ok(f"render_html bez błędu ({e})", False)

    # odporność: puste / None
    ok("Pusty portfel -> brak crasha", isinstance(review([], 100, 100, 4.0), dict))
    ok("None positions -> brak crasha", isinstance(review(None, 100, 100, 4.0), dict))

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== wall_street.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_run_selftest() if "--selftest" in sys.argv else 0)
