"""
.github/scripts/github_api.py

GitHub REST API helpers:
  - Post or update the Ghost Review comment on a PR
  - Create a draft PR for Auto-Fix
  - Parse CODEOWNERS for automatic reviewer assignment
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from github import Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository


# Marker used to identify Ghost Review comments for upsert
_COMMENT_MARKER = "<!-- ghost-review-v1 -->"


def _get_github_client() -> Github:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN environment variable not set")
    return Github(token)


# ─────────────────────────────────────────────────────────────────────
# Review Comment Formatting
# ─────────────────────────────────────────────────────────────────────

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "error":    "🟠",
    "warning":  "🟡",
    "info":     "🔵",
}

_RISK_EMOJI = {
    "critical": "🔴 CRITICAL",
    "high":     "🟠 HIGH",
    "medium":   "🟡 MEDIUM",
    "low":      "🟢 LOW",
    "unknown":  "⚪ UNKNOWN",
}

_REC_EMOJI = {
    "approve":          "✅ Approve",
    "request_changes":  "❌ Request Changes",
    "needs_discussion": "💬 Needs Discussion",
}


def format_review_comment(
    summary: dict[str, Any],
    all_findings: list[dict[str, Any]],
    verdict: dict[str, Any],
    warnings: list[str],
    model_size: str,
    runner_vcpus: str,
    elapsed_seconds: float,
    failed_passes: list[str] | None = None,
) -> str:
    """
    Build the full markdown comment posted to the PR.
    """
    failed_passes = failed_passes or []
    lines: list[str] = [_COMMENT_MARKER]
    lines.append("## 👻 Ghost Review")
    lines.append("")

    # Verdict banner
    risk = verdict.get("risk_level", "unknown")
    rec = verdict.get("merge_recommendation", "needs_discussion")
    conf = verdict.get("confidence", 0.0)
    
    # Adjust display for incomplete analysis
    if failed_passes:
        if risk == "low":
            risk = "medium"  # Be conservative
        if rec == "approve":
            rec = "needs_discussion"
        conf = min(conf, 0.3)  # Reduce confidence
    
    lines.append(
        f"**Risk**: {_RISK_EMOJI.get(risk, risk.upper())}  "
        f"|  **Recommendation**: {_REC_EMOJI.get(rec, rec)}  "
        f"|  **Confidence**: {conf * 100:.0f}%"
    )
    lines.append("")
    
    # Warning banner if analysis incomplete
    if failed_passes:
        lines.append("> ⚠️ **Analysis Incomplete**: Some review passes failed or timed out. "
                     "Results may not be comprehensive. Please review manually.")
        lines.append("")
    
    lines.append("---")
    lines.append("")

    # Summary
    lines.append("### Summary")
    lines.append("")
    summary_text = summary.get("summary", "_No summary available._")
    if failed_passes and "summary" in failed_passes:
        summary_text = "_[Summary generation failed — analysis incomplete]_"
    lines.append(summary_text)
    lines.append("")
    
    risk_assess = summary.get("risk_assessment", "")
    if risk_assess and "summary" not in failed_passes:
        lines.append(f"**Risk assessment**: {risk_assess}")
        lines.append("")

    # Rationale
    rationale = verdict.get("rationale", "")
    if rationale:
        lines.append(f"**Verdict**: {rationale}")
        lines.append("")

    # Changed files table
    changed_files = summary.get("changed_files_summary", [])
    if changed_files and "summary" not in failed_passes:
        lines.append("### Changed Files")
        lines.append("")
        lines.append("| File | Change | Description |")
        lines.append("|------|--------|-------------|")
        for f in changed_files:
            lines.append(
                f"| `{f.get('file', '')}` "
                f"| {f.get('change_type', '')} "
                f"| {f.get('description', '')} |"
            )
        lines.append("")

    # Findings
    if all_findings:
        lines.append("### Findings")
        lines.append("")

        for finding in all_findings:
            sev = finding.get("severity", "info")
            emoji = _SEVERITY_EMOJI.get(sev, "•")
            finding_type = finding.get("type") or finding.get("vulnerability_class", "")
            file_ref = finding.get("file", "")
            line_start = finding.get("line_start")
            line_end = finding.get("line_end")

            # Build location string
            loc = ""
            if file_ref:
                loc = f"`{file_ref}`"
                if line_start:
                    loc += f" line {line_start}"
                    if line_end and line_end != line_start:
                        loc += f"–{line_end}"

            lines.append(
                f"**{emoji} [{sev.upper()}]** "
                f"{finding_type.replace('_', ' ').title()}"
                + (f" — {loc}" if loc else "")
            )
            lines.append("")
            desc = finding.get("description", "")
            if desc:
                lines.append(desc)
                lines.append("")

            fix = finding.get("suggested_fix", "")
            if fix:
                lines.append("<details><summary>Suggested fix</summary>")
                lines.append("")
                lines.append("```")
                lines.append(fix)
                lines.append("```")
                lines.append("</details>")
                lines.append("")

    else:
        lines.append("### Findings")
        lines.append("")
        if "bugs" in failed_passes or "security" in failed_passes:
            lines.append("⚠️ **Findings detection unavailable** — analysis pass(es) failed.")
        else:
            lines.append("✅ No significant issues found.")
        lines.append("")

    # Warnings (secret redaction, truncation notices)
    if warnings:
        lines.append("### ⚠️ Preprocessing Notices")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # Metadata footer (simplified - no token count)
    elapsed_min = int(elapsed_seconds // 60)
    elapsed_sec = int(elapsed_seconds % 60)
    lines.append("<details>")
    lines.append("<summary>Review metadata</summary>")
    lines.append("")
    
    status = "✅ Complete" if not failed_passes else f"⚠️ Incomplete ({len(failed_passes)} pass(es) failed)"
    
    lines.append(
        f"Model: `qwen2.5-coder-{model_size}-instruct-q4_k_m`  \n"
        f"Runner: `ubuntu-24.04-arm` {runner_vcpus}-vCPU  \n"
        f"Status: {status}  \n"
        f"Findings: {len(all_findings)}  \n"
        f"Time: {elapsed_min}m {elapsed_sec}s  \n"
        f"Ghost Review"
    )
    lines.append("</details>")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Comment Upsert (post or edit)
# ─────────────────────────────────────────────────────────────────────

async def post_or_update_review_comment(
    pr: PullRequest,
    comment_body: str,
) -> None:
    """
    Find an existing Ghost Review comment on the PR and edit it,
    or create a new one if none exists. Avoids comment spam on re-runs.
    """
    existing = None
    for comment in pr.get_issue_comments():
        if _COMMENT_MARKER in comment.body:
            existing = comment
            break

    if existing:
        existing.edit(comment_body)
        print(f"Updated existing review comment #{existing.id}")
    else:
        new_comment = pr.create_issue_comment(comment_body)
        print(f"Created new review comment #{new_comment.id}")


# ─────────────────────────────────────────────────────────────────────
# CODEOWNERS Parsing
# ─────────────────────────────────────────────────────────────────────

def _parse_codeowners(repo_path: str) -> list[str]:
    """
    Parse .github/CODEOWNERS or CODEOWNERS and return a list of
    GitHub usernames/team slugs (without the @ prefix).
    """
    owners: list[str] = []
    for candidate in [
        Path(repo_path) / ".github" / "CODEOWNERS",
        Path(repo_path) / "CODEOWNERS",
        Path(repo_path) / "docs" / "CODEOWNERS",
    ]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Format: pattern @owner1 @owner2
                parts = line.split()
                for part in parts[1:]:
                    if part.startswith("@"):
                        handle = part[1:]
                        # Skip team references (org/team) for simplicity
                        if "/" not in handle:
                            owners.append(handle)
            break

    return list(dict.fromkeys(owners))  # deduplicate


# ─────────────────────────────────────────────────────────────────────
# Draft PR Creation
# ─────────────────────────────────────────────────────────────────────

def create_draft_pr(
    repo: Repository,
    branch_name: str,
    base_branch: str,
    issue_number: int,
    issue_title: str,
    patch_summaries: list[dict[str, Any]],
    agent_thinking: str,
    confidence: float,
    repo_path: str,
) -> str:
    """
    Create a draft PR with full model reasoning.
    Returns the PR URL.

    The PR is ALWAYS draft. It never auto-merges. CODEOWNERS are assigned
    as requested reviewers. The body explicitly labels this as AI-generated.
    """
    # Build PR body
    body_lines: list[str] = [
        "<!-- ghost-review-autofix-v1 -->",
        "",
        "> ⚠️ **This PR was generated by AI (Ghost Review). "
        "Human review is required before merging.**",
        "",
        f"Closes #{issue_number}",
        "",
        "## What this PR does",
        "",
        f"Fixes: **{issue_title}**",
        "",
    ]

    if patch_summaries:
        body_lines += [
            "## Changes",
            "",
        ]
        for ps in patch_summaries:
            body_lines.append(f"- `{ps.get('file_path', '')}`: {ps.get('explanation', '')}")
        body_lines.append("")

    body_lines += [
        "## Model reasoning",
        "",
        "<details>",
        "<summary>Agent thinking trace</summary>",
        "",
        agent_thinking or "_No thinking trace available._",
        "",
        "</details>",
        "",
        f"**Patch confidence**: {confidence * 100:.0f}%",
        "",
        "---",
        "_Generated by [Ghost Review](https://github.com/ghost-review) · "
        f"Model: qwen2.5-coder-7b-instruct-q4_k_m_",
    ]

    body = "\n".join(body_lines)
    title = f"fix: {issue_title[:72]} (AI draft)"

    pr = repo.create_pull(
        title=title,
        body=body,
        head=branch_name,
        base=base_branch,
        draft=True,
    )
    print(f"Created draft PR #{pr.number}: {pr.html_url}")

    # Assign CODEOWNERS as reviewers (best-effort)
    try:
        owners = _parse_codeowners(repo_path)
        if owners:
            pr.create_review_request(reviewers=owners[:5])  # max 5
            print(f"Requested review from: {', '.join(owners[:5])}")
    except GithubException as exc:
        print(f"Could not assign reviewers: {exc}")

    return pr.html_url


# ─────────────────────────────────────────────────────────────────────
# Permission Checking
# ─────────────────────────────────────────────────────────────────────

def check_actor_permission(
    repo: Repository,
    username: str,
    required: str = "write",
) -> bool:
    """
    Check if username has at least required permission level on repo.
    Levels: read < triage < write < maintain < admin.
    """
    level_order = ["read", "triage", "write", "maintain", "admin"]
    try:
        permission = repo.get_collaborator_permission(username)
        actor_level = level_order.index(permission)
        required_level = level_order.index(required)
        return actor_level >= required_level
    except (GithubException, ValueError):
        return False
