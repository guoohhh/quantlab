#!/usr/bin/env bash
#
# start-hackathon-demo.sh
#
# Starts the local QuantLab product UI in an isolated hackathon configuration
# on macOS / Linux. Functional peer of scripts/start-hackathon-demo.ps1.
#
# Runs Streamlit on 127.0.0.1 using an isolated database, together with a
# local background job worker so that the AI assistant and expert roundtable
# (both of which run as background jobs) actually execute instead of staying
# stuck at "queued". It skips the project .env file, disables external LLM
# providers and automatic trusted-data refresh, and never starts the
# scheduler, API, formal experiments, or any broker integration.
#
# Usage:
#   ./scripts/start-hackathon-demo.sh                 # start the UI + worker
#   ./scripts/start-hackathon-demo.sh --check-only    # preflight only
#   ./scripts/start-hackathon-demo.sh --no-browser    # start without a browser
#   ./scripts/start-hackathon-demo.sh --no-worker     # start UI only (jobs will not run)
#   ./scripts/start-hackathon-demo.sh --port 8600     # preferred local port

set -euo pipefail

PORT=8501
PORT_SEARCH_RANGE=20
NO_BROWSER=0
CHECK_ONLY=0
NO_WORKER=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="${2:?--port requires a value}"
            shift 2
            ;;
        --port-search-range)
            PORT_SEARCH_RANGE="${2:?--port-search-range requires a value}"
            shift 2
            ;;
        --no-browser)
            NO_BROWSER=1
            shift
            ;;
        --no-worker)
            NO_WORKER=1
            shift
            ;;
        --check-only)
            CHECK_ONLY=1
            shift
            ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Prefer the project venv python, fall back to python3.
PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3 || true)"
fi

DASHBOARD="$PROJECT_ROOT/dashboard/app.py"
DEFAULT_CONFIG="$PROJECT_ROOT/config/default.toml"
FROZEN_DATASET="$PROJECT_ROOT/data/demo/historical-research-v1.json"
DEMO_ROOT="$PROJECT_ROOT/data/demo"
ISOLATED_DATABASE="$DEMO_ROOT/hackathon-ui.db"
ISOLATED_DATA="$DEMO_ROOT/hackathon-ui-data"

assert_required_file() {
    local path="$1"
    local description="$2"
    if [[ ! -f "$path" ]]; then
        echo "ERROR: $description was not found: $path" >&2
        exit 1
    fi
}

if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
    echo "ERROR: QuantLab virtual-environment Python not found. Create it with:" >&2
    echo "  python3.11 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev,agents,ui,data,api]'" >&2
    exit 1
fi

assert_required_file "$DASHBOARD" "Streamlit dashboard entry"
assert_required_file "$DEFAULT_CONFIG" "QuantLab default configuration"
assert_required_file "$FROZEN_DATASET" "Frozen historical demo dataset"

# Isolated, secret-free environment. Process env wins over local dotenv; a
# unique missing env-file path prevents Settings.load() from importing real
# provider secrets.
export QUANTLAB_ENV_FILE="${TMPDIR:-/tmp}/quantlab-hackathon-no-env-$$-$RANDOM.env"
export QUANTLAB_DATABASE_PATH="$ISOLATED_DATABASE"
export QUANTLAB_DATA_DIR="$ISOLATED_DATA"
export QUANTLAB_LLM_PROVIDER="mock"
export QUANTLAB_LLM_MODEL=""
export QUANTLAB_LLM_BASE_URL=""
export QUANTLAB_OPENAI_ENABLED="false"
export QUANTLAB_DEEPSEEK_ENABLED="false"
export QUANTLAB_TRUSTED_DATA_AUTO_REFRESH="false"
export QUANTLAB_ENABLE_TEST_QUOTES="0"
export OPENAI_API_KEY=""
export OPENAI_API_KEYS=""
export DEEPSEEK_API_KEY=""
export DEEPSEEK_API_KEYS=""
export PYTHONUNBUFFERED="1"

if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$PROJECT_ROOT/src:$PROJECT_ROOT:$PYTHONPATH"
else
    export PYTHONPATH="$PROJECT_ROOT/src:$PROJECT_ROOT"
fi

cd "$PROJECT_ROOT"

# --- Dependency check -------------------------------------------------------
STREAMLIT_VERSION="$("$PYTHON" -m streamlit --version 2>&1)" || {
    echo "ERROR: Streamlit dependency check failed: $STREAMLIT_VERSION" >&2
    exit 1
}

# --- Frozen dataset validation ---------------------------------------------
DATASET_PROBE="$("$PYTHON" - <<'PY' 2>&1
from quantlab.config import Settings
from quantlab.workflows.product_demo import historical_demo_dataset

dataset = historical_demo_dataset(Settings.load())
assert dataset["research_only"] is True
assert dataset["training_eligible"] is False
assert dataset["forward_scorecard_eligible"] is False
print(dataset["dataset_fingerprint"])
PY
)" || {
    echo "ERROR: Frozen demo dataset validation failed: $DATASET_PROBE" >&2
    exit 1
}

# --- Port selection ---------------------------------------------------------
resolve_local_port() {
    local preferred="$1"
    local search_range="$2"
    local last=$(( preferred + search_range - 1 ))
    (( last > 65535 )) && last=65535
    local candidate
    for (( candidate = preferred; candidate <= last; candidate++ )); do
        if "$PYTHON" - "$candidate" <<'PY' 2>/dev/null
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
        then
            echo "$candidate"
            return 0
        fi
    done
    echo "ERROR: No free local port was found from $preferred through $last." >&2
    return 1
}

SELECTED_PORT="$(resolve_local_port "$PORT" "$PORT_SEARCH_RANGE")"
URL="http://127.0.0.1:$SELECTED_PORT"
if [[ "$SELECTED_PORT" != "$PORT" ]]; then
    echo "Preferred port $PORT is occupied; selected $SELECTED_PORT instead."
fi

echo "QuantLab hackathon preflight passed."
echo "Streamlit: $STREAMLIT_VERSION"
echo "Frozen dataset SHA-256: $DATASET_PROBE"
echo "Local URL: $URL"
echo "Isolated UI database: $ISOLATED_DATABASE"
if [[ "$NO_WORKER" -eq 1 ]]; then
    echo "Safety boundary: local Streamlit only (worker disabled; AI assistant and roundtable will stay queued)."
else
    echo "Safety boundary: local Streamlit + background job worker; no .env secrets, scheduler, API, formal experiment, or broker execution."
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
    echo "CheckOnly completed; no server was started and no database was created."
    exit 0
fi

HEADLESS="false"
[[ "$NO_BROWSER" -eq 1 ]] && HEADLESS="true"

# --- Background job worker --------------------------------------------------
# The AI assistant (chat_request) and expert roundtable (roundtable_request)
# are submitted as background jobs. Without a worker consuming the queue they
# stay at "queued" forever. Start a lightweight local worker loop alongside
# Streamlit and stop it automatically when this script exits.
WORKER_PID=""
cleanup() {
    if [[ -n "$WORKER_PID" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
        kill "$WORKER_PID" 2>/dev/null || true
        wait "$WORKER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if [[ "$NO_WORKER" -eq 0 ]]; then
    echo "Starting the background job worker."
    "$PYTHON" - <<'PY' &
import time

from quantlab.config import Settings
from quantlab.runtime.worker import JobWorker

worker = JobWorker(Settings.load(), worker_id="hackathon-demo-worker")
while True:
    try:
        worker.run_until_empty(20)
    except Exception as exc:  # keep the loop alive across transient errors
        print(f"[worker] error: {exc}", flush=True)
    time.sleep(1.0)
PY
    WORKER_PID=$!
fi

echo "Starting the product UI. Press Ctrl+C in this window to stop it."
"$PYTHON" -m streamlit run "$DASHBOARD" \
    --server.address 127.0.0.1 \
    --server.port "$SELECTED_PORT" \
    --server.headless "$HEADLESS" \
    --server.fileWatcherType none \
    --server.runOnSave false \
    --browser.gatherUsageStats false \
    --client.toolbarMode viewer \
    --client.showErrorDetails none
