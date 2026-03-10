"""
.github/scripts/review.py

PR Review orchestrator — runs 4 passes against the diff:
  Pass 1: Summary (temperature=0.3)
  Pass 2: Bug/logic detection (temperature=0.1)
  Pass 3: Security scan (temperature=0.1)
  Pass 4: Synthesis/verdict (temperature=0.2)

Passes 2 and 3 run concurrently on 4-vCPU runners (--parallel 2).
All passes use grammar-constrained JSON (enforced at token sampler).
KV prefix cache reuse is activated via cache_prompt=True in each request.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Ensure script directory is on the path
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from context_builder import build_codebase_context
from diff_parser import (
    extract_changed_files,
    get_diff,
    preprocess_diff,
    sanitize,
)
from github import Github
from github_api import format_review_comment, post_or_update_review_comment
from llm_client import LLMClient, LLMError
from prompts import (
    PROMPT_BUGS,
    PROMPT_SECURITY,
    PROMPT_SUMMARY,
    PROMPT_SYNTHESIS,
    build_system_prompt,
)
from schemas import FINDINGS_SCHEMA, SECURITY_SCHEMA, SUMMARY_SCHEMA, SYNTHESIS_SCHEMA


# ─────────────────────────────────────────────────────────────────────
# Per-model configuration
# ─────────────────────────────────────────────────────────────────────

_MODEL_CONFIG = {
    "7b": {
        "diff_budget":    22_000,
        "context_budget": 5_000,
        "max_out_bugs":   2_048,
        "max_out_sec":    1_024,
        "max_out_synth":  512,
        "max_out_summ":   512,
    },
    "3b": {
        "diff_budget":    8_000,
        "context_budget": 2_500,
        "max_out_bugs":   1_024,
        "max_out_sec":    512,
        "max_out_synth":  512,
        "max_out_summ":   512,
    },
}

# Fallback values used when a pass times out or fails
_FALLBACKS = {
    "summary": {
        "summary": "[Summary pass unavailable — analysis incomplete]",
        "pr_type": "mixed",
        "risk_assessment": "Unable to assess — summary pass failed or timed out.",
        "changed_files_summary": [],
    },
    "bugs": {"findings": []},
    "security": {"findings": []},
    "synthesis": {
        "risk_level": "unknown",
        "merge_recommendation": "needs_discussion",
        "confidence": 0.0,
        "rationale": "Analysis incomplete — one or more passes failed or timed out. Please review manually.",
    },
}

# Extended timeouts for more reliable analysis
_PASS_TIMEOUTS = {
    "summary":  180.0,  # 3 minutes
    "bugs":     240.0,  # 4 minutes
    "security": 180.0,  # 3 minutes
    "synthesis": 120.0, # 2 minutes
}


async def run_review() -> None:
    # ── Environment ──────────────────────────────────────────────────
    github_token = os.environ["GITHUB_TOKEN"]
    repo_slug    = os.environ["REPO"]
    pr_number    = int(os.environ["PR_NUMBER"])
    base_sha     = os.environ["BASE_SHA"]
    head_sha     = os.environ["HEAD_SHA"]
    model_size   = os.environ.get("MODEL_SIZE", "3b")
    runner_vcpus = os.environ.get("RUNNER_VCPUS", "2")

    cfg = _MODEL_CONFIG.get(model_size, _MODEL_CONFIG["3b"])

    t_start = time.monotonic()

    # ── GitHub objects ───────────────────────────────────────────────
    g       = Github(github_token)
    repo    = g.get_repo(repo_slug)
    pr      = repo.get_pull(pr_number)
    config  = load_config(".github/localreviewer.yml")
    system  = build_system_prompt(config)

    print(f"=== Ghost Review | PR #{pr_number} | model=qwen2.5-coder-{model_size} ===")

    # ── Diff preprocessing ───────────────────────────────────────────
    raw_diff             = get_diff(base_sha, head_sha)
    clean_diff, warnings = preprocess_diff(raw_diff, cfg["diff_budget"], config)

    if not clean_diff.strip():
        print("No reviewable diff after filtering. Skipping.")
        return

    changed_files = extract_changed_files(raw_diff)
    print(f"Changed files (after filter): {len(changed_files)}")
    print(f"Diff size: ~{len(clean_diff)//3} tokens")

    # ── Codebase context ─────────────────────────────────────────────
    context = build_codebase_context(changed_files, ".", cfg["context_budget"])

    # ── Track pass failures ──────────────────────────────────────────
    failed_passes = []

    async with LLMClient() as llm:
        # ── Pass 1: Summary ──────────────────────────────────────────
        print("[1/4] Summary pass...")
        summary, failed = await llm.chat_with_fallback(
            system=system,
            user=PROMPT_SUMMARY.format(
                title=pr.title,
                body=sanitize(pr.body or "", max_chars=1500),
                diff=clean_diff[:8000],
            ),
            schema=SUMMARY_SCHEMA,
            fallback_value=_FALLBACKS["summary"],
            max_tokens=cfg["max_out_summ"],
            temperature=0.3,
            timeout_seconds=_PASS_TIMEOUTS["summary"],
        )
        if failed:
            failed_passes.append("summary")
            warnings.append("⚠️ Summary pass failed or timed out — using fallback.")

        # ── Passes 2 + 3: Bugs and Security ─────────────────────────
        use_parallel = int(runner_vcpus) >= 4

        if use_parallel:
            print("[2+3/4] Bug detection + security scan (parallel)...")
            bugs_task = llm.chat_with_fallback(
                system=system,
                user=PROMPT_BUGS.format(context=context, diff=clean_diff),
                schema=FINDINGS_SCHEMA,
                fallback_value=_FALLBACKS["bugs"],
                max_tokens=cfg["max_out_bugs"],
                temperature=0.1,
                timeout_seconds=_PASS_TIMEOUTS["bugs"],
            )
            security_task = llm.chat_with_fallback(
                system=system,
                user=PROMPT_SECURITY.format(diff=clean_diff),
                schema=SECURITY_SCHEMA,
                fallback_value=_FALLBACKS["security"],
                max_tokens=cfg["max_out_sec"],
                temperature=0.1,
                timeout_seconds=_PASS_TIMEOUTS["security"],
            )
            (bugs, bugs_failed), (security, security_failed) = await asyncio.gather(
                bugs_task, security_task
            )
        else:
            print("[2/4] Bug detection...")
            bugs, bugs_failed = await llm.chat_with_fallback(
                system=system, user=PROMPT_BUGS.format(context=context, diff=clean_diff),
                schema=FINDINGS_SCHEMA, fallback_value=_FALLBACKS["bugs"],
                max_tokens=cfg["max_out_bugs"], temperature=0.1,
                timeout_seconds=_PASS_TIMEOUTS["bugs"],
            )
            print("[3/4] Security scan...")
            security, security_failed = await llm.chat_with_fallback(
                system=system, user=PROMPT_SECURITY.format(diff=clean_diff),
                schema=SECURITY_SCHEMA, fallback_value=_FALLBACKS["security"],
                max_tokens=cfg["max_out_sec"], temperature=0.1,
                timeout_seconds=_PASS_TIMEOUTS["security"],
            )

        if bugs_failed:
            failed_passes.append("bugs")
            warnings.append("⚠️ Bug detection pass failed or timed out.")
        if security_failed:
            failed_passes.append("security")
            warnings.append("⚠️ Security scan pass failed or timed out.")

        # ── Pass 4: Synthesis ────────────────────────────────────────
        print("[4/4] Synthesis...")
        
        # If critical passes failed, force synthesis to use fallback
        if len(failed_passes) >= 2:
            print(f"  WARNING: {len(failed_passes)} passes failed, using fallback synthesis.")
            synthesis = _FALLBACKS["synthesis"]
            synth_failed = True
        else:
            synthesis, synth_failed = await llm.chat_with_fallback(
                system=system,
                user=PROMPT_SYNTHESIS.format(
                    summary=json.dumps(summary),
                    bugs=json.dumps(bugs),
                    security=json.dumps(security),
                ),
                schema=SYNTHESIS_SCHEMA,
                fallback_value=_FALLBACKS["synthesis"],
                max_tokens=cfg["max_out_synth"],
                temperature=0.2,
                timeout_seconds=_PASS_TIMEOUTS["synthesis"],
            )
        
        if synth_failed:
            failed_passes.append("synthesis")
            warnings.append("⚠️ Synthesis pass failed or timed out.")

    # ── Merge and sort findings ───────────────────────────────────────
    SEV_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    all_findings = bugs.get("findings", []) + security.get("findings", [])

    # Filter out "none_found" security placeholder
    all_findings = [
        f for f in all_findings
        if f.get("vulnerability_class") != "none_found"
    ]

    # Apply min_severity filter from config
    min_sev = config.get("review", {}).get("min_severity", "warning")
    min_level = SEV_ORDER.get(min_sev, 2)
    all_findings = [
        f for f in all_findings
        if SEV_ORDER.get(f.get("severity", "info"), 3) <= min_level
    ]

    all_findings.sort(key=lambda f: SEV_ORDER.get(f.get("severity", "info"), 3))

    # ── Override risk if passes failed ───────────────────────────────
    # If analysis is incomplete, be conservative
    if failed_passes:
        # If synthesis worked but other passes failed, still flag as uncertain
        if "synthesis" not in failed_passes:
            # Keep synthesis result but reduce confidence
            synthesis["confidence"] = min(synthesis.get("confidence", 0.0), 0.3)
            if synthesis.get("risk_level") == "low":
                synthesis["risk_level"] = "medium"
            if synthesis.get("merge_recommendation") == "approve":
                synthesis["merge_recommendation"] = "needs_discussion"
        
        # Add note about failed passes
        if "rationale" in synthesis:
            synthesis["rationale"] = (
                f"⚠️ NOTE: Analysis incomplete ({len(failed_passes)} pass(es) failed). "
                + synthesis["rationale"]
            )

    # ── Format and post comment ──────────────────────────────────────
    elapsed = time.monotonic() - t_start
    comment = format_review_comment(
        summary=summary,
        all_findings=all_findings,
        verdict=synthesis,
        warnings=warnings,
        model_size=model_size,
        runner_vcpus=runner_vcpus,
        elapsed_seconds=elapsed,
        failed_passes=failed_passes,
    )

    await post_or_update_review_comment(pr, comment)

    # ── Step summary ─────────────────────────────────────────────────
    risk    = synthesis.get("risk_level", "unknown")
    rec     = synthesis.get("merge_recommendation", "unknown")
    elapsed_min = int(elapsed // 60)
    elapsed_sec = int(elapsed % 60)

    print(f"\n=== Done in {elapsed_min}m {elapsed_sec}s ===")
    print(f"Risk: {risk} | Recommendation: {rec} | Findings: {len(all_findings)}")
    if failed_passes:
        print(f"⚠️ Failed passes: {', '.join(failed_passes)}")

    summary_lines = [
        f"## Ghost Review — PR #{pr_number}",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| Risk | {risk} |",
        f"| Recommendation | {rec} |",
        f"| Findings | {len(all_findings)} |",
        f"| Time | {elapsed_min}m {elapsed_sec}s |",
        f"| Model | qwen2.5-coder-{model_size} |",
    ]
    if failed_passes:
        summary_lines.append(f"| ⚠️ Failed Passes | {len(failed_passes)} |")
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as fh:
        fh.write("\n".join(summary_lines) + "\n")


if __name__ == "__main__":
    asyncio.run(run_review())
