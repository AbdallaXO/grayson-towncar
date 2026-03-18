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
    load_balance_multiplier = models.IntegerField(default=5, help_text="Penalty per existing leg on driver")

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
