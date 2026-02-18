@echo off
setlocal

:: ─────────────────────────────────────────────
:: ALU Telemetry – Launcher
:: Sets up the virtual environment on first run,
:: installs all Python dependencies, then starts
:: the application.
:: ─────────────────────────────────────────────

set VENV_DIR=%~dp0venv
set REQUIREMENTS=pymem pywin32 keyboard Pillow

:: ── Step 1: Create venv if it does not exist ──
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [ALU Telemetry] Virtual environment not found. Creating...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        echo Make sure Python 3.9+ is installed and added to PATH.
        pause
        exit /b 1
    )
    echo [ALU Telemetry] Virtual environment created.
)

:: ── Step 2: Activate venv ──────────────────────
call "%VENV_DIR%\Scripts\activate.bat"

:: ── Step 3: Install / upgrade dependencies ────
echo [ALU Telemetry] Checking dependencies...
pip install --quiet --upgrade %REQUIREMENTS%
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

:: ── Step 4: Launch the application ────────────
echo [ALU Telemetry] Starting...
python "%~dp0main.py"

endlocal
