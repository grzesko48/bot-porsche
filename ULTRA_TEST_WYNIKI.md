# ULTRA TEST — surowe wyniki audytu (2026-05)

Dokument referencyjny, **nie edytować**. Świadomy zapis tego, na czym stoi
obecny config (wariant H) — i czego o sobie nie wiemy.

---

## Cztery sweepy, w kolejności

### Sweep 1 — baseline (5 lat tylko)
Config: cap 400, 5 pozycji, stały trailing -8%.
- CAGR bot +15.4% / SPY +15.4% → **edge +0.1pp**
- Werdykt: bot ≈ SPY, brak edge.

### Sweep 2 — dokręcona koncentracja (5 lat)
Config: cap 700, 3 pozycje, ratchet ladder.
- CAGR bot +28.9% / SPY +15.4% → **edge +13.6pp**
- Sharpe 1.14, MaxDD 19.1%, hit 39%
- **5 lat to mały sample** — uruchomiło walidację wielookienną

### Sweep 3 — multi-period momentum + makro-filtr (5/10/15/20 lat)
Wariant H zwycięski:
| Okno | CAGR | edge | MaxDD |
|------|------|------|-------|
| 5l | +57% | +39pp | 19% |
| 10l | +23% | +8pp | 30% |
| 15l | +16% | +1pp | 27% |
| 20l | +12% | +1pp | 42% |

- Edge dodatni na każdym oknie → wyglądało jak prawdziwa strategia
- Ale CAGR +57% na 5l = poziom Renaissance Medallion = matematycznie niemożliwe
- Sygnał ostrzegawczy: survivorship bias albo dotuning do cyklu post-2020

### Sweep 4 — ultra test (5/10/15/20/30/50/100 + OOS split)
- **30 lat: edge −1pp** (negatywny w długim oknie)
- **OOS 1994-2026: edge −4pp** (decydujące, 32 lata danych)
- **Concentration TOP 5 = 42%** — APP + NVDA = ~25% edge na 5 lat (dwa ex-post zwycięzcy)
- **Werdykt:** edge cykliczny, nie permanentny.

## Trzy kryteria "twardej strategii" (sam test je zdefiniował)

| Kryterium | Wynik | Status |
|-----------|-------|--------|
| Edge dodatni na **wszystkich** oknach | 30l = -1pp | ❌ FAIL |
| Concentration TOP 5 < 50% | 42% (na granicy) | ⚠️ NA GRANICY |
| OOS: edge dodatni na **obu** połówkach | 1994-2026 = -4pp | ❌ FAIL |

**Bot nie przeszedł ultra testu według własnych kryteriów. Świadomie startujemy
z większą ekspozycją kapitału, akceptując że to cyclical bet, nie kompounder.**

## Co działa naprawdę (zwalidowane)

- **Bezpieczeństwo:** MaxDD 35-50% w długich oknach (≤ SPY 55%). Makro-filtr zwalidowany.
- **Edge w hossie (5-15 lat):** realny, +1pp do +39pp (po haircut: ~+5pp do +20pp realnie).
- **Inżynieria:** 241 testów, fail-CLOSED, reconcile, 13 bezpieczników. Pewne.

## Co nie działa (świadomie)

- **Edge w długim oknie:** −1pp do −4pp na 30l/OOS.
- **Stagflacje, lata 70., długie stagnacje** — strategia momentum tam nie ma sensu.
- **Bias mega-cap winners** — bot żyje częściowo z NVDA/MSTR/AVGO bo dzisiaj są w S&P.

## Reguła wyjścia (wbudowana w `performance_tracker.py`)

- **Próg miękki:** bot ≥10pp pod SPY przez 8 tygodni → ALERT pomarańczowy
- **Próg twardy:** bot ≥20pp pod SPY → HARD ALERT czerwony, sugestia wyjścia do SPY

Te progi są startowe. Do przemyślenia świadomego po 3-6 miesiącach live.

## Co znaczy "świadome ryzyko" w tym projekcie

Wchodzimy z większą ekspozycją niż konserwatywne 10-20%, **wiedząc** że:
1. Strategia w długim oknie OOS przegrywa z SPY.
2. Cykliczny edge ma sens **w tym cyklu** (post-2020 tech bull) — kiedy się odwróci, bot przestanie działać.
3. Performance tracker da sygnał ostrzegawczy, gdy realny zwrot zacznie odbiegać.
4. Sprzedaż do SPY jest zawsze dostępna w jednym kliknięciu.

To **świadomy wybór ryzyka** — nie nadzieja, nie ślepota. Decyzja podjęta na podstawie 4 sweepów i OOS validation.
