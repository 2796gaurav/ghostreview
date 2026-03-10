#!/usr/bin/env bash
# .github/scripts/lib/download.sh
# Download llama.cpp binary and model files with caching support.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Install llama-cpp-python with server support
# This provides llama-server binary via pip wheels (better ARM64 support)
# ──────────────────────────────────────────────────────────────────────
download_llama_cpp_if_needed() {
    local BINDIR="$HOME/.cache/ghost-review/llama-bin"
    mkdir -p "$BINDIR"

    # Check if we already have it in cache
    if [[ -f "$BINDIR/llama-server" ]]; then
        echo "llama-server cached: $(${BINDIR}/llama-server --version 2>&1 | head -1 || echo 'version unknown')"
        return 0
    fi

    echo "Installing llama-cpp-python with server support..."
    
    # Install llama-cpp-python with server support
    # This downloads pre-built wheels when available
    pip install -q "llama-cpp-python[server]>=0.3.0" --no-cache-dir 2>&1 | tail -5 || {
        echo "ERROR: Failed to install llama-cpp-python"
        exit 1
    }

    # Find the installed llama-server binary
    local SERVER_BIN
    SERVER_BIN=$(python3 -c "import llama_cpp.server; print(llama_cpp.server.__file__)" 2>/dev/null | xargs dirname | xargs -I{} dirname)/bin/llama-server || true
    
    if [[ -z "$SERVER_BIN" ]] || [[ ! -f "$SERVER_BIN" ]]; then
        # Try to find it in PATH
        SERVER_BIN=$(which llama-server 2>/dev/null || true)
    fi
    
    if [[ -z "$SERVER_BIN" ]] || [[ ! -f "$SERVER_BIN" ]]; then
        # Try alternative: python -m llama_cpp.server
        echo "Creating llama-server wrapper..."
        cat > "$BINDIR/llama-server" << 'EOF'
#!/usr/bin/env bash
# Wrapper for llama-cpp-python server
exec python3 -m llama_cpp.server "$@"
EOF
        chmod +x "$BINDIR/llama-server"
    else
        cp "$SERVER_BIN" "$BINDIR/llama-server"
        chmod +x "$BINDIR/llama-server"
    fi

    # Add to PATH for this session
    export PATH="$BINDIR:$PATH"
    
    # Verify installation
    if "$BINDIR/llama-server" --version 2>&1 | head -1; then
        echo "llama-server installed successfully"
    else
        echo "WARNING: llama-server may not work correctly"
    fi
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
        echo "Downloading Qwen2.5-Coder-7B Q4_K_M (~4.7 GB)..."
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
        echo "Downloading Qwen2.5-Coder-3B Q4_K_M (~2.3 GB)..."
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
