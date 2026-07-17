---
description: Run a DeepSeek read-only code review against local git state
argument-hint: '[--base <ref>] [--scope auto|working-tree|branch]'
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(python3:*), Bash(git:*), AskUserQuestion
---

Run a plain (non-gate) DeepSeek code review through the plugin companion. DeepSeek reviews
as a read-only agent and returns findings; unlike `/deepseek:adversarial-review` it does
not emit a ship/no-ship `VERDICT` line.

Raw slash-command arguments:
`$ARGUMENTS`

Core constraint:
- Review-only. Do not fix issues or imply you are about to. Return stdout verbatim.

The companion runs synchronously and records a job; backgrounding is Claude Code's job.
Estimate size with `git diff --shortstat` for the scope and use `AskUserQuestion` once
(`Wait for results` / `Run in background`, recommended-first) for anything beyond ~1-2 files.

Foreground:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" review "$ARGUMENTS"
```

Background — the SAME command with `Bash` (`run_in_background: true`), no extra flag:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" review "$ARGUMENTS"
```
Then: "DeepSeek review started in the background. Check `/deepseek:status`, read
`/deepseek:result <id>`."

For a steerable, adversarial ship/no-ship gate, use `/deepseek:adversarial-review` instead.
