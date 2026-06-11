# Sandbox Scheduling — How It Works

A per-day **hold → build privately → manager review → publish → notify** workflow for
driver schedules, plus live-change awareness so drafters never get blindsided.

## Status & roadmap (read this first)

**v1 — DONE, but UNCOMMITTED.** Everything in this doc is implemented and tested in the
working tree on `main` (migrations `reservations/0110`, `0111`, `0112` applied to the dev DB).
It has **not** been committed or pushed yet — we were about to push it to a `sandbox` branch
and paused. Next step when resuming: branch off `main`, commit the working tree, push.

**v2 — PLANNED & APPROVED, NOT STARTED: "Sandbox POV Review Mode" (per-user private sandboxes).**
The plan is approved and saved at `~/.claude/plans/tingly-petting-bengio.md`. It replaces the
current *one-shared-draft-per-date* model with *one private sandbox per (date, owner)*:
- New `ScheduleDraft` fields: `owner`, `superseded_by`, `superseded_at`, `last_activity_at`;
  new state `superseded`; constraint `uniq_active_draft_per_date` → `(schedule_date, owner)`;
  migration `0113` (with `owner = created_by` backfill — safe, verified).
- **Explicit POV routing:** every edit carries a `sandbox_id` (or none = Live) instead of the
  implicit `_active_draft_for_date(date)`. A sandbox user viewing Live edits live; one user's
  edits can never land in another's overlay.
- **Sandbox selector** on board/dashboard/planner (Live / My Sandbox / — managers — anyone's
  sandbox as a read-only POV). Manager reviews a chosen POV, **publishes one** → applies to
  `Leg.driver` and **supersedes all other** sandboxes for that date.
- **Forgotten-publish/safety warnings** (today unpublished / tomorrow awaiting review /
  within-48h not published / published-but-not-notified) → navbar badge + a "Sandbox reviews" page.
- v2 scope decided: **Core + safety warnings**, **read-only** manager review. Deferred:
  clone/rebase of superseded, manager edit-takeover, and the schedule-quality checklist.
- **Dev-DB caveat:** a stray active test-draft (id 21, date 2026-06-06, owner abdi) exists from
  this session's testing — delete it before running the `0113` migration. Demo day 2026-06-06
  (`seed_sandbox_demo`) has 2 assigned / 3 unassigned legs.

Everything below describes the **v1** that is live in the working tree.

---

---

## 1. The problem it solves

In this app, **driver visibility *is* the `Leg.driver` field.** The moment a dispatcher
sets `leg.driver`, that job appears on the driver's app
(`drivers/views.py` → `Leg.objects.filter(driver=driver, pickup_date=...)`).
There was no "draft" state — so building or adjusting a future day exposed half-finished
assignments to drivers, causing calls, texts, and confusion.

The sandbox decouples **"what I'm planning"** from **"what drivers see"** without changing
the driver app at all.

---

## 2. Core idea — a delta overlay

`Leg.driver` stays the **single source of truth for the LIVE / published schedule.**
Drivers keep querying it, unchanged.

When a day is **held**, a granted user's edits are routed into a **separate overlay table**
(`DraftAssignment`) instead of `Leg.driver`. Because the overlay never touches `Leg.driver`,
drivers keep seeing only the last-published state until a manager publishes.

```
                    held day, granted user edits
   dispatcher edit ───────────────► DraftAssignment (overlay)   ← drivers DON'T see this
                                          │
                              manager clicks Publish
                                          ▼
                    apply overlay ──► Leg.driver (live)          ← drivers see this
                                          │
                              manager clicks "Text drivers"
                                          ▼
                                   Twilio SMS to affected drivers
```

**The invariant that makes it safe:** *no draft code path ever calls `leg.save()` touching
`driver` until publish.* That means none of `Leg.save()`'s side effects (driver-pay calc,
gratuity split, night bonus, the NTFY status push, ops task auto-close) fire while you draft.
They fire correctly **at publish**, exactly as a normal live edit would.

- **Effective draft value** for a leg = its overlay row's `proposed_driver` if a row exists,
  else the live `Leg.driver`. Three meaningful states:
  - row with a driver → "draft assigns this driver"
  - row with `proposed_driver = NULL` → "draft says unassigned"
  - no row → "no draft opinion; show live"
- **Delta-only:** only legs you actually touch get an overlay row. A booking created after the
  hold has no row, so it shows up as **unassigned / needs attention** for free (live-merge).

---

## 3. Who can use it (access control)

Access is **per-user**, granted via a standard Django permission:
**`reservations.use_schedule_sandbox`** ("Can build/hold sandbox schedules").

| Role | How identified | Can do |
|---|---|---|
| **Sandbox builder** | has `use_schedule_sandbox` (granted in admin) | Hold a day, edit the overlay, submit for review, discard own draft |
| **Manager** | `is_superuser` (always has the permission implicitly) | Everything above **+ approve/publish, request changes, text drivers** |
| **Everyone else** | staff without the permission | **Edits the LIVE schedule exactly as before — a held day never affects them, and they see no banner** |

**Granting it:** Django admin → **Users** → pick the user → **Permissions** → *User permissions*
→ add *"reservations | schedule draft | Can build/hold sandbox schedules"*. (Or attach it to a
Group and add people to the group.) Superusers don't need it explicitly.

Helper: `can_use_sandbox(user)` in `dispatching/views.py`
= `user.is_superuser or user.has_perm("reservations.use_schedule_sandbox")`.

---

## 4. The lifecycle (state machine)

```
 (none) ──Hold[builder/mgr]──► draft ──Submit[builder]──► in_review ──Approve+Publish[mgr]──► published ●
                                 │  ▲                          │  │ Request changes (note)[mgr]
                       discard   │  │ resubmit                 │  ▼
                                 ▼  └──────────── changes_requested
                             discarded ●                       │ discard
                                                               ▼
                                                           discarded ●
```

- `published` and `discarded` are **terminal**.
- A manager building their **own** draft may publish directly from `draft` (skip the review step).
- A partial unique constraint allows **re-holding a day that was already published** — a new
  draft opens over the old one with a fresh baseline ("publish, then keep adjusting").
- NTFY/SMS side effects fire **only** on the `→ published` edge and the separate manual Notify.

Every transition is recorded on a single timeline (`ScheduleDraftEvent`) that doubles as the
**audit trail** (who created/edited/submitted/approved/rejected/published/notified) **and** the
**manager-feedback log** (rejection notes). Visible in the *Changes & activity* modal.

---

## 5. Data model (`reservations/models.py`)

| Model | Purpose | Key fields |
|---|---|---|
| **`ScheduleDraft`** | One active draft per date | `schedule_date`, `state`, `created/submitted/reviewed/published/notified_by/at`, `base_snapshot` (FK), `baseline_leg_ids`, `baseline_legs` |
| **`DraftAssignment`** | The delta overlay | `draft`, `leg`, `proposed_driver` (NULL = unassign), `assigned_by/at`; unique `(draft, leg)` |
| **`ScheduleDraftEvent`** | Audit + feedback timeline | `draft`, `event_type`, `actor`, `note`, `metadata`, `created_at` |

- **`baseline_leg_ids`** — leg IDs present at hold time → drives "new since draft" (live-merge).
- **`baseline_legs`** — per-leg snapshot at hold time `{driver_id, pickup_time, pickup_date,
  pickup_location, dropoff_location}` → drives **live-change detection** (driver conflicts + time moves).
- **`base_snapshot`** — a `ScheduleSnapshot` of the live assignments at hold time → used by the
  publish-time conflict check and is restorable via the existing snapshot/undo feature.
- Constraint **`uniq_active_draft_per_date`** — at most one *non-terminal* draft per date;
  terminal (published/discarded) drafts coexist as history.

---

## 6. How edits are routed (the three write paths)

When a date is **held**, these existing endpoints route a *granted* user's edits to the overlay;
a non-granted user (or `live_override`) writes live as before:

| Path | View | Held + granted → | Else → |
|---|---|---|---|
| Manual assign / drag-drop | `update_leg_assignment` | overlay row | live `Leg.driver` |
| Auto-assign (apply) | `auto_assign_drivers` | overlay rows | live |
| Reset day | `reset_schedule` | reset the *draft* to all-unassigned | wipe live |

Gate per path: `draft = _active_draft_for_date(date)` then
`use_overlay = draft and can_use_sandbox(user) and not live_override`.

The overlay writer is `_upsert_draft_assignment(...)` — it **never calls `leg.save()`**, which is
what keeps all the side effects off until publish.

**Emergency "Edit live" hatch:** on a held day, a granted user can flip the **Edit live** toggle
in the banner; drag changes then write `Leg.driver` directly (reaching the driver immediately) and
are mirrored into the overlay so a later publish won't revert the fix.

---

## 7. Publish & notify

**Publish** (`publish_draft`, manager-only) runs in one transaction:

1. **Conflict detection (three-way):** for each staged leg, compare the **baseline** driver
   (at hold), the **live-now** driver, and the **proposed** driver. If live diverged from baseline
   *and* from proposed → a conflict (someone changed it live under you). Returns **HTTP 409** with a
   named list unless `force=true`. (Unassigned-at-hold counts as baseline = *nobody*, so a fresh leg
   a dispatcher assigned live is caught too — never silently overwritten.)
2. **Apply:** for each delta, set `leg.driver = proposed`, `driver_assigned_by/at`, then a **full
   `leg.save()`** so pay/gratuity/night-bonus/NTFY recompute now. No-op legs are skipped.
3. Mark `published`, log the event, invalidate the capacity cache. Date returns to live.

**Notify** (`notify_drivers`, manager-only, **separate button**) texts the affected drivers (those
who gained/lost legs in the publish) via Twilio — `notify_drivers_of_release()` in
`dispatching/confirmation_sms.py`, run in the background, **idempotent** (the button can't double-send).
Message example: *"Grayson Towncar — your schedule for Wed Jun 3 was updated. You now have 4 trips,
first pickup 6:15 AM (MCO → Disney). Open the app for details."*

---

## 8. Live-change awareness ("changed since you started")

While you hold and build a day, other people (or non-sandbox dispatchers) keep operating live.
The held-day **summary** surfaces what changed under you, in two categories:

### a) Driver conflicts — *publishing overwrites*
A leg **you staged** whose **live driver was changed** by someone else.

- The board/dashboard now show the **LIVE driver** (reality — e.g. Angel), **not** your staged pick.
  (Conflicted legs are *not* re-pointed to the proposed value.)
- A clean card shows both sides with **who + when**:
  ```
  6:15 AM · MCO → Disney's Grand Floridian
    [LIVE NOW]   Angel Almanzar   — dispatch · 3:42 PM
    [YOU STAGED] abdi             — you · 2:15 PM
    On publish:  A̶n̶g̶e̶l̶  →  abdi
  ```
- Publishing is **blocked (409)** until you reconcile or confirm overwrite.
- "Who set it live" comes from `Leg.driver_assigned_by/at`; "who staged it" from
  `DraftAssignment.assigned_by/at`.

### b) Field changes — *informational (publish doesn't touch them)*
A leg's **pickup time** moved live since you opened the draft (the overlay only carries drivers,
so publishing won't change the time — this is just a heads-up):
```
⏰ MCO → Disney's: Pickup time  7̶:̶3̶0̶ ̶A̶M̶ → 7:00 AM  — dispatch · 3:47 PM
```
Detected by comparing the live leg to `baseline_legs`; "who/when" comes from the leg's
`simple_history` (the `HistoryRequestMiddleware` is installed, so real edits capture the editor).

Both appear as **count chips** in the banner ("1 changed live", "1 time change"), dedicated
sections in the **Changes & activity** modal, and per-job indicators (red outline / ⚠ CHANGED LIVE
and purple / ⏰ TIME CHANGED).

---

## 9. Where it shows up (UI)

The same self-contained partial renders on **all three** dispatcher surfaces (so a held day looks
consistent and the safety holds everywhere edits can happen):

| Page | URL | What's added |
|---|---|---|
| Schedule board | `/dispatching/schedule-board/` | banner, proposed lanes, conflict/time indicators, DnD → overlay, lock-when-in-review, review modal |
| Legs dashboard | `/dispatching/` | banner, dropdown shows proposed/live, ◇ PROPOSED / NEW / ⚠ CHANGED LIVE / ⏰ TIME CHANGED badges |
| Capacity planner | `/dispatching/capacity-planner/` | banner; capacity/coverage reflect the proposed world; cache bypassed while held |

- **Banner states:** grey *Live* (with **Hold this day**) · gold *Draft — building* · blue
  *Awaiting review* · amber *Changes requested* · green *Published* (with **Text drivers**).
- **Non-granted users see none of this** — the partial renders nothing for them.

Shared partial: `dispatching/templates/dispatching/includes/_draft_banner.html`
(self-contained CSS + banner + review modal + JS).

---

## 10. Endpoints (`dispatching/urls.py`)

| Path | Name | Guard |
|---|---|---|
| `open-draft/` | `open_draft` (Hold) | sandbox |
| `submit-draft/` | `submit_draft` | sandbox |
| `reject-draft/` | `reject_draft` (note required) | manager |
| `discard-draft/` | `discard_draft` | sandbox (own) / manager (any) |
| `draft-review/` (GET) | `draft_review` (diff + events JSON) | manager |
| `approve-draft/` | `publish_draft` | manager |
| `notify-drivers/` | `notify_drivers` | manager |

Driver edits keep using the existing `update-leg-assignment/` — it's gated transparently, so the
drag-drop JS needed no endpoint change.

---

## 11. Key code map

| Concern | Location |
|---|---|
| Models | `reservations/models.py` (`ScheduleDraft`, `DraftAssignment`, `ScheduleDraftEvent`) |
| Access helper | `dispatching/views.py` → `can_use_sandbox()` |
| Gate / overlay write | `_active_draft_for_date()`, `_upsert_draft_assignment()` |
| Render context + overlay | `_draft_view_context()`, `_apply_draft_overlay()`, `_compute_draft_diff()` |
| Lifecycle views | `open_draft / submit_draft / reject_draft / discard_draft / draft_review / publish_draft / notify_drivers` |
| Twilio release text | `dispatching/confirmation_sms.py` → `notify_drivers_of_release()` |
| Shared UI | `dispatching/templates/dispatching/includes/_draft_banner.html` |
| DnD routing + lock | `content/static/js/timeline-dnd.js` |
| Demo data | `dispatching/management/commands/seed_sandbox_demo.py` |

---

## 12. Operational walkthroughs

### Dispatcher (granted) builds a day
1. Open the board/dashboard/planner for the date → click **Hold this day**.
2. Drag / assign / auto-assign — everything stages privately (drivers see nothing).
3. Click **Submit for review** (optional note). The board locks for you.

### Manager reviews & releases
1. Open the held day → **Changes & activity** shows the diff (reassignments / new / unassign /
   needs-attention) + any **live conflicts** and **time changes** + the timeline.
2. **Approve & Publish** (drivers' assignments go live now) — or **Request changes** with a note.
3. If a conflict pops up, you'll see exactly what changed live and by whom; choose to overwrite or back off.
4. Click **Text drivers** to send the Twilio release notice (manual, idempotent).

### Anyone without the grant
Works exactly as before — live edits, no banner, instantly visible to drivers. If they touch a leg
you're drafting, you get the "changed live" warning and publish protects you from silently clobbering them.

---

## 13. Edge cases handled

- **Unassigned-at-hold leg changed live** → caught at publish (no silent overwrite).
- **Same-day hold** → warning banner + **Edit live** toggle for genuine emergencies.
- **Leg cancelled / moved off the date while held** → skipped at publish, logged.
- **Two granted users on one draft** → collaborate on the same overlay; per-leg last-write-wins, all audited.
- **Re-hold a published day** → new draft over the old one with a fresh baseline.
- **Auto-assign preview / driver status updates** → unaffected (preview never saves; status edits aren't overlaid).

---

## 14. Testing the feature

Seed a future demo day (reuses existing rates/vehicles/drivers; tagged `[SANDBOX DEMO]`):

```bash
python manage.py seed_sandbox_demo                 # today + 3
python manage.py seed_sandbox_demo --date 2026-06-10
python manage.py seed_sandbox_demo --teardown      # remove it
```

Then: log in as a manager (or grant a dispatcher the permission), open the board for that date,
**Hold**, make changes, optionally have a second account change a job live to see the conflict /
time-change warnings, then **Submit → Approve & Publish → Text drivers**.

The critical invariant to spot-check: while a day is held, a driver's own app/dashboard must keep
showing the **pre-hold** state until you publish.
