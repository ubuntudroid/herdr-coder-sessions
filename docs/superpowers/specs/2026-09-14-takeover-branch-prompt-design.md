# Take over a branchless session — Design

**Problem:** `prefix+ctrl+t` on a session that has not branched yet refuses:
its mirror is detached, and a local agent committing there would put its work on
no branch. The refusal is a herdr notification, which is easy to miss — on
2026-09-14 it fired four times for CON2-150 and read as "the key does nothing".

**Goal:** Make the takeover usable in that state. The key opens a popup that
offers to take the session over on a new branch, pre-filled with the name Linear
assigns the ticket, or to cancel. Accepting creates the branch at the mirror's
commit, pushes it, tells the remote agent, and continues the existing takeover.
Everything the takeover prints is visible in that popup, including failures.

**Architecture:** The `takeover` action opens a popup plugin pane and re-runs
`coder-sessions.py --takeover` inside it, the way the picker action opens `list`.
With a terminal, `hold()` already waits for a key on failure and `notify()`
becomes a printed line. The detached guard in `takeover()` turns from
`sys.exit` into a prompt; the answer is carried through the existing steps and
acted on only after `demote_mirror()`, because the last mirror refresh would
detach a branch created earlier. New code: a branch suggestion, a prompt, a
create-and-push step, and one ssh call that types a line into the remote agent.

**Tech stack:** Python 3.9+, standard library only. git, ssh, the `herdr` CLI,
optionally the `linear` CLI (schpet/linear-cli 2.6.0, `linear api`).

## Constraints

Inherited from `docs/superpowers/plans/2026-08-28-take-over-locally.md`, with
one amendment:

- Standard library only; two files stay two files; `agentty` learns nothing.
- Tests are `assert`s in `selftest()`. No framework, no test files.
- Comments carry a reason, never a restatement; `ponytail:` marks a named ceiling.
- **Remote writes:** the only write to the Coder workspace is one line typed
  into the agent's composer, at the user's explicit request from the prompt.
  Nothing is installed, started, paused or checked out over there.

## Behaviour

### Entry

`[[panes]] takeover` in `herdr-plugin.toml`: `placement = "popup"`, `width =
"90%"`, `height = "50%"`, command `python3 coder-sessions.py --takeover`. The
existing `[[actions]] takeover` keeps its command. In `main()`, `--takeover`
without a terminal on stdin resolves the session name first (argument, then
`CODER_SESSION`, then `focused_session()`), then opens the pane with `--env
CODER_SESSION=<name>` and returns. With a terminal it runs `takeover(name)`.
The standalone CLI path is unchanged: a shell has a terminal.

`notify(title, body)` prints `title: body` and returns when stdin is a
terminal. Without one it raises the herdr notification as today. The popup
therefore shows "Taking over X: reading the session over ssh" as a progress
line, and the notification path stays for anything that still runs without a
terminal.

### Guards, in order

1. `open_workspaces().get(name)` is a mirror workspace — else exit, as today.
2. The checkout is detached (`checkout_branch(...) == "HEAD"`) — **new:** prompt
   (below) instead of exiting. The answer is `new_branch`, or the prompt exits.
3. Remote agent kind, `takeover_agent`, history path — unchanged.

### The prompt

Shown only in guard 2. `session = session_named(name)` is read first for the
ticket and the display name. Text:

```
<name> (<ticket>) has no branch of its own yet: its mirror is detached.
Take it over on a new branch, pushed to origin, and tell the remote agent.

branch [<suggestion>]:
  enter = use this   type a name = use yours   ctrl-c = cancel
```

- Empty answer: the suggestion. Any text: that text, whole.
- `EOFError` / `KeyboardInterrupt`: the prompt answers `None`, the takeover
  notes the cancel and returns (exit 0). Nothing changed: the pane closes,
  agentty keeps running, the mirror stays a mirror. Not exit 130: the
  `__main__` handler holds any non-zero `SystemExit` for a keypress.
- Validation, re-prompting on failure with the reason: `git check-ref-format
  --branch <answer>` (syntax) and `git -C <checkout> rev-parse --verify -q
  refs/heads/<answer>` must find nothing (a branch that exists locally cannot
  be created with `-b`, and a branch checked out in another worktree is the
  case the detached mirror exists to avoid).

The suggestion sits in the prompt text, not inside the editable field. Same
semantics as a placeholder — enter keeps it, typing replaces it — at zero code.

### The suggestion

`suggest_branch(session, conf, linear=linear_branch) -> str`:

1. `ticket`: `readable_name(session)` when `TICKET_RE.fullmatch()` accepts
   it, else `None`. `readable_name` returns a ticket, a truncated display name
   or the lowercase session name, and only the first can match.
2. Ticket found: `linear(ticket)`. Non-empty result wins.
3. Fallback: `conf["branch_prefix"] + "-".join(p for p in (ticket.lower(),
   slug(display_name)) if p)`, with the ticket's own occurrence removed from
   the display name before slugging so it does not appear twice, and the
   session name when both parts are empty.

`linear_branch(ticket) -> str` runs
`linear api 'query { issue(id: "<ticket>") { branchName } }'` via
`subprocess.run(..., timeout=10)` and returns `branchName`, or `""` on a
missing binary, non-zero exit, timeout, or unparsable output. One `log_line`
names the reason; nothing is printed. `run()` is not used: it exits on a
missing binary, and Linear is optional here.

`slug(text) -> str`: Slack markup stripped (`SLACK_MARKUP`), apostrophes
dropped (so `engine's` reads `engines`, as Linear slugs it), lowercased, runs
of non-`[a-z0-9]` collapsed to `-`, trimmed, cut at 50 characters on a `-`
boundary. Pure.

`branch_prefix` is a new key in `DEFAULTS`, default `""`, documented in the
module docstring and the README table. Linear's own name already carries the
user's prefix, so the key only shapes the fallback.

### Order inside `takeover()`

```
guards 1-2, prompt                     local, instant
notify("Taking over ...")
session, remote agent kind, chosen agent, remote repo, history path   (as today)
mirror_session(name, conf, focus=True) (as today: refreshes the detached mirror)
agent_pane guard                       (as today)
branch = new_branch or checkout_branch(checkout) or branch
render turns, write TAKEOVER_FILE with branch=branch, exclude_locally
demote_mirror(checkout, branch)        (as today; for a new branch the ref it deletes never existed)
if new_branch:
    git -C checkout checkout -q -b new_branch          # HEAD unchanged, working tree kept
    push_error = git -C checkout push -q -u origin new_branch   (stderr on failure, else "")
    if not push_error: tell_remote_agent(host, message)
split local agent, run LAUNCH, close agentty            (as today)
report_tokens(... branch ...)                            (ticket token now derives from the branch)
if new_branch: herdr workspace rename <workspace> <new_branch>   (what mirror_session does on a branch move)
herdr workspace focus, note(...)                        (as today, note names the branch and the push result)
```

The branch is created after `demote_mirror()` and never before
`mirror_session()`: that call's detached path runs `checkout --detach` on any
mirror sitting on a branch the remote is not on, which would silently undo the
branch. `exclude_locally` and the handover need no change; `TAKEOVER_FILE` is
untracked and stays so on the new branch.

### Telling the remote agent

`tell_remote_agent(host, text)`: one `ssh_out(host, ...)` running a python3
one-liner that POSTs `{"type": "raw", "content": text + "\r"}` to
`http://localhost:3284/message`. This is the request agentty sends for every
keystroke, so it is accepted whether the agent is idle or mid-turn; mid-turn
the line queues as the agent's next input. `check=False`; a failure is one
`note()` line, not a stop — the local flow does not depend on it. Text, one
line:

```
Taken over locally: work continues on branch <name>, pushed to origin. If you keep working here, run `git fetch origin && git checkout <name>` first.
```

The remote checkout is not switched. Switching it under a running agent is
the one thing that could corrupt its in-progress edit, and the message gives
it the choice.

### Failure handling

| where | what happens |
| --- | --- |
| Linear CLI missing, logged out, slow, or the ticket unknown | fallback suggestion; one log line |
| invalid or existing branch name | re-prompt with the reason |
| ctrl-c / ctrl-d at the prompt | nothing changed; the takeover notes the cancel and returns, exit 0 |
| any exit before `demote_mirror()` | as today: held in the pane, safe to run again |
| `git checkout -b` fails after demotion | `sys.exit` with the git error; the worktree is already the user's (marker gone, handover written), and the reason is on screen |
| push fails | continue; skip the agent message; the closing `note()` says "not pushed: <reason>" |
| agent message fails | continue; one `note()` line |

### Out of scope

Editing the suggestion in place. Checking `origin` for the branch before
offering it (a push rejection is reported instead). Switching the remote
checkout. Confirming a takeover whose mirror already sits on a branch. Any
change to `prefix+ctrl+m` or the promote offer.

## Files

| file | change |
| --- | --- |
| `coder-sessions.py` | `slug()`, `linear_branch()`, `suggest_branch()`, `ask_branch()`, `tell_remote_agent()`; `takeover()` prompt and branch step; `--takeover` opens the pane without a terminal; `notify()` prints with one; `branch_prefix` default; selftest asserts |
| `herdr-plugin.toml` | `[[panes]] takeover`, popup |
| `README.md` | Usage paragraph on the keys (the popup replaces the start notification for take-over); Take over locally (the branchless case, the prompt, the push, the message); Configuration (`branch_prefix`); Requirements (optional `linear` CLI) |

## Testing

`--selftest` asserts, pure and offline:

- `slug("<@U0B9X> Batch: supply the engine's client error type") == "batch-supply-the-engines-client-error-type"`; the 50-character cut lands on a `-`; empty in, empty out.
- `suggest_branch(sess(display_name="fix CON2-150 now"), {"branch_prefix": "sven/"}, linear=lambda t: "sven/con2-150-from-linear") == "sven/con2-150-from-linear"`.
- Same with `linear=lambda t: ""` → `"sven/con2-150-fix-now"` (the ticket appears once); `sess(display_name="fix now")` → `"sven/fix-now"`; `sess()` → the session name; the empty default prefix gives `"con2-150-fix-now"`.

Acceptance, by hand: `prefix+ctrl+t` on the CON2-150 workspace. Expected: the
popup with Linear's name pre-filled; enter; progress lines; the popup closes; a
local Claude pane in the worktree on that branch; `git -C <worktree> status -sb`
shows `## sven/con2-150-...origin/sven/con2-150-...`; the remote agent's
composer shows the message; the sidebar row reads CON2-150 with the taken-over
icon. Then ctrl-c on a second session to confirm cancel changes nothing.
