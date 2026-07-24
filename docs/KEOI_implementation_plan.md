KEOI — "Keep Eye On It" Implementation Plan
Context
Schedulers often build tight-but-workable driver schedules (e.g. a 9:00 AM MCO arrival followed by a 10:30 AM Disney→MCO return). The risk knowledge stays in the scheduler's head; day-of dispatchers can't see which legs need watching. KEOI lets a scheduler/dispatcher flag a specific Leg with a category and a required description (what's risky, what to monitor, what to do). The flag stands out on every dispatcher board until that leg completes, then auto-closes with its history preserved. Purely manual in v1 — no automatic conflict detection. Staff-only: the driver portal is explicitly untouched (user-confirmed).

All line anchors below were verified against the working tree on 2026-07-24. If lines have drifted, anchor on the quoted code/identifiers, not the numbers. Ignore .claude\worktrees\* (stale copies).

1. Current Architecture Findings
Leg model — reservations/models.py:925-2289. status CharField (:1062-1068), choices DRIVER_STATUS (reservations/constants.py:21-29): in-progress (default), confirmed, on-the-way, on-location, picked-up, completed, cancelled. Terminal = completed + cancelled (double-L; payment_status uses single-L canceled — don't confuse). No no-show status exists. reservation FK related_name=legs; driver FK SET_NULL; pickup_date DateField + pickup_time TimeField (naive; TIME_ZONE="America/New_York", USE_TZ=True — use timezone.now() for stamps). Leg has HistoricalRecords() (:1766) and a heavy custom save() (:1563-1764) that auto-resets non-terminal statuses to in-progress on driver change and never resurrects terminal legs.

Every leg-completion pathway (verified exhaustively — all go through instance .save() with 'status' in update_fields, so Django signals fire on every one):

A. Board AJAX update_leg_assignment dispatching/views.py:2326-2532 (status branch :2455-2508; writes LegStatus row :2477; calls check_and_update_completion_status() on complete :2487; is_staff inline gate :2338).
B. Driver portal update_leg_status drivers/views.py:414-467 (no cancelled allowed).
C. Driver accept_job drivers/views.py:636-669 (sets confirmed only).
D. Bulk bulk_update_leg_status dispatching/views.py:8174-8219 (loops with per-leg .save()).
E. Refund cancellation process_refund dispatching/views.py:9343-9383 — per-leg leg.save(update_fields=['status','payment_status','driver']) at :9350-9353 / :9371-9375 (verified: signals fire).
F. Bulk driver reset dispatching/views.py:11708-11722 — the only queryset .update() on status; verified it never crosses a terminal boundary (first .update() at :11715 strips drivers from completed legs, removing them from the lazy driver__isnull=False queryset before the second .update() forces in-progress).
No background task, import, admin action, or signal writes leg completed. Reservation-level admin actions don't touch legs.
Existing Leg signal pair — reservations/signals.py: store_leg_old_values pre_save (:742-761, skips DB fetch when update_fields excludes status/driver) + log_leg_changes post_save (:764-835, writes AuditLog on driver/status transitions, actor via thread-local). Trap: it deletes _pre_save_old_values at :832-833 — new receivers must use their own attribute.

Audit prior art — generic AuditLog (reservations/models.py:3043-3145: model_name/object_id/action/field_name/old_value/new_value/user FK/username string/notes; indexes incl. (model_name, object_id)), written via create_audit_log() (reservations/signals.py:580, truncates values >500, normalizes AnonymousUser). Actor recovery for signals: ThreadLocalMiddleware → get_current_user()/get_current_request() (reservations/middleware.py:17-27). Unattributed-event convention: user=None, username="guest" (dispatching/pickup_moves.py:74). The time-changed badge (pickup_time_changed_at/pickup_change_ack_at, models.py:1277-1291, has_unacked_time_change :1336) is prior art for "visible until cleared". LegStatus (models.py:3148-3204) is the companion-model pattern.

Boards (all server-rendered Django + vanilla JS, Bootstrap 5 + bi-icons; no jQuery/HTMX/WebSockets/polling; filters are GET params rebuilt into links; AJAX = fetch + X-CSRFToken, responses {"success": bool, "error": str}):

Main dashboard index dispatching/views.py:110-875 → legs_filter.html (5338 lines). One date, all legs, _base_legs_qs (:145-180) with big select_related + prefetch (incl. Prefetch("status_history"...) :166-169), evaluated once (:186); driver/trip_type filters applied in Python (:199-219). Desktop <tr> @1080 + mobile card @1623/1624 (both must carry any row treatment). Badge prior art: VIP @1090, time-changed @1262, flight flags @1129-1141. Modals: Status History (hidden-div copy) @4755, Leg History (lazy fragment via leg_history_partial) @4782. showToast @3770 is scoped inside a click handler, not global.
Schedule board schedule_board views.py:879-1497 → schedule_board.html (same template for ?view=inhouse|affiliate); chips @649 (unassigned) / @790 (driver) with data-note-* attrs from _slot_notes() (views.py:2585-2630) feeding a JS hover popup (showJobPopup @990, has escapeHtml @984); chips click-through to dashboard ?highlight= @1240-1251 (the touch affordance).
Dashboard driver timeline includes/driver_timeline.html slot @71-89; popup builder in legs_filter.html @4359-4373 (no escapeHtml in scope — must add one).
Legs List legs_list views.py:2124 → legs_list.html, paginated, uses shared get_filtered_legs_queryset (dispatching/utils.py:10-102, prefetch block @52-58). The only surface browsing future dates.
detect_leg_flags (utils.py:684-750) computes transient warnings per render — leave untouched; KEOI is persistent state with its own badge.
Permissions — stock auth.User; dispatcher tier = is_staff (inline checks returning 403 JSON, e.g. views.py:2338); manager tier = is_superuser. Custom-permission precedent: use_schedule_sandbox in ScheduleDraft.Meta.permissions (models.py:3371-3375) + can_use_sandbox() (dispatching/assignment.py:58-69). Partial-unique precedent: uniq_active_draft_per_date (models.py:3379-3385), legflight_one_controlling_per_leg (:2429-2433).

Tests — Django TestCase, no pytest/factories. setUpTestData fixture mixins; best templates: dispatching/tests_sandbox.py (permission grant via user_permissions.add + re-fetch, JSON endpoints), tests_affiliate_board.py (board assertContains), tests_flight_change_safety.py (AuditLog assertions), reservations/tests.py:342 VipFlagTests. Run with ENABLE_DEBUG_TOOLBAR=0, system python. New file convention: dispatching/tests_keoi.py.

Other facts: no soft-delete anywhere (cancellation = status string); no leg/reservation cloning code; Leg.reservation FK never reassigned; latest migration reservations/0121; hard-deleting a leg only happens outside normal flows (CASCADE is fine); notes render with auto-escaping only (no |safe/|linebreaks anywhere — multi-line via white-space: pre-wrap).

2. Recommended Architecture: Option B — dedicated LegKeoi model
Not Option A (fields on Leg): KEOI needs ~10 fields incl. a TextField. Leg has HistoricalRecords() — every field mirrors into HistoricalLeg and the description would be re-copied into a new historical row on every Leg save (Leg saves are frequent and perf-instrumented). Leg.save()'s update_fields-widening (:1648-1651, :1732-1744) could clobber KEOI fields from stale in-memory instances. No closed-flag history possible. The time-changed badge got away with fields-on-Leg because it's 3 nullable timestamps with no text and no history requirement — KEOI is not that.
Not Option C (generic flag system): zero generic-FK precedent in this codebase; kills Prefetch ergonomics and makes the one-active-per-leg constraint awkward; buys reuse nobody asked for. If a second flag type ever appears, LegKeoi's shape generalizes then.
Option B mirrors the established LegStatus companion pattern, is purely additive (no Leg/HistoricalLeg churn), supports full history as closed rows, and the partial-unique-constraint pattern is already proven in this exact project.
Active state is derived: closed_at IS NULL = active. No separate boolean — the DB constraint enforces "one active per leg" against the same expression, no shadow flag to drift. Operational status is a separate field and never controls visibility. The brief's suggested "Completed" operational status is intentionally not a manual choice: it would duplicate the closed state and invite "mark it completed to hide it" confusion. Manual statuses are Needs Attention / Being Monitored / Backup Arranged; "completed" is represented by the close itself (closed_at + closed_reason), shown as such in history.

3. Data Model
New model LegKeoi in reservations/models.py, placed after LegStatus.__str__ (~line 3204), before DriverLocation. Choices as model-local TextChoices (newest convention, per ScheduleDraftEvent.EventType), using the brief's category list:

class LegKeoi(models.Model):
    """'Keep Eye On It' — dispatcher-raised watch flag on ONE leg.
    Active while closed_at IS NULL. Auto-closes when the leg reaches a terminal
    status; auto-reactivates if the leg leaves it (unless admin-removed).
    operational_status is workflow color only — it NEVER hides the flag."""

    class Category(models.TextChoices):
        TIGHT_SCHEDULE       = "tight_schedule", "Tight Schedule"
        DRIVER_CONFLICT      = "driver_conflict", "Possible Driver Conflict"
        FLIGHT_DELAY         = "flight_delay", "Flight Delay Risk"
        VEHICLE_AVAILABILITY = "vehicle_availability", "Vehicle Availability Risk"
        PASSENGER_READINESS  = "passenger_readiness", "Passenger Readiness Risk"
        TRAFFIC              = "traffic", "Traffic Risk"
        WAITING_INFO         = "waiting_info", "Waiting on Information"
        OTHER                = "other", "Other"

    class OperationalStatus(models.TextChoices):
        NEEDS_ATTENTION = "needs_attention", "Needs Attention"
        BEING_MONITORED = "being_monitored", "Being Monitored"
        BACKUP_ARRANGED = "backup_arranged", "Backup Arranged"

    class ClosedReason(models.TextChoices):
        LEG_COMPLETED = "leg_completed", "Leg Completed"
        LEG_CANCELLED = "leg_cancelled", "Leg Cancelled"
        ADMIN_REMOVED = "admin_removed", "Removed by Admin"

    leg = models.ForeignKey("Leg", on_delete=models.CASCADE, related_name="keoi_flags")
    category = models.CharField(max_length=30, choices=Category.choices)
    description = models.TextField()  # required; enforced in views (codebase uses no forms for AJAX)
    operational_status = models.CharField(max_length=20, choices=OperationalStatus.choices,
                                          default=OperationalStatus.NEEDS_ATTENTION)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="keoi_created")
    created_at = models.DateTimeField(default=timezone.now)   # not auto_now_add (tests can backdate; matches LegStatus)
    updated_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="keoi_updated")
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=20, choices=ClosedReason.choices, null=True, blank=True)
    closed_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="keoi_closed")
    removal_reason = models.TextField(blank=True, default="")  # required iff admin_removed

    TERMINAL_LEG_STATUSES = ("completed", "cancelled")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["leg", "-created_at"])]
        permissions = [("remove_keoi", "Can remove KEOI flags (with reason)")]
        constraints = [
            models.UniqueConstraint(fields=["leg"], condition=models.Q(closed_at__isnull=True),
                                    name="uniq_active_keoi_per_leg"),
            models.CheckConstraint(
                check=(models.Q(closed_at__isnull=True, closed_reason__isnull=True)
                       | models.Q(closed_at__isnull=False, closed_reason__isnull=False)),
                name="keoi_closed_fields_paired"),
        ]

    @property
    def is_active(self):
        return self.closed_at is None
Plus a convenience property on Leg (near has_unacked_time_change, ~`models.py:1341`):

@property
def active_keoi(self):
    """Active KEOI flag or None. Uses the board prefetch when present (no N+1)."""
    if hasattr(self, "active_keoi_list"):       # set by Prefetch(to_attr=...)
        return self.active_keoi_list[0] if self.active_keoi_list else None
    return self.keoi_flags.filter(closed_at__isnull=True).first()
Notes: CASCADE matches LegStatus/LegStop/LegFlight; if a leg is ever hard-deleted, AuditLog rows (generic model_name/object_id) remain the permanent record. No HistoricalRecords() on LegKeoi (AuditLog carries transitions; the row carries final state). The partial unique index doubles as the board prefetch's index (WHERE leg_id IN (...) AND closed_at IS NULL) — no additional index needed. Description max length 2000 enforced in views (TextField, so raising later needs no migration).

4. Backend Changes
4a. Service module — NEW reservations/keoi.py
Two idempotent, race-safe functions (audit via existing create_audit_log from reservations.signals; actor fallback via get_current_user(), else user=None → username="guest" per pickup_moves.py:74 convention):

close_active_keoi(leg, reason, actor=None) — queryset .update() on filter(leg=leg, closed_at__isnull=True) setting closed_at=timezone.now(), closed_reason=reason, closed_by=<actor if authenticated else None>. If a row was updated, write AuditLog: model_name='Leg', object_id=leg.id, action='updated', field_name='keoi_closed', old_value='active', new_value=reason, notes=f"KEOI auto-closed: {reason}". Returns count.
reactivate_keoi(leg, actor=None) — no-op if an active flag already exists; find newest row with closed_reason__in=("leg_completed","leg_cancelled") (admin_removed never reactivates); null out closed_at/closed_reason/closed_by via save(update_fields=[...]) inside try/except IntegrityError (concurrent reactivation loses gracefully); audit field_name='keoi_reactivated'.
4b. Auto-close/reactivate — new signal pair appended to reservations/signals.py (after line 835)
Central enforcement point. Every terminal transition happens via instance .save() with 'status' in update_fields (§1), so signals catch pathways A/B/D/E; F's .update() provably never crosses a terminal boundary (leave a one-line comment at views.py:11712-11714 noting KEOI relies on that invariant).

@receiver(pre_save, sender=Leg)
def keoi_store_old_status(sender, instance, **kwargs):
    if not instance.pk:
        return
    uf = kwargs.get("update_fields")
    if uf is not None and "status" not in uf:
        return                                    # same skip-guard as store_leg_old_values
    instance._keoi_old_status = (Leg.objects.filter(pk=instance.pk)
                                 .values_list("status", flat=True).first())

@receiver(post_save, sender=Leg)
def keoi_sync_on_status_change(sender, instance, created, **kwargs):
    if created:
        return
    old = getattr(instance, "_keoi_old_status", None)
    if hasattr(instance, "_keoi_old_status"):
        del instance._keoi_old_status
    new = instance.status
    if old is None or old == new:
        return
    from reservations.keoi import close_active_keoi, reactivate_keoi
    TERMINAL = ("completed", "cancelled")
    if new in TERMINAL and old not in TERMINAL:
        close_active_keoi(instance, reason="leg_completed" if new == "completed" else "leg_cancelled")
    elif old in TERMINAL and new not in TERMINAL:
        reactivate_keoi(instance)
Must use the dedicated _keoi_old_status attribute — log_leg_changes deletes _pre_save_old_values and receiver order is module-position-dependent. Cost: one extra indexed single-column SELECT per status-touching save (same as the existing pair). Not in Leg.save() (200 lines of widening/side effects) and not per-call-site (the invariant belongs at the model layer; the codebase's own history shows call sites get missed — see F's apology comment).

4c. Endpoints — NEW dispatching/keoi_views.py (module-per-feature precedent: flight_verify_views, overnight_views in urls.py:1-8)
All @login_required + @require_POST (history is GET), json.loads(request.body), JsonResponse shapes per convention, standard CSRF. Three endpoints (a separate quick status endpoint was considered and dropped — the modal is the only status surface in v1, and keoi_save covers it with per-field audit):

keoi_save (create/edit upsert, addressed by leg_id — the partial unique constraint makes "the active KEOI for leg N" unambiguous). Request: {"leg_id", "category", "description", "operational_status"?}. Validation in order → {"success": false, "error": ...}: is_staff else 403; leg exists else 404; category in LegKeoi.Category.values else 400; description.strip() nonempty else 400 ("Description is required — say what to watch and what to do."), len ≤ 2000 else 400; operational_status in OperationalStatus.values (default needs_attention on create) else 400; terminal-leg guard: leg already completed/cancelled → 400 "Cannot flag a completed/cancelled leg" (it would auto-close instantly; mirrors the cancelled-leg driver-assign guard views.py:2386-2391). Logic inside transaction.atomic(): fetch active flag → edit path (stamp updated_by, audit only changed fields); else create with created_by inside try/except IntegrityError → re-fetch and apply as edit (double-create race resolved by the constraint). Response: {"success": true, "created": bool, "keoi": {id, leg_id, category, category_label, operational_status, status_label, description, created_by, created_at_display, updated_by, updated_at_display}} — labels from get_*_display(), never duplicated in JS.

keoi_remove — gate request.user.has_perm("reservations.remove_keoi") else 403 (superusers pass automatically; managers can delegate via admin without a deploy — the use_schedule_sandbox playbook; do not hard-code is_superuser). Request {"leg_id", "reason"}; reason.strip() required else 400; no active flag → 404. Close with closed_reason='admin_removed', closed_by=request.user, store removal_reason; audit field_name='keoi_removed' with the reason in notes. Removed flags never reactivate.

keoi_history (GET ?leg_id=) — is_staff; all LegKeoi rows for the leg, newest first, incl. closed_reason/closed_by/removal_reason. Powers the modal's "previous flags" section.

URLs in dispatching/urls.py near the acknowledge-time-change block (~line 118): keoi/save/ → keoi_save, keoi/remove/ → keoi_remove, keoi/history/ → keoi_history; add from . import keoi_views at top.

4d. Audit events (all via create_audit_log, logged against model_name='Leg' so they surface in the existing Leg History modal for free; do NOT extend AuditLog.ACTION_CHOICES — the event lives in field_name)
Event	action	field_name	old → new	actor
created	created	keoi	— → category	request.user
category changed	updated	keoi_category	old → new	request.user
description changed	updated	keoi_description	old → new (helper truncates >500; full text lives on the row)	request.user
op status changed	status_changed	keoi_operational_status	old → new	request.user
auto-closed	updated	keoi_closed	active → reason	thread-local user (driver completions attribute to the driver's User); fallback username="guest"
reactivated	updated	keoi_reactivated	reason → active	thread-local user / guest
admin removed	updated	keoi_removed	active → admin_removed	request.user; notes carry the reason
No duplicate records: the signal pair writes only close/reactivate events; views write only create/edit/remove events; there is no overlap. Driver/vehicle reassignment and leg-time changes while a KEOI is active are already audited by the existing log_leg_changes / pickup-move AuditLog rows — no new events needed (avoids duplication).

4e. Query integration (no N+1)
Add to each board queryset's prefetch_related:

Prefetch("keoi_flags",
         queryset=LegKeoi.objects.filter(closed_at__isnull=True).select_related("created_by"),
         to_attr="active_keoi_list")
index → _base_legs_qs prefetch block (views.py:166-169).
schedule_board → prefetch block (views.py:928-933).
get_filtered_legs_queryset full branch (utils.py:52-58) — covers Legs List; skip the optimize_for_stats branch.
Templates/views then read leg.active_keoi (property; zero queries when prefetched). Served by the partial unique index. detect_leg_flags untouched.

5. Frontend and Board Changes
Visual identity: teal #0d9488 (text #0f766e, fill #e6fffa, tint rgba(13,148,136,.07)) — the one calm unclaimed hue (danger red, warning amber, VIP/history golds, time-changed purple are all taken); never color-only (binoculars icon + literal "KEOI" + category text everywhere). Icon bi-binoculars-fill (bi-eye is taken by the row View link @1594); tiny bi-eye-fill only on ~8px timeline chip markers. No pulse animation — danger rows keep the attention hierarchy. Status sub-pill CSS keys must equal stored values: keoi-status-needs_attention / keoi-status-being_monitored / keoi-status-backup_arranged (dot via ::before plus the status word).

5a. NEW dispatching/templates/dispatching/includes/_keoi_badge.html
Reusable badge. Default: a real <button class="keoi-badge"> with data-bs-toggle="modal" data-bs-target="#keoiModal" and data-keoi-* attrs (id, category, category-label, status, status-label, description, created/updated by+at display strings, leg-label "Customer · 9:00 AM · Res #123"), title= hover preview (truncatechars:140), aria-label ("Keep eye on it: {category}, status {status}. Opens details."). Content: icon + KEOI + category + status sub-pill. With readonly=True (Legs List): a <span> with title= only. Django auto-escapes all attrs; newlines survive attribute encoding and dataset reads.

5b. NEW dispatching/templates/dispatching/includes/_keoi_modal.html (+ self-contained <script>)
Singleton #keoiModal included once in legs_filter.html after the Leg History modal (~line 4807). Populated from the trigger's data-* attrs via show.bs.modal relatedTarget — no per-leg hidden divs (fields are small and already prefetched), no fetch on open. Contains: teal header; form with category <select required> (8 options, disabled placeholder; values = model values), description <textarea rows="4" maxlength="2000" required> with helper text ("Required — a dispatcher must understand the risk without opening the reservation."), operational-status as three btn-check radios (default Needs Attention), meta block ("Flagged by X · date / Updated by Y · date", edit mode only), inline role="alert" error region; footer: Remove button {% if perms.reservations.remove_keoi %} (left, me-auto), Cancel, Save. JS: show-handler (edit vs create mode from data-keoi-id), shown.bs.modal → focus category, client-side validation (category set, description.trim() nonempty) before fetch to keoi_save; Remove uses native prompt() for the required reason → keoi_remove. Define a local keoiToast() — copy of showToast @3770-3789 (that one is scoped inside a click handler; the frrToast shim @3223 already proves the trap). On success: hide modal, toast, then renderKeoi(legId, keoiOrNull) updates in place — no reload (matches the status/notes inline-update precedent; DND's reload is for layout restructuring, which KEOI doesn't cause). renderKeoi must: rebuild badges in every .keoi-slot[data-leg-id=…] via createElement + textContent (never innerHTML with user text), toggle .leg-keoi on #leg-row-{id} and #leg-card-{id}'s card, toggle the add-button's d-none, refresh data-*/title/aria-label from the response.

5c. Main board legs_filter.html
CSS block in extra_css after ~line 521: row tint tr.leg-keoi:not(.table-danger):not(.table-warning):not(.leg-time-changed) > td { background-color: rgba(13,148,136,.07) !important; } (yields like VIP does) + right-edge rail that never yields tr.leg-keoi > td:last-child { box-shadow: inset -4px 0 0 0 #0d9488; } (left edge belongs to flight flags/VIP); .card.leg-keoi same rail; .keoi-badge styles (min-height 28px, :focus-visible ring); status sub-pill styles; .keoi-add-btn; .tl-keoi-flag; .timeline-slot.has-keoi.
Desktop row @1080: append {% if leg.active_keoi %}leg-keoi{% endif %} to the class list. Badge slot in the Customer cell between ~1095/1096 — always render the wrapper <div class="keoi-slot mt-1" data-leg-id="{{ leg.id }}">…</div> (empty when no flag) so JS can inject without reload.
Add-button in the actions cluster after the View link (~1595): bi-binoculars-fill + "KEOI", with {% if leg.active_keoi %} d-none{% endif %} (not {% if not %}) so JS can toggle.
Mobile card: class on inner .card @1624; same keoi-slot after the header row (1638); add-button near "View Leg History" (1990).
Dashboard timeline: driver_timeline.html slot @71 add has-keoi class; data-keoi-* attrs after @88; tl-keoi-flag marker in label flow @90. Popup builder @4359-4373: teal KEOI banner + escaped pre-wrap description block — add a local escapeHtml helper (none in that IIFE's scope). Slot data: _slot_keoi(leg) helper (see 5e); non-hover affordance is the existing slot onclick → scroll to the fully-badged row.
5d. Schedule board schedule_board.html (both inhouse and affiliate views — staff-facing)
Minimal: chips get has-keoi outline class (@649 unassigned, @790 driver), four data-keoi-* attrs (@682/@823), tl-keoi-flag glyph in the label flow before the refund flag (@683/@824, flex-shrink:0 so never shed); showJobPopup @990: teal banner after the refund banner (@995-997, uses existing escapeHtml @984) + KEOI description prepended to the notes array (@1044) with pre-wrap class; legend entry before the drag hint (~@852). Touch affordance = existing chip click-through to the dashboard row. CSS near @60.

5e. View plumbing dispatching/views.py
_slot_keoi(leg) helper next to _slot_notes (2585) returning {'keoi_category','keoi_category_label','keoi_status_label','keoi_desc'} from leg.active_keoi (empty strings when none). Do not fold into _slot_notes — keeps the KEOI marker distinct from the folded-corner note marker. Merge at the schedule-board call sites (1222-1223 driver slots, 1376 unassigned) and in the dashboard timeline slot loop (688-699; hoist _leg_by_id above ~680, reuse at ~756).
index: read keoi_filter = request.GET.get("keoi") (133); after trip-type filtering (214-219): keoi_count = sum(1 for l in _all_day_legs if l.active_keoi) and if keoi_filter == "active": legs = [l for l in legs if l.active_keoi] (Python-side, matching existing filters; count from the already-evaluated day list — zero extra queries); add keoi_filter, keoi_count to context.
5f. Legs List legs_list.html — read-only badge (include)
KEOI's highest value is advance flagging; Legs List is the only future-dates surface. Row class @352, {% include "..._keoi_badge.html" with keoi=leg.active_keoi leg=leg readonly=True %} after the VIP badge (~360-362), duplicate the small CSS block into its extra_css (inline-CSS-per-template is the convention). Prefetch already covered via get_filtered_legs_queryset (§4e). No filter pill here in v1.

5g. Accessibility / mobile
Real <button> badges with aria-labels, aria-hidden icons, visible focus ring, ≥28px tap targets; Bootstrap modal handles focus trap/return; no hover-only info anywhere (badge text carries category+status; description is one tap away; title=/popups are desktop enhancements). Dim-completed: no conflict — auto-close means a freshly rendered board never shows a badge on a dimmed row; mid-session completion dims badge with the row (correct; next render drops it).

6. Completion Lifecycle
Close: leg status transitions into completed → active flag closed leg_completed; into cancelled (board pick or process_refund) → closed leg_cancelled. Enforced by the signal pair (§4b) — catches every pathway (§1). History preserved as closed rows + AuditLog.
Reopen (chosen rule: auto-reactivate): leg leaves a terminal state (manual status pick on board or driver portal — the only reopen paths, both .save()) → most recent auto-closed flag reactivates with an audit event. Rationale: failure asymmetry — a wrongly visible flag costs a glance; a wrongly hidden one is the missed pickup KEOI exists to prevent; a leg leaving completed means the trip is NOT done, which is exactly when the watch matters again. Matches the codebase's safety-over-convenience bias (forced re-acceptance on driver change, models.py:1630-1635). admin_removed flags never reactivate (a privileged, reasoned human decision that status churn must not undo).
Never closes on: view/acknowledge, operational-status change (incl. Backup Arranged), driver/vehicle reassignment (driver-change auto-reset goes to in-progress, non-terminal), leg edits, parent-reservation edits, sibling-leg completion (reservation auto-complete touches only Reservation.status), board/browser refresh, date change, bulk driver reset (F — verified terminal-safe; add the one-line comment).
Flagging a terminal leg: rejected 400 (would auto-close instantly).
Reservation cancelled without leg cancellation: flag stays active but all boards exclude reservation__status='cancelled' legs (views.py:147, :920, utils.py:61) — nothing stale visible; closes when the leg itself cancels.
Deletion: no user-facing leg delete exists; CASCADE removes rows, AuditLog persists. Cloning/moving: no clone/move code exists — nothing to handle. Date move: boards are date-scoped; flag simply renders on the new date (tested).
Concurrency: last-write-wins + complete audit trail (no version fields exist anywhere; don't introduce locking for a human-speed workflow); double-create resolved by the DB constraint + IntegrityError-retry-as-edit; double-reactivate by constraint + swallowed IntegrityError. updated_at echoed in responses so optimistic checks are a future UI-only change.
7. Filtering
Single GET param keoi=active on the dashboard, one pill "KEOI (n)" appended to the Quick Filter Pills (@737-777, new pill before ~@775): outline-teal when off, solid-teal with ✕ when on. No per-status sub-filters (a day carries a handful of flags; the badge already shows status on every hit — revisit if volume grows). Count basis = whole day (_all_day_legs), matching vehicle_type_counts semantics. Preservation tax: every existing pill href must carry {% if keoi_filter %}&keoi={{ keoi_filter }}{% endif %} — hrefs at ~@742, 746, 750, 754, 762, 769 — and the new pill preserves date/driver/trip_type/vehicle (follow the existing manual-querystring pattern exactly; highlight stays transient). Combines correctly with existing filters because it's applied after them in the same Python pipeline.

8. Migration Plan
reservations/migrations/0122_legkeoi.py (autogenerated): CreateModel + 1 index + 2 constraints + auto-created remove_keoi permission (post_migrate). Purely additive — zero AlterField on Leg, zero HistoricalLeg churn, no locks on hot tables. Existing legs are simply unflagged.
Deploy order: migrate, then release code (old code never references the table; new code without it would 500). No compatibility shims needed; existing API clients unaffected (no existing serializers change).
Rollback: manage.py migrate reservations 0121 drops the table (flag rows lost — acceptable for feature rollback); AuditLog rows survive as the permanent record.
Portability: Django emits the partial unique index on both Postgres (prod) and SQLite (test runner); uniq_active_draft_per_date already proves it in this project.
Grant remove_keoi to intended non-superuser removers via admin post-deploy (superusers pass implicitly).
9. Testing Plan
New dispatching/tests_keoi.py, fixture mixin copied structurally from tests_sandbox.py:36-104: _make_driver, _grant_remove (Permission remove_keoi + user re-fetch for perm cache), setUpTestData with vehicle/route/rate/customer/legs inline, _leg(), _keoi(), _post_json(). Users: dispatcher (is_staff), plain (non-staff), remover (staff + perm), manager (superuser), a driver User. Run: ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_keoi (system python).

KeoiCreateEditTests (~10): create success (row + payload + AuditLog keoi/created); missing/whitespace/overlong description → 400; invalid category → 400; create on completed/cancelled leg → 400; non-staff → 403; upsert edits existing flag (no second row; keoi_category+keoi_description audit rows with old/new); unchanged resubmit writes no audit rows.
KeoiStatusTests (~4): status change via keoi_save + audit row; invalid value 400; Backup Arranged leaves closed_at NULL and flag visible (assert explicitly — the spec's core rule); no active flag → behaves as create (upsert) or 400 per terminal guard.
KeoiAutoCloseTests (~8, through real endpoints): board complete (A) closes with leg_completed, audit actor = dispatcher; driver-portal complete (B) closes, actor = driver's User; bulk (D) closes across selected legs; refund partial-cancellation (E) closes with leg_cancelled; survival set — driver reassign (status auto-reset), pickup time/date edit, parent-reservation edit, sibling-leg completion: all leave the flag active; unit test close_active_keoi with no request → AuditLog user=None, username='guest'.
KeoiReactivateTests (~4): completed→in-progress via endpoint A reopens + keoi_reactivated audit; cancelled→active reopens; admin-removed does NOT reactivate; reactivate no-ops when another active flag exists.
KeoiRemoveTests (~5): remover succeeds (admin_removed, removal_reason stored, reason in audit notes); superuser succeeds without explicit perm; staff without perm → 403; blank reason → 400; removed flag stays closed through later status churn.
KeoiBoardTests (~6): dashboard assertContains category + description for an active flag; closed flag absent; keoi=active filter returns only flagged legs and combines with driver/trip_type params; pill count correct; leg moved to another date renders under the new date; <script> in description renders escaped (assertContains(resp, "&lt;script&gt;") / assertNotContains(resp, "<script>")); query-count guard — evaluate get_filtered_legs_queryset with 3 flagged legs under CaptureQueriesContext, assert reading leg.active_keoi for all adds zero queries (first such test in the repo; scope to the queryset so it stays stable).
KeoiConstraintTests (~3): second active create for same leg raises IntegrityError; multiple closed rows + zero-or-one active coexist; keoi_save against a pre-existing active flag takes the edit path.
~30 tests total. Also run the existing suites that touch the changed surfaces: dispatching.tests_affiliate_board, tests_day_setup, tests_flight_change_safety, drivers tests (signal pair fires in their flows).

10. Implementation Sequence (dependency-ordered, for Opus 4.8)
Model + migration — reservations/models.py (LegKeoi after ~3204; active_keoi property near ~1341); python manage.py makemigrations reservations → 0122; migrate; verify with python manage.py check. Depends on: nothing. Validate: shell-create a flag, confirm the partial unique constraint rejects a second active row.
Service module — new reservations/keoi.py (close_active_keoi, reactivate_keoi). Depends: 1.
Signal pair — append to reservations/signals.py after ~835 (dedicated _keoi_old_status); one-line KEOI comment at dispatching/views.py:11712-11714. Depends: 2. Validate: shell — complete a flagged leg via .save(update_fields=['status']), confirm close + AuditLog row; revert status, confirm reactivation.
Endpoints + URLs — new dispatching/keoi_views.py (keoi_save/keoi_remove/keoi_history); wire in dispatching/urls.py (~118). Depends: 1-3. Validate: python manage.py test dispatching.tests_keoi once step 8's create/status/remove tests exist, or curl-style shell client checks.
Query integration — Prefetch in views.py:166-169, views.py:928-933, utils.py:52-58. Depends: 1.
Main board frontend — new _keoi_badge.html + _keoi_modal.html; legs_filter.html edits (CSS 521; row @1080; slot ~1095/1096; add-btn ~1595; mobile @1624/1638/1990; modal include ~4807; timeline popup ~4360/4373 + local escapeHtml); driver_timeline.html (@71/@88/@90); views.py _slot_keoi helper + dashboard slot loop merge (~688-699, hoist _leg_by_id). Depends: 4, 5. Validate: load dashboard with a flagged leg — badge, tint, right rail, modal open/prefill/save/remove all work; check mobile viewport.
Filter pill + schedule board + legs list — index filter/count (133, ~214-219, context); pill + href preservation (@737-777); schedule_board.html chips/popup/legend + call-site merges (1222, ~1376); legs_list.html read-only badge. Depends: 5, 6. Validate: filter combines with driver/trip-type; schedule board hover popup shows KEOI; affiliate view unaffected structurally.
Tests — dispatching/tests_keoi.py (all classes in §9). Depends: 1-7 (write create/status tests as early as step 4 to validate incrementally). Run: ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_keoi dispatching.tests_affiliate_board dispatching.tests_day_setup dispatching.tests_flight_change_safety drivers.
Full regression + manual smoke — full manage.py test; manual: flag → reassign driver → flag survives; complete via board → auto-closes; reopen → reactivates; superuser remove with reason; filter pill; Legs List future date shows badge.
11. Risks and Open Questions
Risks (mitigated, watch during implementation):

Line-anchor drift in the two huge templates (5338/1260 lines) — anchor on quoted code, not numbers.
The signal pair adds one SELECT per status-touching Leg save — same cost as the existing pair; acceptable, but keep the update_fields skip-guard.
Querystring-preservation misses (a pill href without &keoi=) silently drop the filter — step 7 must touch all six hrefs; test #3 in KeoiBoardTests covers the combination.
showToast scoping trap — the modal ships its own keoiToast; don't call the nested one.
Timeline chips/popup and pill count are stale after a no-reload create/edit until next load — same staleness class as inline notes edits today; acceptable, state it in the PR.
First CaptureQueriesContext test in the repo — keep it scoped to the queryset, not a full view render, to avoid brittleness.
Resolved by user: KEOI is staff-only; driver portal untouched.

Open (none blocking): who besides superusers gets remove_keoi is a post-deploy admin decision; whether to also close KEOI when a reservation (not leg) is cancelled without leg cancellation was deliberately left as "stays active but invisible, closes when the leg cancels" — revisit only if a stale-flag report ever surfaces.

12. Acceptance Criteria
 Staff can flag a leg with a category + required description; empty/whitespace-only descriptions and missing/invalid categories are rejected server-side (400) and client-side.
 Flagging a completed/cancelled leg is rejected with a clear error.
 Flagged legs stand out on: dashboard desktop row, mobile card, dashboard timeline slot, schedule-board chips (inhouse + affiliate), Legs List — with icon + "KEOI" text + category (never color-only).
 Description is readable without opening the reservation: hover preview on desktop, tap-to-modal everywhere; renders escaped with line breaks preserved.
 Operational status (Needs Attention / Being Monitored / Backup Arranged) is editable and visible; setting Backup Arranged does not hide the flag.
 The flag survives: driver reassignment, vehicle reassignment, leg edits, parent-reservation edits, sibling-leg completion, board/browser refresh, filter changes, pickup date/time moves.
 Completing the flagged leg via ANY pathway (board, driver portal, bulk, refund-cancellation) auto-closes it; cancellation closes it with its own reason.
 Reopening a completed/cancelled leg auto-reactivates the flag; admin-removed flags never reactivate.
 Only users with reservations.remove_keoi (incl. superusers) can remove a flag, a reason is mandatory, and the removal is audited; other staff get 403.
 Every KEOI event (create, category/description/status edits, auto-close, reactivate, remove) produces exactly one AuditLog row with correct actor attribution (driver completions attribute to the driver; system events fall back to "guest").
 ?keoi=active pill with count filters the dashboard and composes with date/driver/trip-type/vehicle filters without resetting them.
 Board queries add zero per-leg queries for KEOI (prefetch verified by test).
 Migration is additive; existing legs unflagged; migrate reservations 0121 cleanly rolls back.
 Driver portal is completely unchanged.
 All new tests pass plus existing board/flight-change/driver suites (ENABLE_DEBUG_TOOLBAR