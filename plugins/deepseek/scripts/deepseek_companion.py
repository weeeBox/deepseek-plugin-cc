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
import shlex
import subprocess
import sys
import tempfile
import time

ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
CLAUDE_BIN = os.environ.get("DEEPSEEK_GATE_CLAUDE_BIN", "claude")


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

Tie every finding to file:line. Do NOT modify any file. End your reply with exactly one \
line: 'VERDICT: SHIP' or 'VERDICT: SHIP-WITH-CHANGES' or 'VERDICT: BLOCK'. BLOCK if any \
finding could ship a bug.

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

def extract_verdict(reply):
    """Last recognized VERDICT: line wins; unknown/absent -> BLOCK (fail-closed)."""
    verdict = "BLOCK"
    for line in reply.splitlines():
        s = line.strip()
        if s.upper().startswith("VERDICT:"):
            cand = s.split(":", 1)[1].strip().upper()
            if cand in VALID:
                verdict = cand
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
    env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = key       # DeepSeek key, per the official integration
    env["ANTHROPIC_MODEL"] = MODEL
    # Read-only enforcement: --allowedTools only AUTO-APPROVES (so reads run unattended);
    # --disallowedTools is what actually REMOVES a tool from the model's context. List
    # every mutation/exfil/spawn tool there so the reviewer genuinely cannot modify the
    # repo, shell out, or fan out. (Confirmed via Claude Code CLI docs.)
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", MODEL,
           "--allowedTools", "Read,Grep,Glob",
           "--disallowedTools",
           "Bash,Write,Edit,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task",
           "--output-format", "text"]
    r = run(cmd, env=env, capture_output=True, text=True, timeout=REVIEW_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {(r.stderr or '')[:200]}")
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
        return _final(out, "ERROR", f"{type(e).__name__}: {e}", adversarial)
    out.append(reply.rstrip())
    if not adversarial:
        return None, "\n".join(out)
    return _final(out, extract_verdict(reply), None, True)


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
    claude_ok = False
    try:
        claude_ok = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True,
                                    timeout=10).returncode == 0
    except Exception:
        claude_ok = False
    ready = key and claude_ok
    status = {"ready": ready, "deepseek_api_key": key, "claude_cli": claude_ok,
              "model": MODEL, "endpoint": ANTHROPIC_BASE_URL}
    if "--json" in args:
        print(json.dumps(status))
    else:
        print(f"DeepSeek gate ready: {ready}")
        print(f"  DEEPSEEK_API_KEY set: {key}")
        print(f"  claude CLI available: {claude_ok}")
        print(f"  model: {MODEL}  endpoint: {ANTHROPIC_BASE_URL}")
        if not key:
            print("  -> add DEEPSEEK_API_KEY to your environment (or repo .env).")
        if not claude_ok:
            print("  -> install Claude Code CLI (`claude`) and ensure it is on PATH.")
    return 0 if ready else 1


# ---- selftest -------------------------------------------------------------

def selftest():
    for reply, want in [("VERDICT: SHIP", "SHIP"), ("x\nVERDICT: BLOCK", "BLOCK"),
                        ("VERDICT: ship", "SHIP"), ("VERDICT: SHIP\nVERDICT: BLOCK", "BLOCK"),
                        ("none", "BLOCK"), ("VERDICT: MAYBE", "BLOCK"),
                        ("VERDICT: SHIP-WITH-CHANGES", "SHIP-WITH-CHANGES")]:
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
    assert "Read,Grep,Glob" in seen["cmd"]
    # read-only is enforced by --disallowedTools (removes tools from context), not by the
    # absence of names: every mutation/exfil/spawn tool must be in the disallowed list.
    di = seen["cmd"][seen["cmd"].index("--disallowedTools") + 1]
    for t in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert t in di, f"{t} not disallowed: {di}"
    try:
        call_reviewer("p", run=lambda *a, **k: R(1, "")); assert False
    except RuntimeError:
        pass
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
