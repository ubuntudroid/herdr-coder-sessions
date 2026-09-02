#!/usr/bin/env python3
"""Browse running Coder agent sessions and open each as its own herdr workspace.

Coder task workspaces have no tmux: the agent runs under agentapi on port 3284.
Picking a session opens a workspace running `agentty` on the session, with the
session's changes mirrored into a local worktree beside it. Picking a session
that is already open focuses its workspace instead of building a second one.

    coder-sessions.py                  fzf picker (the plugin's pane and action)
    coder-sessions.py --list           print the rows, no picker
    coder-sessions.py --open NAME      open or focus one session's workspace
    coder-sessions.py --show NAME      preview text for one session
    coder-sessions.py --mirror NAME    what a finished agent turn triggers: refresh
                                       the mirror, or offer one once it is possible
    coder-sessions.py --refresh        same, for the focused workspace (plugin action)
    coder-sessions.py --web [NAME]     open a session in the Coder web UI: the one
                                       named, else the focused workspace's
    coder-sessions.py --promote        the pane that asks before moving a session
    coder-sessions.py --takeover [NAME] hand this session's conversation to a local
                                        agent and stop mirroring it
    coder-sessions.py --restamp        re-publish the sidebar tokens on open workspaces
    coder-sessions.py --selftest       check the naming helpers

`coder task list` costs ~700ms, too slow to run per keystroke, so --list writes
a cache that --show reads back.

The session's identity goes in `report-metadata` tokens, never in the workspace
label: the label is one slot every plugin writes to, while tokens are placed and
styled per plugin by `ui.sidebar.spaces.rows`. See the README for the snippet --
a plugin cannot add it for you.

Settings, all optional, in $HERDR_PLUGIN_CONFIG_DIR/config.json:

    {"host_suffix": ".coder", "clone_root": "~/projects/github", "mirror": true,
     "takeover_agent": "match", "token_prefix": "coder_"}
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
import webbrowser
from datetime import datetime, timezone

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
PLUGIN_ID = "ubuntudroid.coder-sessions"  # keep in step with herdr-plugin.toml

# Runtime state belongs in the state dir herdr provides, never in the plugin root:
# a GitHub install replaces that checkout wholesale. herdr sets the variable only
# for the processes it spawns itself, so agentty's on-idle refresh -- and any
# hand-run command -- has to rebuild the same path, or its log lands in a tempdir
# nobody thinks to look in and the cache is written twice over.
STATE = (os.environ.get("HERDR_PLUGIN_STATE_DIR")
         or os.path.expanduser(f"~/.local/state/herdr/plugins/{PLUGIN_ID}"))
CACHE = os.path.join(STATE, f"coder-sessions-{os.getuid()}.json")
LOG = os.path.join(STATE, "coder-sessions.log")
SELF = os.path.abspath(__file__)

try:
    os.makedirs(STATE, exist_ok=True)
except OSError:
    pass  # writes report their own failure; a missing dir must not stop the picker

DEFAULTS = {
    "host_suffix": ".coder",  # ssh host is <session name> + this
    "clone_root": "~/projects/github",  # <clone_root>/<owner>/<repo>, as gh-dash maps it
    "mirror": True,        # mirror the session into a local worktree when we can
    # Which agent picks a session up locally. "match" uses whatever agentapi ran
    # on the workspace; a name ("claude", "codex") always uses that one, since the
    # handover is plain markdown and any agent can read any other's.
    "takeover_agent": "match",
    # Namespace for this plugin's sidebar metadata tokens: the prefix plus each
    # name in TOKEN_SUFFIXES, rendered by `ui.sidebar.spaces.rows` as
    # `$coder_icon` and so on. Prefixed because the token namespace is global --
    # `--source` scopes the seq counter only, so two plugins publishing the same
    # name overwrite each other, and either one clearing it takes the other's
    # value with it. Change this if something else already claims these names,
    # and mirror it in `rows`.
    "token_prefix": "coder_",
}

DIM, BOLD, RESET = "\x1b[2m", "\x1b[1m", "\x1b[0m"
STATE_COLOR = {"idle": "\x1b[32m", "working": "\x1b[33m",
               "complete": "\x1b[32m", "failure": "\x1b[31m", "error": "\x1b[31m"}


def merge_settings(user):
    """DEFAULTS overlaid with the user's config.json."""
    return dict(DEFAULTS, **user)


def settings():
    path = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    user = {}
    if path:
        try:
            with open(os.path.join(path, "config.json")) as handle:
                user = json.load(handle)
        except (OSError, ValueError):
            pass  # absent or malformed config just means defaults
    return merge_settings(user)


def config_hint():
    """Where to change the settings, named so a message about them is actionable."""
    path = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if path:
        return os.path.join(path, "config.json")
    return f"config.json under `herdr plugin config-dir {PLUGIN_ID}`"


def run(argv, check=True):
    """Run a command, returning stdout. stderr is dropped: the coder CLI prints
    a version-mismatch warning there on every call.

    A missing binary is fatal whatever `check` says -- there is no partial answer
    from a tool that is not installed -- so it exits with a sentence rather than
    the traceback the caller would otherwise get.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(f"{argv[0]} is not installed, or not on this PATH")
    if check and proc.returncode != 0:
        sys.exit(f"{argv[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def herdr(*args):
    out = run([HERDR, *args]).strip()
    if not out:
        return {}  # mutators such as `pane run` answer with nothing on success
    try:
        return json.loads(out)
    except ValueError:
        sys.exit(f"herdr {' '.join(args)} returned no JSON: {out[:200]}")


def agentty_cmd():
    """Prefer the agentty shipped next to this script over one on PATH.

    The pane must run the copy this plugin was installed with -- a PATH lookup
    would silently pick up a different version, or nothing at all.
    """
    local = os.path.join(os.path.dirname(SELF), "agentty")
    if os.access(local, os.X_OK):
        return shlex.quote(local)
    if os.path.isfile(local):
        # A copy that lost its executable bit -- a zip download, a restrictive
        # umask -- still runs perfectly well through the interpreter.
        return f"{shlex.quote(sys.executable)} {shlex.quote(local)}"
    if shutil.which("agentty"):
        return "agentty"
    sys.exit("agentty not found: expected it beside this script or on PATH")


def running_sessions(running_only=True):
    """Coder tasks. By default only those whose workspace is up, so agentapi is
    reachable; pass False when stamping workspace tokens, where a stopped
    session's ticket is still worth showing."""
    out = run(["coder", "task", "list", "-o", "json"])
    start = out.find("[")
    try:
        tasks = json.loads(out[start:]) if start >= 0 else []
    except ValueError:
        sys.exit("could not read `coder task list -o json` -- this needs a coder "
                 f"CLI with task support. It answered: {out.strip()[:200]}")
    if not running_only:
        return tasks
    return [t for t in tasks if t.get("workspace_status") == "running"]


def coder_url():
    """The deployment the coder CLI is logged in to.

    Read out of the CLI's own config rather than asked for over the network:
    `coder whoami` spends a round trip to print the same string. The mac config
    dir is tried on every platform -- it simply is not there on Linux -- so the
    lookup needs no platform test.
    """
    if os.environ.get("CODER_URL"):
        return os.environ["CODER_URL"].strip()
    dirs = [os.environ.get("CODER_CONFIG_DIR"),
            os.path.expanduser("~/Library/Application Support/coderv2"),
            os.path.join(os.environ.get("XDG_CONFIG_HOME")
                         or os.path.expanduser("~/.config"), "coderv2")]
    for base in filter(None, dirs):
        try:
            with open(os.path.join(base, "url")) as handle:
                return handle.read().strip()
        except OSError:
            continue
    sys.exit("could not find the Coder deployment: no CODER_URL, and no `url` "
             "in the coder CLI's config dir -- is `coder login` done?")


def task_url(task, base):
    """The task's page in the Coder web UI, in the shape the UI's own links use.

    The id is the task's own, not the workspace's: a task carries both, and they
    are different objects with different ids.
    """
    return f"{base.rstrip('/')}/tasks/{task['owner_name']}/{task['id']}"


METADATA_SOURCE = "coder-sessions"
REVIEWR = "persiyanov.reviewr"  # the review pane this plugin opens beside the agent


# What each token carries, under `token_prefix`:
#   icon    C■, marking the workspace as mirroring a Coder one
#   ticket  the ticket the branch or task names, else a summary
#   name    the Coder session name -- also this plugin's key on a workspace
TOKEN_SUFFIXES = ("icon", "ticket", "name")


def token_names(conf=None):
    """This plugin's token names, keyed by suffix: `{"icon": "coder_icon", ...}`."""
    prefix = (conf or settings())["token_prefix"]
    return {suffix: prefix + suffix for suffix in TOKEN_SUFFIXES}


def name_token(conf=None):
    """The token carrying the session name -- this plugin's handle on a workspace."""
    return token_names(conf)["name"]


# Ticket ids make the best thing to show. Prefixes here carry digits (CON2,
# PGROWTH), so allow them after the first letter; skip encodings and standards
# that share the shape.
TICKET_RE = re.compile(r"\b(?!UTF-|ISO-|RFC-|SHA-)[A-Z][A-Z0-9]{1,9}-\d{1,5}\b")
SLACK_MARKUP = re.compile(r"<[^>]*>|<[^>]*$")  # <@U123>, <https://x|text>, truncated


def branch_ticket(branch):
    """The ticket a branch name leads with, if any: `automations/con2-106-x` -> CON2-106.

    Anchored at the last path segment rather than searched, because that is where
    both the agents and I put the key, and a bare search would read any `-<digit>`
    further along as one.

    ponytail: a branch that genuinely starts `add-2-...` still reads as ADD-2.
    Telling those apart needs the project's real key list, which nothing here has.
    """
    segment = (branch or "").rsplit("/", 1)[-1].upper()
    found = TICKET_RE.match(segment)
    return found.group(0) if found else None


def readable_name(session, limit=28, branch=""):
    """A human name for the session: the ticket the branch names, else one the
    task names, else the task's own display name with Slack markup stripped.

    The branch wins because it is fixed for the life of the session, while the
    task text moves on -- an agent's latest message often names a different ticket
    than the branch it is working on.
    """
    ticket = branch_ticket(branch)
    if ticket:
        return ticket
    haystack = " ".join(filter(None, (session.get("display_name"),
                                      session.get("message"), session.get("prompt"))))
    found = TICKET_RE.search(haystack)
    if found:
        return found.group(0)
    text = " ".join(SLACK_MARKUP.sub(" ", session.get("display_name") or "").split())
    if not text:
        return session["name"]
    return text[:limit].rstrip() + "…" if len(text) > limit else text


ICON = "C■"  # stands in for the Coder logo: these workspaces mirror a Coder one


def session_tokens(session, branch="", conf=None, icon=ICON):
    """What this plugin puts in the sidebar: `{token name: value}`, "" to clear.

    Three tokens rather than one string, so the user's `ui.sidebar.spaces.rows`
    decides where each piece goes and how it is styled. The workspace label is
    left alone -- it is one slot every plugin writes to, and herdr-git-status is
    already prepending to it.
    """
    names = token_names(conf)
    head = readable_name(session, branch=branch)
    return {
        names["icon"]: icon,
        # Nothing better to say than the name itself: cleared, so the row does not
        # show the session name twice.
        names["ticket"]: "" if head == session["name"] else head,
        names["name"]: session["name"],
    }


def report_tokens(workspace, values, conf=None):
    """Publish (or clear, on "") this plugin's tokens on a workspace, in one call.

    No `--seq`: a report without one is always accepted, and this plugin has no
    ordering to defend -- every write is the latest state of one session. No
    `--ttl-ms`: nothing here ticks, so an expiry would blank the row seconds after
    the event that wrote it, with nothing left to refresh it.
    """
    args = []
    for token, value in values.items():
        args += ["--clear-token", token] if value == "" else ["--token", f"{token}={value}"]
    if args:
        herdr("workspace", "report-metadata", workspace, "--source", METADATA_SOURCE, *args)


def clear_tokens(workspace, conf=None):
    """Drop every token this plugin owns on a workspace."""
    report_tokens(workspace, {t: "" for t in token_names(conf).values()}, conf)


MIRROR_MARK = "coder-mirror"  # marks a worktree as derived, so refreshing may reset it
# Where the plugin records each branch tip it set. The worktree marker proves
# ownership only while the worktree exists; this outlives it, so a leftover mirror
# branch is still recognisable as one.
MIRROR_REFS = "refs/coder-mirror"


def mirror_marker(checkout):
    """Marker path inside the worktree's git dir, so it never shows in status."""
    gitdir = run(["git", "-C", checkout, "rev-parse", "--absolute-git-dir"]).strip()
    return os.path.join(gitdir, MIRROR_MARK)


def claim_branch(clone, branch, tip):
    """Record that the plugin put `branch` at `tip`."""
    run(["git", "-C", clone, "update-ref", f"{MIRROR_REFS}/{branch}", tip])


def is_mirror(checkout):
    stray = os.path.join(checkout, "." + MIRROR_MARK)  # pre-0.2 in-tree marker
    if os.path.exists(stray):
        os.remove(stray)
        open(mirror_marker(checkout), "w").close()
    return os.path.exists(mirror_marker(checkout))


def repo_slug(origin_url):
    """`owner/repo` from any of git@host:o/r.git, https://host/o/r.git, ssh://host/o/r."""
    url = origin_url.strip().removesuffix(".git")
    if not url:
        return None
    body = url.split("://", 1)[-1]
    if "@" in body and ":" in body and "/" in body.split(":", 1)[1]:
        body = body.split(":", 1)[1]  # scp-style: host:owner/repo
    parts = [seg for seg in body.split("/") if seg]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def clone_path(slug, clone_root):
    """Where that repo is cloned locally, or None if it is not."""
    if not slug:
        return None
    path = os.path.join(os.path.expanduser(clone_root), *slug.split("/"))
    return path if os.path.isdir(os.path.join(path, ".git")) else None


def ssh_out(host, command, check=True):
    """Run one command on the session host. Trailing newline stripped: a stray
    CR here silently corrupts a git refspec."""
    return run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes",
                host, command], check=check).strip()


def remote_repo(host):
    """The session's checkout: (repo path, branch, origin slug, is it shallow).

    The login directory is inside the repo (the Coder template puts it there), so
    ask git from there rather than guessing a name. Shallowness comes along in the
    same round trip because it decides where the mirror's commits come from.
    """
    probe = ('cd "$HOME" 2>/dev/null; '
             'root=$(git rev-parse --show-toplevel 2>/dev/null) || '
             'root=$(for d in "$HOME"/*/.git; do dirname "$d"; break; done); '
             '[ -n "$root" ] || exit 1; '
             'printf "%s\t%s\t%s\t%s\n" "$root" '
             '"$(git -C "$root" rev-parse --abbrev-ref HEAD)" '
             '"$(git -C "$root" remote get-url origin 2>/dev/null)" '
             '"$(git -C "$root" rev-parse --is-shallow-repository)"')
    line = ssh_out(host, probe, check=False)
    if not line or "\t" not in line:
        return None, None, None, False
    root, branch, origin, shallow = (line.split("\t") + ["", "", ""])[:4]
    return root.strip(), branch.strip(), repo_slug(origin), shallow.strip() == "true"


# agentapi launched the agent and names it on its own command line, which makes
# this the one authoritative answer. Both ~/.claude and ~/.codex exist on every
# Coder workspace, so "which history directory has files" is not a test.
AGENT_TYPE = re.compile(r"agentapi\s+server\b[^\n]*?--type\s+(\S+)")

# Picks the session's own codex rollout. Subagent threads live in the same tree
# and carry the parent's session_id, so `thread_source` is the discriminator;
# `cwd` keeps a second checkout on the same box out of it. The agent runs in the
# repo root or a subdirectory, so match on prefix of the cwd argument (git root
# from remote_repo). Run on the workspace because that is where the files are, and
# because shipping 19 candidates back to choose between them would be absurd.
CODEX_PICK = """
import json, glob, os, sys
best = ("", 0.0)
for path in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")):
    try:
        with open(path) as handle:
            meta = json.loads(handle.readline()).get("payload") or {}
    except (OSError, ValueError):
        continue
    cwd = meta.get("cwd") or ""
    if meta.get("thread_source") != "user" or not (
            cwd == sys.argv[1] or cwd.startswith(sys.argv[1] + "/")):
        continue
    stamp = os.path.getmtime(path)
    if stamp > best[1]:
        best = (path, stamp)
print(best[0])
"""

# Claude Code files one transcript per session under an encoded copy of the cwd,
# so the directory is known and only the file is in question. The agent runs in the
# repo root or a subdirectory, so glob the encoded root with a prefix and reject
# non-boundary extensions (a dash means it is a subdirectory, anything else means
# it is a different checkout like "content-backend2" against "content-backend").
# The branch is what tells the session's transcript from an earlier one in the same
# checkout; mtime breaks a tie, and stands alone on a session that never branched.
CLAUDE_PICK = """
import json, glob, os, sys
best, fallback = ("", 0.0), ("", 0.0)
root = os.path.expanduser("~/.claude/projects")
for folder in sorted(glob.glob(os.path.join(root, sys.argv[1] + "*"))):
    # The encoding maps "/" to "-", so a directory for a path *inside* the repo
    # extends the root's encoding at a dash. Anything else -- "content-backend2"
    # against "content-backend" -- is a different checkout, not a subdirectory.
    tail = os.path.basename(folder)[len(sys.argv[1]):]
    if tail and not tail.startswith("-"):
        continue
    for path in glob.glob(os.path.join(folder, "*.jsonl")):
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
    # "[a]gentapi": grep's own argv contains the literal pattern too, so a plain
    # 'agentapi server' can match grep itself under `ps` -- and with -m1, does,
    # whenever that line sorts first, silently returning no --type. The bracket
    # is invisible to the process being searched for but breaks grep's argv out
    # of matching its own pattern.
    line = ssh_out(host, "ps -eo args= | grep -m1 '[a]gentapi server'", check=False)
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


def connected(clone, sha):
    """Is `sha` in the clone *with* its history?

    `cat-file -e` is not enough. A fetch that gets rejected for shallowness still
    leaves the tip object behind, so the commit exists while its parents do not:
    enough to check out, useless to review, because nothing can log or diff it.
    """
    return bool(sha) and subprocess.run(
        ["git", "-C", clone, "rev-list", "--quiet", sha],
        capture_output=True).returncode == 0


def fetched(clone, ref):
    """The commit `ref` resolves to, or "" if it is missing or unwalkable."""
    sha = run(["git", "-C", clone, "rev-parse", "--verify", "-q", ref],
              check=False).strip()
    return sha if connected(clone, sha) else ""


def shared_base(host, repo, clone):
    """(commit, commits above it) -- the newest commit both sides can use.

    Only ever a commit the *session* holds, so `git diff <base>` always works over
    there; and only ever one the clone can walk, so the mirror can be logged and
    diffed here. On a shallow session `rev-list` lists just its window, which is
    short, and the boundary commit is an ordinary upstream one the clone knows.

    A count of 0 means the mirror is commit-for-commit; anything higher is how
    many of the session's commits ride along inside the diff instead.
    """
    listing = ssh_out(host, f'git -C {shlex.quote(repo)} rev-list --max-count=50 HEAD',
                      check=False)
    for depth, sha in enumerate(listing.split()):
        if connected(clone, sha):
            return sha, depth
    return None, 0


def fetch_review_base(clone):
    """Refresh what reviewr diffs against, so its merge-base is not stale.

    reviewr's branch scope diffs from `merge-base(base, HEAD)` and resolves that base
    as: its `--base` flag, then the repository's pick, then the branch `origin/HEAD`
    names (reviewr's `git.rs::resolve_base`). No flag reaches a plugin pane, so the
    pick or `origin/HEAD` it is. Either way the winner is a remote-tracking ref, and a
    clone whose copy predates what the session branched from drags every commit merged
    upstream since into the diff -- files the branch never touched.

    One branch and never a whole `fetch origin`, because this runs on every finished
    agent turn; silent on failure, because offline must not stop a mirror.
    """
    pick = run(["git", "-C", clone, "cat-file", "blob", "refs/reviewr/base-pick"],
               check=False).strip()
    if pick:
        # reviewr resolves a pick that is not a branch name -- a SHA, `HEAD~2` -- inside
        # the clone, where no fetch can help. Its own guard, so the same picks qualify.
        if pick.startswith("-") or ".." in pick or "@{" in pick or \
                any(c in pick for c in "~^:?*[\\") or len(pick.split()) != 1:
            return None
        branch = pick
    else:
        branch = run(["git", "-C", clone, "rev-parse", "--abbrev-ref", "origin/HEAD"],
                     check=False).strip().split("/", 1)[-1] or "main"
    ok = subprocess.run(["git", "-C", clone, "fetch", "-q", "--no-tags", "origin",
                         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                        capture_output=True).returncode == 0
    return branch if ok else None


def mirror_session(name, conf, focus=False):
    """Reproduce the session's checkout locally and return its herdr workspace.

    Returns (workspace_id, checkout_path), or (None, None) when the session cannot
    be mirrored -- the caller then falls back to a plain workspace.

    The mirror carries the session's commits *and* its uncommitted work, which is
    the state a review tool needs and no PR view can show. It is derived, never
    authored in: a refresh resets it.

    Two ways to land the same state. Normally the session's commits fetch straight
    over ssh and the mirror is commit-for-commit. When the session's clone is
    shallow they cannot, so the mirror sits on the newest commit both sides share
    and the session's work above it arrives as one diff. Same working tree either
    way; only the commit boundary differs.
    """
    host = f"{name}{conf['host_suffix']}"
    repo, branch, slug, shallow = remote_repo(host)
    if not repo or not branch:
        note(f"could not read {host}'s checkout over ssh -- opening without a mirror")
        return None, None
    clone = clone_path(slug, conf["clone_root"])
    if not clone:
        note(f"no local clone for {slug or 'the session repo'} under "
             f"{conf['clone_root']} -- opening without a mirror "
             f"(point clone_root at your clones in {config_hint()})")
        return None, None
    fetch_review_base(clone)  # before the base: a fresher origin also widens `connected`

    # A named local branch, not a detached ref: reviewr's PR tab resolves the PR
    # from the current branch name, the same answer `gh pr view` gives. Forced,
    # because agents amend and rebase.
    scratch = f"refs/coder/{name}"
    base, notes, reason = "", [], ""
    if not shallow:
        # Not `run`: the fetch exits 0 even when it rejects the ref, so the ref is
        # what says whether the commits arrived.
        fetch = subprocess.run(["git", "-C", clone, "fetch", "-q", f"{host}:{repo}",
                                f"+HEAD:{scratch}"], capture_output=True, text=True)
        base = fetched(clone, scratch)
        reason = (fetch.stderr or fetch.stdout).strip().replace("\n", "; ")[:200]
    else:
        # Fetching a shallow session is worse than useless: git rejects the ref
        # ("shallow roots are not allowed to be updated") AND keeps the tip object,
        # after which the clone claims commits whose ancestry it lacks -- later
        # origin fetches then skip that ancestry too, and only
        # `git fetch --negotiation-tip=refs/remotes/origin/main origin main`
        # recovers it. Origin has the same commits with their history attached, and
        # an agent branch is usually pushed: that is what the PR is.
        subprocess.run(["git", "-C", clone, "fetch", "-q", "--no-tags", "origin",
                        f"+refs/heads/{branch}:{scratch}"], capture_output=True)
        notes.append("via origin, its clone being shallow")
    if not base:
        base, above = shared_base(host, repo, clone)
        if above:
            notes.append(f"{above} session commit(s) inside the diff")
    source = f" ({'; '.join(notes)})" if base and notes else ""
    if not base:
        note(f"could not mirror {name}: no commit of the session is in {clone} with "
             f"its history -- try `git -C {clone} fetch origin`"
             f"\n  {reason or 'nothing shared with the session'}")
        return None, None

    checkout = worktree_for(clone, branch)
    if checkout:
        if not is_mirror(checkout):
            # Nearly always the clone's own checkout: a session that has not
            # branched yet sits on main, and so does the clone. Resetting either
            # that or a worktree of your own would throw away work the plugin
            # never made, so mirror nothing rather than refuse the session.
            note(f"{branch} is checked out at {checkout}, which is not a mirror "
                 f"(no {MIRROR_MARK} marker) -- {name} gets no mirror while it is "
                 f"on that branch; the turn the agent branches it is offered one, "
                 f"and prefix+ctrl+m moves it there")
            return None, None
        run(["git", "-C", checkout, "reset", "-q", "--hard", base])
        run(["git", "-C", checkout, "clean", "-qfd"])
        claim_branch(clone, branch, base)
        workspace = workspace_for_path(checkout)
        if workspace is None:
            workspace = herdr("worktree", "open", "--cwd", clone, "--branch", branch,
                              *(("--focus",) if focus else ("--no-focus",))
                              )["result"]["workspace"]["workspace_id"]
    else:
        # Forcing the branch is the point -- agents amend and rebase, so the base
        # is regularly not a descendant of where the branch sits. Only refuse when
        # the tip is somewhere the plugin did not put it, which means work that is
        # not the plugin's to move. An unrecorded branch predates that bookkeeping:
        # force it as this always did, and say so, since `git branch -f` writes a
        # reflog either way.
        existing = run(["git", "-C", clone, "rev-parse", "--verify", "-q",
                        f"refs/heads/{branch}"], check=False).strip()
        claimed = run(["git", "-C", clone, "rev-parse", "--verify", "-q",
                       f"{MIRROR_REFS}/{branch}"], check=False).strip()
        if existing and existing not in (base, claimed) and claimed:
            sys.exit(f"local branch {branch} is at {existing[:12]}, not where this "
                     f"plugin left it ({claimed[:12]}) -- refusing to move it")
        if existing and existing != base and not claimed:
            note(f"moving unrecorded local branch {branch} from {existing[:12]} to "
                 f"{base[:12]} (recover with `git -C {clone} reflog {branch}`)")
        run(["git", "-C", clone, "branch", "-f", branch, base])
        claim_branch(clone, branch, base)
        created = herdr("worktree", "create", "--cwd", clone, "--branch", branch,
                        *(("--focus",) if focus else ("--no-focus",)))["result"]
        workspace = created["workspace"]["workspace_id"]
        checkout = created["workspace"]["worktree"]["checkout_path"]
        open(mirror_marker(checkout), "w").close()

    # herdr creates a branch at the clone's HEAD unless told otherwise, so never
    # trust the name alone -- check the commit.
    landed = run(["git", "-C", checkout, "rev-parse", "HEAD"]).strip()
    if landed != base:
        sys.exit(f"mirror landed on {landed[:12]}, expected {base[:12]} -- "
                 f"refusing to review the wrong commits")

    apply_session_changes(host, repo, checkout, base)
    note(f"mirrored {name} at {checkout} ({branch} @ {base[:12]}){source}")
    return workspace, checkout


def worktree_for(clone, branch):
    """Path of an existing worktree checked out on `branch`, if any."""
    listing = run(["git", "-C", clone, "worktree", "list", "--porcelain"])
    path = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return None


def checkout_branch(checkout):
    """The branch a checkout is on, or "" when there is no checkout."""
    if not checkout:
        return ""
    return run(["git", "-C", checkout, "rev-parse", "--abbrev-ref", "HEAD"],
               check=False).strip()


def workspace_for_path(checkout):
    """The herdr workspace whose worktree is this checkout, if it is open."""
    for w in herdr("workspace", "list").get("result", {}).get("workspaces", []):
        if (w.get("worktree") or {}).get("checkout_path") == checkout:
            return w["workspace_id"]
    return None


def apply_session_changes(host, repo, checkout, base):
    """Copy the session's work above `base` onto the mirror.

    Two pieces, because `git diff` covers tracked files only and a new file is
    usually the point of a change. `git add -N` on the session would show up in
    the agent's own status, so it is deliberately not used.

    `base` is the session's own HEAD in the normal case, which makes this the
    uncommitted work alone; on a shallow session it is further back and the diff
    also carries the agent's commits. `--binary` because a diff spanning commits
    reaches image and jar changes that a text patch cannot represent.
    """
    patch = ssh_out(host, f'git -C {shlex.quote(repo)} diff --binary {shlex.quote(base)}')
    if patch:
        proc = subprocess.run(["git", "-C", checkout, "apply", "-"],
                              input=patch + "\n", text=True, capture_output=True)
        if proc.returncode != 0:
            print(f"warning: could not apply the session's tracked changes: "
                  f"{proc.stderr.strip()[:200]}")
    listing = ssh_out(host, f'cd {shlex.quote(repo)} && '
                            'git ls-files -o --exclude-standard')
    untracked = [f for f in listing.splitlines() if f.strip()]
    if untracked:
        tar = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", host,
             f'cd {shlex.quote(repo)} && git ls-files -o --exclude-standard -z '
             '| tar -czf - --null -T -'],
            capture_output=True)
        if tar.returncode == 0 and tar.stdout:
            subprocess.run(["tar", "-xzf", "-", "-C", checkout],
                           input=tar.stdout, capture_output=True)
    bits = []
    if patch:
        bits.append("tracked changes")
    if untracked:
        bits.append(f"{len(untracked)} untracked file(s)")
    note(f"  applied vs {base[:12]}: " + (", ".join(bits) or "nothing"))


def pane_sessions(workspace=None):
    """herdr workspace id -> the session name agentty is running in it.

    herdr's own agent detection reads the session off the agentty pane and reports
    it as that pane's `agent` (`"agent": "asked-in-db1a"`), which is what still
    identifies a workspace after a herdr restart has dropped the display-only
    tokens. One call covers every workspace: each pane comes back with its own
    `workspace_id`.

    A name here is a candidate, not proof -- every other agent pane reports its
    program instead (`claude`) -- so anything acted on is checked against the
    sessions that actually exist.
    """
    args = ["pane", "list"] + (["--workspace", workspace] if workspace else [])
    found = {}
    for pane in herdr(*args).get("result", {}).get("panes", []):
        if pane.get("agent") and pane.get("workspace_id"):
            found.setdefault(pane["workspace_id"], pane["agent"])
    return found


def open_workspaces(sessions=None):
    """Coder session name -> herdr workspace id.

    Keyed off the metadata token this plugin stamps. Tokens are display-only and
    a herdr restart drops them, so a workspace carrying none falls back to the
    session name on its agentty pane.

    Given `sessions`, a workspace recovered that way is re-stamped on the spot:
    that is what refills the sidebar after a restart, and it is gated on the list
    because without it there is no telling a session's pane from any other agent's.
    """
    conf = settings()
    token = name_token(conf)
    known = {s["name"]: s for s in sessions} if sessions is not None else {}
    by_pane = None
    found = {}
    for w in herdr("workspace", "list").get("result", {}).get("workspaces", []):
        name = (w.get("tokens") or {}).get(token)
        if not name:
            if by_pane is None:
                by_pane = pane_sessions()
            name = by_pane.get(w["workspace_id"], "")
            if name in known:
                branch = checkout_branch((w.get("worktree") or {}).get("checkout_path"))
                report_tokens(w["workspace_id"],
                              session_tokens(known[name], branch, conf), conf)
        if name:
            found.setdefault(name, w["workspace_id"])
    return found


def workspace_info(workspace):
    """That workspace's entry in herdr's list, or {} once it is gone."""
    return next((w for w in herdr("workspace", "list").get("result", {}).get("workspaces", [])
                 if w["workspace_id"] == workspace), {})


def pane_workspace(pane):
    """The workspace a pane sits in."""
    return herdr("pane", "get", pane).get("result", {}).get("pane", {}).get("workspace_id", "")


def session_pane(workspace):
    """The pane running agentty in this workspace -- where the session is."""
    for pane in herdr("pane", "list", "--workspace", workspace)["result"]["panes"]:
        if pane_running(pane["pane_id"], "agentty"):
            return pane["pane_id"]
    return None


def plugin_workspace(workspace, name):
    """Is this the workspace this plugin opened for `name`?

    Read off that workspace, never looked up by name: while a session is being
    moved, two workspaces answer to it, and only one of them is the one being
    left. The agentty pane is the fallback open_workspaces() uses too, for after
    a herdr restart has dropped the display-only token. `name` is already known
    to be a session's, so matching it needs no further check.
    """
    if (workspace_info(workspace).get("tokens") or {}).get(name_token()) == name:
        return True
    return pane_sessions(workspace).get(workspace) == name


def last_pane(workspace, pane):
    """Would closing `pane` empty this workspace, and so close the workspace too?

    Our own pane does not count: the offer runs in a split beside the agent and
    exits the moment the answer is acted on.
    """
    ours = os.environ.get("HERDR_PANE_ID", "")
    return not [p for p in herdr("pane", "list", "--workspace", workspace)["result"]["panes"]
                if p["pane_id"] not in (pane, ours)]


def mirror_workspace(workspace):
    """Is this workspace open on a mirror of ours?

    The workspace being a worktree one is not enough: a session parked in a
    worktree workspace of yours would look mirrored and never be offered one.
    """
    checkout = (workspace_info(workspace).get("worktree") or {}).get("checkout_path", "")
    return bool(checkout) and is_mirror(checkout)


def age(stamp):
    if not stamp:
        return "-"
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return f"{int(secs)}s"


def describe(task):
    state = task.get("current_state") or {}
    return {
        "name": task.get("name", "?"),
        "display_name": (task.get("display_name") or "").strip(),
        "status": task.get("status", "?"),
        "state": state.get("state") or "-",
        "message": (state.get("message") or "").replace("\n", " ").strip(),
        "uri": state.get("uri") or "",
        "age": age(state.get("timestamp") or task.get("updated_at")),
        "template": task.get("template_name", "?"),
        "prompt": (task.get("initial_prompt") or "").strip(),
    }


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


def rows(sessions, opened, width):
    """One fzf line per session: plain name, TAB, then the display columns."""
    out = []
    for s in sessions:
        mark = "●" if s["name"] in opened else " "
        head = f"{mark} {BOLD}{s['name']:<22}{RESET}"
        colour = STATE_COLOR.get(s["state"], "")
        state = f"{colour}{s['state']:<8}{RESET}"
        meta = f"{s['age']:>4} {DIM}{s['template']}{RESET}"
        # 46 covers the escape-free width of the columns above
        room = max(10, width - 46 - len(s["name"]))
        msg = s["message"][:room] + ("…" if len(s["message"]) > room else "")
        out.append(f"{s['name']}\t{head} {state} {meta}  {msg}")
    return out


def preview(session):
    lines = [f"{BOLD}{session['name']}{RESET}   {session['template']}",
             f"task status : {session['status']}",
             f"agent state : {session['state']}  ({session['age']} ago)"]
    if session["uri"]:
        lines.append(f"link        : {session['uri']}")
    if session["message"]:
        lines += ["", f"{BOLD}Last report{RESET}", session["message"]]
    if session["prompt"]:
        lines += ["", f"{BOLD}Initial prompt{RESET}", session["prompt"][:1200]]
    return "\n".join(lines)


def cache_write(sessions):
    try:
        with open(CACHE, "w") as handle:
            json.dump(sessions, handle)
    except OSError as exc:
        # Only the fzf preview reads this; losing it must not cost you the picker.
        note(f"could not write the preview cache ({exc}) -- previews will be empty")


def cache_read():
    try:
        with open(CACHE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return []


def pane_running(pane, program):
    """Is `program` in this pane's foreground?

    Labels are not a safe way to tell an open pane apart -- other plugins rewrite
    them -- so ask the pane itself.
    """
    info = herdr("pane", "process-info", "--pane", pane).get("result", {})
    running = (info.get("process_info") or {}).get("foreground_processes") or []
    return any(program in (proc.get("cmdline") or "") for proc in running)


def open_session(name, sessions=None):
    """Focus this session's workspace, building it the first time."""
    existing = open_workspaces(sessions).get(name)
    if existing:
        herdr("workspace", "focus", existing)
        print(f"focused existing workspace {existing} for {name}")
        return

    sessions = sessions if sessions is not None else [describe(t) for t in running_sessions()]
    if name not in {s["name"] for s in sessions}:
        sys.exit(f"{name} is not a running Coder session "
                 f"(start it with: coder start {name})")

    conf = settings()
    session = next(s for s in sessions if s["name"] == name)

    workspace = checkout = None
    if conf.get("mirror", True):
        workspace, checkout = mirror_session(name, conf)
    attach_session(name, session, conf, workspace, checkout)


def attach_session(name, session, conf, workspace, checkout):
    """Give the session a workspace: tokens, agentty, focus.

    Shared by the first open and by promote(), which is this same setup run again
    once a mirror the first open could not build has become possible.
    """
    if workspace:
        # The worktree workspace is the session's workspace: herdr gives it the
        # branch line in the sidebar, and reviewr's own auto-open puts reviewr in it.
        top = herdr("pane", "list", "--workspace", workspace)["result"]["panes"][0]["pane_id"]
    else:
        created = herdr("workspace", "create",
                        "--cwd", os.path.expanduser("~"), "--no-focus")["result"]
        workspace = created["workspace"]["workspace_id"]
        top = created["root_pane"]["pane_id"]
    # Stamped after the mirror, not before: the checkout is where the session's
    # branch, and so its ticket, can be read. The workspace label is left as
    # herdr named it -- the identity goes in tokens the user places themselves.
    report_tokens(workspace, session_tokens(session, checkout_branch(checkout), conf), conf)

    host = f"{name}{conf['host_suffix']}"
    # Refresh the mirror whenever the agent finishes a turn: agentty already knows
    # the exact running -> stable moment, so nothing has to poll. A workspace with
    # no mirror gets the hook too -- there it is what notices the agent branching,
    # which is the moment a mirror becomes possible at all.
    hook = f"AGENTTY_ON_IDLE={shlex.quote(f'{SELF} --mirror {name}')} "
    # `herdr pane run` sends text plus Enter, so aiming it at a pane that already
    # holds agentty types the command line into the *agent's* composer and sends it
    # to the remote agent as a prompt.
    # ponytail: agentty only. Any other foreground process in that pane -- a build,
    # a REPL -- still gets typed into; widen to "anything but the shell" once that
    # is checked against a genuinely idle shell, which reports no foreground child.
    if pane_running(top, "agentty"):
        note(f"focused {workspace} for {name}: agentty already in {top}")
    else:
        herdr("pane", "run", top, f"{hook}{agentty_cmd()} {host}")
        note(f"opened {workspace} for {name}: agentty in {top}")
    # reviewr opens itself on worktree.created and, since 0.36.2, on worktree.opened
    # too, so a reused checkout gets a pane like a fresh mirror does. The
    # open_reviewr() workaround for reviewr#82 was removed 2026-09-01 when that
    # landed -- racing reviewr's own handler was what left two panes.
    herdr("workspace", "focus", workspace)
    return workspace


def session_named(name):
    """The session's description, for its tokens: the picker's cache, else the CLI."""
    for session in cache_read():
        if session["name"] == name:
            return session
    found = [s for s in (describe(t) for t in running_sessions(False)) if s["name"] == name]
    return found[0] if found else {"name": name}


def promote(name, pane):
    """Move a session out of a mirrorless workspace and into a mirrored one.

    What the first open could not do, because the session was still on the branch
    the clone itself has checked out. Closing `pane` -- where agentty runs now --
    is what moves the session, and herdr collapses whatever that empties: a tab of
    its own, or the whole workspace once that was its last tab. Collapsing is right
    for the workspace this plugin built for the session and wrong for one of yours
    the session is parked in, so the pane stays put when it is the last one there.
    """
    conf = settings()
    old = pane_workspace(pane)
    ours = plugin_workspace(old, name)
    workspace, checkout = mirror_session(name, conf)
    if not workspace:
        return None  # mirror_session has already said why
    attach_session(name, session_named(name), conf, workspace, checkout)
    if old and old != workspace:
        # The tokens are what open_workspaces() matches on, and what the sidebar
        # draws: left behind, the picker would go on focusing the workspace the
        # session has just left, and its row would still name the session.
        clear_tokens(old, conf)
    if ours or not last_pane(old, pane):
        herdr("pane", "close", pane)  # last: this can take the tab we run in with it
    else:
        note(f"left agentty in {pane}: it is the last pane of {old}, which this "
             f"plugin did not open, and closing it would close that workspace too")
    return workspace


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


def git_common_dir(checkout):
    """The clone's shared git dir, absolute.

    Where a worktree's refs and the per-clone ignore file live. `rev-parse`
    answers relative to the worktree it was asked in, so the join is what makes
    the answer usable from a caller that is not standing there.
    """
    common = run(["git", "-C", checkout, "rev-parse", "--git-common-dir"]).strip()
    return common if os.path.isabs(common) else os.path.join(checkout, common)


def exclude_locally(checkout, entry):
    """Keep `entry` out of `git status` without touching the repository's .gitignore.

    `.git/info/exclude` is the per-clone ignore file: it is not tracked, so a
    handover never shows up in a diff and never reaches a PR. Worktrees share the
    common dir, so one line covers every session of the same clone.
    """
    path = os.path.join(git_common_dir(checkout), "info", "exclude")
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
    run(["git", "--git-dir", git_common_dir(checkout), "update-ref", "-d",
         f"{MIRROR_REFS}/{branch}"], check=False)


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

    # mirror_session() builds a worktree and a workspace from scratch for any
    # session that does not have one yet -- right for opening a session, wrong
    # here: a takeover ends an *existing* mirror, so a session whose agentty
    # pane sits in a mirrorless workspace (the promote offer was declined, or
    # never reached because the agent had not branched) has nothing to end.
    # Checked first, off local herdr state only, before the ssh calls below --
    # and before mirror_session can build the stray workspace this replaces.
    existing = open_workspaces().get(name)
    if not existing or not mirror_workspace(existing):
        sys.exit(f"{name} has no mirror to take over -- prefix+ctrl+m moves it "
                 f"into one first")
    session = session_named(name)

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

    workspace, checkout, _ = mirror_session(name, conf, focus=True)
    if not checkout:
        return None  # mirror_session has already said why

    # Read before anything is written or demoted. This is a cheap list call and
    # everything below it survives a failure only as a half-finished takeover:
    # the marker is gone, the handover is on disk, and re-running cannot put
    # either back. refresh() guards the same failure the same way before promote().
    agent_pane = session_pane(workspace)
    if not agent_pane:
        sys.exit(f"no agentty pane in {workspace} -- nothing to take over")

    branch = checkout_branch(checkout) or branch

    render = render_codex if kind == "codex" else render_claude
    turns = render(history_text(host, path))
    if not turns:
        # Guarding turns, not history_text: an empty string is not the only way
        # to end up with nothing -- a renderer that no longer recognises the
        # history file's schema returns [] on real content too, and that failure
        # deserves the same stop. The mirror above was only refreshed, same as
        # any other turn, so nothing here is demoted and agentty is still
        # running: safe to run the takeover again.
        sys.exit(f"no conversation came back from {name}'s {kind} history at "
                 f"{host}:{path} -- nothing to hand over")
    body = transcript(turns, name=name, host=host, kind=kind, checkout=checkout,
                      branch=branch, repo=repo)
    with open(os.path.join(checkout, TAKEOVER_FILE), "w") as handle:
        handle.write(body)
    exclude_locally(checkout, TAKEOVER_FILE)
    demote_mirror(checkout, branch)

    # Split first, close agentty last. Closing first would leave the agent's slot
    # to whatever herdr collapses into it -- on a mirrored session that is reviewr,
    # and `herdr pane run` sends text plus Enter, so the launch line would be typed
    # into reviewr's TUI. Splitting from the agent's own pane also means the
    # workspace can never empty mid-move, which is what promote()'s last-pane guard
    # exists to prevent; here there is nothing to guard.
    # --cwd is not optional: agentty's pane sits wherever herdr opened it, and an
    # agent started anywhere but the worktree reads the handover's paths against
    # the wrong tree.
    local = herdr("pane", "split", agent_pane, "--direction", "right",
                  "--cwd", checkout)["result"]["pane"]["pane_id"]
    herdr("pane", "run", local, LAUNCH[chosen])
    herdr("pane", "close", agent_pane)

    report_tokens(workspace, session_tokens(session, branch, conf,
                                            icon=ICON_TAKEN), conf)
    herdr("workspace", "focus", workspace)
    note(f"{name} taken over locally: {len(turns)} turns in {checkout}/{TAKEOVER_FILE}, "
         f"{chosen} running in {local}; the mirror is gone and {host} is left running")
    return workspace


def asked_before(name, branch):
    """Has this branch already been offered for this session? Records as it asks.

    An offer the agent's next turn repeats is nagging, and prefix+ctrl+m is there
    for anyone who said no and then changed their mind.
    """
    path = os.path.join(STATE, f"asked-{name}")
    try:
        with open(path) as handle:
            if handle.read().strip() == branch:
                return True
    except OSError:
        pass
    try:
        with open(path, "w") as handle:
            handle.write(branch)
    except OSError:
        pass  # an unwritable state dir means asking twice, never failing
    return False


def turn_finished(name):
    """The idle hook: refresh the mirror, or offer one that has become possible.

    A workspace with no mirror is one opened while the session sat on the branch
    the clone has checked out -- usually main, before the agent branched. Nothing
    could be mirrored then, so here the hook watches the branch instead and offers
    once it changes: in a split beside the agent, unfocused, because this fires
    mid-work and must not swallow what is being typed at the session.
    """
    conf = settings()
    pane = os.environ.get("HERDR_PANE_ID", "")  # agentty's pane; the hook is its child
    workspace = pane_workspace(pane) if pane else ""
    if not workspace or mirror_workspace(workspace):
        return mirror_session(name, conf)

    _, branch, slug, _ = remote_repo(f"{name}{conf['host_suffix']}")
    clone = clone_path(slug, conf["clone_root"])
    if not branch or not clone:
        return None
    taken = worktree_for(clone, branch)
    if taken and not is_mirror(taken):
        return None  # still a checkout of yours; there is nothing to offer yet
    if asked_before(name, branch):
        return None
    return herdr("plugin", "pane", "open",
                 "--plugin", os.environ.get("HERDR_PLUGIN_ID", PLUGIN_ID),
                 "--entrypoint", "promote", "--placement", "split",
                 "--target-pane", pane, "--direction", "down", "--no-focus",
                 "--env", f"CODER_SESSION={name}", "--env", f"CODER_PANE={pane}",
                 "--env", f"CODER_BRANCH={branch}")


def promote_pane():
    """The offer itself, in a pane of its own: ask, then move the session.

    A plugin pane closes the moment its command exits, so a refusal needs no
    goodbye -- but anything that failed has to wait on a keypress to be read.
    """
    name = os.environ.get("CODER_SESSION", "")
    pane = os.environ.get("CODER_PANE", "")
    branch = os.environ.get("CODER_BRANCH", "a branch of its own")
    if not name or not pane:
        sys.exit("nothing to promote (CODER_SESSION/CODER_PANE unset)")
    print(f"{name} is on {branch} now, so it can be mirrored: a worktree workspace "
          f"with the review pane, and the agent moved into it.")
    try:
        answer = input("move it there? [y/N]  (prefix+ctrl+m does this later) ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        return
    if not promote(name, pane):
        input("press enter to close ")


def pick(sessions):
    if not shutil.which("fzf"):
        sys.exit("fzf is not installed; use --list and --open NAME")
    width = shutil.get_terminal_size((120, 40)).columns
    proc = subprocess.run(
        ["fzf", "--ansi", "--delimiter=\t", "--with-nth=2..",
         "--header=enter: open or focus    ctrl-o: web UI    "
         "ctrl-r: refresh    ● already open",
         "--preview", f"{SELF} --show {{1}}",
         "--preview-window=right,50%,wrap",
         "--bind", f"ctrl-r:reload({SELF} --list)",
         # execute, not execute-silent: the browser prints nothing on success,
         # and a failure needs the terminal to be read on.
         "--bind", f"ctrl-o:execute({SELF} --web {{1}})"],
        input="\n".join(rows(sessions, open_workspaces(sessions), width)),
        text=True, capture_output=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split("\t", 1)[0].strip()


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


def refresh(workspace=None):
    """Refresh the mirror behind a workspace -- the plugin action's job.

    Takes the workspace from the action's environment, so a keybinding needs no
    argument, and reads the session name from the token this plugin stamps.
    """
    workspace, name = focused_session(workspace)
    conf = settings()
    if mirror_workspace(workspace):
        return mirror_session(name, conf)
    # Taken over: the worktree is the user's now, the marker is gone, and there is
    # no agentty left to promote. Worth its own sentence, because the generic
    # "no agentty pane" below names the symptom and leaves the cause to be guessed
    # at -- and here the absence is the feature working, not something broken.
    # The icon token rather than the worktree: it is what takeover() stamps and
    # restamp() preserves, and a herdr restart that drops it also drops the name
    # token, so focused_session() has already refused by then.
    if (workspace_info(workspace).get("tokens") or {}).get(token_names(conf)["icon"]) \
            == ICON_TAKEN:
        sys.exit(f"{name} was taken over locally -- its worktree is yours now and "
                 f"nothing resets it, so there is no mirror left to refresh")
    # No mirror to refresh: this workspace was opened while the session sat on the
    # clone's own branch. Moving it into a mirror is the useful thing the key can
    # do instead, and the keypress is the consent the idle hook has to ask for.
    pane = session_pane(workspace)
    if not pane:
        sys.exit(f"no agentty pane in {workspace} -- nothing to refresh or promote")
    promote(name, pane)


def web(name=None, workspace=None):
    """Open a session's page in the Coder web UI: the picker's row, or the
    focused workspace's session when the picker did not name one.

    Naming it off the workspace falls back to the agentty pane the way
    plugin_workspace() does, which refresh() deliberately does not: the token is
    display-only and a herdr restart drops it, and opening a link cannot damage
    what a wrong guess lands on. `coder task list` rejects a name that is not a
    session's, which is the check the fallback itself cannot make.
    """
    if not name:
        workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID")
        if not workspace:
            sys.exit("no session named, and no workspace to read one off "
                     "(HERDR_WORKSPACE_ID unset)")
        name = ((workspace_info(workspace).get("tokens") or {}).get(name_token())
                or pane_sessions(workspace).get(workspace, ""))
        if not name:
            sys.exit(f"{workspace} is not a Coder session workspace -- nothing to open")
    # Before the task list, so a CLI that was never logged in is answered by the
    # sentence naming CODER_URL rather than by `coder task list` failing first.
    base = coder_url()
    # Stopped sessions count: their page is where you start them again.
    task = next((t for t in running_sessions(False) if t.get("name") == name), None)
    if not task:
        sys.exit(f"{name} is not in `coder task list` -- nothing to open")
    url = task_url(task, base)
    if not webbrowser.open(url):
        note(f"no browser to open it in -- the session is at {url}")


def restamp():
    """Re-publish this plugin's sidebar tokens on every workspace running a session.

    Idempotent, and the explicit form of what open_workspaces() does on its own
    when the picker runs: tokens are display-only, so a herdr restart drops them
    all and the sidebar rows go blank until something writes them again.

    Workspaces are found by the session name on their agentty pane, checked
    against the sessions that exist, so this also reaches one whose token was
    never stamped -- opened before the token existed, or by an older version.
    """
    conf = settings()
    known = {s["name"]: s for s in (describe(t) for t in running_sessions(False))}
    by_pane = pane_sessions()
    names = token_names(conf)
    token = names["name"]
    changes = 0
    for w in herdr("workspace", "list").get("result", {}).get("workspaces", []):
        workspace = w["workspace_id"]
        tokens = w.get("tokens") or {}
        name = tokens.get(token) or by_pane.get(workspace, "")
        session = known.get(name)
        if not session:
            continue
        branch = checkout_branch((w.get("worktree") or {}).get("checkout_path"))
        # session_tokens() defaults to ICON, the mirror icon -- right for every
        # workspace except one takeover() already stamped ICON_TAKEN on. That
        # icon is the only sidebar signal telling a taken-over worktree apart
        # from a live mirror, so read it the same way `name` above is read off
        # the existing tokens, rather than always rebuilding the default.
        icon = ICON_TAKEN if tokens.get(names["icon"]) == ICON_TAKEN else ICON
        wanted = session_tokens(session, branch, conf, icon=icon)
        # "" means clear, and a token already absent is already cleared.
        if all(tokens.get(t, "") == v for t, v in wanted.items() if t):
            continue
        report_tokens(workspace, wanted, conf)
        print(f"{workspace}: stamped " + ", ".join(f"{t}={v}" for t, v in wanted.items()
                                                   if t and v))
        changes += 1
    if not changes:
        print("every Coder workspace is already stamped")


def selftest():
    def sess(**kw):
        base = {"name": "example-task-4f21", "display_name": "", "message": "", "prompt": ""}
        base.update(kw)
        return base

    assert readable_name(sess(display_name="tackle PROJ-42")) == "PROJ-42"
    assert readable_name(sess(display_name="<@U0B9X> get cracking on TEAM2-7")) == "TEAM2-7"
    assert readable_name(sess(message="Rebased PROJ-1234 onto main")) == "PROJ-1234"
    # no ticket: Slack mentions and links go, including a truncated one
    assert readable_name(sess(display_name="<@U08B> I got the issue in the <https://cod")) \
        == "I got the issue in the"
    assert readable_name(sess(display_name="a" * 40)) == "a" * 28 + "…"
    assert readable_name(sess()) == "example-task-4f21"  # nothing to go on
    assert readable_name(sess(display_name="encoded as UTF-8 here")) == "encoded as UTF-8 here"

    assert repo_slug("git@github.com:Photoroom/content_backend.git") == "Photoroom/content_backend"
    assert repo_slug("https://github.com/Photoroom/content_backend.git") == "Photoroom/content_backend"
    assert repo_slug("https://github.com/Photoroom/content_backend") == "Photoroom/content_backend"
    assert repo_slug("ssh://git@github.com/Photoroom/content_backend.git") == "Photoroom/content_backend"
    assert repo_slug("") is None
    assert clone_path(None, "~/projects/github") is None
    assert clone_path("Nope/nope", "~/projects/github") is None

    assert branch_ticket("automations/con2-106-support-multiple-placeholders") == "CON2-106"
    assert branch_ticket("proj-4031-disabled-personal-space-credit-tests") == "PROJ-4031"
    assert branch_ticket("someone/proj-4029-preserve-magic-codes") == "PROJ-4029"
    assert branch_ticket("standup-multirepo-commit-gathering") is None
    assert branch_ticket("dependabot/gradle/com.example.thing-thing-10.15.1") is None
    assert branch_ticket("main") is None
    assert branch_ticket("") is None
    assert branch_ticket(None) is None
    # the branch's ticket beats whatever the task text is discussing now
    assert readable_name(sess(display_name="tackle PROJ-42"),
                         branch="automations/con2-106-x") == "CON2-106"

    assert task_url({"owner_name": "someone", "id": "62a38be6-ebc1"},
                    "https://coder.example.com/") \
        == "https://coder.example.com/tasks/someone/62a38be6-ebc1"

    default = token_names(DEFAULTS)
    assert session_tokens(sess(display_name="tackle PROJ-42")) == {
        default["icon"]: "C■",
        default["ticket"]: "PROJ-42",
        default["name"]: "example-task-4f21",
    }
    assert session_tokens(sess(display_name="tackle PROJ-42"),
                          branch="feat/team2-7-thing")[default["ticket"]] == "TEAM2-7"
    # Nothing better to say than the name: the ticket token is cleared, not
    # left showing the session name a second time.
    assert session_tokens(sess())[default["ticket"]] == ""
    # One prefix names every token, so an override moves all three together.
    assert set(default) == {"icon", "ticket", "name"} and default["name"] == "coder_name"
    renamed = merge_settings({"token_prefix": "cs_"})
    assert set(session_tokens(sess(), conf=renamed)) == {"cs_icon", "cs_ticket", "cs_name"}
    assert session_tokens(sess(), icon=ICON_TAKEN)[default["icon"]] == ICON_TAKEN

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
    # takeover()'s guard against an empty handover trusts this: an unreadable
    # or transiently unreachable history file renders to [], same as no history
    # at all, so checking `turns` catches both.
    assert render_codex("") == []
    assert render_claude("") == []

    body = transcript([("user", "go"), ("tool", "Bash: ls"), ("assistant", "done")],
                      name="task-1a2b", host="task-1a2b.coder", kind="codex",
                      checkout="/local/wt", branch="feat/x", repo="/home/coder/r")
    assert "ran on a remote machine" in body
    assert "ssh task-1a2b.coder" in body
    assert "/local/wt" in body
    assert "- `Bash: ls`" in body
    assert body.index("## User") < body.index("## Assistant")

    assert claude_dir("/home/coder/content_backend/backend") == \
        "-home-coder-content-backend-backend"
    assert claude_dir("/Users/me/.config/x") == "-Users-me--config-x"

    assert local_agent({"takeover_agent": "match"}, "codex") == "codex"
    assert local_agent({"takeover_agent": "match"}, "claude") == "claude"
    assert local_agent({"takeover_agent": "claude"}, "codex") == "claude"
    assert local_agent({}, "codex") == "codex"  # absent key behaves as "match"
    # The product's name, not the binary's, is what people write in a config file.
    assert local_agent({"takeover_agent": "Claude-Code"}, "codex") == "claude"
    assert local_agent({"takeover_agent": "codex-cli"}, "claude") == "codex"
    assert set(LAUNCH) == {"claude", "codex"}
    assert TAKEOVER_FILE in LAUNCH["claude"] and TAKEOVER_FILE in LAUNCH["codex"]

    print("selftest ok")


def main():
    if sys.version_info < (3, 9):
        # Said here rather than left to bite later: the first failure would
        # otherwise be an AttributeError from the middle of a mirror.
        sys.exit(f"this needs Python 3.9 or newer; {sys.executable} is "
                 f"{'.'.join(str(n) for n in sys.version_info[:3])}")

    parser = argparse.ArgumentParser(add_help=True, description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="check the pure helpers")
    parser.add_argument("--refresh", action="store_true",
                        help="refresh the focused workspace's mirror (the plugin action)")
    parser.add_argument("--web", nargs="?", const="", metavar="NAME",
                        help="open a session in the Coder web UI; without NAME, "
                             "the focused workspace's session")
    parser.add_argument("--mirror", metavar="NAME",
                        help="what a finished agent turn triggers: refresh the "
                             "mirror, or offer one now that it is possible")
    parser.add_argument("--promote", action="store_true",
                        help="the pane that asks before moving a session into a mirror")
    parser.add_argument("--takeover", nargs="?", const="", metavar="NAME",
                        help="hand a session's conversation to a local agent and "
                             "drop its mirror; without NAME, the focused workspace's")
    parser.add_argument("--restamp", action="store_true",
                        help="re-publish the sidebar tokens on open Coder workspaces")
    parser.add_argument("--pane", action="store_true",
                        help="open the picker as a plugin pane (what the action does)")
    parser.add_argument("--list", action="store_true", help="print rows, no picker")
    parser.add_argument("--open", metavar="NAME", help="open or focus a session")
    parser.add_argument("--show", metavar="NAME", help="preview text for a session")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.refresh:
        return refresh()

    if args.web is not None:  # --web with no NAME is "", which is not None
        return web(args.web or None)

    if args.mirror:
        return turn_finished(args.mirror)

    if args.promote:
        return promote_pane()

    if args.takeover is not None:
        return takeover(args.takeover or focused_session()[1])

    if args.restamp:
        return restamp()

    if args.pane:
        # An action's command runs with no terminal, so it cannot host fzf --
        # it has to ask herdr for a pane, which does have one.
        herdr("plugin", "pane", "open",
              "--plugin", os.environ.get("HERDR_PLUGIN_ID", "ubuntudroid.coder-sessions"),
              "--entrypoint", "list")
        return

    if args.show:  # fzf preview: read the cache, never the slow CLI
        for session in cache_read():
            if session["name"] == args.show:
                print(preview(session))
                return
        print(f"{args.show}: not in cache; press ctrl-r to refresh")
        return

    if args.open:
        open_session(args.open)
        return

    sessions = [describe(t) for t in running_sessions()]
    cache_write(sessions)
    if not sessions:
        print("no running Coder sessions")
        return

    if args.list:
        width = shutil.get_terminal_size((120, 40)).columns
        print("\n".join(rows(sessions, open_workspaces(sessions), width)))
        return

    chosen = pick(sessions)
    if chosen:
        open_session(chosen, sessions)


def log_line(text):
    """Append one line to the plugin log.

    A plugin pane closes the moment its command exits -- on success as fast as on
    failure -- so anything worth reading afterwards has to land in a file too.
    """
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with open(LOG, "a") as handle:
            handle.write(f"{stamp} {text}\n")
    except OSError:
        pass  # a failure to log must not replace the failure being logged


def note(text):
    """Say something the popup will close over: print it and keep a copy."""
    print(text)
    log_line(text)


def hold(text):
    """Report a failure so it can actually be read: log it, then wait for a key
    when there is a terminal to wait on."""
    log_line(text)
    print(f"\n{text}\n\n({LOG})", file=sys.stderr)
    if sys.stdin.isatty():
        try:
            input("press enter to close ")
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:  # sys.exit("message") -- our own error path
        if exc.code not in (None, 0):
            hold(str(exc.code))
            # Not a bare `raise`: re-raising a string code makes the interpreter
            # print the same sentence a second time, under the one just held.
            raise SystemExit(1)
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        hold(traceback.format_exc())
        raise SystemExit(1)
