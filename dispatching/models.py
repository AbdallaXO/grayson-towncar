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
    inter_job_buffer = models.IntegerField(default=5, help_text="Minutes between jobs (break + buffer)")
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
