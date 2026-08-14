# LRR-AGENT-002 mini credit-capped bakeoff

This experiment compares four non-product custom-agent profiles. Nothing under
this directory is part of `.copilot`, the Windows payload, the managed product
agent list, or a release artifact.

The zero-credit gate is:

```powershell
python -B tools/experiments/agent_002_bakeoff/test_preflight.py
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py --self-test
```

Actual Copilot execution is deliberately opt-in. Use one new output directory
outside the Git worktree. Each invocation launches at most one case (one or two
user turns), then stops before any later case so the natural-language evidence
can be reviewed immediately:

The fixture keeps its RAG `USERPROFILE` isolated, while `COPILOT_HOME` points to
the caller's authenticated CLI home. The runner validates a recorded CLI login
before creating any prompt-ledger entry.

```powershell
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage1 `
  --output-root <new-artifact-directory> `
  --allow-metered-run

# When the result says AWAITING_HUMAN_REVIEW, inspect the answer and sealed
# summary. Copy the case's human-review-template.json to human-review.json and
# replace PENDING with PASS or FAIL. Re-run the exact same command. That call
# records the decision and launches no prompt. Re-run once more for the next
# case. Continue until reviewed-stage-summary.json exists.
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage1 `
  --output-root <same-artifact-directory> `
  --allow-metered-run

python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage2 `
  --output-root <same-artifact-directory> `
  --allow-metered-run

# Use the same per-case review checkpoint loop for Stage 2.
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage2 `
  --output-root <same-artifact-directory> `
  --allow-metered-run

python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage3 `
  --output-root <same-artifact-directory> `
  --allow-metered-run

# Use the same per-case review checkpoint loop for Stage 3. Only the reviewed
# summary may declare Mini Stable@2-Lite PASS and name a winner.
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage3 `
  --output-root <same-artifact-directory> `
  --allow-metered-run
```

The prompt ledger is written before every process launch and has a hard limit
of 24. A nonzero exit, unexpected tool, retry, wrong argv, altered `Q`, wrong DB,
or a human-reviewed unsupported answer immediately excludes that candidate
before its next case. The runner never retries a Copilot prompt. A review
checkpoint is bound to the case assessment and current ledger hash; replay,
drift, or a pending decision blocks prelaunch. All stage commands
for one bakeoff must use the same canonical output root; an exclusive runner
lock prevents concurrent ledger updates in that root.

This is a low-budget screen. Its result must not be described as a statistical
demonstration of 95% stability.
