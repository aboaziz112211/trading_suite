@echo off
REM Refresh assets from source folders into the website's bundled copies.
REM Run this whenever you update ChartEdge / TradePulse / the xlsx,
REM then `git commit -am "update assets" && git push` to redeploy on Render.

setlocal
set ROOT=%~dp0..
echo Syncing products and data from %ROOT%

copy /Y "%ROOT%\01_ChartEdge\ChartEdge_v9.2.html"            "%~dp0products\ChartEdge_v9.2.html"
copy /Y "%ROOT%\07_TradePulse\TradePulse_USv1_LIVE.html"     "%~dp0products\TradePulse_USv1_LIVE.html"
copy /Y "%ROOT%\07_TradePulse\TradePulse_SARv1_LIVE.html"    "%~dp0products\TradePulse_SARv1_LIVE.html"
copy /Y "%ROOT%\07_TradePulse\all .xlsx"                     "%~dp0data\all.xlsx"

echo Done.
endlocal
