"""
.github/scripts/schemas.py

JSON Schema definitions for LLM structured output.
All schemas use strict=True for grammar-constrained generation.
"""

# ─────────────────────────────────────────────────────────────────────
# Agent Actions for Auto-Fix
# ─────────────────────────────────────────────────────────────────────

AGENT_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "thinking": {"type": "string", "description": "Step-by-step reasoning for this action"},
        "action": {
            "type": "string",
            "enum": ["read_file", "list_directory", "generate_patch", "finish", "give_up"],
        },
        "action_params": {
            "type": "object",
            "description": "Parameters for the chosen action",
        },
        "confidence": {"type": "number", "description": "Confidence level 0.0-1.0"},
    },
    "required": ["thinking", "action", "action_params", "confidence"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────
# Verification Schema for Patch Quality Check
# ─────────────────────────────────────────────────────────────────────

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "correct": {"type": "boolean", "description": "Does patch correctly fix the issue?"},
        "verified_confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
        "concern": {"type": "string", "description": "Any concerns with the fix"},
    },
    "required": ["correct", "verified_confidence", "concern"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────
# PR Review Schemas
# ─────────────────────────────────────────────────────────────────────

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "pr_type": {
            "type": "string",
            "enum": ["feature", "bugfix", "refactor", "docs", "tests", "config", "chore", "security"],
        },
        "pr_description": {"type": "string"},
        "affected_components": {"type": "array", "items": {"type": "string"}},
        "risk_areas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["pr_type", "pr_description", "affected_components", "risk_areas"],
    "additionalProperties": False,
}


FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "line_numbers": {"type": "string", "description": "Line numbers as string (e.g., 12-15)"},
        "title": {"type": "string", "description": "Brief title of the issue"},
        "description": {"type": "string", "description": "Detailed description"},
        "severity": {"type": "string", "enum": ["critical", "error", "warning", "info"]},
        "confidence": {"type": "number", "description": "0.0-1.0"},
        "suggested_fix": {"type": "string"},
        "vulnerability_class": {"type": "string", "description": "For security findings"},
    },
    "required": ["file_path", "line_numbers", "title", "description", "severity", "confidence", "suggested_fix"],
    "additionalProperties": False,
}


BUGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": FINDING_SCHEMA},
    },
    "required": ["findings"],
    "additionalProperties": False,
}


SECURITY_SCHEMA = BUGS_SCHEMA  # Same structure


SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "confidence": {"type": "number", "description": "0.0-1.0"},
        "summary": {"type": "string"},
        "recommendation": {"type": "string"},
    },
    "required": ["risk_level", "confidence", "summary", "recommendation"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────
# Error Analysis Schema for Self-Healing
# ─────────────────────────────────────────────────────────────────────

ERROR_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string", "description": "Detailed analysis of what caused the error"},
        "fix_suggestion": {"type": "string", "description": "Explanation of how to fix the error"},
        "corrected_code": {"type": "string", "description": "The complete corrected file content"},
        "confidence": {"type": "number", "description": "Confidence that the fix is correct (0.0-1.0)"},
    },
    "required": ["analysis", "fix_suggestion", "corrected_code", "confidence"],
    "additionalProperties": False,
}
