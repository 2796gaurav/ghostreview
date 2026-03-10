#!/usr/bin/env bash
# .github/scripts/lib/server.sh
# llama-server startup functions for each runner/model configuration.
# Every flag is documented. None are blindly applied defaults.

set -euo pipefail

LLAMA_SERVER_BIN="$HOME/.cache/ghost-review/llama-bin/llama-server"
MODEL_DIR="$HOME/.cache/ghost-review/models"
LLAMA_PID_FILE="/tmp/ghost-review-llama.pid"
LLAMA_LOG_FILE="/tmp/ghost-review-llama.log"

# ──────────────────────────────────────────────────────────────────────
# wait_for_server — poll /health until 200 OK or timeout
# ──────────────────────────────────────────────────────────────────────
wait_for_server() {
    local MAX_WAIT="${1:-90}"
    local ELAPSED=0

    echo -n "Waiting for llama-server..."
    while [[ $ELAPSED -lt $MAX_WAIT ]]; do
        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
            http://127.0.0.1:8080/health 2>/dev/null || echo "000")
        if [[ "$HTTP_STATUS" == "200" ]]; then
            echo " ready after ${ELAPSED}s"
            return 0
        fi
        # Check if the process died
        if [[ -f "$LLAMA_PID_FILE" ]]; then
            local PID
            PID=$(cat "$LLAMA_PID_FILE")
            if ! kill -0 "$PID" 2>/dev/null; then
                echo ""
                echo "ERROR: llama-server (PID $PID) died. Logs:"
                tail -50 "$LLAMA_LOG_FILE" || true
                exit 1
            fi
        fi
        sleep 2
        ELAPSED=$((ELAPSED + 2))
        echo -n "."
    done

    echo ""
    echo "ERROR: llama-server did not become healthy within ${MAX_WAIT}s. Logs:"
    tail -50 "$LLAMA_LOG_FILE" || true
    exit 1
}

# ──────────────────────────────────────────────────────────────────────
# start_server — internal helper
# Args: model_path ctx_size threads batch_size ubatch n_predict parallel keep
# ──────────────────────────────────────────────────────────────────────
_start_server() {
    local MODEL="$1"
    local CTX="$2"
    local THREADS="$3"
    local BATCH="$4"
    local UBATCH="$5"
    local N_PREDICT="$6"
    local PARALLEL="$7"
    local KEEP="$8"

    echo "Starting llama-server:"
    echo "  Model      : ${MODEL##*/}"
    echo "  Context    : ${CTX} tokens"
    echo "  Threads    : ${THREADS}"
    echo "  Batch size : ${BATCH} / ubatch ${UBATCH}"
    echo "  Max output : ${N_PREDICT} tokens"
    echo "  Parallel   : ${PARALLEL} slots"
    echo "  KV keep    : ${KEEP} tokens"

    "$LLAMA_SERVER_BIN" \
        --model              "$MODEL" \
        --host               127.0.0.1 \
        --port               8080 \
        --ctx-size           "$CTX" \
        --threads            "$THREADS" \
        --threads-batch      "$THREADS" \
        --batch-size         "$BATCH" \
        --ubatch-size        "$UBATCH" \
        --n-predict          "$N_PREDICT" \
        --parallel           "$PARALLEL" \
        --keep               "$KEEP" \
        --cache-reuse        256 \
        --flash-attn \
        --mlock \
        --no-mmap \
        --cache-type-k       q8_0 \
        --cache-type-v       q8_0 \
        --temp               0.3 \
        --top-p              0.8 \
        --top-k              20 \
        --repeat-penalty     1.1 \
        --log-disable \
        --jinja \
        > "$LLAMA_LOG_FILE" 2>&1 &

    echo $! > "$LLAMA_PID_FILE"
    echo "llama-server PID: $(cat "$LLAMA_PID_FILE")"
}

# ──────────────────────────────────────────────────────────────────────
# Public start functions — one per supported configuration
# ──────────────────────────────────────────────────────────────────────

# 4-vCPU ARM64 / 16 GB RAM / 7B model
# Context  : 65536 (uses KV shift within native 32K window)
# Parallel : 2 (passes 2+3 run concurrently via asyncio.gather)
# Keep     : 1024 (pins system prompt ~700 tokens; passes 2-4 free)
start_server_4vcpu_7b() {
    _start_server \
        "$MODEL_DIR/qwen2.5-coder-7b-instruct-q4_k_m.gguf" \
        65536 \
        4 \
        1024 \
        512 \
        4096 \
        2 \
        1024
    wait_for_server 90
}

# 2-vCPU ARM64 / 8 GB RAM / 3B model (private repo default)
# Context  : 32768 (native window; 3B has dense MHA, larger KV than GQA)
# Parallel : 1 (2 cores cannot sustain two inference slots; sequential wins)
# Keep     : 768 (slightly smaller system prompt on 3B)
start_server_2vcpu_3b() {
    _start_server \
        "$MODEL_DIR/qwen2.5-coder-3b-instruct-q4_k_m.gguf" \
        32768 \
        2 \
        512 \
        256 \
        2048 \
        1 \
        768
    wait_for_server 60
}

# 2-vCPU ARM64 / 8 GB RAM / 7B model (opt-in via MODEL_SIZE_OVERRIDE=7b)
# Context  : 16384 (reduced to fit 7B weights + KV in 8 GB; ~2 GB margin)
# Parallel : 1 (same reasoning as 3B on 2-vCPU)
# Keep     : 1024 (system prompt still fits)
start_server_2vcpu_7b() {
    _start_server \
        "$MODEL_DIR/qwen2.5-coder-7b-instruct-q4_k_m.gguf" \
        16384 \
        2 \
        512 \
        256 \
        2048 \
        1 \
        1024
    wait_for_server 90
}

# ──────────────────────────────────────────────────────────────────────
# stop_server — graceful shutdown at end of job
# ──────────────────────────────────────────────────────────────────────
stop_server() {
    if [[ -f "$LLAMA_PID_FILE" ]]; then
        local PID
        PID=$(cat "$LLAMA_PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill -TERM "$PID" 2>/dev/null || true
            wait "$PID" 2>/dev/null || true
        fi
        rm -f "$LLAMA_PID_FILE"
        echo "llama-server stopped."
    fi
}