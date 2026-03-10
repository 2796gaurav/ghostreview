"""
.github/scripts/auto_fix.py

Enhanced Auto-PR Creator with ReAct pattern, validation, and reflection.
Integrates best practices from autonomous coding agents research.

Architecture:
  - ReAct Pattern: Think → Act → Observe → Repeat
  - Grammar-constrained JSON for reliable structured output
  - Syntax validation before accepting patches
  - Self-verification for quality assurance
  - Hard-coded safety gates
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
    iterations_used: int = 0


# Protected paths - cannot be modified by Auto-Fix
_PROTECTED_PATHS = [
    ".github/**", "*.yml", "*.yaml", "Makefile", "makefile",
    "Dockerfile", "dockerfile", "*.tf", "*.tfvars", "*.tfstate",
    ".env", ".env.*",
]


def _is_protected(file_path: str, config: dict) -> bool:
    """Check if file path is protected."""
    import fnmatch
    patterns = _PROTECTED_PATHS + config.get("auto_fix", {}).get("protected_paths", [])
    for pattern in patterns:
        if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(Path(file_path).name, pattern.lstrip("*/")):
            return True
    return False


def _read_file(repo_path: str, file_path: str, max_chars: int = 8000) -> str:
    """Read file with error handling."""
    try:
        full = Path(repo_path) / file_path.lstrip("/")
        if not full.exists():
            return f"ERROR: File not found: {file_path}"
        content = full.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n... [truncated, total {len(content)} chars]"
        return content
    except Exception as e:
        return f"ERROR reading {file_path}: {e}"


def _list_dir(repo_path: str, dir_path: str, max_entries: int = 100) -> str:
    """List directory contents."""
    try:
        full = Path(repo_path) / dir_path.lstrip("/")
        if not full.exists():
            return f"ERROR: Directory not found: {dir_path}"
        entries = sorted(
            [p for p in full.iterdir() if not p.name.startswith(".")],
            key=lambda p: (p.is_file(), p.name.lower())
        )
        lines = []
        for p in entries[:max_entries]:
            icon = "📁" if p.is_dir() else "📄"
            lines.append(f"{icon} {p.name}")
        if len(entries) > max_entries:
            lines.append(f"... and {len(entries) - max_entries} more")
        return f"Contents of {dir_path}:\n" + "\n".join(lines) if lines else f"Directory {dir_path} is empty"
    except Exception as e:
        return f"ERROR listing {dir_path}: {e}"


def _build_tree(repo_path: str, max_depth: int = 3) -> str:
    """Build file tree for initial context."""
    SKIP = {".git", ".github", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", ".gradle", ".cache", ".tox", ".mypy_cache"}
    lines = []
    count = 0
    
    def walk(path: Path, depth: int, prefix: str = ""):
        nonlocal count
        if depth > max_depth or count > 150:
            return
        try:
            entries = sorted([p for p in path.iterdir() if p.name not in SKIP and not p.name.startswith(".")],
                           key=lambda p: (p.is_file(), p.name.lower()))
        except:
            return
        for p in entries:
            if count > 150:
                lines.append(f"{prefix}...")
                return
            if p.is_dir():
                lines.append(f"{prefix}{p.name}/")
                count += 1
                walk(p, depth + 1, prefix + "  ")
            else:
                lines.append(f"{prefix}{p.name}")
                count += 1
    
    walk(Path(repo_path), 0)
    return "\n".join(lines)


def _validate_syntax(file_path: str, content: str) -> tuple[bool, str]:
    """Validate file syntax before accepting patch."""
    ext = Path(file_path).suffix.lower()
    
    if ext == ".py":
        try:
            compile(content, file_path, "exec")
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError line {e.lineno}: {e.msg}"
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        # Basic checks
        if content.count("{") != content.count("}"):
            return False, "Brace mismatch"
        if content.count("(") != content.count(")"):
            return False, "Parenthesis mismatch"
        return True, ""
    elif ext == ".json":
        try:
            json.loads(content)
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"JSON error: {e}"
    elif ext in (".yml", ".yaml"):
        try:
            import yaml
            yaml.safe_load(content)
            return True, ""
        except Exception as e:
            return False, f"YAML error: {e}"
    return True, ""


async def _verify_patch(patch: PatchSpec, issue_body: str, llm: LLMClient) -> PatchSpec:
    """Verify patch correctness."""
    try:
        result = await asyncio.wait_for(
            llm.chat(
                system="You are a code reviewer. Assess if a patch correctly fixes an issue.",
                user=(
                    f"Issue: {sanitize(issue_body, 800)}\n\n"
                    f"File: {patch.file_path}\n"
                    f"Explanation: {patch.explanation}\n\n"
                    f"Patched content (first 50 lines):\n"
                    + "\n".join(patch.patched_content.splitlines()[:50])
                    + "\n\nDoes this patch correctly fix the issue?"
                ),
                schema=VERIFY_SCHEMA,
                max_tokens=256,
                temperature=0.1,
            ),
            timeout=60.0,
        )
        if result["correct"]:
            patch.final_confidence = (patch.patch_confidence + result["verified_confidence"]) / 2
        else:
            patch.final_confidence = patch.patch_confidence * 0.4
            patch.verification_concern = result["concern"]
    except Exception as exc:
        print(f"  Verification warning: {exc}")
        patch.final_confidence = patch.patch_confidence
    return patch


async def run_agentic_fix(issue: dict, repo_path: str, config: dict, llm: LLMClient) -> FixResult:
    """
    ReAct-based agentic loop.
    """
    MAX_ITER = 15
    CONFIDENCE_THRESHOLD = config.get("auto_fix", {}).get("confidence_threshold", 0.70)
    MAX_FILES = min(config.get("auto_fix", {}).get("max_files", 5), 5)
    
    file_tree = _build_tree(repo_path)
    patches: list[PatchSpec] = []
    thinking_trace: list[str] = []
    conversation: list[dict] = []
    files_read: set[str] = set()
    
    initial = f"""Fix this GitHub issue. Work step by step.

ISSUE #{issue['number']}: {issue['title']}

DESCRIPTION:
{sanitize(issue.get('body') or '', max_chars=3000)}

REPOSITORY:
{file_tree}

INSTRUCTIONS:
1. Explore structure with list_directory
2. Read relevant files with read_file
3. Generate complete patches with generate_patch
4. Patches must be FULL file content, not diffs
5. You MUST read a file before patching it

Start exploring."""

    conversation.append({"role": "user", "content": initial})
    
    for iteration in range(MAX_ITER):
        print(f"  Iteration {iteration + 1}/{MAX_ITER}")
        
        user_content = "\n\n".join(f"[{m['role'].upper()}]\n{m['content'][:4000]}" for m in conversation[-8:])
        
        try:
            result = await asyncio.wait_for(
                llm.chat(
                    system=AGENT_SYSTEM_PROMPT,
                    user=user_content,
                    schema=AGENT_ACTION_SCHEMA,
                    max_tokens=3000,
                    temperature=0.2,
                ),
                timeout=180.0,
            )
        except asyncio.TimeoutError:
            print(f"  Timeout on iteration {iteration+1}")
            if patches:
                break
            return FixResult(gave_up=True, reason="Timeout", iterations_used=iteration+1)
        except LLMError as exc:
            print(f"  LLM Error: {exc}")
            if patches:
                break
            return FixResult(gave_up=True, reason=f"LLM error: {exc}", iterations_used=iteration+1)
        
        action = result.get("action", "give_up")
        params = result.get("action_params", {})
        thinking = result.get("thinking", "")
        
        if thinking:
            thinking_trace.append(f"[Step {iteration+1}] {thinking}")
        
        conversation.append({"role": "assistant", "content": json.dumps(result)})
        
        # Execute action
        if action == "read_file":
            path = params.get("path", "").lstrip("/")
            content = _read_file(repo_path, path)
            files_read.add(path)
            action_result = f"Contents of {path}:\n```\n{content}\n```"
            
        elif action == "list_directory":
            path = params.get("path", ".").lstrip("/")
            action_result = _list_dir(repo_path, path)
            
        elif action == "generate_patch":
            fp = params.get("file_path", "").lstrip("/")
            patched = params.get("patched_content", "")
            explanation = params.get("explanation", "")
            confidence = float(params.get("confidence") or 0.5)
            
            errors = []
            if not fp:
                errors.append("file_path required")
            elif _is_protected(fp, config):
                errors.append(f"{fp} is protected")
            elif len(patches) >= MAX_FILES:
                errors.append(f"Max {MAX_FILES} files")
            elif not patched:
                errors.append("patched_content required")
            elif fp not in files_read:
                errors.append(f"Must read {fp} first")
            
            if errors:
                action_result = "ERROR:\n" + "\n".join(f"- {e}" for e in errors)
            else:
                valid, error = _validate_syntax(fp, patched)
                if not valid:
                    action_result = f"ERROR: Validation failed: {error}\nRegenerate with correct syntax."
                else:
                    patch = PatchSpec(
                        file_path=fp,
                        patched_content=patched,
                        explanation=explanation,
                        patch_confidence=confidence,
                        final_confidence=confidence,
                    )
                    patches.append(patch)
                    action_result = f"✅ Patch staged for {fp} (confidence={confidence:.2f})"
                    print(f"    {action_result}")
        
        elif action == "finish":
            print(f"  Finished after {iteration + 1} iterations")
            break
            
        elif action == "give_up":
            reason = params.get("explanation", "Gave up without explanation")
            print(f"  Gave up: {reason}")
            return FixResult(gave_up=True, reason=reason, agent_thinking_trace="\n".join(thinking_trace), iterations_used=iteration+1)
        
        else:
            action_result = f"ERROR: Unknown action '{action}'"
        
        conversation.append({"role": "user", "content": f"[RESULT]\n{action_result}\n\nContinue."})
    
    else:
        if not patches:
            return FixResult(gave_up=True, reason=f"Max iterations ({MAX_ITER}) reached", iterations_used=MAX_ITER)
    
    if not patches:
        return FixResult(gave_up=True, reason="No patches generated", iterations_used=len(thinking_trace))
    
    # Verify low-confidence patches
    print("  Verifying patches...")
    issue_body = issue.get("body") or ""
    verified = []
    for patch in patches:
        if patch.patch_confidence < 0.85:
            patch = await _verify_patch(patch, issue_body, llm)
        verified.append(patch)
    
    final_patches = [p for p in verified if p.final_confidence >= CONFIDENCE_THRESHOLD]
    rejected = [p for p in verified if p.final_confidence < CONFIDENCE_THRESHOLD]
    
    if rejected:
        for p in rejected:
            print(f"  Rejected {p.file_path}: confidence={p.final_confidence:.2f}")
    
    return FixResult(
        patches=final_patches,
        gave_up=len(final_patches) == 0,
        reason=f"{len(rejected)} rejected" if rejected and not final_patches else "",
        agent_thinking_trace="\n".join(thinking_trace),
        iterations_used=len(thinking_trace),
    )


def _apply_patches(patches: list, branch_name: str, issue_num: int, issue_title: str, base: str) -> None:
    """Create branch and commit patches."""
    def run(cmd: list):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Git failed: {' '.join(cmd)}\n{result.stderr[:500]}")
    
    run(["git", "checkout", "-b", branch_name])
    
    for patch in patches:
        path = Path(patch.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(patch.patched_content, encoding="utf-8")
        run(["git", "add", patch.file_path])
        print(f"  Staged: {patch.file_path}")
    
    msg = f"fix: {issue_title[:60]} (AI-generated)\n\nCloses #{issue_num}\n\nGenerated by Ghost Review."
    run(["git", "commit", "-m", msg])
    run(["git", "push", "origin", branch_name])
    print(f"  Pushed: {branch_name}")


async def run_auto_fix() -> None:
    github_token = os.environ["GITHUB_TOKEN"]
    repo_slug = os.environ["REPO"]
    issue_number = int(os.environ["ISSUE_NUMBER"])
    actor = os.environ.get("ACTOR", "")
    model_size = os.environ.get("MODEL_SIZE", "7b")
    
    g = Github(github_token)
    repo = g.get_repo(repo_slug)
    issue = repo.get_issue(issue_number)
    config = load_config(".github/localreviewer.yml")
    
    print(f"=== Ghost Review Auto-Fix | Issue #{issue_number} | model={model_size} ===")
    
    if model_size == "3b":
        issue.create_comment("⚠️ Auto-Fix requires 7B model. 3B is insufficient.")
        print("Abort: 3B model")
        return
    
    if actor and not check_actor_permission(repo, actor, required="write"):
        print(f"Skip: {actor} lacks permission")
        return
    
    if not config.get("auto_fix", {}).get("enabled", True):
        print("Skip: Disabled")
        return
    
    issue_data = {"number": issue_number, "title": issue.title, "body": issue.body or ""}
    
    working = issue.create_comment("🤖 Auto-Fix analyzing... (3-5 minutes)")
    
    try:
        async with LLMClient() as llm:
            result = await run_agentic_fix(issue_data, ".", config, llm)
    finally:
        try:
            working.delete()
        except:
            pass
    
    if result.gave_up or not result.patches:
        issue.create_comment(
            f"❌ Auto-Fix failed\n\n**Reason**: {result.reason or 'Unable to generate patch'}\n\n"
            f"**Trace**:\n<details><summary>Click to expand</summary>\n\n{result.agent_thinking_trace}\n</details>\n\n"
            "Please fix manually."
        )
        print(f"Failed: {result.reason}")
        return
    
    safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in issue.title[:30])
    branch_name = f"fix/ai-{issue_number}-{safe_title}"
    
    _apply_patches(result.patches, branch_name, issue_number, issue.title, repo.default_branch)
    
    avg_conf = sum(p.final_confidence for p in result.patches) / len(result.patches)
    notice = "✅ High confidence" if avg_conf >= 0.85 else "ℹ️ Medium confidence" if avg_conf >= 0.70 else "⚠️ Low confidence"
    
    patch_summaries = [{"file_path": p.file_path, "explanation": p.explanation} for p in result.patches]
    
    pr_url = create_draft_pr(
        repo=repo, branch_name=branch_name, base_branch=repo.default_branch,
        issue_number=issue_number, issue_title=issue.title,
        patch_summaries=patch_summaries, agent_thinking=result.agent_thinking_trace,
        confidence=avg_conf, repo_path=".",
    )
    
    issue.create_comment(
        f"✅ Draft PR: {pr_url}\n\n{notice} ({avg_conf*100:.0f}%)\n\n"
        f"**Files**:\n" + "\n".join(f"- `{p.file_path}`" for p in result.patches)
        + "\n\n⚠️ Review carefully before merging."
    )
    
    print(f"✅ Success: {pr_url}")


if __name__ == "__main__":
    asyncio.run(run_auto_fix())
