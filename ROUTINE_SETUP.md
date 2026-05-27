# ROUTINE SETUP — od zera do pierwszego automatycznego runu

Architektura cloud-native: bot żyje w prywatnym repo GitHub, uruchamia się
w chmurze Anthropic (Claude Code Routines) raz dziennie po sesji USA, pobiera
eksport XTB z Google Drive, wysyła mail przez Resend, commituje stan z powrotem
do repo. **Twoja maszyna nie musi być włączona.**

> Wymagania wstępne: konto Anthropic z **planem Max** (15 runów/dzień),
> konto GitHub, konto Google z Drive, konto Resend (nowy klucz),
> konto Finnhub (free wystarczy).

---

## KROK 1 — Repozytorium prywatne na GitHub

1. Wejdź na <https://github.com/new>
2. Nazwa: `bot-porsche` (lub Twoja własna)
3. **Visibility: Private** — KRYTYCZNE, repo zawiera Twoje pliki stanu finansowego
4. Bez README, bez .gitignore, bez licencji (mamy własne)
5. **Create repository**

Zapamiętaj adres repo, np. `git@github.com:<user>/bot-porsche.git`.

## KROK 2 — Wgranie kodu z `bot_porsche_repo.zip`

Rozpakuj zip lokalnie i wypchnij na GitHub:

```bash
unzip bot_porsche_repo.zip -d bot-porsche
cd bot-porsche
git init
git add .
git commit -m "init: bot porsche, wariant H, 241 testów"
git branch -M main
git remote add origin git@github.com:<user>/bot-porsche.git
git push -u origin main
```

Sanity check: w repo na GitHub musisz widzieć foldery `porsche-pipeline/`,
`data/`, `docs/` i plik `.gitignore`. **W `.gitignore` musi być `.env` i `*.key`** —
to chroni przed wypchnięciem kluczy.

## KROK 3 — Regeneracja klucza RESEND

Stary klucz był eksponowany w rozmowie, musi być nowy.

1. <https://resend.com> → Login → API Keys
2. Stary klucz: **Revoke**
3. **Create API Key** → nazwa `bot-porsche-routine`, uprawnienia `Sending access`
4. Skopiuj nowy klucz **JEDNORAZOWO** (więcej go nie pokaże). Trzymaj w bezpiecznym miejscu (np. menedżer haseł). NIE wklejaj nigdzie w plikach repo.

## KROK 4 — Połączenie konektorów w Claude Code

Wejdź na <https://claude.ai/code> i upewnij się, że masz aktywne:

- **GitHub** — uprawnienia do repo `<user>/bot-porsche` (read + write — bot będzie commitował stan)
- **Google Drive** — dostęp do folderu z eksportem XTB (już masz go w sesji ze mną)

Jeśli któregoś z konektorów nie ma — w UI Claude Code jest sekcja "Connectors" do podłączenia.

## KROK 5 — Tworzenie Routine

W <https://claude.ai/code/routines> kliknij **Create routine**. Cztery sekcje:

### 5a. Repository
- Wybierz `<user>/bot-porsche`, branch `main`

### 5b. Environment variables (sekrety routine — nie idą do repo)

| Klucz | Wartość |
|-------|---------|
| `RESEND_API_KEY` | (klucz z kroku 3) |
| `FINNHUB_API_KEY` | Twój klucz Finnhub |
| `FRED_API_KEY` | `43b3985ff9099545ca8398fac7a8951e` |
| `REPORT_RECIPIENT` | `grzesko48@gmail.com` |
| `REPORT_SENDER` | `Market Bot <onboarding@resend.dev>` |

> SSL bundle (`CURL_CA_BUNDLE` itd.) NIE jest potrzebny w chmurze.
> Routine ma czyste środowisko bez TLS interception — standardowy bundle Pythona wystarcza.

### 5c. Setup script (uruchamiany raz przed promptem)

```bash
cd /workspace
pip install -r requirements.txt --quiet
echo "Bot gotowy: $(date)"
```

### 5d. Prompt routine (serce automatyzacji)

```
Jesteś agentem uruchamiającym Bot Porsche. Twoja jedyna rola: codzienny cykl bota.

KROK 1 — Pobierz najnowszy eksport XTB z Google Drive
- Folder Drive ID: 1YRH_4DO8f1SA565miyZcjVA8_5zkaqBV
- Wyszukaj najnowszy plik xlsx (po modifiedTime), nazwa zawiera "PLN_54820945"
- Pobierz do /tmp/xtb_export.xlsx

KROK 2 — Sprawdź kurs USD/PLN
- Web search lub api: aktualny kurs USD/PLN, zaokrąglij do 2 miejsc po przecinku
- Jeśli niedostępne, użyj 4.0 jako fallback

KROK 3 — Uruchom bota
cd /workspace
python porsche-pipeline/main_pipeline.py \
  --export /tmp/xtb_export.xlsx \
  --fx <kurs_z_kroku_2> \
  --live --send

KROK 4 — Sprawdź output
- Jeśli stdout zawiera "BOT ZATRZYMANY" lub "HALT" → ALERT (wyślij mi prywatny mail przez Resend z tematem "Bot HALT" i pełnym output jako body)
- Jeśli mail bota poszedł (kod wyjścia 0) → OK

KROK 5 — Commit stanu z powrotem do repo
git add data/portfolio.json data/equity_log.json data/decision_log.json
git -c user.email=routine@bot-porsche -c user.name="Bot Routine" \
    commit -m "auto: cykl $(date +%Y-%m-%d)" || echo "Brak zmian stanu"
git push origin main

KRYTYCZNE ZASADY:
- NIE modyfikuj kodu w porsche-pipeline/
- NIE uruchamiaj bota bez --live --send (pełen cykl)
- Jeśli krok się nie udaje, NIE próbuj naprawiać — wyślij mi prywatny mail z opisem
- NIGDY nie loguj klucza RESEND_API_KEY ani innych sekretów w outputach
```

### 5e. Trigger (harmonogram)

- **Schedule** → `Daily` → `23:30 UTC` (1:30 AM polskiego czasu zimą / 0:30 latem)
  - Sesja USA zamyka się 22:00 polskiego czasu, dane yfinance są świeże po ~1h
- Możesz też dodać API trigger, gdybyś chciał ręcznie uruchamiać przez kliknięcie

**Save routine.**

## KROK 6 — Wgranie eksportu XTB na Drive (codziennie)

Po zamknięciu sesji USA:
1. xStation 5 → eksport historii / saldo do XLSX
2. Wrzuć na Drive do folderu `1YRH_4DO8f1SA565miyZcjVA8_5zkaqBV`
3. Routine sama go znajdzie (sortuje po `modifiedTime`)

> Można zautomatyzować eksport XTB do Drive, ale to osobny projekt.
> Na MVP — ręczny upload raz dziennie.

## KROK 7 — Pierwszy test routine RĘCZNY (przed schedule)

W UI routine kliknij **Run now**. Obserwuj live log:

**Co MUSI się stać:**
- Routine pobiera eksport z Drive (komunikat w logu)
- `pip install -r requirements.txt` przechodzi czysto
- `main_pipeline.py` wypisuje statystyki (gotówka, equity, radar, kandydaci)
- **Mail przychodzi na grzesko48@gmail.com**
- Commit "auto: cykl YYYY-MM-DD" w repo (sprawdź na GitHub w zakładce Commits)

**Co NIE może się stać:**
- BOT ZATRZYMANY / HALT — jeśli tak, pisz do mnie z pełnym logiem
- Push odrzucony (problem z uprawnieniami GitHub — sprawdź krok 4)
- `ModuleNotFoundError` — sprawdź `requirements.txt` w repo

## KROK 8 — Włączenie schedule

Jeśli krok 7 przeszedł czysto: schedule jest już zapisany. Routine odpali się
sama następnego dnia o ustalonej godzinie.

**Co teraz Twoja rola:**
1. Codziennie po sesji USA wrzuć eksport XTB na Drive (5 sekund)
2. Rano przeczytaj mail bota
3. Jeśli mail mówi `PORTFEL DO OTWARCIA` — wykonaj zlecenia ręcznie w xStation 5
4. Jeśli mail mówi `ZARZĄDZANIE POZYCJAMI: SELL_*` lub `HOLD — PODNIEŚ STOP` — wykonaj
5. Jeśli mail ma sekcję `⚠ ALERT` / `🛑 HARD ALERT` — patrz reguła wyjścia (`PLAN_WDROZENIA_CLOUD.md`)

---

## Co kosztuje to wdrożenie

- Repo prywatne GitHub: $0 (limit free)
- Routine: w ramach planu Max (15/dzień, używamy 1/dzień)
- Resend: $0 (free 100 maili/dzień)
- Finnhub: $0 (free)
- yfinance: $0
- Google Drive: $0 (limit free)

**Łącznie $0 dodatkowo** — wszystko mieści się w planach, które już masz.

## Problemy, które MOŻE napotkasz

- **GitHub push odrzucony** — uprawnienia konektora GitHub w Claude Code, sprawdź czy ma write
- **Mail nie dochodzi** — sprawdź RESEND_API_KEY w env routine; alternatywnie spam folder
- **yfinance timeout** — czasem się zdarza, routine odpali się ponownie następnego dnia. Jeśli powtarza się 3 dni z rzędu, pisz do mnie
- **xtb export nie znaleziony** — sprawdź czy plik jest w folderze Drive, czy nazwa zawiera `PLN_54820945`

W każdym z tych przypadków routine NIE wykonuje transakcji — bot ma fail-CLOSED.
Bezpieczeństwo > tempo.
