from flask import Flask, render_template, jsonify, abort, send_from_directory, request
import pandas as pd
import math
import os
import base64
from datetime import datetime
from pathlib import Path
from config import PRODUCTS, LIVE_XLSX_PATH, LIVE_XLSX_SHEET, REFRESH_SECONDS, DATA_DIR, REPORTS, REPORTS_DIR, RS_PDFS

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

    # Latest RS Rating PDF (newest entry in config.RS_PDFS) — exposed to every
    # template so the /rs-rating tab can feature it without a route change.
    latest_rs_pdf = RS_PDFS[0] if RS_PDFS else None

    return {"is_admin": is_admin, "admin_p": pw if is_admin else "", "admin_url": admin_url,
            "latest_rs_pdf": latest_rs_pdf}


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
    """Classify a scan CSV's market by sniffing the first (Ticker) column.

    Saudi (Tadawul) tickers are 4-digit codes, usually carrying a '.SR' suffix
    (e.g. '2030.SR'). US tickers are alphabetic (e.g. 'VSH', 'AAPL', 'BRK.B').
    Returns 'sa', 'us', or None when the sample is too ambiguous to be sure.

    NOTE: the earlier version tested `cell.isdigit()`, which is False for the
    real '2030.SR' format and silently classified Saudi files as US — a bug that
    let a TASI scan get written into the US slot twice. Handle the suffix here.
    """
    try:
        text = raw[:8192].decode("utf-8", errors="ignore")
    except Exception:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    saudi = us = total = 0
    for ln in lines[1:21]:  # skip header, sample up to 20 rows
        cell = ln.split(",")[0].strip().strip('"').strip("'").upper()
        if not cell:
            continue
        total += 1
        base = cell[:-3] if cell.endswith(".SR") else cell
        if cell.endswith(".SR") or (base.isdigit() and len(base) == 4):
            saudi += 1
        elif cell.replace(".", "").isalpha():
            us += 1
    if total == 0:
        return None
    if saudi / total >= 0.6:
        return "sa"
    if us / total >= 0.6:
        return "us"
    return None


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
    content_market = _detect_csv_market_from_content(raw) if raw is not None else None
    if market not in ("us", "sa"):
        market = content_market or ""
    # SAFETY GUARD — never let a clearly-Saudi scan land in the US slot (or vice
    # versa). A market mismatch here has overwritten the US dashboard with TASI
    # data twice. If the chosen market and the file's actual tickers disagree
    # confidently, refuse the upload instead of corrupting the wrong dashboard.
    if market in ("us", "sa") and content_market in ("us", "sa") and content_market != market:
        names = {"us": "US (NYSE/NASDAQ)", "sa": "Saudi (TASI)"}
        looks = "Saudi 4-digit / .SR codes" if content_market == "sa" else "US letter symbols"
        return None, (
            f"⚠ Market mismatch — upload refused. You selected {names[market]}, "
            f"but this file's tickers look like {names[content_market]} data ({looks}). "
            f"This guard protects the {names[market]} dashboard from being overwritten "
            f"with the wrong market. Pick the correct market, or check the file."
        )
    if market == "us":
        return GH_CSV_US_PATH, None
    if market == "sa":
        return GH_CSV_SA_PATH, None
    return None, ("Could not determine market for this CSV. "
                  "Pick US or Saudi from the Market dropdown and try again.")

US_PUSH_TOKEN = _secret("US_PUSH_TOKEN")
SA_PUSH_TOKEN = _secret("SA_PUSH_TOKEN")
CRON_TOKEN    = _secret("CRON_TOKEN")
# in-memory live US feed: bytes of xlsx + last update timestamp
_US_STATE = {"xlsx": None, "updated": None, "rows": 0}

# Last-known US index prices (SPY/QQQ/IWM/DIA) so the homepage never shows
# blank "market closed" cells during Saudi browsing hours. Disk copy is
# refreshed on every push; the GitHub copy at most once per UTC day (Render's
# free-tier disk is wiped on every spin-up, so disk alone won't survive).
def _save_us_indices_cache(idx):
    import json as _json
    try:
        (DATA_DIR / "us_last_indices.json").write_text(_json.dumps(
            {"updated": datetime.now().isoformat(timespec="seconds"),
             "indices": idx}, indent=2))
    except Exception:
        pass


def _load_us_indices_cache():
    """Disk copy first (fresh while the instance lives), then the GitHub
    copy (survives restarts). Returns {} when neither exists yet."""
    import json as _json
    p = DATA_DIR / "us_last_indices.json"
    try:
        if p.exists():
            return _json.loads(p.read_text())
    except Exception:
        pass
    try:
        raw = _github_file_bytes("data/us_last_indices.json")
        if raw:
            return _json.loads(raw.decode("utf-8"))
    except Exception:
        pass
    return {}
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


def _close_snapshot_path(date_str: str) -> Path:
    """Path to the frozen post-close xlsx snapshot for a given date."""
    return DATA_DIR / "sa_close" / f"{date_str}.xlsx"


def load_live_data(for_date: str = None):
    """Read TASI live data.

    Resolution order:
      1. If `for_date` is given AND a frozen close snapshot exists for it -> use it.
      2. Live in-memory feed pushed by sar_feed.py (most recent intraday).
      3. Disk `all.xlsx` last uploaded manually (may be stale).
    """
    import io
    if for_date:
        snap = _close_snapshot_path(for_date)
        if snap.exists():
            df = pd.read_excel(snap, sheet_name=LIVE_XLSX_SHEET)
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
                "source": f"close_snapshot:{for_date}",
            }

    if _SA_STATE.get("xlsx"):
        df = pd.read_excel(io.BytesIO(_SA_STATE["xlsx"]), sheet_name=LIVE_XLSX_SHEET)
        src = "live_memory"
    elif LIVE_XLSX_PATH.exists():
        df = pd.read_excel(LIVE_XLSX_PATH, sheet_name=LIVE_XLSX_SHEET)
        src = "disk_all_xlsx"
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
        "source": src,
    }


def fetch_tasi_index(for_date: str = None):
    """Read the TASI index OHLCV from the SASEIDX row of the Bloomberg xlsx.

    Same source the TradePulse SA dashboard uses (the live xlsx pushed by
    sar_feed.py). No external API.

    Returns dict shaped like:
      {
        "today": {"date": "...", "open":..., "high":..., "low":..., "close":..., "volume":...},
        "prev":  {"close": ...}   # from Prev Cls column
      }
    or None if the index row can't be located.
    """
    market = load_live_data(for_date=for_date)
    rows = market.get("rows", []) or []
    saseidx = None
    for r in rows:
        t = (r.get("Ticker") or "").strip().upper()
        if t == "SASEIDX" or t.startswith("SASEIDX"):
            saseidx = r
            break
    if not saseidx:
        return None

    def num(v):
        return v if isinstance(v, (int, float)) else None

    today = {
        "date": for_date or datetime.now().strftime("%Y-%m-%d"),
        "open":  num(saseidx.get("Open")),
        "high":  num(saseidx.get("High")),
        "low":   num(saseidx.get("Low")),
        "close": num(saseidx.get("Last Price")),
        "volume": num(saseidx.get("Volume")),
    }
    prev = None
    pc = num(saseidx.get("Prev Cls"))
    if pc is not None:
        prev = {"close": pc}
    if today["close"] is None:
        return None
    return {"today": today, "prev": prev, "source": "bloomberg_xlsx"}


def _homepage_live_stats():
    """Build the small dataset the new landing page needs:
      - latest_brief: most recent /brief/<date>.json (close, top mover, etc.)
      - scan: scan_total / stage2 / score10 / potential from chartedge_sa.csv
      - us_rows: current /api/us/push payload size (0 when feed asleep)

    All optional — the template safely renders fallbacks if any of these
    are missing (e.g. on first deploy before any brief exists)."""
    out = {"latest_brief": None, "scan": None, "us_rows": 0, "sa_have_data": False}

    # Most recent archived brief
    dates = _list_briefs()
    if dates:
        b = _load_brief(dates[0])
        if b:
            idx = b.get("index") or b.get("yahoo") or {}
            today = idx.get("today") or {}
            prev = idx.get("prev") or {}
            close = today.get("close")
            prev_close = prev.get("close")
            chg_pts = (close - prev_close) if (close and prev_close) else None
            chg_pct = ((chg_pts / prev_close) * 100) if (chg_pts and prev_close) else None
            tg = (b.get("top_gainers") or [{}])[0]
            tl = (b.get("top_losers") or [{}])[0]
            sec_top = (b.get("sectors_leading") or [{}])[0]
            breadth = b.get("breadth") or {}
            out["latest_brief"] = {
                "date": dates[0],
                "close": close,
                "chg_pts": chg_pts,
                "chg_pct": chg_pct,
                "top_gainer_ticker": (tg.get("Ticker") or "").split(".")[0],
                "top_gainer_pct": tg.get("%1D"),
                "top_loser_ticker": (tl.get("Ticker") or "").split(".")[0],
                "top_loser_pct": tl.get("%1D"),
                "sector_top_name": sec_top.get("name") if sec_top else None,
                "sector_top_pct": sec_top.get("avg_pct") if sec_top else None,
                "sector_top_adv": sec_top.get("advancers") if sec_top else None,
                "sector_top_dec": sec_top.get("decliners") if sec_top else None,
                "ad_ratio": breadth.get("ad_ratio"),
                "advancers": breadth.get("advancers"),
                "decliners": breadth.get("decliners"),
                "stage2": b.get("stage2_count"),
                "score10": b.get("score10_count"),
                "potential": b.get("potential_count"),
                "scan_total": b.get("scan_total"),
                "volume_m": (today.get("volume") / 1e6) if today.get("volume") else None,
            }

    # Live scan stats (from disk CSV — same one /report reads)
    import csv as _csv
    csv_path = DATA_DIR / "chartedge_sa.csv"
    if csv_path.exists():
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
            stage2 = sum(1 for r in rows if "stage2" in (r.get("Stage", "") or "").lower())
            score10 = sum(1 for r in rows if (r.get("Score", "") or "").isdigit() and int(r["Score"]) == 10)
            potential = sum(1 for r in rows if (r.get("Stage", "") or "").lower() == "potential")
            out["scan"] = {
                "total": len(rows),
                "stage2": stage2,
                "score10": score10,
                "potential": potential,
            }
        except Exception:
            pass

    out["us_rows"] = _US_STATE.get("rows", 0) or 0
    out["sa_have_data"] = bool(_SA_STATE.get("xlsx"))

    # US index cells: live values when the feed pushed within ~20 min,
    # otherwise last-known prices (disk → GitHub cache) tagged with a date so
    # the homepage never shows blank "—" cells outside US market hours.
    live_idx = _US_STATE.get("indices") or {}
    upd = _US_STATE.get("updated")
    fresh = False
    if live_idx and upd:
        try:
            fresh = (datetime.now() - datetime.fromisoformat(upd)).total_seconds() < 1200
        except Exception:
            fresh = False
    if live_idx and fresh:
        out["us_indices"] = {t: dict(v, live=True) for t, v in live_idx.items()}
    elif live_idx:
        out["us_indices"] = {t: dict(v, live=False, asof=(upd or "")[:10] or None)
                             for t, v in live_idx.items()}
    else:
        cached = _load_us_indices_cache()
        asof = (cached.get("updated") or "")[:10] or None
        out["us_indices"] = {t: dict(v, live=False, asof=asof)
                             for t, v in (cached.get("indices") or {}).items()}
    return out


@app.route("/")
def index():
    return render_template("index.html", products=PRODUCTS, live=_homepage_live_stats(),
                           latest_report=(REPORTS[0] if REPORTS else None))


@app.route("/contact")
def contact():
    return render_template(
        "contact.html",
        tg_channel_url="https://t.me/+R_m5lVLFnBJhYWJk",
        tg_admin_url="https://t.me/chartedgeai",
        tg_admin_handle="@chartedgeai",
    )


@app.route("/about")
def about():
    return render_template("about.html")


# ── Research reports ──────────────────────────────────────────────────────────
def _find_report(slug):
    for r in REPORTS:
        if r["slug"] == slug:
            return r
    return None


@app.route("/reports")
def reports_index():
    return render_template("reports_index.html", reports=REPORTS)


@app.route("/reports/<slug>")
def report_page(slug):
    r = _find_report(slug)
    if not r:
        abort(404)
    return render_template("reports_view.html", report=r)


@app.route("/reports/<slug>/file")
def report_file(slug):
    r = _find_report(slug)
    if not r:
        abort(404)
    return send_from_directory(REPORTS_DIR, r["file"])


def _compute_brief_data(for_date: str = None):
    """Build a unified data bundle for both /report and /agent.

    All data — TASI index OHLCV, movers, breadth, volume leaders — comes from
    the same Bloomberg xlsx that TradePulse SA reads (the live feed pushed by
    sar_feed.py). No external APIs.

    If `for_date` is given (YYYY-MM-DD), values come from the frozen post-close
    snapshot for that date if it exists, so the brief is reproducible and can't
    be polluted by stale intraday data after a Render redeploy.
    """
    tasi = fetch_tasi_index(for_date=for_date)

    # Build scan summary from chartedge_sa.csv (in-memory if pushed, else disk)
    import csv as _csv
    import io as _io
    # Prefer the frozen scan CSV for `for_date` (so an archived brief always
    # uses the scan it was generated with), falling back to the live one.
    scan_source = "live_csv"
    csv_path = DATA_DIR / "chartedge_sa.csv"
    if for_date:
        snap = _scan_snapshot_path(for_date)
        if snap.exists():
            csv_path = snap
            scan_source = f"scan_snapshot:{for_date}"
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

    # Build movers + breadth from the close-snapshot if for_date is given,
    # otherwise from live /api/data (in-memory or disk fallback).
    market = load_live_data(for_date=for_date)
    api_rows = market.get("rows", [])
    market_source = market.get("source", "unknown")

    def num(v):
        return v if isinstance(v, (int, float)) else None

    def pct(s):
        return num(s.get("%1D"))

    # Walk rows tagging each stock with the most recent sector header it appeared
    # under. The Bloomberg xlsx groups stocks by section-header rows like
    # "Banks  (10)" with no Last Price — that's the sector name + member count.
    import re as _re_sec
    current_sector = None
    stocks = []
    for r in api_rows:
        t = (r.get("Ticker") or "").strip()
        if t == "SASEIDX":
            continue
        # Detect a sector header: "<name>  (<n>)" with letters and no Last Price
        m = _re_sec.match(r"^(.+?)\s*\((\d+)\)\s*$", t)
        if m and r.get("Last Price") is None and any(c.isalpha() for c in t):
            current_sector = m.group(1).strip()
            continue
        if r.get("Last Price") is None:
            continue
        # Stamp the row with its parent sector so downstream code can group
        r["_sector"] = current_sector
        stocks.append(r)

    valid_pct = [s for s in stocks if pct(s) is not None]
    top_g = sorted(valid_pct, key=lambda s: pct(s), reverse=True)[:5]
    top_l = sorted(valid_pct, key=lambda s: pct(s))[:5]

    # ── Match TradePulse SA dashboard rules exactly (updateBreadth in TradePulse_SARv1_LIVE.html) ──
    above_ma200 = sum(1 for s in stocks if num(s.get("MA 200D Pct Chg")) is not None and s["MA 200D Pct Chg"] > 0)
    total_ma200 = sum(1 for s in stocks if num(s.get("MA 200D Pct Chg")) is not None)
    # Dashboard: new high = today's High touched/exceeded 52W high (within 0.2%)
    new_highs = sum(1 for s in stocks
                    if num(s.get("High")) and num(s.get("52W High")) and s["52W High"] > 0
                    and s["High"] >= s["52W High"] * 0.998)
    # Dashboard: new low = today's Low touched/breached 52W low (within 0.2%)
    new_lows = sum(1 for s in stocks
                   if num(s.get("Low")) and num(s.get("52W Low")) and s["52W Low"] > 0
                   and s["Low"] <= s["52W Low"] * 1.002)
    # Dashboard: advancers/decliners use %1D (chg)
    adv = sum(1 for s in valid_pct if pct(s) > 0)
    dec = sum(1 for s in valid_pct if pct(s) < 0)
    unch = sum(1 for s in stocks if num(s.get("%1D")) is None or s.get("%1D") == 0)
    ad_ratio = (adv / dec) if dec else None
    # Above 50MA — dashboard uses Mov Avg 50; only counts rows where MA50 is non-null
    has50 = [s for s in stocks if num(s.get("Mov Avg 50")) is not None
             and num(s.get("Last Price")) is not None]
    abv50 = sum(1 for s in has50 if s["Last Price"] > s["Mov Avg 50"])

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

    # ── Sector rotation ────────────────────────────────────────────────
    # Group stocks by their parent sector (set during row walk above).
    # Only sectors with >=3 stocks contribute (smaller buckets are noisy).
    _by_sector = {}
    for s in stocks:
        sec = s.get("_sector")
        if not sec:
            continue
        p = pct(s)
        if p is None:
            continue
        _by_sector.setdefault(sec, []).append(s)

    def _sector_summary(name, members):
        pcts = [pct(s) for s in members]
        avg = sum(pcts) / len(pcts)
        sec_adv = sum(1 for p in pcts if p > 0)
        sec_dec = sum(1 for p in pcts if p < 0)
        top = max(members, key=lambda s: pct(s))
        return {
            "name": name,
            "count": len(members),
            "avg_pct": avg,
            "advancers": sec_adv,
            "decliners": sec_dec,
            "top_ticker": (top.get("Ticker") or "").split(".")[0],
            "top_pct": pct(top),
        }
    _sec_stats = [_sector_summary(n, m) for n, m in _by_sector.items() if len(m) >= 3]
    _sec_stats.sort(key=lambda s: s["avg_pct"], reverse=True)
    sectors_leading = _sec_stats[:3]
    sectors_lagging = list(reversed(_sec_stats[-3:])) if len(_sec_stats) >= 3 else []

    return {
        "index": tasi,
        "stage2_count": len(stage2),
        "potential_count": len(potential),
        "score10_count": len(score10),
        "scan_total": len(rows),
        "top_picks": top_picks,
        "top_gainers": top_g,
        "top_losers": top_l,
        "vol_leaders": vol_leaders,
        "sectors_leading": sectors_leading,
        "sectors_lagging": sectors_lagging,
        "sectors_all": _sec_stats,
        "breadth": {
            "above_ma200": above_ma200, "total_ma200": total_ma200,
            "above_ma50": abv50, "total_ma50": len(has50),
            "new_highs": new_highs, "new_lows": new_lows,
            "advancers": adv, "decliners": dec, "unchanged": unch,
            "ad_ratio": ad_ratio,
            "total_stocks": len(stocks),
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "market_source": market_source,
        "scan_source": scan_source,
    }


def _brief_to_report_ctx(brief_json, source_label):
    """Adapt an archived brief JSON into the context shape report.html expects.

    Accepts both the new "index" key and legacy "yahoo" key for back-compat with
    any old archived brief JSONs that predate the rename.
    """
    return {
        "index":          brief_json.get("index") or brief_json.get("yahoo"),
        "breadth":        brief_json.get("breadth") or {},
        "top_gainers":    brief_json.get("top_gainers") or [],
        "top_losers":     brief_json.get("top_losers") or [],
        "vol_leaders":    brief_json.get("vol_leaders") or [],
        "top_picks":      brief_json.get("top_picks") or [],
        "stage2_count":   brief_json.get("stage2_count", 0),
        "potential_count":brief_json.get("potential_count", 0),
        "score10_count":  brief_json.get("score10_count", 0),
        "scan_total":     brief_json.get("scan_total", 0),
        "generated_at":   brief_json.get("generated_at"),
        "market_source":  source_label,
    }


@app.route("/report")
def market_report():
    """Closing brief for the Saudi market.

    Resolution order:
      1. Today's frozen close snapshot (post-15:30 cron has run) -> live recompute.
      2. Live in-memory feed pushed by sar_feed.py is fresh (<10 min old) -> live recompute.
      3. Most recent archived brief JSON -> render that as the report.
         (Avoids showing stale movers/breadth from a disk fallback xlsx.)
      4. Pure live recompute (will use whatever disk has — last resort).
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. today's frozen snapshot exists -> recompute from it
    if _close_snapshot_path(today).exists():
        return render_template("report.html", **_compute_brief_data(for_date=today))

    # 2. live feed is fresh enough to recompute in real time
    upd = _SA_STATE.get("updated")
    fresh = False
    if _SA_STATE.get("xlsx") and upd:
        try:
            age = (datetime.now() - datetime.fromisoformat(upd)).total_seconds()
            fresh = age < 600  # 10 min
        except Exception:
            fresh = False
    if fresh:
        return render_template("report.html", **_compute_brief_data())

    # 3. fall back to the latest archived brief (correct closing snapshot)
    dates = _list_briefs()
    if dates:
        brief = _load_brief(dates[0])
        if brief:
            ctx = _brief_to_report_ctx(brief, f"archived_brief:{dates[0]}")
            return render_template("report.html", **ctx)

    # 4. last resort: pure live recompute (may use stale disk all.xlsx)
    return render_template("report.html", **_compute_brief_data())


# ── Brief archive: snapshots saved per day ──────────────────────────────
BRIEFS_DIR = DATA_DIR / "briefs"
BRIEFS_DIR.mkdir(parents=True, exist_ok=True)


def _serialize_brief(brief):
    """Make the brief data JSON-safe (drop heavy objects, keep what's needed)."""
    def keep(s):
        wanted = ["Ticker","Last Price","%1D","%1M","%YTD","Volume","Avg Vol 30D",
                  "_vol_ratio","Company","Stage","Score","RS","12m%"]
        return {k: s.get(k) for k in wanted if k in s or k == "_vol_ratio"}
    return {
        "generated_at": brief.get("generated_at"),
        "index": brief.get("index") or brief.get("yahoo"),
        "stage2_count": brief.get("stage2_count"),
        "potential_count": brief.get("potential_count"),
        "score10_count": brief.get("score10_count"),
        "scan_total": brief.get("scan_total"),
        "top_picks": [keep(r) for r in (brief.get("top_picks") or [])],
        "top_gainers": [keep(s) for s in (brief.get("top_gainers") or [])],
        "top_losers": [keep(s) for s in (brief.get("top_losers") or [])],
        "vol_leaders": [keep(s) for s in (brief.get("vol_leaders") or [])],
        "sectors_leading": brief.get("sectors_leading") or [],
        "sectors_lagging": brief.get("sectors_lagging") or [],
        "breadth": brief.get("breadth"),
    }


def _commit_bytes_to_github(repo_path: str, payload_bytes: bytes, message: str):
    """Commit arbitrary bytes to a path in the GitHub repo. Returns (ok, message)."""
    if not GH_PAT:
        return False, "GH_PAT not set; saved locally only"
    import requests as _rq
    api = f"https://api.github.com/repos/{GH_REPO}/contents/{repo_path}"
    headers = {"Authorization": f"token {GH_PAT}",
               "Accept": "application/vnd.github.v3+json",
               "User-Agent": "trading-suite-cron"}
    sha = None
    try:
        r = _rq.get(api, headers=headers, params={"ref": GH_BRANCH}, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    payload = {
        "message": message,
        "content": base64.b64encode(payload_bytes).decode("ascii"),
        "branch": GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    try:
        r = _rq.put(api, headers=headers, json=payload, timeout=90)
        if r.status_code in (200, 201):
            return True, "committed"
        return False, f"GitHub PUT {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"exception: {e}"


def _commit_brief_to_github(date_str, payload_bytes):
    """Commit data/briefs/<date>.json to GitHub. Returns (ok, message)."""
    return _commit_bytes_to_github(
        f"data/briefs/{date_str}.json",
        payload_bytes,
        f"Closing brief {date_str}",
    )


def _scan_snapshot_path(date_str: str) -> Path:
    """Path to the frozen ChartEdge scan CSV for a given date."""
    return DATA_DIR / "sa_scan" / f"{date_str}.csv"


def _freeze_sa_scan(date_str: str):
    """Freeze the current chartedge_sa.csv as the post-close scan for date_str.

    The CSV is uploaded daily and changes day to day (new scores, new
    Stage 2 promotions etc.), so we snapshot whatever's on disk at brief
    generation time to disk + GitHub. Each archived brief becomes fully
    reproducible from its (xlsx, csv) snapshot pair.

    Returns (ok, message, meta). It's non-fatal if this fails — the brief
    still gets built; we just lose the audit trail for that day.
    """
    csv_path = DATA_DIR / "chartedge_sa.csv"
    if not csv_path.exists():
        return False, "chartedge_sa.csv not on disk", {"source": None}
    snap_path = _scan_snapshot_path(date_str)
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = csv_path.read_bytes()
        snap_path.write_bytes(raw)
    except Exception as e:
        return False, f"local write failed: {e}", {"source": "disk"}
    gh_ok, gh_msg = _commit_bytes_to_github(
        f"data/sa_scan/{date_str}.csv",
        raw,
        f"SA scan snapshot {date_str}",
    )
    return True, f"frozen; github: {gh_msg}", {
        "source": "disk",
        "bytes": len(raw),
        "github_committed": gh_ok,
        "github_message": gh_msg,
    }


def _freeze_sa_close(date_str: str, force: bool = False):
    """Freeze the current live SA feed as the post-close snapshot for date_str.

    Writes to disk (data/sa_close/<date>.xlsx) AND commits to GitHub so it
    survives Render redeploys. Returns (ok, message, source_meta).

    Refuses to freeze if there's no in-memory feed unless force=True, in
    which case it falls back to the disk all.xlsx (last manual upload).
    """
    snap_path = _close_snapshot_path(date_str)
    snap_path.parent.mkdir(parents=True, exist_ok=True)

    raw = None
    source = None
    feed_age_sec = None
    if _SA_STATE.get("xlsx"):
        raw = _SA_STATE["xlsx"]
        source = "live_memory"
        upd = _SA_STATE.get("updated")
        if upd:
            try:
                dt = datetime.fromisoformat(upd)
                feed_age_sec = (datetime.now() - dt).total_seconds()
            except Exception:
                feed_age_sec = None
    elif force and LIVE_XLSX_PATH.exists():
        raw = LIVE_XLSX_PATH.read_bytes()
        source = "disk_fallback"
    else:
        return False, "no live SA feed in memory; refuse to freeze stale data", {
            "source": None, "feed_age_sec": None,
        }

    try:
        snap_path.write_bytes(raw)
    except Exception as e:
        return False, f"local write failed: {e}", {
            "source": source, "feed_age_sec": feed_age_sec,
        }

    gh_ok, gh_msg = _commit_bytes_to_github(
        f"data/sa_close/{date_str}.xlsx",
        raw,
        f"SA close snapshot {date_str}",
    )
    return True, f"frozen ({source}); github: {gh_msg}", {
        "source": source,
        "feed_age_sec": feed_age_sec,
        "bytes": len(raw),
        "github_committed": gh_ok,
        "github_message": gh_msg,
    }


@app.route("/cron/closing-brief", methods=["GET", "POST"])
def cron_closing_brief():
    """Triggered by external cron (e.g. cron-job.org) at 15:30 AST Sun-Thu.

    Hardened flow:
      1. Verify the live SA feed is fresh (in-memory, last push within fresh-window).
      2. Freeze the current SA xlsx to disk + GitHub as the post-close snapshot.
      3. Compute the brief explicitly from that snapshot (reproducible).
      4. Save the brief JSON to disk + GitHub.

    Query params:
      token=<CRON_TOKEN>      required
      force=1                 skip freshness check (use disk fallback if no feed)
      date=YYYY-MM-DD         override the date stamp (default: today AST)
    """
    if not CRON_TOKEN:
        return {"error": "server missing CRON_TOKEN"}, 500
    token = request.args.get("token") or request.headers.get("X-Cron-Token")
    if token != CRON_TOKEN:
        return {"error": "bad token"}, 401

    force = request.args.get("force") == "1"
    today = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")

    # ── Step 1: freshness check on the live feed ──
    feed_age = None
    upd = _SA_STATE.get("updated")
    if upd:
        try:
            feed_age = (datetime.now() - datetime.fromisoformat(upd)).total_seconds()
        except Exception:
            feed_age = None

    if not force:
        if not _SA_STATE.get("xlsx"):
            return {
                "ok": False, "stage": "freshness_check",
                "error": "no live SA feed in memory — sar_feed.py is not pushing. "
                         "Start it on the desktop, wait for one push, then retry. "
                         "Or call with &force=1 to use the last disk all.xlsx (NOT recommended).",
            }, 409
        # Allow up to 6h of staleness — covers the case where sar_feed.py last pushed
        # at 15:20 and cron fires at 15:30. Reject older.
        if feed_age is not None and feed_age > 6 * 3600:
            return {
                "ok": False, "stage": "freshness_check",
                "error": f"live SA feed is stale (last push {feed_age/60:.0f} min ago). "
                         "Restart sar_feed.py, wait for a push, then retry. "
                         "Or call with &force=1 to use it anyway.",
                "feed_age_sec": feed_age,
            }, 409

    # ── Step 2a: freeze the Bloomberg xlsx ──
    frozen_ok, frozen_msg, frozen_meta = _freeze_sa_close(today, force=force)
    if not frozen_ok:
        return {
            "ok": False, "stage": "freeze_xlsx",
            "error": frozen_msg, "meta": frozen_meta,
        }, 500

    # ── Step 2b: freeze the ChartEdge scan CSV (non-fatal if it fails) ──
    scan_ok, scan_msg, scan_meta = _freeze_sa_scan(today)

    # ── Step 3: compute brief from the frozen snapshot ──
    import json as _json
    brief = _compute_brief_data(for_date=today)
    serial = _serialize_brief(brief)
    raw = _json.dumps(serial, indent=2, default=str).encode("utf-8")

    # ── Step 4: save brief JSON ──
    local_path = BRIEFS_DIR / f"{today}.json"
    try:
        local_path.write_bytes(raw)
    except Exception:
        pass
    ok, msg = _commit_brief_to_github(today, raw)

    return {
        "ok": True, "date": today, "bytes": len(raw),
        "snapshot": frozen_meta,
        "snapshot_message": frozen_msg,
        "scan_snapshot": scan_meta,
        "scan_snapshot_message": scan_msg,
        "scan_snapshot_ok": scan_ok,
        "market_source": brief.get("market_source"),
        "feed_age_sec_at_trigger": feed_age,
        "github_committed": ok, "github_message": msg,
        "tasi_close": (serial.get("index") or {}).get("today", {}).get("close") if serial.get("index") else None,
        "stage2": serial.get("stage2_count"),
        "score10": serial.get("score10_count"),
        "top_gainer": (serial.get("top_gainers") or [{}])[0].get("Ticker"),
        "top_loser": (serial.get("top_losers") or [{}])[0].get("Ticker"),
    }


@app.route("/admin/regen-brief", methods=["GET", "POST"])
def admin_regen_brief():
    """Manually regenerate today's (or any) brief.

    Useful when:
      - The 15:30 cron fired but the data was wrong.
      - You re-uploaded a post-close all.xlsx and want to rebuild from it.

    Query params:
      p=<ADMIN_PASSWORD>      required
      date=YYYY-MM-DD         which brief to (re)generate (default: today)
      force=1                 allow using disk fallback if no live feed
      use_existing_snapshot=1 skip the freeze step; recompute from the existing snapshot
    """
    if not ADMIN_PASSWORD or request.args.get("p") != ADMIN_PASSWORD:
        return ("Append ?p=<ADMIN_PASSWORD>. Optional: &date=YYYY-MM-DD&force=1&use_existing_snapshot=1",
                401, {"Content-Type": "text/plain"})

    today = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    force = request.args.get("force") == "1"
    use_existing = request.args.get("use_existing_snapshot") == "1"

    frozen_meta = {"source": "skipped_use_existing"}
    frozen_msg = "skipped"
    scan_ok = None
    scan_msg = "skipped"
    scan_meta = {"source": "skipped_use_existing"}
    if not use_existing:
        ok_freeze, frozen_msg, frozen_meta = _freeze_sa_close(today, force=force)
        if not ok_freeze:
            return {
                "ok": False, "stage": "freeze_xlsx",
                "error": frozen_msg, "meta": frozen_meta,
            }, 409
        scan_ok, scan_msg, scan_meta = _freeze_sa_scan(today)

    import json as _json
    brief = _compute_brief_data(for_date=today)
    serial = _serialize_brief(brief)
    raw = _json.dumps(serial, indent=2, default=str).encode("utf-8")
    local_path = BRIEFS_DIR / f"{today}.json"
    try:
        local_path.write_bytes(raw)
    except Exception:
        pass
    gh_ok, gh_msg = _commit_brief_to_github(today, raw)

    return {
        "ok": True, "date": today, "bytes": len(raw),
        "snapshot": frozen_meta, "snapshot_message": frozen_msg,
        "scan_snapshot": scan_meta, "scan_snapshot_message": scan_msg,
        "scan_snapshot_ok": scan_ok,
        "market_source": brief.get("market_source"),
        "github_committed": gh_ok, "github_message": gh_msg,
        "tasi_close": (serial.get("index") or {}).get("today", {}).get("close") if serial.get("index") else None,
        "stage2": serial.get("stage2_count"),
        "score10": serial.get("score10_count"),
        "top_gainer": (serial.get("top_gainers") or [{}])[0].get("Ticker"),
        "top_loser": (serial.get("top_losers") or [{}])[0].get("Ticker"),
    }


def _load_brief(date_str):
    """Load a brief JSON from disk."""
    p = BRIEFS_DIR / f"{date_str}.json"
    if not p.exists():
        return None
    try:
        import json as _json
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_briefs():
    """Return list of dates with saved briefs (newest first)."""
    if not BRIEFS_DIR.exists():
        return []
    files = sorted(
        [p for p in BRIEFS_DIR.glob("*.json") if len(p.stem) == 10],
        key=lambda p: p.stem, reverse=True,
    )
    return [p.stem for p in files]


@app.route("/brief")
def brief_index():
    """Public archive of all saved closing briefs."""
    dates = _list_briefs()
    return render_template("brief_index.html", dates=dates)


@app.route("/brief/<date_str>")
def brief_archived(date_str):
    """Public view of a specific date's archived brief."""
    if not (len(date_str) == 10 and date_str.count("-") == 2):
        abort(404)
    data = _load_brief(date_str)
    if not data:
        abort(404)
    return render_template("brief_view.html", date=date_str, brief=data)


# ── RS Rating archive (Minervini RPR — M1/M2/M3 percentiles) ───────────
RS_DIR = DATA_DIR / "rs_archive"
RS_DIR.mkdir(parents=True, exist_ok=True)


def _parse_rs_html(html_text: str, date_hint: str = None) -> dict:
    """Parse a ChartEdge_RS_Rating_<date>.html report into a structured dict.

    The report has the following tables:
      1. Top 10 by M2 (cols: #, Ticker, M1, M2, M3, 12m %, Q4 %, Price)
      2. Improved Tickers / Pre-Breakout (cols: Ticker, M1, M2, Gap, Justification)
      3. Full RS Rating Rankings (same shape as table 1, all tickers)
      4. Gap Analysis (M2 - M1)

    Returns a payload with: date, universe, top_leaders[], pre_breakout[],
    full_rankings[], stats. Safe with partial data — fields missing → None.
    """
    import re as _re
    h = html_text

    # Date — prefer info-bar span; fall back to date_hint
    date = date_hint
    m = _re.search(r'<label>\s*Date\s*</label>\s*<span>([^<]+)</span>', h)
    if m:
        raw = m.group(1).strip()
        # Try "May 30, 2026" → "2026-05-30"
        try:
            from datetime import datetime as _dt
            date = _dt.strptime(raw, "%b %d, %Y").strftime("%Y-%m-%d")
        except Exception:
            try:
                date = _dt.strptime(raw, "%B %d, %Y").strftime("%Y-%m-%d")
            except Exception:
                pass

    # Universe count
    universe = None
    m = _re.search(r'<label>\s*Universe\s*</label>\s*<span>([^<]+)</span>', h)
    if m:
        try:
            universe = int(_re.search(r'\d+', m.group(1)).group())
        except Exception:
            pass

    # Pull all tables
    tables = _re.findall(r'<table[^>]*>(.*?)</table>', h, _re.S)

    def parse_table(t):
        rows = _re.findall(r'<tr[^>]*>(.*?)</tr>', t, _re.S)
        return [
            [_re.sub(r'<[^>]+>', '', c).strip()
             for c in _re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', r, _re.S)]
            for r in rows
        ]

    leaders = parse_table(tables[0]) if tables else []
    improved = parse_table(tables[1]) if len(tables) >= 2 else []
    full = parse_table(tables[2]) if len(tables) >= 3 else []
    gaps = parse_table(tables[3]) if len(tables) >= 4 else []

    def _i(v):
        try: return int(v)
        except Exception: return None
    def _f(v):
        try: return float(v.replace(",", "").replace("%", "").replace("+", "").replace("SAR", "").strip())
        except Exception: return None
    def _tk(v):
        # "2380.SR" → "2380"
        return (v or "").split(".")[0].strip()

    # Leader rows: [#, Ticker, M1, M2, M3, 12m, Q4, Price]
    def parse_leader(r):
        if len(r) < 8: return None
        return {
            "rank":   _i(r[0]),
            "ticker": _tk(r[1]),
            "m1":     _i(r[2]),
            "m2":     _i(r[3]),
            "m3":     _i(r[4]),
            "pct12m": _f(r[5]),
            "pctQ4":  _f(r[6]),
            "price":  _f(r[7]),
        }
    # Improved rows: [Ticker, M1, M2, Gap, Justification]
    def parse_improved(r):
        if len(r) < 5: return None
        return {
            "ticker":       _tk(r[0]),
            "m1":           _i(r[1]),
            "m2":           _i(r[2]),
            "gap":          _i(r[3].replace("+", "")),
            "justification":r[4],
        }

    leader_rows = [parse_leader(r) for r in leaders[1:]] if leaders else []
    leader_rows = [r for r in leader_rows if r]
    improved_rows = [parse_improved(r) for r in improved[1:]] if improved else []
    improved_rows = [r for r in improved_rows if r]
    full_rows = [parse_leader(r) for r in full[1:]] if full else []
    full_rows = [r for r in full_rows if r]

    # Headline stats
    top_m2 = leader_rows[0]["m2"] if leader_rows else None
    pre_breakout_count = sum(1 for r in improved_rows if (r.get("gap") or 0) >= 15)

    return {
        "date": date,
        "universe": universe or len(full_rows) or None,
        "top_leaders":   leader_rows[:10],   # only the top 10 cards
        "pre_breakout":  improved_rows[:12], # cap UI density
        "full_rankings": full_rows or leader_rows,  # fall back if no separate full
        "stats": {
            "top_m2": top_m2,
            "pre_breakout_count": pre_breakout_count or len(improved_rows),
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _save_rs(date_str: str, payload: dict) -> tuple[bool, str]:
    """Save the parsed RS Rating payload to disk + commit to GitHub for persistence."""
    import json as _json
    raw = _json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    path = RS_DIR / f"{date_str}.json"
    try:
        path.write_bytes(raw)
    except Exception as e:
        return False, f"local write failed: {e}"
    ok, msg = _commit_bytes_to_github(
        f"data/rs_archive/{date_str}.json",
        raw,
        f"RS Rating archive {date_str}",
    )
    return True, f"saved ({msg})"


def _load_rs(date_str: str) -> dict:
    """Load an RS Rating JSON archive from disk."""
    p = RS_DIR / f"{date_str}.json"
    if not p.exists():
        return None
    try:
        import json as _json
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_rs() -> list:
    """List available RS Rating archive dates (newest first)."""
    if not RS_DIR.exists():
        return []
    files = sorted(
        [p for p in RS_DIR.glob("*.json") if len(p.stem) == 10],
        key=lambda p: p.stem, reverse=True,
    )
    return [p.stem for p in files]


@app.route("/rs-rating")
def rs_rating_latest():
    """Public — latest RS Rating report."""
    dates = _list_rs()
    if not dates:
        return render_template("rs_rating.html", data=None, date=None, archive_dates=[])
    return rs_rating_archived(dates[0])


@app.route("/rs-rating/<date_str>")
def rs_rating_archived(date_str):
    """Public view of an archived RS Rating report by date."""
    if not (len(date_str) == 10 and date_str.count("-") == 2):
        abort(404)
    data = _load_rs(date_str)
    if not data:
        abort(404)
    return render_template("rs_rating.html",
                           data=data, date=date_str, archive_dates=_list_rs())


@app.route("/rs-rating/index")
def rs_rating_index():
    """Public archive index of all saved RS Rating reports."""
    return render_template("rs_rating_index.html", dates=_list_rs())


def _tk(t):
    """Clean a ticker for display in tweets: drop '.SR' suffix."""
    if not t: return ""
    return str(t).split(".")[0].strip()


# Lazy ticker → Arabic-company-name lookup (loaded from chartedge_sa.csv on
# first call, cached for subsequent calls).
_TICKER_AR_CACHE = {"loaded": False, "map": {}}


def _ticker_ar_lookup():
    """Return a dict of {4-digit-ticker → Arabic-company-name}. Loaded lazily
    from data/chartedge_sa.csv on first call so the agent can render Arabic
    company names alongside numeric tickers."""
    if _TICKER_AR_CACHE["loaded"]:
        return _TICKER_AR_CACHE["map"]
    import csv as _csv
    out = {}
    csv_path = DATA_DIR / "chartedge_sa.csv"
    if csv_path.exists():
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                for r in _csv.DictReader(f):
                    t = (r.get("Ticker") or "").split(".")[0].strip()
                    name = (r.get("Company") or "").strip().strip('"')
                    if t and name:
                        out[t] = name
        except Exception:
            pass
    _TICKER_AR_CACHE["loaded"] = True
    _TICKER_AR_CACHE["map"] = out
    return out


def _tk_label_ar(t, sa_id=None):
    """Format a ticker for an Arabic tweet: '#TICKER name_ar' if we have an
    Arabic name, else just '#TICKER'. `sa_id` (Bloomberg "SA Exch ID" column)
    bridges Latin tickers like ARAMCO → 2222 → Arabic name."""
    code = _tk(t)
    if not code: return ""
    name_map = _ticker_ar_lookup()
    name = name_map.get(code)
    if not name and sa_id:
        sa_code = str(sa_id).strip()
        if sa_code:
            name = name_map.get(sa_code)
    return f"#{code} {name}" if name else f"#{code}"


def _build_tweet_drafts(brief):
    """Generate tweet templates from the brief data. Returns list of draft cards.

    Currently emits Arabic-only tweets. The EN side is still computed and
    persisted in tw['en'] for future re-enable, but the agent template
    shows only AR.
    """
    idx = brief.get("index") or brief.get("yahoo")  # accept legacy key from old archives
    today = idx.get("today") if idx else None
    prev = idx.get("prev") if idx else None
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
            f"{_tk_label_ar(g['Ticker'], g.get('SA Exch ID'))}  +{g['%1D']:.2f}%{vol_str_ar}\n"
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
            ar_lines.append(f"{medals[i]} {_tk_label_ar(s['Ticker'], s.get('SA Exch ID'))}  {ratio:.0f}% من المتوسط")
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
            ar_lines.append(f"📈 {_tk_label_ar(p['Ticker'])} — قوة نسبية {rs}، عام {twm}%")
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
        gn_ar = "\n".join(f"{_tk_label_ar(s['Ticker'], s.get('SA Exch ID'))}  +{s['%1D']:.2f}%" for s in gn)
        ls_ar = "\n".join(f"{_tk_label_ar(s['Ticker'], s.get('SA Exch ID'))}  {s['%1D']:.2f}%" for s in ls)
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
        s10_ar = "\n".join(f"{_tk_label_ar(p['Ticker'])} — قوة نسبية {p.get('RS','—')}" for p in s10)
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

    # ── 6. Sector Rotation Alert ─────────────────────────────────────
    # Where did the money actually go today? Top 3 sectors by average %1D
    # vs bottom 3. Built from the sector tags _compute_brief_data sets when
    # it walks the Bloomberg xlsx row stream.
    leading = brief.get("sectors_leading") or []
    lagging = brief.get("sectors_lagging") or []
    if leading and lagging and today:
        medals = ["🥇", "🥈", "🥉"]

        def _row_en(s, i):
            return (f"  {medals[i]} {s['name']}  {s['avg_pct']:+.2f}%  "
                    f"({s['advancers']}▲ / {s['decliners']}▼)  "
                    f"top: #{s['top_ticker']} {s['top_pct']:+.2f}%")
        def _row_ar(s, i):
            top_name = _ticker_ar_lookup().get(s['top_ticker'])
            top_lbl = f"#{s['top_ticker']} {top_name}" if top_name else f"#{s['top_ticker']}"
            return (f"  {medals[i]} {SECTOR_AR.get(s['name'], s['name'])}  {s['avg_pct']:+.2f}%  "
                    f"({s['advancers']}▲ / {s['decliners']}▼)  "
                    f"الأعلى: {top_lbl} {s['top_pct']:+.2f}%")

        en_lines = [f"🔀 #TASI Sector Rotation — {today['date']}", "",
                    "🔥 Leading sectors:"]
        for i, s in enumerate(leading): en_lines.append(_row_en(s, i))
        en_lines += ["", "❄️ Lagging sectors:"]
        for i, s in enumerate(lagging): en_lines.append(_row_en(s, i))
        en_lines += ["",
                     "Money rotated into the top names — watch for follow-through tomorrow.",
                     "→ trading-suite-l9e4.onrender.com/report"]

        ar_lines = [f"🔀 دوران القطاعات — #تاسي {today['date']}", "",
                    "🔥 القطاعات الرائدة:"]
        for i, s in enumerate(leading): ar_lines.append(_row_ar(s, i))
        ar_lines += ["", "❄️ القطاعات المتراجعة:"]
        for i, s in enumerate(lagging): ar_lines.append(_row_ar(s, i))
        ar_lines += ["",
                     "السيولة دارت إلى القطاعات الأولى — راقب الاستمرارية غداً.",
                     "→ trading-suite-l9e4.onrender.com/report"]

        drafts.append({
            "id": "sector_rotation",
            "title": "🔀 Sector Rotation Alert",
            "desc": "Where the money rotated today — top 3 sectors vs bottom 3 by average %1D.",
            "tweets": [{"en": "\n".join(en_lines), "ar": "\n".join(ar_lines)}],
        })

    return drafts


# Tadawul official Arabic sector names (used by Sector Rotation draft + future UI)
SECTOR_AR = {
    "Banks": "البنوك",
    "Energy": "الطاقة",
    "Materials": "المواد الأساسية",
    "Capital Goods": "السلع الرأسمالية",
    "Commercial & Professional Services": "الخدمات التجارية والمهنية",
    "Transportation": "النقل",
    "Consumer Durables & Apparel": "السلع الكمالية والملابس",
    "Consumer Services": "الخدمات الاستهلاكية",
    "Consumer Discretionary Distribution & Retail": "تجزئة الكماليات",
    "Consumer Staples Distribution & Retail": "تجزئة الأغذية",
    "Media & Entertainment": "الإعلام والترفيه",
    "Food, Beverage & Tobacco": "الأغذية والمشروبات والتبغ",
    "Health Care Equipment & Services": "معدات وخدمات الرعاية الصحية",
    "Pharmaceuticals, Biotechnology & Life Sciences": "الأدوية والتكنولوجيا الحيوية",
    "Diversified Financials": "الخدمات المالية المتنوعة",
    "Financial Services": "الخدمات المالية",
    "Insurance": "التأمين",
    "Software & Services": "البرمجيات والخدمات",
    "Technology Hardware & Equipment": "أجهزة وتقنيات",
    "Telecommunication Services": "خدمات الاتصالات",
    "Utilities": "المرافق العامة",
    "Equity Real Estate Investment Trusts (REITs)": "صناديق الريت العقارية",
    "Real Estate Management & Development": "إدارة وتطوير العقارات",
    "Household & Personal Products": "السلع المنزلية والشخصية",
}


@app.route("/agent")
def agent():
    """X/Twitter content generator from the last completed trading session.
    Admin-gated.

    Resolution order:
      1. ?date=YYYY-MM-DD  → load that specific archived brief
      2. Otherwise         → the most recently archived brief (last closed session)
      3. If no archive yet → live compute from in-memory feed (rare; first-day case)

    Why archive-first: tweet drafts should be reproducible, match what
    subscribers got at 15:30, and never reflect mid-session data the
    public hasn't seen yet. Re-opening /agent later in the day always
    gives the same drafts.
    """
    if not ADMIN_PASSWORD:
        return ("Server missing ADMIN_PASSWORD env var.", 500, {"Content-Type": "text/plain"})
    if request.args.get("p") != ADMIN_PASSWORD:
        return render_template("admin_gate.html"), 401

    date_q = request.args.get("date")
    archive_dates = _list_briefs()
    session_date = None
    brief = None
    source = None

    # 1. explicit date param
    if date_q and len(date_q) == 10 and date_q.count("-") == 2:
        loaded = _load_brief(date_q)
        if loaded:
            brief = loaded
            session_date = date_q
            source = "archive_explicit"

    # 2. latest archived brief
    if not brief and archive_dates:
        latest = archive_dates[0]
        loaded = _load_brief(latest)
        if loaded:
            brief = loaded
            session_date = latest
            source = "archive_latest"

    # 3. fallback: live compute (only used before first brief is published)
    if not brief:
        today = datetime.now().strftime("%Y-%m-%d")
        use_snap = _close_snapshot_path(today).exists()
        brief = _compute_brief_data(for_date=today if use_snap else None)
        session_date = today
        source = "live_compute"

    drafts = _build_tweet_drafts(brief)

    # Build prev/next links for in-page navigation between sessions
    prev_date = None
    next_date = None
    if session_date and session_date in archive_dates:
        i = archive_dates.index(session_date)
        # archive_dates is sorted newest-first
        if i + 1 < len(archive_dates):
            prev_date = archive_dates[i + 1]
        if i > 0:
            next_date = archive_dates[i - 1]

    return render_template(
        "agent.html",
        drafts=drafts,
        brief=brief,
        session_date=session_date,
        source=source,
        archive_dates=archive_dates,
        prev_date=prev_date,
        next_date=next_date,
    )


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


# ── Runtime GitHub file fetch ──────────────────────────────────────────────
# Admin uploads commit to GitHub, but Render's free tier has no persistent disk,
# so the live server otherwise only sees new data after a (currently blocked)
# redeploy. Fetching the scan CSVs from GitHub at request time — short cache,
# disk fallback — lets uploads go live WITHOUT a deploy. This is a READ only;
# it never commits, so it's safe on the request path (commits/deploys are the
# hot-path hazard, not reads — see CLAUDE.md).
_GH_FILE_CACHE = {}      # repo_path -> (fetched_at_epoch, bytes)
_GH_FILE_TTL = 60.0      # seconds → ≤60 GitHub reads/hour/file, far under limits


def _github_file_bytes(repo_path):
    """Current bytes of a repo file from GitHub, cached ~60s.

    Returns None on any failure so callers fall back to the on-disk copy.
    Serves the last-good cached copy if a refresh fetch fails transiently.
    """
    if not GH_PAT:
        return None
    import time as _t
    import requests as _rq
    now = _t.time()
    hit = _GH_FILE_CACHE.get(repo_path)
    if hit and (now - hit[0]) < _GH_FILE_TTL:
        return hit[1]
    try:
        r = _rq.get(
            f"https://api.github.com/repos/{GH_REPO}/contents/{repo_path}",
            headers={"Authorization": f"token {GH_PAT}",
                     "Accept": "application/vnd.github.v3.raw",
                     "User-Agent": "trading-suite"},
            params={"ref": GH_BRANCH}, timeout=8)
        if r.status_code == 200:
            _GH_FILE_CACHE[repo_path] = (now, r.content)
            return r.content
    except Exception:
        pass
    return hit[1] if hit else None   # last-good on transient failure


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
    # Scan CSVs: prefer the live GitHub copy so admin uploads appear without a
    # redeploy. Falls back to the on-disk copy when GitHub is unreachable.
    if filename in ("chartedge_us.csv", "chartedge_sa.csv"):
        data = _github_file_bytes(f"data/{filename}")
        if data is not None:
            return Response(data, mimetype="text/csv; charset=utf-8")
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
    # Cache the major-index ETFs so the homepage can render them without
    # re-parsing the xlsx on every visit.
    INDEX_TICKERS = {"SPY", "QQQ", "IWM", "DIA"}
    idx = {}
    for r in rows:
        t = (r.get("ticker") or "").upper().strip()
        if t in INDEX_TICKERS:
            idx[t] = {
                "price": r.get("price"),
                "chg":   r.get("chg"),
                "vol":   r.get("vol"),
            }
    _US_STATE["indices"] = idx
    if idx:
        _save_us_indices_cache(idx)
        # Refresh the GitHub fallback copy at most once per UTC day so the
        # homepage's last-close prices survive Render restarts.
        today_utc = datetime.utcnow().strftime("%Y-%m-%d")
        if _US_STATE.get("idx_gh_date") != today_utc:
            _US_STATE["idx_gh_date"] = today_utc
            try:
                import json as _json
                _commit_bytes_to_github(
                    "data/us_last_indices.json",
                    _json.dumps({"updated": _US_STATE["updated"],
                                 "indices": idx}, indent=2).encode("utf-8"),
                    f"US index price cache {today_utc}",
                )
            except Exception:
                pass
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
    age = None
    upd = _SA_STATE.get("updated")
    if upd:
        try:
            age = (datetime.now() - datetime.fromisoformat(upd)).total_seconds()
        except Exception:
            age = None
    # List frozen close snapshots so admin can see what's been captured
    snap_dir = DATA_DIR / "sa_close"
    snapshots = []
    if snap_dir.exists():
        snapshots = sorted([p.stem for p in snap_dir.glob("*.xlsx")], reverse=True)[:10]
    return {
        "have_data": bool(_SA_STATE.get("xlsx")),
        "bytes": _SA_STATE.get("size", 0),
        "updated": _SA_STATE.get("updated"),
        "age_sec": age,
        "fresh": (age is not None and age < 600),  # last push within 10 min
        "close_snapshots": snapshots,
    }


@app.route("/api/data")
def api_data():
    return jsonify(load_live_data())


@app.route("/healthz")
def healthz():
    return {"ok": True}


# ── Analyze-a-stock requests → admin's Telegram (via bot) ──────────────
TELEGRAM_BOT_TOKEN = _secret("TELEGRAM_BOT_TOKEN")
ANALYZE_CHAT_ID    = _secret("ANALYZE_CHAT_ID")   # admin's numeric Telegram chat id
_analyze_hits = {}   # ip -> [timestamps]  (simple in-memory rate limit)


# ── Site assistant (Gemini-powered Q&A about ChartEdge) ────────────────────
GEMINI_API_KEY = _secret("GEMINI_API_KEY")
GEMINI_MODEL   = _secret("GEMINI_MODEL", "gemini-2.0-flash")
_chat_hits = {}   # ip -> [timestamps]

# Grounding for the assistant. Scope is intentionally narrow: explain the
# website and its products, never give investment advice. Keep it factual —
# anything not here, the bot should say it doesn't know rather than invent.
ASSISTANT_SYSTEM_PROMPT = """You are the ChartEdge assistant — a friendly, concise guide for visitors of the ChartEdge Analytics website. You are bilingual: ALWAYS reply in the same language the visitor writes in (Arabic or English). For Arabic, write natural Modern Standard Arabic.

WHAT CHARTEDGE IS
ChartEdge Analytics is a live market-analysis platform. It is NOT a signals or recommendations service — it gives traders the tools to read the market themselves, instead of depending on paid analyst alerts or Telegram signals.

PRODUCTS
1. ChartEdge — a SEPA/VCP scanner that scores stocks out of 10 and surfaces Stage-2 and near-breakout names. The full scanner is a desktop app; the web view is display-only. To download the desktop app, share this link: https://drive.google.com/drive/folders/1FiRXYRO-rrrlVlMBc3ly-fsPPwYmM4FA?usp=drive_link
2. TradePulse TASI — a live Saudi (Tadawul) dashboard: live watchlist, market breadth, sector rotation, and breakout signals, refreshed about every 30 seconds during the session.
3. TradePulse US — a live NYSE/NASDAQ dashboard with SEPA scoring, a sector heatmap, and the SPY/QQQ/IWM indices.
4. RS Rating — a daily Minervini-style relative-strength ranking (M1/M2/M3 sub-scores) that highlights real leaders and pre-breakout candidates.
5. Daily Closing Brief — an automated end-of-session summary (close, top gainers/losers, strongest sector, breadth), also delivered on Telegram.

TERMS (define briefly only to explain a feature)
- SEPA = Mark Minervini's Specific Entry Point Analysis. VCP = Volatility Contraction Pattern. Stage 2 = the uptrend/markup phase in stage analysis. RS Rating = relative strength vs the market.

HOW TO USE THE SITE
- Open the TASI or US dashboard from the top navigation. Dashboards are best viewed on a laptop/desktop (heavy data).
- "Request a company analysis" (button in the top bar): enter a ticker and your Telegram handle (required) — the analyst replies on Telegram, outside trading hours.
- Join the Telegram channel for the daily brief: https://t.me/+R_m5lVLFnBJhYWJk . Contact/admin: @chartedgeai .

RULES (important)
- Only answer questions about the ChartEdge website, its products, and how to use them. If asked something unrelated, politely say you can only help with ChartEdge.
- This platform is for educational and informational purposes only and is NOT investment advice. NEVER give buy/sell/hold recommendations, price targets, or predictions for any specific security. If asked, politely decline, note it is not investment advice, and suggest the "Request a company analysis" button plus reviewing the site disclaimer.
- You do NOT have live prices or specific current scores. For live data, point the visitor to the relevant dashboard rather than guessing.
- Keep answers short (a few sentences). Be warm and professional. Never invent features or facts that aren't above."""


# Fold the official PDF manuals (extracted to data/kb_chartedge.md) into the
# system prompt at startup, so the assistant answers detailed how-to / feature
# questions from the real ChartEdge & TradePulse guides — not just the summary.
def _load_assistant_kb():
    try:
        p = DATA_DIR / "kb_chartedge.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


_ASSISTANT_KB = _load_assistant_kb()
if _ASSISTANT_KB:
    ASSISTANT_SYSTEM_PROMPT += (
        "\n\n## REFERENCE MANUALS — المصدر الموثوق للأسئلة التفصيلية\n"
        "Use the manuals below to answer detailed how-to and feature questions about "
        "ChartEdge and TradePulse. Quote the steps faithfully; if something isn't "
        "covered, say so and point the user to the Read-Guide button or Contact.\n\n"
        + _ASSISTANT_KB
    )


@app.route("/assistant")
def assistant_page():
    return render_template("assistant.html")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Site assistant: relay a visitor question to Gemini with ChartEdge
    grounding. Scope-limited + rate-limited to keep cost and liability low."""
    import time as _t

    if not GEMINI_API_KEY:
        return {"ok": False, "error": "assistant is not configured yet"}, 503

    # Rate limit: 30 messages / 10 min per IP
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?").split(",")[0].strip()
    now = _t.time()
    hits = [t for t in _chat_hits.get(ip, []) if now - t < 600]
    if len(hits) >= 30:
        return {"ok": False, "error": "too many messages — please wait a few minutes"}, 429
    hits.append(now)
    _chat_hits[ip] = hits

    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}

    message = str(body.get("message", "")).strip()[:1000]
    if not message:
        return {"ok": False, "error": "empty message"}, 400

    # Optional short history for context: [{role:'user'|'assistant', text:'…'}]
    contents = []
    hist = body.get("history")
    if isinstance(hist, list):
        for turn in hist[-8:]:
            if not isinstance(turn, dict):
                continue
            role = "model" if str(turn.get("role")) == "assistant" else "user"
            text = str(turn.get("text", "")).strip()[:1000]
            if text:
                contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "system_instruction": {"parts": [{"text": ASSISTANT_SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024},
    }

    try:
        import requests as _rq
        import time as _t2
        # Try the configured model first, then fall back to known-good ones so a
        # stale/renamed default never takes the assistant down.
        candidates = []
        for m in (GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash"):
            if m and m not in candidates:
                candidates.append(m)
        data = None
        last_status, last_body = None, ""
        # Free-tier quota (429) is per-model, so if one model is exhausted another
        # may still answer — try each candidate before giving up.
        for m in candidates:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{m}:generateContent?key={GEMINI_API_KEY}")
            r = _rq.post(url, json=payload, timeout=30)
            last_status, last_body = r.status_code, (r.text or "")[:400]
            if r.status_code == 200:
                data = r.json()
                break
            if r.status_code in (429, 404, 500, 503):
                continue   # model busy/unavailable — try the next one
            break          # auth/bad-request etc. — stop, fallbacks won't help
        if data is None and last_status == 429:
            # All models rate-limited — one short backoff, then retry the primary.
            _t2.sleep(2)
            r = _rq.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{candidates[0]}:generateContent?key={GEMINI_API_KEY}",
                json=payload, timeout=30)
            last_status, last_body = r.status_code, (r.text or "")[:400]
            if r.status_code == 200:
                data = r.json()
        if data is None:
            # Surface the real reason in the server logs (Render → Logs) so quota
            # vs rate-limit vs auth is diagnosable without guesswork.
            print(f"[chat] Gemini failed: status={last_status} body={last_body}", flush=True)
            if last_status == 429:
                # Quota / rate limit — keep the UX graceful instead of an error.
                return {"ok": True, "reply": (
                    "The assistant is at capacity right now — please try again a bit later. "
                    "· المساعد مشغول حالياً، يرجى المحاولة بعد قليل.")}
            return {"ok": False, "error": f"assistant error ({last_status})"}, 502
        cands = data.get("candidates") or []
        if not cands:
            # Safety block or empty — give a graceful fallback
            return {"ok": True, "reply": "Sorry, I couldn't answer that. Try rephrasing, "
                    "or use 'Request a company analysis' to reach the analyst."}
        parts = (cands[0].get("content") or {}).get("parts") or []
        reply = "".join(p.get("text", "") for p in parts).strip()
        if not reply:
            reply = "Sorry, I couldn't answer that one."
        return {"ok": True, "reply": reply}
    except Exception as e:
        return {"ok": False, "error": f"assistant failed: {e}"}, 502


@app.route("/api/analyze-request", methods=["POST"])
def api_analyze_request():
    """Visitor drops a ticker → we relay it to the admin's Telegram DM via a
    bot. The visitor includes their own Telegram handle so the admin can
    reply directly. Rate-limited to curb spam."""
    import time as _t
    import re as _re

    if not (TELEGRAM_BOT_TOKEN and ANALYZE_CHAT_ID):
        return {"ok": False, "error": "analysis requests are not configured yet"}, 503

    # ── Rate limit: max 5 requests / 10 min per IP ──
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?").split(",")[0].strip()
    now = _t.time()
    window = 600
    hits = [t for t in _analyze_hits.get(ip, []) if now - t < window]
    if len(hits) >= 5:
        return {"ok": False, "error": "too many requests — please wait a few minutes"}, 429
    hits.append(now)
    _analyze_hits[ip] = hits

    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}

    ticker  = str(body.get("ticker", "")).strip().upper()[:20]
    market  = str(body.get("market", "")).strip()[:10]
    contact = str(body.get("contact", "")).strip()[:60]
    note    = str(body.get("note", "")).strip()[:280]

    # Validate ticker: 1-10 alphanumerics (handles "2222", "AAPL", "RJHI")
    if not _re.match(r"^[A-Z0-9.\-]{1,12}$", ticker):
        return {"ok": False, "error": "Please enter a valid ticker (e.g. 2222 or AAPL)."}, 400

    # Telegram handle is REQUIRED so the analyst has somewhere to reply.
    if len(contact.lstrip("@").strip()) < 4:
        return {"ok": False,
                "error": "Please enter your Telegram so the analyst can reply (e.g. @yourname)."}, 400

    flag = "🇸🇦" if market.upper() in ("TASI", "SA", "SAR") else ("🇺🇸" if market.upper() in ("US", "USA") else "📈")
    contact_line = f"\n👤 Reply to: {contact}"
    note_line    = f"\n📝 Note: {note}" if note else ""

    msg = (
        f"📊 *New analysis request*\n"
        f"{flag} Ticker: *{ticker}* ({market or 'unspecified'})"
        f"{contact_line}"
        f"{note_line}\n"
        f"🌐 via ChartEdge website"
    )

    try:
        import requests as _rq
        r = _rq.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": ANALYZE_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return {"ok": True}
        return {"ok": False, "error": f"telegram error: {r.text[:160]}"}, 502
    except Exception as e:
        return {"ok": False, "error": f"send failed: {e}"}, 502


# ── Visit counter — persistent across redeploys ────────────────────────
# In-memory hot state, periodically flushed to disk + committed to GitHub
# so cumulative counts survive worker restarts on Render's free plan.
import threading as _threading
from collections import defaultdict as _defaultdict
from datetime import timedelta as _timedelta
import json as _json
import time as _time

_visit_lock = _threading.Lock()
_visit_counts = {}                                      # path -> all-time count
_visit_daily = _defaultdict(lambda: _defaultdict(int))  # 'YYYY-MM-DD' -> path -> count
_visit_first_seen = datetime.now()
_visit_dirty = False           # set True on every new visit
_visit_last_disk_flush = 0.0   # epoch seconds of last disk write
_visit_last_github_flush = 0.0 # epoch seconds of last GitHub commit
_visit_flush_lock = _threading.Lock()

VISIT_COUNTS_PATH = DATA_DIR / "visit_counts.json"
# Hot-flush to local disk every N seconds (free, doesn't trigger redeploy).
_VISIT_DISK_FLUSH_INTERVAL = 300
# GitHub commit (which DOES trigger a Render redeploy) only happens via
# explicit /admin/flush-stats OR a daily background timer — never on every
# visit. Otherwise we'd push 100+ commits/day and Render free tier would
# never finish a real code deploy.
_VISIT_GITHUB_FLUSH_INTERVAL = 86400  # once per day at most

# Pages we actually want to count (excludes APIs, static, /admin, etc.)
_COUNTABLE_ENDPOINTS = {"index", "product_page", "market_report", "contact"}


def _load_visit_counts():
    """Restore visit counts from disk on app boot. Non-fatal if file missing
    or unreadable — just starts fresh in that case."""
    global _visit_first_seen
    if not VISIT_COUNTS_PATH.exists():
        return
    try:
        d = _json.loads(VISIT_COUNTS_PATH.read_text(encoding="utf-8"))
        totals = d.get("totals") or {}
        daily = d.get("daily") or {}
        fs = d.get("first_seen")
        with _visit_lock:
            _visit_counts.update(totals)
            for date_str, by_path in daily.items():
                for p, n in by_path.items():
                    _visit_daily[date_str][p] = int(n)
            if fs:
                try:
                    _visit_first_seen = datetime.fromisoformat(fs)
                except Exception:
                    pass
    except Exception:
        pass  # corrupted file -> start fresh, don't crash boot


def _flush_visit_counts(commit_to_github: bool = False):
    """Write visit counts to disk and (optionally) commit to GitHub. Safe to
    call from any thread. By default this writes to local disk only — set
    commit_to_github=True for the daily / manual commit path.

    GitHub commits trigger a Render redeploy, so we keep them rare; otherwise
    the build queue churns and real code deploys never finish."""
    global _visit_last_disk_flush, _visit_last_github_flush, _visit_dirty
    with _visit_flush_lock:
        with _visit_lock:
            payload = {
                "first_seen": _visit_first_seen.isoformat(timespec="seconds"),
                "last_flushed": datetime.now().isoformat(timespec="seconds"),
                "totals": dict(_visit_counts),
                "daily": {k: dict(v) for k, v in _visit_daily.items()},
            }
        raw = _json.dumps(payload, indent=2, default=str).encode("utf-8")
        try:
            VISIT_COUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            VISIT_COUNTS_PATH.write_bytes(raw)
            _visit_last_disk_flush = _time.time()
        except Exception:
            pass
        if commit_to_github:
            try:
                _commit_bytes_to_github(
                    "data/visit_counts.json",
                    raw,
                    f"Visit stats flush ({payload.get('totals',{}).get('_total','?')} total)",
                )
                _visit_last_github_flush = _time.time()
            except Exception:
                pass
        _visit_dirty = False


def _maybe_flush_visit_counts():
    """Throttled flush — LOCAL DISK ONLY, never GitHub.

    Why disk-only: every GitHub commit triggers a Render auto-deploy, and
    each deploy restarts the worker. The old 'once per day' GitHub guard
    used an in-memory timestamp (_visit_last_github_flush) that RESET to 0
    on every restart — so after each deploy it thought 24h had passed and
    committed again, which triggered another deploy, which restarted, which
    committed again… a self-sustaining loop that burned all the Render
    pipeline minutes.

    Fix: the per-visit path now ONLY writes to the container's local disk
    (free, no deploy). Cumulative counts still survive redeploys because
    _load_visit_counts() restores them from the committed repo snapshot on
    boot. To persist a fresh permanent checkpoint to GitHub, the admin hits
    /admin/flush-stats explicitly — that's the only path that commits."""
    if not _visit_dirty:
        return
    now = _time.time()
    if (now - _visit_last_disk_flush) >= _VISIT_DISK_FLUSH_INTERVAL:
        _flush_visit_counts(commit_to_github=False)   # disk only, never auto-commit


# Restore previous counts at boot — runs once when the module loads
_load_visit_counts()


# ── Maintenance / Under-Construction gate ──────────────────────────────
# Toggle by setting MAINTENANCE=1 in Render env vars. Backend stays alive
# so sar_feed.py/us_feed.py can still push, cron-job.org can still trigger
# the closing brief, and you can still admin via ?p=ADMIN_PASSWORD.
_MAINT_ALLOW_ENDPOINTS = {
    "healthz",            # uptime monitor
    "api_sa_push",        # sar_feed.py POST → keep the feed pipeline alive
    "api_sa_status",
    "api_us_push",
    "api_us_status",
    "cron_closing_brief", # external cron at 15:30 AST
    "static",             # logo + css for the maintenance page itself
    "data_file",          # /data/<filename> — closing brief json is downloadable
}
# Path prefixes that bypass the gate (covers /admin/*, /api/* etc.).
_MAINT_ALLOW_PATH_PREFIXES = ("/admin", "/api/", "/cron/", "/static/", "/healthz")


def _maintenance_active() -> bool:
    v = _secret("MAINTENANCE", "")
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@app.before_request
def _maintenance_gate():
    if not _maintenance_active():
        return
    # Admin with valid password bypasses (so you can preview the live site
    # while everyone else sees the maintenance page).
    if ADMIN_PASSWORD and request.args.get("p") == ADMIN_PASSWORD:
        return
    # Whitelisted endpoints + paths bypass.
    if request.endpoint in _MAINT_ALLOW_ENDPOINTS:
        return
    path = request.path or ""
    for pref in _MAINT_ALLOW_PATH_PREFIXES:
        if path.startswith(pref):
            return
    # Everything else gets the maintenance page (HTTP 503 so search engines
    # know it's temporary, not a real 404).
    return render_template("maintenance.html"), 503, {
        "Cache-Control": "no-store, max-age=0",
        "Retry-After": "3600",  # hint to bots: check back in an hour
    }


@app.before_request
def _count_visit():
    global _visit_dirty
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
        _visit_dirty = True
    # Throttled flush in a background thread so visitors don't wait on
    # a GitHub PUT (which can take 1-2 sec).
    _threading.Thread(target=_maybe_flush_visit_counts, daemon=True).start()


@app.route("/admin/stats")
def admin_stats():
    # Auth via admin password as ?p= query param so you can bookmark
    if not ADMIN_PASSWORD or request.args.get("p") != ADMIN_PASSWORD:
        return ("Append ?p=<ADMIN_PASSWORD> to the URL.", 401, {"Content-Type": "text/plain"})

    # Force a flush (local disk only — skip GitHub commit) so the page
    # always shows current numbers including in-memory pending visits.
    if _visit_dirty:
        try:
            _flush_visit_counts(commit_to_github=False)
        except Exception:
            pass

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


@app.route("/admin/flush-stats")
def admin_flush_stats():
    """Force a sync flush of visit counts to disk + GitHub. Admin-gated."""
    if not ADMIN_PASSWORD or request.args.get("p") != ADMIN_PASSWORD:
        return ("Append ?p=<ADMIN_PASSWORD> to the URL.", 401, {"Content-Type": "text/plain"})
    _flush_visit_counts(commit_to_github=True)
    with _visit_lock:
        total = _visit_counts.get("_total", 0)
        first = _visit_first_seen.isoformat(timespec="seconds")
        last = datetime.now().isoformat(timespec="seconds")
    return {
        "ok": True,
        "total_visits_since_launch": total,
        "first_seen": first,
        "flushed_at": last,
    }


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

    # Special case: ChartEdge RS Rating HTML report → parse + archive as JSON
    fname_lower = (f.filename or "").lower()
    is_rs_report = (
        fname_lower.endswith(".html")
        and ("rs_rating" in fname_lower or "rs-rating" in fname_lower
             or b"RS Rating" in raw[:4096] or b"M2 (Minervini RPR)" in raw[:8192])
    )
    if is_rs_report:
        try:
            # Try to pull date from filename: ChartEdge_RS_Rating_2026-05-30.html
            import re as _re_fn
            dm = _re_fn.search(r"(\d{4}-\d{2}-\d{2})", f.filename or "")
            date_hint = dm.group(1) if dm else None
            payload = _parse_rs_html(raw.decode("utf-8", errors="ignore"),
                                      date_hint=date_hint)
            if not payload.get("date"):
                payload["date"] = date_hint or datetime.now().strftime("%Y-%m-%d")
            if not payload.get("top_leaders"):
                return render_template("admin.html", success=False,
                    error="Could not parse RS Rating report — no leader rows found. "
                          "Make sure it's the ChartEdge HTML report (not the PDF)."), 400
            ok, msg = _save_rs(payload["date"], payload)
            if not ok:
                return render_template("admin.html", success=False,
                    error=f"RS Rating save failed: {msg}"), 500
            return render_template("admin.html", success=True,
                error=f"RS Rating archived for {payload['date']} "
                      f"({len(payload['top_leaders'])} leaders · "
                      f"{len(payload['pre_breakout'])} pre-breakout · "
                      f"{payload.get('universe','?')} universe). "
                      f"Live at /rs-rating")
        except Exception as e:
            return render_template("admin.html", success=False,
                error=f"RS Rating parse exception: {e}"), 500

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
