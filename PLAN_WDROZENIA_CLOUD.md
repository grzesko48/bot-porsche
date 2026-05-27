# PLAN WDROŻENIA — Cloud-native

Bot żyje w chmurze Anthropic (Claude Code Routines). Twoja maszyna nie musi być
włączona. Ten dokument jest operacyjny — krok po kroku, co dziś, co jutro,
co reguluje wycofanie.

> **Zanim coś z tego zaczniesz: przeczytaj `ROUTINE_SETUP.md`.** Tam jest
> jednorazowa konfiguracja środowiska. Ten plik to praca codzienna.

---

## 1. CO BOT TERAZ ROBI

**Config zamrożony (wariant H z ultra-testu):**
- multi-period momentum 3/6/12 mies, pomijając ostatni miesiąc
- makro-filtr `SPY < 200SMA` → zero nowych zakupów (czeka w gotówce)
- ratchet adaptacyjny — stop podnosi się równo z zyskiem, nigdy nie schodzi
- cap pozycji 700 zł, max 3 pozycje, ~90% kapitału w grze
- performance tracker — co cykl mierzy bot vs SPY, alarmuje przy underperformance

**Co bot wie o sobie świadomie (z ultra-testu):**
- edge CYKLICZNY (działa w hossie), nie permanentny
- 5-15 lat: edge dodatni; 20+ lat OOS: edge ujemny
- bezpieczeństwo MaxDD ≤ SPY w długich oknach — zwalidowane

---

## 2. UCHOMIENIE ROUTINE — kolejność na dziś

1. **Wykonaj `ROUTINE_SETUP.md` w całości** (8 kroków). Jednorazowe.
2. **Krok 7 z setupu (Run now) MUSI przejść czysto** zanim włączysz schedule.
3. **Pierwszy planowy run** — następnego dnia po sesji USA, sprawdź mail rano.

## 3. PRACA CODZIENNA

### Wieczorem (po sesji USA — po 22:00)
- Eksport historii z xStation 5 → XLSX → wrzuć na Drive (folder z setupu)

### Rano (przed sesją USA — najlepiej między 7:00 a 13:00)
- Otwórz mail bota
- **Sekcja "Performance vs SPY"** — sprawdź edge w pp i ewentualny ALERT
- **Sekcja "ZARZĄDZANIE POZYCJAMI"** — wykonaj WSZYSTKIE dyrektywy (po kolei):
  - `SELL_ALL <ticker> @ ~<cena>` → market sell w xStation 5
  - `SELL_PARTIAL <ticker> <X>% @ ~<cena>` → częściowy sell market
  - `HOLD <ticker> — PODNIEŚ STOP na <cena_USD>` → modyfikuj istniejący Sell Stop w xStation
- **Sekcja "PORTFEL DO OTWARCIA"** — wykonaj zlecenia:
  - Buy market — podana liczba akcji ułamkowych
  - Osobny Sell Stop — cena USD z maila

> Bot **NIGDY** nie klika za Ciebie — wszystkie zlecenia ręcznie w xStation 5.
> To `człowiek w pętli` z konstytucji projektu. Bezpieczeństwo > tempo.

### Po zleceniach
- Pobierz nowy eksport z xStation, wrzuć na Drive
- Następny cykl routine (jutro w nocy) zrobi reconcile z nowym stanem

## 4. REGUŁA WYJŚCIA — wbudowana w trackerze

Tracker mierzy realny zwrot bota vs SPY od daty startu. **W każdym mailu**
widzisz sekcję `Performance vs SPY` z bot/SPY/edge w pp.

### Próg miękki — ALERT (pomarańczowy banner w mailu)
- **Bot ≥ 10pp pod SPY przez 8 tygodni z rzędu**
- Działanie: **przemyśl wyjście do SPY/IUSP.UK**
- Strategia może już być poza swoim cyklem działania

### Próg twardy — HARD ALERT (czerwony banner)
- **Bot ≥ 20pp pod SPY** (niezależnie od liczby dni)
- Działanie: **wyjście do SPY** — strategia wyraźnie nie działa
- Sprzedaj wszystkie pozycje w xStation, przerzuć kapitał na IUSP.UK

> Bot **NIE** zamyka pozycji sam przy alercie — to **sugestia operacyjna**.
> Decyzja zawsze Twoja. Klikasz w xStation świadomie.

Te progi są zapisane w `porsche-pipeline/performance_tracker.py` jako
`DEFAULT_ALERT_THRESHOLD_PP=10` i `DEFAULT_HARD_STOP_THRESHOLD_PP=20`.
Po 3-6 miesiącach realnych danych można je przemyśleć świadomie.

## 5. KIEDY ZATRZYMAĆ ROUTINE

Routine można wyłączyć/wstrzymać w UI Claude Code → Routines. Kiedy to robić:

- Wyjazd, kilka dni bez dostępu do XTB → wyłącz schedule, bo bot będzie mailował
  propozycje, których nie wykonasz
- HARD ALERT → wyłącz schedule **po** zamknięciu pozycji
- Cokolwiek dziwnego w mailu (HALT, niezgodności, zera tam gdzie nie powinny być)
  → wyłącz schedule, otwórz logi routine, debuguj zanim wrócisz

## 6. CO NA PEWNO NIE ROBIĆ

- ❌ Nie wpychać kluczy do repo (są w `.gitignore` i w env routine — sprawdź przed każdym commitem)
- ❌ Nie ustawiać repo na public — to dane finansowe
- ❌ Nie używać dźwigni / CFD
- ❌ Nie tykać konta IKE (54821934) — bot tylko na koncie roboczym (54820945)
- ❌ Nie handlować ręcznie spółkami, których bot nie wskazał — zaburza tracking vs SPY
- ❌ Nie ignorować HARD ALERT — to wbudowana reguła wyjścia, nie sugestia

## 7. GDY COŚ SIĘ ZEPSUJE

- **Routine pada przy `pip install`** — sprawdź `requirements.txt` w repo, czy nie brakuje paczki
- **Mail nie przychodzi** — sprawdź `RESEND_API_KEY` w env routine; sprawdź spam
- **reconcile delta > 0.01** — eksport XTB nie zgadza się z oczekiwanym stanem,
  bot zrobi HALT (poprawne zachowanie). Sprawdź ręcznie czy plik eksportu jest aktualny
- **Commit do GitHub nieudany** — uprawnienia konektora GitHub w Claude Code (write?)
- **yfinance timeout** — pojedyncze padnięcie OK, routine sama spróbuje jutro

W każdym z tych przypadków bot **nie wykonuje fałszywych transakcji** — fail-CLOSED.

## 8. AGENCJA AI — to dalej główna droga

Bot kompounderuje w tle z dyscypliną wpisaną w tracker. **Główna droga do Porsche
to agencja AI** — robisz na pełnym etacie, bot 10 minut dziennie po sesji.

Te dwie rzeczy NIE konkurują o Twój czas. Bot to scoreboard rynku, nie codzienny
projekt.

---

**Powodzenia.** Zbudowaliśmy realny system z wbudowaną dyscypliną. Reszta to wykonanie.
