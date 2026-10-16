@echo off
rem Start ETF Return Leaders on Windows. First run: installs everything and asks three setup questions.
setlocal
cd /d "%~dp0"
rem Windows defaults to cp1252 for files without an explicit encoding; the data files are UTF-8.
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
  echo Setting up Python environment, first run only...
  rem --clear wipes any macOS/Linux .venv that came along in a zip; it can't run here.
  py -3 -m venv --clear .venv 2>nul || python -m venv --clear .venv || goto :fail
)
.venv\Scripts\python -c "import sys; sys.exit(sys.version_info < (3, 11))" || (
  echo This app needs Python 3.11 or newer. Install it, delete the .venv folder, and run again.
  goto :fail
)
.venv\Scripts\python -m pip install -q -r requirements.txt || goto :fail
.venv\Scripts\python -m playwright install chromium >nul || goto :fail
if not exist config.toml (
  .venv\Scripts\python -m app.setup config.toml || goto :fail
)
.venv\Scripts\python -m app.main
goto :eof

:fail
echo Setup failed - see the message above.
exit /b 1
