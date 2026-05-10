from flask import Flask, render_template, jsonify, abort, send_from_directory, request
import pandas as pd
import math
import os
import base64
from datetime import datetime
from pathlib import Path
from config import PRODUCTS, LIVE_XLSX_PATH, LIVE_XLSX_SHEET, REFRESH_SECONDS, DATA_DIR

app = Flask(__name__)


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


def _resolve_upload_target(filename: str, market_form: str = None, raw: bytes = None):
    """Decide where the uploaded file should be committed in the GitHub repo.

    Priority:
      1. Explicit market dropdown ('us' or 'sa')
      2. Filename markers ('_US', '_SA', etc.)
      3. CSV content sniff (numeric Tadawul codes -> Saudi, else US)
    """
    n = filename.lower()
    if n.endswith(".xlsx"):
        return GH_XLSX_PATH, None
    if not n.endswith(".csv"):
        return None, "Upload a .xlsx or .csv file."

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
# in-memory live US feed: bytes of xlsx + last update timestamp
_US_STATE = {"xlsx": None, "updated": None, "rows": 0}

# Fast-path in-memory cache for admin-uploaded files.
# When set, /data/<filename> serves these directly (no Render redeploy required).
# Cleared when the dyno restarts; falls back to whatever's on disk from the last GitHub commit.
_LIVE_FILES = {
    "all.xlsx":         {"data": None, "updated": None},
    "chartedge_us.csv": {"data": None, "updated": None},
    "chartedge_sa.csv": {"data": None, "updated": None},
}

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
    if not LIVE_XLSX_PATH.exists():
        return {"error": f"xlsx not found: {LIVE_XLSX_PATH}", "rows": [], "columns": []}
    df = pd.read_excel(LIVE_XLSX_PATH, sheet_name=LIVE_XLSX_SHEET)
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
        "source": LIVE_XLSX_PATH.name,
    }


@app.route("/")
def index():
    return render_template("index.html", products=PRODUCTS)


@app.route("/p/<key>")
def product_page(key):
    if key not in PRODUCTS:
        abort(404)
    show_live = (key == "tradepulse_sar")
    needs_ibkr = (key == "tradepulse_us")
    auto_load_data = key.startswith("tradepulse")
    return render_template(
        "product.html",
        key=key,
        product=PRODUCTS[key],
        show_live=show_live,
        needs_ibkr=needs_ibkr,
        auto_load_data=auto_load_data,
        refresh_seconds=REFRESH_SECONDS,
    )


@app.route("/p/<key>/file")
def product_file(key):
    if key not in PRODUCTS:
        abort(404)
    p = PRODUCTS[key]
    return send_from_directory(p["dir"], p["file"])


_MIME = {
    "all.xlsx":         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "us_live.xlsx":     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "chartedge_us.csv": "text/csv; charset=utf-8",
    "chartedge_sa.csv": "text/csv; charset=utf-8",
}


@app.route("/data/<path:filename>")
def data_file(filename):
    from flask import Response
    if filename == "us_live.xlsx":
        if not _US_STATE.get("xlsx"):
            abort(404)
        return Response(_US_STATE["xlsx"], mimetype=_MIME["us_live.xlsx"])
    if filename in _LIVE_FILES:
        # 1. Fast path — in-memory upload
        cached = _LIVE_FILES[filename].get("data")
        if cached is not None:
            return Response(cached, mimetype=_MIME[filename])
        # 2. Fallback — last committed file on disk
        if (DATA_DIR / filename).exists():
            return send_from_directory(DATA_DIR, filename)
        abort(404)
    abort(404)


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


@app.route("/api/data")
def api_data():
    return jsonify(load_live_data())


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/admin", methods=["GET"])
def admin_page():
    return render_template("admin.html", success=False, error=None, success_msg=None)


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
    target_path, why = _resolve_upload_target(f.filename, market_form, raw)
    if not target_path:
        return render_template("admin.html", success=False, error=why), 400

    target_filename = target_path.rsplit("/", 1)[-1]

    # FAST PATH — write to in-memory cache so /data/<filename> serves it instantly.
    # Visitors see the new data on their next 30s polling tick (no Render redeploy).
    if target_filename in _LIVE_FILES:
        _LIVE_FILES[target_filename]["data"] = raw
        _LIVE_FILES[target_filename]["updated"] = datetime.now().isoformat(timespec="seconds")

    persist = request.form.get("persist") == "on"
    if not persist:
        return render_template(
            "admin.html",
            success=True, error=None,
            success_msg=(f"Live in-memory now (visitors see it on next 30s tick). "
                         f"NOT saved to GitHub — will be lost on next Render restart. "
                         f"Re-upload with the 'Save permanently' checkbox to back it up.")
        )

    # SLOW PATH — also commit to GitHub (will trigger a ~3 min Render redeploy that
    # wipes the in-memory cache and serves from disk afterwards).
    api = f"https://api.github.com/repos/{GH_REPO}/contents/{target_path}"
    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "trading-suite-admin",
    }

    sha = None
    try:
        r = _rq.get(api, headers=headers, params={"ref": GH_BRANCH}, timeout=30)
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
        r = _rq.put(api, headers=headers, json=payload, timeout=60)
    except Exception as e:
        return render_template("admin.html", success=False,
            error=f"GitHub PUT exception: {e}"), 502

    if r.status_code in (200, 201):
        return render_template(
            "admin.html",
            success=True, error=None,
            success_msg=("Live in-memory now (visitors see it on next 30s tick). "
                         "Also saved permanently to GitHub — Render will redeploy in ~3 min.")
        )
    return render_template("admin.html", success=False,
        error=f"GitHub PUT failed ({r.status_code}): {r.text[:300]}"), 502


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
