@echo off
rem TrailNet - installation en un double-clic (Windows)
rem Choisit un Python 3.10 a 3.14, installe les bibliotheques,
rem telecharge le modele Whisper et verifie tout ce PC.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PY="
for %%C in ("py" "py -3.12" "py -3.13" "py -3.14" "py -3.11" "py -3.10" "python") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)" >nul 2>&1 && set "PY=%%~C"
  )
)
if not defined PY goto sans_python
if not exist "trailnet_pc.py" goto sans_script
echo.
echo   TrailNet : installation avec "%PY%"
echo.
%PY% trailnet_pc.py installer --py "%PY%"
echo.
pause
exit /b 0

:sans_python
echo.
echo   Python 3.10 a 3.14 introuvable sur ce PC.
echo   Installe Python 3.12 depuis https://www.python.org/downloads/windows/
echo   en cochant "Add python.exe to PATH", puis relance installer.bat.
echo.
pause
exit /b 1

:sans_script
echo.
echo   trailnet_pc.py introuvable : garde installer.bat dans le dossier TrailNet.
echo.
pause
exit /b 1
