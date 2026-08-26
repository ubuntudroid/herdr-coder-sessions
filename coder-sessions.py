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
    coder-sessions.py --mirror NAME    refresh one session's local mirror worktree
    coder-sessions.py --refresh        same, for the focused workspace (plugin action)
    coder-sessions.py --relabel        re-apply the label scheme to open workspaces
    coder-sessions.py --selftest       check the naming helpers

`coder task list` costs ~700ms, too slow to run per keystroke, so --list writes
a cache that --show reads back.

Settings, all optional, in $HERDR_PLUGIN_CONFIG_DIR/config.json:

    {"host_suffix": ".coder", "clone_root": "~/projects/github", "mirror": true}
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
}

DIM, BOLD, RESET = "\x1b[2m", "\x1b[1m", "\x1b[0m"
STATE_COLOR = {"idle": "\x1b[32m", "working": "\x1b[33m",
               "complete": "\x1b[32m", "failure": "\x1b[31m", "error": "\x1b[31m"}


def settings():
    path = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    merged = dict(DEFAULTS)
    if path:
        try:
            with open(os.path.join(path, "config.json")) as handle:
                merged.update(json.load(handle))
        except (OSError, ValueError):
            pass  # absent or malformed config just means defaults
    return merged


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
    reachable; pass False when naming workspaces, where a stopped session's
    ticket is still the best label."""
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


METADATA_SOURCE = "coder-sessions"
TOKEN = "coder"
REVIEWR = "persiyanov.reviewr"  # the review pane this plugin opens beside the agent

# Ticket ids make the best workspace label. Prefixes here carry digits (CON2,
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
    """A human label for the workspace: the ticket the branch names, else one the
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
SIDEBAR_MAX = 36  # `sidebar_max_width` default; the sidebar auto-scales up to it


def workspace_label(session, branch=""):
    """`C■ <ticket or summary> · <session name>`, kept inside the sidebar width.

    The sidebar shows one line per workspace: the branch line on worktree
    workspaces is herdr's own, `--cwd` inside a repo does not earn one, and
    metadata tokens never render -- so the identifier has to share the label.
    """
    name = session["name"]
    # icon + its space + " · " + a column for "…" on a truncated summary
    head = readable_name(session, limit=max(8, SIDEBAR_MAX - len(ICON) - len(name) - 5),
                         branch=branch)
    if head == name:
        return f"{ICON} {name}"  # nothing better to say than the name itself
    return f"{ICON} {head} · {name}"


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

    Returns (workspace_id, checkout_path, made_the_worktree), or (None, None, False)
    when the session's repo has no local clone -- the caller then falls back to a
    plain workspace. The last flag says which herdr event the open fired, which is
    what decides whether reviewr auto-opened (see open_reviewr).

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
        return None, None, False
    clone = clone_path(slug, conf["clone_root"])
    if not clone:
        note(f"no local clone for {slug or 'the session repo'} under "
             f"{conf['clone_root']} -- opening without a mirror "
             f"(point clone_root at your clones in {config_hint()})")
        return None, None, False
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
        return None, None, False

    checkout = worktree_for(clone, branch)
    made = not checkout
    if checkout:
        if not is_mirror(checkout):
            sys.exit(f"{checkout} is a worktree for {branch} but not a mirror "
                     f"(no {MIRROR_MARK} marker); refusing to reset work that is not mine")
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
    return workspace, checkout, made


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


def open_workspaces():
    """Coder session name -> herdr workspace id.

    Keyed off the metadata token this plugin stamps, so the label stays free to
    be human-readable; the label is a fallback for workspaces made before that.
    """
    result = herdr("workspace", "list").get("result", {})
    found = {}
    for w in result.get("workspaces", []):
        label = w.get("label", "")
        # The token is the reliable handle, but `report-metadata` is display-only
        # and does not survive a herdr restart, so fall back to the label, whose
        # last word is the session name -- until another plugin rewrites the label
        # wholesale (herdr-git-status does), which is why the pane, not the label,
        # is what agentty_running() asks before running anything.
        name = (w.get("tokens") or {}).get(TOKEN) or (label.split()[-1] if label else "")
        if name:
            found.setdefault(name, w["workspace_id"])
    return found


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


def reviewr_config():
    """reviewr's own resolved settings, or None when it is not installed.

    Its binary owns the TOML parsing and the defaults, so asking it beats reading
    the config file here: placement, direction and auto_open all come back
    normalized, and one contract serves reviewr's entry points and this one. The
    paths are the stable symlinks reviewr keeps pointed at whatever hashed install
    directory it lives under today.
    """
    for path in (os.path.expanduser(f"~/.local/state/herdr/plugins/{REVIEWR}/bin/herdr-reviewr"),
                 os.path.expanduser("~/.local/bin/herdr-reviewr"),
                 shutil.which("herdr-reviewr")):
        if path and os.access(path, os.X_OK):
            try:
                return json.loads(run([path, "--resolve-plugin-config"], check=False))
            except ValueError:
                return None  # a validation error, which reviewr's own actions report
    return None


def open_reviewr(workspace, pane, checkout):
    """Put reviewr beside the agent, because the event that would have will not fire.

    reviewr auto-opens on `worktree.created`, which only the *first* mirror of a
    branch fires: every open after that reuses the checkout and goes through
    `worktree open`, whose `worktree.opened` reviewr does not subscribe to
    (https://github.com/persiyanov/herdr-reviewr/issues/82). So the caller asks for
    a pane on exactly the openings the event misses -- racing it on the others
    would leave two -- and reviewr's own settings decide the rest.
    """
    conf = reviewr_config()
    if not conf or not conf.get("auto_open", True):
        return  # not installed, or the user turned auto-opening off
    panes = herdr("pane", "list", "--workspace", workspace)["result"]["panes"]
    if any(pane_running(p["pane_id"], "herdr-reviewr") for p in panes):
        return  # a workspace herdr already had may carry one
    placement = conf.get("toggle_placement", "split")
    where = ["--target-pane", pane] if placement in ("split", "zoomed") else []
    if placement == "split":
        where += ["--direction", conf.get("toggle_direction", "right")]
    elif placement == "tab":
        where = ["--workspace", workspace]
    # Not `herdr`: a review pane is a bonus, so a failure here reports and leaves
    # the session open rather than aborting an open that has already landed.
    out = run([HERDR, "plugin", "pane", "open", "--plugin", REVIEWR,
               "--entrypoint", "pane", "--placement", placement, *where,
               "--cwd", checkout, "--no-focus"], check=False)
    try:
        new = ((json.loads(out).get("result") or {}).get("plugin_pane") or {}).get("pane") or {}
    except ValueError:
        new = {}
    if placement == "tab" and new.get("tab_id"):
        # herdr labels a fresh tab with a bare index; reviewr names its own.
        herdr("tab", "rename", new["tab_id"], "reviewr")
    note(f"  reviewr: {new.get('pane_id') or 'failed to open'} ({placement})")


def open_session(name, sessions=None):
    """Focus this session's workspace, building it the first time."""
    existing = open_workspaces().get(name)
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
        workspace, checkout, made = mirror_session(name, conf)
    # Labelled after the mirror, not before: the checkout is where the session's
    # branch, and so its ticket, can be read.
    label = workspace_label(session, checkout_branch(checkout))
    if workspace:
        # The worktree workspace is the session's workspace: herdr gives it the
        # branch line in the sidebar, and open_reviewr() puts reviewr in it.
        herdr("workspace", "rename", workspace, label)
        top = herdr("pane", "list", "--workspace", workspace)["result"]["panes"][0]["pane_id"]
    else:
        created = herdr("workspace", "create", "--label", label,
                        "--cwd", os.path.expanduser("~"), "--no-focus")["result"]
        workspace = created["workspace"]["workspace_id"]
        top = created["root_pane"]["pane_id"]
    # The Coder session name goes in a metadata token, not the label: it is the
    # identifier, shown under the human-readable name, and it is what
    # open_workspaces() matches on.
    herdr("workspace", "report-metadata", workspace,
          "--source", METADATA_SOURCE, "--token", f"{TOKEN}={name}")

    host = f"{name}{conf['host_suffix']}"
    # Refresh the mirror whenever the agent finishes a turn: agentty already knows
    # the exact running -> stable moment, so nothing has to poll.
    hook = ""
    if checkout:
        hook = (f"AGENTTY_ON_IDLE={shlex.quote(f'{SELF} --mirror {name}')} ")
    # `herdr pane run` sends text plus Enter, so aiming it at a pane that already
    # holds agentty types the command line into the *agent's* composer and sends it
    # to the remote agent as a prompt.
    # ponytail: agentty only. Any other foreground process in that pane -- a build,
    # a REPL -- still gets typed into; widen to "anything but the shell" once that
    # is checked against a genuinely idle shell, which reports no foreground child.
    if pane_running(top, "agentty"):
        note(f"focused {workspace} \"{label}\" for {name}: agentty already in {top}")
    else:
        herdr("pane", "run", top, f"{hook}{agentty_cmd()} {host}")
        note(f"opened {workspace} \"{label}\" for {name}: agentty in {top}")
    if checkout and not made:
        open_reviewr(workspace, top, checkout)  # a fresh worktree gets reviewr's own event
    herdr("workspace", "focus", workspace)


def pick(sessions):
    if not shutil.which("fzf"):
        sys.exit("fzf is not installed; use --list and --open NAME")
    width = shutil.get_terminal_size((120, 40)).columns
    proc = subprocess.run(
        ["fzf", "--ansi", "--delimiter=\t", "--with-nth=2..",
         "--header=enter: open or focus    ctrl-r: refresh    ● already open",
         "--preview", f"{SELF} --show {{1}}",
         "--preview-window=right,50%,wrap",
         "--bind", f"ctrl-r:reload({SELF} --list)"],
        input="\n".join(rows(sessions, open_workspaces(), width)),
        text=True, capture_output=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split("\t", 1)[0].strip()


def refresh(workspace=None):
    """Refresh the mirror behind a workspace -- the plugin action's job.

    Takes the workspace from the action's environment, so a keybinding needs no
    argument, and reads the session name from the token this plugin stamps.
    """
    workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace:
        sys.exit("no workspace to refresh (HERDR_WORKSPACE_ID unset)")
    match = [w for w in herdr("workspace", "list").get("result", {}).get("workspaces", [])
             if w["workspace_id"] == workspace]
    name = (match[0].get("tokens") or {}).get(TOKEN) if match else None
    if not name:
        sys.exit(f"{workspace} is not a Coder session workspace "
                 f"(no {TOKEN} token) -- nothing to refresh")
    mirror_session(name, settings())


def relabel():
    """Bring every Coder workspace up to the current label scheme.

    Idempotent, and it also stamps the metadata token on workspaces opened
    before that existed -- those are recognised by their label still being the
    bare session name.
    """
    known = {s["name"]: s for s in (describe(t) for t in running_sessions(False))}
    changes = 0
    for w in herdr("workspace", "list").get("result", {}).get("workspaces", []):
        tokens = w.get("tokens") or {}
        name = tokens.get(TOKEN) or w.get("label", "")
        session = known.get(name)
        if not session:
            continue
        wanted = workspace_label(
            session, checkout_branch((w.get("worktree") or {}).get("checkout_path")))
        if w.get("label") != wanted:
            herdr("workspace", "rename", w["workspace_id"], wanted)
            print(f"{w['workspace_id']}: {w.get('label')!r} -> {wanted!r}")
            changes += 1
        if tokens.get(TOKEN) != name:
            herdr("workspace", "report-metadata", w["workspace_id"],
                  "--source", METADATA_SOURCE, "--token", f"{TOKEN}={name}")
            print(f"{w['workspace_id']}: tagged {TOKEN}={name}")
            changes += 1
    if not changes:
        print("every Coder workspace label is already current")


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

    assert workspace_label(sess(display_name="tackle PROJ-42")) \
        == "C■ PROJ-42 · example-task-4f21"
    assert workspace_label(sess(display_name="tackle PROJ-42"),
                           branch="feat/team2-7-thing") == "C■ TEAM2-7 · example-task-4f21"
    assert workspace_label(sess()) == "C■ example-task-4f21"  # no better name to use
    long = workspace_label(sess(display_name="a really long summary with no ticket in it"))
    assert long == "C■ a really lon… · example-task-4f21", long
    assert len(long) <= SIDEBAR_MAX, (long, len(long))
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
    parser.add_argument("--mirror", metavar="NAME",
                        help="refresh one session's local mirror worktree, no panes")
    parser.add_argument("--relabel", action="store_true",
                        help="re-apply the current label scheme to open Coder workspaces")
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

    if args.mirror:
        return mirror_session(args.mirror, settings(), focus=False)

    if args.relabel:
        return relabel()

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
        print("\n".join(rows(sessions, open_workspaces(), width)))
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
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        hold(traceback.format_exc())
        raise SystemExit(1)
