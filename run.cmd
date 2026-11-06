@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Setting up Verwrity encrypted backup ^(Python 3.11 or newer required^)...
    py -3 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import cryptography" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" backup.py %*
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Couldn't finish. Check the message above. Python 3.11 or newer and internet access are needed for first-time setup.
pause
exit /b 1
