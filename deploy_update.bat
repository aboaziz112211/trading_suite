@echo off
REM Sync the latest TradePulse / ChartEdge dashboards from your working folder
REM to the deployed website folder, then commit + push to GitHub. Render
REM auto-redeploys in ~3 minutes.
REM
REM Run this whenever you've edited any of the dashboard HTML files.

setlocal
set ROOT=%~dp0..

echo Copying dashboards...
copy /Y "%ROOT%\01_ChartEdge\ChartEdge_v9.2.html"            "%~dp0products\ChartEdge_v9.2.html"
copy /Y "%ROOT%\07_TradePulse\TradePulse_USv1_LIVE.html"     "%~dp0products\TradePulse_USv1_LIVE.html"
copy /Y "%ROOT%\07_TradePulse\TradePulse_SARv1_LIVE.html"    "%~dp0products\TradePulse_SARv1_LIVE.html"

echo.
echo Pushing to GitHub...
cd /d "%~dp0"
git add -A
git commit -m "Update dashboards"
git push

echo.
echo Done. Render will redeploy in ~3 minutes.
echo Check status: https://dashboard.render.com
pause
endlocal
