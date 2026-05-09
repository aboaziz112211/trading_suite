# Trading Suite — Website

Public portal for **ChartEdge**, **TradePulse US**, and **TradePulse SAR**, with a live data table on the SAR page that streams from `data/all.xlsx` every 30s.

## Share with others — 4 steps

You'll get a permanent public URL like `https://trading-suite.onrender.com` that anyone can open from any device.

### 1. Create a GitHub repo and push this folder

```powershell
cd "C:\Users\wlayo\OneDrive\Desktop\Claude Projects\08_Website"
git init -b main
git add .
git commit -m "Initial website"
gh repo create trading-suite --public --source=. --push
```

(If you don't have `gh`, create an empty repo on github.com → it'll give you the two `git remote add` + `git push` lines to paste.)

### 2. Sign in to https://render.com (use your existing account)

### 3. New + → Web Service → connect the `trading-suite` repo

Render reads [`render.yaml`](render.yaml) and auto-fills everything. Just click **Create Web Service**.

### 4. Wait ~3 min, then share the URL

Render shows it at the top: `https://trading-suite-xxxx.onrender.com`. Send that to anyone.

---

## Updating data / dashboards

Whenever you update ChartEdge, TradePulse, or `all .xlsx` in their original folders:

```powershell
cd "C:\Users\wlayo\OneDrive\Desktop\Claude Projects\08_Website"
.\sync.bat
git add .
git commit -m "refresh assets"
git push
```

Render auto-deploys on push (~2 min). [`sync.bat`](sync.bat) just copies the latest files from `01_ChartEdge/` and `07_TradePulse/` into `products/` and `data/`.

---

## Run locally

```powershell
cd "C:\Users\wlayo\OneDrive\Desktop\Claude Projects\08_Website"
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

---

## What works on the deployed site

| | Local | Render |
|---|---|---|
| Home + ChartEdge | ✅ | ✅ |
| TradePulse SAR + live xlsx table | ✅ | ✅ (refreshes from the bundled xlsx; rerun `sync.bat`+push to update) |
| **TradePulse US (IBKR live)** | ✅ if TWS + `us_feed.py` running locally | ❌ won't have live data — IBKR feed is your local machine only. The dashboard still loads, just without the live ticks. |

If you want TradePulse US live on the public site, that needs a separate change (run a public WebSocket bridge to your TWS, or move the IBKR connection to a server) — happy to do it as a follow-up.

---

## Updating live data via the website (admin upload)

Visit `/admin` (e.g. `https://trading-suite-l9e4.onrender.com/admin`), enter your admin password, drop a new `.xlsx`, click **Upload & Publish**.

What happens:
1. The site replaces `data/all.xlsx` in the GitHub repo via the GitHub API.
2. GitHub commit triggers Render auto-deploy.
3. New rows are live for all visitors in ~3 minutes.

**Required env vars on Render** (Settings → Environment):
- `ADMIN_PASSWORD` — anything you want; protects `/admin/upload`
- `GH_PAT` — GitHub Personal Access Token with `repo` scope (https://github.com/settings/tokens)
- `GH_REPO` — `aboaziz112211/trading_suite` (already set in render.yaml)
- `GH_BRANCH` — `main` (already set in render.yaml)

## Routes

- `/` — home
- `/p/<key>` — product page (`chartedge` / `tradepulse_us` / `tradepulse_sar`)
- `/p/<key>/file` — raw HTML
- `/p/<key>/download` — downloadable copy
- `/api/data` — JSON feed from `data/all.xlsx`
- `/admin` — upload form to refresh `data/all.xlsx` (password-gated)
- `/admin/upload` — POST endpoint, commits new xlsx to GitHub
- `/healthz` — health check
