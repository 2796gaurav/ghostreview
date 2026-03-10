# 👻 Ghost Review

**Zero-Cost, Privacy-First AI Code Review & Auto-PR via GitHub Actions**

Ghost Review performs AI-powered code review on pull requests and can automatically generate fix patches for issues — all running entirely within GitHub Actions, with zero external API calls and complete privacy.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

---

## 🚀 Features

### PR Review
- **4-Pass Analysis**: Summary, Bug Detection, Security Scan, and Final Synthesis
- **Grammar-Constrained Output**: 100% reliable structured JSON output
- **Parallel Processing**: Bug detection and security scans run concurrently on 4-vCPU runners
- **Smart Context Building**: Automatically includes imported modules, callers, and test files
- **Secret Redaction**: Automatically detects and redacts secrets from diffs before LLM ingestion

### Auto-Fix (Auto-PR)
- **Agentic Loop**: Explores codebase, reads files, generates patches
- **Self-Verification**: Low-confidence patches undergo independent verification
- **Safety Gates**: Hard-coded protections for protected paths, max file limits, confidence thresholds
- **Always Draft**: Auto-generated PRs are always created as drafts with clear AI attribution

---

## 📋 Requirements

- **Public Repositories**: Free `ubuntu-24.04-arm` runner (4-vCPU, 16 GB RAM)
- **Private Repositories**: Free `ubuntu-24.04-arm` runner (2-vCPU, 8 GB RAM) — uses 3B fallback model
- **No API Keys Required**: Runs entirely on GitHub-hosted ARM64 runners

---

## 🛠️ Quick Start

### 1. Add the Workflow Files

Copy the workflow files from `.github/workflows/` to your repository:

```bash
mkdir -p .github/workflows
cp ghostreview/.github/workflows/ghost-review-pr.yml .github/workflows/
cp ghostreview/.github/workflows/ghost-review-autofix.yml .github/workflows/
```

### 2. Add the Scripts

Copy the scripts directory:

```bash
cp -r ghostreview/.github/scripts .github/
```

### 3. Configure (Optional)

Create `.github/localreviewer.yml` to customize behavior:

```yaml
model:
  size: auto  # auto | 7b | 3b

review:
  passes: [summary, bugs, security]
  ignore_paths:
    - "*.lock"
    - "dist/**"
    - "build/**"
  security_critical_paths:
    - "src/auth/**"
    - "src/payment/**"
  min_severity: warning
  always_comment: true

auto_fix:
  enabled: true
  trigger_label: "auto-fix"
  trigger_comment: "/fix"
  required_permission: write
  confidence_threshold: 0.70
  max_files: 5
  protected_paths:
    - ".github/**"
    - "*.yml"
    - "*.yaml"
    - "Dockerfile"
    - "Makefile"

conventions:
  language: python
  framework: fastapi
  notes: |
    All database queries must use parameterized statements.
    Authentication is handled by AuthMiddleware.
```

### 4. Enable Permissions

Go to **Settings → Actions → General** in your repository and ensure:
- **Workflow permissions**: Read and write permissions

---

## 🧠 How It Works

### Architecture

```
GitHub Event (PR / Issue)
    ↓
GitHub Actions Runner (ARM64, ephemeral)
    ↓
llama.cpp server (Qwen2.5-Coder, local)
    ↓
Grammar-Constrained JSON Output
    ↓
PR Comment / Draft PR
```

### Model Selection

| Runner | Model | Context | Speed |
|--------|-------|---------|-------|
| 4-vCPU / 16 GB (Public) | Qwen2.5-Coder-7B Q4_K_M | 65K tokens | ~6-9 tok/s |
| 2-vCPU / 8 GB (Private) | Qwen2.5-Coder-3B Q4_K_M | 32K tokens | ~8-12 tok/s |

### Cache Strategy

Three independent caches ensure fast warm starts:
1. **LLM Models** (~6.8 GB combined) — 7B + 3B models
2. **llama.cpp Binary** (~30 MB) — pre-built for ARM64
3. **Python Packages** (~80 MB) — pinned dependencies

---

## 🔒 Security & Privacy

- **Zero Data Exfiltration**: All processing happens on the GitHub Actions runner
- **No API Keys**: Uses local LLM inference via llama.cpp
- **Secret Redaction**: Automatically redacts API keys, tokens, passwords from diffs
- **Protected Paths**: CI/CD, infrastructure, and workflow files cannot be modified by Auto-Fix
- **Permission Gates**: Auto-Fix requires write permissions

---

## 📝 Usage

### PR Review

Reviews happen automatically on:
- Pull request opened
- Pull request synchronized (new commits)
- Pull request marked ready for review

### Auto-Fix

Trigger auto-fix in two ways:

1. **Label an issue** with `auto-fix`
2. **Comment `/fix`** on an issue (requires write permission)

The bot will:
1. Analyze the issue
2. Explore the codebase
3. Generate a patch
4. Create a **draft PR** with the fix

---

## ⚙️ Configuration Reference

### Model Size Override

Force a specific model size in the workflow:

```yaml
env:
  MODEL_SIZE_OVERRIDE: "7b"  # or "3b"
```

### Custom llama.cpp Version

Pin to a specific llama.cpp build:

```yaml
env:
  LLAMA_CPP_VERSION: "b8252"
```

---

## 📊 Performance

| Configuration | Cold Start | Warm Cache | End-to-End Review |
|--------------|------------|------------|-------------------|
| 7B / 4-vCPU | ~2:30 | ~2:30 | ~2:00-2:30 |
| 3B / 2-vCPU | ~1:45 | ~1:45 | ~1:30-2:00 |

*Cache restoration (~20s) approximately equals download time on cold start.*

---

## 🤝 Contributing

Contributions are welcome! Please ensure:
1. Code follows the existing style
2. Shell scripts use `set -euo pipefail`
3. Python code is type-hinted
4. All safety gates remain non-configurable

---

## 📄 License

Apache License 2.0 — See [LICENSE](LICENSE) for details.

The primary model (Qwen2.5-Coder-7B) is Apache 2.0 licensed.
The fallback model (Qwen2.5-Coder-3B) uses the Qwen-Research License.

---

## 🙏 Acknowledgments

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — High-performance LLM inference
- [Qwen](https://github.com/QwenLM/Qwen) — Qwen2.5-Coder models
- [PyGithub](https://github.com/PyGithub/PyGithub) — GitHub API client

---

**Developed by Gaurav Chauhan** © 2026
