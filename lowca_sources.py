# -*- coding: utf-8 -*-
"""
lowca_sources.py — DODATKOWE ŹRÓDŁA OKAZJI dla Bota Łowcy (smart money).
Osobny plik (lowca_*), NIE rusza opportunity_lens.py (współdzielonego z bilansem).

Ocenia surowe sygnały smart-money na okazje:
- INSIDER  — klastrowe zakupy insiderów (OpenInsider): wielu kupujących, duża kwota, świeżo.
- FUND13F  — znaczący ruch funduszu (Dataroma/13F): nowa/zwiększona pozycja, % portfela.
- CONGRESS — zakup kongresmena (Capitol Trades/Quiver): świeży, komisja powiązana.

Zwraca dicty zgodne z opportunity_lens (ticker/kind/note/risk/label/price_usd/base_score),
żeby lowca_pipeline mógł je scalić z radarem lensa i policzyć KONFLUENCJĘ
(ta sama spółka w wielu źródłach = mocniejszy sygnał).
"""
from __future__ import annotations


def _num(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def evaluate_insider(s: dict):
    """Klaster zakupów insiderów. Mocniej: >=3 kupujących, >=1M USD, świeżo (<=14 dni)."""
    if not isinstance(s, dict) or not s.get("ticker"):
        return None
    buyers = int(_num(s.get("buyers", 1)))
    usd = _num(s.get("usd_total", 0))
    days = _num(s.get("days_ago", 99))
    if days > 14 or buyers < 1 or usd < 100_000:
        return None  # za stare / za słabe
    score = 6.0
    if buyers >= 3:
        score += 0.7
    elif buyers >= 2:
        score += 0.3
    if usd >= 1_000_000:
        score += 0.6
    elif usd >= 300_000:
        score += 0.3
    if days <= 3:
        score += 0.2
    note = f"Insider: {buyers} kupujących, ${usd/1e6:.1f}M, {int(days)}d temu"
    if s.get("role"):
        note += f" ({s['role']})"
    return {"ticker": str(s["ticker"]).upper(), "kind": "INSIDER", "note": note,
            "risk": s.get("risk", "WYSOKIE"), "label": "KUP",
            "price_usd": _num(s.get("price_usd", 0)), "base_score": round(min(score, 9.0), 2)}


def evaluate_13f(s: dict):
    """Ruch znanego funduszu (13F). Mocniej: NEW + duży % portfela."""
    if not isinstance(s, dict) or not s.get("ticker"):
        return None
    pct = _num(s.get("pct_portfolio", 0))
    action = str(s.get("action", "")).upper()
    score = 6.0
    if action == "NEW":
        score += 0.5
    elif action in ("ADD", "INCREASE"):
        score += 0.3
    if pct >= 10:
        score += 0.5
    elif pct >= 5:
        score += 0.3
    note = f"13F: {s.get('fund','fundusz')} {action or 'pozycja'}"
    if pct:
        note += f", {pct:.0f}% portfela"
    return {"ticker": str(s["ticker"]).upper(), "kind": "FUND13F", "note": note,
            "risk": s.get("risk", "WYSOKIE"), "label": "OBSERWUJ",
            "price_usd": _num(s.get("price_usd", 0)), "base_score": round(min(score, 8.5), 2)}


def evaluate_congress(s: dict):
    """Zakup kongresmena. Mocniej: powiązana komisja + duża kwota, świeżo (<=45 dni)."""
    if not isinstance(s, dict) or not s.get("ticker"):
        return None
    days = _num(s.get("days_ago", 99))
    amt = _num(s.get("amount_usd", 0))
    if days > 45:
        return None  # raporty kongresu bywają opóźnione, ale >45d za stare
    score = 6.0
    if s.get("committee_relevant"):
        score += 0.5
    if amt >= 250_000:
        score += 0.4
    elif amt >= 50_000:
        score += 0.2
    note = f"Kongres: {s.get('member','członek')} kupił"
    if amt:
        note += f" ~${amt/1e3:.0f}k"
    note += f", {int(days)}d temu"
    return {"ticker": str(s["ticker"]).upper(), "kind": "CONGRESS", "note": note,
            "risk": s.get("risk", "WYSOKIE"), "label": "OBSERWUJ",
            "price_usd": _num(s.get("price_usd", 0)), "base_score": round(min(score, 8.0), 2)}


def build_smart(signals: dict) -> dict:
    """Z surowych sygnałów -> {insider, fund13f, congress, all}. Odporne na None/śmieci."""
    out = {"insider": [], "fund13f": [], "congress": [], "all": []}
    if not isinstance(signals, dict):
        return out
    for s in (signals.get("insider") or []):
        o = evaluate_insider(s)
        if o:
            out["insider"].append(o)
    for s in (signals.get("fund13f") or []):
        o = evaluate_13f(s)
        if o:
            out["fund13f"].append(o)
    for s in (signals.get("congress") or []):
        o = evaluate_congress(s)
        if o:
            out["congress"].append(o)
    out["all"] = out["insider"] + out["fund13f"] + out["congress"]
    return out


# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=== SELFTEST lowca_sources (smart money) ===")
    P = F = 0
    def ok(n, cond):
        nonlocal P, F
        if cond: P += 1; print(f"  [OK] {n}")
        else: F += 1; print(f"  [FAIL] {n}")

    # insider mocny
    i = evaluate_insider({"ticker": "abcd", "buyers": 4, "usd_total": 2_000_000, "days_ago": 2, "price_usd": 12.0, "role": "CEO"})
    ok("Insider mocny -> okazja", i and i["kind"] == "INSIDER")
    ok("Insider: ticker upper", i and i["ticker"] == "ABCD")
    ok("Insider mocny -> score >= 7", i and i["base_score"] >= 7.0)
    ok("Insider niesie price_usd", i and i["price_usd"] == 12.0)
    # insider za stary/słaby -> None
    ok("Insider stary -> None", evaluate_insider({"ticker": "X", "buyers": 1, "usd_total": 2e6, "days_ago": 40}) is None)
    ok("Insider mała kwota -> None", evaluate_insider({"ticker": "X", "buyers": 1, "usd_total": 50_000, "days_ago": 1}) is None)
    ok("Insider bez tickera -> None", evaluate_insider({"buyers": 3, "usd_total": 2e6, "days_ago": 1}) is None)

    # 13F
    f = evaluate_13f({"ticker": "GH", "fund": "Scion", "action": "NEW", "pct_portfolio": 15, "price_usd": 30})
    ok("13F NEW duży % -> score >= 6.8", f and f["base_score"] >= 6.8)
    ok("13F kind", f and f["kind"] == "FUND13F")

    # congress
    c = evaluate_congress({"ticker": "IJ", "member": "X", "amount_usd": 300_000, "days_ago": 10, "committee_relevant": True, "price_usd": 5})
    ok("Congress mocny -> score >= 6.8", c and c["base_score"] >= 6.8)
    ok("Congress stary -> None", evaluate_congress({"ticker": "IJ", "amount_usd": 1e6, "days_ago": 60}) is None)

    # build_smart
    sig = {
        "insider": [{"ticker": "AAA", "buyers": 3, "usd_total": 1_500_000, "days_ago": 1, "price_usd": 10}],
        "fund13f": [{"ticker": "BBB", "fund": "Burry", "action": "NEW", "pct_portfolio": 8, "price_usd": 20}],
        "congress": [{"ticker": "AAA", "member": "Y", "amount_usd": 100_000, "days_ago": 5, "price_usd": 10}],
    }
    sm = build_smart(sig)
    ok("build_smart: 3 okazje w 'all'", len(sm["all"]) == 3)
    ok("build_smart: AAA w insider i congress (konfluencja możliwa)",
       any(o["ticker"] == "AAA" for o in sm["insider"]) and any(o["ticker"] == "AAA" for o in sm["congress"]))
    ok("build_smart None -> bezpieczny dict", isinstance(build_smart(None), dict) and build_smart(None)["all"] == [])
    ok("build_smart {} -> puste", build_smart({})["all"] == [])

    print(f"\n=== WYNIK: {P} OK, {F} FAIL ===")
    if F == 0:
        print("=== lowca_sources.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if F == 0 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_run_selftest() if "--selftest" in sys.argv else 0)
