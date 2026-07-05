import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRODUCTS_DIR = HERE / "products"
DATA_DIR = HERE / "data"
REPORTS_DIR = HERE / "reports"

PRODUCTS = {
    "chartedge": {
        "title": "ChartEdge",
        "title_ar": "ChartEdge",  # brand name kept Latin in AR too
        "tagline": "SEPA / VCP scanner & analyzer",
        "tagline_ar": "ماسح SEPA · VCP وأداة التحليل",
        "dir": PRODUCTS_DIR,
        "file": "ChartEdge_v9.2.html",
    },
    "tradepulse_us": {
        "title": "TradePulse US",
        "title_ar": "السوق الأمريكي",
        "tagline": "US market live dashboard",
        "tagline_ar": "لوحة السوق الأمريكي المباشرة",
        "dir": PRODUCTS_DIR,
        "file": "TradePulse_USv2_LIVE.html",
    },
    "tradepulse_sar": {
        "title": "TradePulse TASI",
        "title_ar": "السوق السعودي",
        "tagline": "Saudi market live dashboard",
        "tagline_ar": "لوحة السوق السعودي المباشرة",
        "dir": PRODUCTS_DIR,
        "file": "TradePulse_SARv2_LIVE.html",
    },
}

# ── Research reports (shown under the "Reports" tab) ──────────────────────────
# Newest first. To add a report: drop its self-contained .html into reports/,
# add an entry here, then commit + deploy.
REPORTS = [
    {
        "slug": "tasi-q2-2026-earnings-preview",
        "title": "TASI Q2 2026 — High-Conviction Earnings Preview (20 Companies)",
        "title_ar": "معاينة أرباح الربع الثاني 2026 — السوق السعودي (20 شركة)",
        "date": "2026-07-05",
        "tagline": "High-conviction earnings preview across 20 TASI companies for Q2 2026.",
        "tagline_ar": "معاينة أرباح عالية القناعة لـ20 شركة سعودية عن الربع الثاني 2026.",
        "file": "tasi_q2_2026_earnings_preview.html",
        # Optional Arabic PDF, served straight from static/ (path relative to static/).
        "pdf_ar_static": "reports/tasi_q2_2026_earnings_preview_ar.pdf",
        # Which format the viewer embeds by default: "pdf_ar" or "html".
        "default_display": "pdf_ar",
    },
]

LIVE_XLSX_PATH = DATA_DIR / "all.xlsx"
LIVE_XLSX_SHEET = "Sheet 1"
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))
