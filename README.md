# Bot Porsche

Autonomiczny asystent researchu giełdowego. Codziennie skanuje rynek (S&P 500
+ ADR), proponuje pozycje, zarządza istniejącymi przez adaptacyjny stop (ratchet),
mailuje raport. **Nie handluje sam** — człowiek wykonuje zlecenia ręcznie w XTB.

## Co bot ROBI

- ranking sektorów + spółek przez multi-period momentum (3/6/12 mies, klasyczne Jegadeesh-Titman)
- makro-filtr `SPY < 200SMA` → zero nowych zakupów (czeka w gotówce, lekarstwo na momentum crashes)
- adaptacyjny stop (ratchet) — podnosi się równo z zyskiem, nigdy nie schodzi
- realizacja częściowa zysku (zdejmuje ryzyko ze stołu, reszta biegnie)
- 13 bezpieczników, reconcile z eksportem XTB, performance tracker vs SPY z alertami

## Co bot świadomie NIE robi

- nie używa dźwigni / CFD (zero, na zawsze — patrz konstytucja)
- nie tyka konta IKE
- nie kupuje w spadającym rynku (makro-filtr)
- nie obiecuje konkretnego zwrotu (edge cykliczny — patrz wyniki niżej)

## Wyniki ultra testu (2026-05, 4 sweepy + OOS validation)

| Okno | CAGR bot (wariant H) | edge vs SPY | MaxDD |
|------|----------------------|-------------|-------|
| 5 lat | +57% | +39pp | 19% |
| 10 lat | +23% | +8pp | 30% |
| 15 lat | +16% | +1pp | 27% |
| 20 lat | +12% | +1pp | 42% |
| 30 lat | +8% | **−1pp** | 35% |
| OOS 1994-2026 | +7% | **−4pp** | — |

**Wniosek:** strategia ma edge CYKLICZNY (działa w hossie), nie permanentny.
Bezpieczeństwo (MaxDD ≤ SPY) zwalidowane. Bot startuje świadomie — większa
ekspozycja kapitału z wbudowaną regułą wyjścia (performance tracker).

## Architektura

- **Kod:** Python 3.12, czysty pandas, yfinance, curl_cffi
- **Hosting:** Claude Code Routines (chmura Anthropic), schedule daily
- **Dane wejściowe:** eksport XTB przez Google Drive, ceny przez yfinance
- **Wyjście:** mail przez Resend, stan zapisywany do `data/*.json` i commitowany do repo
- **Trwałość:** repo GitHub jako jedyny magazyn stanu między runami

## Pliki

```
porsche-pipeline/      # cały kod (15 modułów, 241 testów)
data/                  # stan trwały (portfolio.json, equity_log.json, decision_log.json)
docs/
  KONSTYTUCJA_v2_AGRESYWNA.md     # nadrzędne zasady projektu
  ROUTINE_SETUP.md                # jak skonfigurować routine od zera (krok po kroku)
  PLAN_WDROZENIA_CLOUD.md         # operacyjne wdrożenie i reguły codzienne
  ULTRA_TEST_WYNIKI.md            # surowe wyniki sweepów (audyt)
requirements.txt
.gitignore
README.md
```

## Selftest

```bash
cd porsche-pipeline
for f in *.py; do python "$f" --selftest 2>&1 | grep "WYNIK"; done
# RAZEM: 241 OK, 0 FAIL
```

## Uruchomienie ręczne (lokalnie, opcjonalnie)

```bash
cd porsche-pipeline
python main_pipeline.py --export /path/to/xtb_export.xlsx --fx 4.02
```

Tryb `--live --send` wymaga ustawionych env: `RESEND_API_KEY`, `FINNHUB_API_KEY`,
`REPORT_RECIPIENT`, `REPORT_SENDER`. Nigdy nie commituj kluczy do repo.

---

**To nie jest porada inwestycyjna. Decyzję podejmuje człowiek, świadomie.**
