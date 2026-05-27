# KONSTYTUCJA PROJEKTU — BOT PORSCHE v2 (AGRESYWNA)

> Ten dokument jest nadrzędny. Zastępuje wersję v1. Gdy cokolwiek się rozjeżdża,
> wracamy tutaj. Nowy pomysł albo pasuje do konstytucji, albo zmieniamy ją świadomie.
> Nie budujemy nic "obok".

---

## CEL

Zbudować bota giełdowego, który **realnie i konsekwentnie bije rynek (SP500/QQQ)**
o maksymalny możliwy margines — bez sztucznego sufitu na to, ile może zarobić.
Jeśli edge i rynek pozwolą na +40%, +70%, +100% w danym roku — bot tego NIE dławi.
Pozwala zyskom biec. Mierzy się ambitnie: górą jest rynek do pobicia, nie arbitralny próg.

Droga do Porsche (1,7 mln zł): kapitał + dopłaty + składanie zysków przez lata, przy
zwrocie tak wysokim, jak realny edge pozwala wycisnąć. Bot jest silnikiem zwrotu,
nie jedynym źródłem kapitału.

---

## ZASADA NADRZĘDNA: CEL > POJEDYNCZY STRZAŁ

**Najważniejszy jest CEL, nie zrobienie x10 na jednej spółce i niedoczekanie.**
Konsekwentne składanie realnych zysków bije loterię "trzymam jedną do księżyca".
To odróżnia inwestora od hazardzisty. Bot realizuje zyski systematycznie (część lub
całość), rotuje kapitał do najlepszych okazji i NIE zakochuje się w pojedynczej pozycji.
Lepiej zdjąć pewny +30% i złożyć go dalej, niż trzymać marząc o +900% i oddać wszystko.

## CO ZNACZY "BEZ LIMITU" (precyzyjnie)

**Sufit zdjęty z ZYSKÓW:**
- Brak sztucznego "sprzedaj przy +20% i koniec". Zwycięzca biegnie, dopóki trend trwa.
- Wyjście wyznacza trailing (liczony przez bota, bo XTB nie trailuje akcji OMI — zweryfikowane)
  i odwrócenie trendu, NIE arbitralny target.
- Koncentracja dozwolona, gdy przekonanie jest wysokie i poparte wieloma soczewkami.
- Brak sufitu na liczbę % rocznie w żadnym miejscu kodu.

**STOP PODĄŻA ZA ZYSKIEM (ratchet — tylko w górę, nigdy w dół):**
- Gdy cena rośnie, stop-loss podnosi się wraz z nią, blokując coraz więcej zysku.
- Przykład: kupno @100, cena @150 → stop podniesiony do ~110+ (blokuje min. +10% zysku).
- Stop NIGDY nie schodzi w dół. Raz podniesiony, zostaje albo idzie wyżej.
- Bot liczy nowy poziom i MAILUJE go do ręcznego ustawienia (XTB nie trailuje akcji OMI).
- NAPIĘCIE do strojenia: za ciasny trailing = wylot na zygzaku przed ruchem; za luźny =
  oddanie dużej części zysku. Strojony per zmienność spółki, walidowany backtestem.

**REALIZACJA ZYSKU — część lub całość:**
- Bot może sprzedać CZĘŚĆ pozycji (zdjąć ryzyko ze stołu, resztę zostawić biegnącą)
  albo CAŁOŚĆ na plusie (gdy trend się odwraca / lepsza okazja gdzie indziej).
- Częściowa sprzedaż = zabezpieczenie zysku BEZ zabijania całego biegu pozycji.

**MAKSYMALNE UŻYCIE KAPITAŁU:**
- Bezczynna gotówka to drag na zwrocie. Bot deployuje WIĘKSZOŚĆ portfela.
- Rezerwa tylko na koszt FX + zaokrąglenia (~8-10%), reszta pracuje.
- Przykład: z 1580 zł pracuje ~1420-1450 zł, rezerwa ~130-160 zł.

**Hamulec ZOSTAJE na ryzyku ruiny — i to jest świadoma decyzja, nie ograniczenie ambicji:**
- ZERO dźwigni / CFD. Powód twardy, nie ostrożnościowy: dźwignia nie zwiększa EDGE,
  zwiększa WARIANCJĘ. Przy ujemnej passie lewar zamienia obsunięcie w zero konta.
  Research jednoznaczny: 69-80% kont CFD na XTB traci. Dźwignia to nie przyspieszacz
  zysku — to przyspieszacz ruiny. Bot bez konta = zero ścieżek do jakiegokolwiek celu.
- Konto IKE (54821934) NIETYKALNE.
- Stop-loss na KAŻDEJ pozycji. Bez stopu nie ma wejścia.
- Per-position risk cap: pojedyncza zła transakcja nie może skasować > określonego %
  kapitału (parametr, domyślnie agresywny ale skończony — patrz PARAMETRY).

**Dlaczego ten jeden hamulec zostaje, skoro reszta sufitów spada:**
Innowator maksymalizuje zwrot tam, gdzie wybuch nie zabija (Musk testował rakiety,
które mogły eksplodować — ale nie z załogą na pokładzie w pierwszym locie).
Ruina jest pochłaniająca: -100% to koniec gry, z którego nie ma składania.
Można gonić +100% rocznie agresywnie BEZ ryzyka ruiny — przez edge i pozwalanie
zyskom biec, nie przez dźwignię. To jest różnica między agresją a hazardem.

---

## ŹRÓDŁA EDGE (na czym budujemy przewagę — oparte na research)

Każda soczewka ma udokumentowany lub wiarygodny edge. Dokładamy po jednej, walidujemy backtestem.

1. **Rdzeń: sektor momentum + rotacja** — literatura: +3,6% (Fidelity 15 lat) do +5%/rok
   nadwyżki. Nasz obecny backtest: +0,1pp = NIEDOPRACOWANE. Priorytet: dociągnąć do
   poziomu z literatury (lepszy ranking, multi-period momentum, makro-filtr regime).

2. **PEAD tekstowy** — klasyczny PEAD (sam "beat o X%") spadł do ~0, ale TEKSTOWY
   (interpretacja transkryptu, guidance, tonu) wciąż żyje (research 2025/2026).
   To naturalna przewaga modelu językowego (Claude czyta transkrypt jak analityk).

3. **Newsy / katalizatory** — Finnhub /news + klasyfikacja przez Claude. Reakcja
   tego samego dnia na deal/kontrakt/supply-crunch. Darmowe.

4. **Pozwól zyskom biec** — trailing zamiast fixed target. Największy błąd momentum
   to ucinanie zwycięzcy. To samo w sobie jest edge.

5. **Smart money (insiderzy)** — już mamy. Klaster zakupów C-Suite = potwierdzenie.

NIE budujemy edge na: options flow / dark pool (research: "same dane nie dają edge"),
X/Twitter (brak wiarygodnego dostępu — web_search nie X API). To filtry potwierdzające
w najlepszym razie, nie alfa. Nie wydajemy na nie pieniędzy bez dowodu edge z backtestu.

---

## METODA (niezmienna — to ona uratowała projekt)

- **Jeden moduł naraz, testuj.** Każdy edge dokładany osobno, z testami, potem RE-BACKTEST.
  Edge wpisany do konstytucji nie jest edge, dopóki backtest go nie potwierdzi w liczbach.
- **Weryfikuj, nie zgaduj.** API, limity, koszty, mechanika brokera — SPRAWDŹ.
  (Przykład z tej sesji: trailing dla akcji OMI na XTB NIE działa — zweryfikowane, nie założone.)
- **Fail-CLOSED.** Brak danych = konserwatywna decyzja, nie optymistyczna.
- **Reconcile + inwarianty + HALT** przy niespójnym stanie. Nienaruszalne.
- **Backtest bez lookahead, z realnymi kosztami.** Survivorship bias nazwany wprost.

---

## PARAMETRY (agresywne, ale skończone — wszystkie do strojenia backtestem)

- Take-profit: BRAK sztywnego sufitu. Trailing liczony przez bota, dynamiczny wg ATR/zmienności.
- Trailing ratchet: stop podnosi się wraz z zyskiem, NIGDY nie schodzi. Progi strojone
  per zmienność spółki (np. spółka spokojna: ciaśniej; MSTR/ARM: luźniej, by nie wylecieć
  na normalnej zmienności). Walidowane backtestem.
- Realizacja częściowa: dozwolona (zdjęcie ryzyka), bez zabijania całej pozycji.
- Deployment kapitału: ~90% pracuje, ~10% rezerwy (FX + zaokrąglenia).
- Koncentracja: do wysokiego % w jedną pozycję przy multi-soczewkowym potwierdzeniu
  (wyżej niż konserwatywne 30% — ale skończone).
- Per-position stop: zawsze obecny. Maksymalna strata na jednej pozycji ograniczona.
- Liczba pozycji: elastyczna, koncentracja > rozproszenie przy wysokim przekonaniu.
- Dźwignia: 0. Na zawsze. To jedyny parametr niepodlegający strojeniu.

---

## TRYB TESTU EKSTREMALNEGO (opcjonalny, świadomy)

Jeśli chcesz sprawdzić strategię ultra-agresywną (max koncentracja, najszybsze rotacje):
robimy to na OSOBNYM, MAŁYM kapitale, który można stracić w całości — NIE na kapitale
fundamentowym. "Fail fast, ale nie fail fatal." Skalujemy dopiero po dowodzie na małym.

---

## CZEGO NIE ROBIMY

- Nie używamy dźwigni/CFD/shortów (XTB akcje rzeczywiste spot only).
- Nie tykamy IKE.
- Nie wpisujemy "gwarantowanego" tempa zysku jako założenia — cel to BIĆ RYNEK
  maksymalnie, a nie obiecywać konkretną liczbę, której nic nie gwarantuje.
- Nie budujemy edge na niesprawdzonych/płatnych źródłach bez dowodu z backtestu.
- Nie budujemy "obok" konstytucji.
```
```
```
ZATWIERDZENIE: Grzegorz Rybak, [data]. v2 zastępuje v1.
```
