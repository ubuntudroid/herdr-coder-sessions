# Take over locally — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a mirrored Coder session into a local one — render the remote agent's conversation to markdown, drop the mirror, and start a local agent in the same worktree with that handover in hand.

**Architecture:** One new entry point, `coder-sessions.py --takeover NAME`. It reuses `mirror_session()` to bring the worktree up to date one last time, renders the remote agent's own history file (codex rollout or Claude Code transcript) to markdown, strips the mirror marker so nothing can reset the worktree again, closes the agentty pane that carries the idle hook, and runs a local agent in its place. The remote workspace is left untouched and stays reachable over ssh as a gap-filler.

**Tech Stack:** Python 3.9+, standard library only. git, ssh, the `herdr` CLI, the `coder` CLI.

**Spec:** This document. The mechanism was established empirically in the session that produced it; the "Verified mechanism" section below is the spec, and every claim in it was measured, not assumed.

## Global Constraints

- **Standard library only.** No new dependencies, ever. The README's promise is "two Python files, standard library only".
- **Python 3.9+.** `main()` already enforces this.
- **Two files stay two files.** All code goes in `coder-sessions.py`; `agentty` knows nothing about this plugin and must keep knowing nothing.
- **Tests are `assert`s in `selftest()`**, run by `coder-sessions.py --selftest`. No test framework, no test files.
- **Comments explain *why*, never *what*.** Match the existing density: every non-obvious decision in this file carries a paragraph explaining the alternative that was rejected.
- **`ponytail:` comments** mark deliberate simplifications with a named ceiling and upgrade path.
- **Never mutate the Coder workspace.** No pausing, no installing, no starting daemons.

---

## Verified mechanism

Measured on live sessions on 2026-08-28. Do not re-derive; do not "improve" on it without re-measuring.

**Agent detection.** agentapi launches the agent and names it on its own command line:

```
agentapi server --type codex --term-width 67 ... -- codex --model gpt-5.6-terra ...
```

`--type` is `codex` or `claude`. Both `~/.claude` and `~/.codex` exist on every workspace, so guessing from which directory has files is wrong.

**Codex history.** `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`. One workspace held 19 files; exactly one had `session_meta.payload.thread_source == "user"` and the other 18 were subagent threads carrying the parent's `session_id`. Filter on `thread_source == "user"` plus a `cwd` equal to the session's repo root.

Entry shape: `{"type": "response_item", "payload": {...}}`. Payload types seen, with counts from one 3.3 MB session:

| payload type | count | use |
| --- | --- | --- |
| `custom_tool_call` | 185 | one-line summary (`name`, `input`) |
| `custom_tool_call_output` | 185 | **drop** |
| `reasoning` | 174 | **drop** (`encrypted_content`) |
| `message` | 84 | the conversation |
| `function_call` | 41 | one-line summary (`name`, `arguments`) |
| `function_call_output` | 41 | **drop** |
| `agent_message` | 24 | **drop** (subagent chatter) |

`message` payloads carry `role`: 65 `assistant` (blocks of type `output_text`), 13 `user` (`input_text`), 6 `developer` (the harness's skills prompt — **drop**).

**Claude history.** `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, where the encoding replaces every non-alphanumeric character with `-` (`/home/coder/content_backend/backend` → `-home-coder-content-backend-backend`). Entries are typed `user` / `assistant`; `message.content` is **either a plain string** (the initial prompt) **or a list of blocks** typed `text`, `thinking`, `tool_use`, `tool_result`. Handle both. Each entry also carries `cwd`, `gitBranch`, `sessionId`.

**Why render instead of resuming natively.** Both CLIs can resume a transplanted history file — this was tested end to end, and it works. It is also the expensive path. On the same 3.3 MB codex session:

| path | tokens |
| --- | --- |
| native `codex exec resume` | **1,032,229** (auto-compacted mid-run) |
| full conversation rendered to markdown | ~21,700 |
| last 20 turns only | ~14,300 |

97% of the file is tool payloads, reasoning traces and world state that native resume replays in full. Rendering is ~70× cheaper, needs no LLM, works across agent types, and does not depend on either CLI's on-disk schema staying stable. The dropped tool outputs are recoverable by re-running the command in the worktree, which already holds the work.

**Why the remote is not paused.** A task's `paused` status and its workspace's `stopped` status are the same state:

```
NAME             STATUS   WORKSPACE STATUS  STATE
asked-in-1b8e    paused   stopped           idle
```

Pausing therefore removes the ssh access the local agent needs for gap-filling. It also buys nothing: a Coder agent acts only when sent input, so an idle session does not spontaneously move the branch. And `ssh <name>.coder` **auto-starts a stopped workspace** (~30s, observed), so the escape hatch survives Coder's own inactivity timer either way.

---

## File Structure

| file | change |
| --- | --- |
| `coder-sessions.py` | all new code: renderers, remote discovery, `takeover()`, one CLI flag, one config key |
| `herdr-plugin.toml` | one new `[[actions]]` block |
| `README.md` | one new section, plus the new key in Configuration and the new flag in the usage list |

No new files. The plugin is two scripts and stays two scripts.

---

### Task 1: Transcript rendering

Pure functions over text. No ssh, no git, no herdr — so the whole task is testable from `--selftest`.

**Files:**
- Modify: `coder-sessions.py` — add after `describe()` (~line 677), before `rows()`
- Modify: `coder-sessions.py:1100` — `selftest()`

**Interfaces:**
- Produces: `summarise(value, limit=120) -> str`; `render_codex(text) -> list[tuple[str, str]]`; `render_claude(text) -> list[tuple[str, str]]`; `transcript(turns, **fields) -> str`; constant `TAKEOVER_FILE = ".coder-takeover.md"`. Turn tuples are `(role, body)` where role is `"user"`, `"assistant"` or `"tool"`.

- [ ] **Step 1: Write the failing tests**

Add to `selftest()` in `coder-sessions.py`, just before its final `print`:

```python
    # Take over locally: the renderers, on one line of each shape that matters.
    codex_lines = "\n".join(json.dumps(entry) for entry in [
        {"type": "session_meta", "payload": {"cwd": "/repo", "thread_source": "user"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"text": "skills prompt"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"text": "fix the validator"}]}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                                              "input": "pytest  -q\n  backend/"}},
        {"type": "response_item", "payload": {"type": "reasoning",
                                              "encrypted_content": "opaque"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"text": "Done."}]}},
    ])
    assert render_codex(codex_lines) == [
        ("user", "fix the validator"),
        ("tool", "exec: pytest -q backend/"),
        ("assistant", "Done."),
    ], render_codex(codex_lines)

    claude_lines = "\n".join(json.dumps(entry) for entry in [
        {"type": "summary", "summary": "ignored"},
        {"type": "user", "message": {"content": "review this branch"}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "Looking now."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "git  diff"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "a diff"}]}},
    ])
    assert render_claude(claude_lines) == [
        ("user", "review this branch"),
        ("assistant", "Looking now."),
        ("tool", "Bash: {'command': 'git diff'}"),
    ], render_claude(claude_lines)

    # Malformed lines are skipped, never fatal: a truncated tail is normal in a
    # file the remote agent may still be writing.
    assert render_codex("not json\n") == []
    assert render_claude("{broken\n") == []

    body = transcript([("user", "go"), ("tool", "Bash: ls"), ("assistant", "done")],
                      name="task-1a2b", host="task-1a2b.coder", kind="codex",
                      checkout="/local/wt", branch="feat/x", repo="/home/coder/r")
    assert "ran on a remote machine" in body
    assert "ssh task-1a2b.coder" in body
    assert "/local/wt" in body
    assert "- `Bash: ls`" in body
    assert body.index("## User") < body.index("## Assistant")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `./coder-sessions.py --selftest`
Expected: `NameError: name 'render_codex' is not defined`

- [ ] **Step 3: Write the implementation**

Add to `coder-sessions.py`:

```python
# Where the handover lands inside the taken-over worktree. A dotfile at the root
# rather than a directory: one file to read, one line in .git/info/exclude, and
# nothing left behind if the user deletes it.
TAKEOVER_FILE = ".coder-takeover.md"


def summarise(value, limit=120):
    """One line standing in for a tool call's input: whitespace collapsed, clipped."""
    text = " ".join(str(value).split())
    return text[:limit] + "…" if len(text) > limit else text


def render_codex(text):
    """`[(role, body)]` from a codex rollout: the conversation, no tool output.

    Kept: `message` payloads whose role is user or assistant, and a one-line note
    of each tool call. Dropped: every `*_output`, `reasoning` (its content is
    encrypted and useless to another agent), `agent_message` (subagent chatter),
    and role `developer` (codex's own skills prompt, which the local agent has its
    own version of). That is 97% of the bytes and none of the meaning -- see the
    plan's measurements.
    """
    turns = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a half-written tail line, not a reason to lose the rest
        if entry.get("type") != "response_item":
            continue  # `event_msg` outnumbers it 689 to 727 and shares payload shapes
        payload = entry.get("payload") or {}
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            body = "".join(block.get("text", "") for block in payload.get("content") or []
                           if isinstance(block, dict)).strip()
            if body:
                turns.append((role, body))
        elif kind in ("function_call", "custom_tool_call"):
            # `input` for custom tools, `arguments` for function calls -- same idea,
            # different key, and neither is worth a branch of its own.
            arg = payload.get("input") or payload.get("arguments") or ""
            turns.append(("tool", f"{payload.get('name') or kind}: {summarise(arg)}"))
    return turns


def render_claude(text):
    """`[(role, body)]` from a Claude Code transcript: the conversation, no tool output.

    `message.content` is a bare string for a typed prompt and a list of blocks
    otherwise, so both shapes are handled. `thinking` blocks carry a signature and
    no readable text; `tool_result` is the output we are deliberately dropping.
    """
    turns = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        role = entry.get("type")
        if role not in ("user", "assistant"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            if content.strip():
                turns.append((role, content.strip()))
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                turns.append((role, block["text"].strip()))
            elif block.get("type") == "tool_use":
                turns.append(("tool", f"{block.get('name')}: {summarise(block.get('input'))}"))
    return turns


def transcript(turns, name, host, kind, checkout, branch, repo):
    """The markdown the local agent is handed.

    The header is the whole point of the feature: without it the agent reads a
    conversation in the first person and takes it for its own, then trusts tool
    output it never saw and paths that do not exist here. It says three things --
    this ran somewhere else, the code in front of you is already the result, and
    the machine it ran on is reachable but is a last resort.
    """
    lines = [
        f"# Handover from Coder session `{name}`",
        "",
        "**This is a rendering of a conversation that ran on a remote machine, not",
        f"on this one.** The agent was `{kind}`, working in `{repo}` on branch",
        f"`{branch}` of the Coder workspace `{name}`. It was not you.",
        "",
        f"You are continuing that work **locally**, in `{checkout}`, on the same",
        "branch. The session's commits and its uncommitted changes are already",
        "applied there, so the code in front of you is the state that agent left.",
        "Nothing else about the remote machine is reproduced here.",
        "",
        "**Tool outputs are not included below** -- only what was said, plus a",
        "one-line note of each tool call. If you need a result, re-run the command",
        "locally. That is almost always faster than asking the remote, and the",
        "worktree already holds the work the commands produced.",
        "",
        f"The remote machine is still reachable as `ssh {host}`, for the cases where",
        "something genuinely cannot be reproduced here: an environment-specific",
        "failure, a service that only runs there, a file outside the repository.",
        "It may take ~30s to answer if Coder has stopped it. Do not reach for it for",
        "anything you can do in this worktree.",
        "",
        "---",
        "",
    ]
    for role, body in turns:
        if role == "tool":
            lines += [f"- `{body}`", ""]
        else:
            lines += [f"## {role.capitalize()}", "", body, ""]
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `./coder-sessions.py --selftest`
Expected: PASS, the script's existing "all good" line

- [ ] **Step 5: Check it against a real file**

The renderers must survive real input, not just the fixtures. Copy one real history file down and render it:

```bash
python3 - <<'PY'
import importlib.util, subprocess, sys
spec = importlib.util.spec_from_file_location("cs", "coder-sessions.py")
cs = importlib.util.module_from_spec(spec); spec.loader.exec_module(cs)
host = sys.argv[1] if len(sys.argv) > 1 else "implement-ticket-3a7d.coder"
path = subprocess.run(["ssh", host, "ls -t ~/.codex/sessions/*/*/*/*.jsonl | head -1"],
                      capture_output=True, text=True).stdout.strip()
text = subprocess.run(["ssh", host, f"cat {path}"], capture_output=True, text=True).stdout
turns = cs.render_codex(text)
body = cs.transcript(turns, name="probe", host=host, kind="codex",
                     checkout="/tmp/wt", branch="b", repo="/home/coder/r")
print(f"{len(turns)} turns, {len(body):,} chars (~{len(body)//4:,} tokens)")
PY
```

Expected: a few dozen turns and well under 100,000 characters. If it prints megabytes, a `*_output` payload type is leaking through — fix the filter, do not ship it.

- [ ] **Step 6: Commit**

```bash
git add coder-sessions.py
git commit -m "feat: render a remote agent's conversation to markdown"
```

---

### Task 2: Reading the session's history off the workspace

**Files:**
- Modify: `coder-sessions.py` — add after `remote_repo()` (~line 307)
- Modify: `coder-sessions.py:1100` — `selftest()`

**Interfaces:**
- Consumes: `ssh_out(host, command, check=True)` and `run(argv, check=True)` from the existing file.
- Produces: `claude_dir(path) -> str`; `remote_agent(host) -> str`; `history_path(host, kind, repo, branch) -> str`; `history_text(host, path) -> str`.

- [ ] **Step 1: Write the failing test**

Only `claude_dir` is pure; the rest need a workspace and are covered by Task 4's manual check. Add to `selftest()`:

```python
    assert claude_dir("/home/coder/content_backend/backend") == \
        "-home-coder-content-backend-backend"
    assert claude_dir("/Users/me/.config/x") == "-Users-me--config-x"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `./coder-sessions.py --selftest`
Expected: `NameError: name 'claude_dir' is not defined`

- [ ] **Step 3: Write the implementation**

```python
# agentapi launched the agent and names it on its own command line, which makes
# this the one authoritative answer. Both ~/.claude and ~/.codex exist on every
# Coder workspace, so "which history directory has files" is not a test.
AGENT_TYPE = re.compile(r"agentapi\s+server\b[^\n]*?--type\s+(\S+)")

# Picks the session's own codex rollout. Subagent threads live in the same tree
# and carry the parent's session_id, so `thread_source` is the discriminator;
# `cwd` keeps a second checkout on the same box out of it. Run on the workspace
# because that is where the files are, and because shipping 19 candidates back
# to choose between them would be absurd.
CODEX_PICK = """
import json, glob, os, sys
best = ("", 0.0)
for path in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")):
    try:
        with open(path) as handle:
            meta = json.loads(handle.readline()).get("payload") or {}
    except (OSError, ValueError):
        continue
    if meta.get("thread_source") != "user" or meta.get("cwd") != sys.argv[1]:
        continue
    stamp = os.path.getmtime(path)
    if stamp > best[1]:
        best = (path, stamp)
print(best[0])
"""

# Claude Code files one transcript per session under an encoded copy of the cwd,
# so the directory is known and only the file is in question. The branch is what
# tells the session's transcript from an earlier one in the same checkout; mtime
# breaks a tie, and stands alone on a session that never branched.
CLAUDE_PICK = """
import json, glob, os, sys
best, fallback = ("", 0.0), ("", 0.0)
for path in glob.glob(os.path.join(os.path.expanduser("~/.claude/projects"),
                                   sys.argv[1], "*.jsonl")):
    stamp = os.path.getmtime(path)
    if stamp > fallback[1]:
        fallback = (path, stamp)
    try:
        with open(path) as handle:
            branches = {json.loads(line).get("gitBranch") for line in handle}
    except (OSError, ValueError):
        continue
    if sys.argv[2] in branches and stamp > best[1]:
        best = (path, stamp)
print(best[0] or fallback[0])
"""


def claude_dir(path):
    """Claude Code's project-directory encoding: every non-alphanumeric becomes a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def remote_agent(host):
    """Which agent agentapi started on the session -- "codex", "claude" -- or ""."""
    line = ssh_out(host, "ps -eo args= | grep -m1 'agentapi server'", check=False)
    found = AGENT_TYPE.search(line or "")
    return found.group(1) if found else ""


def history_path(host, kind, repo, branch):
    """Where this session's own history file sits on the workspace, or ""."""
    if kind == "codex":
        script, args = CODEX_PICK, [repo]
    elif kind == "claude":
        script, args = CLAUDE_PICK, [claude_dir(repo), branch]
    else:
        return ""
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return ssh_out(host, f"python3 -c {shlex.quote(script)} {quoted}",
                   check=False).strip()


def history_text(host, path):
    """The history file's bytes. Multi-megabyte and read once, so no streaming."""
    return ssh_out(host, f"cat {shlex.quote(path)}", check=False)
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `./coder-sessions.py --selftest`
Expected: PASS

- [ ] **Step 5: Check it against real workspaces**

Both agent types must be exercised. Pick one running session of each from `coder tasks list`:

```bash
python3 - <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cs", "coder-sessions.py")
cs = importlib.util.module_from_spec(spec); spec.loader.exec_module(cs)
for name in sys.argv[1:]:
    host = name + ".coder"
    kind = cs.remote_agent(host)
    repo, branch, slug, shallow = cs.remote_repo(host)
    path = cs.history_path(host, kind, repo, branch)
    print(f"{name}: kind={kind!r} repo={repo!r} branch={branch!r}\n  -> {path!r}")
PY
```

Expected: a non-empty `kind` and a non-empty path for each. Run it against a `claude` session and a `codex` session — `asked-in-db1a` and `implement-ticket-3a7d` were one of each on 2026-08-28. An empty `kind` means agentapi's flag has moved; an empty path means the pick script's filter is wrong.

- [ ] **Step 6: Commit**

```bash
git add coder-sessions.py
git commit -m "feat: find a session's own agent history on the workspace"
```

---

### Task 3: The takeover itself

**Files:**
- Modify: `coder-sessions.py` — add `takeover()` after `promote()` (~line 916); add an `icon` parameter to `session_tokens()` (~line 274)
- Modify: `coder-sessions.py:1100` — `selftest()`

**Interfaces:**
- Consumes: `render_codex`/`render_claude`/`transcript`/`TAKEOVER_FILE` (Task 1); `remote_agent`/`history_path`/`history_text` (Task 2); `settings`, `session_named`, `mirror_session`, `mirror_marker`, `MIRROR_REFS`, `report_tokens`, `session_tokens`, `checkout_branch`, `session_pane`, `last_pane`, `plugin_workspace`, `remote_repo`, `herdr`, `run`, `note` from the existing file.
- Produces: `local_agent(conf, kind) -> str`; `exclude_locally(checkout, entry) -> None`; `demote_mirror(checkout, branch) -> None`; `takeover(name) -> str`; constants `ICON_TAKEN`, `LAUNCH`, `AGENT_ALIASES`.

**Before writing `takeover()`, check one thing:** `herdr pane split --help` and one
real result, to confirm the pane id in the response is at `result.pane.pane_id`.
Every other `herdr` call in this file was written against a shape that was checked
first; do not guess this one.

- [ ] **Step 1: Write the failing test**

```python
    assert local_agent({"takeover_agent": "match"}, "codex") == "codex"
    assert local_agent({"takeover_agent": "match"}, "claude") == "claude"
    assert local_agent({"takeover_agent": "claude"}, "codex") == "claude"
    assert local_agent({}, "codex") == "codex"  # absent key behaves as "match"
    # The product's name, not the binary's, is what people write in a config file.
    assert local_agent({"takeover_agent": "Claude-Code"}, "codex") == "claude"
    assert local_agent({"takeover_agent": "codex-cli"}, "claude") == "codex"
    assert set(LAUNCH) == {"claude", "codex"}
    assert TAKEOVER_FILE in LAUNCH["claude"] and TAKEOVER_FILE in LAUNCH["codex"]
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `./coder-sessions.py --selftest`
Expected: `NameError: name 'local_agent' is not defined`

- [ ] **Step 3: Write the implementation**

First, let `session_tokens` carry a different icon. Change its signature and the icon line:

```python
def session_tokens(session, branch="", conf=None, icon=ICON):
```

```python
        names["icon"]: icon,
```

Then add:

```python
ICON_TAKEN = "L■"  # was mirroring a Coder session; now worked on locally

# The local agent is started with one instruction: read the handover first. Both
# CLIs take a bare prompt as their first argument, so there is nothing to branch
# on beyond the binary's name.
LAUNCH = {
    "claude": f"claude {shlex.quote('Read ./' + TAKEOVER_FILE + ' before anything else: it is the handover from the remote Coder session you are continuing.')}",
    "codex": f"codex {shlex.quote('Read ./' + TAKEOVER_FILE + ' before anything else: it is the handover from the remote Coder session you are continuing.')}",
}


# What people write in a config file versus what the binary is called. The
# products are "Claude Code" and "Codex"; the commands are `claude` and `codex`.
# Both spellings turn up, and a typo here would otherwise surface as a KeyError
# traceback in the middle of a takeover.
AGENT_ALIASES = {"claude-code": "claude", "claudecode": "claude",
                 "codex-cli": "codex", "openai-codex": "codex"}


def local_agent(conf, kind):
    """Which agent takes over: the configured pick, or the remote's own type."""
    want = str(conf.get("takeover_agent") or "match").strip().lower()
    want = kind if want == "match" else AGENT_ALIASES.get(want, want)
    if want not in LAUNCH:
        sys.exit(f"takeover_agent={conf.get('takeover_agent')!r} is not an agent this "
                 f'knows how to start -- use "match", {", ".join(sorted(LAUNCH))} '
                 f"in {config_hint()}")
    return want


def exclude_locally(checkout, entry):
    """Keep `entry` out of `git status` without touching the repository's .gitignore.

    `.git/info/exclude` is the per-clone ignore file: it is not tracked, so a
    handover never shows up in a diff and never reaches a PR. Worktrees share the
    common dir, so one line covers every session of the same clone.
    """
    common = run(["git", "-C", checkout, "rev-parse", "--git-common-dir"]).strip()
    if not os.path.isabs(common):
        common = os.path.join(checkout, common)
    path = os.path.join(common, "info", "exclude")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as handle:
            if any(line.strip() == entry for line in handle):
                return
    except OSError:
        pass  # no exclude file yet is the normal case, not a failure
    with open(path, "a") as handle:
        handle.write(f"\n# herdr-coder-sessions: local takeover handover\n{entry}\n")


def demote_mirror(checkout, branch):
    """Stop this worktree being a mirror, permanently.

    Two pieces of bookkeeping say a worktree is derived: the marker inside its git
    dir, which `--mirror` checks before it resets anything, and the ref recording
    where this plugin last put the branch. Both go. After this a stray idle hook
    firing `--mirror` hits the existing "not a mirror" guard and declines, which is
    exactly the protection a worktree you author in should have.

    Not a tear-down and rebuild: the session's *uncommitted* work lives only in
    this working tree, and removing the worktree would throw it away to arrive at
    the same branch and the same files.
    """
    marker = mirror_marker(checkout)
    if os.path.exists(marker):
        os.remove(marker)
    common = run(["git", "-C", checkout, "rev-parse", "--git-common-dir"]).strip()
    if not os.path.isabs(common):
        common = os.path.join(checkout, common)
    run(["git", "--git-dir", common, "update-ref", "-d", f"{MIRROR_REFS}/{branch}"],
        check=False)


def takeover(name):
    """Move a session from mirrored-remote to worked-on-locally, once and for good.

    The mirror is refreshed one last time so the worktree holds everything the
    remote agent did, then demoted so nothing can ever reset it again. The remote
    workspace is deliberately left running: pausing a task stops its workspace,
    which is the ssh the local agent needs when the handover's missing tool output
    turns out to matter.
    """
    conf = settings()
    host = f"{name}{conf['host_suffix']}"
    session = session_named(name)
    if not session:
        sys.exit(f"{name}: not a running Coder session -- ctrl-r in the picker")

    kind = remote_agent(host)
    if kind not in LAUNCH:
        sys.exit(f"could not tell which agent runs on {host} "
                 f"(agentapi reported {kind or 'nothing'}) -- taking over needs to "
                 f"know which history to read")
    # Resolved before anything is moved: a misspelt takeover_agent should stop the
    # takeover, not surface once the mirror is already demoted and unrecoverable.
    chosen = local_agent(conf, kind)

    repo, branch, slug, shallow = remote_repo(host)
    path = history_path(host, kind, repo, branch) if repo else ""
    if not path:
        sys.exit(f"no {kind} history for {name} on {host} -- nothing to hand over")

    workspace, checkout, made = mirror_session(name, conf, focus=True)
    if not checkout:
        return None  # mirror_session has already said why

    render = render_codex if kind == "codex" else render_claude
    turns = render(history_text(host, path))
    body = transcript(turns, name=name, host=host, kind=kind, checkout=checkout,
                      branch=checkout_branch(checkout) or branch, repo=repo)
    with open(os.path.join(checkout, TAKEOVER_FILE), "w") as handle:
        handle.write(body)
    exclude_locally(checkout, TAKEOVER_FILE)
    demote_mirror(checkout, checkout_branch(checkout) or branch)

    # Split first, close agentty last. Closing first would leave the agent's slot
    # to whatever herdr collapses into it -- on a mirrored session that is reviewr,
    # and `herdr pane run` sends text plus Enter, so the launch line would be typed
    # into reviewr's TUI. Splitting from the agent's own pane also means the
    # workspace can never empty mid-move, which is what promote()'s last-pane guard
    # exists to prevent; here there is nothing to guard.
    agent_pane = session_pane(workspace)
    if not agent_pane:
        sys.exit(f"no agentty pane in {workspace} -- nothing to take over")
    # --cwd is not optional: agentty's pane sits wherever herdr opened it, and an
    # agent started anywhere but the worktree reads the handover's paths against
    # the wrong tree.
    local = herdr("pane", "split", agent_pane, "--direction", "right",
                  "--cwd", checkout)["result"]["pane"]["pane_id"]
    herdr("pane", "run", local, LAUNCH[chosen])
    herdr("pane", "close", agent_pane)

    report_tokens(workspace, session_tokens(session, checkout_branch(checkout), conf,
                                            icon=ICON_TAKEN), conf)
    herdr("workspace", "focus", workspace)
    note(f"{name} taken over locally: {len(turns)} turns in {checkout}/{TAKEOVER_FILE}, "
         f"{chosen} running in {local}; the mirror is gone and {host} is left running")
    return workspace
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `./coder-sessions.py --selftest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add coder-sessions.py
git commit -m "feat: take a Coder session over locally, dropping its mirror"
```

---

### Task 4: Config, CLI, action, docs

**Files:**
- Modify: `coder-sessions.py:61-66` — `DEFAULTS`
- Modify: `coder-sessions.py:1-30` — the module docstring's usage list
- Modify: `coder-sessions.py` — `main()`'s parser and dispatch
- Modify: `herdr-plugin.toml` — one `[[actions]]` block
- Modify: `README.md` — Usage, Configuration, and a new section

**Interfaces:**
- Consumes: `takeover(name)` from Task 3.

- [ ] **Step 1: Add the config key**

In `DEFAULTS`:

```python
    # Which agent picks a session up locally. "match" uses whatever agentapi ran
    # on the workspace; a name ("claude", "codex") always uses that one, since the
    # handover is plain markdown and any agent can read any other's.
    "takeover_agent": "match",
```

- [ ] **Step 2: Add the CLI flag**

In `main()`, beside `--promote`:

```python
    parser.add_argument("--takeover", metavar="NAME",
                        help="hand a session's conversation to a local agent and "
                             "drop its mirror")
```

and in the dispatch chain, after the `--promote` branch:

```python
    if args.takeover:
        return takeover(args.takeover)
```

Add to the module docstring's usage list, after the `--promote` line:

```
    coder-sessions.py --takeover NAME  hand this session's conversation to a local
                                       agent and stop mirroring it
```

- [ ] **Step 3: Add the plugin action**

Append to `herdr-plugin.toml`:

```toml
# Take the focused session over locally: render its remote conversation into the
# worktree, drop the mirror, and start a local agent there. The Coder workspace is
# left running on purpose -- the local agent may need to ssh in for a tool output
# the handover does not carry, and pausing a task stops its workspace.
[[actions]]
id = "takeover"
title = "Coder sessions: take over locally"
contexts = ["workspace", "tab", "pane"]
command = ["python3", "coder-sessions.py", "--takeover"]
```

`--takeover` needs the session name, which the action cannot pass. Resolve it the
way `--refresh` and `--web` do — from the focused workspace's `coder` metadata
token. Change the dispatch to accept an empty value:

```python
    parser.add_argument("--takeover", nargs="?", const="", metavar="NAME",
                        help="hand a session's conversation to a local agent and "
                             "drop its mirror; without NAME, the focused workspace's")
```

```python
    if args.takeover is not None:
        return takeover(args.takeover or focused_session()[1])
```

`refresh()` already resolves the focused workspace's session name and `--takeover`
needs the identical lookup, so extract it rather than writing it twice. Add beside
`refresh()`:

```python
def focused_session(workspace=None):
    """`(workspace id, session name)` for the workspace an action fired in.

    The name comes from the token this plugin stamps, which is why a keybinding
    needs no argument. A helper rather than two copies, so --refresh and --takeover
    fail with the same sentence in the same situation.
    """
    workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace:
        sys.exit("no workspace in focus (HERDR_WORKSPACE_ID unset)")
    token = name_token()
    name = (workspace_info(workspace).get("tokens") or {}).get(token)
    if not name:
        sys.exit(f"{workspace} is not a Coder session workspace (no {token} token)")
    return workspace, name
```

and replace the first eight lines of `refresh()` (`coder-sessions.py:1019-1027`,
from `workspace = workspace or ...` through the `if not name:` block) with:

```python
    workspace, name = focused_session(workspace)
```

- [ ] **Step 4: Document it**

In `README.md`, add to the standalone usage block:

```
./coder-sessions.py --takeover <name>  # hand it to a local agent, stop mirroring
```

Add the key to the Configuration table: `takeover_agent` — `"match"` (default) uses
whichever agent ran on the workspace; `"claude"` or `"codex"` always uses that one.

Add a section after **Mirroring**:

````markdown
## Take over locally

`prefix+ctrl+t` (the `takeover` action) ends the remote flow for the focused
session and continues it here. It refreshes the mirror one last time, renders the
remote agent's conversation into `.coder-takeover.md` at the worktree root, drops
the mirror marker so nothing can reset the worktree again, closes the agentty
pane, and starts a local agent on the same branch.

The handover is markdown, not the agent's own session format. That is deliberate:
resuming a real session natively cost **1,032,229 tokens** on a 3.3 MB codex
rollout and compacted itself mid-run, because 97% of that file is tool output,
reasoning traces and world state replayed in full. The conversation alone is
~22,000 tokens. Rendering is ~70× cheaper, works between agents — a codex session
can be picked up by Claude Code — and does not break when either CLI changes its
on-disk format. What it drops is the tool *output*; the commands are still listed,
and the worktree already holds everything they produced.

`takeover_agent` decides who picks it up: `"match"` (the default) uses whichever
agent agentapi ran on the workspace, and a name always uses that one.

The Coder workspace is **not** paused. A paused task is a stopped workspace, so
pausing would remove the `ssh <session>.coder` the local agent is told to fall back
on when it needs something that genuinely cannot be reproduced here. Nothing is
lost by leaving it: a Coder agent only acts when it is sent input, and ssh restarts
a stopped workspace on demand in about 30 seconds.

Taking over is one-way. The worktree is yours afterwards — `prefix+ctrl+m` on it
declines, the same way it declines on any worktree this plugin did not derive.
````

Add the binding to the README's key list:

```toml
[[keys.command]]
key = "prefix+ctrl+t"
type = "plugin_action"
command = "ubuntudroid.coder-sessions.takeover"
description = "take this Coder session over locally"
```

- [ ] **Step 5: End-to-end check on a real session**

This is the only test of the whole path. Use a session you do not mind moving —
`coder tasks list` for a running one.

```bash
./coder-sessions.py --takeover <name>
```

Verify, in order:

1. `<worktree>/.coder-takeover.md` exists, opens with "This is a rendering of a
   conversation that ran on a remote machine", and names the right `ssh` host.
2. `git -C <worktree> status --short` is **clean of the handover** — the
   `.git/info/exclude` line worked.
3. `ls "$(git -C <worktree> rev-parse --git-dir)/coder-mirror"` fails — the marker
   is gone.
4. The workspace shows `L■` in the sidebar, not `C■`.
5. The agentty pane is gone and the configured agent is running in its place.
6. `./coder-sessions.py --mirror <name>` declines with the "not a mirror" note
   instead of resetting anything. **This is the important one** — it is what
   protects work done after the takeover.
7. `coder tasks list` still shows the session `active`, not `paused`.

- [ ] **Step 6: Commit**

```bash
git add coder-sessions.py herdr-plugin.toml README.md
git commit -m "feat: expose take-over-locally as a flag, an action and docs"
```

---

## Notes for the implementer

- **`--mirror` after a takeover is the safety net, not an edge case.** The idle
  hook lives in the agentty process's environment and dies with the pane, but a
  second agentty on the same session, or a hand-run `--mirror`, would still call
  in. `demote_mirror()` is what makes that call harmless; step 5.6 is what proves
  it. Do not skip it.
- **Do not add a `--resume` path.** It was tested, it works, and it costs 70× more
  for a worse result. If someone asks for full fidelity later, the answer is to
  render tool outputs into the markdown under a size cap, not to reintroduce the
  native format.
- **Do not pause the remote task.** See the README section for why; the reasoning
  is measured, not stylistic.
- **A session that never branched cannot be taken over yet**, and says so in
  `mirror_session`'s words — which end with "prefix+ctrl+m moves it there", a
  mirror-flavoured hint in a takeover context. Left as is for v1: the advice is
  still correct (mirror it, then take it over), just phrased for the other caller.
- `ponytail:` the pick scripts sort by mtime and take the newest match. A workspace
  running two concurrent sessions in the same checkout would pick the more recent
  one. Add a session-id source if that ever happens; it has not.
