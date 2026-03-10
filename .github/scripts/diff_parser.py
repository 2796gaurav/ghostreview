"""
.github/scripts/diff_parser.py

Diff preprocessing pipeline:
  1. Filter out non-reviewable files (lock files, minified JS, etc.)
  2. Apply user-configured ignore_paths from localreviewer.yml
  3. Redact secrets before they reach the model
  4. Compress repetitive hunks
  5. Truncate to token budget while preserving structure

Also provides get_diff() and extract_changed_files().
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────
# Files always skipped — not reviewable, waste tokens
# ─────────────────────────────────────────────────────────────────────
_ALWAYS_SKIP: list[str] = [
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Gemfile.lock",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "mix.lock",
    "Pipfile.lock",
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".map",
    "/dist/",
    "/build/",
    "/.next/",
    "/__pycache__/",
    "/node_modules/",
    "/.gradle/",
    "Binary files",
]

# ─────────────────────────────────────────────────────────────────────
# Secret patterns — redacted before model sees the diff
# ─────────────────────────────────────────────────────────────────────
_SECRET_PATTERNS: list[tuple[str, str]] = [
    # Generic key=value credentials
    (
        r'(?i)(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token'
        r'|password|passwd|private[_-]?key)\s*[=:]\s*["\']([^"\']{8,})["\']',
        "credential",
    ),
    # AWS access key ID
    (r'(?i)aws_access_key_id\s*[=:]\s*([A-Z0-9]{20})', "aws_key"),
    # AWS secret access key
    (r'(?i)aws_secret_access_key\s*[=:]\s*([A-Za-z0-9/+=]{40})', "aws_secret"),
    # PEM private keys
    (r'-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----', "private_key"),
    # GitHub tokens
    (r'gh[psoruat]_[A-Za-z0-9]{36,}', "github_token"),
    # Slack tokens
    (r'xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+', "slack_token"),
    # Discord tokens
    (r'[a-z0-9]{24}\.[a-z0-9]{6}\.[a-z0-9_\-]{27}', "discord_token"),
    # Generic base64-looking secrets in env files
    (r'(?i)(SECRET|TOKEN|KEY|PASSWORD)\s*=\s*[A-Za-z0-9+/]{32,}={0,2}', "env_secret"),
]


def get_diff(base_sha: str, head_sha: str) -> str:
    """
    Run git diff between base and head SHAs and return the raw diff text.
    Uses --unified=3 (standard context lines).
    """
    result = subprocess.run(
        ["git", "diff", "--unified=3", f"{base_sha}...{head_sha}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def extract_changed_files(raw_diff: str) -> list[str]:
    """
    Parse the diff header lines to extract all changed file paths.
    Returns a list of relative paths (no 'b/' prefix).
    """
    files: list[str] = []
    for line in raw_diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null":
                files.append(path)
    return list(dict.fromkeys(files))  # deduplicate, preserve order


def redact_secrets(content: str) -> tuple[str, list[str]]:
    """
    Scan for and redact known secret patterns.
    Returns (redacted_content, list_of_warning_messages).
    """
    warnings: list[str] = []
    for pattern, label in _SECRET_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            warnings.append(
                f"⚠️ Potential `{label}` detected in diff — "
                f"redacted before model ingestion"
            )
            content = re.sub(
                pattern,
                f"[REDACTED:{label.upper()}]",
                content,
                flags=re.IGNORECASE,
            )
    return content, warnings


def sanitize(content: str, max_chars: int = 8000) -> str:
    """
    Sanitize user-controlled content (PR description, issue body).
    Caps length and strips non-printable control characters.
    Mitigates prompt injection via untrusted user content.
    """
    content = content[:max_chars]
    content = "".join(c for c in content if ord(c) >= 32 or c in "\n\t\r")
    return content


def _should_skip_file(diff_header: str, config: dict[str, Any]) -> bool:
    """Determine whether a file's diff block should be skipped."""
    # Always-skip patterns
    if any(pattern in diff_header for pattern in _ALWAYS_SKIP):
        return True
    # User-configured ignore_paths
    user_ignores = config.get("review", {}).get("ignore_paths", [])
    for pattern in user_ignores:
        if fnmatch.fnmatch(diff_header, f"*{pattern}*"):
            return True
    return False


def _split_diff_into_file_blocks(diff: str) -> list[tuple[str, list[str]]]:
    """
    Split a unified diff into (header_line, block_lines) per file.
    Returns a list of (diff_header, all_lines_for_this_file).
    """
    blocks: list[tuple[str, list[str]]] = []
    current_header = ""
    current_lines: list[str] = []

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if current_header:
                blocks.append((current_header, current_lines))
            current_header = line.rstrip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_header:
        blocks.append((current_header, current_lines))

    return blocks


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 3.5 characters for code."""
    return int(len(text) / 3.5)


def _truncate_block_to_budget(
    lines: list[str], token_budget: int
) -> tuple[list[str], int]:
    """
    Truncate a single file's diff block to fit within token_budget.
    Always includes at minimum the first hunk header and a few lines.
    Returns (truncated_lines, tokens_used).
    """
    result: list[str] = []
    tokens_used = 0
    truncated_at = -1

    for i, line in enumerate(lines):
        line_cost = _estimate_tokens(line)
        if tokens_used + line_cost > token_budget and i > 5:
            truncated_at = i
            break
        result.append(line)
        tokens_used += line_cost

    if truncated_at > 0:
        omitted = len(lines) - truncated_at
        result.append(f"[TRUNCATED: {omitted} lines omitted due to token budget]\n")

    return result, tokens_used


def compress_repetitive_hunks(diff: str) -> str:
    """
    When N consecutive identical-pattern hunks exist across multiple files
    (e.g., same import added to 20 files), replace hunks 3 through N with
    a placeholder. Reduces token cost of mechanical bulk changes.
    """
    # Simple heuristic: if a hunk appears verbatim 3+ times, compress after 2
    hunk_pattern = re.compile(r"(@@ -\d+,\d+ \+\d+,\d+ @@[^\n]*\n(?:[^@d][^\n]*\n)*)")
    seen_hunks: dict[str, int] = {}
    compressed_count = 0

    def replace_repeated(match: re.Match) -> str:  # type: ignore[type-arg]
        nonlocal compressed_count
        hunk = match.group(0)
        # Normalize line numbers for comparison
        normalized = re.sub(r"@@ -\d+,\d+ \+\d+,\d+ @@", "@@ ... @@", hunk)
        seen_hunks[normalized] = seen_hunks.get(normalized, 0) + 1
        if seen_hunks[normalized] > 2:
            compressed_count += 1
            return (
                f"[COMPRESSED: identical hunk repeated "
                f"(occurrence {seen_hunks[normalized]}), shown twice above]\n"
            )
        return hunk

    result = hunk_pattern.sub(replace_repeated, diff)
    if compressed_count > 0:
        print(f"  Compressed {compressed_count} repetitive hunks.")
    return result


def preprocess_diff(
    raw_diff: str,
    max_tokens: int,
    config: dict[str, Any],
) -> tuple[str, list[str]]:
    """
    Full preprocessing pipeline.

    Steps:
      1. Split into per-file blocks
      2. Filter non-reviewable files
      3. Redact secrets
      4. Compress repetitive hunks
      5. Truncate to token budget (proportional allocation)

    Returns:
        (processed_diff_text, warning_messages)
    """
    all_warnings: list[str] = []

    # Split into blocks
    file_blocks = _split_diff_into_file_blocks(raw_diff)
    if not file_blocks:
        return "", []

    # Filter
    kept_blocks = [
        (header, lines)
        for header, lines in file_blocks
        if not _should_skip_file(header, config)
    ]

    if not kept_blocks:
        return "", ["All changed files were filtered (lock files, minified, etc.)"]

    # Reassemble and redact
    full_text = "".join(
        "".join(lines) for _, lines in kept_blocks
    )
    full_text, secret_warnings = redact_secrets(full_text)
    all_warnings.extend(secret_warnings)

    # Compress repetitive hunks
    full_text = compress_repetitive_hunks(full_text)

    # Check if we're within budget
    if _estimate_tokens(full_text) <= max_tokens:
        return full_text, all_warnings

    # Truncate: allocate budget proportionally by block size
    all_warnings.append(
        f"⚠️ Diff exceeded token budget ({max_tokens} tokens). "
        "Some files were truncated."
    )

    # Prioritize security-critical paths
    critical_paths = config.get("review", {}).get("security_critical_paths", [])

    def _block_priority(header: str) -> int:
        for pattern in critical_paths:
            if fnmatch.fnmatch(header, f"*{pattern}*"):
                return 0
        return 1

    kept_blocks.sort(key=lambda b: _block_priority(b[0]))

    # Allocate tokens proportionally
    total_raw = sum(
        _estimate_tokens("".join(lines)) for _, lines in kept_blocks
    )
    result_parts: list[str] = []
    budget_remaining = max_tokens

    for header, lines in kept_blocks:
        block_text = "".join(lines)
        block_tokens = _estimate_tokens(block_text)
        # Proportional share, minimum 500 tokens per file
        share = max(500, int(max_tokens * block_tokens / max(total_raw, 1)))
        share = min(share, budget_remaining)
        if share <= 0:
            break
        truncated, used = _truncate_block_to_budget(lines, share)
        result_parts.append("".join(truncated))
        budget_remaining -= used

    return "\n".join(result_parts), all_warnings