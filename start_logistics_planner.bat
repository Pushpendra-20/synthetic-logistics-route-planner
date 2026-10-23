@echo off
setlocal
cd /d "%~dp0"

set "ENV_DIR=%~dp0.venv-logistics-planner"
set "ENV_PY=%ENV_DIR%\Scripts\python.exe"

if not exist "%ENV_PY%" (
    echo Creating a separate Python environment for the route planner...
    python -m venv "%ENV_DIR%"
    if errorlevel 1 goto :failed
)

"%ENV_PY%" -c "import streamlit, networkx, numpy, folium, streamlit_folium, pyrosm" >nul 2>nul
if errorlevel 1 (
    echo Installing route-planner packages into its separate environment...
    "%ENV_PY%" -m pip install -r requirements-logistics-planner.txt
    if errorlevel 1 goto :failed
)

set "ETC2_OSM_PBF=C:\Users\sharm\OneDrive\Desktop\ANUJ\JAPAN\VS-CODE\research_code\kyushu-261020.osm.pbf"
"%ENV_PY%" -m streamlit run logistics_route_planner.py
if errorlevel 1 goto :failed
goto :eof

:failed
echo.
echo The route planner could not start. Check the message above and LOGISTICS_PLANNER_README.md.
pause
