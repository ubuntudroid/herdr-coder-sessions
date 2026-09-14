# Take over a branchless session — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `prefix+ctrl+t` take over a session whose mirror is detached: a popup asks for a branch name pre-filled from Linear, creates and pushes it, tells the remote agent, and runs the existing takeover — with everything it prints on screen.

**Architecture:** The `takeover` action re-runs `coder-sessions.py --takeover` inside a popup plugin pane so it has a terminal. In `takeover()`, the detached guard becomes a prompt; the answer is carried through the unchanged steps and acted on only after `demote_mirror()`, because the last mirror refresh would detach a branch created earlier. New helpers: a slug, a Linear lookup, a suggestion, a validated prompt, and one ssh call that types a line into the remote agent.

**Tech Stack:** Python 3.9+, standard library only. git, ssh, the `herdr` CLI, optionally the `linear` CLI (schpet/linear-cli 2.6.0).

**Spec:** `docs/superpowers/specs/2026-09-14-takeover-branch-prompt-design.md`

## Global Constraints

- Standard library only. No new dependencies.
- Python 3.9+; `main()` enforces this.
- Two files stay two files: all code goes in `coder-sessions.py`; `agentty` learns nothing.
- Tests are `assert`s in `selftest()`, run by `python3 coder-sessions.py --selftest`. No framework, no test files.
- Comments carry a reason, never a restatement. `ponytail:` marks a deliberate ceiling.
- The only write to the Coder workspace is one line typed into the agent's composer, at the user's request from the prompt. Nothing is installed, started, paused or checked out over there.
- This checkout is shared with other sessions: `git add` by explicit path in every commit, never `-A` or `.`.
- Commits go to `main`, as the repo's history does. No PR is part of this plan.
- Executors run without a terminal. Never run `coder-sessions.py --takeover` end to end from a tool: without a TTY it opens a real popup in the user's herdr. Call functions through `runpy` instead; end-to-end runs are the user's, in Task 5.
- Edits are live: the plugin is linked from this checkout (`source.kind: local`), so Python changes need no reload. Whether a manifest (`herdr-plugin.toml`) change is live is verified in Task 3.
- Line numbers in **Files:** and steps refer to the files before any of this plan's edits. Match on the quoted text; earlier steps shift later lines.

---

### Task 1: The suggestion — `slug()`, `linear_branch()`, `suggest_branch()`, `branch_prefix`

**Files:**
- Modify: `coder-sessions.py:34-35` (module docstring settings line)
- Modify: `coder-sessions.py:86` (DEFAULTS, after `"takeover_agent": "match",`)
- Modify: `coder-sessions.py:285-287` (insert after `readable_name()`, before `ICON = "C■"`)
- Modify: `coder-sessions.py:1799` (selftest, after the `TAKEOVER_FILE in LAUNCH` assert)

**Interfaces:**
- Consumes: `readable_name(session, limit=28, branch="")`, `TICKET_RE`, `SLACK_MARKUP`, `log_line(text)` — all existing.
- Produces: `slug(text, limit=50) -> str`; `linear_branch(ticket) -> str` (`""` on any failure); `suggest_branch(session, conf, linear=None) -> str` where `linear` is a callable `str -> str` defaulting to `linear_branch`; config key `branch_prefix` (default `""`).

- [ ] **Step 1: Write the failing asserts**

In `selftest()`, after line 1799 (`assert TAKEOVER_FILE in LAUNCH["claude"] and TAKEOVER_FILE in LAUNCH["codex"]`) and before the blank line that precedes `print("selftest ok")`, add:

```python

    assert slug("<@U0B9X> Batch: supply the engine's client error type") == \
        "batch-supply-the-engines-client-error-type"
    assert slug("a" * 30 + " " + "b" * 30) == "a" * 30  # cut on a word, never mid-word
    assert slug("") == ""
    # Linear's name wins when there is a ticket; the fallback is prefix + ticket + task,
    # with the ticket appearing once; no ticket means prefix + task; nothing means the name.
    assert suggest_branch(sess(display_name="fix CON2-150 now"), {"branch_prefix": "sven/"},
                          linear=lambda t: "sven/con2-150-from-linear") == "sven/con2-150-from-linear"
    assert suggest_branch(sess(display_name="fix CON2-150 now"), {"branch_prefix": "sven/"},
                          linear=lambda t: "") == "sven/con2-150-fix-now"
    assert suggest_branch(sess(display_name="fix now"), {"branch_prefix": "sven/"},
                          linear=lambda t: "") == "sven/fix-now"
    assert suggest_branch(sess(), {}, linear=lambda t: "") == "example-task-4f21"
    assert suggest_branch(sess(display_name="fix CON2-150 now"), {},
                          linear=lambda t: "") == "con2-150-fix-now"
```

`sess(**kw)` is the helper defined at the top of `selftest()`.

- [ ] **Step 2: Run the selftest to see it fail**

Run: `python3 coder-sessions.py --selftest`
Expected: `NameError: name 'slug' is not defined` (traceback, exit 1).

- [ ] **Step 3: Add the config key and its docstring line**

Line 34-35, the module docstring's settings example, becomes:

```
    {"host_suffix": ".coder", "clone_root": "~/projects/github", "mirror": true,
     "takeover_agent": "match", "branch_prefix": "", "token_prefix": "coder_"}
```

In `DEFAULTS`, directly after `"takeover_agent": "match",` (line 86), add:

```python
    # Prefix for the branch a branchless takeover suggests when Linear cannot
    # name the ticket: "sven/" gives "sven/con2-150-<task>". Linear's own names
    # already carry one, so this shapes the fallback only.
    "branch_prefix": "",
```

- [ ] **Step 4: Add the three helpers**

Insert after the last line of `readable_name()` (`return text[:limit].rstrip() + "…" if len(text) > limit else text`, line 285) and before `ICON = "C■"`:

```python


def slug(text, limit=50):
    """Free text as a branch segment, cut the way Linear cuts its own names.

    Apostrophes go rather than split ("engine's" -> "engines"), which is what
    Linear does and what keeps a fallback name looking like a Linear one.
    """
    text = SLACK_MARKUP.sub(" ", text or "").replace("'", "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].rsplit("-", 1)[0] if len(text) > limit else text


def linear_branch(ticket):
    """Linear's own branch name for a ticket, via the `linear` CLI, or "".

    Optional by design: no CLI, no login, no such issue, or no answer in ten
    seconds all fall through to the fallback suggestion, so nothing here exits.
    Not run(): that exits on a missing binary. The ticket matched TICKET_RE, so
    it cannot break out of the quoted query.
    """
    query = 'query { issue(id: "%s") { branchName } }' % ticket
    try:
        proc = subprocess.run(["linear", "api", query], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_line(f"no branch name from Linear for {ticket}: {exc}")
        return ""
    if proc.returncode != 0:
        log_line(f"no branch name from Linear for {ticket}: "
                 f"{(proc.stderr or proc.stdout).strip()[:200]}")
        return ""
    try:
        return json.loads(proc.stdout)["data"]["issue"]["branchName"] or ""
    except (ValueError, KeyError, TypeError):
        log_line(f"Linear answered without a branchName for {ticket}: {proc.stdout[:200]}")
        return ""


def suggest_branch(session, conf, linear=None):
    """The branch to take a branchless session over on.

    Linear's name for the ticket when the session names one -- it already
    carries the user's prefix and the issue title. Otherwise branch_prefix plus
    the ticket plus a slug of the task's display name, with the ticket taken
    out of that name first so it does not appear twice.
    """
    head = readable_name(session)
    ticket = head if TICKET_RE.fullmatch(head) else ""
    if ticket:
        found = (linear or linear_branch)(ticket)
        if found:
            return found
    title = (session.get("display_name") or "").replace(ticket, "")
    parts = [part for part in (ticket.lower(), slug(title)) if part]
    return conf.get("branch_prefix", "") + ("-".join(parts) or session["name"])
```

- [ ] **Step 5: Run the selftest to see it pass**

Run: `python3 coder-sessions.py --selftest`
Expected: `selftest ok`

- [ ] **Step 6: Check `linear_branch` against Linear, both ways**

Run:
```bash
python3 -c "import runpy; g = runpy.run_path('coder-sessions.py'); print(repr(g['linear_branch']('CON2-150')))"
```
Expected: `'sven/con2-150-batch-supply-the-engines-client-error-type-so-a-gated-ai'`

Run (no `linear` on PATH):
```bash
python3 -c "import os, runpy; os.environ['PATH'] = '/nonexistent'; g = runpy.run_path('coder-sessions.py'); print(repr(g['linear_branch']('CON2-150')))"
```
Expected: `''`, and the last line of `~/.local/state/herdr/plugins/ubuntudroid.coder-sessions/coder-sessions.log` reads `no branch name from Linear for CON2-150: [Errno 2] No such file or directory: 'linear'`.

- [ ] **Step 7: Commit**

```bash
git add coder-sessions.py
git commit -m "feat: suggest a branch for a branchless takeover, from Linear or the task

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The prompt and the branch step in `takeover()`

**Files:**
- Modify: `coder-sessions.py:1336` (insert `AGENT_SAY`, `ask_branch()`, `tell_remote_agent()` before `def takeover(name):`)
- Modify: `coder-sessions.py:1360-1373` (the detached guard, the notify, `session = session_named(name)`)
- Modify: `coder-sessions.py:1401` (`branch = ...`)
- Modify: `coder-sessions.py:1419-1440` (after `demote_mirror`, and the closing tokens / note)

**Interfaces:**
- Consumes: `suggest_branch(session, conf)` (Task 1); `readable_name`, `session_named`, `checkout_branch`, `workspace_info`, `run`, `ssh_out`, `note`, `herdr`, `shlex` — existing.
- Produces: `ask_branch(suggestion, checkout) -> str | None` (`None` = cancelled); `tell_remote_agent(host, text) -> bool`; `AGENT_SAY` (python source run on the workspace).

- [ ] **Step 1: Add the two helpers and the remote script**

Insert directly before `def takeover(name):` (line 1337):

```python
def ask_branch(suggestion, checkout):
    """Which branch a branchless takeover should create, or None to cancel.

    Enter keeps the suggestion and anything typed replaces it whole -- the
    semantics of a placeholder, without a line editor. Validated here, before
    the takeover reaches anything it cannot undo: a bad name would otherwise
    surface from `checkout -b` with the marker already gone.
    """
    print(f"branch [{suggestion}]:")
    print("  enter = use this   type a name = use yours   ctrl-c = cancel")
    while True:
        try:
            answer = input("> ").strip() or suggestion
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if subprocess.run(["git", "check-ref-format", "--branch", answer],
                          capture_output=True).returncode != 0:
            print(f"  {answer!r} is not a valid branch name")
            continue
        if run(["git", "-C", checkout, "rev-parse", "--verify", "-q",
                f"refs/heads/{answer}"], check=False).strip():
            print(f"  {answer} already exists locally -- pick another name")
            continue
        return answer


# Typed into the remote agent as keystrokes plus Enter, through agentapi on the
# workspace: the request agentty makes for every key, so it lands whether the
# agent is idle or mid-turn -- mid-turn it queues as the next input. A "user"
# message would be refused while the agent is busy. Prints ok so the caller can
# tell a delivered line from an ssh that ran and failed.
AGENT_SAY = """
import json, sys, urllib.request
body = json.dumps({"type": "raw", "content": sys.argv[1] + "\\r"}).encode()
req = urllib.request.Request("http://localhost:3284/message", body,
                             {"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=10).read()
print("ok")
"""


def tell_remote_agent(host, text):
    """Type one line into the remote agent's composer. True when it landed."""
    return ssh_out(host, f"python3 -c {shlex.quote(AGENT_SAY)} {shlex.quote(text)}",
                   check=False) == "ok"


```

- [ ] **Step 2: Check the helpers offline**

Run:
```bash
python3 -c "import runpy; g = runpy.run_path('coder-sessions.py'); compile(g['AGENT_SAY'], 'AGENT_SAY', 'exec'); print('compiles')"
printf 'bad..name\n' | python3 -c "import runpy; g = runpy.run_path('coder-sessions.py'); print(repr(g['ask_branch']('sven/x-1', '.')))"
printf 'main\n' | python3 -c "import runpy; g = runpy.run_path('coder-sessions.py'); print(repr(g['ask_branch']('sven/x-1', '.')))"
printf '\n' | python3 -c "import runpy; g = runpy.run_path('coder-sessions.py'); print(repr(g['ask_branch']('sven/x-1', '.')))"
printf 'sven/typed-2\n' | python3 -c "import runpy; g = runpy.run_path('coder-sessions.py'); print(repr(g['ask_branch']('sven/x-1', '.')))"
```
Expected, in order: `compiles`; the two prompt lines then `  'bad..name' is not a valid branch name` then `None` (EOF after the re-prompt); `  main already exists locally -- pick another name` then `None`; `'sven/x-1'`; `'sven/typed-2'`.

- [ ] **Step 3: Turn the detached guard into the prompt**

Replace lines 1360-1373 — from `# A mirror of a session with no branch of its own is detached, and a local` through `session = session_named(name)` — with:

```python
    checkout = (workspace_info(existing).get("worktree") or {}).get("checkout_path", "")
    session = session_named(name)
    # A mirror of a session with no branch of its own is detached, and a local
    # agent committing there would put its work on no branch at all. So the
    # takeover branches -- on a name you accept: Linear's own for the ticket
    # when there is one, and always yours to replace. Asked here, off local
    # state and before any ssh, so a cancel costs nothing; the branch itself is
    # created after the demote below, because the refresh in between would
    # detach it again.
    new_branch = None
    if checkout_branch(checkout) == "HEAD":
        print(f"{name} ({readable_name(session)}) has no branch of its own yet: "
              f"its mirror is detached.")
        print("Take it over on a new branch, pushed to origin, and tell the remote agent.\n")
        new_branch = ask_branch(suggest_branch(session, conf), checkout)
        if not new_branch:
            note(f"takeover of {name} cancelled at the branch prompt; nothing changed")
            return None
    # Said before the first remote call, not after: everything below is ssh and a
    # fetch, which is seconds of a keybinding looking like it did nothing. The
    # local guards above are instant, and a refusal from one of them is better as
    # its own notification alone than under a "starting" that was never true.
    notify(f"Taking over {name}", "reading the session over ssh")
```

Note `session = session_named(name)` moved up from after the notify; there must be exactly one such line in the function afterwards.

- [ ] **Step 4: Carry the branch through**

Line 1401 `    branch = checkout_branch(checkout) or branch` becomes:

```python
    branch = new_branch or checkout_branch(checkout) or branch
```

- [ ] **Step 5: Create, push, and tell — after the demote**

Directly after line 1419 `    demote_mirror(checkout, branch)` and before the `# Split first, close agentty last.` comment, insert:

```python

    pushed = ""
    if new_branch:
        # -b at the commit the mirror sits on: the session's uncommitted work
        # stays in the working tree, which is the point of taking it over.
        run(["git", "-C", checkout, "checkout", "-q", "-b", new_branch])
        print(f"pushing {new_branch} to origin ...")
        push = subprocess.run(["git", "-C", checkout, "push", "-q", "-u", "origin", new_branch],
                              capture_output=True, text=True)
        if push.returncode != 0:
            # Not a stop: the local flow does not need the push, and the branch
            # is there to push by hand. The agent is not told about a branch
            # origin does not have.
            pushed = f"NOT pushed: {(push.stderr or push.stdout).strip()[:200]}"
        else:
            told = tell_remote_agent(
                host, f"Taken over locally: work continues on branch {new_branch}, pushed "
                      f"to origin. If you keep working here, run `git fetch origin && "
                      f"git checkout {new_branch}` first.")
            pushed = "pushed to origin, " + \
                ("the remote agent told" if told else "but the remote agent could not be told")
```

- [ ] **Step 6: Rename the workspace and say what happened**

Replace lines 1435-1440 — from `    report_tokens(workspace, session_tokens(session, branch, conf,` through `    return workspace` — with:

```python
    report_tokens(workspace, session_tokens(session, branch, conf,
                                            icon=ICON_TAKEN), conf)
    if new_branch:
        # What mirror_session does when a mirror moves onto a branch: the label
        # herdr gave the detached checkout was the session name, not a branch.
        herdr("workspace", "rename", workspace, new_branch)
    herdr("workspace", "focus", workspace)
    note(f"{name} taken over locally: {len(turns)} turns in {checkout}/{TAKEOVER_FILE}, "
         f"{chosen} running in {local}; the mirror is gone and {host} is left running"
         + (f"; {new_branch} {pushed}" if new_branch else ""))
    return workspace
```

- [ ] **Step 7: Verify it compiles and the selftest still passes**

Run: `python3 -m py_compile coder-sessions.py && python3 coder-sessions.py --selftest`
Expected: `selftest ok`

Run: `grep -c 'session = session_named(name)' coder-sessions.py`
Expected: `1`

- [ ] **Step 8: Commit**

```bash
git add coder-sessions.py
git commit -m "feat: take a branchless session over on a branch you name, pushed and announced

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: The popup — pane entry, terminal-less re-open, printed notifications

**Files:**
- Modify: `herdr-plugin.toml:52-61` (comment and a new `[[panes]]` after the `takeover` action)
- Modify: `coder-sessions.py:1850-1851` (`--takeover` dispatch in `main()`)
- Modify: `coder-sessions.py:1912-1924` (`notify()`)

**Interfaces:**
- Consumes: `PLUGIN_ID`, `focused_session()`, `herdr()`, `takeover(name)` — existing.
- Produces: pane id `takeover-pane`; env var `CODER_SESSION` read by `--takeover`.

- [ ] **Step 1: Add the pane to the manifest**

Replace lines 52-60 of `herdr-plugin.toml` (the comment block and the `takeover` action) with:

```toml
# Take the focused session over locally: render its remote conversation into the
# worktree, drop the mirror, and start a local agent there. The Coder workspace is
# left running on purpose -- the local agent may need to ssh in for a tool output
# the handover does not carry, and pausing a task stops its workspace.
# The action has no terminal, so `--takeover` re-opens itself in the popup below:
# it may have a branch name to ask for, and what it prints -- progress and
# failures -- is worth reading. The session travels in CODER_SESSION.
[[actions]]
id = "takeover"
title = "Coder sessions: take over locally"
contexts = ["workspace", "tab", "pane"]
command = ["python3", "coder-sessions.py", "--takeover"]

[[panes]]
id = "takeover-pane"
title = "Coder sessions: take over locally"
placement = "popup"
width = "90%"
height = "50%"
command = ["python3", "coder-sessions.py", "--takeover"]
```

- [ ] **Step 2: Check herdr sees the pane, or note that it needs a restart**

Run:
```bash
herdr plugin list --plugin ubuntudroid.coder-sessions --json | python3 -c "import json, sys; d = json.load(sys.stdin); print([p['id'] for p in d['result']['plugins'][0]['panes']])"
```
Expected: `['list', 'promote', 'takeover-pane']`. If `takeover-pane` is missing, the manifest is read at startup: tell the user herdr needs a restart before Task 5, and carry on.

- [ ] **Step 3: Re-open in the popup when there is no terminal**

Replace lines 1850-1851 in `main()`:

```python
    if args.takeover is not None:
        return takeover(args.takeover or focused_session()[1])
```

with:

```python
    if args.takeover is not None:
        name = args.takeover or os.environ.get("CODER_SESSION") or focused_session()[1]
        if not sys.stdin.isatty():
            # The action runs with no terminal, and a takeover may have a question
            # to ask and always has progress worth watching: hand it to a popup,
            # which has one. The name goes along in the environment because the
            # pane's process is not the action's and has no focused workspace.
            herdr("plugin", "pane", "open",
                  "--plugin", os.environ.get("HERDR_PLUGIN_ID", PLUGIN_ID),
                  "--entrypoint", "takeover-pane", "--env", f"CODER_SESSION={name}")
            return
        return takeover(name)
```

- [ ] **Step 4: Print instead of toasting when there is a terminal**

In `notify()` (line 1912), after the docstring and before the `try:`, insert:

```python
    if sys.stdin.isatty():
        print(f"{title}: {body}" if body else title)
        return
```

The docstring's first sentence changes from "The only channel a plugin action has: it runs with no terminal, so its stdout and stderr go nowhere anyone sees." to "The only channel a terminal-less action has; with a terminal -- the takeover's popup, or a shell -- the same words are printed where they can be read."

- [ ] **Step 5: Check the dispatch offline, without opening a popup**

Run:
```bash
HERDR_BIN_PATH=/bin/echo python3 coder-sessions.py --takeover some-session < /dev/null; echo "exit=$?"
```
Expected: stderr contains `herdr plugin pane open --plugin ubuntudroid.coder-sessions --entrypoint takeover-pane --env CODER_SESSION=some-session returned no JSON: plugin pane open ...` (the echo stand-in printed the arguments, which the JSON parse then rejected), followed by the log path in parentheses, then `exit=1`. The stand-in's `notification show` output is captured, not shown. What matters: the pane path was taken with the right entrypoint and env.

Run: `python3 -m py_compile coder-sessions.py && python3 coder-sessions.py --selftest`
Expected: `selftest ok`

- [ ] **Step 6: Commit**

```bash
git add coder-sessions.py herdr-plugin.toml
git commit -m "feat: run the takeover in a popup, so its questions and failures are on screen

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Docs — README and one spec line

**Files:**
- Modify: `README.md:84-93` (Usage: the notification paragraph)
- Modify: `README.md:272-307` (Take over locally: the branchless paragraph)
- Modify: `README.md:338-352` (Requirements: optional list)
- Modify: `README.md:409-420` (Configuration table)
- Modify: `docs/superpowers/specs/2026-09-14-takeover-branch-prompt-design.md` (the cancel line)

- [ ] **Step 1: Usage paragraph**

Replace the paragraph starting `A plugin action runs with no terminal, so anything it prints goes nowhere anyone sees.` (lines 84-93) with:

```markdown
A plugin action runs with no terminal, so anything it prints goes nowhere anyone sees.
The take-over key therefore re-opens itself in a popup: what it prints is on screen,
a failure waits for a key before the popup closes, and a session without a branch
gets asked which one to create — see [Take over locally](#take-over-locally). The
refresh key stays a bare action and says what it is doing in a herdr notification
instead: one when the ssh work starts, because it spends seconds on the network before
there is anything to look at, and one when it finishes. A failure from a keypress
notifies as well, and every failure lands in `coder-sessions.log` under
`HERDR_PLUGIN_STATE_DIR`. The idle hook's own refresh runs through `--mirror` and says
nothing at all, failures included: a mirror that cannot be built yet would otherwise
notify after every agent turn. One notification per keypress, never one per turn.
```

- [ ] **Step 2: Take over locally**

Replace the paragraph starting `A session with [no branch of its own](#when-the-session-has-no-branch-of-its-own)` (through `remote agent branches.`) with the following (the outer `~~~` fence is this plan's; the inner triple-backtick block goes into the README as written):

~~~markdown
A session with [no branch of its own](#when-the-session-has-no-branch-of-its-own)
has a detached mirror, and a local agent's commits there would land on no branch at
all. So the popup asks first:

```
asked-in-allengineering-e780 (CON2-150) has no branch of its own yet: its mirror is detached.
Take it over on a new branch, pushed to origin, and tell the remote agent.

branch [sven/con2-150-batch-supply-the-engines-client-error-type-so-a-gated-ai]:
  enter = use this   type a name = use yours   ctrl-c = cancel
```

The suggestion is Linear's own branch name for the ticket the session names, read
through the [`linear` CLI](https://github.com/schpet/linear-cli) when it is installed
and logged in. Without one — or without a ticket — it is `branch_prefix` plus the
ticket plus a slug of the task's title. Enter takes it, anything typed replaces it
whole, ctrl-c cancels with nothing changed. The branch is created at the mirror's
commit once the mirror is demoted, so the session's uncommitted work comes along,
then pushed with its upstream set, and the remote agent is told in one line typed
into its composer — queued if it is mid-turn — so any further work over there starts
from the same branch. A push that fails is reported and not fatal: the branch exists
locally, and the agent is not told about a branch origin does not have.
~~~

- [ ] **Step 3: Requirements**

After the `fzf` bullet (line 340), add:

```markdown
- **[`linear` CLI](https://github.com/schpet/linear-cli), logged in**, for the branch
  name a take-over suggests. Without it the suggestion is built from `branch_prefix`,
  the ticket and the task title.
```

- [ ] **Step 4: Configuration table**

After the `takeover_agent` row, add:

```markdown
| `branch_prefix` | `""` | prefix for the branch a take-over suggests when Linear cannot name the ticket, e.g. `"sven/"`. Linear's own names already carry one |
```

- [ ] **Step 5: Spec cancel line**

In the spec, under "The prompt", replace:

```
- `EOFError` / `KeyboardInterrupt`: exit 130 with nothing changed. The pane
  closes; agentty keeps running; the mirror stays a mirror.
```

with:

```
- `EOFError` / `KeyboardInterrupt`: the prompt answers `None`, the takeover
  notes the cancel and returns (exit 0). Nothing changed: the pane closes,
  agentty keeps running, the mirror stays a mirror. Not exit 130: the
  `__main__` handler holds any non-zero `SystemExit` for a keypress.
```

- [ ] **Step 6: Check the README renders its anchors**

Run: `grep -c 'take-over-locally\|when-the-session-has-no-branch-of-its-own' README.md`
Expected: a number at least 4 (the links added above resolve to headings that exist).

- [ ] **Step 7: Commit**

```bash
git add README.md docs/superpowers/specs/2026-09-14-takeover-branch-prompt-design.md
git commit -m "docs: describe the branch prompt, the popup, and the linear CLI

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Acceptance on CON2-150 — user at the keyboard

**Files:** none. All steps marked *user* are typed into herdr by the user; the executor hands them over and verifies afterwards.

**Preconditions (executor checks):**

```bash
coder list --output json 2>/dev/null | python3 -c "import json, sys; raw = sys.stdin.read(); d = json.loads(raw[raw.find('['):]); print(next(w['latest_build']['status'] for w in d if w['name'] == 'asked-in-allengineering-e780'))"
git -C ~/.herdr/worktrees/photoroom_android/coder-asked-in-allengineering-e780 status -sb | head -1
ls "$(git -C ~/.herdr/worktrees/photoroom_android/coder-asked-in-allengineering-e780 rev-parse --absolute-git-dir)" | grep -c coder-mirror
```
Expected: `running`; `## HEAD (no branch)`; `1` (the `coder-mirror` marker file). If the status is not `running`, stop: ssh would start the workspace, and that is the user's call.

- [ ] **Step 1 (user): cancel path**

Focus the CON2-150 workspace (sidebar row `CON2-150`, label `coder-asked-in-allengineering-e780`), press `prefix+ctrl+t`. A popup opens with the two-line explanation and the `branch [sven/con2-150-...]:` prompt. Press ctrl-c.

- [ ] **Step 2: Verify nothing changed**

Run the three precondition commands again, plus:
```bash
tail -2 ~/.local/state/herdr/plugins/ubuntudroid.coder-sessions/coder-sessions.log
```
Expected: same three answers; the log's last line is `... takeover of asked-in-allengineering-e780 cancelled at the branch prompt; nothing changed`; agentty still runs in the workspace (`herdr pane list --workspace w8B` shows it).

- [ ] **Step 3 (user): accept**

Press `prefix+ctrl+t` again. Press enter at the prompt. Watch the popup: `Taking over ...: reading the session over ssh`, `pushing sven/con2-150-... to origin ...`, then the popup closes and a local Claude pane appears in the worktree.

- [ ] **Step 4: Verify the result**

```bash
W=~/.herdr/worktrees/photoroom_android/coder-asked-in-allengineering-e780
git -C $W status -sb | head -1
git -C $W ls-remote --heads origin 'sven/con2-150-*'
ls "$(git -C $W rev-parse --absolute-git-dir)" | grep -c coder-mirror
ls $W/.coder-takeover.md
tail -1 ~/.local/state/herdr/plugins/ubuntudroid.coder-sessions/coder-sessions.log
herdr workspace list | python3 -c "import json, sys; w = next(w for w in json.load(sys.stdin)['result']['workspaces'] if w['workspace_id'] == 'w8B'); print(w['label'], w['tokens'])"
ssh -o BatchMode=yes asked-in-allengineering-e780.coder 'python3 -c "import urllib.request; print(urllib.request.urlopen(\"http://localhost:3284/messages\").read()[-700:])"'
```
Expected: `## sven/con2-150-batch-supply-the-engines-client-error-type-so-a-gated-ai...origin/sven/con2-150-...`; one remote head with that name; `0` markers; the handover file exists; the log line ends `sven/con2-150-... pushed to origin, the remote agent told`; label is the branch and tokens carry `coder_ticket: CON2-150` with the taken-over icon. The agentapi messages list shows `Taken over locally: work continues on branch sven/con2-150-...` as a user message once the agent has read it; if the agent was mid-turn the line sits queued in its composer until that turn ends, and the user can see it in the Coder web UI (`prefix+ctrl+w` on the workspace still opens the task page: the name token survives the takeover).

- [ ] **Step 5: Report**

Tell the user what is now true in the worktree, on origin, and in the remote composer, and anything from Step 4 that did not match.
