#!/usr/bin/env bash
# .github/scripts/setup_inference.sh
# Master entrypoint: detect runner, install llama.cpp, download models, start server.
# Must be run from the repository root after checkout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/lib/detect.sh"
source "$SCRIPT_DIR/lib/download.sh"
source "$SCRIPT_DIR/lib/server.sh"

# ── 1. Detect runner and select model ───────────────────────────────
detect_runner_config

# ── 2. Ensure llama.cpp is installed (via pip) ──────────────────────
download_llama_cpp_if_needed

# ── 3. Ensure model files are available ─────────────────────────────
download_models_if_needed

# ── 4. Start the appropriate server configuration ───────────────────
echo "::group::Start llama-server"

if [[ "$RUNNER_VCPUS" -ge 4 ]] && [[ "$MODEL_SIZE" == "7b" ]]; then
    start_server_4vcpu_7b
elif [[ "$RUNNER_VCPUS" -lt 4 ]] && [[ "$MODEL_SIZE" == "3b" ]]; then
    start_server_2vcpu_3b
elif [[ "$RUNNER_VCPUS" -lt 4 ]] && [[ "$MODEL_SIZE" == "7b" ]]; then
    # Opt-in 7B on 2-vCPU with reduced context (MODEL_SIZE_OVERRIDE=7b)
    start_server_2vcpu_7b
else
    # Fallback: 2-vCPU with 3B regardless
    start_server_2vcpu_3b
fi

echo "::endgroup::"

# ── 5. Export server URL and model info to environment ──────────────
{
    echo "LLAMA_SERVER_URL=http://127.0.0.1:8080"
    echo "GHOST_REVIEW_MODEL=qwen2.5-coder-${MODEL_SIZE}-instruct-q4_k_m"
} >> "$GITHUB_ENV"

# ── 6. Verify inference works ────────────────────────────────────────
echo "Verifying inference endpoint..."
HEALTH=$(curl -s http://127.0.0.1:8080/health || echo '{"status":"error"}')
echo "Health: $HEALTH"

echo "Inference ready — qwen2.5-coder-${MODEL_SIZE} q4_k_m" >> "$GITHUB_STEP_SUMMARY"
echo "Runner: ${RUNNER_VCPUS} vCPU / ${AVAILABLE_RAM_GB} GB RAM / ${MODEL_ARCH}" >> "$GITHUB_STEP_SUMMARY"
