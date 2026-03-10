#!/usr/bin/env bash
# .github/scripts/lib/server.sh
# llama-server startup functions for each runner/model configuration.

set -euo pipefail

# Native llama-server built from source
LLAMA_SERVER_BIN="${HOME}/.cache/ghost-review/llama-bin/llama-server"
MODEL_DIR="$HOME/.cache/ghost-review/models"
LLAMA_PID_FILE="/tmp/ghost-review-llama.pid"
LLAMA_LOG_FILE="/tmp/ghost-review-llama.log"

# ──────────────────────────────────────────────────────────────────────
# wait_for_server — poll /health until 200 OK or timeout
# ──────────────────────────────────────────────────────────────────────
wait_for_server() {
    local MAX_WAIT="${1:-90}"
    local ELAPSED=0

    echo -n "Waiting for llama-server"
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
                tail -100 "$LLAMA_LOG_FILE" || true
                
                # Check for shared library error
                if tail -20 "$LLAMA_LOG_FILE" 2>/dev/null | grep -q "error while loading shared libraries"; then
                    echo ""
                    echo "HINT: This is a shared library error. The binary may need to be rebuilt with static linking."
                    echo "Try clearing the cache and re-running the workflow."
                fi
                
                exit 1
            fi
        fi
        sleep 2
        ELAPSED=$((ELAPSED + 2))
        echo -n "."
    done

    echo ""
    echo "ERROR: llama-server did not become healthy within ${MAX_WAIT}s. Logs:"
    tail -100 "$LLAMA_LOG_FILE" || true
    exit 1
}

# ──────────────────────────────────────────────────────────────────────
# verify_binary — check binary exists and is executable
# ──────────────────────────────────────────────────────────────────────
verify_binary() {
    if [[ ! -f "$LLAMA_SERVER_BIN" ]]; then
        echo "ERROR: llama-server binary not found at $LLAMA_SERVER_BIN"
        echo "The binary may not have been built or cached properly."
        exit 1
    fi
    
    if [[ ! -x "$LLAMA_SERVER_BIN" ]]; then
        echo "Making binary executable..."
        chmod +x "$LLAMA_SERVER_BIN"
    fi
    
    # Test binary can execute (check for shared lib errors)
    if ! "$LLAMA_SERVER_BIN" --version &>/dev/null; then
        echo "ERROR: llama-server binary cannot execute. Testing for errors..."
        "$LLAMA_SERVER_BIN" --version 2>&1 || true
        
        # Check for shared library issues
        if ldd "$LLAMA_SERVER_BIN" 2>&1 | grep -q "not found"; then
            echo ""
            echo "Shared library dependencies missing:"
            ldd "$LLAMA_SERVER_BIN" 2>&1 | grep "not found" || true
            echo ""
            echo "SOLUTION: Rebuild with static linking by clearing the llama-bin cache."
        fi
        exit 1
    fi
    
    echo "Binary verified: $(${LLAMA_SERVER_BIN} --version 2>&1 | head -1)"
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

    # Verify binary first
    verify_binary

    echo "Starting llama-server:"
    echo "  Binary     : ${LLAMA_SERVER_BIN}"
    echo "  Model      : ${MODEL##*/}"
    echo "  Context    : ${CTX} tokens"
    echo "  Threads    : ${THREADS}"
    echo "  Batch size : ${BATCH} / ubatch ${UBATCH}"
    echo "  Max output : ${N_PREDICT} tokens"
    echo "  Parallel   : ${PARALLEL} slots"
    echo "  KV keep    : ${KEEP} tokens"

    # New llama.cpp (b8252+) uses different flag syntax:
    # --flash-attn on (instead of just --flash-attn)
    # -fa is short form
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
        --flash-attn         on \
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

# 4-vCPU ARM64 / 16 GB RAM / 7B model
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

# 2-vCPU ARM64 / 8 GB RAM / 3B model
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

# 2-vCPU ARM64 / 8 GB RAM / 7B model (opt-in)
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

# Stop server
stop_server() {
    if [[ -f "$LLAMA_PID_FILE" ]]; then
        local PID
        PID=$(cat "$LLAMA_PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping llama-server (PID $PID)..."
            kill -TERM "$PID" 2>/dev/null || true
            wait "$PID" 2>/dev/null || true
            echo "llama-server stopped."
        else
            echo "llama-server already stopped."
        fi
        rm -f "$LLAMA_PID_FILE"
    else
        echo "No PID file found."
    fi
}
