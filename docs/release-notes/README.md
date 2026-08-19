# Release notes

One file per shipped change that a **dispatcher** or a **chauffeur** will notice.
Each file carries a ready-to-paste message for the group chat — open it, copy the
block, send it. That is the whole point of this folder.

Nobody has to ask for these. Claude writes one as part of shipping the change, and
a git hook refuses the commit if it didn't. See [Enforcement](#enforcement).

## Using them (Abdalla)

Newest file at the bottom of `ls`. Open it, copy everything inside the
**Send this to the team** block, paste it into the dispatcher chat. Send it the day
the change goes live — the note is written before the deploy, not after.

The **Behind the scenes** section is for you, not the chat. It's what to say when
someone asks "why did this change" or "it didn't work for me."

---

## Writing one (Claude)

### When

Write a note when the change alters something a dispatcher or chauffeur can **see or
do** — a new button, a moved button, a screen that behaves differently, a rule that
now fires, a field they now have to fill in, a thing that used to break and doesn't.

Do **not** write one for work that is invisible to them: refactors, tests, migrations
with no UI, performance, logging, docs, dependency bumps. Record those with a
`Release-Note: none` line in the commit message body instead.

Judgement call: *would a dispatcher notice, on a normal shift, without being told?*
If yes, it needs a note. If it changes their habits, it definitely needs one.

### File name

`YYYY-MM-DD-short-slug.md` — the date it ships, and a slug a human can scan:

```
2026-08-19-text-guest-from-trip.md
2026-08-17-staffing-board-time-off.md
```

Start from `_TEMPLATE.md`. One note per shipped change, not per commit — a feature
built over six commits gets one note, written on the commit that makes it live.

### Voice — this is the part that matters

The message block gets pasted, unedited, into a chat with people who do not read
code. Write it the way you would say it standing next to them.

**Do:**
- Name screens the way *they* name them — "the schedule board", "the trip card",
  "the driver app" — not the template name or the URL.
- Lead with what they can now do, not with what was built.
- Give numbered steps when there's a click path. Two or three steps, not eight.
- Say plainly what did **not** change. That's what stops the worried questions.
- Keep it under ~150 words. If it needs more, the change needed to be simpler.

**Never:**
- File names, function names, model or field names, URLs with query strings, commit
  SHAs, "endpoint", "migration", "refactor", "we deployed".
- "Should" or "hopefully". Either it works or it isn't shipped.
- Apologising for the old behaviour, or explaining the bug's cause. They want the
  new instruction, not the postmortem.

**Bad:** "Added a LegClientMessage model and a comms_views endpoint so drivers can
fire templated SMS via an `sms:` deep link from the trip detail partial."

**Good:** "Drivers can now text the guest straight from the trip screen — tap
Message, pick the situation, and it opens their texting app with the words already
written. They just hit send."

### Rules the copy has to keep

These carry over from how the business actually runs — a note that contradicts them
teaches dispatchers something false:

1. **Airport pickups are never curbside.** The chauffeur walks in and meets the
   guest inside. Never write "curb", "outside", or "arrivals level".
2. **Never promise a car colour.** Nothing in the system stores one.
3. **Nothing auto-texts or auto-calls a driver.** If a note describes an automated
   nudge to a chauffeur, it's wrong — dispatch calls, a human decides.
4. Don't quote a flight time for a departure. Only arrivals have one.

### Behind the scenes section

Short. Where it lives (the screen, in their words), why it was built, and the one or
two things that will generate questions. This is Abdalla's cheat sheet, not a
changelog entry — the git history already is one.

---

## Enforcement

`.claude/hooks/release_note_guard.py`, wired as a PreToolUse hook in
`.claude/settings.json`. Before any `git commit` or `git push`, it looks at what is
about to ship. If anything under `dispatching/`, `drivers/`, `ops/`, or
`reservations/admin.py` changed and no new file in this folder is staged alongside
it, the command is refused with instructions.

Escape hatch, for genuinely invisible work:

```
git commit -m "Speed up the legs query

Release-Note: none"
```

Excluded automatically, no trailer needed: migrations, test files, `.md` files,
`__pycache__`, fixtures.

**Changing what counts as visible:** edit `WATCHED` at the top of the guard script.
The guard is deliberately fail-open — if it errors, the commit goes through. It is a
backstop for the rule in `CLAUDE.md`, not a gate that can wedge the repo.
