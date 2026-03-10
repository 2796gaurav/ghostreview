#!/usr/bin/env bash
# .github/scripts/lib/detect.sh
# Detect runner configuration and select appropriate model size.
# Exports: MODEL_SIZE, RUNNER_VCPUS, MODEL_ARCH, AVAILABLE_RAM_GB

set -euo pipefail

detect_runner_config() {
    local OVERRIDE="${MODEL_SIZE_OVERRIDE:-}"

    RUNNER_VCPUS=$(nproc)
    AVAILABLE_RAM_GB=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
    MODEL_ARCH=$(uname -m)

    echo "::group::Runner detection"
    echo "Architecture : ${MODEL_ARCH}"
    echo "vCPUs        : ${RUNNER_VCPUS}"
    echo "Total RAM    : ${AVAILABLE_RAM_GB} GB"
    echo "::endgroup::"

    if [[ -n "$OVERRIDE" ]]; then
        MODEL_SIZE="$OVERRIDE"
        echo "Model override: ${MODEL_SIZE} (from MODEL_SIZE_OVERRIDE)"
    elif [[ "$RUNNER_VCPUS" -ge 4 ]] && [[ "$AVAILABLE_RAM_GB" -ge 14 ]]; then
        MODEL_SIZE="7b"
    else
        MODEL_SIZE="3b"
    fi

    echo "Selected model: qwen2.5-coder-${MODEL_SIZE}-instruct-q4_k_m"

    # Export to GitHub Actions environment
    {
        echo "MODEL_SIZE=${MODEL_SIZE}"
        echo "RUNNER_VCPUS=${RUNNER_VCPUS}"
        echo "MODEL_ARCH=${MODEL_ARCH}"
        echo "AVAILABLE_RAM_GB=${AVAILABLE_RAM_GB}"
    } >> "$GITHUB_ENV"
}