# smart_money_confluence.py — DODAJE warstwy smart money zgubione przy przepisaniu.
#
# Stary bot miał 3 warstwy: insiderzy 35% / fundusze 13F 40% / politycy 25%.
# Nowy smart_money_engine.py robi TYLKO insiderów (live OpenInsider) + put/call.
# Ten moduł dokłada brakujące dwie warstwy (fundusze, politycy) i łączy wszystko
# w jeden werdykt CONFLUENCE.
#
# ZASADA Z KONSTYTUCJI: smart money to KONFLUENCJA (potwierdzenie), NIGDY trigger.
# Dane są opóźnione (insiderzy ~2 dni, 13F ~45 dni, politycy ~30-45 dni), więc to
# potwierdzenie pomysłów z momentum + TARCZA WYJŚCIA (klaster sprzedaży = blokada).
#
# ARCHITEKTURA DANYCH (spójna z news_lens — agent wypełnia JSON, nie kruche scrapery):
# - insider score: przekazywany z smart_money_engine (live, już działa)
# - fundusze + politycy: smart_money.json wypełniany przez AGENTA routine przez
#   web_search (Dataroma dla 13F, senate/house disclosures dla polityków).
#   Brak pliku -> te warstwy = 0 (neutralne), insider score działa sam.
#
# ODPORNOSC: brak danych dowolnej warstwy -> ta warstwa = 0, reszta liczy normalnie.

from __future__ import annotations
import os
import json
from typing import Optional

SMART_MONEY_PATH = "smart_money.json"

# Wagi warstw (jak w starym bocie). Suma cap-ów = 10 pkt.
THRESHOLDS = {"confirmation": 5.0, "convergence": 7.0, "negative": -2.0}

# Cap-y punktów per warstwa (proporcje ~ 35/40/25 ze starego)
CAP_INSIDER = 3.5
CAP_FUNDS = 4.0
CAP_POLITICIANS = 2.5


def _insider_pts(rec: Optional[dict]) -> float:
    """rec: {n_insiders, n_directors, total_usd, price_vs_52wh_pct, cluster_sell}.
    cluster_sell = tarcza wyjścia (negatyw)."""
    if not rec:
        return 0.0
    if rec.get("cluster_sell"):
        return -2.0
    pts = 0.0
    nb = rec.get("n_insiders", 0)
    pts += min(2.0, nb * 1.0)                       # P-buys C-suite, cap 2
    if nb >= 3 or rec.get("n_directors", 0) >= 2:
        pts += 1.5                                  # KLASTER
    if (rec.get("price_vs_52wh_pct") or 0) <= -20:
        pts += 0.5                                  # kupno w dołku
    return min(CAP_INSIDER, pts)


def _fund_pts(recs: Optional[list]) -> float:
    """recs: lista {manager, pct_portfolio, action}  action: new/add/hold/trim."""
    if not recs:
        return 0.0
    pts = 0.0
    for r in recs:
        if (r.get("pct_portfolio") or 0) >= 1.0:
            pts += 1.0
        if r.get("action") in ("new", "add"):
            pts += 1.0
    return min(CAP_FUNDS, pts)


def _pol_pts(recs: Optional[list]) -> float:
    """recs: lista {name, days_to_disclose, top_tier, committee_match}."""
    if not recs:
        return 0.0
    pts = 0.0
    for r in recs:
        pts += 0.5
        if r.get("top_tier"):
            pts += 0.5
        if r.get("committee_match"):
            pts += 0.5
    return min(CAP_POLITICIANS, pts)


def load_smart_money(path: str = SMART_MONEY_PATH) -> dict:
    """Wczytaj smart_money.json (fundusze+politycy od agenta). Brak/błąd -> pusty."""
    try:
        if not os.path.exists(path):
            return {"insiders": {}, "funds": {}, "politicians": {}, "sector_exits": [], "_empty": True}
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"insiders": {}, "funds": {}, "politicians": {}, "sector_exits": [], "_empty": True}


def score_ticker(ticker: str, sm: dict, insider_override: Optional[dict] = None) -> dict:
    """Łączny score 0-10 z rozbiciem na warstwy.
    insider_override: rec insiderów z LIVE engine (smart_money_engine). Jeśli podany,
    używamy go zamiast sm['insiders'][ticker] (live > json)."""
    ins = insider_override if insider_override is not None else sm.get("insiders", {}).get(ticker)
    fun = sm.get("funds", {}).get(ticker, [])
    pol = sm.get("politicians", {}).get(ticker, [])
    p_ins = _insider_pts(ins)
    p_fun = _fund_pts(fun)
    p_pol = _pol_pts(pol)
    total = max(-2.0, p_ins + p_fun + p_pol)
    flag = ("convergence" if total >= THRESHOLDS["convergence"]
            else "confirmation" if total >= THRESHOLDS["confirmation"]
            else "negative" if total <= THRESHOLDS["negative"]
            else "none")
    return {"ticker": ticker, "total": round(total, 1), "insiders": round(p_ins, 1),
            "funds": round(p_fun, 1), "politicians": round(p_pol, 1), "flag": flag,
            "detail_insider": ins, "detail_funds": fun, "detail_pol": pol}


def exit_shield(sm: dict) -> list:
    """Tarcza wyjścia: tickery do BLOKADY longów (klaster sprzedaży / masowe wyjścia)."""
    blocked = []
    for tk, rec in sm.get("insiders", {}).items():
        if rec.get("cluster_sell"):
            blocked.append(tk)
    blocked += sm.get("sector_exits", [])
    return sorted(set(blocked))


def confluence_badge(score: dict) -> Optional[tuple]:
    """Plakietka (typ, tekst) do maila na podstawie łącznego score."""
    if not score:
        return None
    flag = score.get("flag")
    total = score.get("total", 0)
    if flag == "convergence":
        return ("sm_conv", f"Smart money: KONWERGENCJA ({total}/10)")
    if flag == "confirmation":
        return ("sm_conf", f"Smart money: potwierdzenie ({total}/10)")
    if flag == "negative":
        return ("sm_neg", f"Smart money: wyjścia ({total})")
    return None


def confluence_scalar(score: dict) -> float:
    """Mnożnik do scoringu kandydata. 1.0 = neutral.
    BEZPIECZNY: brak danych -> 1.0. Konwergencja wzmacnia, wyjścia osłabiają."""
    if not score:
        return 1.0
    flag = score.get("flag")
    if flag == "convergence":
        return 1.20
    if flag == "confirmation":
        return 1.10
    if flag == "negative":
        return 0.80
    return 1.0


def _run_selftest() -> int:
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {name}")

    demo = {
        "insiders": {
            "UNH": {"n_insiders": 4, "n_directors": 3, "total_usd": 3_000_000, "price_vs_52wh_pct": -46},
            "XYZ": {"n_insiders": 0, "cluster_sell": True},
        },
        "funds": {"UNH": [{"manager": "Pabrai", "pct_portfolio": 5.0, "action": "add"}]},
        "politicians": {"UNH": [{"name": "Suozzi", "days_to_disclose": 20, "top_tier": True, "committee_match": False}]},
        "sector_exits": ["XLF"],
    }
    s_unh = score_ticker("UNH", demo)
    check("UNH ma 3 warstwy > 0", s_unh["insiders"] > 0 and s_unh["funds"] > 0 and s_unh["politicians"] > 0)
    check("UNH convergence/confirmation", s_unh["flag"] in ("convergence", "confirmation"))
    check("UNH scalar > 1", confluence_scalar(s_unh) > 1.0)
    s_xyz = score_ticker("XYZ", demo)
    check("XYZ cluster_sell -> negative", s_xyz["flag"] == "negative")
    check("XYZ scalar < 1", confluence_scalar(s_xyz) < 1.0)
    check("exit_shield ma XYZ i XLF", set(exit_shield(demo)) == {"XYZ", "XLF"})
    # brak danych -> neutralne
    empty = load_smart_money("/tmp/nie_ma_sm_xyz.json")
    check("brak pliku -> _empty", empty.get("_empty") is True)
    s_none = score_ticker("AAA", empty)
    check("brak danych -> total 0", s_none["total"] == 0.0)
    check("brak danych -> scalar 1.0", confluence_scalar(s_none) == 1.0)
    # insider_override (live z engine)
    s_ov = score_ticker("BBB", empty, insider_override={"n_insiders": 3, "n_directors": 2})
    check("insider_override działa", s_ov["insiders"] > 0)
    # badge
    check("badge convergence", confluence_badge({"flag": "convergence", "total": 8})[0] == "sm_conv")
    check("badge none -> None", confluence_badge({"flag": "none", "total": 2}) is None)

    print(f"WYNIK smart_money_confluence: {passed} OK, {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    print("smart_money_confluence.py — warstwy fundusze 13F + politycy. Użyj --selftest.")
