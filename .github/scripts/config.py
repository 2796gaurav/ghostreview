"""
.github/scripts/config.py

Load and validate .github/localreviewer.yml.
Returns a typed dict with all fields populated (defaults filled in).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "model": {
        "size": "auto",
    },
    "review": {
        "passes": ["summary", "bugs", "security"],
        "ignore_paths": [
            "*.lock",
            "*.min.js",
            "*.min.css",
            "dist/**",
            "build/**",
            ".next/**",
        ],
        "security_critical_paths": [],
        "min_severity": "warning",
        "always_comment": True,
    },
    "auto_fix": {
        "enabled": True,
        "trigger_label": "auto-fix",
        "trigger_comment": "/fix",
        "required_permission": "write",
        "confidence_threshold": 0.70,
        "max_files": 5,
        "protected_paths": [
            ".github/**",
            "*.yml",
            "*.yaml",
            "Makefile",
            "Dockerfile",
            "*.tf",
            "*.tfvars",
        ],
    },
    "conventions": {
        "language": None,
        "framework": None,
        "notes": "",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(path: str | None = None) -> dict[str, Any]:
    """
    Load .github/localreviewer.yml and merge with defaults.
    Missing keys are filled from DEFAULT_CONFIG.
    Unknown keys are allowed (future-proofing).
    """
    if path is None:
        path = ".github/localreviewer.yml"

    config = dict(DEFAULT_CONFIG)

    cfg_path = Path(path)
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"localreviewer.yml must be a YAML mapping, got {type(raw).__name__}"
            )
        config = _deep_merge(config, raw)

    # Validate max_files hard cap
    max_files = config.get("auto_fix", {}).get("max_files", 5)
    if max_files > 5:
        config["auto_fix"]["max_files"] = 5

    # Validate confidence_threshold range
    ct = config.get("auto_fix", {}).get("confidence_threshold", 0.70)
    config["auto_fix"]["confidence_threshold"] = max(0.0, min(1.0, float(ct)))

    # Normalize min_severity
    valid_sev = {"info", "warning", "error", "critical"}
    sev = config.get("review", {}).get("min_severity", "warning")
    if sev not in valid_sev:
        config["review"]["min_severity"] = "warning"

    return config