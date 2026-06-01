# lens_shadow.py — ŚRODOWISKO POMIAROWE skuteczności soczewek (news/PEAD/smart money).
#
# PROBLEM: soczewek nie da się klasycznie zbacktestować (brak darmowych danych
# historycznych: kto miał katalizator dnia X, transkrypty, 13F per dzień). Jedyny
# UCZCIWY pomiar to FORWARD TEST: codziennie logujemy co bot wybrałby BEZ soczewek
# i co Z soczewkami, a po czasie mierzymy który zestaw faktycznie zarobił.
#
# TRYB SHADOW (domyślny): soczewki liczą scalary i logują, ale NIE zmieniają realnych
# decyzji. Bot kupuje wg samego momentum (jak w backteście). Rejestr zbiera dowody.
# Po 2-4 tyg lens_evaluate.py liczy: czy ranking z soczewkami bije ranking bez.
#
# TRYB LIVE (po dowodach): scalary wpływają na ranking realnych zakupów.
#
# Rejestr: lens_shadow_log.json (append-only). Każdy wpis = jeden cykl:
#   data, lista kandydatów z (momentum_score, news_scalar, pead_scalar, sm_scalar,
#   combined_scalar, score_bazowy, score_z_soczewkami), top-N bazowy vs top-N soczewki.

from __future__ import annotations
import os
import json
from datetime import datetime, timezone
from typing import Optional

SHADOW_LOG_PATH = "lens_shadow_log.json"
DEFAULT_TOP_N = 5

# Tryb soczewek: "shadow" = tylko log (bezpieczne), "live" = wpływ na zakupy.
# Czytany z ENV LENS_MODE; domyślnie shadow. Flip na live DOPIERO po dowodach.
def lens_mode() -> str:
    m = os.environ.get("LENS_MODE", "shadow").strip().lower()
    return "live" if m == "live" else "shadow"


def combined_scalar(news_s: float, pead_s: float, sm_s: float) -> float:
    """Łączny mnożnik z trzech soczewek. 1.0 = neutralny (brak sygnału).
    Mnożymy, bo to niezależne potwierdzenia. Clamp do [0.5, 1.6] — żeby pojedyncza
    soczewka nie zdominowała rankingu momentum."""
    raw = float(news_s) * float(pead_s) * float(sm_s)
    return max(0.5, min(1.6, raw))


def build_shadow_entry(candidates_scored: list, top_n: int = DEFAULT_TOP_N) -> dict:
    """Buduje wpis rejestru z listy kandydatów.

    candidates_scored: lista dict {ticker, mom_score, news_scalar, pead_scalar,
                                    sm_scalar} — scalary już policzone przez soczewki.
    Zwraca wpis z rankingiem bazowym (mom) i z soczewkami (mom*combined)."""
    rows = []
    for c in candidates_scored:
        mom = float(c.get("mom_score", 0.0))
        ns = float(c.get("news_scalar", 1.0))
        ps = float(c.get("pead_scalar", 1.0))
        ss = float(c.get("sm_scalar", 1.0))
        comb = combined_scalar(ns, ps, ss)
        rows.append({
            "ticker": c.get("ticker", ""),
            "mom_score": round(mom, 4),
            "news_scalar": round(ns, 3),
            "pead_scalar": round(ps, 3),
            "sm_scalar": round(ss, 3),
            "combined_scalar": round(comb, 3),
            "score_baseline": round(mom, 4),
            "score_lens": round(mom * comb, 4),
        })
    base_rank = sorted(rows, key=lambda r: r["score_baseline"], reverse=True)
    lens_rank = sorted(rows, key=lambda r: r["score_lens"], reverse=True)
    base_top = [r["ticker"] for r in base_rank[:top_n]]
    lens_top = [r["ticker"] for r in lens_rank[:top_n]]
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": lens_mode(),
        "candidates": rows,
        "baseline_top": base_top,
        "lens_top": lens_top,
        "divergence": sorted(set(base_top) ^ set(lens_top)),   # różnice między zestawami
    }


def append_shadow_log(entry: dict, path: str = SHADOW_LOG_PATH) -> bool:
    """Dopisuje wpis do rejestru (append-only). Brak/uszkodzony plik -> nowy [].
    BEZPIECZNE: każdy błąd zwraca False, bot leci dalej (to pomiar, nie krytyczne)."""
    try:
        log = []
        if os.path.exists(path):
            try:
                log = json.loads(open(path, encoding="utf-8").read())
                if not isinstance(log, list):
                    log = []
            except Exception:
                log = []
        log.append(entry)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(log, ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


def has_divergence(entry: dict) -> bool:
    """Czy soczewki w ogóle zmieniły ranking (jest sens mierzyć)."""
    return bool(entry.get("divergence"))


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

    # combined_scalar
    check("combined neutralny = 1.0", combined_scalar(1.0, 1.0, 1.0) == 1.0)
    check("combined bull > 1", combined_scalar(1.15, 1.1, 1.2) > 1.0)
    check("combined clamp górny 1.6", combined_scalar(1.6, 1.6, 1.6) == 1.6)
    check("combined clamp dolny 0.5", combined_scalar(0.5, 0.5, 0.5) == 0.5)

    # build_shadow_entry — soczewki podbijają spółkę B nad A
    cands = [
        {"ticker": "AAA", "mom_score": 10.0, "news_scalar": 1.0, "pead_scalar": 1.0, "sm_scalar": 1.0},
        {"ticker": "BBB", "mom_score": 9.0, "news_scalar": 1.15, "pead_scalar": 1.1, "sm_scalar": 1.2},
    ]
    entry = build_shadow_entry(cands, top_n=1)
    check("baseline top = AAA (wyższy mom)", entry["baseline_top"] == ["AAA"])
    check("lens top = BBB (soczewki podbiły)", entry["lens_top"] == ["BBB"])
    check("divergence wykryta", has_divergence(entry))
    check("wpis ma date", "date" in entry and len(entry["date"]) == 10)
    check("wpis ma mode", entry["mode"] in ("shadow", "live"))

    # brak sygnału -> brak różnicy
    cands_flat = [
        {"ticker": "AAA", "mom_score": 10.0},
        {"ticker": "BBB", "mom_score": 9.0},
    ]
    entry2 = build_shadow_entry(cands_flat, top_n=1)
    check("brak soczewek -> baseline = lens", entry2["baseline_top"] == entry2["lens_top"])
    check("brak soczewek -> brak divergence", not has_divergence(entry2))

    # append + odczyt
    tmp = "/tmp/lens_shadow_test.json"
    if os.path.exists(tmp):
        os.remove(tmp)
    ok1 = append_shadow_log(entry, tmp)
    ok2 = append_shadow_log(entry2, tmp)
    check("append zwraca True", ok1 and ok2)
    saved = json.loads(open(tmp).read())
    check("rejestr ma 2 wpisy", len(saved) == 2)

    print(f"WYNIK lens_shadow: {passed} OK, {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    print("lens_shadow.py — środowisko pomiarowe soczewek (shadow A/B). Użyj --selftest.")
