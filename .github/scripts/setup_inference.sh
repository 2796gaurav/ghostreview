#!/usr/bin/env bash
# .github/scripts/setup_inference.sh
# Setup script - kept for backward compatibility
# Most logic now moved directly into workflow for better caching

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/lib/detect.sh"
source "$SCRIPT_DIR/lib/download.sh"
source "$SCRIPT_DIR/lib/server.sh"

# This script is now mostly a wrapper for backward compatibility
# The workflow handles caching and conditional build/download directly
echo "Setup inference - delegating to workflow steps"
