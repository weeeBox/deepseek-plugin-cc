---
description: Run a DeepSeek adversarial ship/no-ship review against local git state
argument-hint: '[--base <ref>] [--scope auto|working-tree|branch] [focus text]'
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(python3:*), Bash(git:*), AskUserQuestion
---

Run a DeepSeek adversarial review through the plugin companion. DeepSeek reviews as a
read-only agent (it can Grep/Read beyond the diff to catch wiring/dead-code) and returns a
`VERDICT: SHIP | SHIP-WITH-CHANGES | BLOCK` line.

Raw slash-command arguments:
`$ARGUMENTS`

Core constraint:
- Review-only. Do not fix issues, apply patches, or imply you are about to make changes.
  Return the companion's output verbatim. The last `VERDICT:` line is the gate result.

The companion always runs synchronously and records a job; **backgrounding is Claude
Code's job**, not a companion flag. Decide foreground vs background by review size:
- Estimate with `git diff --shortstat` for the relevant scope. For anything larger than
  ~1-2 files (or unclear), prefer background. Use `AskUserQuestion` exactly once with
  `Wait for results` and `Run in background` (recommended-first) unless the user already
  implied one.

Foreground:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" adversarial-review "$ARGUMENTS"
```
Return stdout verbatim.

Background — launch the SAME command with `Bash` (`run_in_background: true`); do not add any
`--background` flag and do not wait for it this turn:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" adversarial-review "$ARGUMENTS"
```
Then tell the user: "DeepSeek review started in the background. Check `/deepseek:status`,
read the verdict with `/deepseek:result <id>`."

Notes:
- Non-flag tokens are passed as extra review focus. `--base`/`--scope` require a value.
- The companion fails closed: any error, timeout, missing key, malformed argument, or
  unparseable reply resolves to `VERDICT: ERROR` (a human stop) - never a silent pass.
