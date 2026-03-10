"""
.github/scripts/context_builder.py

Build codebase context for the review: files related to the changed code
but not in the diff itself (imported modules, callers, test counterparts).

This gives the model crucial context for understanding whether a change
might break downstream callers, or whether tests cover the changed logic.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any


def detect_language(path: str) -> str:
    """Detect primary language from file extension."""
    ext = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".go": "go",
        ".java": "java",
        ".rb": "ruby",
        ".rs": "rust",
        ".php": "php",
        ".cs": "csharp",
        ".cpp": "cpp",
        ".c": "c",
        ".kt": "kotlin",
        ".swift": "swift",
    }.get(ext, "unknown")


def extract_python_imports(file_path: str, repo_path: str) -> list[str]:
    """
    Parse Python AST to extract imported module paths.
    Converts module.submodule → module/submodule.py relative to repo.
    """
    try:
        src = Path(file_path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=file_path)
    except (SyntaxError, OSError):
        return []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidate = alias.name.replace(".", "/") + ".py"
                full = Path(repo_path) / candidate
                if full.exists():
                    imports.append(str(full.relative_to(repo_path)))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Relative imports
                dots = node.level or 0
                base = Path(file_path).parent
                for _ in range(dots - 1):
                    base = base.parent
                candidate_dir = base / node.module.replace(".", "/")
                candidate_file = base / (node.module.replace(".", "/") + ".py")
                for candidate in [candidate_file, candidate_dir / "__init__.py"]:
                    rel = candidate.relative_to(repo_path) if candidate.is_absolute() else candidate
                    if (Path(repo_path) / rel).exists():
                        imports.append(str(rel))
                        break
    return imports


def extract_js_ts_imports(file_path: str, repo_path: str) -> list[str]:
    """
    Extract relative imports from JS/TS files via regex.
    Handles: import x from './y', require('./y'), import('./y')
    """
    try:
        src = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    pattern = re.compile(
        r"""(?:import\s+[^"']*from\s+|require\s*\(\s*|import\s*\(\s*)["'](\./[^"']+)["']"""
    )

    results: list[str] = []
    base_dir = Path(file_path).parent

    for m in pattern.finditer(src):
        rel_import = m.group(1)
        candidate = (base_dir / rel_import).resolve()
        for suffix in ["", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"]:
            test = Path(str(candidate) + suffix)
            if test.exists():
                try:
                    rel = test.relative_to(Path(repo_path).resolve())
                    results.append(str(rel))
                    break
                except ValueError:
                    pass

    return results


def extract_static_imports(
    file_path: str, lang: str, repo_path: str
) -> list[str]:
    """Dispatch to language-specific import extractor."""
    if lang == "python":
        return extract_python_imports(file_path, repo_path)
    if lang in ("javascript", "typescript"):
        return extract_js_ts_imports(file_path, repo_path)
    # For other languages, skip static import analysis
    return []


def find_callers(changed_file: str, repo_path: str) -> list[str]:
    """
    Use ripgrep (or grep fallback) to find files that import or call
    the changed file's module name.
    """
    module_name = Path(changed_file).stem
    # Skip very common names that would match everything
    if module_name in ("index", "main", "__init__", "utils", "helpers"):
        return []

    try:
        result = subprocess.run(
            ["rg", "--files-with-matches", "--no-heading",
             "--glob", "!.git",
             module_name,
             repo_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.strip().splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Fallback to grep if rg not available
        try:
            result = subprocess.run(
                ["grep", "-r", "-l", module_name, repo_path,
                 "--exclude-dir=.git",
                 "--exclude-dir=node_modules",
                 "--include=*.py",
                 "--include=*.js",
                 "--include=*.ts"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = result.stdout.strip().splitlines()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    callers: list[str] = []
    for line in lines[:20]:  # cap at 20 callers
        try:
            rel = str(Path(line).relative_to(repo_path))
            if rel != changed_file and not rel.startswith(".git"):
                callers.append(rel)
        except ValueError:
            pass
    return callers


def find_test_files(changed_file: str, repo_path: str) -> list[str]:
    """
    Find test files associated with the changed file.
    Looks for: test_<name>.py, <name>_test.py, <name>.test.ts, etc.
    """
    stem = Path(changed_file).stem
    tests: list[str] = []

    patterns = [
        f"test_{stem}.py",
        f"{stem}_test.py",
        f"{stem}.test.ts",
        f"{stem}.test.js",
        f"{stem}.spec.ts",
        f"{stem}.spec.js",
        f"test_{stem}.rb",
        f"{stem}_test.go",
    ]

    repo = Path(repo_path)
    for pattern in patterns:
        for match in repo.rglob(pattern):
            if ".git" not in str(match):
                try:
                    tests.append(str(match.relative_to(repo)))
                except ValueError:
                    pass

    return tests[:5]  # cap at 5 test files


def read_truncated(path: str, max_chars: int = 800) -> str:
    """Read a file and truncate to max_chars, appending a note if truncated."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return content
    except OSError:
        return f"[Cannot read file: {path}]"


def build_codebase_context(
    diff_files: list[str],
    repo_path: str,
    token_budget: int,
) -> str:
    """
    Build the codebase context snippet for the review prompts.

    For each changed file:
      - Extracts static imports → score 1.0
      - Finds reverse callers → score 0.8
      - Finds test files → score 0.6

    Fills budget at ~800 chars per file (≈ 229 tokens each).
    Files already in the diff are excluded.
    """
    seen = set(diff_files)
    candidates: list[tuple[float, str]] = []

    for changed in diff_files:
        full_path = str(Path(repo_path) / changed)
        if not Path(full_path).exists():
            continue

        lang = detect_language(changed)

        for path in extract_static_imports(full_path, lang, repo_path):
            if path not in seen:
                candidates.append((1.0, path))
                seen.add(path)

        for path in find_callers(changed, repo_path):
            if path not in seen:
                candidates.append((0.8, path))
                seen.add(path)

        for path in find_test_files(changed, repo_path):
            if path not in seen:
                candidates.append((0.6, path))
                seen.add(path)

    # Sort by relevance score descending
    candidates.sort(key=lambda x: x[0], reverse=True)

    parts: list[str] = []
    tokens_used = 0

    for _, rel_path in candidates:
        full_path = str(Path(repo_path) / rel_path)
        snippet = read_truncated(full_path, max_chars=800)
        cost = int(len(snippet) / 3.5)
        if tokens_used + cost > token_budget:
            break
        parts.append(f"// {rel_path}\n{snippet}")
        tokens_used += cost

    if not parts:
        return "(No relevant context files found)"

    return "\n\n".join(parts)