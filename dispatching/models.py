from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


# Module-level cache for the singleton — avoids repeated DB queries
# during a single scheduling operation (cleared after save or manually).
_settings_cache = None


class SchedulerSettings(models.Model):
    """Singleton model storing all tunable scheduler scoring parameters.

    One row in the DB. Use SchedulerSettings.get_settings() to retrieve it
    (creates with defaults on first access, caches in memory).
    """

    class Meta:
        verbose_name = "Scheduler Settings"
        verbose_name_plural = "Scheduler Settings"

    # ── Buffer Quality (Auto-Assign) ──────────────────────────────
    buffer_perfect = models.IntegerField(default=120, help_text="20-30 min buffer (ideal)")
    buffer_sweet_spot = models.IntegerField(default=100, help_text="30-60 min buffer (comfortable)")
    buffer_good = models.IntegerField(default=80, help_text="60-120 min buffer")
    buffer_tight = models.IntegerField(default=70, help_text="10-20 min buffer")
    buffer_loose = models.IntegerField(default=50, help_text="120+ min buffer")
    buffer_risky = models.IntegerField(default=30, help_text="Under 10 min buffer")

    # ── Buffer Quality (Schedule Builder) ─────────────────────────
    sb_buffer_perfect = models.IntegerField(default=35, help_text="20-30 min buffer (builder, ideal)")
    sb_buffer_sweet_spot = models.IntegerField(default=30, help_text="30-60 min buffer (builder)")
    sb_buffer_good = models.IntegerField(default=20, help_text="60-120 min buffer (builder)")
    sb_buffer_first_job = models.IntegerField(default=25, help_text="First job, no prior (builder)")
    sb_buffer_tight = models.IntegerField(default=15, help_text="10-20 min buffer (builder)")

    # ── Vehicle Tier Match ────────────────────────────────────────
    tier_exact = models.IntegerField(default=60, help_text="Exact tier match")
    tier_1_down = models.IntegerField(default=40, help_text="1 tier below driver's vehicle")
    tier_2_down = models.IntegerField(default=25, help_text="2 tiers below")
    tier_3_down = models.IntegerField(default=15, help_text="3 tiers below")
    tier_4_down = models.IntegerField(default=10, help_text="4 tiers below")

    # ── Scarcity ──────────────────────────────────────────────────
    scarcity_1 = models.IntegerField(default=80, help_text="Only 1 driver can do this job")
    scarcity_2 = models.IntegerField(default=50, help_text="2 eligible drivers")
    scarcity_3 = models.IntegerField(default=30, help_text="3 eligible drivers")
    scarcity_4 = models.IntegerField(default=15, help_text="4 eligible drivers")

    # ── Location Proximity (Auto-Assign) ──────────────────────────
    loc_same_area = models.IntegerField(default=50, help_text="Last dropoff = next pickup area")
    loc_close = models.IntegerField(default=30, help_text="Reposition drive <= 15 min")
    loc_first_job = models.IntegerField(default=40, help_text="Driver has no prior jobs")

    # ── Location Proximity (Schedule Builder) ─────────────────────
    sb_loc_same_area = models.IntegerField(default=35, help_text="Same area bonus (builder)")

    # ── Schedule Flow (Auto-Assign) ───────────────────────────────
    flow_3rd_arrival = models.IntegerField(default=-40, help_text="3rd+ consecutive arrival penalty")
    flow_2nd_arrival = models.IntegerField(default=-15, help_text="2nd consecutive arrival penalty")
    flow_break_bonus = models.IntegerField(default=30, help_text="Return/cruise breaking arrival streak")

    # ── Schedule Flow (Schedule Builder) ──────────────────────────
    sb_flow_3rd_arrival = models.IntegerField(default=-35, help_text="3rd+ arrival penalty (builder)")
    sb_flow_2nd_arrival = models.IntegerField(default=-10, help_text="2nd arrival penalty (builder)")
    sb_flow_break_bonus = models.IntegerField(default=25, help_text="Flow break bonus (builder)")

    # ── In-House Retention ────────────────────────────────────────
    retention_bonus = models.IntegerField(default=25, help_text="Bonus for return/cruise in auto-assign")

    # ── Chain Awareness ───────────────────────────────────────────
    chain_3_plus = models.IntegerField(default=45, help_text="3+ follow-up jobs near dropoff")
    chain_2 = models.IntegerField(default=35, help_text="2 follow-up jobs")
    chain_1 = models.IntegerField(default=20, help_text="1 follow-up job")
    chain_drive_threshold = models.IntegerField(default=30, help_text="Max drive minutes to count as chainable")
    chain_time_min = models.IntegerField(default=10, help_text="Min gap minutes for chain")
    chain_time_max = models.IntegerField(default=180, help_text="Max gap minutes for chain")

    # ── Vehicle Reservation ─────────────────────────────────────
    reserve_penalty = models.IntegerField(default=-60, help_text="Penalty when rare-vehicle driver takes mismatched job while matching jobs wait")
    reserve_max_scarcity = models.IntegerField(default=2, help_text="Max eligible drivers for a job to count as 'needs saving'")

    # ── Load Balance ──────────────────────────────────────────────
    load_balance_multiplier = models.IntegerField(default=10, help_text="Base multiplier for load balance penalty")
    load_balance_exponent = models.FloatField(default=1.5, help_text="Exponent for load balance: multiplier * (n ^ exponent)")

    # ── Idle Gap Penalty ──────────────────────────────────────────
    idle_gap_threshold = models.IntegerField(default=120, help_text="Minutes of gap before idle penalty applies")
    idle_gap_penalty_per_min = models.IntegerField(default=2, help_text="Penalty per minute over idle threshold")

    # ── Schedule Span Penalty ─────────────────────────────────────
    span_threshold_hours = models.IntegerField(default=13, help_text="Max shift span hours before penalty")
    span_penalty_per_hour = models.IntegerField(default=30, help_text="Penalty per hour over span threshold")

    # ── Backward Chain ────────────────────────────────────────────
    backward_chain_bonus = models.IntegerField(default=40, help_text="Bonus when driver's last dropoff chains into this pickup")

    # ── Cluster / Shift Coherence ─────────────────────────────────
    shift_coherence_bonus = models.IntegerField(default=50, help_text="Bonus when job is in driver's assigned time cluster")
    cluster_gap_minutes = models.IntegerField(default=120, help_text="Time gap in minutes to split into new cluster")

    # ── Time Scarcity ─────────────────────────────────────────────
    time_scarcity_bonus = models.IntegerField(default=30, help_text="Bonus for legs in time-scarce hours (demand > supply)")

    # ── Global ────────────────────────────────────────────────────
    min_turn_buffer = models.IntegerField(
        default=5,
        help_text="Default spare minutes the ENGINE must leave between two jobs, on top of "
                  "the drive between them. 0 = aggressive (a driver may be due at his next "
                  "pickup the same instant he clears the last one). The builder and "
                  "auto-assign can override this per run, and a per-driver number "
                  "(Driver.default_min_turn_buffer) beats both. Does not affect a "
                  "dispatcher's own manual assignments.")
    # DEPRECATED 2026-08-09: never read by check_feasibility (it was accepted and ignored as
    # 'superseded by context turnaround'), so this knob has done nothing for a long time.
    # min_turn_buffer above is the live one. Kept only so existing rows/migrations don't
    # break; safe to drop once nothing references it.
    inter_job_buffer = models.IntegerField(default=5, help_text="DEPRECATED — has no effect. Use 'Min turn buffer'.")
    arrival_grace_minutes = models.IntegerField(default=15, help_text="Airport arrival grace: flight lands but pax still deplaning/bags, so driver can arrive this many min after flight time")

    # ── Builder Extras ────────────────────────────────────────────
    base_score = models.IntegerField(default=50, help_text="Starting score for each candidate leg")
    trip_pref_match = models.IntegerField(default=40, help_text="Bonus when leg matches preferred trip type")
    trip_pref_mismatch = models.IntegerField(default=-10, help_text="Penalty when leg doesn't match preference")
    revenue_divisor = models.IntegerField(default=10, help_text="Revenue / this = bonus points")
    revenue_cap = models.IntegerField(default=20, help_text="Max revenue bonus points")

    # ── Swap Optimizer ─────────────────────────────────────────────
    swap_max_depth = models.IntegerField(default=5, help_text="Max chain length for swap search")
    swap_time_limit_ms = models.IntegerField(default=5000, help_text="Time budget in ms for swap search")
    swap_max_iterations = models.IntegerField(default=5000, help_text="Max states to explore per search")
    swap_depth_penalty = models.IntegerField(default=150, help_text="Score penalty per swap depth level")
    swap_buffer_weight = models.IntegerField(default=2, help_text="Score multiplier for min buffer minutes")
    swap_revenue_weight = models.IntegerField(default=10, help_text="Score weight for normalized revenue (revenue/divisor, capped)")
    swap_tier_bonus = models.IntegerField(default=20, help_text="Score bonus for exact vehicle tier match in swap")

    # ── Driver Pay Management ─────────────────────────────────────
    driver_pay_overdue_days = models.IntegerField(default=14, help_text="Days after which unpaid legs are considered overdue")

    # ── Founder Brain: value-aware assign + evict-to-farm ─────────
    auto_assign_value_weight = models.IntegerField(default=1, help_text="Weight of the founder leg-value scoring term (booked class › trip type › revenue › pax); one class step ≈ 10×weight points; 0 disables")
    displacement_min_value_gain = models.IntegerField(default=500, help_text="Evict-to-farm pass: min leg_value(residual) − leg_value(evicted arrival) to displace (1000 ≈ one trip-type step, 10000 ≈ one booked-class step)")
    max_displacements_per_run = models.IntegerField(default=10, help_text="Evict-to-farm pass: max evictions per auto-assign run")

    # ── Rest Advisor (overnight rest gap) ────────────────────────
    # Stored in MINUTES (not hours) so the integer-only settings save path round-trips
    # the founder's 8.5h pick exactly (510). 0 disables rest scoring AND the advisory cards.
    rest_min_gap_minutes = models.IntegerField(default=510, help_text="Min overnight rest: minutes between a driver's last drop-off (prev day) and his first pickup (next day). 510=8.5h. 0 disables rest scoring + advisories.")
    rest_penalty_per_hour = models.IntegerField(default=40, help_text="Score penalty per hour of overnight-rest deficit, charged ONLY when a leg would become a driver's first pickup of the day (soft — never blocks coverage).")

    # ── Shared-Car Handoff (scheduling redesign, Build 1) ─────────
    vehicle_share_pad_min = models.IntegerField(
        default=120,
        help_text="Shared-car handoff pad: when two drivers hold ONE physical unit "
                  "for the day, an insert for one must clear the partner's jobs by "
                  "this many minutes each side. 120 is the empirical anchor from "
                  "measured handoffs — the retired constant 60 sat near the 9th "
                  "percentile of real pickup-to-pickup handoff gaps (optimistic "
                  "nine times in ten). Read by the manual-assign warning and the "
                  "second-shift/mint engine — NOT by the build engine's own "
                  "farm-out gate, which has its own dial below.")
    engine_share_pad_min = models.IntegerField(
        default=65,
        help_text="Shared-car pad for the BUILD ENGINE's farm-out gate only "
                  "(car_share.sharers_conflict) — separate from vehicle_share_pad_"
                  "min on purpose (2026-08-24). The engine measures this pad from "
                  "the outgoing driver's estimated CLEAR time to the partner's "
                  "pickup; the warning/mint pad above measures pickup-to-pickup. "
                  "At the shared 120 the engine was rejecting, and therefore "
                  "farming out, real handoffs the founder confirmed ran fine — one "
                  "as tight as a 48-min clear-to-pickup gap. Tune this one alone; "
                  "it never affects the warning or the second-shift proposals.")

    # ── Manual-Assign Warnings (scheduling redesign, Build 1) ─────
    manual_assign_warnings = models.BooleanField(
        default=True,
        help_text="Warn-only validation on the manual assign path (board drag-drop "
                  "and driver dropdowns): turn-slack and shared-car checks returned "
                  "as dismissible warnings on the response. NEVER blocks an "
                  "assignment. Off (0) skips the computation entirely.")

    # ── Split Shifts & Handoffs (scheduling redesign, Build 2) ────
    share_split_hour = models.IntegerField(
        default=16,
        help_text="Default AM/PM cut hour for a planned shared car when no better "
                  "cut is known (16 = the measured modal handoff hour). A standby "
                  "second-shift proposal derives its own cut from the actual "
                  "handoff; this is the fallback for hand-made shares.")
    handoff_gap_green_pct = models.IntegerField(
        default=100,
        help_text="Handoff GREEN bar as a percent of the central wash-fuel-base "
                  "zone chain (drop zone to next-pickup zone). 100 = the 03-model "
                  "rule exactly; lower is more permissive. Structured chain tables "
                  "live in dispatching/handoff_chain.py.")
    handoff_gap_amber_floor_pct = models.IntegerField(
        default=100,
        help_text="Handoff AMBER floor as a percent of the LOW zone chain — below "
                  "this (and below the skip-wash fast path) a handoff is RED: "
                  "shown, never suggested. 100 = the 03-model rule exactly.")
    mint_min_jobs_soft = models.IntegerField(
        default=2,
        help_text="Soft minimum jobs on a proposed standby second shift (D6). "
                  "NEVER a hard floor — a thinner proposal still shows, flagged "
                  "'thin — worth it?' with the dollars it saves.")
    span_exception_max_hours = models.FloatField(
        default=15.0,
        help_text="Hard ceiling for the priced crunch exception: a per-driver day "
                  "may be proposed past the 13.5h soft cap ONLY up to this many "
                  "hours, priced and rendered as a choice — never a default.")

    # ── Day-Builder (scheduling redesign, Build 3b — Ticket A/B/D) ────────
    # Pass A (the roster-size ladder) was CUT by the Ticket-C surrogate-noise
    # gate (analysis/16, 2026-08-25): between-size differences don't clear
    # within-size jitter. The builder optimizes pairing and splits at the
    # dispatcher's chosen headcount, so there are no pass_a_* knobs.
    opt_enabled = models.BooleanField(
        default=False,
        help_text="Master switch for the Day-Builder ('Build a plan' in Day "
                  "Setup). Ships OFF — the feature exists but stays invisible "
                  "until the founder turns it on (05 §7 acceptance).")
    opt_epsilon_farmouts = models.IntegerField(
        default=0,
        help_text="The coverage dial: allow up to this many MORE farm-outs than "
                  "the same-date suggest+build baseline to buy a better day "
                  "(0-3). At 0 the builder may never worsen coverage. Applies "
                  "to the farm-out count ONLY — it can never buy a wall "
                  "(conflicts, hours, rest, shared-car rules).")
    pass_b_max_swaps = models.IntegerField(
        default=6,
        help_text="Day-Builder: max targeted pairing swaps considered per run. "
                  "A swap is considered only when it changes a tier constraint "
                  "or reshapes a shared car.")
    pass_b_max_evals = models.IntegerField(
        default=10,
        help_text="Day-Builder: hard budget of full pipeline evaluations per "
                  "run (each costs ~6-15s on a real day).")
    opt_runtime_budget_s = models.IntegerField(
        default=240,
        help_text="Day-Builder: wall-clock ceiling in seconds. The job stops "
                  "at the ceiling and returns its best-so-far, flagged "
                  "'budget exhausted' — never silently truncated.")
    opt_stale_after_min = models.IntegerField(
        default=120,
        help_text="Day-Builder: a computed plan older than this many minutes "
                  "renders greyed with a 're-build' prompt (bookings move).")
    opt_w_span = models.FloatField(
        default=1.0,
        help_text="Day-Builder quality weight [assumed]: sum of per-driver "
                  "effective hours over the 13.5h target. Tie-break only — "
                  "never outranks coverage or farm cost, never moves a wall.")
    opt_w_fairness = models.FloatField(
        default=1.0,
        help_text="Day-Builder quality weight [assumed]: stdev of legs per "
                  "working driver. Tie-break only.")
    opt_w_handoff = models.FloatField(
        default=2.0,
        help_text="Day-Builder quality weight [assumed]: count of AMBER "
                  "handoff bands in the plan (RED is a wall, never scored). "
                  "Tie-break only.")
    opt_w_gaps = models.FloatField(
        default=0.5,
        help_text="Day-Builder quality weight [assumed]: hours of internal "
                  "idle gaps above the idle-gap threshold. Tie-break only.")

    # ── Greedy Type Ordering (lower = processed earlier within each hour) ──
    type_priority_return = models.IntegerField(default=0, help_text="Ordering priority for returns/departures within each hour bucket")
    type_priority_cruise = models.IntegerField(default=1, help_text="Ordering priority for cruise legs within each hour bucket")
    type_priority_other = models.IntegerField(default=2, help_text="Ordering priority for 'other' legs within each hour bucket (also the fallback for unknown types)")
    type_priority_arrival = models.IntegerField(default=3, help_text="Ordering priority for arrivals within each hour bucket (last: arrivals are the farm-out currency)")

    @classmethod
    def get_settings(cls):
        """Return the singleton settings row, cached in memory.
        Creates with defaults on first access."""
        global _settings_cache
        if _settings_cache is not None:
            return _settings_cache
        obj, _ = cls.objects.get_or_create(pk=1)
        _settings_cache = obj
        return obj

    @classmethod
    def clear_cache(cls):
        """Clear the in-memory cache (call after saving settings)."""
        global _settings_cache
        _settings_cache = None

    def reset_to_defaults(self):
        """Reset all fields to their model-defined defaults."""
        for field in self._meta.get_fields():
            if hasattr(field, 'default') and field.default is not models.NOT_PROVIDED:
                setattr(self, field.name, field.default)
        self.save()
        SchedulerSettings.clear_cache()

    def to_dict(self):
        """Return all tunable fields as a JSON-serializable dict."""
        skip = {'id'}
        result = {}
        for field in self._meta.get_fields():
            if field.name in skip or not hasattr(field, 'attname'):
                continue
            result[field.name] = getattr(self, field.name)
        return result

    def get_defaults(self):
        """Return dict of field_name -> default value."""
        skip = {'id'}
        result = {}
        for field in self._meta.get_fields():
            if field.name in skip or not hasattr(field, 'default'):
                continue
            if field.default is not models.NOT_PROVIDED:
                result[field.name] = field.default
        return result

    def __str__(self):
        return "Scheduler Settings"


class FlightRefreshTask(models.Model):
    """
    Progress/result state for one bulk flight-refresh run.

    This lived in the Django cache, but the cache is LocMemCache (no REDIS_URL)
    and gunicorn runs 3 workers: the POST that started the refresh wrote the
    state into ONE worker's private memory, while the status poll round-robined
    across all three. Two out of three polls hit a worker that had never heard
    of the task and 404'd "Refresh task not found", killing the poll before the
    review summary could ever be shown. The DB is the only store all workers
    share, so the state lives here.
    """

    task_id = models.CharField(max_length=64, unique=True, db_index=True)
    # DjangoJSONEncoder, not the stock one: the cache used to pickle this blob,
    # so anything serialized. A JSONField calls json.dumps, and a stray
    # datetime/Decimal slipping into the summary would otherwise crash the whole
    # refresh at the final write.
    state = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Flight Refresh Task"
        verbose_name_plural = "Flight Refresh Tasks"

    def __str__(self):
        return f"{self.task_id} ({self.state.get('status', 'unknown')})"


class ChauffeurExceptionDismissal(models.Model):
    """A superuser marked one "Worth a conversation" entry handled.

    Episode semantics (SOP-003): while the same (driver, rule) keeps firing, the entry
    stays out of the active list and shows under the collapsed "Handled" line. When a
    KPI render sees the rule no longer firing, ``cleared_at`` is set — the dismissal is
    spent — so the same problem starting again later surfaces as a fresh conversation.
    Undo deletes the row outright.

    Spending is judged ONLY on renders of the window the dismissal was made on
    (``window``), and only while the driver is actually in the rendered roster. Rules
    have window-scaled floors, so a glance at another window must not clear a dismissal
    whose condition still holds where it was dismissed — and a deactivated driver or an
    empty roster is not an ended episode, just an unevaluated one.
    """

    driver = models.ForeignKey("drivers.Driver", on_delete=models.CASCADE,
                               related_name="exception_dismissals")
    #: One of load_insights.EXCEPTION_RULES — validated at the endpoint, not here,
    #: so a rule renamed in code doesn't strand old rows at migration time.
    rule = models.CharField(max_length=40)
    #: The window key ("7"/"30"/"90") the superuser was viewing when they dismissed.
    window = models.CharField(max_length=3, default="30")
    dismissed_by = models.ForeignKey("auth.User", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=200, blank=True, default="")
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["driver", "rule"],
                condition=models.Q(cleared_at=None),
                name="uniq_active_dismissal_per_driver_rule",
            ),
        ]

    def __str__(self):
        state = "cleared" if self.cleared_at else "active"
        return f"{self.driver} · {self.rule} ({state})"


class DayPlan(models.Model):
    """One Day-Builder job + its latest result, per service date (Build 3b, Ticket D).

    The claim row: "Build a plan" claims the date's row by a race-safe UPDATE
    (status -> running) so a double-click cannot double-run, then the work runs
    in a `_run_in_background` daemon thread (the existing pattern; the wrapper
    closes the thread's DB connection on exit — the 2026-07-18 standing rule).
    The panel polls status and renders `result_json` with the computed-at stamp.

    STRICTLY the job ledger: the plan itself never writes a Leg or a
    DriverVehicleAssignment row — v1 is propose-only (05 Ticket E).
    """
    STATUS_CHOICES = [
        ("idle", "Idle"), ("running", "Running"), ("done", "Done"),
        ("refused", "Refused"), ("error", "Error"),
    ]
    date = models.DateField(unique=True, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="idle")
    epsilon = models.IntegerField(default=0)
    requested_by = models.ForeignKey("auth.User", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    requested_at = models.DateTimeField(null=True, blank=True)
    #: When the job STARTED reading the day — the "from bookings as of" stamp.
    bookings_as_of = models.DateTimeField(null=True, blank=True)
    computed_at = models.DateTimeField(null=True, blank=True)
    result_json = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")
    budget_exhausted = models.BooleanField(default=False)

    def __str__(self):
        return f"DayPlan {self.date} ({self.status})"
