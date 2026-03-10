"""
.github/scripts/prompts.py

System prompt builder and user prompt templates for each review pass.

The system prompt is static across all passes. llama.cpp's --keep 1024
pins it in KV cache after the first request. Passes 2-4 get the ~700-token
system prompt for free, skipping its prefill cost entirely.
"""

from __future__ import annotations

from typing import Any


# ─────────────────────────────────────────────────────────────────────
# System Prompt Construction
# ─────────────────────────────────────────────────────────────────────

_SYSTEM_BASE = """\
You are a senior software engineer performing a structured code review.

SEVERITY DEFINITIONS (use exactly these values in JSON output):
  critical : exploitable vulnerability (injection, auth bypass, RCE, data corruption)
  error    : production crash, data loss, unhandled exception in hot path
  warning  : latent bug, missing validation, performance regression
  info     : missing test, minor improvement opportunity

REVIEW STANDARDS:
- Every finding MUST reference a specific file and line number range
- Every finding MUST include a concrete suggested_fix (code snippet preferred)
- Confidence below 0.6 → omit the finding entirely; do not speculate
- Do NOT report issues already handled by the existing code
- Do NOT hallucinate file names or line numbers you cannot verify from the diff

OUTPUT CONTRACT:
- Output valid JSON matching the provided schema ONLY
- No markdown, no prose, no preamble, no explanation outside JSON fields
- All string fields must be non-empty if the field is required
"""

_CONVENTIONS_TEMPLATE = """\

REPOSITORY CONVENTIONS:
{conventions}
"""


def build_system_prompt(config: dict[str, Any]) -> str:
    """
    Build the static system prompt. Appended with repo conventions if set
    in localreviewer.yml. Kept under 700 tokens to fit --keep 1024.
    """
    prompt = _SYSTEM_BASE

    conv = config.get("conventions", {})
    parts = []
    if conv.get("language"):
        parts.append(f"Primary language: {conv['language']}")
    if conv.get("framework"):
        parts.append(f"Framework: {conv['framework']}")
    if conv.get("notes"):
        parts.append(conv["notes"].strip())

    if parts:
        conventions_text = "\n".join(parts)
        prompt += _CONVENTIONS_TEMPLATE.format(conventions=conventions_text)

    return prompt.strip()


# ─────────────────────────────────────────────────────────────────────
# Pass 1: PR Summary
# temperature=0.3 — minor phrasing variety acceptable
# ─────────────────────────────────────────────────────────────────────

PROMPT_SUMMARY = """\
Summarize this pull request. Identify what it does, classify its type, and \
assess the primary risk areas.

PR Title: {title}
PR Description:
{body}

Diff (first 8000 chars):
{diff}

Output the summary JSON now.
"""

# ─────────────────────────────────────────────────────────────────────
# Pass 2: Bug and Logic Findings
# temperature=0.1 — precision required; minimal hallucination
# ─────────────────────────────────────────────────────────────────────

PROMPT_BUGS = """\
Analyze this diff for bugs, logic errors, performance issues, and code \
quality problems.

RELEVANT CONTEXT (imported modules and callers of changed files):
{context}

DIFF TO REVIEW:
{diff}

Rules:
- Only report findings that are clearly evidenced in the diff
- Include file path and line numbers from the diff headers (+++ b/path, @@ lines)
- If there are no findings, return {{"findings": []}}

Output the findings JSON now.
"""

# ─────────────────────────────────────────────────────────────────────
# Pass 3: Security Scan
# temperature=0.1 — must not fabricate or miss critical findings
# ─────────────────────────────────────────────────────────────────────

PROMPT_SECURITY = """\
Analyze this diff for security vulnerabilities. Focus on:
- Injection flaws (SQL, command, LDAP, XPath)
- Authentication and authorization bypasses
- Hardcoded secrets or credentials
- Path traversal or SSRF
- Cryptographic weaknesses
- Information disclosure

DIFF TO REVIEW:
{diff}

Rules:
- Only report vulnerabilities clearly visible in the diff
- If no vulnerability is found, return a single finding with \
vulnerability_class="none_found" and severity="info"
- Do NOT report theoretical issues not grounded in actual diff code

Output the security findings JSON now.
"""

# ─────────────────────────────────────────────────────────────────────
# Pass 4: Synthesis
# temperature=0.2 — consistent risk + recommendation
# ─────────────────────────────────────────────────────────────────────

PROMPT_SYNTHESIS = """\
Based on the following analysis results, synthesize a final review verdict.

SUMMARY:
{summary}

BUG/LOGIC FINDINGS:
{bugs}

SECURITY FINDINGS:
{security}

Determine the overall risk level and merge recommendation. Your confidence
should reflect how certain you are about the verdict given the evidence above.

Output the synthesis JSON now.
"""

# ─────────────────────────────────────────────────────────────────────
# Auto-PR Agent System Prompt
# ─────────────────────────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """\
You are a senior software engineer fixing a GitHub issue by exploring the \
codebase and generating a targeted patch.

PROCESS:
1. Read the issue description carefully
2. Use list_directory and read_file to understand relevant files
3. Generate a patch ONLY after reading all relevant files
4. Call finish when you have a complete patch, or give_up if the issue is \
ambiguous, out-of-scope, or requires changes you cannot safely make

RULES:
- NEVER patch a file you have not read first
- Minimal change principle: only change what is necessary to fix the issue
- Never modify files in protected_paths (CI, Dockerfiles, Makefiles, IaC)
- Confidence must reflect actual certainty, not optimism
- Maximum 5 files changed
- If the fix requires more than 5 files, call give_up with explanation

OUTPUT CONTRACT:
Output valid JSON matching the schema ONLY.
No markdown, no prose, no preamble.
"""