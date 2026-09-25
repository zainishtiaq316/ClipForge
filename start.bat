@echo off
REM ClipForge one-click launcher for Windows.
REM First run: creates a Python environment, installs dependencies and builds the UI.
REM Later runs: starts immediately and opens your browser.
setlocal
cd /d "%~dp0"
title ClipForge

where python >nul 2>nul || (
  echo Python 3.10 or newer is required. Download it from https://www.python.org/downloads/
  echo During installation, tick "Add python.exe to PATH".
  pause & exit /b 1
)
python -c "import sys; sys.exit(sys.version_info < (3, 10))" || (
  echo Python 3.10 or newer is required. Please update Python.
  pause & exit /b 1
)

if not exist "backend\.venv\.installed" (
  echo [1/3] Installing the video engine. This happens only once and can take a few minutes...
  python -m venv backend\.venv || goto :fail
  backend\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q --upgrade pip || goto :fail
  backend\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r backend\requirements.txt || goto :fail
  echo ok> "backend\.venv\.installed"
)

if not exist "frontend\dist\index.html" (
  where npm >nul 2>nul || (
    echo Node.js 18 or newer is required to build the interface the first time.
    echo Download it from https://nodejs.org/
    pause & exit /b 1
  )
  echo [2/3] Building the interface. This happens only once...
  pushd frontend
  call npm ci --no-audit --no-fund --loglevel=error || (popd & goto :fail)
  call npm run build || (popd & goto :fail)
  popd
)

echo [3/3] Starting ClipForge...
set PYTHONIOENCODING=utf-8
cd backend
.venv\Scripts\python.exe -m app
goto :eof

:fail
echo.
echo Setup failed. Scroll up to see the error, fix it, then run start.bat again.
pause
exit /b 1
