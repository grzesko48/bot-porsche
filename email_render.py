# email_render.py — Render maila "Brief inwestycyjny" (styl raport_porsche_ultra_v3).
#
# Premium: ciemny granatowy nagłówek ze złotym akcentem, białe karty ze złotym paskiem,
# action-blocki z kolorowym lewym paskiem (zielony=kup / złoty=sprzedaj / szary=trzymaj),
# ciemna karta smart money, TABELA PORTFELA PO TRANSAKCJACH, progress bar celu.
#
# Każda sekcja ma krótki opis "co to / jak użyć". Action-blocki rozbite na podpunkty
# (Co zrobić / Dlaczego / Soczewki). Duże czcionki, wysoki kontrast.
#
# Inline CSS (Gmail ignoruje <style>). build_email_html(ctx) — interfejs zgodny z main_pipeline.

from __future__ import annotations
from typing import Optional

# ── PALETA ──
BG = "#eef1f5"
DARK = "#0f172a"          # głębszy granat — dla nagłówków, tickerów, akcentów
DARK_CARD = "#1e293b"
GOLD = "#d4af37"
GOLD_DK = "#b8860b"
GREEN = "#15803d"
GREEN_LT = "#dcfce7"
RED = "#b91c1c"
RED_LT = "#fee2e2"
GREY = "#475569"
INDIGO = "#6366f1"
BLUE = "#3b82f6"
TEXT = "#334155"          # tekst opisowy — średni szary (NIE czarny, czytelniejszy)
MUTED = "#64748b"
MUTED2 = "#94a3b8"
LINE = "#e2e8f0"
LINE2 = "#eef1f5"

SERIF = "font-family:Georgia,'Times New Roman',serif;"
SANS = "font-family:-apple-system,Arial,Helvetica,sans-serif;"

# Plakietki soczewek
BADGE_STYLES = {
    "news_bull":    ("#166534", GREEN_LT, "#86efac"),
    "news_bear":    (RED, RED_LT, "#f87171"),
    "news_neutral": ("#b45309", "#fef3c7", "#fbbf24"),
    "pead_bull":    ("#166534", GREEN_LT, "#86efac"),
    "pead_bear":    (RED, RED_LT, "#f87171"),
    "sm_conv":      ("#166534", GREEN_LT, "#86efac"),
    "sm_conf":      ("#166534", GREEN_LT, "#86efac"),
    "sm_neg":       (RED, RED_LT, "#f87171"),
}


def render_badge(badge: Optional[tuple]) -> str:
    if not badge:
        return ""
    typ, text = badge
    fg, bg, br = BADGE_STYLES.get(typ, (MUTED, LINE2, LINE))
    return (f"<span style='display:inline-block;{SANS}font-size:10pt;font-weight:bold;"
            f"letter-spacing:0.2px;color:{fg};background:{bg};border:1px solid {br};"
            f"padding:5px 10px;border-radius:5px;margin:0 0 5px 6px;'>{text}</span>")


def render_badges(badges: Optional[list]) -> str:
    if not badges:
        return ""
    pills = "".join(render_badge(b) for b in badges if b)
    return f"<div style='margin-bottom:8px;'>{pills}</div>" if pills else ""


def render_sources(sources: Optional[list]) -> str:
    if not sources:
        return ""
    links = " · ".join(
        f"<a href='{url}' style='color:#2563eb;text-decoration:none;font-weight:bold;'>{lbl}</a>"
        for lbl, url in sources
    )
    return f"<div style='{SANS}font-size:11pt;color:{MUTED};margin-top:8px;'>📎 Źródła: {links}</div>"


def _card_open(title: str, subtitle: str = "", top_color: str = GOLD, dark: bool = False) -> str:
    bg = DARK_CARD if dark else "#ffffff"
    tcol = MUTED2 if dark else MUTED
    sub = ""
    if subtitle:
        scol = "#cbd5e1" if dark else MUTED
        sub = (f"<div style='{SANS}font-size:11.5pt;color:{scol};line-height:1.5;"
               f"margin:-8px 0 16px;'>{subtitle}</div>")
    return (f"<div style='background:{bg};border-radius:14px;padding:24px 26px;margin-bottom:18px;"
            f"border-top:5px solid {top_color};box-shadow:0 1px 3px rgba(15,23,42,0.08);'>"
            f"<div style='{SANS}font-size:11.5pt;letter-spacing:1.5px;text-transform:uppercase;"
            f"color:{tcol};font-weight:bold;margin-bottom:14px;'>{title}</div>{sub}")


def _action_block(company: str, order: str, order_color: str, bar_color: str,
                  do_html: str, why_html: str, badges_html: str = "",
                  sources_html: str = "") -> str:
    """Blok akcji z podpunktami: → Co zrobić / → Dlaczego."""
    do_part = (f"<div style='{SANS}font-size:12.5pt;color:{TEXT};line-height:1.6;margin-bottom:10px;'>"
               f"<b style='color:{order_color};'>→ Co zrobić:</b><br>{do_html}</div>") if do_html else ""
    why_part = (f"<div style='{SANS}font-size:11.5pt;color:#475569;line-height:1.55;background:#ffffff;"
                f"padding:12px 14px;border-radius:8px;border:1px solid {LINE};'>"
                f"<b style='color:{TEXT};'>→ Dlaczego:</b> {why_html}</div>") if why_html else ""
    return (
        f"<div style='border:1px solid {LINE};border-left:5px solid {bar_color};padding:18px 20px;"
        f"margin-bottom:16px;background:#f8fafc;border-radius:10px;'>"
        f"{badges_html}"
        f"<div style='{SERIF}font-size:19pt;font-weight:bold;color:{DARK};margin-bottom:2px;'>{company}</div>"
        f"<div style='{SANS}font-size:11.5pt;font-weight:bold;text-transform:uppercase;"
        f"letter-spacing:0.5px;color:{order_color};margin:6px 0 12px;'>{order}</div>"
        f"{do_part}{why_part}{sources_html}</div>"
    )


def _portfolio_table(rows: list, total_pln: float, cash_pln: float,
                     beta: Optional[float] = None, beta_note: str = "") -> str:
    """Tabela 'jak ma wyglądać portfel po transakcjach'."""
    beta_box = ""
    if beta is not None:
        beta_box = (f"<div style='background:{RED_LT};border-left:5px solid #ef4444;padding:12px 14px;"
                    f"{SANS}font-size:11.5pt;color:#7f1d1d;margin-bottom:16px;border-radius:6px;"
                    f"font-weight:bold;'>⚠ Beta portfela: {beta:.2f}"
                    + (f" — {beta_note}" if beta_note else "") + "</div>")
    head = (f"<tr>"
            f"<th style='text-align:left;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"letter-spacing:0.5px;border-bottom:2px solid {LINE};padding:0 8px 12px 0;'>Walor</th>"
            f"<th style='text-align:right;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"letter-spacing:0.5px;border-bottom:2px solid {LINE};padding:0 12px 12px;'>Wartość</th>"
            f"<th style='text-align:right;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"letter-spacing:0.5px;border-bottom:2px solid {LINE};padding:0 12px 12px;'>% portfela</th>"
            f"<th style='text-align:left;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"letter-spacing:0.5px;border-bottom:2px solid {LINE};padding:0 12px 12px;'>Sektor</th>"
            f"<th style='text-align:right;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"letter-spacing:0.5px;border-bottom:2px solid {LINE};padding:0 0 12px;'>Stop Loss</th>"
            f"</tr>")
    body = ""
    for r in rows:
        stop = f"{r['stop_usd']:.2f} USD" if r.get("stop_usd") else "—"
        body += (
            f"<tr>"
            f"<td style='padding:14px 8px 14px 0;border-bottom:1px solid {LINE2};{SANS}font-size:13pt;'>"
            f"<b style='color:{DARK};'>{r.get('ticker','')}</b></td>"
            f"<td style='padding:14px 12px;border-bottom:1px solid {LINE2};text-align:right;"
            f"{SANS}font-size:13pt;color:{TEXT};'>{r.get('value_pln',0):,.0f} zł</td>"
            f"<td style='padding:14px 12px;border-bottom:1px solid {LINE2};text-align:right;"
            f"{SANS}font-size:13pt;font-weight:bold;color:{DARK};'>{r.get('pct',0):.0f}%</td>"
            f"<td style='padding:14px 12px;border-bottom:1px solid {LINE2};{SANS}font-size:12pt;"
            f"color:{MUTED};'>{r.get('sector','—')}</td>"
            f"<td style='padding:14px 0;border-bottom:1px solid {LINE2};text-align:right;"
            f"{SANS}font-size:12.5pt;color:{TEXT};'>{stop}</td>"
            f"</tr>"
        )
    return (
        f"{beta_box}"
        f"<table style='width:100%;border-collapse:collapse;'>"
        f"<thead>{head}</thead><tbody>{body}</tbody></table>"
        f"<div style='{SANS}font-size:12pt;color:{MUTED};margin-top:16px;text-align:right;"
        f"padding-top:12px;border-top:2px solid {LINE};'>"
        f"Wartość aktywów: <b style='color:{DARK};font-size:13pt;'>{total_pln:,.0f} zł</b>"
        f"&nbsp;&nbsp;·&nbsp;&nbsp;Gotówka wolna: <b style='color:{DARK};font-size:13pt;'>{cash_pln:,.0f} zł</b></div>"
    )


def build_email_html(ctx: dict) -> str:
    today = ctx.get("today", "")
    tag = ctx.get("tag", "")
    radar = ctx.get("radar_level", 0)
    radar_color = GREEN if radar == 0 else ("#b45309" if radar < 3 else RED)
    radar_status = "Środowisko stabilne" if radar == 0 else ("Podwyższona ostrożność" if radar < 3 else "Tryb obronny")
    radar_desc = ("Brak sygnałów recesyjnych — alokacja kapitału według normalnego planu."
                  if radar == 0 else
                  "Część sygnałów ostrzegawczych aktywna — bot ogranicza otwieranie nowych pozycji."
                  if radar < 3 else
                  "Radar 3/3 — bot NIE otwiera nowych pozycji. Czeka w gotówce, aż rynek się uspokoi.")

    P = []
    P.append(f"<div style='{SANS}background:{BG};padding:22px;'>"
             f"<div style='max-width:700px;margin:0 auto;'>")

    # ── HEADER ──
    P.append(
        f"<div style='background:{DARK};border-radius:14px;padding:28px 30px;margin-bottom:18px;'>"
        f"<div style='{SANS}font-size:10.5pt;text-transform:uppercase;letter-spacing:2.5px;"
        f"color:{GOLD};font-weight:bold;'>Brief inwestycyjny {tag}</div>"
        f"<div style='{SERIF}font-size:25pt;color:#ffffff;margin:10px 0 6px;font-weight:bold;'>"
        f"Raport operacyjny bota Porsche</div>"
        f"<div style='{SANS}font-size:11.5pt;color:{MUTED2};'>{today} · po zamknięciu sesji USA</div>"
        f"<div style='border-top:1px solid #334155;margin:16px 0 12px;'></div>"
        f"<div style='{SANS}font-size:10.5pt;text-transform:uppercase;letter-spacing:1px;"
        f"color:{GOLD};font-weight:bold;'>Gotówka {ctx.get('cash_pln',0):,.0f} zł · "
        f"Equity {ctx.get('equity_pln',0):,.0f} zł · Radar {radar}/3 · Zlecenia ręczne na XTB</div>"
        f"</div>"
    )

    # ── HALT ──
    if ctx.get("halted"):
        P.append(
            _card_open("Stan systemu", top_color="#ef4444") +
            f"<div style='background:{RED_LT};border-left:5px solid #ef4444;padding:16px;"
            f"border-radius:8px;color:#7f1d1d;{SANS}font-size:13pt;font-weight:bold;'>"
            f"🛑 BOT ZATRZYMANY: {ctx.get('halt_reason','')}<br>"
            f"<span style='font-weight:normal;font-size:11.5pt;'>To celowe — bot nie działa na "
            f"niespójnym stanie (fail-CLOSED). Sprawdź eksport XTB i uruchom ponownie.</span></div></div>"
        )
        P.append("</div></div>")
        return "".join(P)

    # ── JAK CZYTAĆ ──
    P.append(
        _card_open("Jak czytać ten raport",
                   "Krótki przewodnik — wykonujesz wszystko ręcznie w xStation 5.") +
        f"<div style='{SANS}font-size:12pt;color:{TEXT};line-height:1.7;'>"
        f"<b style='color:{GREEN};'>1. Sekcja Akcje na dziś</b> — konkretne zlecenia: "
        f"<b>zielony pasek = KUP</b>, <b style='color:{GOLD_DK};'>złoty = SPRZEDAJ</b>, "
        f"<b style='color:{GREY};'>szary = TRZYMAJ</b> (podnieś stop).<br>"
        f"<b style='color:{GREEN};'>2. Plakietki</b> przy spółce (katalizator, PEAD, smart money) "
        f"to dodatkowe potwierdzenia — im więcej zielonych, tym mocniejszy sygnał.<br>"
        f"<b style='color:{GREEN};'>3. Tabela portfela</b> pokazuje, jak ma wyglądać konto "
        f"PO wykonaniu dzisiejszych zleceń.<br>"
        f"<b style='color:{GREEN};'>4. Radar</b> 0/3 = spokój, 3/3 = bot wstrzymuje zakupy.<br>"
        f"<b style='color:{GREEN};'>5. Ceny i Stop Loss</b> wpisujesz w xStation w <b>USD</b>; "
        f"wartości portfela podane w zł.</div></div>"
    )

    # ── Performance vs SPY ──
    tracker = ctx.get("tracker_section_html", "")
    if tracker:
        P.append(_card_open("Wynik vs rynek (benchmark SPY)",
                            "Czy bot bije rynek. Edge w pp = przewaga nad zwykłym trzymaniem SPY. "
                            "Czerwony alert = strategia może być poza swoim cyklem.") + tracker + "</div>")

    # ── I. AKCJE ──
    decisions = ctx.get("position_decisions") or []
    picks = ctx.get("accepted_picks") or []
    if decisions or picks:
        P.append(_card_open("I. Akcje do wykonania na dziś",
                            "Wykonaj po kolei w xStation 5. Każdy blok mówi co zrobić i dlaczego."))
        for d in decisions:
            action = d.get("action", "")
            extra_raw = d.get("extra_html", "")
            stop_changed = "Sell Stop" in extra_raw or "stop" in extra_raw.lower()
            if action.startswith("SELL"):
                bar, ocol = GOLD_DK, GOLD_DK
                if action == "SELL_ALL":
                    order = "Sprzedaj całość po cenie rynkowej"
                else:
                    order = "Sprzedaj część — resztę zostaw biegnącą"
                do_html = extra_raw.lstrip(" ·") or "Bez zmian wielkości pozycji."
            else:
                # HOLD: dwa warianty w zależności od tego, czy bot faktycznie podnosi stop
                bar, ocol = GREY, GREY
                if stop_changed:
                    order = "Trzymaj — podnieś Sell Stop"
                    do_html = extra_raw.lstrip(" ·") or "Bez zmian wielkości pozycji."
                else:
                    order = "Trzymaj — bez zmian"
                    do_html = "Nic nie rób. Pozycja biegnie dalej, Sell Stop bez zmian."
            P.append(_action_block(
                company=d.get("ticker", ""), order=f"Zlecenie: {order}", order_color=ocol, bar_color=bar,
                do_html=do_html,
                why_html=d.get("reason", ""),
            ))
        if picks:
            n = len(picks)
            total = sum(p.get("value_pln", 0) for p in picks)
            P.append(f"<div style='{SANS}font-size:11.5pt;color:{MUTED};font-weight:bold;"
                     f"letter-spacing:0.5px;margin:8px 0 14px;'>"
                     f"PORTFEL DO OTWARCIA — {n} {'pozycja' if n==1 else 'pozycje' if n<5 else 'pozycji'} "
                     f"(razem ~{total:,.0f} zł)</div>")
            for p in picks:
                lens = p.get("lens_text", "")
                why = lens if lens else (f"Pozycja ~{p.get('value_pln',0):,.0f} zł "
                                         f"({p.get('pct',0):.0f}% portfela) wg rankingu momentum.")
                P.append(_action_block(
                    company=p.get("ticker", ""),
                    order="Zlecenie: Kup po cenie rynkowej + ustaw osobny Sell Stop",
                    order_color=GREEN, bar_color=GREEN,
                    do_html=p.get("directives_html", ""),
                    why_html=why,
                    badges_html=render_badges(p.get("badges")),
                    sources_html=render_sources(p.get("sources")),
                ))
        P.append("</div>")
    else:
        P.append(_card_open("I. Akcje do wykonania na dziś") +
                 f"<div style='{SANS}font-size:13pt;color:{TEXT};line-height:1.6;'>"
                 f"<b>Dziś nie kupujemy.</b> Żadna spółka nie przeszła wszystkich filtrów "
                 f"(momentum, bezpieczniki, radar). To normalne — bot woli czekać w gotówce "
                 f"niż wymuszać słabą pozycję. Trzymaj obecny portfel.</div></div>")

    # ── II. PORTFEL PO TRANSAKCJACH (tabela) ──
    portfolio_rows = ctx.get("portfolio_rows") or []
    if portfolio_rows:
        total_p = ctx.get("portfolio_total_pln", sum(r.get("value_pln", 0) for r in portfolio_rows))
        P.append(
            _card_open("II. Tak ma wyglądać portfel po transakcjach",
                       "Docelowy stan konta po wykonaniu dzisiejszych zleceń. "
                       "Porównaj z tym, co masz w xStation — powinno się zgadzać.") +
            _portfolio_table(portfolio_rows, total_p, ctx.get("cash_pln", 0),
                             ctx.get("portfolio_beta"), ctx.get("portfolio_beta_note", "")) +
            "</div>"
        )

    # ── III. SMART MONEY (ciemna) ──
    sm_lines = []
    for p in picks:
        for b in (p.get("badges") or []):
            if b and b[0].startswith("sm_"):
                sm_lines.append(f"<b style='color:{GOLD};'>{p.get('ticker','')}</b> — {b[1]}")
    if sm_lines:
        P.append(_card_open("III. Porsche Shadow (radar smart money)",
                            "Potwierdzenie z ruchów insiderów, funduszy i polityków. "
                            "Nigdy nie jest powodem zakupu sam w sobie — to dodatkowy głos za zakupem.",
                            top_color=INDIGO, dark=True))
        P.append(f"<div style='{SANS}font-size:12.5pt;line-height:1.8;color:#e2e8f0;'>" +
                 "<br>".join(f"• {ln}" for ln in sm_lines) + "</div></div>")

    # ── IV. RADAR MAKRO ──
    P.append(
        _card_open("IV. Radar makro (ochrona przed krachem)",
                   "Mierzy ryzyko rynkowe. Im wyżej, tym ostrożniej bot kupuje.") +
        f"<div style='{SERIF}font-size:19pt;font-weight:bold;color:{radar_color};'>"
        f"Poziom {radar}/3 · {radar_status}</div>"
        f"<div style='{SANS}font-size:12.5pt;color:{TEXT};margin-top:10px;line-height:1.6;'>"
        f"{radar_desc}</div></div>"
    )

    # ── Radar obserwacyjny ──
    watch = ctx.get("radar_watch") or []
    if watch:
        rows = ""
        for tk, sektor, powod in watch:
            rows += (f"<tr><td style='padding:12px 8px 12px 0;border-bottom:1px solid {LINE2};"
                     f"{SANS}font-size:13pt;'><b style='color:{DARK};'>{tk}</b></td>"
                     f"<td style='padding:12px 8px;border-bottom:1px solid {LINE2};color:{MUTED};"
                     f"{SANS}font-size:12pt;'>{sektor}</td>"
                     f"<td style='padding:12px 0;border-bottom:1px solid {LINE2};color:{MUTED};"
                     f"{SANS}font-size:12pt;'>{powod}</td></tr>")
        P.append(
            _card_open("V. Radar obserwacyjny",
                       "Spółki blisko progu wejścia — jeszcze nie kupujemy, ale obserwujemy.") +
            f"<table style='width:100%;border-collapse:collapse;'>"
            f"<thead><tr>"
            f"<th style='text-align:left;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"border-bottom:2px solid {LINE};padding-bottom:10px;'>Walor</th>"
            f"<th style='text-align:left;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"border-bottom:2px solid {LINE};padding-bottom:10px;'>Sektor</th>"
            f"<th style='text-align:left;color:{MUTED};font-size:10pt;text-transform:uppercase;"
            f"border-bottom:2px solid {LINE};padding-bottom:10px;'>Powód</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )

    # ── VI. RADAR OKAZJI ULTRA (moonshoty — obserwuj, nie auto-kup) ──
    opp = ctx.get("opportunity_radar") or {}
    if opp and not opp.get("_empty"):
        sleeve = ctx.get("moonshot_sleeve_pln", 0)
        equity_for_sleeve = ctx.get("equity_pln", 0)
        sleeve_pct = (sleeve / equity_for_sleeve * 100) if equity_for_sleeve else 0
        blocks = ""
        # Kontrakty (najmocniejszy sygnał) -> wolumen -> IPO
        kind_emoji = {"KONTRAKT": "💰", "WOLUMEN": "📈", "IPO": "🆕"}
        kind_title = {"KONTRAKT": "MAŁA SPÓŁKA + OGROMNY KONTRAKT",
                      "WOLUMEN": "NIETYPOWY WOLUMEN / WYBICIE", "IPO": "ŚWIEŻE IPO"}
        seen_kinds = []
        for o in opp.get("all", []):
            kind = o.get("kind", "")
            if kind not in seen_kinds:
                seen_kinds.append(kind)
                blocks += (f"<div style='{SANS}font-size:10pt;letter-spacing:1px;text-transform:uppercase;"
                           f"color:{GOLD_DK};font-weight:bold;margin:16px 0 8px;'>"
                           f"{kind_emoji.get(kind,'•')} {kind_title.get(kind, kind)}</div>")
            risk = o.get("risk", "WYSOKIE")
            risk_col = RED if "BARDZO" in risk else GOLD_DK
            blocks += (
                f"<div style='background:#fffbeb;border-left:3px solid {GOLD};border-radius:6px;"
                f"padding:12px 14px;margin-bottom:8px;'>"
                f"<b style='{SANS}font-size:13.5pt;color:{DARK};'>{o.get('ticker','')}</b> "
                f"<span style='{SANS}font-size:11.5pt;color:{TEXT};'>— {o.get('note','')}</span><br>"
                f"<span style='{SANS}font-size:11pt;color:{GREEN};font-weight:bold;'>→ {o.get('label','OBSERWUJ')}</span> "
                f"<span style='{SANS}font-size:10.5pt;color:{risk_col};'>· Ryzyko: {risk}</span></div>")
        # Temat + lockup (informacyjnie)
        theme = opp.get("theme") or []
        if theme:
            tnames = ", ".join(str(t.get("ticker", t) if isinstance(t, dict) else t) for t in theme[:5])
            blocks += (f"<div style='{SANS}font-size:11pt;color:{TEXT};margin-top:12px;'>"
                       f"🔥 <b>Liderzy tematu:</b> {tnames} → OBSERWUJ</div>")
        lockup = opp.get("lockup") or []
        if lockup:
            lnames = ", ".join(f"{l.get('ticker','?')} (za {l.get('days_until','?')}d)"
                               if isinstance(l, dict) else str(l) for l in lockup[:3])
            blocks += (f"<div style='{SANS}font-size:11pt;color:{MUTED};margin-top:6px;'>"
                       f"⏳ <b>Lockup:</b> {lnames} → ryzyko podaży insiderów</div>")
        P.append(
            _card_open("VI. Radar Okazji Ultra  ⚠ spekulacja — obserwuj, nie auto-kup",
                       "Spółki przed potencjalnie dużym ruchem. Loteryjne zakłady z barbella — "
                       "decyzja należy do Ciebie.") +
            f"<div style='{SANS}font-size:11.5pt;color:{MUTED};margin-bottom:6px;'>"
            f"Sleeve moonshot: {sleeve:,.0f} zł / {equity_for_sleeve:,.0f} zł "
            f"({sleeve_pct:.0f}% · barbell)</div>"
            f"{blocks}"
            f"<div style='{SANS}font-size:10.5pt;color:{MUTED};line-height:1.6;border-top:1px solid {LINE2};"
            f"margin-top:14px;padding-top:12px;'><b>Zasada:</b> max 1 moonshot = €10-43 zł (próg XTB). "
            f"Akceptujesz −100% tej pozycji. Stop −50% lub brak. Winner bez górnego capa. "
            f"Rdzeń (91%) pracuje niezależnie w sekcji I.</div></div>"
        )

    # ── VII. CEL ──
    goal = ctx.get("goal_pln", 1_700_000)
    equity = ctx.get("equity_pln", 0)
    pct_goal = min(100.0, (equity / goal * 100) if goal else 0)
    P.append(
        f"<div style='background:{DARK};border-radius:14px;padding:26px 30px;'>"
        f"<div style='{SANS}font-size:10.5pt;letter-spacing:2px;text-transform:uppercase;"
        f"color:{GOLD};font-weight:bold;margin-bottom:12px;'>VII. Metryka celu</div>"
        f"<div style='{SERIF}font-size:23pt;font-weight:bold;color:#ffffff;'>"
        f"{equity:,.0f} / {goal:,.0f} PLN</div>"
        f"<div style='{SANS}font-size:11pt;color:{MUTED2};margin-top:6px;'>"
        f"Cel główny (Porsche): {pct_goal:.2f}% drogi za nami</div>"
        f"<div style='background:#334155;border-radius:999px;height:8px;margin:10px 0 18px;width:100%;'>"
        f"<div style='background:{GOLD};height:8px;border-radius:999px;width:{max(pct_goal,0.4):.2f}%;'></div></div>"
        f"<div style='{SANS}font-size:11pt;color:{MUTED2};line-height:1.65;border-top:1px solid #334155;"
        f"padding-top:14px;'><b style='color:#e2e8f0;'>Zasada nadrzędna:</b> składanie realnych zysków "
        f"bije pojedynczy strzał. Bot realizuje zyski systematycznie, ratchet podnosi stop za zyskiem, "
        f"zero dźwigni. Konto IKE pozostaje nietykalne.</div></div>"
    )

    # ── STOPKA ──
    P.append(
        f"<div style='{SANS}font-size:10pt;color:{MUTED2};text-align:center;padding:22px 0;line-height:1.6;'>"
        f"Cel: {goal:,.0f} zł. Zautomatyzowana synteza — logika i egzekucja na XTB to Twoja "
        f"odpowiedzialność. To nie porada inwestycyjna.<br>"
        f"<em>Wartości portfela w zł. Ceny akcji i Sell Stop wpisujesz w xStation w USD.</em></div>"
    )

    P.append("</div></div>")
    return "".join(P)


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

    check("badge pill kolor", "background:#dcfce7" in render_badge(("news_bull", "x")))
    check("badge None -> ''", render_badge(None) == "")
    check("badges lista", "Świeży" in render_badges([("news_bull", "Świeży")]))
    check("sources link", "href" in render_sources([("Reuters", "https://r.com")]))

    rows = [
        {"ticker": "MU", "value_pln": 2245, "pct": 47, "sector": "HBM / AI Memory", "stop_usd": 92.0},
        {"ticker": "AVGO", "value_pln": 1170, "pct": 25, "sector": "Custom ASIC", "stop_usd": 192.0},
    ]
    tbl = _portfolio_table(rows, 3415, 0, beta=1.82, beta_note="agresywna koncentracja tech")
    check("tabela: ma MU", "MU" in tbl)
    check("tabela: beta", "Beta portfela: 1.82" in tbl)
    check("tabela: wartość aktywów", "Wartość aktywów" in tbl)

    ctx = {
        "today": "2026-05-28", "tag": "", "cash_pln": 0, "equity_pln": 4740, "radar_level": 0,
        "tracker_section_html": "<p>Edge +5.5 pp</p>", "halted": False,
        "position_decisions": [{"ticker": "AVGO", "action": "SELL_PARTIAL",
                                "extra_html": "Sprzedaj 25%, przesuń stop do 192 USD.",
                                "reason": "Sektor traci momentum."}],
        "accepted_picks": [{
            "idx": 1, "ticker": "MU", "value_pln": 560, "pct": 35,
            "directives_html": "Kup 1.00 akcji MU<br>Sell Stop 134 USD",
            "badges": [("news_bull", "Świeży katalizator ↑"), ("sm_conv", "Smart money: KONWERGENCJA (8/10)")],
            "lens_text": "Kontrakt na HBM.", "sources": [("Reuters", "https://r.com")],
        }],
        "portfolio_rows": rows, "portfolio_total_pln": 3415, "portfolio_beta": 1.82,
        "portfolio_beta_note": "agresywna koncentracja tech",
        "radar_watch": [("AMD", "GPU", "blisko progu")],
        "goal_pln": 1_700_000,
    }
    html = build_email_html(ctx)
    check("mail: brief", "Brief inwestycyjny" in html)
    check("mail: jak czytać", "Jak czytać ten raport" in html)
    check("mail: PORTFEL DO OTWARCIA", "PORTFEL DO OTWARCIA" in html)
    check("mail: tabela portfela", "Tak ma wyglądać portfel" in html)
    check("mail: beta w tabeli", "1.82" in html)
    check("mail: co zrobić", "Co zrobić" in html)
    check("mail: dlaczego", "Dlaczego" in html)
    check("mail: zł", "zł" in html)
    check("mail: ticker MU", "MU" in html)
    check("mail: plakietka news", "Świeży katalizator" in html)
    check("mail: smart money card", "Porsche Shadow" in html)
    check("mail: radar", "Poziom 0/3" in html)
    check("mail: metryka celu", "Metryka celu" in html)

    ctx2 = dict(ctx); ctx2["accepted_picks"] = []; ctx2["position_decisions"] = []
    check("brak picków -> nie kupujemy", "nie kupujemy" in build_email_html(ctx2))
    ctx3 = dict(ctx); ctx3["halted"] = True; ctx3["halt_reason"] = "delta"
    check("halt -> BOT ZATRZYMANY", "BOT ZATRZYMANY" in build_email_html(ctx3))

    print(f"WYNIK email_render: {passed} OK, {failed} FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    print("email_render.py — render brief inwestycyjny. Użyj --selftest.")
