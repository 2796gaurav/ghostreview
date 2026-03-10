#!/usr/bin/env bash
# .github/scripts/lib/build.sh
# Build llama.cpp from source if not cached

set -euo pipefail

BINDIR="$HOME/.cache/ghost-review/llama-bin"
BUILDDIR="$HOME/.cache/ghost-review/llama-build"

build_llama_cpp_if_needed() {
    mkdir -p "$BINDIR"

    # Check if already cached
    if [[ -f "$BINDIR/llama-server" ]]; then
        echo "llama-server cached: $(${BINDIR}/llama-server --version 2>&1 | head -1 || echo 'unknown version')"
        return 0
    fi

    echo "Building llama.cpp from source..."
    
    # Install build dependencies if missing
    if ! command -v cmake &> /dev/null; then
        echo "Installing build dependencies..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq build-essential cmake git
    fi
    
    mkdir -p "$BUILDDIR"
    
    # Clone llama.cpp (shallow clone for faster download)
    if [[ ! -d "$BUILDDIR/llama.cpp" ]]; then
        echo "Cloning llama.cpp repository..."
        git clone --depth 1 --branch b8252 https://github.com/ggml-org/llama.cpp.git "$BUILDDIR/llama.cpp" 2>&1 | tail -3
    fi
    
    cd "$BUILDDIR/llama.cpp"
    
    # Build with cmake - optimized for ARM64
    echo "Configuring build..."
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=ON \
        -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_NATIVE=OFF \
        2>&1 | tail -5
    
    echo "Building (this may take 3-5 minutes on first run)..."
    cmake --build build --config Release -j$(nproc) --target llama-server 2>&1 | tail -10
    
    # Copy binary to cache
    cp "$BUILDDIR/llama.cpp/build/bin/llama-server" "$BINDIR/llama-server"
    chmod +x "$BINDIR/llama-server"
    
    echo "llama-server built successfully"
    "$BINDIR/llama-server" --version 2>&1 | head -1 || true
}
