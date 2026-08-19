# Grayson Towncar — working rules

## Release notes — automatic, never ask for permission

Every change a **dispatcher or chauffeur can see or do** ships with a release note.
Write it as part of the work, before the commit. Never wait to be asked, and never
ask whether one is wanted — the answer is always yes.

1. Copy `docs/release-notes/_TEMPLATE.md` to `docs/release-notes/YYYY-MM-DD-slug.md`
2. Read [docs/release-notes/README.md](docs/release-notes/README.md) first — it holds
   the voice rules. The **Send this to the team** block gets pasted straight into the
   dispatcher group chat, so write it the way you'd say it out loud: no file names, no
   field names, no jargon, under ~150 words, and always say what did *not* change.
3. `git add` the note alongside the change.

One note per shipped change, not per commit — a feature built over six commits gets a
single note, written on the commit that makes it live.

Work that is invisible to them — refactors, tests, migrations with no UI, performance,
logging, docs, dependency bumps — gets no note. Record that with a `Release-Note: none`
line in the commit message body instead.

A PreToolUse hook, [.claude/hooks/release_note_guard.py](.claude/hooks/release_note_guard.py),
refuses a commit or push that ships a visible change without a note. If it blocks you,
write the note — don't reach for the escape hatch unless the change genuinely is invisible.

## Frontend work

Before touching any HTML, CSS, JS, or template, read [docs/claude.md](docs/claude.md) —
brand context, typography, colour, spacing, and the luxury standards this site holds to.
