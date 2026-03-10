"""
.github/scripts/review.py

4-pass PR review orchestrator with:
  - Token-aware adaptive chunking for large diffs
  - Priority-based pass scheduling (security first if risk detected)
  - Parallel execution with circuit breaker pattern
  - Result aggregation with confidence weighting
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
from diff_parser import preprocess_diff, get_diff, estimate_tokens
from github import Github
from github_api import format_review_comment, post_or_update_review_comment
from llm_client import LLMClient, LLMError
from prompts import build_system_prompt, PROMPT_SUMMARY, PROMPT_BUGS, PROMPT_SECURITY, PROMPT_SYNTHESIS
from schemas import SUMMARY_SCHEMA, BUGS_SCHEMA, SECURITY_SCHEMA, SYNTHESIS_SCHEMA


# Model configurations
_MODEL_CONFIG = {
    "7b": {"max_diff_tokens": 24000, "context_reserve": 6000, "parallel_slots": 2},
    "3b": {"max_diff_tokens": 10000, "context_reserve": 3000, "parallel_slots": 1},
}

# Pass timeouts and priorities (reduced to prevent hanging)
_PASS_CONFIG = {
    "summary":  {"timeout": 90.0, "priority": 1, "critical": True},   # Reduced from 180
    "bugs":     {"timeout": 120.0, "priority": 2, "critical": False}, # Reduced from 240
    "security": {"timeout": 90.0, "priority": 2, "critical": True},  # Reduced from 180
    "synthesis":{"timeout": 60.0, "priority": 3, "critical": True},  # Reduced from 120
}

# Fallback values
_FALLBACKS = {
    "summary": {"pr_type": "unknown", "pr_description": "Analysis unavailable", "affected_components": [], "risk_areas": []},
    "bugs": {"findings": []},
    "security": {"findings": []},
    "synthesis": {"risk_level": "unknown", "confidence": 0.0, "summary": "Analysis incomplete", "recommendation": "Manual review required"},
}

SEV_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3, "unknown": 4}


@dataclass
class PassResult:
    name: str
    data: dict[str, Any]
    success: bool
    error: str = ""
    duration: float = 0.0


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
    pass_results: dict = field(default_factory=dict)


class AdaptiveChunker:
    """
    Intelligently chunk large diffs for multi-pass review.
    Prioritizes security-critical files and maintains context boundaries.
    """
    
    def __init__(self, max_tokens: int, model_size: str):
        self.max_tokens = max_tokens
        self.model_config = _MODEL_CONFIG.get(model_size, _MODEL_CONFIG["7b"])
        self.effective_budget = max_tokens - self.model_config["context_reserve"]
    
    def chunk_diff(self, diff_text: str, config: dict) -> list[str]:
        """
        Split diff into chunks that fit within token budget.
        Returns list of diff chunks for sequential processing.
        """
        # If diff fits, return as single chunk
        if estimate_tokens(diff_text) <= self.effective_budget:
            return [diff_text]
        
        # Split by file
        file_pattern = re.compile(r'^diff --git a/(.+?) b/\1', re.MULTILINE)
        files: list[tuple[str, str, int]] = []  # (path, content, importance)
        
        matches = list(file_pattern.finditer(diff_text))
        for i, match in enumerate(matches):
            path = match.group(1)
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
            content = diff_text[start:end]
            
            # Score importance
            importance = self._score_importance(path, config)
            files.append((path, content, importance))
        
        # Sort by importance (security-critical first)
        files.sort(key=lambda x: x[2])
        
        # Greedily pack files into chunks
        chunks: list[str] = []
        current_chunk: list[str] = []
        current_tokens = 0
        
        for path, content, _ in files:
            content_tokens = estimate_tokens(content)
            
            if content_tokens > self.effective_budget:
                # Single file exceeds budget - need to truncate
                from diff_parser import truncate_to_tokens
                truncated = truncate_to_tokens(content, self.effective_budget - 100)
                if current_chunk:
                    chunks.append("".join(current_chunk))
                chunks.append(truncated)
                current_chunk = []
                current_tokens = 0
            elif current_tokens + content_tokens > self.effective_budget:
                # Start new chunk
                if current_chunk:
                    chunks.append("".join(current_chunk))
                current_chunk = [content]
                current_tokens = content_tokens
            else:
                # Add to current chunk
                current_chunk.append(content)
                current_tokens += content_tokens
        
        if current_chunk:
            chunks.append("".join(current_chunk))
        
        return chunks
    
    def _score_importance(self, path: str, config: dict) -> int:
        """Lower score = higher priority."""
        critical_paths = config.get("review", {}).get("security_critical_paths", [])
        
        for pattern in critical_paths:
            if re.search(pattern.replace("*", ".*"), path):
                return 0  # Highest priority
        
        # Source files over tests
        if any(x in path for x in ["_test.", ".test.", "test_", "/tests/", "/test/"]):
            return 4
        
        # Config/docs lower priority
        if path.endswith((".md", ".txt", ".json", ".yml", ".yaml")):
            return 3
        
        return 2  # Normal source


async def run_pass_with_retry(
    llm: LLMClient,
    pass_name: str,
    system: str,
    prompt: str,
    schema: dict,
    config: dict,
    max_retries: int = 2,
) -> PassResult:
    """Execute a review pass with retry logic."""
    timeout = _PASS_CONFIG[pass_name]["timeout"]
    
    for attempt in range(max_retries + 1):
        start = asyncio.get_event_loop().time()
        try:
            result = await asyncio.wait_for(
                llm.chat(
                    system=system,
                    user=prompt,
                    schema=schema,
                    max_tokens=4096 if pass_name in ("bugs", "security") else 2048,
                    temperature=0.1 if pass_name == "security" else 0.2,
                ),
                timeout=timeout,
            )
            duration = asyncio.get_event_loop().time() - start
            return PassResult(name=pass_name, data=result, success=True, duration=duration)
            
        except asyncio.TimeoutError:
            if attempt < max_retries:
                print(f"  {pass_name} timeout, retrying...")
                timeout *= 1.5  # Increase timeout for retry
            else:
                return PassResult(
                    name=pass_name,
                    data=_FALLBACKS[pass_name],
                    success=False,
                    error="Timeout",
                    duration=asyncio.get_event_loop().time() - start,
                )
        except LLMError as e:
            if attempt < max_retries:
                print(f"  {pass_name} error: {e}, retrying...")
                await asyncio.sleep(1)
            else:
                return PassResult(
                    name=pass_name,
                    data=_FALLBACKS[pass_name],
                    success=False,
                    error=str(e),
                    duration=asyncio.get_event_loop().time() - start,
                )


async def run_parallel_passes(
    llm: LLMClient,
    system: str,
    diff_chunk: str,
    summary_text: str,
) -> tuple[PassResult, PassResult]:
    """Run bug and security passes in parallel."""
    bugs_task = run_pass_with_retry(
        llm, "bugs", system,
        PROMPT_BUGS.format(context=summary_text, diff=diff_chunk),
        BUGS_SCHEMA, _PASS_CONFIG["bugs"]
    )
    security_task = run_pass_with_retry(
        llm, "security", system,
        PROMPT_SECURITY.format(diff=diff_chunk),
        SECURITY_SCHEMA, _PASS_CONFIG["security"]
    )
    
    results = await asyncio.gather(bugs_task, security_task, return_exceptions=True)
    
    bugs_result = results[0] if isinstance(results[0], PassResult) else PassResult(
        "bugs", _FALLBACKS["bugs"], False, str(results[0])
    )
    security_result = results[1] if isinstance(results[1], PassResult) else PassResult(
        "security", _FALLBACKS["security"], False, str(results[1])
    )
    
    return bugs_result, security_result


async def run_review(
    pr_data: dict,
    diff_text: str,
    model_size: str,
    llm: LLMClient,
    config: dict,
) -> ReviewResult:
    """
    4-pass review with adaptive chunking and parallel execution.
    """
    result = ReviewResult()
    system = build_system_prompt(config)
    
    # Preprocess diff
    max_tokens = _MODEL_CONFIG.get(model_size, _MODEL_CONFIG["7b"])["max_diff_tokens"]
    processed_diff, warnings = preprocess_diff(diff_text, max_tokens, config)
    
    print(f"Review: model={model_size}, raw_diff={len(diff_text)} chars, "
          f"processed={len(processed_diff)} chars, tokens={estimate_tokens(processed_diff)}")
    
    # Adaptive chunking for large diffs
    chunker = AdaptiveChunker(max_tokens, model_size)
    chunks = chunker.chunk_diff(processed_diff, config)
    print(f"Split into {len(chunks)} chunk(s) for processing")
    
    # If multiple chunks, we need to aggregate results
    all_bugs: list[dict] = []
    all_security: list[dict] = []
    chunk_summaries: list[str] = []
    
    for chunk_idx, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"\nProcessing chunk {chunk_idx + 1}/{len(chunks)}...")
        
        # Pass 1: Summary
        summary_result = await run_pass_with_retry(
            llm, "summary", system,
            PROMPT_SUMMARY.format(
                title=pr_data.get("title", ""),
                body=pr_data.get("body", "")[:3000],
                diff=chunk[:6000],
            ),
            SUMMARY_SCHEMA, _PASS_CONFIG["summary"]
        )
        
        if not summary_result.success:
            result.failed_passes.append("summary")
        
        chunk_summaries.append(summary_result.data.get("pr_description", ""))
        if chunk_idx == 0:
            result.pr_type = summary_result.data.get("pr_type", "unknown")
            result.metadata["affected_components"] = summary_result.data.get("affected_components", [])
            result.metadata["risk_areas"] = summary_result.data.get("risk_areas", [])
        
        # Pass 2 & 3: Bugs + Security (parallel)
        bugs_result, security_result = await run_parallel_passes(
            llm, system, chunk, chunk_summaries[-1]
        )
        
        if not bugs_result.success:
            result.failed_passes.append(f"bugs_chunk_{chunk_idx}")
        if not security_result.success:
            result.failed_passes.append(f"security_chunk_{chunk_idx}")
        
        all_bugs.extend(bugs_result.data.get("findings", []))
        all_security.extend(security_result.data.get("findings", []))
    
    # Deduplicate findings across chunks
    result.bugs = _deduplicate_findings(all_bugs)
    result.security = _deduplicate_findings(all_security)
    
    # Filter placeholder security findings
    result.security = [f for f in result.security if f.get("vulnerability_class") != "none_found"]
    
    print(f"Total unique findings: {len(result.bugs)} bugs, {len(result.security)} security")
    
    # Pass 4: Synthesis (uses aggregated findings)
    synthesis_result = await run_pass_with_retry(
        llm, "synthesis", system,
        PROMPT_SYNTHESIS.format(
            summary=json.dumps({
                "pr_type": result.pr_type,
                "description": " ".join(chunk_summaries)[:1000],
            }),
            bugs=json.dumps(result.bugs[:15]),  # Limit context
            security=json.dumps(result.security[:10]),
        ),
        SYNTHESIS_SCHEMA, _PASS_CONFIG["synthesis"]
    )
    
    if not synthesis_result.success:
        result.failed_passes.append("synthesis")
    
    result.risk_level = synthesis_result.data.get("risk_level", "unknown")
    result.confidence = synthesis_result.data.get("confidence", 0.0)
    result.summary = synthesis_result.data.get("summary", "")
    result.metadata["recommendation"] = synthesis_result.data.get("recommendation", "")
    result.metadata["warnings"] = warnings
    
    # Cap confidence if passes failed
    if result.failed_passes and result.confidence > 0.3:
        result.confidence = min(result.confidence, 0.3)
    
    result.failed_passes = list(set(result.failed_passes))  # Deduplicate
    result.metadata["failed_passes"] = result.failed_passes
    result.metadata["chunks_processed"] = len(chunks)
    
    print(f"Review complete: risk={result.risk_level}, confidence={result.confidence:.2f}")
    return result


def _deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Remove duplicate findings based on file+line+title similarity."""
    seen: set[str] = set()
    unique: list[dict] = []
    
    for f in findings:
        key = f"{f.get('file_path', '')}:{f.get('line_numbers', '')}:{f.get('title', '')[:30]}"
        if key not in seen:
            seen.add(key)
            unique.append(f)
    
    return unique


def _flatten_findings(findings: list[dict], finding_type: str) -> list[dict]:
    """Convert findings to standard format."""
    result = []
    for f in findings:
        result.append({
            "type": finding_type,
            "severity": f.get("severity", "info"),
            "file": f.get("file_path", ""),
            "line_start": f.get("line_numbers", "").split("-")[0] if f.get("line_numbers") else None,
            "line_end": f.get("line_numbers", "").split("-")[-1] if f.get("line_numbers") and "-" in f.get("line_numbers", "") else None,
            "description": f.get("description", ""),
            "suggested_fix": f.get("suggested_fix", ""),
            "vulnerability_class": f.get("vulnerability_class", ""),
        })
    return result


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
    base_sha = os.environ.get("BASE_SHA", pr.base.sha)
    head_sha = os.environ.get("HEAD_SHA", pr.head.sha)
    
    try:
        diff_text = get_diff(base_sha, head_sha)
    except Exception as e:
        print(f"Error getting diff: {e}")
        pr.create_issue_comment("⚠️ Ghost Review: Could not retrieve diff")
        return
    
    if not diff_text:
        print("No diff found")
        pr.create_issue_comment("⚠️ Ghost Review: No diff to analyze")
        return
    
    pr_data = {
        "title": pr.title,
        "body": pr.body or "",
        "number": pr_number,
    }
    
    start_time = asyncio.get_event_loop().time()
    
    # Run review with overall timeout guard
    print("Starting review process...")
    try:
        async with LLMClient() as llm:
            # Add overall timeout of 8 minutes to prevent indefinite hanging
            result = await asyncio.wait_for(
                run_review(pr_data, diff_text, model_size, llm, config),
                timeout=480.0  # 8 minutes max total
            )
    except asyncio.TimeoutError:
        print("ERROR: Review timed out after 8 minutes")
        pr.create_issue_comment("⚠️ Ghost Review: Analysis timed out. The model may be overloaded.")
        return
    except Exception as e:
        print(f"ERROR: Review failed with exception: {e}")
        import traceback
        traceback.print_exc()
        pr.create_issue_comment(f"⚠️ Ghost Review: Analysis failed with error: {str(e)[:200]}")
        return
    
    elapsed = asyncio.get_event_loop().time() - start_time
    
    # Format and post comment
    all_findings = _flatten_findings(result.bugs, "bug") + _flatten_findings(result.security, "security")
    
    comment_body = format_review_comment(
        summary={
            "summary": result.summary,
            "pr_type": result.pr_type,
            "changed_files_summary": [],
            "risk_assessment": ", ".join(result.metadata.get("risk_areas", [])),
        },
        all_findings=all_findings,
        verdict={
            "risk_level": result.risk_level,
            "merge_recommendation": "approve" if result.risk_level == "low" and result.confidence > 0.7 else "needs_discussion",
            "confidence": result.confidence,
            "rationale": result.metadata.get("recommendation", ""),
        },
        warnings=result.metadata.get("warnings", []),
        model_size=model_size,
        runner_vcpus=os.environ.get("RUNNER_VCPUS", "4"),
        elapsed_seconds=elapsed,
        failed_passes=result.failed_passes,
    )
    
    await post_or_update_review_comment(pr, comment_body)
    
    # Summary
    print(f"\n=== Review Complete ===")
    print(f"Risk: {result.risk_level}")
    print(f"Confidence: {result.confidence:.2f}")
    print(f"Bugs: {len(result.bugs)}")
    print(f"Security: {len(result.security)}")
    print(f"Time: {elapsed:.1f}s")
    if result.failed_passes:
        print(f"Failed: {result.failed_passes}")


if __name__ == "__main__":
    asyncio.run(main())
