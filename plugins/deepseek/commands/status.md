---
description: Show DeepSeek review jobs and their status
argument-hint: '[--json]'
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" status $ARGUMENTS
```

Return the output verbatim. A job whose worker pid has died with no terminal write shows
as `crashed`; read a finished job's output with `/deepseek:result <id>`.
