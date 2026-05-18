"""
VACKER daglig försäljningsrapport
─────────────────────────────────
Hämtar dagens försäljning per butik från Hicore (skriptet är tänkt att köras
sent på kvällen när butikerna stängt), jämför mot samma veckodag förra året,
bygger en polerad PNG-rapport och postar i Slack.
Skämtraden längst ner genereras färskt varje dag via Claude.

Kör manuellt:   python report.py
Kör för datum: python report.py 2024-12-15
Schemalagd:    se .github/workflows/daily.yml
"""

from __future__ import annotations

import os
import sys
import json
import tempfile
import subprocess
import datetime as dt
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from jinja2 import Template
import anthropic
from slack_sdk import WebClient

# ─── konfiguration ────────────────────────────────────────────────────────
load_dotenv()

HICORE_BASE      = os.getenv("HICORE_BASE_URL", "https://hicoredrift5.hicorecloud.se/HiCoreApi")
HICORE_STORE_ID  = os.environ["HICORE_STORE_ID"]
HICORE_API_KEY   = os.environ["HICORE_API_KEY"]
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN  = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL    = os.getenv("SLACK_CHANNEL", "#all-vacker")

# Butiker som ska EXKLUDERAS från rapporten (utöver rader med 0 i försäljning)
SKIP_STORES = {"lager", "vacker e-handel", "e-handel"}

SCRIPT_DIR    = Path(__file__).parent
TEMPLATE_PATH = SCRIPT_DIR / "report_template.html"
OUT_DIR       = SCRIPT_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)


# ─── datum­hjälpare ────────────────────────────────────────────────────────
SE_MONTHS = ["januari", "februari", "mars", "april", "maj", "juni",
             "juli", "augusti", "september", "oktober", "november", "december"]

def today() -> dt.date:
    return dt.date.today()

def same_weekday_prev_year(d: dt.date) -> dt.date:
    """52 veckor bakåt = samma veckodag förra året."""
    return d - dt.timedelta(weeks=52)

def format_sv_date(d: dt.date) -> str:
    return f"{d.day} {SE_MONTHS[d.month-1]} {d.year}"

def parse_target_date() -> dt.date:
    """CLI: python report.py 2024-12-15  →  använd det datumet.
    Utan argument används DAGENS datum (skriptet körs på kvällen)."""
    if len(sys.argv) > 1:
        try:
            return dt.date.fromisoformat(sys.argv[1])
        except ValueError:
            sys.exit(f"Ogiltigt datum: {sys.argv[1]} (förväntat YYYY-MM-DD)")
    return today()


# ─── Hicore API ───────────────────────────────────────────────────────────
def fetch_dashboard(start: str, end: str, dtf: int) -> list[dict]:
    url = f"{HICORE_BASE}/{HICORE_STORE_ID}/Report/DashBoard"
    params = {
        "StartDateTime":  start,
        "EndDateTime":    end,
        "DateTimeFilter": dtf,
        "apikey":         HICORE_API_KEY,
    }
    r = requests.get(url, params=params, timeout=90)
    r.raise_for_status()
    return r.json()

def fetch_all_periods(target: dt.date, prev: dt.date) -> dict:
    """8 parallella Dashboard-anrop: 4 perioder × 2 år."""
    day_now_s, day_now_e   = f"{target}T00:00:00", f"{target}T23:59:59"
    day_prev_s, day_prev_e = f"{prev}T00:00:00",   f"{prev}T23:59:59"
    t, p = target.isoformat(), prev.isoformat()

    jobs = {
        "day_now":   (day_now_s,  day_now_e,  0),
        "day_prev":  (day_prev_s, day_prev_e, 0),
        "week_now":  (t, t, 1),
        "week_prev": (p, p, 1),
        "mon_now":   (t, t, 2),
        "mon_prev":  (p, p, 2),
        "yr_now":    (t, t, 3),
        "yr_prev":   (p, p, 3),
    }
    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_dashboard, s, e, d): k for k,(s,e,d) in jobs.items()}
        for f in futures:
            out[futures[f]] = f.result()
    return out


# ─── aggregering ──────────────────────────────────────────────────────────
def keep_real_stores(rows: list[dict]) -> list[dict]:
    """Filtrera bort Lager, e-handel och rader utan försäljning."""
    out = []
    for r in rows:
        if (r.get("SaleExVAT") or 0) <= 0:
            continue
        name = (r.get("Store") or {}).get("Name", "").lower()
        clean = name.lstrip("0123456789 ").strip()
        if clean in SKIP_STORES:
            continue
        out.append(r)
    return out

def sum_field(rows, field):  return sum((r.get(field) or 0) for r in rows)

def pct(now, prev) -> float:
    return 0.0 if not prev else ((now - prev) / prev) * 100


# ─── formattering ─────────────────────────────────────────────────────────
NBSP = "\u00a0"

def fmt_kr(value) -> str:
    v = int(round(value))
    sign = "−" if v < 0 else ""
    n = f"{abs(v):,}".replace(",", NBSP)
    return f"{sign}{n}{NBSP}kr"

def fmt_pct(value: float) -> str:
    if value > 0.05:   sign = "+"
    elif value < -0.05: sign = "−"
    else:               sign = ""
    v = abs(value)
    s = f"{v:.1f}".replace(".", ",") if v < 10 else f"{v:.0f}"
    return f"{sign}{s}%"

def class_for(value: float) -> str:
    if value >  0.5: return "pos"
    if value < -0.5: return "neg"
    return "zero"


# ─── skämtgenerering ──────────────────────────────────────────────────────
FALLBACK_JOKES = [
    "«Varje krona räknas — och vi räknar bra!»",
    "«Hår av guld, siffror av platina.»",
    "«Vi klipper kostnaderna, aldrig kunderna.»",
    "«Volym i håret, volym i kassan.»",
    "«Bra hår, bra dag, bra siffror — i den ordningen.»",
]

def generate_joke(mood_hint: str) -> str:
    """En färsk one-liner från Claude. Faller tillbaka på fast lista vid fel."""
    if not ANTHROPIC_KEY:
        import random
        return random.choice(FALLBACK_JOKES)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        prompt = (
            "Skriv EN ENDA kort, lättsam svensk one-liner till botten av en "
            "daglig försäljningsrapport från frisörsalongskedjan VACKER. "
            "Temat: hår, skönhet, frisering, färgning eller blondiner "
            "(blondinskämt får förekomma men snälla — blondinen är gärna "
            "hjälten, aldrig dum). Max ~12 ord. Inom franska citationstecken "
            "(« och »). Inga emojis. INGEN preamble — bara raden själv. "
            f"Dagens stämning i siffrorna: {mood_hint}."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip().strip('"').strip("'")
        if not text.startswith("«"):
            text = "«" + text.lstrip("«").rstrip("»") + "»"
        return text
    except Exception as e:
        print(f"Skämtgenereringen krånglade ({e}); använder fallback.", file=sys.stderr)
        import random
        return random.choice(FALLBACK_JOKES)


# ─── rendering ────────────────────────────────────────────────────────────
def render_html(ctx: dict) -> str:
    return Template(TEMPLATE_PATH.read_text(encoding="utf-8")).render(**ctx)

def render_png(html: str, output_path: Path) -> None:
    """HTML → PNG via Playwright (Python)."""
    from playwright.sync_api import sync_playwright
    html_file = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
    html_file.write(html); html_file.close()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(device_scale_factor=2, viewport={"width": 960, "height": 1400})
            page = ctx.new_page()
            page.goto(f"file://{html_file.name}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)
            page.screenshot(path=str(output_path), full_page=True)
            browser.close()
    finally:
        Path(html_file.name).unlink(missing_ok=True)


# ─── Slack ────────────────────────────────────────────────────────────────
def post_to_slack(png_path: Path, summary: str) -> None:
    client = WebClient(token=SLACK_BOT_TOKEN)
    client.files_upload_v2(
        channel=SLACK_CHANNEL,
        file=str(png_path),
        title="VACKER daglig försäljningsrapport",
        initial_comment=summary,
    )


# ─── main ─────────────────────────────────────────────────────────────────
def main():
    target = parse_target_date()
    prev   = same_weekday_prev_year(target)
    print(f"Rapport för {target} (jämfört mot {prev})")

    data = fetch_all_periods(target, prev)

    # Dagliga totaler för header
    day_now  = keep_real_stores(data["day_now"])
    brutto   = sum_field(day_now, "SaleInclVAT")
    netto    = sum_field(day_now, "SaleExVAT")

    # KPI-procent per period
    def period_pct(now_k, prev_k):
        n = sum_field(keep_real_stores(data[now_k]),  "SaleExVAT")
        p = sum_field(keep_real_stores(data[prev_k]), "SaleExVAT")
        return pct(n, p)

    pct_day   = period_pct("day_now",   "day_prev")
    pct_week  = period_pct("week_now",  "week_prev")
    pct_month = period_pct("mon_now",   "mon_prev")
    pct_year  = period_pct("yr_now",    "yr_prev")

    # Butikstabell baserad på MÅNADSDATA
    mon_now  = keep_real_stores(data["mon_now"])
    mon_prev_by_id = {(r.get("Store") or {}).get("Id"): r for r in data["mon_prev"]}

    rows = []
    for s in mon_now:
        prev_s   = mon_prev_by_id.get((s.get("Store") or {}).get("Id"), {})
        ex_now   = s.get("SaleExVAT") or 0
        ex_prev  = prev_s.get("SaleExVAT") or 0
        diff_kr  = ex_now - ex_prev
        diff_pct = pct(ex_now, ex_prev)
        name     = (s.get("Store") or {}).get("Name", "").lstrip("0123456789 ").strip()
        rows.append({
            "name": name,
            "raw_pct": diff_pct,
            "diff_pct":      fmt_pct(diff_pct),
            "diff_pct_class": class_for(diff_pct),
            "forsaljning":   fmt_kr(ex_now),
            "diff_kr":       fmt_kr(diff_kr),
            "diff_kr_class": class_for(diff_kr),
        })
    rows.sort(key=lambda r: r["raw_pct"], reverse=True)

    total_now  = sum((s.get("SaleExVAT") or 0) for s in mon_now)
    total_prev = sum((mon_prev_by_id.get((s.get("Store") or {}).get("Id"), {}).get("SaleExVAT") or 0) for s in mon_now)
    total_diff = total_now - total_prev
    total_pct  = pct(total_now, total_prev)

    # Skämtraden
    if   pct_month >  5: mood = "väldigt bra — vi krossar förra året"
    elif pct_month >  0: mood = "lite plus mot förra året"
    elif pct_month > -5: mood = "lugnt, något minus mot förra året"
    else:                mood = "tufft, ordentligt minus mot förra året"
    joke = generate_joke(mood)

    # Bygg HTML
    ctx = {
        "report_date":    format_sv_date(target),
        "brutto_kr":      fmt_kr(brutto),
        "netto_kr":       fmt_kr(netto),
        "pct_day":        fmt_pct(pct_day),    "pct_day_class":   class_for(pct_day),
        "pct_week":       fmt_pct(pct_week),   "pct_week_class":  class_for(pct_week),
        "pct_month":      fmt_pct(pct_month),  "pct_month_class": class_for(pct_month),
        "pct_year":       fmt_pct(pct_year),   "pct_year_class":  class_for(pct_year),
        "stores_title":   f"Butiker — {SE_MONTHS[target.month-1].capitalize()} {target.year}",
        "stores":         rows,
        "total_forsaljning":   fmt_kr(total_now),
        "total_pct":           fmt_pct(total_pct),
        "total_pct_class":     class_for(total_pct),
        "total_diff_kr":       fmt_kr(total_diff),
        "total_diff_kr_class": class_for(total_diff),
        "joke":           joke,
    }
    html = render_html(ctx)
    png_path = OUT_DIR / f"vacker_{target}.png"
    render_png(html, png_path)
    print(f"Rapport renderad: {png_path}")

    summary = (
        f"*VACKER försäljningsrapport — {format_sv_date(target)}*\n"
        f"Brutto {fmt_kr(brutto)}  ·  Netto {fmt_kr(netto)}\n"
        f"Dag {fmt_pct(pct_day)}  ·  Vecka {fmt_pct(pct_week)}  ·  "
        f"Månad {fmt_pct(pct_month)}  ·  År {fmt_pct(pct_year)}"
    )

    if SLACK_BOT_TOKEN:
        post_to_slack(png_path, summary)
        print(f"Postad i Slack ({SLACK_CHANNEL})")
    else:
        print("SLACK_BOT_TOKEN saknas — hoppar över Slack-postning.")


if __name__ == "__main__":
    main()
