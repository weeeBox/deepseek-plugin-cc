#!/usr/bin/env python3
"""deepseek-companion — the DeepSeek plugin runtime for Claude Code.

DeepSeek runs as an AGENTIC reviewer (DeepSeek's official Claude Code integration):
a headless `claude` process pointed at DeepSeek's Anthropic-compatible endpoint, given
the diff plus READ-ONLY repo access, so it investigates wiring/dead-code beyond the diff
the way Codex does. This companion owns the fail-closed verdict contract and job tracking;
the `claude` CLI owns transport (auth, API retries, streaming).

Subcommands:
  review            [--wait|--background] [--base <ref>] [--scope auto|working-tree|branch]
  adversarial-review [--wait|--background] [--base <ref>] [focus text...]
  setup             [--json]
  status            [--json]
  result            <jobId>
  cancel            <jobId>

Verdict states (adversarial-review; last `VERDICT:` line wins, unknown/absent -> BLOCK):
  SHIP | SHIP-WITH-CHANGES | BLOCK | OVERSIZE | ERROR
Fail-closed everywhere: any failure resolves to a non-pass verdict.

Zero third-party deps (stdlib only). `--selftest` runs the pure-unit regression suite
with no `claude` call and no API spend.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
CLAUDE_BIN = os.environ.get("DEEPSEEK_GATE_CLAUDE_BIN", "claude")
TOPUP_URL = "https://platform.deepseek.com/top_up"


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


# deepseek-chat / deepseek-reasoner are DEPRECATED 2026-07-24 (verified vs official docs
# 2026-07-16). deepseek-v4-pro = flagship (what claude-opus maps to); v4-flash = cheap tier.
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
MAX_DIFF_CHARS = _int_env("DEEPSEEK_MAX_DIFF_CHARS", 1_000_000)
REVIEW_TIMEOUT = _int_env("DEEPSEEK_TIMEOUT", 900)
VALID = {"SHIP", "SHIP-WITH-CHANGES", "BLOCK", "OVERSIZE", "ERROR"}

ADVERSARIAL = """You are an adversarial code reviewer acting as a ship/no-ship gate. You \
have READ-ONLY access to this repository (Read, Grep, Glob). Review {scope}.{focus}

Hunt the two integration-bug classes reviewers routinely miss, using your repo access to \
look BEYOND the diff:
1. wiring/dead-code: a module/function with correct logic that is never imported, \
registered, wired, or called - a shipped no-op. Grep for call sites of what the diff adds.
2. concurrency-path routing: a new lock/queue/idempotency layer one caller still bypasses.

Tie every finding to file:line. Do NOT modify any file. End your reply with the verdict on \
its own final line as bare text with NO markdown (no **bold**, no backticks): exactly \
'VERDICT: SHIP' or 'VERDICT: SHIP-WITH-CHANGES' or 'VERDICT: BLOCK'. BLOCK if any finding \
could ship a bug.

The diff under review:

{diff}
"""

PLAIN = """You are a code reviewer with READ-ONLY access to this repository (Read, Grep, \
Glob). Review {scope} for correctness, clarity, and maintainability. Tie findings to \
file:line. Do NOT modify any file.

The diff under review:

{diff}
"""


# ---- verdict engine (fail-closed) -----------------------------------------

# A verdict LINE begins with `VERDICT:` after optional markdown (`**VERDICT: SHIP**`,
# `` `VERDICT: BLOCK` ``, `## VERDICT: ...`, `> VERDICT: ...`). Anchoring at line-start is
# deliberate: an incidental `VERDICT: SHIP` mentioned mid-prose (a quoted example, a test
# note) is NOT a verdict line and must be ignored - otherwise a real `VERDICT: BLOCK`
# followed by such prose would false-SHIP, the worst outcome for a blocking gate.
# SHIP-WITH-CHANGES precedes SHIP; \b rejects partial words (SHIPPED).
_VERDICT_LINE_RE = re.compile(
    r"^[\s>*#`_-]*VERDICT:\s*(SHIP-WITH-CHANGES|SHIP|BLOCK|OVERSIZE|ERROR)\b",
    re.IGNORECASE)


def extract_verdict(reply):
    """Return the last verdict LINE that is OUTSIDE any code fence, so a quoted example
    verdict (a regression-test snippet showing `VERDICT: SHIP`) can never override the
    reviewer's real verdict. Fences are tracked by delimiter char AND length per CommonMark
    (a fence closes only on a same-char run >= the opener), so NESTED fences (``` inside an
    outer ````) are handled - the shorter inner delimiter is fence content, not a close.
    Absent/unknown, or a verdict only ever seen inside a fence -> BLOCK (fail-closed)."""
    verdict = "BLOCK"
    fence = None  # (char, length) of the open fence, or None when outside
    for line in reply.splitlines():
        # A line indented >= 4 spaces (or by a tab) is a Markdown indented code block:
        # content, never a real (unindented) verdict line - so an indented example verdict
        # cannot override the reviewer's real one. (Fence delimiters allow 0-3 lead spaces.)
        if line[:1] == "\t" or (len(line) - len(line.lstrip(" "))) >= 4:
            continue
        s = line.strip()
        fchar = "`" if s.startswith("```") else ("~" if s.startswith("~~~") else None)
        if fchar:
            run = len(s) - len(s.lstrip(fchar))
            rest = s[run:].strip()                         # text after the fence marker
            if fence is None:
                fence = (fchar, run)                       # open a fence (info string ok)
            elif fence[0] == fchar and run >= fence[1] and rest == "":
                fence = None                               # valid CLOSE: bare, same char, >=
            # anything else while a fence is open (shorter run, different char, OR a same-
            # length run with a trailing info string like ```python) is fence CONTENT: ignore
            continue
        if fence is not None:                              # inside a fence -> skip
            continue
        m = _VERDICT_LINE_RE.match(s)
        if m:
            verdict = m.group(1).upper()                   # last verdict OUTSIDE fences wins
    return verdict


def build_diff(base, scope):
    if scope == "branch" or (base and scope == "auto"):
        ref = base or "main"
        return subprocess.check_output(["git", "diff", f"{ref}...HEAD"], text=True)
    return (subprocess.check_output(["git", "diff", "HEAD"], text=True)
            + subprocess.check_output(["git", "diff", "--cached"], text=True))


def call_reviewer(prompt, run=subprocess.run):
    """Run headless `claude` on DeepSeek's endpoint (read-only). Returns text; raises
    on any failure so the caller fails closed."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    env = dict(os.environ)
    # Force routing to DeepSeek: strip EVERY inherited Anthropic var and provider toggle by
    # PATTERN (not a fixed name list - the CLI keeps adding CLAUDE_CODE_USE_* providers like
    # Bedrock/Vertex/Foundry/Gateway/Mantle), then set exactly the three DeepSeek ones. This
    # is comprehensive and future-proof: no stray provider env can re-route the review.
    for k in list(env):
        if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_"):
            env.pop(k, None)
    env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = key       # DeepSeek key, per the official integration
    env["ANTHROPIC_MODEL"] = MODEL
    # Read-only: --tools is the built-in tool ALLOWLIST (Read/Grep/Glob only - Write/Edit/
    # Bash are excluded from the model's context), and --strict-mcp-config blocks external
    # MCP servers so no MCP-provided tool can mutate the repo either. This scopes the
    # reviewer to the built-in read tools; it does not sandbox the OS beyond that.
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", MODEL,
           "--tools", "Read,Grep,Glob",
           "--strict-mcp-config",
           "--output-format", "text"]
    r = run(cmd, env=env, capture_output=True, text=True, timeout=REVIEW_TIMEOUT)
    if r.returncode != 0:
        # surface the TAIL of combined output - the real cause (e.g. "402 Insufficient
        # Balance") lands after the connector warning - but SCRUB the key first so it is
        # never relayed into logs (defense-in-depth; it is passed via env, not argv).
        detail = ((r.stderr or "") + (r.stdout or "")).strip()
        if key:
            detail = detail.replace(key, "***")
        raise RuntimeError(f"claude exited {r.returncode}: {detail[-300:]}")
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError("empty reviewer output")
    return out


def do_review(base, scope, adversarial, focus=""):
    """Returns (verdict, full_text). Fail-closed for adversarial; plain review returns
    (None, text). ANY failure -> ERROR with a canonical trailing verdict (adversarial)."""
    out = []
    try:
        diff = build_diff(base, scope)
        if not diff.strip():
            return _final(out, "ERROR", "empty diff - nothing to review", adversarial)
        if len(diff) > MAX_DIFF_CHARS:
            return _final(out, "OVERSIZE",
                          f"diff {len(diff)} chars > {MAX_DIFF_CHARS} cap", adversarial)
        scope_txt = (f"the branch diff `git diff {base or 'main'}...HEAD`"
                     if scope == "branch" or (base and scope == "auto")
                     else "the current working-tree and staged diff")
        tmpl = ADVERSARIAL if adversarial else PLAIN
        fx = f"\nExtra focus from the requester: {focus}\n" if focus else ""
        prompt = tmpl.format(scope=scope_txt, diff=diff, focus=fx)
        reply = call_reviewer(prompt)
    except Exception as e:  # broad catch is deliberate — a blocking gate never errors open
        return _final(out, "ERROR", _error_note(e), adversarial)
    out.append(reply.rstrip())
    if not adversarial:
        return None, "\n".join(out)
    return _final(out, extract_verdict(reply), None, True)


def _error_note(exc):
    """Turn an exception into a fail-closed ERROR note, with an actionable recommendation
    for the common, recoverable failure modes (chiefly DeepSeek insufficient balance)."""
    msg = str(exc)
    # Require HTTP/API context around the status code so an internal traceback line
    # number (e.g. "deepseek_companion.py:402") can't be mistaken for a 402 balance error.
    if re.search(r"(?:HTTP|API Error:?)\s*402|insufficient\s+balance", msg, re.IGNORECASE):
        return ("DeepSeek API returned HTTP 402 (insufficient balance): the DeepSeek "
                f"account has no credits, so no review ran. Recommendation: top up at "
                f"{TOPUP_URL}, then re-run the review. This is a human stop, not a code "
                "finding - nothing was reviewed.")
    if re.search(r"(?:HTTP|API Error:?)\s*401|\bunauthorized\b|authentication\s+failed"
                 r"|invalid.{0,20}(?:api\s*key|token)", msg, re.IGNORECASE):
        return ("DeepSeek API returned an auth error: DEEPSEEK_API_KEY looks missing or "
                "invalid. Recommendation: run /deepseek:setup, confirm the key is a valid "
                "DeepSeek Platform key, then re-run.")
    return f"{type(exc).__name__}: {msg}"


def _final(out, verdict, note, adversarial):
    if note:
        out.append(f"({note})")
    if adversarial:
        out.append(f"VERDICT: {verdict}")  # canonical trailing line callers read
    return (verdict if adversarial else None), "\n".join(out)


# ---- job state (per worktree root, mirrors codex/agy) ---------------------

def _state_dir():
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:
        root = os.getcwd()
    h = hashlib.sha1(root.encode()).hexdigest()[:12]
    d = os.path.join(os.path.expanduser("~"), ".deepseek-plugin-cc", h)
    os.makedirs(os.path.join(d, "jobs"), exist_ok=True)
    return d


def _try_state_dir():
    """_state_dir() but returns None instead of raising — job tracking must never crash
    a review (e.g. an unwritable ~/.deepseek-plugin-cc)."""
    try:
        return _state_dir()
    except Exception:
        return None


def _safe_write_job(sd, job):
    """Best-effort job record; swallow all IO errors. The gate result is the stdout
    VERDICT line + exit code, not the job file, so a failed write must not crash."""
    if not sd:
        return
    try:
        _write_job(sd, job)
    except Exception:
        pass


def _job_path(sd, jid):
    return os.path.join(sd, "jobs", f"{jid}.json")


def _write_job(sd, job):
    p = _job_path(sd, job["id"])
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".job.")
    with os.fdopen(fd, "w") as f:
        json.dump(job, f)
    os.replace(tmp, p)  # atomic


def _new_jid():
    return f"{int(time.time()*1000):x}-{os.urandom(3).hex()}"


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


# ---- subcommands ----------------------------------------------------------

def _parse_common(args):
    """Parse [--base <ref>] [--scope ...] + focus text. Fails closed (ValueError) if a
    flag is given without its value, so malformed gate input never silently uses a
    default. Backgrounding is NOT the companion's job - Claude Code's run_in_background
    detaches the whole call (see the command .md), so there is no --background/--wait."""
    def _val(flag):
        if flag not in args:
            return None
        i = args.index(flag)
        if i + 1 >= len(args) or args[i + 1].startswith("--"):
            raise ValueError(f"{flag} requires a value")
        return args[i + 1]
    base = _val("--base")
    scope = _val("--scope") or "auto"
    if scope not in ("auto", "working-tree", "branch"):
        raise ValueError(f"invalid --scope: {scope!r}")
    used = {"--base", base, "--scope", scope}
    focus = " ".join(a for a in args if a not in used and not a.startswith("--"))
    return base, scope, focus


def cmd_review(args, adversarial):
    """Always synchronous: records a 'running' job BEFORE the (slow) review so an
    immediate /deepseek:status sees it, then updates it to 'done'. Backgrounding is
    Claude Code's run_in_background on the command, not an internal fork."""
    try:
        base, scope, focus = _parse_common(args)
    except ValueError as e:  # malformed gate input -> fail closed, never a pass
        print(f"({e})\nVERDICT: ERROR" if adversarial else f"(error: {e})")
        return 1
    # Job tracking is best-effort and runs OUTSIDE the verdict path: it must never crash
    # the review. _try_state_dir/_safe_write_job swallow FS errors; do_review is itself
    # fail-closed, so the canonical VERDICT line always prints even if job IO fails.
    sd = _try_state_dir()
    jid = _new_jid()
    job = {"id": jid, "kind": "adversarial-review" if adversarial else "review",
           "status": "running", "pid": os.getpid(), "base": base, "scope": scope,
           "verdict": None, "started": int(time.time())}
    _safe_write_job(sd, job)  # exists immediately, before the review runs
    verdict, text = do_review(base, scope, adversarial, focus)
    job.update(status="done", verdict=verdict, output=text)
    _safe_write_job(sd, job)
    print(text)
    return 0 if (not adversarial or verdict == "SHIP") else 1


def cmd_status(args):
    sd = _state_dir()
    jobs_dir = os.path.join(sd, "jobs")
    rows = []
    for n in sorted(os.listdir(jobs_dir)):
        if not n.endswith(".json"):
            continue
        try:
            j = json.load(open(os.path.join(jobs_dir, n)))
        except Exception:
            continue
        st = j.get("status")
        if st == "running" and not _pid_alive(j.get("pid", -1)):
            st = "crashed"  # pid gone with no terminal write
        rows.append({"id": j.get("id"), "kind": j.get("kind"), "status": st,
                     "verdict": j.get("verdict")})
    if "--json" in args:
        print(json.dumps(rows))
    else:
        if not rows:
            print("No DeepSeek jobs.")
        for r in rows:
            print(f"{r['id']}  {r['kind']:18}  {r['status']:8}  {r['verdict'] or ''}")
    return 0


def cmd_result(args):
    if not args:
        print("usage: result <jobId>", file=sys.stderr)
        return 2
    sd = _state_dir()
    p = _job_path(sd, args[0])
    if not os.path.exists(p):
        print(f"no such job: {args[0]}", file=sys.stderr)
        return 2
    j = json.load(open(p))
    print(j.get("output") or f"(job {args[0]} status={j.get('status')}, no output yet)")
    # A plain review has no gate (exit 0). An adversarial job passes ONLY on VERDICT: SHIP;
    # a still-running / no-verdict / non-SHIP adversarial job must NOT read as a pass.
    if j.get("kind") == "adversarial-review":
        return 0 if j.get("verdict") == "SHIP" else 1
    return 0


def cmd_cancel(args):
    if not args:
        print("usage: cancel <jobId>", file=sys.stderr)
        return 2
    sd = _state_dir()
    p = _job_path(sd, args[0])
    if not os.path.exists(p):
        print(f"no such job: {args[0]}", file=sys.stderr)
        return 2
    j = json.load(open(p))
    if j.get("status") == "running" and _pid_alive(j.get("pid", -1)):
        try:
            os.kill(int(j["pid"]), 15)
        except Exception:
            pass
    j["status"] = "canceled"
    _write_job(sd, j)
    print(f"canceled {args[0]}")
    return 0


def cmd_setup(args):
    key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    claude_ok = tools_ok = False
    try:  # check --help: it proves the CLI runs AND that it supports the --tools flag the
        h = subprocess.run([CLAUDE_BIN, "--help"], capture_output=True, text=True,
                           timeout=15)  # reviewer relies on (an older CLI lacks it)
        claude_ok = h.returncode == 0
        tools_ok = "--tools" in (h.stdout or "")
    except Exception:
        claude_ok = tools_ok = False
    ready = key and claude_ok and tools_ok
    status = {"ready": ready, "deepseek_api_key": key, "claude_cli": claude_ok,
              "claude_tools_flag": tools_ok, "model": MODEL, "endpoint": ANTHROPIC_BASE_URL}
    if "--json" in args:
        print(json.dumps(status))
    else:
        print(f"DeepSeek gate ready: {ready}")
        print(f"  DEEPSEEK_API_KEY set: {key}")
        print(f"  claude CLI available: {claude_ok}")
        print(f"  claude supports --tools: {tools_ok}")
        print(f"  model: {MODEL}  endpoint: {ANTHROPIC_BASE_URL}")
        if not key:
            print("  -> add DEEPSEEK_API_KEY to your environment (or repo .env).")
        if not claude_ok:
            print("  -> install Claude Code CLI (`claude`) and ensure it is on PATH.")
        elif not tools_ok:
            print("  -> your claude CLI is too old (no --tools flag); upgrade Claude Code.")
    return 0 if ready else 1


# ---- selftest -------------------------------------------------------------

def selftest():
    for reply, want in [("VERDICT: SHIP", "SHIP"), ("x\nVERDICT: BLOCK", "BLOCK"),
                        ("VERDICT: ship", "SHIP"), ("VERDICT: SHIP\nVERDICT: BLOCK", "BLOCK"),
                        ("none", "BLOCK"), ("VERDICT: MAYBE", "BLOCK"),
                        ("VERDICT: SHIP-WITH-CHANGES", "SHIP-WITH-CHANGES"),
                        # markdown the model actually emits (the live-caught bug):
                        ("**VERDICT: SHIP**", "SHIP"),
                        ("`VERDICT: BLOCK`", "BLOCK"),
                        ("## VERDICT: SHIP-WITH-CHANGES", "SHIP-WITH-CHANGES"),
                        ("**VERDICT: SHIP-WITH-CHANGES**", "SHIP-WITH-CHANGES"),
                        ("findings\n**VERDICT: SHIP**\ntrailing note", "SHIP"),
                        ("VERDICT: SHIPPED soon", "BLOCK"),  # \b guards partial words
                        # CRITICAL: a real BLOCK line followed by prose that QUOTES a
                        # verdict must stay BLOCK - the anchored final-line scan ignores
                        # the mid-prose mention (was a false-SHIP with last-match-anywhere).
                        ("finding: parser scans prose.\n\nVERDICT: BLOCK\n\n"
                         "note: a later `VERDICT: SHIP` example is ignored.", "BLOCK"),
                        # a verdict only MENTIONED in prose (no verdict line) -> BLOCK
                        ("the reviewer should emit VERDICT: SHIP on its own line", "BLOCK"),
                        # a genuine final verdict line still wins over an earlier one
                        ("VERDICT: BLOCK\ntext\nVERDICT: SHIP", "SHIP"),
                        # trailing text on the verdict line is fine (anchored at start)
                        ("VERDICT: SHIP (all clear)", "SHIP"),
                        ("> VERDICT: BLOCK", "BLOCK"),  # blockquoted verdict line
                        # CRITICAL: a VERDICT inside a ``` code fence (a quoted example /
                        # regression-test snippet) must NOT override the real verdict.
                        ("Real bug.\n\nVERDICT: BLOCK\n\nRegression test to add:\n"
                         "```text\nVERDICT: SHIP\n```", "BLOCK"),
                        ("VERDICT: SHIP\n```\nexample: VERDICT: BLOCK\n```", "SHIP"),
                        # a verdict seen ONLY inside a fence -> fail-closed BLOCK
                        ("```\nVERDICT: SHIP\n```", "BLOCK"),
                        # NESTED fences: inner ``` inside outer ```` is content, so the
                        # fenced SHIP is skipped and the real BLOCK stands (round-3 residual)
                        ("VERDICT: BLOCK\n````text\n```\nVERDICT: SHIP\n```\n````", "BLOCK"),
                        # different-char nesting (``` inside ~~~)
                        ("VERDICT: BLOCK\n~~~\n```\nVERDICT: SHIP\n```\n~~~", "BLOCK"),
                        # 4-space indented code block example (round-4 residual)
                        ("Real bug.\n\nVERDICT: BLOCK\n\nExample:\n    VERDICT: SHIP",
                         "BLOCK"),
                        ("\tVERDICT: SHIP", "BLOCK"),          # tab-indented example
                        ("    VERDICT: SHIP", "BLOCK"),        # indent-only -> fail-closed
                        ("  VERDICT: SHIP", "SHIP"),           # <4 spaces is still a verdict
                        # a same-length run WITH an info string (```python) is NOT a close
                        # (CommonMark: a closing fence is bare) -> stays inside the fence, so
                        # the example SHIP is skipped and the real BLOCK stands (round-5).
                        ("VERDICT: BLOCK\n```\ntext\n```python\nVERDICT: SHIP\n```", "BLOCK"),
                        # a bare same-length run DOES close, so a following real verdict counts
                        ("```\nexample\n```\nVERDICT: SHIP", "SHIP")]:
        got = extract_verdict(reply)
        assert got == want, f"{reply!r} -> {got!r} want {want!r}"
    # canonical trailing verdict even after a raw model VERDICT line
    v, text = _final(["VERDICT: MAYBE"], extract_verdict("VERDICT: MAYBE"), None, True)
    assert v == "BLOCK" and text.splitlines()[-1] == "VERDICT: BLOCK", text
    # plain review returns no verdict, output preserved
    v2, t2 = _final(["some findings"], None, None, False)
    assert v2 is None and t2 == "some findings"
    assert _int_env("X_BAD_ENV", 5) == 5  # (env unset -> default; never crashes)
    # call_reviewer maps DeepSeek env + read-only tools, fails closed on missing key/nonzero
    os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        call_reviewer("p"); assert False
    except RuntimeError:
        pass
    os.environ["DEEPSEEK_API_KEY"] = "x"
    seen = {}

    class R:
        def __init__(s, rc, out): s.returncode, s.stdout, s.stderr = rc, out, ""

    def fake(cmd, env=None, capture_output=None, text=None, timeout=None):
        seen["cmd"], seen["env"] = cmd, env
        return R(0, "VERDICT: SHIP")
    assert call_reviewer("p", run=fake) == "VERDICT: SHIP"
    assert seen["env"]["ANTHROPIC_BASE_URL"] == ANTHROPIC_BASE_URL
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "x"
    # read-only is enforced by the --tools ALLOWLIST (only Read/Grep/Glob exist to the
    # model); no mutation tool is granted, and a stray Anthropic key is dropped so the
    # review always authenticates against DeepSeek.
    ti = seen["cmd"][seen["cmd"].index("--tools") + 1]
    assert ti == "Read,Grep,Glob", ti
    for t in ("Bash", "Write", "Edit", "NotebookEdit"):
        assert t not in ti
    assert "--strict-mcp-config" in seen["cmd"], "MCP tools must be blocked"
    # EVERY ANTHROPIC_* / CLAUDE_CODE_USE_* var is stripped by pattern (not a fixed list),
    # so no provider toggle can re-route the review; the three DeepSeek vars are then set.
    strays = {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_USE_BEDROCK": "1",
              "CLAUDE_CODE_USE_FOUNDRY": "1", "CLAUDE_CODE_USE_GATEWAY": "1",
              "ANTHROPIC_VERTEX_BASE_URL": "http://x"}
    os.environ.update(strays)
    call_reviewer("p", run=fake)
    for v in strays:
        assert v not in seen["env"], f"{v} must be dropped"
    assert seen["env"]["ANTHROPIC_BASE_URL"] == ANTHROPIC_BASE_URL
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "x"
    for v in strays:
        os.environ.pop(v, None)
    try:
        call_reviewer("p", run=lambda *a, **k: R(1, "")); assert False
    except RuntimeError:
        pass
    # the error path SCRUBS the key from surfaced output (no token in logs)
    os.environ["DEEPSEEK_API_KEY"] = "sk-secret-xyz"
    try:
        call_reviewer("p", run=lambda *a, **k: R(1, "boom near sk-secret-xyz end"))
        assert False
    except RuntimeError as e:
        assert "sk-secret-xyz" not in str(e) and "***" in str(e), str(e)
    os.environ["DEEPSEEK_API_KEY"] = "x"
    # focus parsing: non-flag leftovers become focus text (3-tuple, no background/wait)
    b, sc, fx = _parse_common(["--base", "main", "check", "the", "locks"])
    assert b == "main" and fx == "check the locks", (b, fx)
    # fail-closed on a flag given without its value
    for bad in (["--base"], ["--base", "--scope", "branch"], ["--scope", "bogus"]):
        try:
            _parse_common(bad); assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass
    # arg normalization: a single quoted "$ARGUMENTS" token is split; a pre-split list is
    # left intact; an unbalanced quote falls back to the raw list (never crashes).
    assert _normalize(["--base main check locks"]) == ["--base", "main", "check", "locks"]
    assert _normalize(["--base", "main"]) == ["--base", "main"]
    assert _normalize(['a "b']) == ['a "b']
    # job tracking is best-effort: a None or unwritable state dir must NOT raise, so it
    # can never crash the (fail-closed) verdict path.
    _safe_write_job(None, {"id": "x"})
    _safe_write_job("/nonexistent-root-dir/nope", {"id": "x", "status": "running"})
    # actionable ERROR notes: an insufficient-balance failure recommends the top-up link
    bal = _error_note(RuntimeError("claude exited 1: API Error: 402 Insufficient Balance"))
    assert TOPUP_URL in bal and "top up" in bal.lower() and "402" in bal, bal
    assert _error_note(RuntimeError("boom")) == "RuntimeError: boom"
    assert "setup" in _error_note(RuntimeError("401 unauthorized")).lower()
    # a bare traceback line number must NOT be mistaken for an HTTP 402/401 status
    assert "top up" not in _error_note(RuntimeError("bug at file.py:402 in foo")).lower()
    assert _error_note(RuntimeError("crash at parser.py:401")) == \
        "RuntimeError: crash at parser.py:401"
    print("selftest OK")


def _normalize(rest):
    """Claude passes "$ARGUMENTS" as ONE quoted token; split it into an argv. A pre-split
    list round-trips unchanged; an unbalanced quote falls back to the raw list."""
    try:
        return shlex.split(" ".join(rest)) if rest else []
    except ValueError:
        return rest


COMMANDS = {"review": lambda a: cmd_review(a, False),
            "adversarial-review": lambda a: cmd_review(a, True),
            "status": cmd_status, "result": cmd_result,
            "cancel": cmd_cancel, "setup": cmd_setup}

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        selftest()
        sys.exit(0)
    if not argv or argv[0] not in COMMANDS:
        print("usage: deepseek_companion.py "
              "{review|adversarial-review|setup|status|result|cancel} [...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(COMMANDS[argv[0]](_normalize(argv[1:])))
