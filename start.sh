#!/usr/bin/env bash
# Fukidashi server launcher for macOS / Linux.
#
#   ./start.sh              install on first run, then start the server
#   ./start.sh --core       lightweight install: no OCR/LaMa/offline engine (server starts, can't detect text)
#   ./start.sh --reinstall  re-run the dependency install (e.g. after updating)
#   ./start.sh --cpu        never install the CUDA build of PyTorch
#
# Environment overrides: PYTHON=/path/to/python, TORCH_INDEX_URL=<pip index for torch>.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$ROOT/server"
VENV="$SERVER/.venv"
MARKER="$VENV/.fukidashi-installed"

PROFILE="full"
REINSTALL=0
FORCE_CPU=0
for arg in "$@"; do
  case "$arg" in
    --core) PROFILE="core" ;;
    --reinstall) REINSTALL=1 ;;
    --cpu) FORCE_CPU=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)"; exit 2 ;;
  esac
done

say() { printf '\033[1;38;5;209m[fukidashi]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[fukidashi]\033[0m %s\n' "$*" >&2; }

# --- Python ------------------------------------------------------------------
find_python() {
  for c in "${PYTHON:-}" python3.12 python3.11 python3.13 python3.10 python3 python; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

if [ ! -x "$VENV/bin/python" ]; then
  PY="$(find_python)" || { warn "Python 3.10+ is required. Install it from https://www.python.org/ and try again."; exit 1; }
  say "Creating virtual environment with $("$PY" --version 2>&1)"
  "$PY" -m venv "$VENV" || { warn "Could not create a venv (on Debian/Ubuntu: sudo apt install python3-venv)"; exit 1; }
  REINSTALL=1
fi
VPY="$VENV/bin/python"

# --- Dependencies ------------------------------------------------------------
REQ_HASH="$(cat "$SERVER/requirements.txt" "$SERVER/requirements-full.txt" | "$VPY" -c 'import hashlib,sys; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest()[:12])')"
WANT="$PROFILE $REQ_HASH"
if [ "$REINSTALL" = 1 ] || [ ! -f "$MARKER" ] || [ "$(cat "$MARKER")" != "$WANT" ]; then
  say "Installing core dependencies"
  "$VPY" -m pip install --upgrade pip >/dev/null
  "$VPY" -m pip install -r "$SERVER/requirements.txt"

  if [ "$PROFILE" = "full" ]; then
    TORCH_ARGS=()
    if [ -n "${TORCH_INDEX_URL:-}" ]; then
      TORCH_ARGS=(--index-url "$TORCH_INDEX_URL")
    elif [ "$(uname -s)" = "Linux" ]; then
      if [ "$FORCE_CPU" = 0 ] && command -v nvidia-smi >/dev/null 2>&1; then
        say "NVIDIA GPU found: installing PyTorch with CUDA (large download)"
      else
        # The default Linux wheel bundles CUDA (~2 GB); the CPU build is much smaller.
        TORCH_ARGS=(--index-url https://download.pytorch.org/whl/cpu)
        say "No NVIDIA GPU: installing the CPU build of PyTorch"
      fi
    fi
    say "Installing PyTorch"
    if ! "$VPY" -m pip install torch torchvision ${TORCH_ARGS[@]+"${TORCH_ARGS[@]}"}; then
      if [ -n "${TORCH_ARGS[*]+x}" ]; then
        warn "Couldn't install from the PyTorch index; trying the default build from PyPI"
        "$VPY" -m pip install torch torchvision || warn "PyTorch failed to install: LaMa inpainting and EasyOCR will be unavailable."
      else
        warn "PyTorch failed to install: LaMa inpainting and EasyOCR will be unavailable."
      fi
    fi

    FAILED=()
    for pkg in easyocr manga-ocr argostranslate; do
      say "Installing $pkg"
      "$VPY" -m pip install "$pkg" || { warn "$pkg failed to install; continuing without it."; FAILED+=("$pkg"); }
    done
    [ -z "${FAILED[*]+x}" ] || [ ${#FAILED[@]} -eq 0 ] || warn "Not installed: ${FAILED[*]}. The server will start and explain what is missing."
  fi
  echo "$WANT" > "$MARKER"
fi

# --- Config ------------------------------------------------------------------
if [ ! -f "$SERVER/.env" ]; then
  cp "$SERVER/.env.example" "$SERVER/.env"
  say "Created server/.env from .env.example. Add your API keys there (optional), then restart."
fi

# --- Run ---------------------------------------------------------------------
HOST="$(grep -E '^HOST=' "$SERVER/.env" | tail -n1 | cut -d= -f2- | tr -d '"'"'"' \r' || true)"
PORT="$(grep -E '^PORT=' "$SERVER/.env" | tail -n1 | cut -d= -f2- | tr -d '"'"'"' \r' || true)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5003}"
say "Starting Fukidashi on http://$HOST:$PORT  (Ctrl+C to stop)"
cd "$SERVER"
exec "$VPY" -m uvicorn fukidashi.app:app --host "$HOST" --port "$PORT"
