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
┌───────────────────────────────┐
│ agentty <session>.coder       │  two thirds — the agent's live screen
├───────────────────────────────┤
│ ssh <session>.coder           │  one third — a plain shell in the workspace
└───────────────────────────────┘
```

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
| `ratio` | `0.667` | share of the height given to the agentty pane |
| `host_suffix` | `".coder"` | appended to the session name to form the ssh host |

## Notes

- The agent renders at whatever width its launcher chose (`--term-width` in the
  Coder template's generated `start.sh`, commonly narrower than your pane).
  There is no resize endpoint, so the content width is fixed for the life of a
  session; `agentty` folds rather than truncates it.
- `coder task list` takes roughly 700ms, too slow for an fzf preview per
  keystroke, so the list writes a cache that the preview reads.
