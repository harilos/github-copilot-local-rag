# LRR-AGENT-002 mini credit-capped bakeoff

This experiment compares four non-product custom-agent profiles. Nothing under
this directory is part of `.copilot`, the Windows payload, the managed product
agent list, or a release artifact.

The zero-credit gate is:

```powershell
python -B tools/experiments/agent_002_bakeoff/test_preflight.py
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py --self-test
```

Actual Copilot execution is deliberately opt-in. Use a new output directory
outside the Git worktree and run the stages separately so the natural-language
evidence check can be reviewed between stages:

```powershell
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage1 `
  --output-root <new-artifact-directory> `
  --allow-metered-run

# Copy human-review-template.json to human-review.json, inspect every answer,
# and replace each PENDING status with PASS or FAIL before finalizing.
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage1 `
  --output-root <same-artifact-directory> `
  --finalize-review

python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage2 `
  --output-root <same-artifact-directory> `
  --allow-metered-run

# Complete and finalize the Stage 2 human review in the same way.
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage2 `
  --output-root <same-artifact-directory> `
  --finalize-review

python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage3 `
  --output-root <same-artifact-directory> `
  --allow-metered-run

# Complete and finalize the Stage 3 human review. Only the reviewed summary may
# declare Mini Stable@2-Lite PASS and name a winner.
python -B tools/experiments/agent_002_bakeoff/run_bakeoff.py `
  --stage stage3 `
  --output-root <same-artifact-directory> `
  --finalize-review
```

The prompt ledger is written before every process launch and has a hard limit
of 24. A nonzero exit, unexpected tool, retry, wrong argv, altered `Q`, wrong DB,
or unsupported answer immediately excludes that candidate from the rest of the
current stage. The runner never retries a Copilot prompt. All stage commands
for one bakeoff must use the same canonical output root; an exclusive runner
lock prevents concurrent ledger updates in that root.

This is a low-budget screen. Its result must not be described as a statistical
demonstration of 95% stability.
