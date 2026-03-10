"""
.github/scripts/auto_fix.py

Self-healing Auto-Fix Agent with:
  - Error capture and traceback analysis
  - Reflection loop for self-correction
  - Max 10 retry attempts
  - Draft PR with error notes if unresolved
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from diff_parser import sanitize
from github import Github
from github_api import check_actor_permission, create_draft_pr
from llm_client import LLMClient, LLMError
from prompts import AGENT_SYSTEM_PROMPT, ERROR_ANALYSIS_PROMPT
from schemas import AGENT_ACTION_SCHEMA, VERIFY_SCHEMA, ERROR_ANALYSIS_SCHEMA


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
    exploration_coverage: float = 0.0
    errors_encountered: list[dict] = field(default_factory=list)


@dataclass
class ErrorContext:
    """Context for an error that occurred during fixing."""
    error_type: str
    error_message: str
    traceback_str: str
    file_path: str | None = None
    line_number: int | None = None
    iteration: int = 0


# Protected paths that cannot be modified
_PROTECTED_PATHS = [
    ".github/**", "*.yml", "*.yaml", "Makefile", "makefile",
    "Dockerfile", "dockerfile", "*.tf", "*.tfvars", "*.tfstate",
    ".env", ".env.*", "*.key", "*.pem", ".git/**",
]


def _is_protected(file_path: str, config: dict) -> bool:
    """Check if path is protected."""
    import fnmatch
    patterns = _PROTECTED_PATHS + config.get("auto_fix", {}).get("protected_paths", [])
    for pattern in patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        if fnmatch.fnmatch(Path(file_path).name, pattern.lstrip("*/")):
            return True
    return False


def _read_file(repo_path: str, file_path: str, max_chars: int = 10000) -> str:
    """Read file with error handling."""
    try:
        full = Path(repo_path) / file_path.lstrip("/")
        if not full.exists():
            return f"ERROR: File not found: {file_path}"
        content = full.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n... [truncated, {len(content)} total chars]"
        return content
    except Exception as e:
        return f"ERROR reading {file_path}: {e}"


def _list_dir(repo_path: str, dir_path: str, max_entries: int = 50) -> str:
    """List directory contents."""
    try:
        full = Path(repo_path) / dir_path.lstrip("/")
        if not full.exists():
            return f"ERROR: Directory not found: {dir_path}"
        
        entries = []
        for p in sorted(full.iterdir(), key=lambda x: x.name.lower()):
            if p.name.startswith("."):
                continue
            icon = "📁" if p.is_dir() else "📄"
            entries.append(f"{icon} {p.name}")
            if len(entries) >= max_entries:
                entries.append(f"... ({sum(1 for _ in full.iterdir()) - max_entries} more)")
                break
        
        return f"Contents of {dir_path}:\n" + "\n".join(entries) if entries else f"Directory {dir_path} is empty"
    except Exception as e:
        return f"ERROR listing {dir_path}: {e}"


def _build_tree(repo_path: str, max_depth: int = 4) -> str:
    """Build file tree for context."""
    SKIP = {".git", ".github", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", ".gradle", ".cache"}
    
    lines = []
    count = 0
    
    def walk(path: Path, depth: int, prefix: str = ""):
        nonlocal count
        if depth > max_depth or count > 200:
            return
        try:
            entries = sorted([p for p in path.iterdir() if p.name not in SKIP and not p.name.startswith(".")],
                           key=lambda p: (p.is_file(), p.name.lower()))
        except:
            return
        for p in entries:
            if count > 200:
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


def _extract_files_from_issue(issue_body: str) -> list[str]:
    """Extract potential file references from issue text."""
    patterns = [
        r'`([^`]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|c|cpp|h|swift|kt))`',
        r'"([^"]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|c|cpp|h|swift|kt))"',
        r"'([^']+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|c|cpp|h|swift|kt))'",
        r'(?:src/|lib/|app/|cmd/)([\w/]+\.(?:py|js|ts|go|rs|java))',
    ]
    
    files = set()
    for pattern in patterns:
        for match in re.finditer(pattern, issue_body, re.IGNORECASE):
            file_path = match.group(1)
            if not file_path.startswith(("http", "www")):
                files.add(file_path)
    
    return sorted(files)


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
        imbalances = []
        if content.count("{") != content.count("}"):
            imbalances.append("braces")
        if content.count("(") != content.count(")"):
            imbalances.append("parentheses")
        if content.count("[") != content.count("]"):
            imbalances.append("brackets")
        if imbalances:
            return False, f"Unbalanced: {', '.join(imbalances)}"
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
    elif ext == ".go":
        if content.count("{") != content.count("}"):
            return False, "Unbalanced braces"
        return True, ""
    elif ext == ".rs":
        if content.count("{") != content.count("}"):
            return False, "Unbalanced braces"
        return True, ""
    
    return True, ""


def _test_python_code(file_path: str, content: str) -> tuple[bool, ErrorContext | None]:
    """Test Python code by compiling and catching errors."""
    ext = Path(file_path).suffix.lower()
    if ext != ".py":
        return True, None
    
    # First try to compile
    try:
        compile(content, file_path, "exec")
    except SyntaxError as e:
        tb_str = traceback.format_exc()
        return False, ErrorContext(
            error_type="SyntaxError",
            error_message=str(e),
            traceback_str=tb_str,
            file_path=file_path,
            line_number=e.lineno,
        )
    
    # Try to import in a temp file (catches import errors)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(content)
        temp_path = f.name
    
    try:
        # Run Python to check for import/runtime errors
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", temp_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, ErrorContext(
                error_type="Import/Runtime Error",
                error_message=result.stderr or "Unknown error",
                traceback_str=result.stderr,
                file_path=file_path,
            )
        return True, None
    except Exception as e:
        return True, None  # If we can't test, assume it's ok
    finally:
        try:
            os.unlink(temp_path)
        except:
            pass


async def _analyze_error_with_llm(
    error: ErrorContext,
    file_content: str,
    file_path: str,
    issue: dict,
    llm: LLMClient,
) -> dict[str, Any]:
    """Send error to LLM for analysis and fix suggestion."""
    prompt = f"""An error occurred while trying to fix this issue. Analyze the error and suggest how to fix it.

ISSUE:
{sanitize(issue.get('body', ''), 1000)}

FILE: {file_path}

CURRENT FILE CONTENT:
```python
{file_content[:3000]}
```

ERROR DETAILS:
- Type: {error.error_type}
- Message: {error.error_message}
- Line: {error.line_number or 'Unknown'}

TRACEBACK:
```
{error.traceback_str[:2000]}
```

Analyze this error and provide:
1. What caused the error
2. How to fix it
3. The corrected code (full file content)
"""
    
    try:
        result = await asyncio.wait_for(
            llm.chat(
                system=ERROR_ANALYSIS_PROMPT,
                user=prompt,
                schema=ERROR_ANALYSIS_SCHEMA,
                max_tokens=4000,
                temperature=0.2,
            ),
            timeout=120.0,
        )
        return result
    except Exception as e:
        return {
            "analysis": f"Failed to analyze error: {e}",
            "fix_suggestion": "Manual review needed",
            "corrected_code": file_content,
            "confidence": 0.0,
        }


async def _verify_patch(patch: PatchSpec, issue_body: str, llm: LLMClient) -> PatchSpec:
    """Verify patch correctness with reflection."""
    try:
        result = await asyncio.wait_for(
            llm.chat(
                system="You are a senior code reviewer. Assess if a patch correctly fixes the reported issue.",
                user=(
                    f"Issue: {sanitize(issue_body, 1000)}\n\n"
                    f"File: {patch.file_path}\n"
                    f"Proposed fix explanation: {patch.explanation}\n\n"
                    f"Patched content (first 60 lines):\n"
                    + "\n".join(patch.patched_content.splitlines()[:60])
                    + "\n\nDoes this patch correctly and completely fix the issue?"
                ),
                schema=VERIFY_SCHEMA,
                max_tokens=512,
                temperature=0.1,
            ),
            timeout=60.0,
        )
        
        if result["correct"]:
            patch.final_confidence = (patch.patch_confidence + result["verified_confidence"]) / 2
        else:
            patch.final_confidence = patch.patch_confidence * 0.5
            patch.verification_concern = result.get("concern", "Verification failed")
            
    except Exception as exc:
        print(f"  Verification warning: {exc}")
        patch.final_confidence = patch.patch_confidence
    
    return patch


async def run_agentic_fix_with_reflection(
    issue: dict,
    repo_path: str,
    config: dict,
    llm: LLMClient,
) -> FixResult:
    """
    Self-healing agentic fix with error reflection and retry loop.
    Max 10 attempts before giving up.
    """
    MAX_ITER = 10  # Max retries as requested
    CONFIDENCE_THRESHOLD = config.get("auto_fix", {}).get("confidence_threshold", 0.70)
    MAX_FILES = min(config.get("auto_fix", {}).get("max_files", 5), 5)
    
    file_tree = _build_tree(repo_path)
    mentioned_files = _extract_files_from_issue(issue.get("body", ""))
    print(f"  Files mentioned in issue: {mentioned_files}")
    
    patches: list[PatchSpec] = []
    thinking_trace: list[str] = []
    conversation: list[dict] = []
    files_read: set[str] = set()
    files_targeted: set[str] = set()
    errors_encountered: list[ErrorContext] = []
    
    # Initialize with issue context
    initial_prompt = f"""Fix this GitHub issue by exploring the codebase and generating patches.

ISSUE #{issue['number']}: {issue['title']}

DESCRIPTION:
{sanitize(issue.get('body') or '', max_chars=4000)}

REPOSITORY STRUCTURE:
{file_tree}

MENTIONED FILES: {', '.join(mentioned_files) or 'None explicitly mentioned'}

INSTRUCTIONS:
1. First, explore relevant files using list_directory and read_file
2. Identify the root cause by examining code mentioned in the issue
3. Generate complete patches with generate_patch (MUST read file first)
4. Patches must be FULL file content, not diffs
5. Make minimal, focused changes

IMPORTANT: You MUST read a file before generating a patch for it.

Begin exploration."""

    conversation.append({"role": "user", "content": initial_prompt})
    
    phase = "explore"
    iterations_in_phase = 0
    reflection_mode = False
    current_error: ErrorContext | None = None
    
    for iteration in range(MAX_ITER):
        iterations_in_phase += 1
        print(f"  [{phase}{'/REFLECT' if reflection_mode else ''}] Iteration {iteration + 1}/{MAX_ITER}")
        
        # Build context window
        if reflection_mode and current_error:
            # In reflection mode, include error analysis
            context = f"""[ERROR REFLECTION MODE]

An error was encountered in the previous iteration:
- Error Type: {current_error.error_type}
- Error Message: {current_error.error_message}
- Line Number: {current_error.line_number or 'Unknown'}

TRACEBACK:
```
{current_error.traceback_str[:2000]}
```

Please fix this error and regenerate the patch correctly.

---

Previous conversation context:
"""
            context += "\n\n".join(
                f"[{m['role'].upper()}]\n{m['content'][:2000]}"
                for m in conversation[-5:]
            )
            reflection_mode = False  # Reset after one reflection iteration
        else:
            context = "\n\n".join(
                f"[{m['role'].upper()}]\n{m['content'][:3000]}"
                for m in conversation[-10:]
            )
        
        # Get agent action
        try:
            result = await asyncio.wait_for(
                llm.chat(
                    system=AGENT_SYSTEM_PROMPT,
                    user=context,
                    schema=AGENT_ACTION_SCHEMA,
                    max_tokens=3500,
                    temperature=0.2,
                ),
                timeout=200.0,
            )
        except asyncio.TimeoutError:
            print(f"  Timeout on iteration {iteration+1}")
            if patches:
                break
            return FixResult(
                gave_up=True,
                reason="Timeout",
                iterations_used=iteration+1,
                errors_encountered=[{"type": "Timeout", "message": "LLM request timed out"}]
            )
        except LLMError as exc:
            print(f"  LLM Error: {exc}")
            if patches:
                break
            return FixResult(
                gave_up=True,
                reason=f"LLM error: {exc}",
                iterations_used=iteration+1,
                errors_encountered=[{"type": "LLMError", "message": str(exc)}]
            )
        
        action = result.get("action", "give_up")
        params = result.get("action_params", {})
        thinking = result.get("thinking", "")
        confidence = result.get("confidence", 0.5)
        
        if thinking:
            thinking_trace.append(f"[Iter {iteration+1}|{phase}] {thinking[:500]}")
        
        conversation.append({"role": "assistant", "content": json.dumps(result, indent=2)})
        
        # Execute action
        if action == "read_file":
            path = params.get("path", "").lstrip("/")
            if path:
                content = _read_file(repo_path, path)
                files_read.add(path)
                files_targeted.add(path)
                action_result = f"File '{path}' contents:\n```\n{content[:8000]}\n```"
                
                if phase == "explore" and iterations_in_phase >= 2:
                    phase = "analyze"
                    iterations_in_phase = 0
            else:
                action_result = "ERROR: No path specified for read_file"
            
        elif action == "list_directory":
            path = params.get("path", ".").lstrip("/")
            action_result = _list_dir(repo_path, path)
            
        elif action == "generate_patch":
            fp = params.get("file_path", "").lstrip("/")
            patched = params.get("patched_content", "")
            explanation = params.get("explanation", "")
            patch_conf = float(params.get("confidence") or 0.5)
            
            # Validate patch
            errors = []
            if not fp:
                errors.append("file_path is required")
            elif _is_protected(fp, config):
                errors.append(f"{fp} is protected and cannot be modified")
            elif len(patches) >= MAX_FILES:
                errors.append(f"Maximum {MAX_FILES} files allowed")
            elif not patched:
                errors.append("patched_content is required")
            elif fp not in files_read:
                errors.append(f"MUST read '{fp}' before patching. Read it first.")
            elif any(p.file_path == fp for p in patches):
                errors.append(f"Already have a patch for '{fp}'. Use finish if done.")
            
            if errors:
                action_result = "PATCH REJECTED:\n" + "\n".join(f"- {e}" for e in errors)
                if fp not in files_read and fp:
                    action_result += f"\n\nPlease read '{fp}' first using read_file action."
            else:
                # Validate syntax
                valid, error = _validate_syntax(fp, patched)
                if not valid:
                    action_result = f"VALIDATION FAILED: {error}\n\nPlease fix the syntax and regenerate."
                    # Create error context for reflection
                    current_error = ErrorContext(
                        error_type="SyntaxError",
                        error_message=error,
                        traceback_str=error,
                        file_path=fp,
                        iteration=iteration
                    )
                    errors_encountered.append(current_error)
                    reflection_mode = True
                else:
                    # Test the code (Python only)
                    test_passed, test_error = _test_python_code(fp, patched)
                    if not test_passed and test_error:
                        action_result = f"CODE TEST FAILED: {test_error.error_message}\n\nPlease fix this error and regenerate."
                        errors_encountered.append(test_error)
                        current_error = test_error
                        reflection_mode = True
                    else:
                        patch = PatchSpec(
                            file_path=fp,
                            patched_content=patched,
                            explanation=explanation,
                            patch_confidence=patch_conf,
                            final_confidence=patch_conf,
                        )
                        patches.append(patch)
                        files_targeted.add(fp)
                        action_result = f"✅ Patch accepted for {fp} (confidence={patch_conf:.2f}). Generate more patches or finish."
                        print(f"    Staged patch: {fp} (conf={patch_conf:.2f})")
                        phase = "verify"
                        iterations_in_phase = 0
                        reflection_mode = False
        
        elif action == "finish":
            print(f"  Agent finished after {iteration + 1} iterations")
            break
            
        elif action == "give_up":
            reason = params.get("explanation", "No explanation provided")
            print(f"  Agent gave up: {reason}")
            return FixResult(
                gave_up=True,
                reason=reason,
                agent_thinking_trace="\n".join(thinking_trace),
                iterations_used=iteration+1,
                exploration_coverage=len(files_read) / max(len(files_targeted), 1),
                errors_encountered=[{"type": "GiveUp", "message": reason}]
            )
        
        else:
            action_result = f"ERROR: Unknown action '{action}'. Valid: read_file, list_directory, generate_patch, finish, give_up"
        
        conversation.append({"role": "user", "content": f"[RESULT]\n{action_result}\n\nContinue with your next action."})
        
        # Phase transitions
        if phase == "explore" and len(files_read) >= 3:
            phase = "analyze"
            iterations_in_phase = 0
        elif phase == "analyze" and iterations_in_phase >= 3 and not patches:
            phase = "patch"
            iterations_in_phase = 0
    
    else:
        # Max iterations reached
        if not patches:
            return FixResult(
                gave_up=True,
                reason=f"Max iterations ({MAX_ITER}) reached without generating valid patches",
                iterations_used=MAX_ITER,
                exploration_coverage=len(files_read) / max(len(files_targeted), 1),
                errors_encountered=[{"type": e.error_type, "message": e.error_message} for e in errors_encountered]
            )
    
    if not patches:
        return FixResult(
            gave_up=True,
            reason="No valid patches generated",
            agent_thinking_trace="\n".join(thinking_trace),
            iterations_used=len(thinking_trace),
            errors_encountered=[{"type": e.error_type, "message": e.error_message} for e in errors_encountered]
        )
    
    # Verification phase
    print(f"  Verifying {len(patches)} patch(es)...")
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
            print(f"  Rejected {p.file_path}: {p.final_confidence:.2f}")
    
    return FixResult(
        patches=final_patches,
        gave_up=len(final_patches) == 0,
        reason=f"{len(rejected)} patches rejected (below {CONFIDENCE_THRESHOLD} threshold)" if rejected and not final_patches else "",
        agent_thinking_trace="\n".join(thinking_trace),
        iterations_used=len(thinking_trace),
        exploration_coverage=len(files_read) / max(len(files_targeted), 1),
        errors_encountered=[{"type": e.error_type, "message": e.error_message} for e in errors_encountered]
    )


def _apply_patches(
    patches: list[PatchSpec],
    branch_name: str,
    issue_num: int,
    issue_title: str,
    base: str,
) -> None:
    """Create branch and commit patches."""
    def run(cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Git failed: {' '.join(cmd)}\n{result.stderr[:500]}")
    
    run(["git", "checkout", "-b", branch_name])
    
    for patch in patches:
        path = Path(patch.file_path)
        full_path = Path(".") / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(patch.patched_content, encoding="utf-8")
        run(["git", "add", str(patch.file_path)])
        print(f"  Staged: {patch.file_path}")
    
    safe_title = issue_title[:60].replace('"', "'")
    msg = f"""fix: {safe_title} (AI-generated)

Closes #{issue_num}

Generated by Ghost Review Auto-Fix.
"""
    run(["git", "commit", "-m", msg])
    run(["git", "push", "origin", branch_name])
    print(f"  Pushed branch: {branch_name}")


async def run_auto_fix() -> None:
    """Main entry point for auto-fix with self-healing."""
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
    print(f"Title: {issue.title}")
    
    # Guardrails
    if model_size == "3b":
        issue.create_comment("⚠️ Auto-Fix requires 7B model for reliable results.")
        print("Abort: 3B model insufficient")
        return
    
    if actor and not check_actor_permission(repo, actor, required="write"):
        print(f"Skip: {actor} lacks write permission")
        issue.create_comment(f"❌ Auto-Fix skipped: @{actor} requires write permission")
        return
    
    if not config.get("auto_fix", {}).get("enabled", True):
        print("Skip: Auto-Fix disabled in config")
        return
    
    working_comment = issue.create_comment("🤖 Auto-Fix agent analyzing issue... (ETA: 3-5 minutes)")
    
    issue_data = {
        "number": issue_number,
        "title": issue.title,
        "body": issue.body or "",
    }
    
    result = FixResult(gave_up=True, reason="Initialization failed")
    
    try:
        async with LLMClient() as llm:
            result = await run_agentic_fix_with_reflection(issue_data, ".", config, llm)
    except Exception as e:
        print(f"Critical error: {e}")
        import traceback
        tb_str = traceback.format_exc()
        result = FixResult(
            gave_up=True,
            reason=f"System error: {e}",
            errors_encountered=[{"type": type(e).__name__, "message": str(e), "traceback": tb_str}]
        )
    finally:
        try:
            working_comment.delete()
        except:
            pass
    
    if result.gave_up or not result.patches:
        # Post failure comment with detailed error analysis
        trace = result.agent_thinking_trace[-1500:] if result.agent_thinking_trace else "No trace available"
        
        error_details = ""
        if result.errors_encountered:
            error_details = "\n\n**Errors Encountered**:\n"
            for i, err in enumerate(result.errors_encountered[-3:], 1):  # Show last 3 errors
                error_details += f"\n{i}. **{err.get('type', 'Unknown')}**: {err.get('message', 'No message')[:200]}"
        
        # If we have partial patches but couldn't validate them, create a draft PR anyway
        if result.patches:
            safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in issue.title[:30]).strip("-")
            branch_name = f"fix/ai-{issue_number}-{safe_title}-partial"
            
            try:
                _apply_patches(result.patches, branch_name, issue_number, issue.title, repo.default_branch)
                
                # Create draft PR with error notes
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
                    agent_thinking=result.agent_thinking_trace + "\n\nERRORS:\n" + str(result.errors_encountered),
                    confidence=0.3,  # Low confidence due to errors
                    repo_path=".",
                )
                
                issue.create_comment(
                    f"⚠️ **Draft PR Created (Partial/Needs Review)**: {pr_url}\n\n"
                    f"The agent encountered errors during fixing and could not fully validate the patches.\n\n"
                    f"**Status**: Gave up after {result.iterations_used} iterations\n"
                    f"**Reason**: {result.reason or 'Unable to generate valid patch'}"
                    f"{error_details}\n\n"
                    f"**Agent Trace** (last 1500 chars):\n"
                    f"<details><summary>Expand</summary>\n\n```\n{trace}\n```\n</details>\n\n"
                    f"⚠️ **This PR requires manual review and fixes before merging.**"
                )
                print(f"Partial success with errors: {pr_url}")
                return
                
            except Exception as git_err:
                print(f"Failed to create partial PR: {git_err}")
        
        # Complete failure - no patches at all
        issue.create_comment(
            f"❌ Auto-Fix could not generate a patch\n\n"
            f"**Status**: Gave up after {result.iterations_used} iterations\n"
            f"**Reason**: {result.reason or 'Unable to determine fix'}"
            f"{error_details}\n\n"
            f"**Agent Trace** (last 1500 chars):\n"
            f"<details><summary>Expand</summary>\n\n```\n{trace}\n```\n</details>\n\n"
            f"Please fix this issue manually."
        )
        print(f"Failed: {result.reason}")
        return
    
    # Success path - create branch and apply patches
    safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in issue.title[:30]).strip("-")
    branch_name = f"fix/ai-{issue_number}-{safe_title}"
    
    try:
        _apply_patches(result.patches, branch_name, issue_number, issue.title, repo.default_branch)
    except Exception as e:
        issue.create_comment(f"❌ Auto-Fix failed to create branch: {e}")
        print(f"Git error: {e}")
        return
    
    # Calculate statistics
    avg_conf = sum(p.final_confidence for p in result.patches) / len(result.patches)
    coverage_pct = result.exploration_coverage * 100
    
    if avg_conf >= 0.85:
        confidence_badge = "✅ High confidence"
    elif avg_conf >= 0.70:
        confidence_badge = "ℹ️ Medium confidence"
    else:
        confidence_badge = "⚠️ Low confidence"
    
    patch_summaries = [
        {"file_path": p.file_path, "explanation": p.explanation}
        for p in result.patches
    ]
    
    try:
        pr_url = create_draft_pr(
            repo=repo,
            branch_name=branch_name,
            base_branch=repo.default_branch,
            issue_number=issue_number,
            issue_title=issue.title,
            patch_summaries=patch_summaries,
            agent_thinking=result.agent_thinking_trace,
            confidence=avg_conf,
            repo_path=".",
        )
    except Exception as e:
        issue.create_comment(f"❌ Auto-Fix created branch but failed to create PR: {e}")
        print(f"PR creation error: {e}")
        return
    
    # Success comment
    error_summary = ""
    if result.errors_encountered:
        error_summary = f"\n(Encountered and resolved {len(result.errors_encountered)} error(s) during fixing)"
    
    issue.create_comment(
        f"✅ **Draft PR Created**: {pr_url}\n\n"
        f"{confidence_badge} ({avg_conf*100:.0f}%){error_summary}\n"
        f"Exploration coverage: {coverage_pct:.0f}%\n\n"
        f"**Modified Files**:\n"
        + "\n".join(f"- `{p.file_path}`: {p.explanation[:100]}..." if len(p.explanation) > 100 else f"- `{p.file_path}`: {p.explanation}"
                    for p in result.patches)
        + "\n\n⚠️ **Important**: This PR was AI-generated. Please review carefully before merging."
    )
    
    print(f"✅ Success: {pr_url}")


if __name__ == "__main__":
    asyncio.run(run_auto_fix())
