# herdr-coder-sessions

Browse running [Coder](https://coder.com) agent sessions in herdr, and open each
one as its own workspace.

Coder task workspaces have no tmux or screen: the agent runs under
[agentapi](https://github.com/coder/agentapi), which owns the PTY and exposes it
over HTTP on port 3284. `agentapi attach` renders that screen but has no
viewport, so nothing scrolls. This plugin builds a workspace per session around
[`agentty`](#agentty), which adds scrolling, mouse wheel, a caret, and
local echo over the same endpoints.

## Install

```sh
herdr plugin install ubuntudroid/herdr-coder-sessions
```

Nothing to build: two Python files, standard library only. See
[Requirements](#requirements) for what has to be on the machine, and
[Configuration](#configuration) for the one setting most people need to change.

## Layout

Each session gets one workspace, labelled with the session name:

```
┌───────────────────────────┬───────────────────┐
│ agentty <session>.coder   │ reviewr           │
│ the agent's live screen   │ the session's     │
│                           │ changes, mirrored │
└───────────────────────────┴───────────────────┘
```

The review pane is [reviewr](https://github.com/persiyanov/herdr-reviewr), and it is optional —
without it you get the agent pane alone. Need a shell on the box? `ssh <session>.coder` in any
pane.

Picking a session that is already open focuses its workspace instead of building
a second one; those rows are marked `●` in the list.

## Usage

The list shows every Coder task whose workspace is running, with the agent's
last reported state and message. `enter` opens or focuses, `ctrl-r` refreshes,
and the preview pane shows the full report plus the prompt the session started
from.

Bind the action in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+c"
type = "plugin_action"
command = "ubuntudroid.coder-sessions.pick"
description = "Coder agent sessions"
```

Or, without installing the plugin, point a popup straight at the script:

```toml
[[keys.command]]
key = "prefix+shift+c"
type = "popup"
command = "python3 /path/to/herdr-coder-sessions/coder-sessions.py"
description = "Coder agent sessions"
width = "90%"
height = "80%"
```

The script also works standalone:

```sh
./coder-sessions.py                # picker
./coder-sessions.py --list         # rows only
./coder-sessions.py --open <name>  # open or focus one session
./coder-sessions.py --relabel      # re-apply the label scheme to open workspaces
./coder-sessions.py --selftest     # check the naming helpers
```

Workspaces are labelled `C■ <ticket> · <session>`, e.g.
`C■ PROJ-1234 · example-task-4f21`. The ticket is taken from the task's display
name, the agent's last report, or the initial prompt; failing that the display
name with Slack markup stripped, failing that the session name alone. The `C■`
prefix marks a workspace as mirroring a Coder one.

`--relabel` is for workspaces opened before a ticket was known, or before this
scheme existed — it renames them and stamps the `coder` metadata token that
duplicate detection keys off. It is idempotent.

## What's in here

Two things, deliberately in one repo:

| file | what it is |
| --- | --- |
| `coder-sessions.py` | the herdr plugin: session list, workspace builder |
| `agentty` | a scrollable replacement for `agentapi attach`, useful on its own |

The plugin runs the `agentty` sitting next to it, not one from `PATH`, so
installing the plugin is all you need and the two can never drift apart. See
[agentty](#agentty) for using it standalone.

## Mirroring

A session's work lives on the remote workspace, and usually there is no PR yet — so the
plugin reproduces the session locally and reviews that. On open it:

1. asks the session for its repo root, branch and `origin` URL over ssh;
2. finds the matching local clone (`clone_root/<owner>/<repo>`, the same mapping gh-dash's
   `repoPaths` uses) — if there is none, it falls back to a plain workspace and skips the rest;
3. fetches `+HEAD:<branch>` **straight from the workspace over ssh**, so commits the agent has
   not pushed are included, onto a local branch with the session's own name;
4. creates a herdr worktree from that clone, which makes the workspace worktree-backed (herdr
   then shows the branch under the name in the sidebar);
5. applies the session's uncommitted work: `git diff HEAD` for tracked files, plus a tar of
   `git ls-files -o --exclude-standard` for untracked ones.

You end up with agentty over ssh on the left and [reviewr](https://github.com/persiyanov/herdr-reviewr)
on the right, with no wiring. A brand-new mirror gets reviewr from reviewr's own
`worktree.created` subscription; every open after that reuses the checkout, which fires
`worktree.opened` instead — an event reviewr does not watch
([#82](https://github.com/persiyanov/herdr-reviewr/issues/82)) — so on exactly those openings the
plugin asks for the pane itself, honouring reviewr's own placement and `auto_open` settings. If
reviewr is not installed, or you turned its auto-open off, nothing is opened and the mirror is
still there for whatever tool you prefer.

### When it refreshes

- **After every agent turn.** The plugin launches agentty with
  `AGENTTY_ON_IDLE="<script> --mirror <session>"`, and agentty runs that hook when the agent's
  status goes `running → stable`. It already watches `/events` for the dot, so nothing polls, and
  the refresh lands exactly when new work exists and nothing is mid-write. `AGENTTY_ON_IDLE` is
  generic — agentty knows nothing about this plugin.
- **On demand**, with `prefix+ctrl+m` (the `refresh` action), which refreshes the focused
  workspace's mirror. It reads the session name from the `coder` metadata token, so it needs no
  argument, and refuses cleanly in a workspace that is not a session's.
- **On first open**, as part of building the workspace.

Re-picking an already-open session only focuses it; it does not refresh.

The mirror is **derived, never authored in**: `--mirror <name>` refreshes it by resetting hard
to the session's current state. A marker in the worktree's git dir (not the working tree, so it
never shows in `git status`) records that the worktree is a mirror; without it the refresh
refuses, so a worktree you made yourself is never reset.

Two deliberate choices: the source refspec is `HEAD`, not the branch name, because a workspace
can hold several session branches and `HEAD` is unambiguous; and `git add -N` is *not* used to
capture untracked files, because it writes to the live agent's index and would show up in its
own `git status`.

## Requirements

Needed:

- **herdr 0.8.0+** on **macOS or Linux**. It uses popup plugin panes, workspace
  metadata tokens and the worktree CLI; there is no Windows support, because
  `agentty` drives a Unix terminal directly.
- **Python 3.9+**, standard library only. No build step, nothing to compile.
  Whatever `python3` resolves to on your `PATH` is what runs.
- **`coder` CLI, logged in, new enough to have `coder task`.** The list is
  `coder task list -o json`.
- **ssh access to each session**, as `<session-name><host_suffix>`. Coder's
  generated ssh config gives `<name>.coder`; set `host_suffix` if yours differs.

Optional, each with a working fallback:

- **`fzf`** for the picker. Without it, `--list` and `--open <name>` still work.
- **A local clone of the session's repo**, at `clone_root/<owner>/<repo>`. This
  is what mirroring needs; without a match the session opens as a plain
  workspace. `clone_root` defaults to `~/projects/github` — the setting most
  people have to change.
- **[reviewr](https://github.com/persiyanov/herdr-reviewr)** for the review pane.
  Not installed means the agent pane alone; the mirror worktree is a normal
  worktree, so any review tool can be pointed at it.

### The ideal setup

herdr 0.8.2 on macOS with `fzf` and reviewr installed, `coder` logged in with
its ssh config generated, and every repo you review cloned under one root as
`<owner>/<repo>` (the same layout gh-dash's `repoPaths` uses).

### When something is missing

Nothing here fails with a traceback; each degrades to the next-best thing:

| missing | what happens |
| --- | --- |
| `coder`, `git`, `ssh` | one line naming the binary, then it stops |
| an old `coder` without `task` | one line saying so, with the CLI's own answer |
| `fzf` | the picker refuses and points at `--list` / `--open` |
| ssh to the session | the session opens without a mirror |
| no local clone | the session opens without a mirror, naming the path it looked under and the config file to change |
| a shallow session clone | the mirror comes from `origin` instead, with the agent's commits folded into one diff |
| reviewr | the agent pane opens alone |
| Python below 3.9 | one line naming the interpreter and its version |
| an unwritable state dir | previews go empty; the picker still works |
| an `agentty` that lost its executable bit | it runs through the interpreter instead |

## agentty

`agentty <workspace-ssh-host>` attaches to an agent running under
[agentapi](https://github.com/coder/agentapi) on another machine, opening its own
ssh tunnel. It exists because `agentapi attach` renders the whole remote screen
each frame with no viewport, so nothing scrolls.

It adds PgUp/PgDn/Home/End and mouse-wheel scrolling, a caret at the composer,
local echo (the round trip to a remote workspace is ~600 ms), suppression of the
agent's empty-composer hint, and folding for panes narrower than the agent.
`Ctrl+]` quits; `agentty --selftest` checks the pure helpers. Inside a herdr pane
it also reports the remote agent's state, so the session gets a real agent dot.

Standalone install — it is one stdlib-only file, so copying it is the whole
procedure:

```sh
curl -fsSL https://raw.githubusercontent.com/ubuntudroid/herdr-coder-sessions/main/agentty \
  -o ~/.local/bin/agentty && chmod +x ~/.local/bin/agentty
```

Plugin users need none of that.

## Configuration

Optional, in `$HERDR_PLUGIN_CONFIG_DIR/config.json`:

| key | default | meaning |
| --- | --- | --- |
| `host_suffix` | `".coder"` | appended to the session name to form the ssh host |
| `clone_root` | `"~/projects/github"` | where `<owner>/<repo>` clones live |
| `mirror` | `true` | mirror the session into a local worktree on open |

## Notes

- The agent renders at whatever width its launcher chose (`--term-width` in the
  Coder template's generated `start.sh`, commonly narrower than your pane).
  There is no resize endpoint, so the content width is fixed for the life of a
  session; `agentty` folds rather than truncates it.
- `coder task list` takes roughly 700ms, too slow for an fzf preview per
  keystroke, so the list writes a cache that the preview reads.

## License

MIT. See [LICENSE](LICENSE).
