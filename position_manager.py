"""
position_manager.py — Zarządzanie OTWARTYMI pozycjami (pamięć dzień-na-dzień).

Bot dotąd umiał tylko OTWIERAĆ pozycje. Ten moduł dodaje drugą połowę:
decyzję per istniejąca pozycja — HOLD / SELL_ALL / SELL_PARTIAL — na podstawie
9 reguł w kolejności priorytetu (pierwsza pasująca wygrywa).

KOLEJNOŚĆ REGUŁ (ochrona kapitału > realizacja zysku):
  1. Radar 3/3 (krach)            -> SELL_ALL   (tryb ochrony, wyjście ze wszystkiego)
  2. Stop loss trafiony           -> SELL_ALL   (cena <= stop)
  3. Smart money flip HARD_BLOCK  -> SELL_ALL   (klaster sprzedaży C-Suite — insiderzy wiedzą)
  4. Earnings <= 2 dni            -> SELL_ALL   (binarne ryzyko luki przeskakującej stop)
  5. Trailing -8% od szczytu      -> SELL_ALL   (odwrócenie trendu — twardsze niż target)
  6. Take profit >= +20%          -> SELL_PARTIAL 25% + stop do break-even
  7. Parabola (RSI>85 & 1.25xSMA) -> SELL_PARTIAL 50%
  8. Time-stop (>=15 dni, <+3%)   -> SELL_ALL   (najsłabszy sygnał — na końcu)
  9. inaczej                      -> HOLD       (+ aktualizacja high-water mark)

Pamięć: ManagedPosition trzyma metadane (data wejścia, stop, szczyt, dni) w portfolio.json.
Decyzje logowane do decision_log.json (append-only audit trail).

FAIL-CLOSED: brak danych do reguły (np. brak ceny) -> NIE podejmuje agresywnej akcji,
domyślnie HOLD, ale jeśli brak ceny uniemożliwia ocenę stopu -> ostrzeżenie w reason.

Tryby: --selftest (testy offline, fixtures).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("porsche.posman")


# ─────────────────────────────────────────────────────────────────────────────
# KONFIG — parametry reguł
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PositionManagerConfig:
    # TRAILING ADAPTACYJNY (ratchet) — stop podnosi się RÓWNO z zyskiem, nigdy nie schodzi.
    # Drabinka: (próg_zysku, gwarantowany_zysk_blokowany_stopem).
    # Czytaj: gdy zysk >= próg, stop ustawia się tak, by blokować co najmniej dany zysk.
    # Przykład @100->@150 (+50%): wpada w próg 0.40 -> stop blokuje +10% (czyli ~110).
    profit_ladder: tuple = (
        (0.05, -0.02),   # +5%  -> stop tuż pod break-even (chroni przed dużą stratą)
        (0.10,  0.00),   # +10% -> break-even (pozycja darmowa)
        (0.15,  0.05),   # +15% -> blokuj +5%
        (0.25,  0.10),   # +25% -> blokuj +10%
        (0.40,  0.20),   # +40% -> blokuj +20%
        (0.60,  0.35),   # +60% -> blokuj +35%
        (1.00,  0.70),   # +100%-> blokuj +70% (house money)
    )
    # Trailing od szczytu jako DRUGI bezpiecznik (gdy cena spada od HWM mimo wysokiego zysku):
    trailing_drawdown_pct: float = 0.12      # luźniejszy (-12%), by nie wylecieć na zygzaku
    take_profit_partial_pct: float = 0.30    # +30% -> opcjonalna realizacja CZĘŚCI (nie całość!)
    take_profit_fraction: float = 0.25       # ile ściąć przy realizacji częściowej
    parabola_rsi: float = 85.0               # RSI > 85 = parabola
    parabola_sma_mult: float = 1.25          # cena > 1.25x SMA20 = parabola
    parabola_fraction: float = 0.50          # ile sprzedać przy paraboli
    time_stop_days: int = 15                 # >= 15 dni trzymania
    time_stop_min_profit_pct: float = 0.03   # i zysk < +3% -> uwolnij kapitał
    earnings_exit_days: int = 2              # wyniki za <= 2 dni -> wyjście
    crash_radar_level: int = 3               # radar 3/3 -> wyjście ze wszystkiego


def _days_since(entry_date: str) -> "Optional[int]":
    """Wiek pozycji w dniach: entry_date (ISO YYYY-MM-DD) -> dziś. None gdy brak/zły format.
    FIX days_held: pole w portfolio.json bywa 0 i NIGDY nie rośnie -> liczymy wiek z daty wejścia,
    inaczej reguła 8 (time-stop: uwolnij martwy kapitał po >=15 dniach) nigdy nie zadziała."""
    if not entry_date:
        return None
    try:
        ed = datetime.strptime(str(entry_date)[:10], "%Y-%m-%d").date()
        return max(0, (date.today() - ed).days)
    except Exception:
        return None


def _resolve_days(d: dict) -> int:
    """Wiek pozycji: najpierw z entry_date (poprawne), fallback do zapisanego days_held."""
    days = _days_since(d.get("entry_date", ""))
    return days if days is not None else int(d.get("days_held", 0))


# ─────────────────────────────────────────────────────────────────────────────
# STAN POZYCJI (pamięć dzień-na-dzień)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ManagedPosition:
    """Pozycja z metadanymi do zarządzania. Trzymana w portfolio.json."""
    ticker: str
    shares: float
    entry_price_usd: float
    entry_date: str                          # ISO "YYYY-MM-DD"
    stop_loss_usd: float
    high_water_mark_usd: float = 0.0         # najwyższa cena od wejścia
    days_held: int = 0

    def __post_init__(self):
        if self.high_water_mark_usd <= 0:
            self.high_water_mark_usd = self.entry_price_usd

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "ManagedPosition":
        return ManagedPosition(
            ticker=d["ticker"], shares=float(d["shares"]),
            entry_price_usd=float(d["entry_price_usd"]),
            entry_date=d.get("entry_date", ""),
            stop_loss_usd=float(d.get("stop_loss_usd", 0.0)),
            high_water_mark_usd=float(d.get("high_water_mark_usd", 0.0)),
            days_held=_resolve_days(d),
        )


# ─────────────────────────────────────────────────────────────────────────────
# DANE RYNKOWE do oceny pozycji (dostarczane na żywo / mock w testach)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PositionMarketData:
    """Bieżący kontekst rynkowy dla jednej pozycji."""
    ticker: str
    current_price_usd: float
    rsi14: Optional[float] = None
    price_vs_sma20: Optional[float] = None       # cena / SMA20
    days_to_earnings: Optional[int] = None
    smart_money_hard_block: bool = False          # klaster sprzedaży C-Suite


# ─────────────────────────────────────────────────────────────────────────────
# DECYZJA
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PositionDecision:
    ticker: str
    action: str                              # "HOLD" | "SELL_ALL" | "SELL_PARTIAL"
    reason: str
    rule: int = 0                            # która reguła zadziałała (1-9)
    fraction: float = 1.0                    # dla SELL_PARTIAL: ile sprzedać (0-1)
    new_stop_usd: Optional[float] = None     # nowy poziom Sell Stop (jeśli zmieniony)
    updated_position: Optional[ManagedPosition] = None   # zaktualizowany stan (HWM itd.)


# ─────────────────────────────────────────────────────────────────────────────
# SILNIK DECYZYJNY
# ─────────────────────────────────────────────────────────────────────────────
class PositionManager:
    def __init__(self, config: Optional[PositionManagerConfig] = None):
        self.cfg = config or PositionManagerConfig()

    def adaptive_stop(self, pos: ManagedPosition, price: float) -> float:
        """Liczy ADAPTACYJNY stop wg drabinki zysku (ratchet — tylko w górę).
        Stop podnosi się równo z zyskiem. NIGDY nie schodzi poniżej obecnego stopu."""
        cfg = self.cfg
        entry = pos.entry_price_usd
        if entry <= 0:
            return pos.stop_loss_usd
        pl_pct = (price - entry) / entry
        guaranteed = None
        for threshold, lock in cfg.profit_ladder:
            if pl_pct >= threshold:
                guaranteed = lock
            else:
                break
        if guaranteed is None:
            return pos.stop_loss_usd
        ladder_stop = entry * (1 + guaranteed)
        return max(pos.stop_loss_usd, ladder_stop)   # RATCHET: nigdy w dół

    def evaluate_one(self, pos: ManagedPosition, md: PositionMarketData,
                     radar_level: int) -> PositionDecision:
        """Ocenia JEDNĄ pozycję wg 9 reguł (pierwsza pasująca wygrywa)."""
        cfg = self.cfg
        price = md.current_price_usd
        pl_pct = (price - pos.entry_price_usd) / pos.entry_price_usd if pos.entry_price_usd > 0 else 0.0

        # aktualizuj high-water mark NA POCZĄTKU (każda reguła widzi świeży szczyt)
        hwm = max(pos.high_water_mark_usd, price)
        updated = ManagedPosition(
            ticker=pos.ticker, shares=pos.shares, entry_price_usd=pos.entry_price_usd,
            entry_date=pos.entry_date, stop_loss_usd=pos.stop_loss_usd,
            high_water_mark_usd=hwm, days_held=pos.days_held,
        )

        def decision(action, reason, rule, fraction=1.0, new_stop=None):
            return PositionDecision(ticker=pos.ticker, action=action, reason=reason,
                                    rule=rule, fraction=fraction, new_stop_usd=new_stop,
                                    updated_position=updated)

        # adaptacyjny stop (ratchet): podnosi się równo z zyskiem, nigdy w dół
        ratchet_stop = self.adaptive_stop(pos, price)
        updated.stop_loss_usd = ratchet_stop   # zapamiętaj podniesiony stop w stanie

        # 1. RADAR 3/3 (krach) -> SELL_ALL
        if radar_level >= cfg.crash_radar_level:
            return decision("SELL_ALL", f"radar {radar_level}/3 — tryb ochrony kapitału (krach)", 1)

        # 2. STOP LOSS trafiony -> SELL_ALL (porównanie z ADAPTACYJNYM stopem)
        if ratchet_stop > 0 and price <= ratchet_stop:
            locked = (ratchet_stop - pos.entry_price_usd) / pos.entry_price_usd * 100
            return decision("SELL_ALL",
                            f"cena {price:.2f} <= stop {ratchet_stop:.2f} (zablokowany zysk {locked:+.0f}%) — wyjście", 2)

        # 3. SMART MONEY FLIP (HARD_BLOCK) -> SELL_ALL  [podniesione z #8]
        if md.smart_money_hard_block:
            return decision("SELL_ALL", "klaster sprzedaży C-Suite (HARD_BLOCK) — insiderzy wychodzą", 3)

        # 4. EARNINGS <= 2 dni -> SELL_ALL
        if md.days_to_earnings is not None and 0 <= md.days_to_earnings <= cfg.earnings_exit_days:
            return decision("SELL_ALL", f"wyniki za {md.days_to_earnings} dni — binarne ryzyko luki", 4)

        # 5. TRAILING od szczytu (DRUGI bezpiecznik — gdy cena spada od HWM) -> SELL_ALL
        if hwm > 0:
            drawdown = (hwm - price) / hwm
            if drawdown >= cfg.trailing_drawdown_pct:
                return decision("SELL_ALL",
                                f"cena {price:.2f} spadła {drawdown*100:.1f}% od szczytu {hwm:.2f} — odwrócenie trendu", 5)

        # 6. TAKE PROFIT CZĘŚCIOWY +30% -> SELL_PARTIAL (NIE zabija pozycji, reszta biegnie)
        #    Stop NIE schodzi do break-even — adaptacyjny ratchet już blokuje zysk.
        if pl_pct >= cfg.take_profit_partial_pct:
            return decision("SELL_PARTIAL",
                            f"zysk +{pl_pct*100:.1f}% — realizuję {cfg.take_profit_fraction*100:.0f}% (zdejmuję ryzyko), reszta biegnie; stop {ratchet_stop:.2f}",
                            6, fraction=cfg.take_profit_fraction, new_stop=ratchet_stop)

        # 7. PARABOLA (RSI>85 & cena>1.25xSMA20) -> SELL_PARTIAL 50%
        if (md.rsi14 is not None and md.price_vs_sma20 is not None
                and md.rsi14 > cfg.parabola_rsi and md.price_vs_sma20 > cfg.parabola_sma_mult):
            return decision("SELL_PARTIAL",
                            f"parabola (RSI {md.rsi14:.0f}, cena {md.price_vs_sma20:.2f}xSMA20) — ścinam {cfg.parabola_fraction*100:.0f}%; stop {ratchet_stop:.2f}",
                            7, fraction=cfg.parabola_fraction, new_stop=ratchet_stop)

        # 8. TIME-STOP (>=15 dni i zysk < +3%) -> SELL_ALL  [na końcu — najsłabszy sygnał]
        if pos.days_held >= cfg.time_stop_days and pl_pct < cfg.time_stop_min_profit_pct:
            return decision("SELL_ALL",
                            f"trzymane {pos.days_held} dni, zysk tylko +{pl_pct*100:.1f}% — uwalniam kapitał", 8)

        # 9. HOLD + ADAPTACYJNY STOP (mailuj nowy poziom jeśli się podniósł)
        new_stop = None
        reason = f"trzymam (P/L {pl_pct*100:+.1f}%)"
        if ratchet_stop > pos.stop_loss_usd + 0.001:
            new_stop = ratchet_stop
            locked = (ratchet_stop - pos.entry_price_usd) / pos.entry_price_usd * 100
            reason += f" — PODNIEŚ STOP na {ratchet_stop:.2f} (blokuje zysk {locked:+.0f}%)"
        if hwm > pos.high_water_mark_usd:
            reason += f", nowy szczyt {hwm:.2f}"
        return decision("HOLD", reason, 9, new_stop=new_stop)

    def evaluate(self, positions: list, market: dict, radar_level: int) -> list:
        """Ocenia WSZYSTKIE pozycje. market: {ticker: PositionMarketData}.
        Pozycja bez danych rynkowych -> HOLD z ostrzeżeniem (fail-CLOSED: nie sprzedajemy w ciemno,
        ale flagujemy że nie ocenialiśmy ryzyka)."""
        out = []
        for pos in positions:
            md = market.get(pos.ticker)
            if md is None:
                out.append(PositionDecision(
                    ticker=pos.ticker, action="HOLD", rule=0,
                    reason="BRAK DANYCH RYNKOWYCH — nie oceniono ryzyka (sprawdź ręcznie)",
                    updated_position=pos,
                ))
                continue
            out.append(self.evaluate_one(pos, md, radar_level))
        return out


# ─────────────────────────────────────────────────────────────────────────────
# PERSYSTENCJA — portfolio.json (pamięć) + decision_log.json (audyt)
# ─────────────────────────────────────────────────────────────────────────────
def load_managed_positions(portfolio_path: str | Path) -> list:
    """Czyta ManagedPosition z portfolio.json (klucz 'managed_positions'). Brak -> []."""
    p = Path(portfolio_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [ManagedPosition.from_json(d) for d in data.get("managed_positions", [])]
    except Exception as e:
        logger.warning("Nie wczytano managed_positions (%s) — pusta lista.", e)
        return []


def save_managed_positions(portfolio_path: str | Path, positions: list,
                           cash_pln: Optional[float] = None) -> None:
    """Zapisuje ManagedPosition do portfolio.json (zachowuje inne klucze jeśli są)."""
    p = Path(portfolio_path)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["managed_positions"] = [mp.to_json() for mp in positions]
    if cash_pln is not None:
        data["cash_pln"] = round(cash_pln, 2)
    data["updated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def append_decision_log(log_path: str | Path, decisions: list, run_date: str = "") -> None:
    """Dopisuje decyzje do decision_log.json (append-only audit trail)."""
    p = Path(log_path)
    log = []
    if p.exists():
        try:
            log = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(log, list):
                log = []
        except Exception:
            log = []
    stamp = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for d in decisions:
        log.append({"date": stamp, "ticker": d.ticker, "action": d.action,
                    "rule": d.rule, "reason": d.reason,
                    "fraction": d.fraction, "new_stop_usd": d.new_stop_usd})
    p.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# SELFTEST — fixtures dla każdej z 9 reguł + cascade + persystencja
# ─────────────────────────────────────────────────────────────────────────────
def _run_selftest() -> int:
    print("=== SELFTEST position_manager (offline, fixtures) ===")
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1; print(f"  [OK] {name}")
        else: failed += 1; print(f"  [FAIL] {name}")

    pm = PositionManager()

    def pos(ticker="AVGO", entry=400.0, stop=380.0, hwm=400.0, days=0, shares=0.5):
        return ManagedPosition(ticker=ticker, shares=shares, entry_price_usd=entry,
                               entry_date="2026-05-01", stop_loss_usd=stop,
                               high_water_mark_usd=hwm, days_held=days)

    def md(ticker="AVGO", price=405.0, rsi=55.0, pvs=1.05, d2e=None, hardblock=False):
        return PositionMarketData(ticker=ticker, current_price_usd=price, rsi14=rsi,
                                  price_vs_sma20=pvs, days_to_earnings=d2e,
                                  smart_money_hard_block=hardblock)

    # ── Reguła 1: radar 3/3 -> SELL_ALL ────────────────────────────────────────
    d = pm.evaluate_one(pos(), md(price=405), radar_level=3)
    check("R1 radar 3/3 -> SELL_ALL", d.action == "SELL_ALL" and d.rule == 1)

    # ── Reguła 2: stop trafiony -> SELL_ALL ─────────────────────────────────────
    d = pm.evaluate_one(pos(stop=380), md(price=379), radar_level=0)
    check("R2 stop trafiony -> SELL_ALL", d.action == "SELL_ALL" and d.rule == 2)

    # ── Reguła 3: smart money flip -> SELL_ALL ──────────────────────────────────
    d = pm.evaluate_one(pos(), md(price=405, hardblock=True), radar_level=0)
    check("R3 smart money HARD_BLOCK -> SELL_ALL", d.action == "SELL_ALL" and d.rule == 3)

    # ── Reguła 4: earnings <=2 dni -> SELL_ALL ──────────────────────────────────
    d = pm.evaluate_one(pos(), md(price=405, d2e=1), radar_level=0)
    check("R4 earnings T+1 -> SELL_ALL", d.action == "SELL_ALL" and d.rule == 4)

    # ── Reguła 5: trailing -12% od szczytu (drugi bezpiecznik) -> SELL_ALL ──────
    # szczyt 500, cena 435 = -13% -> trailing. Uwaga: entry niskie by drabinka nie
    # zadziałała pierwsza; tu entry=430 (zysk +1.2%, poniżej drabinki).
    d = pm.evaluate_one(pos(entry=430, stop=350, hwm=500), md(price=435), radar_level=0)
    check("R5 trailing -12% od szczytu -> SELL_ALL", d.action == "SELL_ALL" and d.rule == 5)

    # ── Reguła 6: take profit CZĘŚCIOWY +30% -> SELL_PARTIAL (NIE całość) ────────
    # entry 400, cena 524 = +31%, szczyt 524. Drabinka: +31% wpada w próg 0.25->lock 0.10,
    # więc adaptacyjny stop = 440 (poniżej ceny 524, więc R2 nie strzela). R6 ścina część.
    d = pm.evaluate_one(pos(entry=400, stop=350, hwm=524), md(price=524), radar_level=0)
    check("R6 take profit +30% -> SELL_PARTIAL (część)", d.action == "SELL_PARTIAL" and d.rule == 6 and abs(d.fraction - 0.25) < 0.001)
    check("R6 stop NIE do break-even, tylko ratchet (>entry)", d.new_stop_usd is not None and d.new_stop_usd >= 400.0)

    # ── ADAPTACYJNY STOP (drabinka ratchet): kupno @100, cena @150 -> stop blokuje +10% ──
    p_ladder = ManagedPosition(ticker="X", shares=1, entry_price_usd=100.0, entry_date="2026-05-01",
                               stop_loss_usd=92.0, high_water_mark_usd=100.0, days_held=2)
    new_stop = pm.adaptive_stop(p_ladder, 150.0)   # +50% -> próg 0.40 -> lock 0.20 -> stop 120
    check("RATCHET: @100->@150 stop blokuje min +10% (drabinka)", new_stop >= 110.0)
    # ratchet nigdy w dół: gdy cena spadnie do 130, stop nie schodzi poniżej już ustawionego
    p_ladder.stop_loss_usd = new_stop
    lower = pm.adaptive_stop(p_ladder, 130.0)      # +30% -> lock 0.10 -> 110, ale ratchet trzyma 120
    check("RATCHET: nie schodzi w dół gdy cena spada", lower >= new_stop - 0.01)

    # ── Reguła 7: parabola -> SELL_PARTIAL 50% ──────────────────────────────────
    # RSI 88, cena 1.30x SMA20, ale zysk < 20% i brak trailing
    d = pm.evaluate_one(pos(entry=400, stop=350, hwm=420), md(price=415, rsi=88, pvs=1.30), radar_level=0)
    check("R7 parabola -> SELL_PARTIAL 50%", d.action == "SELL_PARTIAL" and d.rule == 7 and abs(d.fraction - 0.50) < 0.001)

    # ── Reguła 8: time-stop (>=15 dni, <+3%) -> SELL_ALL ────────────────────────
    d = pm.evaluate_one(pos(entry=400, stop=350, hwm=410, days=16), md(price=405, rsi=55, pvs=1.05), radar_level=0)
    check("R8 time-stop 16 dni, +1.25% -> SELL_ALL", d.action == "SELL_ALL" and d.rule == 8)

    # ── Reguła 9: HOLD + update HWM ─────────────────────────────────────────────
    d = pm.evaluate_one(pos(entry=400, stop=350, hwm=410, days=5), md(price=420, rsi=55, pvs=1.05), radar_level=0)
    check("R9 brak triggerów -> HOLD", d.action == "HOLD" and d.rule == 9)
    check("R9 HWM zaktualizowany do nowej ceny (420)", d.updated_position.high_water_mark_usd == 420.0)

    # ── CASCADE PRIORITY: stop (R2) bije take-profit (R6) ──────────────────────
    # entry 400, cena 405 (+1.25%, poniżej drabinki), stop ustawiony wysoko 410 > cena
    # -> R2 strzela (cena <= stop). Adaptacyjny stop nie podniesie (zysk za mały).
    d = pm.evaluate_one(pos(entry=400, stop=410, hwm=405), md(price=405), radar_level=0)
    check("CASCADE: stop (R2) wygrywa nad take-profit (R6)", d.rule == 2)

    # ── CASCADE: radar 3/3 bije wszystko, nawet stop ────────────────────────────
    d = pm.evaluate_one(pos(stop=485), md(price=480), radar_level=3)
    check("CASCADE: radar (R1) wygrywa nad stopem (R2)", d.rule == 1)

    # ── Trailing (R5) łapie głębokie odwrócenie przed częściową realizacją ──────
    # entry 430 (zysk mały), szczyt 520, cena 450 (-13.5% od szczytu) -> R5.
    d = pm.evaluate_one(pos(entry=430, stop=350, hwm=520), md(price=450), radar_level=0)
    check("Trailing (R5) łapie odwrócenie", d.action == "SELL_ALL" and d.rule == 5)

    # ── evaluate() wielu pozycji + brak danych -> HOLD z ostrzeżeniem ───────────
    decisions = pm.evaluate(
        positions=[pos(ticker="AVGO"), pos(ticker="CIEN"), pos(ticker="NODATA")],
        market={"AVGO": md(ticker="AVGO", price=405),
                "CIEN": md(ticker="CIEN", price=405, hardblock=True)},
        radar_level=0)
    by_tk = {d.ticker: d for d in decisions}
    check("evaluate: AVGO HOLD", by_tk["AVGO"].action == "HOLD")
    check("evaluate: CIEN SELL_ALL (hardblock)", by_tk["CIEN"].action == "SELL_ALL")
    check("evaluate: brak danych -> HOLD z ostrzeżeniem", by_tk["NODATA"].action == "HOLD" and "BRAK DANYCH" in by_tk["NODATA"].reason)

    # ── PERSYSTENCJA: zapis/odczyt portfolio.json + HWM przetrwa ────────────────
    import tempfile, os
    tmp = tempfile.mkdtemp()
    pf = os.path.join(tmp, "portfolio.json")
    mp = pos(ticker="AVGO", entry=422.01, stop=405.53, hwm=450.0, days=3)
    save_managed_positions(pf, [mp], cash_pln=1000.0)
    loaded = load_managed_positions(pf)
    check("Persystencja: zapis+odczyt zachowuje pozycję", len(loaded) == 1 and loaded[0].ticker == "AVGO")
    check("Persystencja: HWM zachowany (450)", loaded[0].high_water_mark_usd == 450.0)
    check("Persystencja: stop zachowany (405.53)", abs(loaded[0].stop_loss_usd - 405.53) < 0.01)

    # ── PARTIAL SELL zapamiętany: po SELL_PARTIAL shares maleje, zapis trwały ────
    d = pm.evaluate_one(mp, md(ticker="AVGO", price=560.0), radar_level=0)  # +33% -> partial
    if d.action == "SELL_PARTIAL":
        remaining = ManagedPosition(ticker=mp.ticker, shares=mp.shares * (1 - d.fraction),
                                    entry_price_usd=mp.entry_price_usd, entry_date=mp.entry_date,
                                    stop_loss_usd=d.new_stop_usd or mp.stop_loss_usd,
                                    high_water_mark_usd=d.updated_position.high_water_mark_usd,
                                    days_held=mp.days_held)
        save_managed_positions(pf, [remaining])
        reloaded = load_managed_positions(pf)
        check("Partial sell: shares zmniejszone i zapisane",
              abs(reloaded[0].shares - mp.shares * 0.75) < 0.0001)
    else:
        check("Partial sell: shares zmniejszone i zapisane", False)

    # ── decision_log append-only ────────────────────────────────────────────────
    dl = os.path.join(tmp, "decision_log.json")
    append_decision_log(dl, decisions, run_date="2026-05-27")
    append_decision_log(dl, decisions, run_date="2026-05-28")
    log = json.loads(Path(dl).read_text(encoding="utf-8"))
    check("decision_log rośnie (append-only)", len(log) == 6)  # 3 decyzje x 2 dni

    print(f"\n=== WYNIK: {passed} OK, {failed} FAIL ===")
    if failed == 0:
        print("=== position_manager.py — WSZYSTKIE TESTY PRZESZŁY ===")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Porsche — Position Manager")
    ap.add_argument("--selftest", action="store_true", help="testy offline")
    args = ap.parse_args()
    if args.selftest:
        return _run_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
