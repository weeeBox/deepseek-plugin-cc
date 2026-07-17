# deepseek-plugin-cc

A Claude Code plugin that runs **DeepSeek** as an agentic code reviewer — a review peer to
the Codex and Antigravity plugins (the `deepseek-rescue` subagent lets the main thread
delegate a review to DeepSeek; general task delegation is out of scope). Built to mirror
[`openai/codex-plugin-cc`](https://github.com/openai/codex-plugin-cc)'s structure, minus
the app-server broker (DeepSeek needs no persistent server: each review is one stateless,
read-only headless `claude` run pointed at DeepSeek's Anthropic-compatible endpoint).

## How it works

DeepSeek reviews via its official [Claude Code integration](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/):
the companion launches a headless `claude` process with
`ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` and
`ANTHROPIC_AUTH_TOKEN=$DEEPSEEK_API_KEY`, model `deepseek-v4-pro`, restricted to
**read-only** tools (`Read,Grep,Glob`). Because it is agentic, it can Grep/Read beyond the
diff to catch the two bug classes reviewers miss — wiring/dead-code and concurrency-path
routing — then emit a machine-parseable `VERDICT: SHIP | SHIP-WITH-CHANGES | BLOCK`.

The companion owns a **fail-closed** verdict contract: any error, timeout, missing key, or
unparseable reply resolves to `VERDICT: ERROR` (a human stop) or `BLOCK` — never a silent
pass. It is safe to use as a blocking ship/no-ship gate.

## Install

```
/plugin marketplace add <path-to>/deepseek-plugin-cc
/plugin install deepseek@deepseek
/deepseek:setup
```

Set `DEEPSEEK_API_KEY` (from the DeepSeek Platform API keys page) in your environment or the
repo's `.env`. Requires the `claude` CLI on PATH.

## Commands

| Command | Purpose |
|---------|---------|
| `/deepseek:adversarial-review [--wait\|--background] [--base <ref>] [focus]` | Ship/no-ship gate — returns a `VERDICT` line. |
| `/deepseek:review [--wait\|--background] [--base <ref>] [--scope ...]` | Plain read-only review (no verdict). |
| `/deepseek:setup` | Check `DEEPSEEK_API_KEY` + `claude` CLI readiness. |
| `/deepseek:status [--json]` | List jobs and their status. |
| `/deepseek:result <jobId>` | Show a finished job's output/verdict. |
| `/deepseek:cancel <jobId>` | Cancel a running background job. |

Subagent `deepseek:deepseek-rescue` forwards an independent review/verdict request to the
companion.

## Env knobs

| Var | Default | Meaning |
|-----|---------|---------|
| `DEEPSEEK_API_KEY` | — | Required. Passed to `claude` as `ANTHROPIC_AUTH_TOKEN`. |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | `deepseek-v4-flash` for a ~30x-cheaper run. (`deepseek-chat`/`deepseek-reasoner` deprecate 2026-07-24.) |
| `DEEPSEEK_MAX_DIFF_CHARS` | `1000000` | OVERSIZE guard (v4 window is 1M tokens). |
| `DEEPSEEK_TIMEOUT` | `900` | Per-review timeout (seconds); an agent loop is slower than one API call. |
| `DEEPSEEK_GATE_CLAUDE_BIN` | `claude` | Override if `claude` is not on PATH. |

## Development

```
python3 plugins/deepseek/scripts/deepseek_companion.py --selftest   # pure-unit suite, no API
python3 plugins/deepseek/tests/test_companion.py                    # same, as a test runner
```

## License

MIT.
