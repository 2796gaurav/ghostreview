"""
.github/scripts/auto_fix_v2.py

Enhanced Auto-PR Creator with ReAct pattern, reflection, and validation.

Architecture:
  - ReAct Pattern: Think → Act → Observe → Repeat
  - Reflection: Self-evaluation before generating patches
  - Validation: Verify patches compile/syntax-check before returning
  - Grammar-constrained JSON for reliable output
  - Safety gates hard-coded and non-configurable

Key improvements over v1:
  1. ReAct-style reasoning with explicit thought steps
  2. Must read file before patching (enforced)
  3. Validation pass: patches are syntax-checked
  4. Better persistence: doesn't give up easily
  5. Clearer prompts with examples
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from diff_parser import sanitize
from github import Github
from github_api import check_actor_permission, create_draft_pr
from llm_client import LLMClient, LLMError
from schemas import AGENT_ACTION_SCHEMA_V2, VERIFY_SCHEMA_V2
from prompts import AGENT_SYSTEM_PROMPT  # Fallback

# Import v2 prompts inline to avoid circular imports
AGENT_SYSTEM_PROMPT_V2 = """\
You are an expert software engineer fixing GitHub issues autonomously.
Your goal is to understand the issue, explore the codebase, and generate working patches.

WORKFLOW (ReAct Pattern):
1. THINK: Analyze the issue and plan your approach
2. ACT: Take one action (read_file, list_directory, generate_patch, finish, give_up)
3. OBSERVE: Process the result
4. REPEAT until the fix is complete

AVAILABLE ACTIONS:

1. read_file - Read a file to understand the code
   {"action": "read_file", "action_params": {"path": "path/to/file.py"}}
   
2. list_directory - List directory contents to explore structure
   {"action": "list_directory", "action_params": {"path": "src/"}}
   
3. generate_patch - Generate a complete fixed version of a file
   {"action": "generate_patch", "action_params": {
       "file_path": "path/to/file.py",
       "patched_content": "<COMPLETE_FILE_CONTENT>",
       "explanation": "What was fixed and why",
       "confidence": 0.85
   }}
   
   CRITICAL RULES FOR PATCHES:
   - You MUST read the file first before generating a patch
   - patched_content must be the COMPLETE file content, not just a diff
   - Include all original code with your fix integrated
   - Make minimal changes - only fix what's necessary
   - Ensure the code is syntactically correct
   
4. finish - Complete when all patches are ready
   {"action": "finish", "action_params": {}}
   
5. give_up - Only if the issue is truly impossible to fix
   {"action": "give_up", "action_params": {"explanation": "Why you cannot fix it"}}

STRATEGY GUIDELINES:

1. EXPLORATION PHASE (first 3-5 iterations):
   - Start by listing the root directory
   - Identify relevant files based on the issue
   - Read the main files mentioned in the issue
   - Understand the codebase structure

2. ANALYSIS PHASE:
   - Identify the root cause of the issue
   - Understand how the code currently works
   - Plan the minimal fix needed

3. PATCHING PHASE:
   - Generate complete, working patches
   - Ensure patches are syntactically valid
   - Only modify files you've read

4. VALIDATION MENTAL CHECK:
   - Before generating a patch, ask yourself:
   - "Did I read this file?" → If no, read it first
   - "Is my fix minimal and correct?" → If no, refine it
   - "Will this actually solve the issue?" → If unsure, think more

CONFIDENCE GUIDELINES:
- 0.9-1.0: Completely certain, fix tested mentally, minimal and correct
- 0.7-0.9: Reasonably confident, fix looks correct
- 0.5-0.7: Moderate confidence, some uncertainty
- Below 0.5: Don't generate patch, explore more or ask for help

OUTPUT FORMAT:
You MUST output valid JSON with these fields:
- "thinking": Your step-by-step reasoning (be detailed)
- "reflection": Self-critique of your approach (optional but recommended)
- "action": One of: read_file, list_directory, generate_patch, finish, give_up
- "action_params": Parameters for the action
- "confidence": Your confidence in this step (0.0-1.0)

Remember: ALWAYS read a file before patching it. NEVER give up without trying to read relevant files first.
"""


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
    validation_passed: bool = False


@dataclass
class FixResult:
    patches: list[PatchSpec] = field(default_factory=list)
    gave_up: bool = False
    reason: str = ""
    agent_thinking_trace: str = ""
    iterations_used: int = 0


# ─────────────────────────────────────────────────────────────────────
# Protected paths
# ─────────────────────────────────────────────────────────────────────

_HARDCODED_PROTECTED = [
    ".github/**", "*.yml", "*.yaml", "Makefile", "makefile",
    "Dockerfile", "dockerfile", "*.tf", "*.tfvars", "*.tfstate",
    ".env", ".env.*",
]


def _is_protected_path(file_path: str, config: dict[str, Any]) -> bool:
    """Check if path is protected."""
    import fnmatch

    all_patterns = list(_HARDCODED_PROTECTED)
    user_protected = config.get("auto_fix", {}).get("protected_paths", [])
    all_patterns.extend(user_protected)

    for pattern in all_patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        if fnmatch.fnmatch(Path(file_path).name, pattern.lstrip("*/")):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# File operations with better error handling
# ─────────────────────────────────────────────────────────────────────

def _read_file(repo_path: str, file_path: str, max_chars: int = 8000) -> str:
    """Read file content with error handling."""
    full_path = Path(repo_path) / file_path.lstrip("/")
    try:
        if not full_path.exists():
            return f"ERROR: File not found: {file_path}"
        if not full_path.is_file():
            return f"ERROR: Not a file: {file_path}"
        content = full_path.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[File truncated at {max_chars} chars - total size {len(content)}]"
        return content
    except Exception as e:
        return f"ERROR reading {file_path}: {str(e)}"


def _list_directory(repo_path: str, dir_path: str, max_entries: int = 100) -> str:
    """List directory contents."""
    full_path = Path(repo_path) / dir_path.lstrip("/")
    try:
        if not full_path.exists():
            return f"ERROR: Directory not found: {dir_path}"
        if not full_path.is_dir():
            return f"ERROR: Not a directory: {dir_path}"
        
        entries = []
        for p in sorted(full_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue
            prefix = "📁 " if p.is_dir() else "📄 "
            entries.append(f"{prefix}{p.name}")
            if len(entries) >= max_entries:
                entries.append(f"... and {len(list(full_path.iterdir())) - max_entries} more")
                break
        
        return f"Contents of {dir_path}/:\n" + "\n".join(entries) if entries else f"Directory {dir_path}/ is empty"
    except Exception as e:
        return f"ERROR listing {dir_path}: {str(e)}"


def _build_file_tree(repo_path: str, max_depth: int = 3, max_entries: int = 150) -> str:
    """Build a condensed file tree."""
    SKIP_DIRS = {".git", ".github", "node_modules", "__pycache__", ".venv", "venv",
                 "dist", "build", ".next", ".gradle", ".cache", "vendor", ".tox",
                 "coverage", ".mypy_cache", ".pytest_cache", ".gitignore"}
    
    lines = []
    count = 0
    repo = Path(repo_path)

    def _walk(path: Path, depth: int, prefix: str = ""):
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted([p for p in path.iterdir() if not p.name.startswith(".") and p.name not in SKIP_DIRS],
                           key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return

        for entry in entries:
            if count >= max_entries:
                lines.append(f"{prefix}...")
                return
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                count += 1
                _walk(entry, depth + 1, prefix + "  ")
            else:
                lines.append(f"{prefix}{entry.name}")
                count += 1

    _walk(repo, 0)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Patch validation - CRITICAL for reliability
# ─────────────────────────────────────────────────────────────────────

def _validate_patch(file_path: str, content: str) -> tuple[bool, str]:
    """
    Validate that a patch is syntactically correct.
    Returns (is_valid, error_message).
    """
    ext = Path(file_path).suffix.lower()
    
    # Python validation
    if ext == ".py":
        try:
            compile(content, file_path, "exec")
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError: {e.msg} at line {e.lineno}"
        except Exception as e:
            return False, f"Compilation error: {str(e)}"
    
    # JavaScript/TypeScript validation (basic)
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        # Check for basic syntax issues
        if content.count("{") != content.count("}"):
            return False, "Brace mismatch"
        if content.count("(") != content.count(")"):
            return False, "Parenthesis mismatch"
        if content.count("[") != content.count("]"):
            return False, "Bracket mismatch"
        # TODO: Use eslint or similar for better validation
        return True, ""
    
    # JSON validation
    elif ext == ".json":
        try:
            json.loads(content)
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"JSON Error: {str(e)}"
    
    # YAML validation (basic)
    elif ext in (".yml", ".yaml"):
        try:
            import yaml
            yaml.safe_load(content)
            return True, ""
        except Exception as e:
            return False, f"YAML Error: {str(e)}"
    
    # For other files, just check basic structure
    return True, ""


# ─────────────────────────────────────────────────────────────────────
# Self-verification pass
# ─────────────────────────────────────────────────────────────────────

async def _verify_patch(patch: PatchSpec, issue_body: str, llm: LLMClient) -> PatchSpec:
    """Verify patch correctness."""
    try:
        result = await asyncio.wait_for(
            llm.chat(
                system=(
                    "You are a senior code reviewer. Evaluate if a patch correctly fixes an issue. "
                    "Focus on: 1) Does it address the root cause? 2) Is the fix minimal and correct? "
                    "3) Are there any edge cases missed? 4) Is the code quality good?"
                ),
                user=(
                    f"Issue:\n{sanitize(issue_body, 1000)}\n\n"
                    f"File: {patch.file_path}\n"
                    f"Explanation: {patch.explanation}\n\n"
                    f"Patched content (first 50 lines):\n"
                    + "\n".join(patch.patched_content.splitlines()[:50])
                    + "\n\nEvaluate: Does this patch correctly fix the issue?"
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
            
    except (LLMError, asyncio.TimeoutError) as exc:
        print(f"  Verification warning: {exc}")
        patch.final_confidence = patch.patch_confidence
    
    return patch


# ─────────────────────────────────────────────────────────────────────
# Main Agentic Loop - ReAct Pattern
# ─────────────────────────────────────────────────────────────────────

async def run_agentic_fix(
    issue: dict[str, Any],
    repo_path: str,
    config: dict[str, Any],
    llm: LLMClient,
) -> FixResult:
    """
    ReAct-based agentic loop for code fixing.
    
    Pattern:
    1. THINK: Analyze issue and current state
    2. ACT: Take action (read file, list dir, generate patch)
    3. OBSERVE: Process result
    4. REPEAT until done
    """
    MAX_ITERATIONS = 15  # Increased from 12
    CONFIDENCE_THRESHOLD = config.get("auto_fix", {}).get("confidence_threshold", 0.70)
    MAX_FILES = min(config.get("auto_fix", {}).get("max_files", 5), 5)
    
    file_tree = _build_file_tree(repo_path)
    patches: list[PatchSpec] = []
    thinking_trace: list[str] = []
    conversation: list[dict] = []
    files_read: set[str] = set()
    dirs_listed: set[str] = set()
    
    # Initial context
    initial_message = f"""You are fixing a GitHub issue. Work step by step.

ISSUE #{issue['number']}: {issue['title']}

DESCRIPTION:
{sanitize(issue.get('body') or '', max_chars=3000)}

REPOSITORY STRUCTURE:
{file_tree}

INSTRUCTIONS:
1. First, explore the codebase to understand the structure
2. Read relevant files to understand the code
3. Identify the root cause of the issue
4. Generate a complete, working patch
5. The patch MUST be the COMPLETE file content, not just a diff

Start by exploring the repository structure."""

    conversation.append({"role": "user", "content": initial_message})
    
    for iteration in range(MAX_ITERATIONS):
        print(f"  🤖 Iteration {iteration + 1}/{MAX_ITERATIONS}")
        
        # Build conversation context
        user_content = "\n\n".join(
            f"[{msg['role'].upper()}]\n{msg['content'][:5000]}"  # Limit context size
            for msg in conversation[-10:]  # Keep last 10 messages
        )
        
        # Get agent's next action
        try:
            result = await asyncio.wait_for(
                llm.chat(
                    system=AGENT_SYSTEM_PROMPT_V2,
                    user=user_content,
                    schema=AGENT_ACTION_SCHEMA_V2,
                    max_tokens=3000,
                    temperature=0.2,
                ),
                timeout=180.0,  # 3 minute timeout
            )
        except asyncio.TimeoutError:
            print(f"  ⏱️ Timeout on iteration {iteration+1}")
            if patches:
                break  # Use what we have
            return FixResult(
                gave_up=True,
                reason="Agent timed out without generating patches",
                iterations_used=iteration + 1,
            )
        except LLMError as exc:
            print(f"  ❌ LLM Error: {exc}")
            if patches:
                break
            return FixResult(
                gave_up=True,
                reason=f"LLM error: {exc}",
                iterations_used=iteration + 1,
            )
        
        # Extract action details
        action = result.get("action", "give_up")
        params = result.get("action_params") or {}
        thinking = result.get("thinking", "")
        reflection = result.get("reflection", "")
        
        if thinking:
            thinking_trace.append(f"[Step {iteration+1}] {thinking}")
        if reflection:
            thinking_trace.append(f"[Reflection {iteration+1}] {reflection}")
        
        conversation.append({
            "role": "assistant",
            "content": json.dumps(result, indent=2),
        })
        
        # Execute action
        if action == "read_file":
            file_path = params.get("path", "").lstrip("/")
            content = _read_file(repo_path, file_path)
            files_read.add(file_path)
            action_result = f"Contents of {file_path}:\n```\n{content}\n```"
            
        elif action == "list_directory":
            dir_path = params.get("path", ".").lstrip("/")
            content = _list_directory(repo_path, dir_path)
            dirs_listed.add(dir_path)
            action_result = content
            
        elif action == "generate_patch":
            fp = params.get("file_path", "").lstrip("/")
            patched = params.get("patched_content", "")
            explanation = params.get("explanation", "")
            patch_confidence = float(params.get("confidence") or 0.5)
            
            # Validate patch requirements
            errors = []
            if not fp:
                errors.append("file_path is required")
            elif _is_protected_path(fp, config):
                errors.append(f"{fp} is a protected path")
            elif len(patches) >= MAX_FILES:
                errors.append(f"Maximum {MAX_FILES} files already staged")
            elif not patched:
                errors.append("patched_content is required")
            elif fp not in files_read:
                errors.append(f"You must read {fp} before patching it")
            
            if errors:
                action_result = f"ERROR: Cannot generate patch:\n" + "\n".join(f"- {e}" for e in errors)
            else:
                # Validate syntax
                is_valid, error_msg = _validate_patch(fp, patched)
                if not is_valid:
                    action_result = f"ERROR: Patch validation failed: {error_msg}\nPlease fix and regenerate."
                else:
                    patch = PatchSpec(
                        file_path=fp,
                        patched_content=patched,
                        explanation=explanation,
                        patch_confidence=patch_confidence,
                        final_confidence=patch_confidence,
                        validation_passed=True,
                    )
                    patches.append(patch)
                    action_result = f"✅ Patch staged for {fp} (confidence={patch_confidence:.2f}, validated)"
                    print(f"    {action_result}")
        
        elif action == "finish":
            print(f"  ✅ Agent finished after {iteration + 1} iterations")
            break
            
        elif action == "give_up":
            explanation = params.get("explanation", "Agent gave up without explanation")
            print(f"  🛑 Agent gave up: {explanation}")
            return FixResult(
                gave_up=True,
                reason=explanation,
                agent_thinking_trace="\n\n".join(thinking_trace),
                iterations_used=iteration + 1,
            )
        
        else:
            action_result = f"ERROR: Unknown action '{action}'"
        
        conversation.append({
            "role": "user",
            "content": f"[RESULT]\n{action_result}\n\nContinue with your next step.",
        })
    
    else:
        # Max iterations reached
        if not patches:
            return FixResult(
                gave_up=True,
                reason=f"Max iterations ({MAX_ITERATIONS}) reached without generating patches",
                agent_thinking_trace="\n\n".join(thinking_trace),
                iterations_used=MAX_ITERATIONS,
            )
    
    # No patches generated
    if not patches:
        return FixResult(
            gave_up=True,
            reason="Agent completed without generating any patches",
            agent_thinking_trace="\n\n".join(thinking_trace),
            iterations_used=len(thinking_trace),
        )
    
    # Self-verification for low-confidence patches
    print("  🔍 Running self-verification...")
    issue_body = issue.get("body") or ""
    verified_patches: list[PatchSpec] = []
    
    for patch in patches:
        if patch.patch_confidence < 0.85:
            patch = await _verify_patch(patch, issue_body, llm)
        verified_patches.append(patch)
    
    # Filter by confidence threshold
    final_patches = [p for p in verified_patches if p.final_confidence >= CONFIDENCE_THRESHOLD]
    rejected = [p for p in verified_patches if p.final_confidence < CONFIDENCE_THRESHOLD]
    
    if rejected:
        for p in rejected:
            print(f"  ❌ Rejected {p.file_path}: confidence={p.final_confidence:.2f}")
    
    return FixResult(
        patches=final_patches,
        gave_up=len(final_patches) == 0,
        reason=f"{len(rejected)} patch(es) rejected by verification" if rejected and not final_patches else "",
        agent_thinking_trace="\n\n".join(thinking_trace),
        iterations_used=len(thinking_trace),
    )


# ─────────────────────────────────────────────────────────────────────
# Git operations
# ─────────────────────────────────────────────────────────────────────

def _apply_patches_to_branch(
    patches: list[PatchSpec],
    branch_name: str,
    issue_number: int,
    issue_title: str,
    base_branch: str,
) -> None:
    """Create branch and commit patches."""
    def _run(cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Git failed: {' '.join(cmd)}\n{result.stderr[:500]}")
    
    _run(["git", "checkout", "-b", branch_name])
    
    for patch in patches:
        path = Path(patch.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(patch.patched_content, encoding="utf-8")
        _run(["git", "add", patch.file_path])
        print(f"  📄 Staged: {patch.file_path}")
    
    commit_msg = (
        f"fix: {issue_title[:60]} (AI-generated)\n\n"
        f"Closes #{issue_number}\n\n"
        f"Generated by Ghost Review Auto-Fix."
    )
    _run(["git", "commit", "-m", commit_msg])
    _run(["git", "push", "origin", branch_name])
    print(f"  🚀 Pushed: {branch_name}")


# ─────────────────────────────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────────────────────────────

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
    
    print(f"=== Ghost Review Auto-Fix v2 | Issue #{issue_number} | model={model_size} ===")
    
    # Gate 1: Model size check
    if model_size == "3b":
        issue.create_comment(
            "⚠️ **Auto-Fix Unavailable**: The 3B model is not suitable for code generation. "
            "Auto-Fix requires the 7B model for reliable patch generation."
        )
        print("Abort: 3B model insufficient")
        return
    
    # Gate 2: Permission check
    if actor and not check_actor_permission(repo, actor, required="write"):
        print(f"Skip: {actor} lacks write permission")
        return
    
    # Gate 3: Feature enabled
    if not config.get("auto_fix", {}).get("enabled", True):
        print("Skip: Auto-fix disabled in config")
        return
    
    issue_data = {
        "number": issue_number,
        "title": issue.title,
        "body": issue.body or "",
    }
    
    # Working comment
    working = issue.create_comment(
        "🤖 **Ghost Review Auto-Fix** is analyzing this issue and generating patches...\n"
        "⏱️ This may take 3-5 minutes."
    )
    
    try:
        async with LLMClient() as llm:
            result = await run_agentic_fix(
                issue=issue_data,
                repo_path=".",
                config=config,
                llm=llm,
            )
    finally:
        try:
            working.delete()
        except Exception:
            pass
    
    # Handle result
    if result.gave_up or not result.patches:
        issue.create_comment(
            f"❌ **Auto-Fix Could Not Generate Patch**\n\n"
            f"**Reason**: {result.reason or 'Unable to generate suitable patch'}\n\n"
            f"**Details**:\n"
            f"- Iterations: {result.iterations_used}\n"
            f"- Files explored: {len(result.agent_thinking_trace.split('read_file')) - 1}\n\n"
            f"**Agent Trace**:\n"
            f"<details><summary>Click to expand</summary>\n\n"
            f"{result.agent_thinking_trace or 'No trace'}\n"
            f"</details>\n\n"
            f"The issue may need manual fixing or more detailed description."
        )
        print(f"Failed: {result.reason}")
        return
    
    # Create branch and PR
    safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in issue.title[:30])
    branch_name = f"fix/ai-{issue_number}-{safe_title}"
    
    _apply_patches_to_branch(
        patches=result.patches,
        branch_name=branch_name,
        issue_number=issue_number,
        issue_title=issue.title,
        base_branch=repo.default_branch,
    )
    
    avg_confidence = sum(p.final_confidence for p in result.patches) / len(result.patches)
    
    if avg_confidence < 0.70:
        notice = "⚠️ Low confidence — careful review required"
    elif avg_confidence < 0.85:
        notice = "ℹ️ Medium confidence — standard review recommended"
    else:
        notice = "✅ High confidence"
    
    patch_summaries = [
        {"file_path": p.file_path, "explanation": p.explanation}
        for p in result.patches
    ]
    
    pr_url = create_draft_pr(
        repo=repo,
        branch_name=branch_name,
        base_branch=repo.default_branch,
        issue_number=issue_number,
        issue_title=issue.title,
        patch_summaries=patch_summaries,
        agent_thinking=result.agent_thinking_trace,
        confidence=avg_confidence,
        repo_path=".",
    )
    
    issue.create_comment(
        f"✅ **Auto-Fix Generated Draft PR**: {pr_url}\n\n"
        f"{notice} (confidence: {avg_confidence*100:.0f}%)\n\n"
        f"**Modified Files**:\n"
        + "\n".join(f"- `{p.file_path}`" for p in result.patches)
        + "\n\n⚠️ Please review carefully before merging."
    )
    
    print(f"✅ Success: {pr_url}")


if __name__ == "__main__":
    asyncio.run(run_auto_fix())
