---
description: Cancel a running DeepSeek background job
argument-hint: '<jobId>'
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" cancel $ARGUMENTS
```

Return the output verbatim.
