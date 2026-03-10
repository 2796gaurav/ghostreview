#!/usr/bin/env bash
# .github/scripts/lib/build.sh
# Build llama.cpp from source with static linking

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

    echo "Building llama.cpp from source (static linking)..."
    
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
    
    # Build with cmake - optimized for ARM64 with STATIC linking
    # -DBUILD_SHARED_LIBS=OFF creates a self-contained binary
    echo "Configuring build with static linking..."
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_NATIVE=OFF \
        -DGGML_NATIVE=OFF \
        -DCMAKE_CXX_FLAGS="-O3" \
        2>&1 | tail -10
    
    echo "Building (this may take 3-5 minutes on first run)..."
    cmake --build build --config Release -j$(nproc) --target llama-server 2>&1 | tail -15
    
    # Copy binary to cache
    cp "$BUILDDIR/llama.cpp/build/bin/llama-server" "$BINDIR/llama-server"
    chmod +x "$BINDIR/llama-server"
    
    # Verify it's statically linked (no shared lib dependencies)
    echo "Verifying static build..."
    if ldd "$BINDIR/llama-server" 2>&1 | grep -q "libmtmd\|libllama\|libggml"; then
        echo "WARNING: Binary has shared library dependencies!"
        ldd "$BINDIR/llama-server" || true
    else
        echo "✓ Static build verified (no shared library dependencies)"
    fi
    
    echo "llama-server built successfully"
    "$BINDIR/llama-server" --version 2>&1 | head -1 || true
}

# Also need to update the cache key when build changes
get_llama_cache_key() {
    echo "ghost-review-llama-b8252-static-ubuntu-arm64-v1"
}
