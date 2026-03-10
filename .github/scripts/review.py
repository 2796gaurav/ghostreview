"""
.github/scripts/review.py

Main PR review orchestrator with 4-pass analysis.
Implements: Summary → Bug + Security (parallel) → Synthesis → Comments
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from diff_parser import parse_diff, sanitize
from github import Github
from llm_client import LLMClient, LLMError
from prompts import build_system_prompt, PROMPT_SUMMARY, PROMPT_BUGS, PROMPT_SECURITY, PROMPT_SYNTHESIS
from schemas import SUMMARY_SCHEMA, BUGS_SCHEMA, SECURITY_SCHEMA, SYNTHESIS_SCHEMA


# Model-specific token budgets
_MODEL_CONFIG = {
    "7b": {"max_diff_tokens": 22000, "context_reserve": 5000},
    "3b": {"max_diff_tokens": 8000, "context_reserve": 2500},
}

# Pass timeouts (seconds)
_PASS_TIMEOUTS = {
    "summary": 180.0,
    "bugs": 240.0,
    "security": 180.0,
    "synthesis": 120.0,
}

# Fallback values when passes fail/time out
_FALLBACKS = {
    "summary": {"pr_type": "unknown", "pr_description": "Unable to analyze", "affected_components": [], "risk_areas": []},
    "bugs": {"findings": []},
    "security": {"findings": []},
    "synthesis": {"risk_level": "unknown", "confidence": 0.0, "summary": "Analysis unavailable", "recommendation": "Please review manually"},
}

SEV_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3, "unknown": 4}


@dataclass
class ReviewResult:
    risk_level: str = "unknown"
    confidence: float = 0.0
    summary: str = ""
    pr_type: str = "unknown"
    bugs: list = field(default_factory=list)
    security: list = field(default_factory=list)
    failed_passes: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _get_token_budget(model_size: str) -> tuple[int, int]:
    cfg = _MODEL_CONFIG.get(model_size, _MODEL_CONFIG["7b"])
    return cfg["max_diff_tokens"], cfg["context_reserve"]


def _cap_diff(diff: str, model_size: str) -> str:
    max_tokens, _ = _get_token_budget(model_size)
    # Estimate 4 chars per token
    max_chars = max_tokens * 4
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + f"\n\n... [truncated {len(diff) - max_chars} chars]"


async def run_review(pr_data: dict, diff_text: str, model_size: str, llm: LLMClient) -> ReviewResult:
    """
    4-pass review with parallel bug + security passes.
    """
    result = ReviewResult()
    diff_capped = _cap_diff(diff_text, model_size)
    system = build_system_prompt(pr_data.get("config", {}))
    
    print(f"Starting review: model={model_size}, diff_chars={len(diff_text)}, capped={len(diff_capped)}")
    
    # Pass 1: Summary
    print("Pass 1/4: Summary...")
    try:
        summary = await asyncio.wait_for(
            llm.chat(
                system=system,
                user=PROMPT_SUMMARY.format(title=pr_data.get("title", ""), body=pr_data.get("body", "")[:3000], diff=diff_capped[:4000]),
                schema=SUMMARY_SCHEMA,
                max_tokens=2048,
                temperature=0.2,
            ),
            timeout=_PASS_TIMEOUTS["summary"],
        )
        result.pr_type = summary.get("pr_type", "unknown")
        result.summary = summary.get("pr_description", "")
        result.metadata["affected_components"] = summary.get("affected_components", [])
        result.metadata["risk_areas"] = summary.get("risk_areas", [])
    except (asyncio.TimeoutError, LLMError) as e:
        print(f"  Summary failed: {e}")
        result.failed_passes.append("summary")
        summary = _FALLBACKS["summary"]
        result.pr_type = summary["pr_type"]
        result.summary = summary["pr_description"]
    
    # Pass 2+3: Bug detection + Security (parallel)
    use_parallel = True  # 4-vCPU arm64 runner
    print(f"Pass 2/4: Bug Detection (parallel={use_parallel})")
    print(f"Pass 3/4: Security Scan (parallel={use_parallel})")
    
    bugs_failed, security_failed = False, False
    
    async def run_bugs():
        try:
            return await asyncio.wait_for(
                llm.chat(
                    system=system,
                    user=PROMPT_BUGS.format(context=result.summary, diff=diff_capped),
                    schema=BUGS_SCHEMA,
                    max_tokens=4096,
                    temperature=0.2,
                ),
                timeout=_PASS_TIMEOUTS["bugs"],
            )
        except (asyncio.TimeoutError, LLMError) as e:
            print(f"  Bugs failed: {e}")
            nonlocal bugs_failed
            bugs_failed = True
            return _FALLBACKS["bugs"]
    
    async def run_security():
        try:
            return await asyncio.wait_for(
                llm.chat(
                    system=system,
                    user=PROMPT_SECURITY.format(diff=diff_capped),
                    schema=SECURITY_SCHEMA,
                    max_tokens=4096,
                    temperature=0.1,
                ),
                timeout=_PASS_TIMEOUTS["security"],
            )
        except (asyncio.TimeoutError, LLMError) as e:
            print(f"  Security failed: {e}")
            nonlocal security_failed
            security_failed = True
            return _FALLBACKS["security"]
    
    if use_parallel:
        bugs_res, security_res = await asyncio.gather(run_bugs(), run_security())
    else:
        bugs_res = await run_bugs()
        security_res = await run_security()
    
    if bugs_failed:
        result.failed_passes.append("bugs")
    if security_failed:
        result.failed_passes.append("security")
    
    result.bugs = bugs_res.get("findings", [])
    result.security = security_res.get("findings", [])
    
    # Filter out placeholder security findings
    result.security = [f for f in result.security if f.get("vulnerability_class") != "none_found"]
    
    print(f"  Found {len(result.bugs)} bugs, {len(result.security)} security issues")
    
    # Pass 4: Synthesis
    print("Pass 4/4: Synthesis...")
    try:
        synth = await asyncio.wait_for(
            llm.chat(
                system=system,
                user=PROMPT_SYNTHESIS.format(
                    summary=json.dumps({"pr_type": result.pr_type, "description": result.summary}),
                    bugs=json.dumps(result.bugs[:10]),  # Limit context
                    security=json.dumps(result.security[:5]),
                ),
                schema=SYNTHESIS_SCHEMA,
                max_tokens=1024,
                temperature=0.2,
            ),
            timeout=_PASS_TIMEOUTS["synthesis"],
        )
        result.risk_level = synth.get("risk_level", "unknown")
        result.confidence = synth.get("confidence", 0.0)
        result.metadata["recommendation"] = synth.get("recommendation", "")
    except (asyncio.TimeoutError, LLMError) as e:
        print(f"  Synthesis failed: {e}")
        result.failed_passes.append("synthesis")
        synth = _FALLBACKS["synthesis"]
        result.risk_level = synth["risk_level"]
        result.confidence = synth["confidence"]
    
    # Cap confidence if any pass failed
    if result.failed_passes and result.confidence > 0.3:
        result.confidence = min(result.confidence, 0.3)
        print(f"  Confidence capped due to failures: {result.failed_passes}")
    
    result.metadata["failed_passes"] = result.failed_passes
    print(f"Review complete: risk={result.risk_level}, confidence={result.confidence:.2f}")
    return result


def _format_findings(findings: list, limit: int = 5) -> str:
    """Format findings for PR comment."""
    if not findings:
        return "None detected."
    
    findings_sorted = sorted(findings, key=lambda f: SEV_ORDER.get(f.get("severity", "unknown"), 99))
    lines = []
    for f in findings_sorted[:limit]:
        sev = f.get("severity", "info").upper()
        conf = f.get("confidence", 0) * 100
        title = f.get("title", "Untitled")
        file_path = f.get("file_path", "unknown")
        line_nums = f.get("line_numbers", "?")
        
        badge = "🔴" if sev == "CRITICAL" else "🟠" if sev == "ERROR" else "🟡" if sev == "WARNING" else "ℹ️"
        lines.append(f"{badge} **{title}** ({sev}, {conf:.0f}% conf)\n   `@{file_path}:{line_nums}`")
        
        desc = f.get("description", "")
        if desc:
            lines.append(f"   > {desc[:200]}{'...' if len(desc) > 200 else ''}")
        
        fix = f.get("suggested_fix", "")
        if fix:
            lines.append(f"\n   **Suggested fix:**\n   ```\n{fix[:300]}{'...' if len(fix) > 300 else ''}\n   ```")
        lines.append("")
    
    remaining = len(findings_sorted) - limit
    if remaining > 0:
        lines.append(f"*... and {remaining} more*")
    
    return "\n".join(lines)


def _post_review_comment(pr, result: ReviewResult, model_size: str) -> None:
    """Post structured review comment to PR."""
    
    risk_emoji = {
        "critical": "🔴 Critical",
        "high": "🟠 High", 
        "medium": "🟡 Medium",
        "low": "🟢 Low",
        "unknown": "⚪ Unknown",
    }.get(result.risk_level, "⚪ Unknown")
    
    confidence_emoji = "✅" if result.confidence >= 0.8 else "⚠️" if result.confidence >= 0.5 else "❓"
    
    body = f"""## Ghost Review Report

| Metric | Value |
|--------|-------|
| **Risk Level** | {risk_emoji} |
| **Confidence** | {confidence_emoji} {result.confidence*100:.0f}% |
| **PR Type** | {result.pr_type.upper()} |
| **Model** | {model_size.upper()} |

### Summary
{result.summary or "No summary available."}

### Security Issues ({len(result.security)})
{_format_findings(result.security)}

### Bugs & Quality ({len(result.bugs)})
{_format_findings(result.bugs)}

### Recommendation
{result.metadata.get("recommendation", "Please review manually.")}

---
<sub>Generated by Ghost Review • {os.environ.get('RUN_ID', 'local')}</sub>
"""
    
    # Post main comment
    pr.create_issue_comment(body)
    
    # Add review comments for top findings
    files = {f.get("file_path"): f for f in result.bugs + result.security}
    for f in result.bugs[:2] + result.security[:2]:
        try:
            path = f.get("file_path")
            line = f.get("line_numbers", "").split("-")[0]
            if line.isdigit():
                pr.create_review_comment(
                    body=f"**{f.get('severity', 'info').upper()}**: {f.get('title', 'Issue')}\n\n{f.get('description', '')[:200]}",
                    commit_id=pr.head.sha,
                    path=path,
                    position=int(line),
                )
        except Exception as e:
            print(f"  Could not post line comment: {e}")


async def main():
    github_token = os.environ["GITHUB_TOKEN"]
    repo_slug = os.environ["REPO"]
    pr_number = int(os.environ["PR_NUMBER"])
    model_size = os.environ.get("MODEL_SIZE", "7b")
    
    g = Github(github_token)
    repo = g.get_repo(repo_slug)
    pr = repo.get_pull(pr_number)
    config = load_config(".github/localreviewer.yml")
    
    print(f"=== Ghost Review | PR #{pr_number} | model={model_size} ===")
    
    # Get diff
    diff_text = pr.get_diff()
    if not diff_text:
        print("No diff found")
        pr.create_issue_comment("⚠️ Ghost Review: No diff to analyze")
        return
    
    pr_data = {
        "title": pr.title,
        "body": pr.body or "",
        "number": pr_number,
        "config": config,
    }
    
    # Run review
    async with LLMClient() as llm:
        result = await run_review(pr_data, diff_text, model_size, llm)
    
    # Post results
    _post_review_comment(pr, result, model_size)
    
    # Summary
    print(f"=== Review Complete ===")
    print(f"Risk: {result.risk_level}")
    print(f"Confidence: {result.confidence:.2f}")
    print(f"Bugs: {len(result.bugs)}")
    print(f"Security: {len(result.security)}")
    if result.failed_passes:
        print(f"Failed passes: {result.failed_passes}")


if __name__ == "__main__":
    asyncio.run(main())
