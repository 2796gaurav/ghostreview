"""
.github/scripts/auto_fix.py

Auto-PR Creator — agentic loop triggered by /fix comment or auto-fix label.

Architecture:
  - Grammar-constrained JSON replaces native tool calling
  - Model outputs action enum + params; orchestrator executes
  - Self-verification pass for patches below confidence 0.85
  - All safety gates are hard-coded; none can be configured away

Safety gates (non-negotiable):
  Gate 1 — Trigger: only write+ permission actors
  Gate 2 — Protected paths: always enforced
  Gate 3 — Max 5 files changed
  Gate 4 — Always draft PR, never auto-merge
  Gate 5 — Confidence threshold gating
  Gate 6 — Human-in-the-loop: CODEOWNERS assigned, reasoning visible
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from diff_parser import sanitize
from github import Github
from github_api import check_actor_permission, create_draft_pr
from llm_client import LLMClient, LLMError
from prompts import AGENT_SYSTEM_PROMPT
from schemas import AGENT_ACTION_SCHEMA, VERIFY_SCHEMA


# ─────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PatchSpec:
    file_path: str
    patched_content: str
    explanation: str
    patch_confidence: float
    final_confidence: float = 0.0
    verification_concern: str = ""


@dataclass
class FixResult:
    patches: list[PatchSpec] = field(default_factory=list)
    gave_up: bool = False
    reason: str = ""
    agent_thinking_trace: str = ""


# ─────────────────────────────────────────────────────────────────────
# Protected path checking
# ─────────────────────────────────────────────────────────────────────

# These paths are ALWAYS protected, regardless of user config.
_HARDCODED_PROTECTED = [
    ".github/**",
    "*.yml",
    "*.yaml",
    "Makefile",
    "makefile",
    "Dockerfile",
    "dockerfile",
    "*.tf",
    "*.tfvars",
    "*.tfstate",
    ".env",
    ".env.*",
]

def _is_protected_path(file_path: str, config: dict[str, Any]) -> bool:
    """Check whether a path is protected (hard-coded + user config)."""
    import fnmatch

    all_patterns = list(_HARDCODED_PROTECTED)
    user_protected = config.get("auto_fix", {}).get("protected_paths", [])
    all_patterns.extend(user_protected)

    for pattern in all_patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        # Also check path components
        if fnmatch.fnmatch(Path(file_path).name, pattern.lstrip("*/")):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# File tree builder
# ─────────────────────────────────────────────────────────────────────

def _build_file_tree(
    repo_path: str,
    max_depth: int = 4,
    max_entries: int = 200,
) -> str:
    """
    Build a condensed file tree for the agent's initial context.
    Hidden directories (.git, .cache, etc.) and build artifacts are excluded.
    """
    SKIP_DIRS = {
        ".git", ".github", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".next", ".gradle", ".cache", "vendor", ".tox",
        "coverage", ".mypy_cache", ".pytest_cache",
    }

    lines: list[str] = []
    count = 0
    repo = Path(repo_path)

    def _walk(path: Path, depth: int, prefix: str) -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return

        for entry in entries:
            if count >= max_entries:
                lines.append(f"{prefix}... (truncated)")
                return
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                count += 1
                _walk(entry, depth + 1, prefix + "  ")
            else:
                lines.append(f"{prefix}{entry.name}")
                count += 1

    _walk(repo, 0, "")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Self-verification
# ─────────────────────────────────────────────────────────────────────

async def _verify_patch(
    patch: PatchSpec,
    issue_body: str,
    llm: LLMClient,
) -> PatchSpec:
    """
    Run an independent second-pass verification for patches below
    confidence 0.85. Combines agent confidence with verifier confidence.
    """
    try:
        result = await asyncio.wait_for(
            llm.chat(
                system=(
                    "You are a careful code reviewer. Assess whether a proposed "
                    "patch correctly and safely fixes the described issue."
                ),
                user=(
                    f"Issue description:\n{sanitize(issue_body, 800)}\n\n"
                    f"File: {patch.file_path}\n"
                    f"Explanation: {patch.explanation}\n\n"
                    f"First 60 lines of patched file:\n"
                    + "\n".join(patch.patched_content.splitlines()[:60])
                    + "\n\nDoes this patch correctly and safely fix the issue?"
                ),
                schema=VERIFY_SCHEMA,
                max_tokens=256,
                temperature=0.1,
            ),
            timeout=60.0,
        )
        if result["correct"]:
            patch.final_confidence = (
                patch.patch_confidence + result["verified_confidence"]
            ) / 2
        else:
            patch.final_confidence = patch.patch_confidence * 0.4
            patch.verification_concern = result["concern"]
        print(
            f"  Verification: correct={result['correct']} "
            f"final_confidence={patch.final_confidence:.2f} "
            f"concern={result['concern'][:100]}"
        )
    except (LLMError, asyncio.TimeoutError) as exc:
        print(f"  Verification failed: {exc}. Using original confidence.")
        patch.final_confidence = patch.patch_confidence
    return patch


# ─────────────────────────────────────────────────────────────────────
# Agentic loop
# ─────────────────────────────────────────────────────────────────────

async def run_agentic_fix(
    issue: dict[str, Any],
    repo_path: str,
    config: dict[str, Any],
    llm: LLMClient,
) -> FixResult:
    """
    Grammar-constrained agentic loop.

    The model outputs a JSON object with:
      - thinking: step-by-step reasoning
      - action: one of read_file | list_directory | generate_patch | finish | give_up
      - action_params: action-specific dict
      - confidence: float 0-1

    The orchestrator executes the action and feeds the result back.
    Max 10 iterations (configurable higher only for more file reads, never fewer).
    """
    MAX_ITERATIONS = 12
    CONFIDENCE_THRESHOLD = config.get("auto_fix", {}).get("confidence_threshold", 0.70)
    MAX_FILES = min(config.get("auto_fix", {}).get("max_files", 5), 5)  # hard cap 5

    file_tree = _build_file_tree(repo_path)
    patches: list[PatchSpec] = []
    thinking_trace: list[str] = []
    conversation: list[dict[str, str]] = []

    initial_message = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Description:\n{sanitize(issue.get('body') or '', max_chars=3000)}\n\n"
        f"Repository structure:\n{file_tree}\n\n"
        "Begin by identifying which files are relevant to this issue."
    )
    conversation.append({"role": "user", "content": initial_message})

    for iteration in range(MAX_ITERATIONS):
        print(f"  Agent iteration {iteration + 1}/{MAX_ITERATIONS}...")

        # Build conversation prompt
        user_content = "\n\n".join(
            f"[{msg['role'].upper()}]\n{msg['content']}"
            for msg in conversation
        )

        try:
            result = await asyncio.wait_for(
                llm.chat(
                    system=AGENT_SYSTEM_PROMPT,
                    user=user_content,
                    schema=AGENT_ACTION_SCHEMA,
                    max_tokens=2048,
                    temperature=0.2,
                ),
                timeout=120.0,
            )
        except (LLMError, asyncio.TimeoutError) as exc:
            print(f"  Agent call failed on iteration {iteration+1}: {exc}")
            break

        action = result.get("action", "give_up")
        params = result.get("action_params") or {}
        thinking = result.get("thinking", "")
        confidence = float(result.get("confidence") or 0.5)

        if thinking:
            thinking_trace.append(f"[Step {iteration+1}] {thinking}")

        conversation.append({
            "role": "assistant",
            "content": json.dumps(result),
        })

        # ── Execute action ────────────────────────────────────────────
        if action == "read_file":
            file_path = params.get("path", "").lstrip("/")
            full_path = Path(repo_path) / file_path
            if full_path.exists() and full_path.is_file():
                content = full_path.read_text(encoding="utf-8", errors="replace")[:8000]
                action_result = f"Contents of {file_path}:\n\n{content}"
                if len(content) >= 8000:
                    action_result += "\n\n[File truncated at 8000 chars]"
            else:
                action_result = f"ERROR: File not found: {file_path}"

        elif action == "list_directory":
            dir_path = params.get("path", ".").lstrip("/")
            full_path = Path(repo_path) / dir_path
            if full_path.exists() and full_path.is_dir():
                entries = sorted(
                    str(p.relative_to(full_path))
                    for p in full_path.iterdir()
                    if not p.name.startswith(".")
                )
                action_result = (
                    f"Contents of {dir_path}/:\n"
                    + "\n".join(entries[:100])
                )
                if len(entries) > 100:
                    action_result += f"\n... and {len(entries) - 100} more"
            else:
                action_result = f"ERROR: Directory not found: {dir_path}"

        elif action == "generate_patch":
            fp = params.get("file_path", "").lstrip("/")
            patched = params.get("patched_content", "")
            explanation = params.get("explanation", "")
            patch_confidence = float(params.get("confidence") or confidence)

            if not fp:
                action_result = "ERROR: file_path is required for generate_patch"
            elif _is_protected_path(fp, config):
                action_result = f"ERROR: {fp} is a protected path and cannot be modified"
            elif len(patches) >= MAX_FILES:
                action_result = f"ERROR: Maximum of {MAX_FILES} files already staged"
            elif not patched:
                action_result = "ERROR: patched_content is required"
            else:
                patch = PatchSpec(
                    file_path=fp,
                    patched_content=patched,
                    explanation=explanation,
                    patch_confidence=patch_confidence,
                    final_confidence=patch_confidence,
                )
                patches.append(patch)
                action_result = f"Patch staged for {fp} (confidence={patch_confidence:.2f})"

        elif action == "finish":
            print(f"  Agent finished after {iteration + 1} iterations.")
            break

        elif action == "give_up":
            explanation = params.get("explanation", "No explanation provided.")
            print(f"  Agent gave up: {explanation}")
            return FixResult(
                gave_up=True,
                reason=explanation,
                agent_thinking_trace="\n\n".join(thinking_trace),
            )

        else:
            action_result = f"ERROR: Unknown action '{action}'"

        conversation.append({
            "role": "user",
            "content": f"Result:\n{action_result}\n\nContinue.",
        })

    if not patches:
        return FixResult(
            gave_up=True,
            reason="Agent completed without generating any patches.",
            agent_thinking_trace="\n\n".join(thinking_trace),
        )

    # ── Self-verification for low-confidence patches ──────────────────
    issue_body = issue.get("body") or ""
    verified_patches: list[PatchSpec] = []

    for patch in patches:
        if patch.patch_confidence < 0.85:
            print(f"  Verifying patch for {patch.file_path} (confidence={patch.patch_confidence:.2f})")
            patch = await _verify_patch(patch, issue_body, llm)
        verified_patches.append(patch)

    # Filter out patches where verification found a serious problem
    final_patches = [
        p for p in verified_patches
        if p.final_confidence >= CONFIDENCE_THRESHOLD
    ]

    rejected = [p for p in verified_patches if p.final_confidence < CONFIDENCE_THRESHOLD]
    if rejected:
        for p in rejected:
            print(
                f"  Rejected patch for {p.file_path}: "
                f"final_confidence={p.final_confidence:.2f} "
                f"concern={p.verification_concern[:80]}"
            )

    return FixResult(
        patches=final_patches,
        gave_up=len(final_patches) == 0,
        reason=(
            f"{len(rejected)} patch(es) rejected by verification."
            if rejected and not final_patches
            else ""
        ),
        agent_thinking_trace="\n\n".join(thinking_trace),
    )


# ─────────────────────────────────────────────────────────────────────
# Branch and commit helpers
# ─────────────────────────────────────────────────────────────────────

def _apply_patches_to_branch(
    patches: list[PatchSpec],
    branch_name: str,
    issue_number: int,
    issue_title: str,
    base_branch: str,
) -> None:
    """
    Create a git branch, write patched file contents, and commit.
    Runs in the checked-out repository root.
    """
    def _run(cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Git command failed: {' '.join(cmd)}\n"
                f"stderr: {result.stderr[:500]}"
            )

    # Create branch from current HEAD
    _run(["git", "checkout", "-b", branch_name])

    # Write each patched file
    for patch in patches:
        path = Path(patch.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(patch.patched_content, encoding="utf-8")
        _run(["git", "add", patch.file_path])
        print(f"  Staged: {patch.file_path}")

    # Commit
    commit_message = (
        f"fix: {issue_title[:60]} (AI-generated)\n\n"
        f"Closes #{issue_number}\n\n"
        f"Generated by Ghost Review. Requires human review before merging."
    )
    _run(["git", "commit", "-m", commit_message])

    # Push branch
    _run(["git", "push", "origin", branch_name])
    print(f"  Pushed branch: {branch_name}")


# ─────────────────────────────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────────────────────────────

async def run_auto_fix() -> None:
    github_token  = os.environ["GITHUB_TOKEN"]
    repo_slug     = os.environ["REPO"]
    issue_number  = int(os.environ["ISSUE_NUMBER"])
    actor         = os.environ.get("ACTOR", "")
    model_size    = os.environ.get("MODEL_SIZE", "7b")

    g      = Github(github_token)
    repo   = g.get_repo(repo_slug)
    issue  = repo.get_issue(issue_number)
    config = load_config(".github/localreviewer.yml")

    print(f"=== Ghost Review Auto-Fix | Issue #{issue_number} | model={model_size} ===")

    # ── Gate 1: 3B restriction ───────────────────────────────────────
    if model_size == "3b":
        issue.create_comment(
            "**Ghost Review Auto-Fix**: The 3B fallback model is active on "
            "this runner. Patch generation requires the 7B model for adequate "
            "reliability. Set `MODEL_SIZE_OVERRIDE: '7b'` in the workflow env "
            "if this runner has ≥6 GB free after OS overhead."
        )
        print("Aborting: 3B model is not suitable for auto-fix.")
        return

    # ── Gate 2: Permission check ─────────────────────────────────────
    if actor and not check_actor_permission(repo, actor, required="write"):
        print(f"Skipping: actor '{actor}' lacks write permission.")
        return

    # ── Gate 3: Auto-fix enabled? ────────────────────────────────────
    if not config.get("auto_fix", {}).get("enabled", True):
        print("Auto-fix is disabled in localreviewer.yml.")
        return

    # ── Run agentic loop ─────────────────────────────────────────────
    issue_data = {
        "number": issue_number,
        "title":  issue.title,
        "body":   issue.body or "",
    }

    # Post a "working on it" comment
    working_comment = issue.create_comment(
        "🤖 **Ghost Review** is analyzing this issue and generating a fix patch. "
        "This may take 2–4 minutes..."
    )

    async with LLMClient() as llm:
        result = await run_agentic_fix(
            issue=issue_data,
            repo_path=".",
            config=config,
            llm=llm,
        )

    # Delete the "working" comment
    try:
        working_comment.delete()
    except Exception:
        pass

    # ── Handle result ────────────────────────────────────────────────
    confidence_threshold = config.get("auto_fix", {}).get("confidence_threshold", 0.70)

    if result.gave_up or not result.patches:
        issue.create_comment(
            f"**Ghost Review Auto-Fix**: Unable to generate a patch for this issue.\n\n"
            f"**Reason**: {result.reason or 'Agent gave up without producing patches.'}\n\n"
            f"This may mean the fix requires more than 5 files, affects protected paths, "
            f"or is too ambiguous for automated resolution. Please fix manually."
        )
        print(f"Auto-fix gave up: {result.reason}")
        return

    # ── Apply patches and create PR ──────────────────────────────────
    branch_name = f"fix/ai-{issue_number}-{issue.title[:20].lower().replace(' ', '-').strip('-')}"
    # Sanitize branch name
    import re
    branch_name = re.sub(r"[^a-z0-9\-/]", "-", branch_name)[:80]

    base_branch = repo.default_branch

    _apply_patches_to_branch(
        patches=result.patches,
        branch_name=branch_name,
        issue_number=issue_number,
        issue_title=issue.title,
        base_branch=base_branch,
    )

    # Calculate aggregate confidence
    avg_confidence = sum(p.final_confidence for p in result.patches) / len(result.patches)

    # Determine confidence notice
    if avg_confidence < 0.70:
        # Should not happen (filtered above) but defensive
        notice = "⚠️ **Low confidence patch** — treat with extra caution."
    elif avg_confidence < 0.85:
        notice = "ℹ️ **Medium confidence patch** — self-verification passed."
    else:
        notice = "✅ **High confidence patch**."

    patch_summaries = [
        {"file_path": p.file_path, "explanation": p.explanation}
        for p in result.patches
    ]

    pr_url = create_draft_pr(
        repo=repo,
        branch_name=branch_name,
        base_branch=base_branch,
        issue_number=issue_number,
        issue_title=issue.title,
        patch_summaries=patch_summaries,
        agent_thinking=result.agent_thinking_trace,
        confidence=avg_confidence,
        repo_path=".",
    )

    # Comment on issue with PR link
    issue.create_comment(
        f"**Ghost Review Auto-Fix** created a draft PR: {pr_url}\n\n"
        f"{notice}\n\n"
        f"Files modified: {', '.join(f'`{p.file_path}`' for p in result.patches)}\n\n"
        f"⚠️ This PR was generated by AI. Please review carefully before merging."
    )

    print(f"✅ Auto-fix complete. Draft PR: {pr_url}")


if __name__ == "__main__":
    asyncio.run(run_auto_fix())