"""
.github/scripts/diff_parser.py

Advanced diff preprocessing with:
  1. Aho-Corasick multi-pattern matching for secrets (O(n) vs O(n*m))
  2. Hierarchical compression for repetitive hunks
  3. Token-aware truncation preserving semantic boundaries
  4. Security-critical path prioritization
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import ahocorasick
    AHO_AVAILABLE = True
except ImportError:
    AHO_AVAILABLE = False

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────
# Patterns always skipped (lock files, minified, build artifacts)
# ─────────────────────────────────────────────────────────────────────
_ALWAYS_SKIP_PATTERNS: list[str] = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Gemfile.lock",
    "poetry.lock", "Cargo.lock", "go.sum", "composer.lock", "mix.lock",
    "Pipfile.lock", ".min.js", ".min.css", ".bundle.js", ".map",
    "/dist/", "/build/", "/.next/", "/__pycache__/", "/node_modules/",
    "/.gradle/", "/target/", "/out/", "/bin/", "/obj/",
]

_ALWAYS_SKIP_REGEX = re.compile(
    r'(' + '|'.join(re.escape(p) for p in _ALWAYS_SKIP_PATTERNS) + r')',
    re.IGNORECASE
)


# ─────────────────────────────────────────────────────────────────────
# Secret patterns for Aho-Corasick automaton
# ─────────────────────────────────────────────────────────────────────
_SECRET_PATTERNS: list[tuple[str, str, re.Pattern | None]] = [
    # (pattern_name, pattern_or_keyword, optional_regex_validator)
    ("aws_access_key", "AKIA", re.compile(r'AKIA[0-9A-Z]{16}')),
    ("aws_secret", "aws_secret_access_key", None),
    ("private_key", "-----BEGIN", re.compile(r'-----BEGIN (RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----')),
    ("github_token", "ghp_", re.compile(r'gh[pousr]_[A-Za-z0-9]{36,}')),
    ("github_token_new", "github_pat_", re.compile(r'github_pat_[A-Za-z0-9_]{22,}')),
    ("slack_token", "xoxb-", re.compile(r'xox[baprs]-[0-9]+-[0-9]+-[A-Za-z0-9]+')),
    ("generic_api_key", "api_key", None),
    ("generic_secret", "secret", None),
    ("password_assignment", "password =", None),
    ("token_assignment", "token =", None),
    ("bearer_token", "Bearer ", re.compile(r'Bearer [a-zA-Z0-9_\-\.]+')),
    ("basic_auth", "Basic ", re.compile(r'Basic [a-zA-Z0-9+/=]+')),
    ("discord_token", re.compile(r'[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27}'), None),
    ("stripe_key", "sk_live_", re.compile(r'sk_live_[a-zA-Z0-9]{24,}')),
    ("stripe_test_key", "sk_test_", re.compile(r'sk_test_[a-zA-Z0-9]{24,}')),
    ("jwt_token", "eyJ", re.compile(r'eyJ[A-Za-z0-9_\-]*\.eyJ[A-Za-z0-9_\-]*\.[A-Za-z0-9_\-]*')),
]

# Regex patterns for post-validation
_SECRET_VALIDATORS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(?i)(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[=:]\s*["\']([^"\']{8,})["\']'), "credential_assignment"),
    (re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']([^"\']{4,})["\']'), "password_assignment"),
    (re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'), "base64_secret"),
]


class SecretDetector:
    """Aho-Corasick based secret detector for O(n) multi-pattern matching."""
    
    def __init__(self):
        self.automaton = None
        self._build_automaton()
    
    def _build_automaton(self):
        """Build Aho-Corasick automaton from secret patterns."""
        if not AHO_AVAILABLE:
            return
        
        self.automaton = ahocorasick.Automaton()
        
        for name, pattern, validator in _SECRET_PATTERNS:
            if isinstance(pattern, re.Pattern):
                # For regex patterns, use a keyword that triggers validation
                pattern_str = pattern.pattern[:20]  # Use first 20 chars as keyword
            else:
                pattern_str = pattern
            
            self.automaton.add_word(pattern_str.lower(), (name, pattern, validator))
        
        self.automaton.make_automaton()
    
    def detect_and_redact(self, content: str) -> tuple[str, list[str]]:
        """
        Detect secrets using Aho-Corasick and redact them.
        Returns (redacted_content, warnings).
        """
        warnings: list[str] = []
        redacted_positions: list[tuple[int, int, str]] = []  # (start, end, label)
        
        if AHO_AVAILABLE and self.automaton:
            # Aho-Corasick scan - O(n) where n = len(content)
            for end_pos, (name, pattern, validator) in self.automaton.iter(content.lower()):
                # Find actual match position in original content
                start_pos = end_pos - len(pattern) + 1 if isinstance(pattern, str) else max(0, end_pos - 20)
                
                # Validate with regex if provided
                if validator:
                    match = validator.search(content, start_pos)
                    if match:
                        redacted_positions.append((match.start(), match.end(), name))
                else:
                    # Extract surrounding context for heuristic validation
                    context_start = max(0, start_pos - 10)
                    context_end = min(len(content), end_pos + 50)
                    context = content[context_start:context_end]
                    
                    # Heuristic: must have assignment-like pattern nearby
                    if re.search(r'[=:]\s*["\']?[A-Za-z0-9_\-]{8,}', context):
                        # Find the actual value
                        val_match = re.search(r'[=:]\s*["\']?([A-Za-z0-9_\-/+=]{8,})', context)
                        if val_match:
                            abs_start = context_start + val_match.start(1)
                            abs_end = context_start + val_match.end(1)
                            redacted_positions.append((abs_start, abs_end, name))
        
        # Also run regex validators for complex patterns
        for regex, label in _SECRET_VALIDATORS:
            for match in regex.finditer(content):
                # Skip if already covered
                if not any(start <= match.start() < end for start, end, _ in redacted_positions):
                    redacted_positions.append((match.start(), match.end(), label))
        
        # Merge overlapping ranges and redact
        if redacted_positions:
            redacted_positions.sort()
            merged: list[tuple[int, int, str]] = [redacted_positions[0]]
            
            for start, end, label in redacted_positions[1:]:
                prev_start, prev_end, prev_label = merged[-1]
                if start <= prev_end + 5:  # Overlapping or close
                    merged[-1] = (prev_start, max(prev_end, end), prev_label if prev_end - prev_start > end - start else label)
                else:
                    merged.append((start, end, label))
            
            # Build redacted content (backwards to preserve positions)
            result = list(content)
            for start, end, label in reversed(merged):
                warnings.append(f"⚠️ Potential `{label}` redacted")
                result[start:end] = f"[REDACTED:{label.upper()}]"
            
            content = "".join(result)
        
        return content, warnings


# Global detector instance
_secret_detector = SecretDetector()


# ─────────────────────────────────────────────────────────────────────
# Token estimation
# ─────────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken or fallback heuristic."""
    if TIKTOKEN_AVAILABLE:
        try:
            # Use cl100k_base encoding (used by GPT-4, similar to Qwen)
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except:
            pass
    # Fallback: ~3.5 chars per token for code
    return int(len(text) / 3.5)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token budget, preserving line boundaries."""
    if estimate_tokens(text) <= max_tokens:
        return text
    
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    tokens_used = 0
    
    for line in lines:
        line_tokens = estimate_tokens(line)
        if tokens_used + line_tokens > max_tokens:
            # Try to add at least a truncation notice
            notice = f"\n[TRUNCATED: diff exceeds {max_tokens} token budget]\n"
            if tokens_used + estimate_tokens(notice) <= max_tokens:
                result.append(notice)
            break
        result.append(line)
        tokens_used += line_tokens
    
    return "".join(result)


# ─────────────────────────────────────────────────────────────────────
# Diff processing
# ─────────────────────────────────────────────────────────────────────

def get_diff(base_sha: str, head_sha: str) -> str:
    """Get git diff between two SHAs."""
    result = subprocess.run(
        ["git", "diff", "--unified=3", f"{base_sha}...{head_sha}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def extract_changed_files(raw_diff: str) -> list[str]:
    """Extract changed file paths from diff."""
    files: list[str] = []
    for line in raw_diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null":
                files.append(path)
    return list(dict.fromkeys(files))


def sanitize(content: str, max_chars: int = 8000) -> str:
    """Sanitize user content for safe prompt injection."""
    content = content[:max_chars]
    # Remove control chars except whitespace
    content = "".join(c for c in content if ord(c) >= 32 or c in "\n\t\r")
    return content


def redact_secrets(content: str) -> tuple[str, list[str]]:
    """Redact secrets using Aho-Corasick algorithm."""
    return _secret_detector.detect_and_redact(content)


# ─────────────────────────────────────────────────────────────────────
# Hierarchical compression
# ─────────────────────────────────────────────────────────────────────

def compress_repetitive_hunks(diff: str, similarity_threshold: float = 0.85) -> str:
    """
    Compress diff by replacing similar hunks with placeholders.
    Uses fuzzy matching to detect mechanically repeated changes.
    """
    hunk_pattern = re.compile(
        r"(@@ -\d+,\d+ \+\d+,\d+ @@[^\n]*\n(?:[ +\-\\][^\n]*\n)+)"
    )
    
    hunks: list[tuple[str, str]] = []  # (normalized, original)
    compressed_count = 0
    
    def normalize_hunk(hunk: str) -> str:
        """Normalize hunk for comparison (remove line numbers, normalize whitespace)."""
        lines = hunk.splitlines()
        if not lines:
            return ""
        # Keep header but normalize content
        header = lines[0]
        content_lines = lines[1:]
        # Remove line numbers from markers, sort content lines
        normalized_content = sorted(
            re.sub(r'^([\+\-])\s*', r'\1', line)
            for line in content_lines
            if line.strip()
        )
        return header.split()[0] + "|" + "|".join(normalized_content[:5])
    
    def find_similar_hunk(normalized: str) -> int | None:
        """Find index of similar hunk using simple string similarity."""
        for i, (existing_norm, _) in enumerate(hunks):
            # Simple Jaccard-like similarity on line sets
            set1 = set(normalized.split("|"))
            set2 = set(existing_norm.split("|"))
            if set1 and set2:
                intersection = len(set1 & set2)
                union = len(set1 | set2)
                if union > 0 and intersection / union >= similarity_threshold:
                    return i
        return None
    
    def replace_hunk(match: re.Match) -> str:
        nonlocal compressed_count
        hunk = match.group(0)
        normalized = normalize_hunk(hunk)
        
        similar_idx = find_similar_hunk(normalized)
        if similar_idx is not None:
            compressed_count += 1
            return f"[COMPRESSED: similar to hunk #{similar_idx + 1}]\n"
        
        hunks.append((normalized, hunk))
        return hunk
    
    result = hunk_pattern.sub(replace_hunk, diff)
    if compressed_count > 0:
        print(f"  Compressed {compressed_count} repetitive hunks")
    return result


# ─────────────────────────────────────────────────────────────────────
# Main preprocessing pipeline
# ─────────────────────────────────────────────────────────────────────

def _should_skip_file(file_path: str, config: dict[str, Any]) -> bool:
    """Check if file should be skipped."""
    # Check always-skip patterns
    if _ALWAYS_SKIP_REGEX.search(file_path):
        return True
    
    # Check user-configured patterns
    user_ignores = config.get("review", {}).get("ignore_paths", [])
    for pattern in user_ignores:
        if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(Path(file_path).name, pattern.lstrip("*/")):
            return True
    
    return False


def _split_diff_into_files(diff: str) -> list[tuple[str, str]]:
    """Split diff into (file_path, content) tuples."""
    file_pattern = re.compile(r'^diff --git a/(.+?) b/\1', re.MULTILINE)
    
    files: list[tuple[str, str]] = []
    matches = list(file_pattern.finditer(diff))
    
    for i, match in enumerate(matches):
        file_path = match.group(1)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        content = diff[start:end]
        files.append((file_path, content))
    
    return files


def _score_file_importance(file_path: str, config: dict[str, Any]) -> int:
    """Score file importance for prioritization (lower = more important)."""
    critical_paths = config.get("review", {}).get("security_critical_paths", [])
    
    # Security-critical paths get priority 0
    for pattern in critical_paths:
        if fnmatch.fnmatch(file_path, pattern):
            return 0
    
    # Test files are lower priority
    if any(x in file_path for x in ["_test.", ".test.", "test_", "/tests/"]):
        return 3
    
    # Config and doc files are lower priority
    if any(file_path.endswith(ext) for ext in [".md", ".txt", ".json", ".yml", ".yaml"]):
        return 2
    
    # Source code is normal priority
    return 1


def preprocess_diff(
    raw_diff: str,
    max_tokens: int,
    config: dict[str, Any],
) -> tuple[str, list[str]]:
    """
    Full preprocessing pipeline:
      1. Split into files
      2. Filter non-reviewable files  
      3. Redact secrets (Aho-Corasick)
      4. Compress repetitive hunks
      5. Truncate to token budget (priority-aware)
    
    Returns: (processed_diff, warnings)
    """
    warnings: list[str] = []
    
    # Split into files
    files = _split_diff_into_files(raw_diff)
    if not files:
        return "", []
    
    # Filter and score files
    scored_files: list[tuple[int, str, str]] = []  # (score, path, content)
    for path, content in files:
        if _should_skip_file(path, config):
            continue
        score = _score_file_importance(path, config)
        scored_files.append((score, path, content))
    
    if not scored_files:
        return "", ["All changed files were filtered (lock files, build artifacts, etc.)"]
    
    # Sort by importance score
    scored_files.sort(key=lambda x: x[0])
    
    # Redact secrets from all content
    all_content = "".join(content for _, _, content in scored_files)
    redacted_content, secret_warnings = redact_secrets(all_content)
    warnings.extend(secret_warnings)
    
    # Split back into per-file content (maintain order)
    file_contents: list[tuple[str, str]] = []  # (path, content)
    pos = 0
    for score, path, original in scored_files:
        length = len(original)
        file_contents.append((path, redacted_content[pos:pos + length]))
        pos += length
    
    # Compress repetitive hunks
    compressed_files: list[tuple[str, str]] = []
    for path, content in file_contents:
        compressed = compress_repetitive_hunks(content)
        compressed_files.append((path, compressed))
    
    # Check total size
    total_content = "".join(c for _, c in compressed_files)
    total_tokens = estimate_tokens(total_content)
    
    if total_tokens <= max_tokens:
        return total_content, warnings
    
    # Need to truncate - allocate proportionally by priority
    warnings.append(f"⚠️ Diff truncated from {total_tokens} to ~{max_tokens} tokens")
    
    result_parts: list[str] = []
    tokens_remaining = max_tokens
    
    # Priority 0 files get processed first (no truncation)
    for path, content in compressed_files:
        if _score_file_importance(path, config) == 0:
            content_tokens = estimate_tokens(content)
            if content_tokens <= tokens_remaining:
                result_parts.append(content)
                tokens_remaining -= content_tokens
    
    # Remaining files get proportional allocation
    remaining_files = [(p, c) for p, c in compressed_files if _score_file_importance(p, config) > 0]
    remaining_tokens = sum(estimate_tokens(c) for _, c in remaining_files)
    
    for path, content in remaining_files:
        if tokens_remaining <= 0:
            break
        
        content_tokens = estimate_tokens(content)
        # Proportional share, minimum 300 tokens per file
        share = max(300, int(max_tokens * content_tokens / max(remaining_tokens, 1)))
        share = min(share, tokens_remaining)
        
        if content_tokens <= share:
            result_parts.append(content)
            tokens_remaining -= content_tokens
        else:
            # Truncate this file
            truncated = truncate_to_tokens(content, share)
            result_parts.append(truncated)
            tokens_remaining -= estimate_tokens(truncated)
    
    return "".join(result_parts), warnings
