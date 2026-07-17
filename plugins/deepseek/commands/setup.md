---
description: Check whether the DeepSeek gate is ready (API key + claude CLI)
argument-hint: ''
allowed-tools: Bash(python3:*), AskUserQuestion
---

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek_companion.py" setup --json
```

Report the result to the user:
- `ready: true` means `DEEPSEEK_API_KEY` is set and the `claude` CLI is available.
- If `deepseek_api_key` is false, tell the user to add `DEEPSEEK_API_KEY=<key>` to their
  environment (or the repo `.env`), obtained from the DeepSeek Platform API keys page. The
  plugin passes it to `claude` as `ANTHROPIC_AUTH_TOKEN` against
  `https://api.deepseek.com/anthropic`.
- If `claude_cli` is false, tell the user to install the Claude Code CLI and ensure
  `claude` is on PATH.

Do not print the key value.
