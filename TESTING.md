# Ghost Review Testing Guide

This document explains how to test both the PR Review and Auto-Fix features.

---

## Testing PR Review

PR Review is triggered automatically when a pull request is opened or updated.

### Test Branch: `test/pr-review-bugs-v3`

This branch contains `test_app/calculator.py` with intentional bugs for testing.

### How to Test

1. **Create the PR** (one-time setup):
   ```bash
   # Go to GitHub and create a PR:
   # https://github.com/2796gaurav/ghostreview/compare/main...test/pr-review-bugs-v3
   # 
   # Or use gh CLI:
   gh pr create --base main --head test/pr-review-bugs-v3 \
     --title "TEST: PR Review with intentional bugs" \
     --body "This PR tests the Ghost Review PR analysis feature. It contains intentional security issues and bugs that should be detected."
   ```

2. **Trigger the Review**:
   - The review runs automatically when the PR is opened
   - It also runs on new commits (synchronize event)
   - You can manually re-run from the Actions tab

3. **Expected Results**:
   The review should detect these issues in `test_app/calculator.py`:
   - **Critical**: Hardcoded secrets (API_KEY, DATABASE_PASSWORD)
   - **Critical**: `eval()` on user input (code injection)
   - **Critical**: SQL injection in `get_user_data()`
   - **Critical**: Command injection via `subprocess.check_output(shell=True)`
   - **Critical**: Insecure deserialization with `pickle.load()`
   - **Error**: Division by zero in `divide()`
   - **Error**: List modification during iteration in `process_list()`
   - **Error**: Empty list handling in `find_max()`
   - **Warning**: Infinite recursion in `recursive_function()`
   - **Warning**: Plaintext password storage
   - **Warning**: No timeout in `fetch_data()`

4. **Verify the Comment**:
   - Check that a comment is posted on the PR
   - Verify risk level is "critical" or "high"
   - Confirm security findings are listed
   - Check that line-specific comments are added

---

## Testing Auto-Fix

Auto-Fix is triggered by issues, not PRs. It generates draft PRs to fix reported issues.

### How to Test

Auto-Fix requires **write permission** and the **7B model**.

#### Method 1: Using `/fix` Comment

1. **Create a test issue** on GitHub:
   ```markdown
   Title: Bug: Division by zero in calculator
   
   Body:
   The `divide` function in test_app/calculator.py doesn't handle division by zero.
   
   ```python
   def divide(a, b):
       return a / b  # This will crash if b is 0
   ```
   
   Should add a check for zero before dividing.
   ```

2. **Comment `/fix`** on the issue

3. **The workflow triggers** and:
   - Checks your permission level (requires write access)
   - Starts the 7B model inference server
   - Runs the agentic fix loop
   - Creates a draft PR if successful

#### Method 2: Using Labels

1. **Create an issue** describing a bug

2. **Add label** `auto-fix` to the issue

3. **The workflow triggers** automatically

### Expected Behavior

1. **Permission Check**: If you don't have write access, it posts a comment and exits
2. **Model Check**: If running 3B model, it posts a warning and exits
3. **Working Indicator**: Posts "🤖 Auto-Fix analyzing..." comment
4. **Success**: Creates a draft PR with:
   - Title: `fix: <issue title> (AI draft)`
   - Full explanation in body
   - Thinking trace in collapsible section
   - Confidence score badge
5. **Failure**: Posts comment explaining why it couldn't fix

### Test Issues to Create

#### Test 1: Simple Division by Zero
```markdown
Title: Fix division by zero in calculator.py

The divide() function crashes on zero input. Add a check to return None 
or raise a proper exception when dividing by zero.

File: test_app/calculator.py
```

#### Test 2: Add Timeout to fetch_data
```markdown
Title: Add timeout to fetch_data function

test_app/calculator.py's fetch_data() function uses urlopen without a timeout.
This could hang indefinitely. Add a timeout parameter.
```

#### Test 3: Remove eval() usage
```markdown
Title: Replace eval() with safer alternative in calculate()

The calculate() function uses eval() which is dangerous. Replace with 
ast.literal_eval or a proper math expression parser.
```

---

## Workflow Dispatch (Manual Testing)

### PR Review Manual Run

You can manually trigger PR review from the Actions tab:

1. Go to **Actions** → **Ghost Review — PR Analysis**
2. Click **Run workflow**
3. Enter a PR number
4. Click **Run workflow**

### Checking Logs

To debug issues:

1. Go to the Actions run
2. Look for these key steps:
   - **Detect runner configuration** - Shows model selection (7b/3b)
   - **Cache llama.cpp binary** - Should show cache miss on first run
   - **Build llama.cpp** - Shows static linking build
   - **Start inference server** - Shows llama-server startup
   - **Run PR review** - Shows the 4-pass analysis

3. Expand logs to see:
   - Token counts
   - Pass timings
   - Any errors or timeouts

### Fixing Shared Library Errors

If you see:
```
error while loading shared libraries: libmtmd.so.0: cannot open shared object file
```

The cache needs to be cleared. The build script has been updated to use static linking.
To clear the cache:
1. Go to **Actions** → **Caches**
2. Find `ghost-review-llama-*` cache
3. Delete it
4. Re-run the workflow

---

## Performance Expectations

### PR Review (7B model, 4-vCPU)
- Small PR (<1000 lines): 2-4 minutes
- Medium PR (1000-5000 lines): 4-8 minutes  
- Large PR (>5000 lines): 8-15 minutes (with chunking)

### Auto-Fix (7B model, 4-vCPU)
- Simple fix (1 file): 3-5 minutes
- Complex fix (2-3 files): 5-10 minutes
- Multi-file fix (4-5 files): 10-15 minutes

---

## Troubleshooting

### "There isn't anything to compare"
This means the branch is identical to main. The test branch has changes now.

### Auto-fix not triggering
- Check you have write permission to the repo
- Ensure the issue is not a PR (auto-fix only works on issues)
- Verify the `/fix` command is on its own line

### Model timeout errors
- Large diffs may timeout - the system now uses adaptive chunking
- If a pass times out, the review continues with partial results
- Check the comment for "Analysis Incomplete" warnings

### Server startup failures (libmtmd.so.0 not found)
- This was caused by shared library dependencies
- **Solution**: The build script now uses `-DBUILD_SHARED_LIBS=OFF`
- Clear the llama-bin cache and re-run
- New cache key: `ghost-review-llama-b8252-static-ubuntu-arm64-v3`

### Build cache not updating
- The cache key includes version numbers
- Updating the key forces a fresh build
- Current key: `ghost-review-llama-b8252-static-ubuntu-arm64-v3`

### Fixed Issues (for reference)

These issues have been fixed in the current version:

1. **`AttributeError: 'LLMClient' object has no attribute '_circuit_breaker'`**
   - Fixed: Typo in attribute name (`circuit_breaker` vs `_circuit_breaker`)
   
2. **`error while loading shared libraries: libmtmd.so.0`**
   - Fixed: Build now uses `-DBUILD_SHARED_LIBS=OFF` for static linking

3. **Token count showing 0/0 in metadata**
   - Fixed: Removed placeholder token counts from display

4. **Auto-fix giving up too early**
   - Fixed: Added better ReAct prompts and exploration strategy

---

## Algorithmic Improvements Tested

The recent update includes these algorithmic improvements:

1. **Aho-Corasick Secret Detection**: O(n) instead of O(n*m) for secret scanning
2. **Adaptive Diff Chunking**: Large diffs are split intelligently
3. **Token-aware Truncation**: Uses tiktoken for accurate token counting
4. **Circuit Breaker**: Fault tolerance for LLM server failures
5. **ReAct Pattern**: Agent explores → analyzes → patches → verifies
6. **Reflection**: Low-confidence patches get second-pass verification
7. **Static Linking**: Self-contained binary without shared library dependencies

Verify these by checking the Action logs for:
- "Compressed X repetitive hunks" (compression working)
- "Split into N chunk(s)" (chunking working)
- "[explore]" / "[analyze]" / "[patch]" phase indicators (ReAct working)
- "✓ Static build verified" (static linking working)
