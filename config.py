import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRODUCTS_DIR = HERE / "products"
DATA_DIR = HERE / "data"

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
        "file": "TradePulse_SARv1_LIVE.html",
    },
}

LIVE_XLSX_PATH = DATA_DIR / "all.xlsx"
LIVE_XLSX_SHEET = "Sheet 1"
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))
