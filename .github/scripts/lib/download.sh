#!/usr/bin/env bash
# .github/scripts/lib/download.sh
# Download llama.cpp binary and model files with caching support.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Verify llama-cpp-python is installed and find llama-server
# ──────────────────────────────────────────────────────────────────────
download_llama_cpp_if_needed() {
    local BINDIR="$HOME/.cache/ghost-review/llama-bin"
    mkdir -p "$BINDIR"

    # Check if we already have it in cache
    if [[ -f "$BINDIR/llama-server" ]]; then
        echo "llama-server cached"
        return 0
    fi

    echo "Setting up llama-server..."
    
    # Find llama-server from pip installation
    local SERVER_BIN=""
    
    # Try to find in PATH first
    SERVER_BIN=$(which llama-server 2>/dev/null || true)
    
    # If not in PATH, try to find via python
    if [[ -z "$SERVER_BIN" ]]; then
        SERVER_BIN=$(python3 -c "
import llama_cpp.server
import os
import sys
# Look for llama-server in the package bin directory
pkg_dir = os.path.dirname(llama_cpp.server.__file__)
for root, dirs, files in os.walk(os.path.join(pkg_dir, '..', '..')):
    if 'llama-server' in files:
        print(os.path.join(root, 'llama-server'))
        sys.exit(0)
" 2>/dev/null || true)
    fi
    
    # Alternative: try to find with pip show
    if [[ -z "$SERVER_BIN" ]] || [[ ! -f "$SERVER_BIN" ]]; then
        local PKG_PATH
        PKG_PATH=$(pip show llama-cpp-python 2>/dev/null | grep "Location:" | cut -d' ' -f2)
        if [[ -n "$PKG_PATH" ]]; then
            for candidate in "$PKG_PATH/bin/llama-server" "$PKG_PATH/../bin/llama-server"; do
                if [[ -f "$candidate" ]]; then
                    SERVER_BIN="$candidate"
                    break
                fi
            done
        fi
    fi
    
    if [[ -n "$SERVER_BIN" ]] && [[ -f "$SERVER_BIN" ]]; then
        cp "$SERVER_BIN" "$BINDIR/llama-server"
        chmod +x "$BINDIR/llama-server"
        echo "llama-server installed from: $SERVER_BIN"
    else
        # Create a wrapper script that uses python -m
        echo "Creating llama-server wrapper..."
        cat > "$BINDIR/llama-server" << 'EOF'
#!/usr/bin/env bash
# Wrapper for llama-cpp-python server
exec python3 -m llama_cpp.server "$@"
EOF
        chmod +x "$BINDIR/llama-server"
        echo "llama-server wrapper created"
    fi

    # Add to PATH for this session
    export PATH="$BINDIR:$PATH"
    
    # Verify installation
    if "$BINDIR/llama-server" --help &>/dev/null; then
        echo "llama-server ready"
    else
        echo "WARNING: llama-server may have issues, but continuing..."
    fi
}

# ──────────────────────────────────────────────────────────────────────
# Download Qwen model based on MODEL_SIZE (7b or 3b)
# Only downloads the model that will actually be used
# ──────────────────────────────────────────────────────────────────────
download_models_if_needed() {
    local DIR="$HOME/.cache/ghost-review/models"
    mkdir -p "$DIR"
    
    # Get the model size from environment (set by detect.sh)
    local MODEL_SIZE="${MODEL_SIZE:-7b}"
    
    if [[ "$MODEL_SIZE" == "7b" ]]; then
        # Download 7B model only
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
        
        echo "Model ready: qwen2.5-coder-7b-instruct-q4_k_m"
        ls -lh "$DIR"/*7b* 2>/dev/null || true
        
    elif [[ "$MODEL_SIZE" == "3b" ]]; then
        # Download 3B model only
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
        
        echo "Model ready: qwen2.5-coder-3b-instruct-q4_k_m"
        ls -lh "$DIR"/*3b* 2>/dev/null || true
        
    else
        echo "ERROR: Unknown MODEL_SIZE: $MODEL_SIZE"
        exit 1
    fi
}
