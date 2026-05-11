from flask import Flask, render_template, jsonify, abort, send_from_directory, request
import pandas as pd
import math
import os
import base64
from datetime import datetime
from pathlib import Path
from config import PRODUCTS, LIVE_XLSX_PATH, LIVE_XLSX_SHEET, REFRESH_SECONDS, DATA_DIR

app = Flask(__name__)


@app.context_processor
def _inject_admin_state():
    """Expose `is_admin` and a helper `admin_url(endpoint)` to all templates.
    A request is treated as admin when ?p=ADMIN_PASSWORD is present.
    """
    from flask import url_for as _u
    pw = request.args.get("p") if request else None
    is_admin = bool(ADMIN_PASSWORD) and pw == ADMIN_PASSWORD

    def admin_url(endpoint):
        try:
            return _u(endpoint) + (f"?p={pw}" if is_admin else "")
        except Exception:
            return "#"

    return {"is_admin": is_admin, "admin_p": pw if is_admin else "", "admin_url": admin_url}


def _secret(name: str, default: str = None) -> str:
    """Read a secret from env vars first, then Render Secret Files (/etc/secrets/<name>)."""
    val = os.getenv(name)
    if val:
        return val
    p = Path("/etc/secrets") / name
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return default
    return default


ADMIN_PASSWORD = _secret("ADMIN_PASSWORD")
GH_PAT = _secret("GH_PAT")
GH_REPO = _secret("GH_REPO", "aboaziz112211/trading_suite")
GH_BRANCH = _secret("GH_BRANCH", "main")
GH_XLSX_PATH = "data/all.xlsx"
GH_CSV_US_PATH = "data/chartedge_us.csv"
GH_CSV_SA_PATH = "data/chartedge_sa.csv"
GH_PDF_PATHS = {
    "chartedge":      "data/guide_chartedge.pdf",
    "tradepulse_us":  "data/guide_tradepulse_us.pdf",
    "tradepulse_sar": "data/guide_tradepulse_sar.pdf",
}


def _detect_csv_market(filename: str):
    """Return 'us' or 'sa' based on filename markers, else None."""
    n = filename.lower()
    if any(k in n for k in ["_us.", "_us_", "-us.", "-us-", "us_stock", "us_screen", "_usa.", "_usa_"]):
        return "us"
    if any(k in n for k in ["_sa.", "_sa_", "-sa.", "-sa-", "_sar.", "_sar_", "tasi", "saudi", "_ksa."]):
        return "sa"
    return None


def _detect_csv_market_from_content(raw: bytes) -> str:
    """Peek at the first column of the CSV; mostly 4-digit numeric tickers -> Saudi."""
    try:
        text = raw[:8192].decode("utf-8", errors="ignore")
    except Exception:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    first_cells = []
    for ln in lines[1:11]:  # skip header, sample up to 10 rows
        cell = ln.split(",")[0].strip().strip('"').strip("'")
        if cell:
            first_cells.append(cell)
    if not first_cells:
        return None
    numeric = sum(1 for c in first_cells if c.isdigit() and len(c) == 4)
    return "sa" if (numeric / len(first_cells)) >= 0.6 else "us"


def _resolve_upload_target(filename: str, market_form: str = None, raw: bytes = None,
                            pdf_target: str = None):
    """Decide where the uploaded file should be committed in the GitHub repo.

    Priority for CSV:
      1. Explicit market dropdown ('us' or 'sa')
      2. Filename markers ('_US', '_SA', etc.)
      3. CSV content sniff (numeric Tadawul codes -> Saudi, else US)

    For PDFs, the 'pdf_target' dropdown picks the product slot.
    """
    n = filename.lower()
    if n.endswith(".xlsx"):
        return GH_XLSX_PATH, None
    if n.endswith(".pdf"):
        target = (pdf_target or "").lower().strip()
        if target in GH_PDF_PATHS:
            return GH_PDF_PATHS[target], None
        return None, ("Pick a product (ChartEdge / TradePulse US / TradePulse SAR) "
                      "from the dropdown when uploading a PDF guide.")
    if not n.endswith(".csv"):
        return None, "Upload a .xlsx, .csv, or .pdf file."

    market = (market_form or "").lower().strip()
    if market not in ("us", "sa"):
        market = _detect_csv_market(filename) or ""
    if market not in ("us", "sa") and raw is not None:
        market = _detect_csv_market_from_content(raw) or ""
    if market == "us":
        return GH_CSV_US_PATH, None
    if market == "sa":
        return GH_CSV_SA_PATH, None
    return None, ("Could not determine market for this CSV. "
                  "Pick US or Saudi from the Market dropdown and try again.")

US_PUSH_TOKEN = _secret("US_PUSH_TOKEN")
SA_PUSH_TOKEN = _secret("SA_PUSH_TOKEN")
# in-memory live US feed: bytes of xlsx + last update timestamp
_US_STATE = {"xlsx": None, "updated": None, "rows": 0}
# in-memory live SA feed: raw xlsx bytes pushed by sar_feed.py from the user's PC
_SA_STATE = {"xlsx": None, "updated": None, "size": 0}

# us_feed.Row → Bloomberg-style column mapping
_US_COLUMNS = [
    "Ticker", "Last Price", "Last Price.1", "%1D", "%1M", "%YTD",
    "Prev Cls", "52W High", "52W Low", "Volume", "Open", "Low", "High", "VWAP",
    "Avg Vol 30D", "Bid", "Ask", "MA 200D Pct Chg",
    "30D Hi", "30D Low", "MA200_22d_ago", "Mov Avg 50", "MA 200D",
    "Sector", "Industry",
]


def _us_rows_to_xlsx(rows):
    """Build a Bloomberg-style xlsx (bytes) from a list of us_feed.Row dicts."""
    import io
    out = []
    for r in rows or []:
        price = r.get("price")
        ma200d = None
        if price is not None and r.get("ma200ChgPct") is not None:
            try:
                ma200d = price / (1 + (r["ma200ChgPct"] / 100.0))
            except Exception:
                ma200d = None
        out.append({
            "Ticker": r.get("ticker"),
            "Last Price": price,
            "Last Price.1": price,
            "%1D": r.get("chg"),
            "%1M": r.get("chg1m"),
            "%YTD": r.get("chgYtd"),
            "Prev Cls": r.get("prevCls"),
            "52W High": r.get("high52"),
            "52W Low": r.get("low52"),
            "Volume": r.get("vol"),
            "Open": r.get("dayOpen"),
            "Low": r.get("dayLow"),
            "High": r.get("dayHigh"),
            "VWAP": r.get("vwap"),
            "Avg Vol 30D": r.get("avgVol"),
            "Bid": r.get("bid"),
            "Ask": r.get("ask"),
            "MA 200D Pct Chg": r.get("ma200ChgPct"),
            "30D Hi": r.get("hi30d"),
            "30D Low": r.get("lo30d"),
            "MA200_22d_ago": r.get("ma200_22d"),
            "Mov Avg 50": r.get("ma50"),
            "MA 200D": ma200d,
            "Sector": r.get("sector"),
            "Industry": r.get("industry"),
        })
    df = pd.DataFrame(out, columns=_US_COLUMNS)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="Sheet 1")
    return buf.getvalue()


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def load_live_data():
    # Prefer the live in-memory feed pushed by sar_feed.py; fall back to disk.
    if _SA_STATE.get("xlsx"):
        import io
        df = pd.read_excel(io.BytesIO(_SA_STATE["xlsx"]), sheet_name=LIVE_XLSX_SHEET)
    elif LIVE_XLSX_PATH.exists():
        df = pd.read_excel(LIVE_XLSX_PATH, sheet_name=LIVE_XLSX_SHEET)
    else:
        return {"error": f"xlsx not found: {LIVE_XLSX_PATH}", "rows": [], "columns": []}
    df.columns = [str(c) for c in df.columns]
    df = df.dropna(axis=1, how="all")
    bad = df.columns.str.startswith("Unnamed") | df.columns.str.lower().isin(["nan", "nat"])
    df = df.loc[:, ~bad]
    df = df.dropna(subset=[df.columns[0]])
    rows = [{c: _clean(v) for c, v in r.items()} for r in df.to_dict(orient="records")]
    return {
        "columns": list(df.columns),
        "rows": rows,
        "row_count": len(rows),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


_YAHOO_CACHE = {"ts": 0.0, "data": None}
_YAHOO_TTL = 30 * 60  # 30 min


def fetch_tasi_yahoo():
    """Fetch TASI 5-day daily history from Yahoo Finance. Cached 30 min."""
    import time as _t
    import urllib.request as _ur
    now = _t.time()
    if _YAHOO_CACHE["data"] and (now - _YAHOO_CACHE["ts"]) < _YAHOO_TTL:
        return _YAHOO_CACHE["data"]
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETASI.SR?range=5d&interval=1d"
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=15) as resp:
            d = __import__("json").loads(resp.read().decode("utf-8"))
        r = (d.get("chart", {}).get("result") or [None])[0]
        if not r:
            return None
        ts = r.get("timestamp", [])
        q = (r.get("indicators", {}).get("quote") or [{}])[0]
        sessions = []
        for i, t in enumerate(ts):
            sessions.append({
                "date": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"),
                "open": (q.get("open") or [None])[i] if i < len(q.get("open") or []) else None,
                "high": (q.get("high") or [None])[i] if i < len(q.get("high") or []) else None,
                "low":  (q.get("low")  or [None])[i] if i < len(q.get("low")  or []) else None,
                "close": (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None,
                "volume": (q.get("volume") or [None])[i] if i < len(q.get("volume") or []) else None,
            })
        out = {"sessions": [s for s in sessions if s["close"] is not None]}
        if out["sessions"]:
            out["today"] = out["sessions"][-1]
            out["prev"] = out["sessions"][-2] if len(out["sessions"]) >= 2 else None
        _YAHOO_CACHE.update({"ts": now, "data": out})
        return out
    except Exception:
        return None


@app.route("/")
def index():
    return render_template("index.html", products=PRODUCTS)


@app.route("/contact")
def contact():
    return render_template(
        "contact.html",
        tg_channel_url="https://t.me/+R_m5lVLFnBJhYWJk",
        tg_admin_url="https://t.me/chartedgeai",
        tg_admin_handle="@chartedgeai",
    )


def _compute_brief_data():
    """Build a unified data bundle for both /report and /agent."""
    yahoo = fetch_tasi_yahoo()

    # Build scan summary from chartedge_sa.csv (in-memory if pushed, else disk)
    import csv as _csv
    import io as _io
    csv_path = DATA_DIR / "chartedge_sa.csv"
    rows = []
    if csv_path.exists():
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
        except Exception:
            rows = []
    stage2 = [r for r in rows if "stage2" in (r.get("Stage", "") or "").lower()]
    potential = [r for r in rows if (r.get("Stage", "") or "").lower() == "potential"]
    score10 = [r for r in rows if (r.get("Score", "") or "").isdigit() and int(r["Score"]) == 10]
    top_picks = sorted(
        [r for r in rows if (r.get("Score", "") or "").isdigit()],
        key=lambda r: (-int(r["Score"]), -(int(r.get("RS") or "0") if (r.get("RS") or "").isdigit() else 0)),
    )[:10]

    # Build movers + breadth from /api/data data (in-memory or disk)
    market = load_live_data()
    api_rows = market.get("rows", [])

    def num(v):
        return v if isinstance(v, (int, float)) else None

    def pct(s):
        return num(s.get("%1D"))

    stocks = []
    for r in api_rows:
        t = (r.get("Ticker") or "").strip()
        if t == "SASEIDX":
            continue
        if "(" in t and any(c.isalpha() for c in t):
            continue  # section header rows
        if r.get("Last Price") is None:
            continue
        stocks.append(r)

    valid_pct = [s for s in stocks if pct(s) is not None]
    top_g = sorted(valid_pct, key=lambda s: pct(s), reverse=True)[:5]
    top_l = sorted(valid_pct, key=lambda s: pct(s))[:5]

    above_ma200 = sum(1 for s in stocks if num(s.get("MA 200D Pct Chg")) is not None and s["MA 200D Pct Chg"] > 0)
    total_ma200 = sum(1 for s in stocks if num(s.get("MA 200D Pct Chg")) is not None)
    new_highs = sum(1 for s in stocks
                    if num(s.get("Last Price")) and num(s.get("52W High")) and s["52W High"] > 0
                    and s["Last Price"] / s["52W High"] >= 0.99)
    new_lows = sum(1 for s in stocks
                   if num(s.get("Last Price")) and num(s.get("52W Low")) and s["52W Low"] > 0
                   and s["Last Price"] / s["52W Low"] <= 1.01)
    adv = sum(1 for s in valid_pct if pct(s) > 0)
    dec = sum(1 for s in valid_pct if pct(s) < 0)
    ad_ratio = (adv / dec) if dec else None

    def vol_ratio(s):
        v = num(s.get("Volume"))
        a = num(s.get("Avg Vol 30D"))
        if v is not None and a is not None and a > 0:
            return v / a
        return None

    vol_leaders = sorted(
        [s for s in stocks if vol_ratio(s) is not None],
        key=lambda s: vol_ratio(s),
        reverse=True,
    )[:5]
    for s in vol_leaders:
        s["_vol_ratio"] = vol_ratio(s)

    return {
        "yahoo": yahoo,
        "stage2_count": len(stage2),
        "potential_count": len(potential),
        "score10_count": len(score10),
        "scan_total": len(rows),
        "top_picks": top_picks,
        "top_gainers": top_g,
        "top_losers": top_l,
        "vol_leaders": vol_leaders,
        "breadth": {
            "above_ma200": above_ma200, "total_ma200": total_ma200,
            "new_highs": new_highs, "new_lows": new_lows,
            "advancers": adv, "decliners": dec, "ad_ratio": ad_ratio,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.route("/report")
def market_report():
    """Closing brief for the Saudi market."""
    return render_template("report.html", **_compute_brief_data())


def _tk(t):
    """Clean a ticker for display in tweets: drop '.SR' suffix."""
    if not t: return ""
    return str(t).split(".")[0].strip()


def _build_tweet_drafts(brief):
    """Generate tweet templates from the brief data. Returns list of draft cards."""
    yahoo = brief.get("yahoo")
    today = yahoo.get("today") if yahoo else None
    prev = yahoo.get("prev") if yahoo else None
    breadth = brief.get("breadth") or {}
    site = "chartedge"  # short tag
    drafts = []

    # ── 1. Market close (single tweet) ──────────────────────
    if today and prev:
        chg_pts = today["close"] - prev["close"]
        chg_pct = (chg_pts / prev["close"]) * 100 if prev["close"] else 0
        arrow = "▲" if chg_pts >= 0 else "▼"
        sign = "+" if chg_pts >= 0 else ""
        en = (
            f"📊 #TASI close: {today['close']:,.2f} {arrow} {sign}{chg_pts:.2f} ({sign}{chg_pct:.2f}%)\n"
            f"🌡️ Breadth: {breadth.get('advancers',0)} ▲ / {breadth.get('decliners',0)} ▼\n"
            f"📊 Volume: {today['volume']/1e6:,.1f}M\n"
            f"🎯 {brief['stage2_count']} stocks confirmed Stage 2 today\n\n"
            f"Full report → trading-suite-l9e4.onrender.com/report"
        )
        ar = (
            f"📊 إغلاق #تاسي: {today['close']:,.2f} {arrow} {sign}{chg_pts:.2f} ({sign}{chg_pct:.2f}%)\n"
            f"🌡️ المتقدمون: {breadth.get('advancers',0)} · المتراجعون: {breadth.get('decliners',0)}\n"
            f"📊 السيولة: {today['volume']/1e6:,.1f} مليون\n"
            f"🎯 {brief['stage2_count']} سهم في المرحلة الثانية اليوم\n\n"
            f"التقرير → trading-suite-l9e4.onrender.com/report"
        )
        drafts.append({"id": "close", "title": "📊 Market Close",
                       "desc": "Single-tweet snapshot of today's index close + breadth + scan summary.",
                       "tweets": [{"en": en, "ar": ar}]})

    # ── 2. Top gainer spotlight ───────────────────────────
    gainers = brief.get("top_gainers") or []
    if gainers:
        g = gainers[0]
        vol_ratio = g.get("_vol_ratio") or (
            (g.get("Volume") / g.get("Avg Vol 30D")) if g.get("Avg Vol 30D") else None)
        vol_str_en = f" on {vol_ratio*100:.0f}% of 30D avg volume" if vol_ratio and vol_ratio > 1.5 else ""
        vol_str_ar = f" بحجم {vol_ratio*100:.0f}% من المتوسط" if vol_ratio and vol_ratio > 1.5 else ""
        en = (
            f"🚀 Today's top gainer on #TASI:\n\n"
            f"#{_tk(g['Ticker'])}  +{g['%1D']:.2f}%{vol_str_en}\n"
            f"🏷️ Last: {g['Last Price']:,.2f}\n\n"
            f"More setups → trading-suite-l9e4.onrender.com/report"
        )
        ar = (
            f"🚀 أعلى ارتفاع اليوم في #تاسي:\n\n"
            f"#{_tk(g['Ticker'])}  +{g['%1D']:.2f}%{vol_str_ar}\n"
            f"🏷️ السعر: {g['Last Price']:,.2f}\n\n"
            f"المزيد → trading-suite-l9e4.onrender.com/report"
        )
        drafts.append({"id": "gainer", "title": "🚀 Top Gainer Spotlight",
                       "desc": "Highlight the single biggest gainer with stats.",
                       "tweets": [{"en": en, "ar": ar}]})

    # ── 3. Volume alert ──────────────────────────────────
    vol = brief.get("vol_leaders") or []
    if len(vol) >= 3:
        v = vol[:3]
        en_lines = [f"⚡ Unusual volume on #TASI today:\n"]
        ar_lines = [f"⚡ سيولة غير اعتيادية في #تاسي اليوم:\n"]
        medals = ["🥇", "🥈", "🥉"]
        for i, s in enumerate(v):
            ratio = s.get("_vol_ratio", 0) * 100
            en_lines.append(f"{medals[i]} #{_tk(s['Ticker'])}  {ratio:.0f}% of 30D avg")
            ar_lines.append(f"{medals[i]} #{_tk(s['Ticker'])}  {ratio:.0f}% من المتوسط")
        en_lines.append("\nWhen stocks trade 3x+ avg volume, it's worth a look.")
        ar_lines.append("\nالحجم 3x+ من المتوسط يستحق المتابعة.")
        en_lines.append("→ trading-suite-l9e4.onrender.com/report")
        ar_lines.append("→ trading-suite-l9e4.onrender.com/report")
        drafts.append({"id": "volume", "title": "⚡ Volume Alert",
                       "desc": "Top 3 unusual-volume names today.",
                       "tweets": [{"en": "\n".join(en_lines), "ar": "\n".join(ar_lines)}]})

    # ── 4. Top SEPA picks ────────────────────────────────
    picks = brief.get("top_picks") or []
    s10 = [p for p in picks if (p.get("Score") or "") == "10"][:4]
    if s10:
        en_lines = [f"🏆 Today's Score 10/10 setups on #TASI:\n"]
        ar_lines = [f"🏆 أعلى التقييمات اليوم في #تاسي (10/10):\n"]
        for p in s10:
            rs = p.get("RS", "—")
            twm = p.get("12m%", "—")
            en_lines.append(f"📈 #{_tk(p['Ticker'])} — RS {rs}, 12m {twm}%")
            ar_lines.append(f"📈 #{_tk(p['Ticker'])} — قوة نسبية {rs}، عام {twm}%")
        en_lines.append("\nAll Stage 2 confirmed.\nFull scan → trading-suite-l9e4.onrender.com/report")
        ar_lines.append("\nجميعها في المرحلة الثانية.\nالماسح الكامل → trading-suite-l9e4.onrender.com/report")
        drafts.append({"id": "picks", "title": "🏆 Top SEPA Picks",
                       "desc": "Stocks scoring 10/10 in today's ChartEdge scan.",
                       "tweets": [{"en": "\n".join(en_lines), "ar": "\n".join(ar_lines)}]})

    # ── 5. Market wrap thread (5 tweets) ─────────────────
    if today and prev:
        chg_pts = today["close"] - prev["close"]
        chg_pct = (chg_pts / prev["close"]) * 100 if prev["close"] else 0
        thread = []
        # T1 — hook
        thread.append({
            "en": f"🧵 #TASI Closing Wrap — {today['date']}\n\n"
                  f"Index closed at {today['close']:,.2f} ({'+' if chg_pts>=0 else ''}{chg_pct:.2f}%).\n"
                  f"Here's what mattered today 👇",
            "ar": f"🧵 ملخص إغلاق #تاسي — {today['date']}\n\n"
                  f"إغلاق المؤشر عند {today['close']:,.2f} ({'+' if chg_pts>=0 else ''}{chg_pct:.2f}%).\n"
                  f"تفاصيل الجلسة 👇",
        })
        # T2 — breadth
        ratio = (breadth.get("advancers",0)/breadth.get("decliners",1)) if breadth.get("decliners") else 0
        thread.append({
            "en": f"2/ 🌡️ Breadth\n\n"
                  f"Advancers: {breadth.get('advancers',0)}\n"
                  f"Decliners: {breadth.get('decliners',0)}\n"
                  f"A/D ratio: {ratio:.2f}\n"
                  f"Above 200MA: {breadth.get('above_ma200',0)}/{breadth.get('total_ma200',0)} stocks",
            "ar": f"2/ 🌡️ اتساع السوق\n\n"
                  f"المتقدمون: {breadth.get('advancers',0)}\n"
                  f"المتراجعون: {breadth.get('decliners',0)}\n"
                  f"النسبة: {ratio:.2f}\n"
                  f"فوق متوسط 200 يوم: {breadth.get('above_ma200',0)} من {breadth.get('total_ma200',0)} سهم",
        })
        # T3 — top movers
        gn = (brief.get("top_gainers") or [])[:3]
        ls = (brief.get("top_losers") or [])[:3]
        gn_en = "\n".join(f"#{_tk(s['Ticker'])}  +{s['%1D']:.2f}%" for s in gn)
        ls_en = "\n".join(f"#{_tk(s['Ticker'])}  {s['%1D']:.2f}%" for s in ls)
        gn_ar = "\n".join(f"#{_tk(s['Ticker'])}  +{s['%1D']:.2f}%" for s in gn)
        ls_ar = "\n".join(f"#{_tk(s['Ticker'])}  {s['%1D']:.2f}%" for s in ls)
        thread.append({
            "en": f"3/ 🚀 Top movers\n\nGainers:\n{gn_en}\n\nLosers:\n{ls_en}",
            "ar": f"3/ 🚀 الأكثر حركة\n\nمرتفعون:\n{gn_ar}\n\nمتراجعون:\n{ls_ar}",
        })
        # T4 — scan summary
        thread.append({
            "en": f"4/ 🎯 ChartEdge scan ({brief['scan_total']} stocks)\n\n"
                  f"✅ Stage 2 confirmed: {brief['stage2_count']}\n"
                  f"📍 Potential Stage 2: {brief['potential_count']}\n"
                  f"🏆 Score 10/10: {brief['score10_count']}",
            "ar": f"4/ 🎯 الماسح ({brief['scan_total']} سهم)\n\n"
                  f"✅ المرحلة 2 مؤكدة: {brief['stage2_count']}\n"
                  f"📍 محتمل المرحلة 2: {brief['potential_count']}\n"
                  f"🏆 تقييم 10/10: {brief['score10_count']}",
        })
        # T5 — top picks + CTA
        s10 = [p for p in (brief.get("top_picks") or []) if (p.get("Score") or "") == "10"][:3]
        s10_en = "\n".join(f"#{_tk(p['Ticker'])} — RS {p.get('RS','—')}" for p in s10)
        s10_ar = "\n".join(f"#{_tk(p['Ticker'])} — قوة نسبية {p.get('RS','—')}" for p in s10)
        thread.append({
            "en": f"5/ 🏆 Highest-conviction setups today:\n\n{s10_en}\n\n"
                  f"Full report + live dashboard → trading-suite-l9e4.onrender.com\n\n"
                  f"Follow @chartedgeai for tomorrow's brief.",
            "ar": f"5/ 🏆 أعلى الترشيحات اليوم:\n\n{s10_ar}\n\n"
                  f"التقرير + اللوحة → trading-suite-l9e4.onrender.com\n\n"
                  f"تابع @chartedgeai للتقرير اليومي.",
        })
        drafts.append({"id": "wrap", "title": "🧵 Market Wrap Thread",
                       "desc": "Full 5-tweet thread covering the session — post sequentially.",
                       "tweets": thread})

    return drafts


@app.route("/agent")
def agent():
    """X/Twitter content generator from today's closing data. Admin-gated."""
    if not ADMIN_PASSWORD:
        return ("Server missing ADMIN_PASSWORD env var.", 500, {"Content-Type": "text/plain"})
    if request.args.get("p") != ADMIN_PASSWORD:
        return render_template("admin_gate.html"), 401
    brief = _compute_brief_data()
    drafts = _build_tweet_drafts(brief)
    return render_template("agent.html", drafts=drafts, brief=brief)


@app.route("/p/<key>")
def product_page(key):
    if key not in PRODUCTS:
        abort(404)
    show_live = (key == "tradepulse_sar")
    needs_ibkr = (key == "tradepulse_us")
    auto_load_data = key.startswith("tradepulse")
    # Show "Read Guide" button only if the PDF actually exists
    guide_filename = f"guide_{key}.pdf"
    has_guide = (DATA_DIR / guide_filename).exists()
    return render_template(
        "product.html",
        key=key,
        product=PRODUCTS[key],
        show_live=show_live,
        needs_ibkr=needs_ibkr,
        auto_load_data=auto_load_data,
        has_guide=has_guide,
        guide_url=(f"/data/{guide_filename}" if has_guide else None),
        refresh_seconds=REFRESH_SECONDS,
    )


@app.route("/p/<key>/file")
def product_file(key):
    if key not in PRODUCTS:
        abort(404)
    p = PRODUCTS[key]
    return send_from_directory(p["dir"], p["file"])


@app.route("/data/<path:filename>")
def data_file(filename):
    from flask import Response
    XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if filename == "us_live.xlsx":
        if not _US_STATE.get("xlsx"):
            abort(404)
        return Response(_US_STATE["xlsx"], mimetype=XLSX_MIME)
    if filename == "all.xlsx":
        # 1. Live in-memory push from sar_feed.py (preferred — fresh every 30s during market hours)
        if _SA_STATE.get("xlsx"):
            return Response(_SA_STATE["xlsx"], mimetype=XLSX_MIME)
        # 2. Fallback to last manually-uploaded all.xlsx on disk
        return send_from_directory(DATA_DIR, "all.xlsx")
    allowed = {"chartedge_us.csv", "chartedge_sa.csv",
               "guide_chartedge.pdf", "guide_tradepulse_us.pdf", "guide_tradepulse_sar.pdf"}
    if filename not in allowed:
        abort(404)
    return send_from_directory(DATA_DIR, filename)


@app.route("/api/us/push", methods=["POST"])
def api_us_push():
    """us_feed.py POSTs JSON snapshot here (auth via header)."""
    if not US_PUSH_TOKEN:
        return {"error": "server missing US_PUSH_TOKEN"}, 500
    if request.headers.get("X-Push-Token") != US_PUSH_TOKEN:
        return {"error": "bad token"}, 401
    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return {"error": f"bad json: {e}"}, 400
    rows = body.get("rows") or []
    if not isinstance(rows, list):
        return {"error": "rows must be a list"}, 400
    try:
        xlsx_bytes = _us_rows_to_xlsx(rows)
    except Exception as e:
        return {"error": f"xlsx build failed: {e}"}, 500
    _US_STATE["xlsx"] = xlsx_bytes
    _US_STATE["updated"] = datetime.now().isoformat(timespec="seconds")
    _US_STATE["rows"] = len(rows)
    return {"ok": True, "rows": len(rows), "xlsx_bytes": len(xlsx_bytes),
            "updated": _US_STATE["updated"]}


@app.route("/api/us/status")
def api_us_status():
    return {
        "have_data": bool(_US_STATE.get("xlsx")),
        "rows": _US_STATE.get("rows", 0),
        "updated": _US_STATE.get("updated"),
        "bytes": len(_US_STATE["xlsx"]) if _US_STATE.get("xlsx") else 0,
    }


@app.route("/api/sa/push", methods=["POST"])
def api_sa_push():
    """sar_feed.py POSTs the locked Bloomberg xlsx (raw bytes) here.
    Auth via X-Push-Token header. Stored in memory only — falls back to
    last GitHub-committed all.xlsx if memory empty (e.g. after restart)."""
    if not SA_PUSH_TOKEN:
        return {"error": "server missing SA_PUSH_TOKEN"}, 500
    if request.headers.get("X-Push-Token") != SA_PUSH_TOKEN:
        return {"error": "bad token"}, 401
    raw = request.get_data()
    if not raw:
        return {"error": "empty body"}, 400
    if len(raw) > 25 * 1024 * 1024:
        return {"error": "too large (>25 MB)"}, 400
    # Sanity check: xlsx files start with 'PK' (zip header)
    if raw[:2] != b"PK":
        return {"error": "not an xlsx (missing PK header)"}, 400
    _SA_STATE["xlsx"] = raw
    _SA_STATE["size"] = len(raw)
    _SA_STATE["updated"] = datetime.now().isoformat(timespec="seconds")
    return {"ok": True, "bytes": len(raw), "updated": _SA_STATE["updated"]}


@app.route("/api/sa/status")
def api_sa_status():
    return {
        "have_data": bool(_SA_STATE.get("xlsx")),
        "bytes": _SA_STATE.get("size", 0),
        "updated": _SA_STATE.get("updated"),
    }


@app.route("/api/data")
def api_data():
    return jsonify(load_live_data())


@app.route("/healthz")
def healthz():
    return {"ok": True}


# ── Visit counter (in-memory; resets on dyno restart) ──────────────────
import threading as _threading
from collections import defaultdict as _defaultdict
from datetime import timedelta as _timedelta

_visit_lock = _threading.Lock()
_visit_counts = {}                                      # path -> all-time count
_visit_daily = _defaultdict(lambda: _defaultdict(int))  # 'YYYY-MM-DD' -> path -> count
_visit_first_seen = datetime.now()

# Pages we actually want to count (excludes APIs, static, /admin, etc.)
_COUNTABLE_ENDPOINTS = {"index", "product_page", "market_report", "contact"}


@app.before_request
def _count_visit():
    if not request.endpoint:
        return
    if request.endpoint not in _COUNTABLE_ENDPOINTS:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    key = request.path
    with _visit_lock:
        _visit_counts[key] = _visit_counts.get(key, 0) + 1
        _visit_counts["_total"] = _visit_counts.get("_total", 0) + 1
        _visit_daily[today][key] += 1
        _visit_daily[today]["_total"] += 1


@app.route("/admin/stats")
def admin_stats():
    # Auth via admin password as ?p= query param so you can bookmark
    if not ADMIN_PASSWORD or request.args.get("p") != ADMIN_PASSWORD:
        return ("Append ?p=<ADMIN_PASSWORD> to the URL.", 401, {"Content-Type": "text/plain"})

    label_map = [
        ("/", "Home"),
        ("/p/chartedge", "ChartEdge"),
        ("/p/tradepulse_us", "TradePulse US"),
        ("/p/tradepulse_sar", "TradePulse SAR"),
        ("/report", "Market Report"),
        ("/contact", "Contact"),
    ]

    with _visit_lock:
        all_time = dict(_visit_counts)
        # Build last 14 days
        today = datetime.now().date()
        daily_table = []
        for i in range(14):
            d = today - _timedelta(days=i)
            ds = d.strftime("%Y-%m-%d")
            day_buckets = dict(_visit_daily.get(ds, {}))
            row = {
                "date": ds,
                "weekday": d.strftime("%a"),
                "total": day_buckets.get("_total", 0),
                "by_page": [day_buckets.get(p, 0) for p, _ in label_map],
            }
            daily_table.append(row)

    rows = [{"label": lbl, "path": p, "count": all_time.get(p, 0)} for p, lbl in label_map]

    today_total = daily_table[0]["total"] if daily_table else 0
    yesterday_total = daily_table[1]["total"] if len(daily_table) > 1 else 0
    delta_pct = None
    if yesterday_total:
        delta_pct = ((today_total - yesterday_total) / yesterday_total) * 100

    return render_template(
        "admin_stats.html",
        rows=rows,
        total=all_time.get("_total", 0),
        today_total=today_total,
        yesterday_total=yesterday_total,
        delta_pct=delta_pct,
        first_seen=_visit_first_seen.isoformat(timespec="seconds"),
        now=datetime.now().isoformat(timespec="seconds"),
        daily_table=daily_table,
        labels=[lbl for _, lbl in label_map],
    )


@app.route("/admin/diag")
def admin_diag():
    """Diagnostic — gated by ADMIN_PASSWORD as ?p= query param.
    Shows whether env vars are set and whether the server can reach GitHub.
    """
    if not ADMIN_PASSWORD or request.args.get("p") != ADMIN_PASSWORD:
        return ("Append ?p=<ADMIN_PASSWORD> to the URL.", 401, {"Content-Type": "text/plain"})
    import requests as _rq
    out = {
        "env": {
            "ADMIN_PASSWORD_set": bool(ADMIN_PASSWORD),
            "GH_PAT_set": bool(GH_PAT),
            "GH_PAT_starts_with": (GH_PAT[:4] + "..." if GH_PAT else None),
            "GH_PAT_length": (len(GH_PAT) if GH_PAT else 0),
            "GH_REPO": GH_REPO,
            "GH_BRANCH": GH_BRANCH,
            "US_PUSH_TOKEN_set": bool(US_PUSH_TOKEN),
        },
        "github_repo_check": None,
        "github_branch_check": None,
        "github_xlsx_check": None,
    }
    if not GH_PAT:
        return out
    h = {"Authorization": f"token {GH_PAT}", "User-Agent": "trading-suite-diag"}

    # Repo accessible?
    try:
        r = _rq.get(f"https://api.github.com/repos/{GH_REPO}", headers=h, timeout=15)
        out["github_repo_check"] = {
            "status": r.status_code,
            "permissions": (r.json().get("permissions") if r.status_code == 200 else None),
            "private": (r.json().get("private") if r.status_code == 200 else None),
            "error": (r.text[:200] if r.status_code != 200 else None),
        }
    except Exception as e:
        out["github_repo_check"] = {"exception": str(e)}

    # Branch accessible?
    try:
        r = _rq.get(f"https://api.github.com/repos/{GH_REPO}/branches/{GH_BRANCH}",
                    headers=h, timeout=15)
        out["github_branch_check"] = {
            "status": r.status_code,
            "error": (r.text[:200] if r.status_code != 200 else None),
        }
    except Exception as e:
        out["github_branch_check"] = {"exception": str(e)}

    # Can I read the existing xlsx (proves write-target SHA is fetchable)?
    try:
        r = _rq.get(f"https://api.github.com/repos/{GH_REPO}/contents/data/all.xlsx",
                    headers=h, params={"ref": GH_BRANCH}, timeout=15)
        out["github_xlsx_check"] = {
            "status": r.status_code,
            "size": (r.json().get("size") if r.status_code == 200 else None),
            "sha": ((r.json().get("sha") or "")[:10] if r.status_code == 200 else None),
            "error": (r.text[:200] if r.status_code != 200 else None),
        }
    except Exception as e:
        out["github_xlsx_check"] = {"exception": str(e)}

    return out


@app.route("/admin", methods=["GET"])
def admin_page():
    # Gate the admin page behind ADMIN_PASSWORD so curious visitors can't
    # see the upload form or its routing table.
    if not ADMIN_PASSWORD:
        return ("Server missing ADMIN_PASSWORD env var.", 500, {"Content-Type": "text/plain"})
    if request.args.get("p") != ADMIN_PASSWORD:
        return render_template("admin_gate.html"), 401
    return render_template("admin.html", success=False, error=None)


@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    import requests as _rq
    if not ADMIN_PASSWORD:
        return render_template("admin.html", success=False,
            error="Server missing ADMIN_PASSWORD env var. Add it on Render."), 500
    if not GH_PAT:
        return render_template("admin.html", success=False,
            error="Server missing GH_PAT env var. Add it on Render."), 500
    if request.form.get("password") != ADMIN_PASSWORD:
        return render_template("admin.html", success=False,
            error="Wrong password."), 401

    f = request.files.get("xlsx")
    if not f or not f.filename:
        return render_template("admin.html", success=False,
            error="No file selected."), 400

    raw = f.read()
    if len(raw) > 25 * 1024 * 1024:
        return render_template("admin.html", success=False,
            error="File too large (>25 MB)."), 400
    market_form = request.form.get("market", "auto")
    pdf_target = request.form.get("pdf_target", "")
    target_path, why = _resolve_upload_target(f.filename, market_form, raw, pdf_target)
    if not target_path:
        return render_template("admin.html", success=False, error=why), 400

    api = f"https://api.github.com/repos/{GH_REPO}/contents/{target_path}"
    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "trading-suite-admin",
    }

    sha = None
    try:
        r = _rq.get(api, headers=headers, params={"ref": GH_BRANCH}, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
        elif r.status_code != 404:
            return render_template("admin.html", success=False,
                error=f"GitHub GET failed ({r.status_code}): {r.text[:200]}"), 502
    except Exception as e:
        return render_template("admin.html", success=False,
            error=f"GitHub GET exception: {e}"), 502

    payload = {
        "message": f"Admin upload: refresh {target_path} ({datetime.utcnow().isoformat(timespec='seconds')}Z)",
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        # Slightly under gunicorn's 120s worker timeout so we always return cleanly
        r = _rq.put(api, headers=headers, json=payload, timeout=90)
    except Exception as e:
        return render_template("admin.html", success=False,
            error=f"GitHub PUT exception: {e}"), 502

    if r.status_code in (200, 201):
        return render_template("admin.html", success=True, error=None)
    return render_template("admin.html", success=False,
        error=f"GitHub PUT failed ({r.status_code}): {r.text[:300]}"), 502


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
