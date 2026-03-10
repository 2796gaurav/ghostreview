"""
.github/scripts/schemas.py

All JSON Schema definitions used for grammar-constrained LLM output.

llama.cpp enforces these at the token sampler via GGML grammar, making
invalid output structurally impossible — not merely unlikely. The model
cannot produce a severity value outside the enum, cannot omit required
fields, and cannot generate malformed JSON.

Usage:
    from schemas import SUMMARY_SCHEMA, FINDINGS_SCHEMA, SECURITY_SCHEMA,
                        SYNTHESIS_SCHEMA, AGENT_ACTION_SCHEMA, VERIFY_SCHEMA
"""

# ── Pass 1: PR Summary ───────────────────────────────────────────────
SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["summary", "pr_type", "risk_assessment", "changed_files_summary"],
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-4 sentence plain-English description of what this PR does"
        },
        "pr_type": {
            "type": "string",
            "enum": [
                "feature", "bugfix", "refactor", "security",
                "performance", "docs", "test", "ci",
                "dependency", "mixed"
            ]
        },
        "risk_assessment": {
            "type": "string",
            "description": "Brief paragraph on the primary risk areas introduced"
        },
        "changed_files_summary": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "change_type", "description"],
                "additionalProperties": False,
                "properties": {
                    "file":        {"type": "string"},
                    "change_type": {
                        "type": "string",
                        "enum": ["added", "modified", "deleted", "renamed"]
                    },
                    "description": {"type": "string"}
                }
            }
        }
    }
}

# ── Pass 2: Bug and Logic Findings ───────────────────────────────────
FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "severity", "description"],
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["bug", "logic", "performance", "style", "suggestion"]
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error", "critical"]
                    },
                    "file":         {"type": "string"},
                    "line_start":   {"type": "integer"},
                    "line_end":     {"type": "integer"},
                    "description":  {"type": "string"},
                    "suggested_fix": {"type": "string"}
                }
            }
        }
    }
}

# ── Pass 3: Security Findings ─────────────────────────────────────────
SECURITY_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "vulnerability_class", "severity",
                    "description", "exploitability"
                ],
                "additionalProperties": False,
                "properties": {
                    "vulnerability_class": {
                        "type": "string",
                        "enum": [
                            "injection", "authentication_bypass",
                            "authorization_bypass", "insecure_deserialization",
                            "hardcoded_credential", "path_traversal", "ssrf",
                            "xss", "cryptographic_weakness",
                            "information_disclosure", "none_found"
                        ]
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error", "critical"]
                    },
                    "file":          {"type": "string"},
                    "line_start":    {"type": "integer"},
                    "description":   {"type": "string"},
                    "suggested_fix": {"type": "string"},
                    "exploitability": {
                        "type": "string",
                        "enum": ["theoretical", "requires_auth",
                                 "unauthenticated", "trivial"]
                    }
                }
            }
        }
    }
}

# ── Pass 4: Synthesis / Final Verdict ────────────────────────────────
SYNTHESIS_SCHEMA = {
    "type": "object",
    "required": ["risk_level", "merge_recommendation", "confidence", "rationale"],
    "additionalProperties": False,
    "properties": {
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"]
        },
        "merge_recommendation": {
            "type": "string",
            "enum": ["approve", "request_changes", "needs_discussion"]
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0
        },
        "rationale": {
            "type": "string",
            "description": "2-4 sentences explaining the recommendation"
        }
    }
}

# ── Auto-PR Agentic Loop Action Schema ───────────────────────────────
AGENT_ACTION_SCHEMA = {
    "type": "object",
    "required": ["thinking", "action", "action_params"],
    "additionalProperties": False,
    "properties": {
        "thinking": {
            "type": "string",
            "description": "Step-by-step reasoning about what to do next and why"
        },
        "action": {
            "type": "string",
            "enum": ["read_file", "list_directory", "generate_patch", "finish", "give_up"]
        },
        "action_params": {
            "type": "object",
            "description": "Parameters specific to the chosen action"
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0
        }
    }
}

# ── Auto-PR Patch Self-Verification Schema ────────────────────────────
VERIFY_SCHEMA = {
    "type": "object",
    "required": ["correct", "verified_confidence", "concern"],
    "additionalProperties": False,
    "properties": {
        "correct": {
            "type": "boolean",
            "description": "True if the patch correctly and safely fixes the issue"
        },
        "verified_confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0
        },
        "concern": {
            "type": "string",
            "description": "Empty string if correct=true; specific concern if correct=false"
        }
    }
}

# ── Auto-Fix v2: Enhanced Action Schema with ReAct Pattern ───────────
AGENT_ACTION_SCHEMA_V2 = {
    "type": "object",
    "required": ["thinking", "action", "action_params", "confidence"],
    "additionalProperties": False,
    "properties": {
        "thinking": {
            "type": "string",
            "description": "Step-by-step reasoning about what to do next"
        },
        "reflection": {
            "type": "string",
            "description": "Self-critique and evaluation of approach"
        },
        "action": {
            "type": "string",
            "enum": ["read_file", "list_directory", "generate_patch", "finish", "give_up"]
        },
        "action_params": {
            "type": "object",
            "description": "Parameters specific to the chosen action"
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in this step (0.0-1.0)"
        }
    }
}

# ── Auto-Fix v2: Enhanced Verification Schema ─────────────────────────
VERIFY_SCHEMA_V2 = {
    "type": "object",
    "required": ["correct", "verified_confidence", "concern"],
    "additionalProperties": False,
    "properties": {
        "correct": {
            "type": "boolean",
            "description": "True if the patch correctly and safely fixes the issue"
        },
        "verified_confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence that the patch is correct"
        },
        "concern": {
            "type": "string",
            "description": "Empty if correct=true; specific concerns if correct=false"
        },
        "suggested_improvements": {
            "type": "string",
            "description": "Optional suggestions to improve the patch"
        }
    }
}