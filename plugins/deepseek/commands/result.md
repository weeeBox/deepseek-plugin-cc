---
description: Show the output/verdict of a finished DeepSeek job
argument-hint: '<jobId>'
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" result $ARGUMENTS
```

Return the output verbatim. For an adversarial-review job the last `VERDICT:` line is the
ship/no-ship result. Do not fix any issues mentioned; this command only reports.
