@echo off
rem Fukidashi server launcher for Windows.
rem
rem   start.bat              install on first run, then start the server
rem   start.bat --core       lightweight install: no OCR/LaMa/offline engine (server starts, can't detect text)
rem   start.bat --reinstall  re-run the dependency install (e.g. after updating)
rem   start.bat --cpu        never install the CUDA build of PyTorch
rem
rem Environment override: TORCH_INDEX_URL=<pip index for torch>, e.g.
rem   https://download.pytorch.org/whl/cu126 for older NVIDIA drivers.
setlocal EnableExtensions

set "ROOT=%~dp0"
set "SERVER=%ROOT%server"
set "VENV=%SERVER%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "PROFILE=full"
set "REINSTALL=0"
set "FORCE_CPU=0"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--core" (set "PROFILE=core" & shift & goto parse)
if /i "%~1"=="--reinstall" (set "REINSTALL=1" & shift & goto parse)
if /i "%~1"=="--cpu" (set "FORCE_CPU=1" & shift & goto parse)
echo Unknown option: %~1
exit /b 2
:parsed

rem --- Python ------------------------------------------------------------------
if exist "%VPY%" goto have_venv
set "PY="
for %%V in (3.12 3.11 3.13 3.10) do call :try_py "py -%%V"
if not defined PY call :try_py "python"
if not defined PY (
  echo [fukidashi] Python 3.10 or newer is required. Install it from https://www.python.org/
  echo            and tick "Add python.exe to PATH" during setup, then run start.bat again.
  pause
  exit /b 1
)
echo [fukidashi] Creating virtual environment with %PY%
%PY% -m venv "%VENV%"
if errorlevel 1 (
  echo [fukidashi] Could not create the virtual environment.
  pause
  exit /b 1
)
set "REINSTALL=1"
:have_venv

rem --- Dependencies ------------------------------------------------------------
if "%REINSTALL%"=="1" goto install
"%VPY%" -c "import hashlib,pathlib,sys; d=pathlib.Path(sys.argv[1]); h=hashlib.sha1((d/'requirements.txt').read_bytes()+(d/'requirements-full.txt').read_bytes()).hexdigest()[:12]; m=d/'.venv'/'.fukidashi-installed'; sys.exit(0 if m.exists() and m.read_text().strip()==sys.argv[2]+' '+h else 1)" "%SERVER%" %PROFILE%
if not errorlevel 1 goto deps_done

:install
echo [fukidashi] Installing core dependencies
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r "%SERVER%\requirements.txt"
if errorlevel 1 (
  echo [fukidashi] Installing the core dependencies failed. Check your internet connection and try again.
  pause
  exit /b 1
)
if "%PROFILE%"=="core" goto write_marker

set "TORCH_ARGS="
if defined TORCH_INDEX_URL (
  set "TORCH_ARGS=--index-url %TORCH_INDEX_URL%"
  goto torch
)
if "%FORCE_CPU%"=="1" goto torch
where nvidia-smi >nul 2>&1
if errorlevel 1 goto torch
echo [fukidashi] NVIDIA GPU found: installing PyTorch with CUDA (large download)
set "TORCH_ARGS=--index-url https://download.pytorch.org/whl/cu128"
:torch
echo [fukidashi] Installing PyTorch
"%VPY%" -m pip install torch torchvision %TORCH_ARGS%
if errorlevel 1 echo [fukidashi] PyTorch failed to install: LaMa inpainting and EasyOCR will be unavailable.

for %%P in (easyocr manga-ocr argostranslate) do (
  echo [fukidashi] Installing %%P
  "%VPY%" -m pip install %%P
  if errorlevel 1 echo [fukidashi] %%P failed to install; continuing without it.
)

:write_marker
"%VPY%" -c "import hashlib,pathlib,sys; d=pathlib.Path(sys.argv[1]); h=hashlib.sha1((d/'requirements.txt').read_bytes()+(d/'requirements-full.txt').read_bytes()).hexdigest()[:12]; (d/'.venv'/'.fukidashi-installed').write_text(sys.argv[2]+' '+h+'\n')" "%SERVER%" %PROFILE%
:deps_done

rem --- Config ------------------------------------------------------------------
if not exist "%SERVER%\.env" (
  copy "%SERVER%\.env.example" "%SERVER%\.env" >nul
  echo [fukidashi] Created server\.env from .env.example. Add your API keys there ^(optional^), then restart.
)

set "HOST=127.0.0.1"
set "PORT=5003"
for /f "usebackq tokens=1,* delims==" %%A in ("%SERVER%\.env") do (
  if /i "%%A"=="HOST" if not "%%B"=="" set "HOST=%%B"
  if /i "%%A"=="PORT" if not "%%B"=="" set "PORT=%%B"
)

rem --- Run ---------------------------------------------------------------------
echo [fukidashi] Starting Fukidashi on http://%HOST%:%PORT%  (close this window or press Ctrl+C to stop)
cd /d "%SERVER%"
"%VPY%" -m uvicorn fukidashi.app:app --host %HOST% --port %PORT%
echo [fukidashi] The server stopped.
pause
exit /b 0

:try_py
rem Sets PY to %~1 if it runs Python 3.10+.
if defined PY exit /b 0
%~1 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
