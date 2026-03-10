#!/usr/bin/env bash
# .github/scripts/lib/download.sh
# Download model files with caching support.
# Note: llama.cpp binary is handled by workflow cache, not this script.

set -euo pipefail

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
