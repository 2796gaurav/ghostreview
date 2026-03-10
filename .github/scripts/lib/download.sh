#!/usr/bin/env bash
# .github/scripts/lib/download.sh
# Download llama.cpp binary and model files with caching support.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Build llama.cpp from source (fallback since binaries aren't available)
# ──────────────────────────────────────────────────────────────────────
download_llama_cpp_if_needed() {
    local BINDIR="$HOME/.cache/ghost-review/llama-bin"
    mkdir -p "$BINDIR"

    # Check if we already have it in cache
    if [[ -f "$BINDIR/llama-server" ]]; then
        echo "llama-server cached: $(${BINDIR}/llama-server --version 2>&1 | head -1 || echo 'version unknown')"
        return 0
    fi

    echo "Building llama.cpp from source..."
    
    local BUILDDIR="$HOME/.cache/ghost-review/llama-build"
    mkdir -p "$BUILDDIR"
    
    # Clone llama.cpp (shallow clone for faster download)
    if [[ ! -d "$BUILDDIR/llama.cpp" ]]; then
        echo "Cloning llama.cpp repository..."
        git clone --depth 1 --branch b8252 https://github.com/ggml-org/llama.cpp.git "$BUILDDIR/llama.cpp" 2>&1 | tail -3
    fi
    
    cd "$BUILDDIR/llama.cpp"
    
    # Build with cmake
    echo "Building with cmake..."
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=ON \
        -DLLAMA_BUILD_SERVER=ON 2>&1 | tail -5
    
    cmake --build build --config Release -j$(nproc) 2>&1 | tail -10
    
    # Copy binary to cache
    cp "$BUILDDIR/llama.cpp/build/bin/llama-server" "$BINDIR/llama-server"
    chmod +x "$BINDIR/llama-server"
    
    echo "llama-server built successfully"
    "$BINDIR/llama-server" --version 2>&1 | head -1 || true
}

# ──────────────────────────────────────────────────────────────────────
# Download Qwen model based on MODEL_SIZE (7b or 3b)
# Only downloads the model that will actually be used
# ──────────────────────────────────────────────────────────────────────
download_models_if_needed() {
    local DIR="$HOME/.cache/ghost-review/models"
    mkdir -p "$DIR"
    
    # Get the model size from environment (set by detect.sh)
    # Default to 7b if not set
    local MODEL_SIZE="${MODEL_SIZE:-7b}"
    
    echo "Model size selected: $MODEL_SIZE"
    
    if [[ "$MODEL_SIZE" == "7b" ]]; then
        # Download 7B model only
        if [[ ! -f "$DIR/qwen2.5-coder-7b-instruct-q4_k_m.gguf" ]]; then
            echo "Downloading Qwen2.5-Coder-7B Q4_K_M (~4.7 GB)..."
            python3 -c "
from huggingface_hub import hf_hub_download
import os
os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'
hf_hub_download(
    repo_id='Qwen/Qwen2.5-Coder-7B-Instruct-GGUF',
    filename='qwen2.5-coder-7b-instruct-q4_k_m.gguf',
    local_dir='$DIR',
    local_dir_use_symlinks=False
)
" 2>&1 || {
                echo "ERROR: Failed to download 7B model"
                exit 1
            }
        else
            echo "7B model cached"
        fi
        
        echo "Model ready: qwen2.5-coder-7b-instruct-q4_k_m"
        ls -lh "$DIR"/*7b*.gguf 2>/dev/null || true
        
    elif [[ "$MODEL_SIZE" == "3b" ]]; then
        # Download 3B model only
        if [[ ! -f "$DIR/qwen2.5-coder-3b-instruct-q4_k_m.gguf" ]]; then
            echo "Downloading Qwen2.5-Coder-3B Q4_K_M (~2.3 GB)..."
            python3 -c "
from huggingface_hub import hf_hub_download
import os
os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'
hf_hub_download(
    repo_id='Qwen/Qwen2.5-Coder-3B-Instruct-GGUF',
    filename='qwen2.5-coder-3b-instruct-q4_k_m.gguf',
    local_dir='$DIR',
    local_dir_use_symlinks=False
)
" 2>&1 || {
                echo "ERROR: Failed to download 3B model"
                exit 1
            }
        else
            echo "3B model cached"
        fi
        
        echo "Model ready: qwen2.5-coder-3b-instruct-q4_k_m"
        ls -lh "$DIR"/*3b*.gguf 2>/dev/null || true
        
    else
        echo "ERROR: Unknown MODEL_SIZE: $MODEL_SIZE"
        exit 1
    fi
}
