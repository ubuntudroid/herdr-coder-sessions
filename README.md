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

Each session gets one workspace, named in the sidebar by [tokens](#sidebar-tokens):

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
last reported state and message. `enter` opens or focuses, `ctrl-o` opens the
highlighted session in the Coder web UI, `ctrl-r` refreshes,
and the preview pane shows the full report plus the prompt the session started
from.

Bind the action in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+c"
type = "plugin_action"
command = "ubuntudroid.coder-sessions.pick"
description = "Coder agent sessions"

[[keys.command]]
key = "prefix+ctrl+m"
type = "plugin_action"
command = "ubuntudroid.coder-sessions.refresh"
description = "refresh this Coder mirror"

[[keys.command]]
key = "prefix+ctrl+w"
type = "plugin_action"
command = "ubuntudroid.coder-sessions.web"
description = "open this Coder session in the web UI"

[[keys.command]]
key = "prefix+ctrl+t"
type = "plugin_action"
command = "ubuntudroid.coder-sessions.takeover"
description = "take this Coder session over locally"
```

The second key refreshes the focused session's mirror, and in a workspace that has
none yet it moves the session into one — see [Mirroring](#mirroring). The third opens
the focused session's page in the Coder web UI — see [The web UI](#the-web-ui). The
fourth ends the remote flow and continues it locally — see
[Take over locally](#take-over-locally).

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
./coder-sessions.py --web [<name>] # open a session in the Coder web UI
./coder-sessions.py --takeover [<name>] # hand it to a local agent, stop mirroring
./coder-sessions.py --restamp      # re-publish the sidebar tokens on open workspaces
./coder-sessions.py --selftest     # check the naming helpers
```

## Sidebar tokens

The plugin never touches the workspace **label**. That is one slot every plugin
writes to, so they fight over it. It publishes **metadata tokens** instead, which
`ui.sidebar.spaces.rows` places and styles per plugin:

| token | example | what it is |
|---|---|---|
| `$coder_icon` | `C■` | marks the workspace as mirroring a Coder one |
| `$coder_ticket` | `PROJ-1234` | the ticket the branch names, else the one the task names, else the display name with Slack markup stripped, truncated. Cleared when there is nothing to say beyond the session name |
| `$coder_name` | `example-task-4f21` | the Coder session name — also the plugin's own handle on the workspace |

**A plugin cannot add these to your config.** Put them in `~/.config/herdr/config.toml`
yourself, wherever you want them, and run `herdr server reload-config`:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "git_status"],
  [{ token = "$coder_icon" }, { token = "$coder_ticket", bold = true }],
  ["$coder_name"],
]
```

Two rows, not one: on a single line the session name crowds the ticket out and it
gets truncated. `fg` takes a strict `#RGB`/`#RRGGBB` — there are no theme colour
names, and anything else is rejected with `invalid ui config` (herdr keeps the
previous UI settings, so a bad value costs a reload, not your sidebar). Omitting
`fg` uses the default foreground, which is the only thing that follows the theme.

Without a label of its own, a session's workspace falls back to what herdr derives:
the mirror's branch name, or `~` when there is no mirror. The session name appears
nowhere but `$coder_name`.

Token names are a **global namespace** — `--source` scopes the sequence counter,
not the name. Two plugins publishing `$coder_name` would overwrite each other, and
either one clearing it would take the other's value with it. So every token this
plugin publishes is prefixed `coder_`. Change the prefix in the plugin's own
`config.json` (`token_prefix`) if something else already claims those names, and
mirror it in `rows`; set it to `""` for bare `icon` / `ticket` / `name`.

Tokens are display-only: a herdr restart drops them. The plugin re-stamps a
workspace whenever the picker runs, recognising it by the session name herdr's
agent detection reads off the agentty pane. `--restamp` is the same thing on
demand, and is idempotent.

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
3. refreshes the one branch reviewr will diff against — the repository's base pick if you set
   one, otherwise whatever `origin/HEAD` names — because a clone whose copy of that branch
   predates what the session branched from puts every commit merged upstream since inside the
   diff, as files the branch never touched;
4. fetches `+HEAD:<branch>` **straight from the workspace over ssh**, so commits the agent has
   not pushed are included, onto a local branch with the session's own name;
5. creates a herdr worktree from that clone, which makes the workspace worktree-backed (herdr
   then shows the branch under the name in the sidebar);
6. applies the session's uncommitted work: `git diff HEAD` for tracked files, plus a tar of
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
  workspace's mirror — or, in a workspace that has none yet, moves the session into one. It
  reads the session name from the [`$coder_name` token](#sidebar-tokens), so it needs no argument,
  and refuses cleanly in a workspace that is not a session's. It resets the mirror hard, so it
  will not guess: after a herdr restart has dropped the tokens, open the picker once (or run
  `--restamp`) to put them back.
- **On first open**, as part of building the workspace.

Re-picking an already-open session only focuses it; it does not refresh.

The mirror is **derived, never authored in**: `--mirror <name>` refreshes it by resetting hard
to the session's current state. A marker in the worktree's git dir (not the working tree, so it
never shows in `git status`) records that the worktree is a mirror; without it the refresh
refuses, so a worktree you made yourself is never reset.

### When the session is still on your branch

A session that has not branched yet sits on `main`, and so does your clone: the only worktree
on that branch is your own checkout, which the plugin will not reset. The session opens with
the agent pane alone.

The idle hook stays on, and watches the branch instead of the mirror. The turn the agent
branches, a pane splits in below the agent and asks whether to move the session into a mirror.
`y` builds the worktree workspace, opens reviewr, and closes the agent's old pane — herdr
collapses whatever that empties, so a session that had a workspace to itself takes it along,
and one parked in a tab of yours loses only that tab. The single pane it will not close is the
last one in a workspace this plugin did not open, because that would close your workspace too;
agentty is left running there instead, and says so. `n` closes the offer, and nothing asks
again until the branch changes.
`prefix+ctrl+m` makes the same move whenever you want it.

Two deliberate choices: the source refspec is `HEAD`, not the branch name, because a workspace
can hold several session branches and `HEAD` is unambiguous; and `git add -N` is *not* used to
capture untracked files, because it writes to the live agent's index and would show up in its
own `git status`.

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
~14,000 tokens. Rendering is ~70× cheaper, works between agents — a codex session
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

Taking over is one-way. The worktree is yours afterwards, and `prefix+ctrl+m` on
it says so rather than refusing generically: it recognises the taken-over icon and
answers that there is no mirror left to refresh. It never reaches
`mirror_session`'s not-a-mirror guard, because agentty went with the takeover.

## The web UI

`prefix+ctrl+w` opens the focused session's page in the Coder web UI, and `ctrl-o`
in the picker does the same for the highlighted row —
`<deployment>/tasks/<owner>/<task id>`, the same link the UI's own task list
builds — in the machine's default browser. That is the task page, not the
workspace page: it has the agent's conversation, the apps, and the controls for
pausing or restarting the session.

The deployment comes from `$CODER_URL`, else the `url` file in the coder CLI's
config dir, so it costs no network call. Like the refresh key it needs no
argument: with no name it reads the session off the focused workspace, and refuses
cleanly in one that is not a session's. Stopped sessions still open — their page
is where you start them again.

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
- **[reviewr](https://github.com/persiyanov/herdr-reviewr) 0.30.0+** for the
  review pane. Not installed means the agent pane alone; the mirror worktree is a
  normal worktree, so any review tool can be pointed at it. The version matters
  only for step 3 above: 0.30.0 is where reviewr's base became the `--base` flag,
  then a per-repository pick, then `origin/HEAD`, and where the `base_branches`
  config key was removed. Older reviewrs have no pick ref, so the refresh always
  lands on `origin/HEAD` while reviewr may still resolve a `base_branches` entry
  nothing fetched.

### The ideal setup

herdr 0.8.2 on macOS with `fzf` and reviewr 0.34.0 installed, `coder` logged
in with its ssh config generated, and every repo you review cloned under one
root as `<owner>/<repo>` (the same layout gh-dash's `repoPaths` uses).

### When something is missing

Nothing here fails with a traceback; each degrades to the next-best thing:

| missing | what happens |
| --- | --- |
| `coder`, `git`, `ssh` | one line naming the binary, then it stops |
| an old `coder` without `task` | one line saying so, with the CLI's own answer |
| `fzf` | the picker refuses and points at `--list` / `--open` |
| ssh to the session | the session opens without a mirror |
| no local clone | the session opens without a mirror, naming the path it looked under and the config file to change |
| a session still on the branch your clone has checked out | the session opens without a mirror, and is offered one the turn the agent branches |
| a shallow session clone | the mirror comes from `origin` instead, with the agent's commits folded into one diff |
| reviewr | the agent pane opens alone |
| reviewr below 0.30.0 | the base refresh can only guess `origin/HEAD`, so a `base_branches` base stays stale |
| Python below 3.9 | one line naming the interpreter and its version |
| an unwritable state dir | previews go empty and the mirror offer repeats each turn; the picker still works |
| an `agentty` that lost its executable bit | it runs through the interpreter instead |
| a coder CLI that was never logged in | the web-UI key says so and names `CODER_URL`; everything else still works |

## agentty

`agentty <workspace-ssh-host>` attaches to an agent running under
[agentapi](https://github.com/coder/agentapi) on another machine, opening its own
ssh tunnel. It exists because `agentapi attach` renders the whole remote screen
each frame with no viewport, so nothing scrolls.

It adds PgUp/PgDn/Home/End and mouse-wheel scrolling, a caret at the composer,
local echo (the round trip to a remote workspace is ~600 ms), suppression of the
agent's empty-composer hint, and folding for panes narrower than the agent.
Shift+Enter inserts a newline: herdr sends it as xterm modifyOtherKeys, which
the agent ignores, so it is rewritten to the ESC+CR the agent reads as one.
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
| `takeover_agent` | `"match"` | which agent [takes a session over locally](#take-over-locally): `"match"` uses whichever agent ran on the workspace, `"claude"` or `"codex"` always uses that one |
| `token_prefix` | `"coder_"` | namespace for the [sidebar tokens](#sidebar-tokens): this prefix plus `icon` / `ticket` / `name`. Change it if another plugin already claims those names, and mirror it in `rows` |

## Notes

- The agent renders at whatever width its launcher chose (`--term-width` in the
  Coder template's generated `start.sh`, commonly narrower than your pane).
  There is no resize endpoint, so the content width is fixed for the life of a
  session; `agentty` folds rather than truncates it.
- `coder task list` takes roughly 700ms, too slow for an fzf preview per
  keystroke, so the list writes a cache that the preview reads.

## License

MIT. See [LICENSE](LICENSE).
