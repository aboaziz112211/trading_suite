import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRODUCTS_DIR = HERE / "products"
DATA_DIR = HERE / "data"

PRODUCTS = {
    "chartedge": {
        "title": "ChartEdge v9.2",
        "tagline": "SEPA / VCP scanner & analyzer",
        "dir": PRODUCTS_DIR,
        "file": "ChartEdge_v9.2.html",
    },
    "tradepulse_us": {
        "title": "TradePulse US v1",
        "tagline": "US market live dashboard",
        "dir": PRODUCTS_DIR,
        "file": "TradePulse_USv1_LIVE.html",
    },
    "tradepulse_sar": {
        "title": "TradePulse SAR v1",
        "tagline": "Saudi market live dashboard",
        "dir": PRODUCTS_DIR,
        "file": "TradePulse_SARv1_LIVE.html",
    },
}

LIVE_XLSX_PATH = DATA_DIR / "all.xlsx"
LIVE_XLSX_SHEET = "Sheet 1"
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))
