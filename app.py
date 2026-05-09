from flask import Flask, render_template, jsonify, abort, send_from_directory, request
import pandas as pd
import math
import os
import base64
from datetime import datetime
from config import PRODUCTS, LIVE_XLSX_PATH, LIVE_XLSX_SHEET, REFRESH_SECONDS, DATA_DIR

app = Flask(__name__)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
GH_PAT = os.getenv("GH_PAT")
GH_REPO = os.getenv("GH_REPO", "aboaziz112211/trading_suite")
GH_BRANCH = os.getenv("GH_BRANCH", "main")
GH_XLSX_PATH = "data/all.xlsx"
GH_CSV_PATH = "data/chartedge.csv"

UPLOAD_TARGETS = {
    ".xlsx": GH_XLSX_PATH,
    ".csv": GH_CSV_PATH,
}


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
    return render_template(
        "product.html",
        key=key,
        product=PRODUCTS[key],
        show_live=show_live,
        needs_ibkr=needs_ibkr,
        refresh_seconds=REFRESH_SECONDS,
    )


_AUTO_LOAD_SCRIPT = """
<script>
(function(){
  function dispatchFile(inputId, file){
    var inp = document.getElementById(inputId);
    if(!inp) return false;
    try {
      var dt = new DataTransfer();
      dt.items.add(file);
      inp.files = dt.files;
      inp.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    } catch(e){ console.warn('[auto-load] dispatch failed for '+inputId+':', e); return false; }
  }
  async function loadFromServer(url, name, mime, inputId){
    try {
      var r = await fetch(url, {cache:'no-store'});
      if(!r.ok){ console.log('[auto-load] '+url+' -> '+r.status+' (skipping)'); return; }
      var blob = await r.blob();
      var f = new File([blob], name, {type: mime});
      console.log('[auto-load] feeding '+name+' ('+blob.size+' bytes) -> #'+inputId);
      dispatchFile(inputId, f);
    } catch(e){ console.warn('[auto-load] failed for '+name+':', e); }
  }
  function start(){
    setTimeout(async function(){
      await loadFromServer('/data/all.xlsx', 'all.xlsx',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'bbg-in');
      await new Promise(function(r){ setTimeout(r, 1800); });
      await loadFromServer('/data/chartedge.csv', 'chartedge.csv', 'text/csv', 'ce-in');
    }, 1200);
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', start);
  } else { start(); }
})();
</script>
"""


@app.route("/p/<key>/file")
def product_file(key):
    if key not in PRODUCTS:
        abort(404)
    p = PRODUCTS[key]
    if key.startswith("tradepulse"):
        try:
            html = (p["dir"] / p["file"]).read_text(encoding="utf-8")
            if "</body>" in html:
                html = html.replace("</body>", _AUTO_LOAD_SCRIPT + "</body>", 1)
            else:
                html += _AUTO_LOAD_SCRIPT
            return html, 200, {"Content-Type": "text/html; charset=utf-8"}
        except Exception:
            pass
    return send_from_directory(p["dir"], p["file"])


@app.route("/data/<path:filename>")
def data_file(filename):
    if filename not in {"all.xlsx", "chartedge.csv"}:
        abort(404)
    return send_from_directory(DATA_DIR, filename)


@app.route("/p/<key>/download")
def product_download(key):
    if key not in PRODUCTS:
        abort(404)
    p = PRODUCTS[key]
    return send_from_directory(p["dir"], p["file"], as_attachment=True)


@app.route("/api/data")
def api_data():
    return jsonify(load_live_data())


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/admin", methods=["GET"])
def admin_page():
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

    fname = f.filename.lower()
    target_path = None
    for ext, path in UPLOAD_TARGETS.items():
        if fname.endswith(ext):
            target_path = path
            break
    if not target_path:
        return render_template("admin.html", success=False,
            error="Upload a .xlsx or .csv file."), 400

    raw = f.read()
    if len(raw) > 25 * 1024 * 1024:
        return render_template("admin.html", success=False,
            error="File too large (>25 MB)."), 400

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
        return render_template("admin.html", success=True, error=None)
    return render_template("admin.html", success=False,
        error=f"GitHub PUT failed ({r.status_code}): {r.text[:300]}"), 502


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
