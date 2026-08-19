#!/usr/bin/env python3
"""Block a commit/push that ships dispatcher- or driver-facing changes without a release note.

Wired as a PreToolUse hook on Bash git commands (see .claude/settings.json).
The rule itself lives in CLAUDE.md; this is the backstop that makes it stick.

Fails open on purpose: any unexpected error here lets the git command through.
A broken guard must never block work.
"""

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

NOTES_DIR = "docs/release-notes"
NOTES_META = {"README.md", "_TEMPLATE.md"}
SKIP_TRAILER = "release-note: none"

# Paths a dispatcher or a chauffeur can actually see the effect of.
WATCHED = ("dispatching/", "drivers/", "ops/", "reservations/admin.py")

# ...minus the parts of those apps nobody outside the repo ever notices.
IGNORED_PARTS = ("/migrations/", "/__pycache__/", "/fixtures/")


def git(*args):
    """Run git in the repo; return stdout, or None if the command failed."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=15
        )
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def lines(out):
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def is_watched(path):
    p = path.replace("\\", "/")
    if not p.startswith(WATCHED):
        return False
    if any(part in p for part in IGNORED_PARTS):
        return False
    base = p.rsplit("/", 1)[-1]
    if base.startswith("test") or p.endswith(".md"):
        return False
    return True


def is_note(path):
    p = path.replace("\\", "/")
    return (
        p.startswith(NOTES_DIR + "/")
        and p.endswith(".md")
        and p.rsplit("/", 1)[-1] not in NOTES_META
    )


def has_all_flag(cmd):
    """True for `git commit -a`, `-am`, `--all` — those sweep in unstaged tracked edits."""
    for tok in re.findall(r"(?<!\S)--?[A-Za-z]+", cmd):
        if tok == "--all":
            return True
        if not tok.startswith("--") and "a" in tok[1:]:
            return True
    return False


def commit_scope(cmd):
    """(files this commit will contain, release notes it will add)"""
    files = set(lines(git("diff", "--cached", "--name-only")))
    added = set(lines(git("diff", "--cached", "--name-only", "--diff-filter=A")))
    if has_all_flag(cmd):
        files |= set(lines(git("diff", "--name-only")))
    if "--amend" in cmd:
        head = set(lines(git("show", "--pretty=", "--name-only", "HEAD")))
        files |= head
        added |= {f for f in head if is_note(f)}
    return files, {f for f in added if is_note(f)}


def push_range():
    upstream = (git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "").strip()
    if not upstream:
        branch = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        if not branch:
            return None
        upstream = "origin/" + branch
        if git("rev-parse", "--verify", "--quiet", upstream) is None:
            return None
    return upstream + "..HEAD"


def push_scope(rng):
    """(files being pushed, notes being pushed, shas that ship watched changes un-noted)"""
    files = set(lines(git("diff", "--name-only", rng)))
    added = {f for f in lines(git("diff", "--name-only", "--diff-filter=A", rng)) if is_note(f)}
    unnoted = []
    for sha in lines(git("rev-list", rng)):
        touched = lines(git("show", "--pretty=", "--name-only", sha))
        if not any(is_watched(f) for f in touched):
            continue
        msg = (git("log", "-1", "--format=%B", sha) or "").lower()
        if SKIP_TRAILER not in msg:
            unnoted.append(sha)
    return files, added, unnoted


def allow():
    sys.exit(0)


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def build_reason(verb, watched_files, untracked_notes):
    shown = sorted(watched_files)[:8]
    listed = "\n".join("  " + f for f in shown)
    if len(watched_files) > len(shown):
        listed += "\n  ...and %d more" % (len(watched_files) - len(shown))

    if untracked_notes:
        staging = (
            "You already wrote a note but it is not staged:\n"
            + "\n".join("  " + f for f in sorted(untracked_notes))
            + "\n\nStage it and re-run the %s:\n  git add %s\n"
            % (verb, " ".join(sorted(untracked_notes)))
        )
    else:
        staging = (
            "Write the note before this ships:\n"
            "  1. Read %s/README.md (voice + rules), then copy %s/_TEMPLATE.md\n"
            "     to %s/%s-<short-slug>.md\n"
            "  2. Fill it in. The 'Send this to the team' block is what gets pasted into\n"
            "     the group chat -- plain language, no file names, no field names, no jargon.\n"
            "     Say what to click, what is different, and what did NOT change.\n"
            "  3. git add %s/<the-new-file>.md\n"
            "  4. Re-run the %s.\n"
            % (NOTES_DIR, NOTES_DIR, NOTES_DIR, date.today().isoformat(), NOTES_DIR, verb)
        )

    return (
        "Release note required.\n\n"
        "This %s ships changes a dispatcher or chauffeur will see:\n%s\n\n"
        "%s\n"
        "If this change is genuinely invisible to them (refactor, tests, internal plumbing),\n"
        "skip the note by putting this line in the commit message body instead:\n"
        "  Release-Note: none"
    ) % (verb, listed, staging)


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        allow()

    cmd = ((data.get("tool_input") or {}).get("command") or "")
    low = cmd.lower()

    # Cheap bail-out first: this hook runs on every git command.
    if "commit" not in low and "push" not in low:
        allow()
    if SKIP_TRAILER in low:
        allow()

    is_commit = re.search(r"\bgit\s+(?:-\S+\s+)*commit\b", low)
    is_push = re.search(r"\bgit\s+(?:-\S+\s+)*push\b", low)
    if not (is_commit or is_push):
        allow()

    try:
        if is_commit:
            verb = "commit"
            files, notes_added = commit_scope(cmd)
        else:
            verb = "push"
            rng = push_range()
            if rng is None:
                allow()  # first push of a branch, or no upstream to compare against
            files, notes_added, unnoted = push_scope(rng)
            if not unnoted:
                allow()

        watched = {f for f in files if is_watched(f)}
        if not watched or notes_added:
            allow()

        untracked = [
            f for f in lines(git("ls-files", "--others", "--exclude-standard", NOTES_DIR))
            if is_note(f)
        ]
        deny(build_reason(verb, watched, untracked))
    except SystemExit:
        raise
    except Exception:
        allow()


if __name__ == "__main__":
    main()
