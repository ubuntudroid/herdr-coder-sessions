# herdr-coder-sessions

Browse running [Coder](https://coder.com) agent sessions in herdr, and open each
one as its own workspace.

Coder task workspaces have no tmux or screen: the agent runs under
[agentapi](https://github.com/coder/agentapi), which owns the PTY and exposes it
over HTTP on port 3284. `agentapi attach` renders that screen but has no
viewport, so nothing scrolls. This plugin builds a workspace per session around
[`agentty`](#requirements), which adds scrolling, mouse wheel, a caret, and
local echo over the same endpoints.

## Layout

Each session gets one workspace, labelled with the session name:

```
┌───────────────────────────┬───────────────────┐
│ agentty <session>.coder   │ reviewr           │
│ the agent's live screen   │ the session's     │
│                           │ changes, mirrored │
└───────────────────────────┴───────────────────┘
```

reviewr opens itself (herdr's `worktree.created` event); the plugin never mentions it. Need a
shell on the box? `ssh <session>.coder` in any pane.

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
description = "Coder agent sessions (agentty + ssh)"
```

Or, without installing the plugin, point a popup straight at the script:

```toml
[[keys.command]]
key = "prefix+shift+c"
type = "popup"
command = "python3 /path/to/herdr-coder-sessions/coder-sessions.py"
description = "Coder agent sessions (agentty + ssh)"
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

`reviewr` auto-opens in that workspace on herdr's `worktree.created` event, so the review pane
needs no wiring — you end up with agentty over ssh on the left and reviewr on the right.

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

- `coder` CLI, logged in. Sessions come from `coder task list`.
- `fzf` for the picker. `--list` and `--open` work without it.
- An ssh host per session. Coder's generated ssh config gives `<name>.coder`;
  change `host_suffix` if yours differs.
- Python 3.9+, standard library only. No build step, nothing to compile.

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
