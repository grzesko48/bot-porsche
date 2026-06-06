# WYMAGANIA — oba boty (v2, po retrospektywie tygodnia 1)

Stan: 2026-06-05. Reguły obowiązujące oba boty bota Porsche po pierwszym tygodniu na żywo.

## Wspólne (oba boty)
1. **Wynik mierzymy w USD, nie PLN** — FX (słaby/mocny złoty) to NIE nasza zasługa ani wina. (Tydzień 1: +11,4% PLN, ale realnie tylko +1,4% USD — reszta to słaby złoty.)
2. **Nie kupujemy górki:** ruch **+25–40%** w ~miesiąc bez cofnięcia → **CZEKAJ na cofnięcie**, *chyba że* mocne przesłanki dalszego wzrostu (**FURTKA**). Powyżej **+50% (parabola) → ZAWSZE czekaj** (furtka nie działa).
3. **Stop chroni, ratchet blokuje zysk** (nigdy nie oddaje do straty).
4. **Każda rekomendacja zapisana** (dziennik decyzji = forward-test, mierzymy realny edge na żywo).
5. **Konto IKE 54821934 NIETYKALNE. Egzekucja ręczna na XTB** — bot niczego nie kupuje sam.

## Bot ŁOWCA (spekulacja ≤15% sleeve, dni robocze 14:30)
1. Pokazuje **TYLKO swoje** otwarte pozycje (nie rdzeń bilansu).
2. Skanuje: IPO / nietypowy wolumen / kontrakt + smart-money (insiderzy / fundusze 13F / kongres) + **POLITYKA/TRUMP** (kontrakty rządowe DoD/DOE/NASA, executive orders, wypowiedzi → mapowane na sektor/spółkę).
3. Dla każdej okazji podaje: **`pct_1m`** (ruch ~1-mies), **`catalyst`** (tag), **`still_upside`** + **`upside_reason`** (gdy mocne przesłanki dalszego wzrostu).
4. **Bezpiecznik „nie kupuj górki" + FURTKA:** rozgrzane (≥+25%) → CZEKAJ; furtka (świeży katalizator ≫ cena / podniesione guidance / konfluencja≥3) → KUP mimo, ale **mniejszą pozycją** (×0,6, ryzyko ↑). ≥+50% → zawsze CZEKAJ.
5. **Ultra-pick** (ramka ★) tylko przy **score ≥ 8 ORAZ konfluencja ≥ 2**.
6. Sizing w klatce min(sleeve 15%, wolna gotówka); konkretny Sell Stop, cel, ryzyko w zł.
7. **Pętla nauki:** loguje przeoczone duże wzrosty + lekcje (żeby nie powtarzać błędów).

## Bot RYNKOWY / BILANS (rdzeń ~85%, codziennie 21:00)
1. Zarządza pozycjami rdzenia (HOLD/SELL per 9-regułowa kaskada), **ratchet stop**, **time-stop liczony z `entry_date`** (naprawione — wcześniej days_held=0 i reguła nie działała).
2. **12 bezpieczników przed zakupem:** earnings gate (≤2 dni → blok), płynność, RSI<75, **NIE parabola** (cena < SMA20×1,15) **+ FURTKA** (`strong_catalyst` → luźniej do 1,30×), koncentracja, gotówka, min. zlecenie, akcje ułamkowe…
3. **Makro-filtr:** SPY < 200SMA → ZERO nowych zakupów (czeka w gotówce; istniejące pozycje dalej zarządzane).
4. **Radar 3/3 → SELL wszystko** (ochrona kapitału przy krachu).
5. Pokazuje **WSZYSTKIE** pozycje (rdzeń + łowcy) w tabeli o 21:00 z żywym P&L (teraz/start/cena kupna/cena teraz/stop/TP/% zysk/kwota zysk + suma).
6. Pozycje / cash / equity zapisywane poprawnie (potwierdzone testami).

## Czego NIE robimy (zamknięte danymi z tygodnia 1)
- ❌ Nie kupujemy szczytu na nagłówku (lekcja **MRVL** — kupione po poppie Jensena Huanga, −7,5%).
- ❌ Nie gonimy hype'u z social media (piki z TikToka spadły 5–21% w tym samym tygodniu).
- ❌ **Nie mylimy FX z alfą.**
- ❌ Nie ufamy 5-letniemu backtestowi (overfit; edge momentum jest reżimowy, OOS 2006-16 ujemny).

## Status techniczny
- Kod obu botów wdrożony (commit `cb698b9`+). Selftesty: lowca 35/35, safety_gates 14/14, pipeline 19/19, bilans 23/23, position_manager 24/24.
- Instrukcja routine łowcy zawiera skaner POLITYKA/TRUMP, `pct_1m`, `catalyst`, `still_upside`.
- Furtka działa dwiema ścieżkami: ocena agenta (`still_upside`) ORAZ automatycznie przy konfluencji ≥ 3.
