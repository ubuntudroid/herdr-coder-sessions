#!/usr/bin/env python3
"""Browse running Coder agent sessions and open each as its own herdr workspace.

Coder task workspaces have no tmux: the agent runs under agentapi on port 3284.
Picking a session opens a workspace holding `agentty` on top (two thirds of the
height) and a plain ssh shell below it. Picking a session that is already open
focuses its workspace instead of building a second one.

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

    {"ratio": 0.667, "host_suffix": ".coder",
     "clone_root": "~/projects/github", "mirror": true}
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"

# Runtime state belongs in the state dir herdr provides, never in the plugin
# root: a GitHub install replaces that checkout wholesale.
CACHE = os.path.join(os.environ.get("HERDR_PLUGIN_STATE_DIR") or tempfile.gettempdir(),
                     f"coder-sessions-{os.getuid()}.json")
SELF = os.path.abspath(__file__)

DEFAULTS = {
    "ratio": 0.667,        # share of the height for the top pane: agentty
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


def run(argv, check=True):
    """Run a command, returning stdout. stderr is dropped: the coder CLI prints
    a version-mismatch warning there on every call."""
    proc = subprocess.run(argv, capture_output=True, text=True)
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
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentty")
    if os.access(local, os.X_OK):
        return shlex.quote(local)
    if shutil.which("agentty"):
        return "agentty"
    sys.exit("agentty not found: expected it beside this script or on PATH")


def running_sessions(running_only=True):
    """Coder tasks. By default only those whose workspace is up, so agentapi is
    reachable; pass False when naming workspaces, where a stopped session's
    ticket is still the best label."""
    out = run(["coder", "task", "list", "-o", "json"])
    start = out.find("[")
    tasks = json.loads(out[start:]) if start >= 0 else []
    if not running_only:
        return tasks
    return [t for t in tasks if t.get("workspace_status") == "running"]


METADATA_SOURCE = "coder-sessions"
TOKEN = "coder"

# Ticket ids make the best workspace label. Prefixes here carry digits (CON2,
# PGROWTH), so allow them after the first letter; skip encodings and standards
# that share the shape.
TICKET_RE = re.compile(r"\b(?!UTF-|ISO-|RFC-|SHA-)[A-Z][A-Z0-9]{1,9}-\d{1,5}\b")
SLACK_MARKUP = re.compile(r"<[^>]*>|<[^>]*$")  # <@U123>, <https://x|text>, truncated


def readable_name(session, limit=28):
    """A human label for the workspace: the ticket id if the task names one,
    else the task's own display name with Slack markup stripped."""
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


def workspace_label(session):
    """`C■ <ticket or summary> · <session name>`, kept inside the sidebar width.

    The sidebar shows one line per workspace: the branch line on worktree
    workspaces is herdr's own, `--cwd` inside a repo does not earn one, and
    metadata tokens never render -- so the identifier has to share the label.
    """
    name = session["name"]
    # icon + its space + " · " + a column for "…" on a truncated summary
    head = readable_name(session, limit=max(8, SIDEBAR_MAX - len(ICON) - len(name) - 5))
    if head == name:
        return f"{ICON} {name}"  # nothing better to say than the name itself
    return f"{ICON} {head} · {name}"


MIRROR_MARK = "coder-mirror"  # marks a worktree as derived, so refreshing may reset it


def mirror_marker(checkout):
    """Marker path inside the worktree's git dir, so it never shows in status."""
    gitdir = run(["git", "-C", checkout, "rev-parse", "--absolute-git-dir"]).strip()
    return os.path.join(gitdir, MIRROR_MARK)


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


def ssh_out(host, command):
    """Run one command on the session host. Trailing newline stripped: a stray
    CR here silently corrupts a git refspec."""
    return run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes",
                host, command]).strip()


def remote_repo(host):
    """The session's checkout: (repo path, branch, origin slug).

    The login directory is inside the repo (the Coder template puts it there), so
    ask git from there rather than guessing a name.
    """
    probe = ('cd "$HOME" 2>/dev/null; '
             'root=$(git rev-parse --show-toplevel 2>/dev/null) || '
             'root=$(for d in "$HOME"/*/.git; do dirname "$d"; break; done); '
             '[ -n "$root" ] || exit 1; '
             'printf "%s\t%s\t%s\n" "$root" '
             '"$(git -C "$root" rev-parse --abbrev-ref HEAD)" '
             '"$(git -C "$root" remote get-url origin 2>/dev/null)"')
    line = ssh_out(host, probe)
    if not line or "\t" not in line:
        return None, None, None
    root, branch, origin = (line.split("\t") + ["", ""])[:3]
    return root.strip(), branch.strip(), repo_slug(origin)


def mirror_session(name, conf, focus=False):
    """Reproduce the session's checkout locally and return its herdr workspace.

    Returns (workspace_id, checkout_path) or (None, None) when the session's repo
    has no local clone -- the caller then falls back to a plain workspace.

    The mirror carries the session's commits *and* its uncommitted work, which is
    the state a review tool needs and no PR view can show. It is derived, never
    authored in: a refresh resets it.
    """
    host = f"{name}{conf['host_suffix']}"
    repo, branch, slug = remote_repo(host)
    if not repo or not branch:
        return None, None
    clone = clone_path(slug, conf["clone_root"])
    if not clone:
        print(f"no local clone for {slug or 'the session repo'} under "
              f"{conf['clone_root']} -- skipping the mirror")
        return None, None

    # A named local branch, not a detached ref: reviewr's PR tab resolves the PR
    # from the current branch name, the same answer `gh pr view` gives. Forced,
    # because agents amend and rebase.
    scratch = f"refs/coder/{name}"
    run(["git", "-C", clone, "fetch", "-q", f"{host}:{repo}", f"+HEAD:{scratch}"])
    head = run(["git", "-C", clone, "rev-parse", scratch]).strip()

    checkout = worktree_for(clone, branch)
    if checkout:
        if not is_mirror(checkout):
            sys.exit(f"{checkout} is a worktree for {branch} but not a mirror "
                     f"(no {MIRROR_MARK} marker); refusing to reset work that is not mine")
        run(["git", "-C", checkout, "reset", "-q", "--hard", head])
        run(["git", "-C", checkout, "clean", "-qfd"])
        workspace = workspace_for_path(checkout)
        if workspace is None:
            workspace = herdr("worktree", "open", "--cwd", clone, "--branch", branch,
                              *(("--focus",) if focus else ("--no-focus",))
                              )["result"]["workspace"]["workspace_id"]
    else:
        run(["git", "-C", clone, "branch", "-f", branch, head])
        created = herdr("worktree", "create", "--cwd", clone, "--branch", branch,
                        *(("--focus",) if focus else ("--no-focus",)))["result"]
        workspace = created["workspace"]["workspace_id"]
        checkout = created["workspace"]["worktree"]["checkout_path"]
        open(mirror_marker(checkout), "w").close()

    # herdr creates a branch at the clone's HEAD unless told otherwise, so never
    # trust the name alone -- check the commit.
    landed = run(["git", "-C", checkout, "rev-parse", "HEAD"]).strip()
    if landed != head:
        sys.exit(f"mirror landed on {landed[:12]}, expected {head[:12]} -- "
                 f"refusing to review the wrong commits")

    apply_session_changes(host, repo, checkout)
    print(f"mirrored {name} at {checkout} ({branch} @ {head[:12]})")
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


def workspace_for_path(checkout):
    """The herdr workspace whose worktree is this checkout, if it is open."""
    for w in herdr("workspace", "list").get("result", {}).get("workspaces", []):
        if (w.get("worktree") or {}).get("checkout_path") == checkout:
            return w["workspace_id"]
    return None


def apply_session_changes(host, repo, checkout):
    """Copy the session's uncommitted work onto the mirror.

    Two pieces, because `git diff HEAD` covers tracked files only and a new file
    is usually the point of a change. `git add -N` on the session would show up
    in the agent's own status, so it is deliberately not used.
    """
    patch = ssh_out(host, f'git -C {shlex.quote(repo)} diff HEAD')
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
    print("  uncommitted: " + (", ".join(bits) or "none"))


def open_workspaces():
    """Coder session name -> herdr workspace id.

    Keyed off the metadata token this plugin stamps, so the label stays free to
    be human-readable; the label is a fallback for workspaces made before that.
    """
    result = herdr("workspace", "list").get("result", {})
    found = {}
    for w in result.get("workspaces", []):
        name = (w.get("tokens") or {}).get(TOKEN) or w.get("label", "")
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
    with open(CACHE, "w") as handle:
        json.dump(sessions, handle)


def cache_read():
    try:
        with open(CACHE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return []


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
    label = workspace_label(session)

    workspace = checkout = None
    if conf.get("mirror", True):
        workspace, checkout = mirror_session(name, conf)
    if workspace:
        # The worktree workspace is the session's workspace: herdr gives it the
        # branch line in the sidebar, and reviewr auto-opens on worktree.created.
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

    # Split while both panes are still bare shells. Starting agentty first would
    # only make it repaint at a new size, and a `pane run` aimed at a pane that
    # already hosts agentty types into the agent's composer instead.
    bottom = herdr("pane", "split", top, "--direction", "down",
                   "--ratio", str(conf["ratio"]), "--no-focus")["result"]["pane"]["pane_id"]
    host = f"{name}{conf['host_suffix']}"
    # Refresh the mirror whenever the agent finishes a turn: agentty already knows
    # the exact running -> stable moment, so nothing has to poll.
    hook = ""
    if checkout:
        hook = (f"AGENTTY_ON_IDLE={shlex.quote(f'{SELF} --mirror {name}')} ")
    herdr("pane", "run", top, f"{hook}{agentty_cmd()} {host}")
    herdr("pane", "run", bottom, f"ssh {host}")
    herdr("workspace", "focus", workspace)
    print(f"opened {workspace} \"{label}\" for {name}: agentty in {top}, ssh in {bottom}")


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
        wanted = workspace_label(session)
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

    assert workspace_label(sess(display_name="tackle PROJ-42")) \
        == "C■ PROJ-42 · example-task-4f21"
    assert workspace_label(sess()) == "C■ example-task-4f21"  # no better name to use
    long = workspace_label(sess(display_name="a really long summary with no ticket in it"))
    assert long == "C■ a really lon… · example-task-4f21", long
    assert len(long) <= SIDEBAR_MAX, (long, len(long))
    print("selftest ok")


def main():
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


if __name__ == "__main__":
    main()
