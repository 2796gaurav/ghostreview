#!/usr/bin/env bash
# .github/scripts/lib/download.sh
# Download llama.cpp binary and model files with caching support.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Download llama.cpp pre-built binary
# ──────────────────────────────────────────────────────────────────────
download_llama_cpp_if_needed() {
    local BINDIR="$HOME/.cache/ghost-review/llama-bin"
    local BINARY="$BINDIR/llama-server"
    local VERSION="${LLAMA_CPP_VERSION:-b8252}"

    if [[ -f "$BINARY" ]]; then
        echo "llama-server cached: $(${BINARY} --version 2>&1 | head -1 || echo 'version unknown')"
        return 0
    fi

    echo "Downloading llama.cpp ${VERSION}..."
    mkdir -p "$BINDIR"

    local ARCH
    ARCH=$(uname -m)
    local URL

    if [[ "$ARCH" == "aarch64" ]]; then
        URL="https://github.com/ggml-org/llama.cpp/releases/download/${VERSION}/llama-${VERSION}-bin-ubuntu-arm64.zip"
    else
        URL="https://github.com/ggml-org/llama.cpp/releases/download/${VERSION}/llama-${VERSION}-bin-ubuntu-x64.zip"
    fi

    echo "URL: ${URL}"

    # Download and extract
    wget -q --show-progress "$URL" -O /tmp/llama.zip 2>&1 || {
        echo "ERROR: Failed to download llama.cpp binary from ${URL}"
        exit 1
    }

    unzip -q /tmp/llama.zip -d "$BINDIR/"
    chmod +x "$BINDIR"/llama-server* 2>/dev/null || true

    # Handle different zip structures
    if [[ -f "$BINDIR/llama-server" ]]; then
        chmod +x "$BINDIR/llama-server"
    elif [[ -f "$BINDIR/build/bin/llama-server" ]]; then
        ln -sf "$BINDIR/build/bin/llama-server" "$BINDIR/llama-server"
    else
        # Find llama-server binary
        local FOUND
        FOUND=$(find "$BINDIR" -name "llama-server" -type f | head -1)
        if [[ -n "$FOUND" ]]; then
            ln -sf "$FOUND" "$BINDIR/llama-server"
        else
            echo "ERROR: llama-server binary not found in extracted archive"
            exit 1
        fi
    fi

    rm -f /tmp/llama.zip
    echo "llama-server installed: ${BINDIR}/llama-server"
}

# ──────────────────────────────────────────────────────────────────────
# Download Qwen models if not cached
# ──────────────────────────────────────────────────────────────────────
download_models_if_needed() {
    local DIR="$HOME/.cache/ghost-review/models"
    mkdir -p "$DIR"

    # Install huggingface_hub if needed
    if ! python3 -c "import huggingface_hub" 2>/dev/null; then
        echo "Installing huggingface_hub..."
        pip install -q huggingface_hub hf_transfer
    fi

    # Download 7B model
    if [[ ! -f "$DIR/qwen2.5-coder-7b-instruct-q4_k_m.gguf" ]]; then
        echo "Downloading Qwen2.5-Coder-7B Q4_K_M..."
        HF_HUB_ENABLE_HF_TRANSFER=1 huggingface-cli download \
            Qwen/Qwen2.5-Coder-7B-Instruct-GGUF \
            --include "qwen2.5-coder-7b-instruct-q4_k_m.gguf" \
            --local-dir "$DIR" \
            2>&1 || {
            echo "ERROR: Failed to download 7B model"
            exit 1
        }
    else
        echo "7B model cached"
    fi

    # Download 3B fallback model
    if [[ ! -f "$DIR/qwen2.5-coder-3b-instruct-q4_k_m.gguf" ]]; then
        echo "Downloading Qwen2.5-Coder-3B Q4_K_M (fallback)..."
        HF_HUB_ENABLE_HF_TRANSFER=1 huggingface-cli download \
            Qwen/Qwen2.5-Coder-3B-Instruct-GGUF \
            --include "qwen2.5-coder-3b-instruct-q4_k_m.gguf" \
            --local-dir "$DIR" \
            2>&1 || {
            echo "ERROR: Failed to download 3B model"
            exit 1
        }
    else
        echo "3B model cached"
    fi

    echo "Models ready in ${DIR}"
    ls -lh "${DIR}"/*.gguf 2>/dev/null || true
}
