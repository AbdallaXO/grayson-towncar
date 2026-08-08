"""The Recovery Advisor PRESENTATION layer — dispatching/advisor_display.py.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_advisor_display

What is pinned here and why:
  * NO JARGON REACHES A DISPATCHER. The single most important test in this file
    sweeps every string a dispatcher can read on a card — headline, story,
    timeline labels, plan headline, outcome, warnings — for engine vocabulary
    ("chain math", "clock_only", "engine feasibility", "observed-history",
    "buffer", raw leg ids, bare airport codes). The panel exists to be read at
    4 a.m. by someone who does not know what a chain clear is.
  * THE PICTURE CANNOT LIE. The bracket measuring the shortfall is defined as
    impact − slack, i.e. the engine's own arithmetic; the spare region is drawn
    between real datetimes and labelled with turn_slack_minutes — the one shared
    formula. A drawing that disagrees with the engine is worse than no drawing.
  * NOTHING IS INVENTED. An unassigned leg has no old driver, a farmed leg's
    receiver has no schedule we hold, a monitor plan moves nothing. Each of
    those OMITS the element rather than guessing at it.
  * "CONFLICT CLEARED" IS ONLY SAID WHEN TRUE.
  * THE ENGINE'S OWN WORDS SURVIVE VERBATIM under "Show the math" — the audit
    trail is the reason the rewrite is safe.
  * A DISPLAY FAILURE IS NEVER A LOST CARD: safe_display returns None and every
    surface falls back to the engine's raw text.
"""
import json
from datetime import time as dt_time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from dispatching import advisor_display as ad
from dispatching import conflict_advisor as ca
from dispatching.scheduler import preload_timing_cache
from dispatching.tests_conflict_advisor import (
    DAY, PREV, _board, _dt, _fake_farm_ctx, _leg, _one_card, _PureBoardTestCase,
)


# Engine vocabulary that must never surface where a dispatcher reads. Each of
# these is real text the engine emits today (conflict_advisor.py) — the point of
# the redesign is that it lives under "Show the math" instead.
_JARGON = [
    "chain math", "chain clear", "clock_only", "gps_fresh", "gps_stale_parked",
    "recorded_pickup", "engine feasibility", "engine-feasible", "observed-history",
    "observed history", "buffer_minutes", "min buffer", "turnaround math",
    "signal basis", "issue class", "planning clock", "detection clock",
    "feasibility", "slack", "tier ", "swap_chain", "farm_out", "evict_and_farm",
    "leg_id", "resulting_slack", "validation", "band", "sim slot",
]
# Acronyms a dispatcher should never have to expand.
_ACRONYMS = ["SFB", "MCO", "MLB", "KEOI", "GPS", "ETA", "VIP"]


def _visible_strings(card):
    """Every string on a card a dispatcher actually reads — deliberately NOT
    including `math`, which is the verbatim engine audit trail."""
    out = []
    d = card.get("display") or {}
    out += [d.get("headline") or "", d.get("story") or ""]
    scope = d.get("scope") or {}
    out += [scope.get("text") or "", scope.get("detail") or ""]
    tl = d.get("conflict")
    if tl:
        out += [tl.get("title") or "", tl.get("subtitle") or ""]
        for lane in tl.get("lanes", []):
            out += [b.get("label", "") for b in lane.get("blocks", [])]
        out += [m.get("label", "") for m in tl.get("markers", [])]
        br = tl.get("bracket") or {}
        out += [br.get("label") or "", br.get("detail") or ""]
        out += [l.get("label", "") for l in tl.get("legend", [])]
    for plan in card.get("plans", []):
        pd = plan.get("display") or {}
        out += [pd.get("headline") or "", pd.get("outcome") or "",
                pd.get("action_label") or ""]
        out += [w.get("text", "") for w in pd.get("warnings", [])]
        after = pd.get("after")
        if after:
            for lane in after.get("lanes", []):
                out += [b.get("label", "") for b in lane.get("blocks", [])]
                out += [g.get("label", "") for g in lane.get("gaps", [])]
                out += [(lane.get("tag") or {}).get("text") or "",
                        lane.get("note") or ""]
    return [s for s in out if s]


def _all_geometry(card):
    """(left_pct, width_pct) for everything placed on any timeline of a card."""
    pairs = []
    d = card.get("display") or {}
    for tl in [d.get("conflict")] + [
            (p.get("display") or {}).get("after") for p in card.get("plans", [])]:
        if not tl:
            continue
        for t in (tl.get("axis") or {}).get("ticks", []):
            pairs.append((t["left_pct"], 0))
        for lane in tl.get("lanes", []):
            for b in lane.get("blocks", []):
                pairs.append((b["left_pct"], b["width_pct"]))
            for g in lane.get("gaps", []):
                pairs.append((g["left_pct"], g["width_pct"]))
            if lane.get("tag"):
                pairs.append((lane["tag"]["left_pct"], 0))
        for m in tl.get("markers", []):
            pairs.append((m["left_pct"], 0))
        if tl.get("bracket"):
            pairs.append((tl["bracket"]["left_pct"], tl["bracket"]["width_pct"]))
    return pairs


# ════════════════════════════════════════════════════════════════════════════
# BOARDS
# ════════════════════════════════════════════════════════════════════════════

def _double_booked(now=None, **kw):
    """The canonical card: one driver holds two airport runs ten minutes apart,
    and a second driver has room later in the morning."""
    a = _leg(101, 3, 50, pickup="Disney's Port Orleans Resort, Orlando, FL",
             dropoff="SFB", driver_id=1)
    b = _leg(102, 4, 0, pickup="Walt Disney World Swan Reserve, Orlando, FL",
             dropoff="SFB", driver_id=1)
    c = _leg(201, 8, 52, pickup="Disney Contemporary", dropoff="MCO",
             driver_id=2)
    return _board({1: [a, b], 2: [c]}, now=now or _dt(13, 42, day=PREV), **kw)


def _no_one_free(now=None, **kw):
    """The double-booking with NO in-house receiver anywhere — the only board
    on which the engine will offer an affiliate at all (farming is a last
    resort, never an alternative to keeping the work in-house)."""
    a = _leg(101, 3, 50, pickup="Disney's Port Orleans Resort", dropoff="SFB",
             driver_id=1)
    b = _leg(102, 4, 0, pickup="Walt Disney World Swan Reserve", dropoff="SFB",
             driver_id=1)
    return _board({1: [a, b]}, now=now or _dt(13, 42, day=PREV), **kw)


def _tight_receiver(now=None, **kw):
    """Same conflict, but the receiver's next job sits 3 minutes behind the
    moved run — the tight-turn sliver case."""
    a = _leg(501, 4, 0, pickup="Disney's Grand Floridian Resort", dropoff="MCO",
             driver_id=1)
    b = _leg(502, 4, 5, pickup="Disney's Yacht Club Resort", dropoff="MCO",
             driver_id=1)
    c = _leg(503, 5, 3, pickup="Disney Polynesian", dropoff="MCO", driver_id=2)
    return _board({1: [a, b], 2: [c]}, now=now or _dt(13, 42, day=PREV), **kw)


def _flight_break(next_at=(12, 30)):
    """An arrival whose plane moved without acknowledgement, breaking the turn
    into the same driver's next job."""
    from dispatching.tests_conflict_advisor import _flight
    a = _leg(601, 11, 0, trip="arrival", pickup="SFB",
             dropoff="Disney Contemporary", driver_id=1,
             has_unacked_time_change=True,
             pickup_time_changed_at=_dt(8, 0), flight=_flight(_dt(11, 25)))
    b = _leg(602, next_at[0], next_at[1], pickup="Disney Contemporary",
             dropoff="MCO", driver_id=1)
    return _board({1: [a, b]}, now=_dt(9, 0))


def _overrunning_turn():
    """Back-to-back jobs, not simultaneous ones: an 11:00 airport run that drops
    off at 12:15 and leaves the driver unable to get back for his OWN 12:30
    pickup. He is late, not double-booked."""
    g1 = _leg(101, 11, 0, trip="arrival", pickup="MCO",
              dropoff="Disney Beach Club", driver_id=1)
    g2 = _leg(102, 12, 30, pickup="Stella Nova Resort", dropoff="MCO",
              driver_id=1)
    s1 = _leg(201, 9, 45, trip="arrival", pickup="MCO",
              dropoff="Disney Pop Century", driver_id=2)
    s2 = _leg(202, 14, 0, trip="arrival", pickup="MCO",
              dropoff="Disney Saratoga Springs", driver_id=2)
    board = _board({1: [g1, g2], 2: [s1, s2]}, now=_dt(15, 0, day=PREV))
    return board, _first_card(board)


def _state(board):
    return ca._advisor_state(board, "fp-test")


def _first_card(board, kind="overlap"):
    for c in _state(board)["disruptions"]:
        if c["kind"] == kind:
            return c
    raise AssertionError(f"no {kind} card on this board")


# ════════════════════════════════════════════════════════════════════════════
# THE PRIME TEST — no engine vocabulary reaches a dispatcher
# ════════════════════════════════════════════════════════════════════════════

class NoJargonTests(_PureBoardTestCase):

    def _assert_clean(self, card):
        for s in _visible_strings(card):
            low = s.lower()
            for word in _JARGON:
                self.assertNotIn(word, low, f"jargon {word!r} in: {s!r}")
            for code in _ACRONYMS:
                self.assertNotIn(
                    code, s, f"acronym {code!r} unexpanded in: {s!r}")

    def test_double_booked_card_reads_plainly(self):
        self._assert_clean(_first_card(_double_booked()))

    def test_tight_receiver_card_reads_plainly(self):
        self._assert_clean(_first_card(_tight_receiver()))

    def test_farm_plan_card_reads_plainly(self):
        board = _no_one_free(farm_ctx=_fake_farm_ctx())
        self._assert_clean(_first_card(board))

    def test_unassigned_card_reads_plainly(self):
        orphan = _leg(301, 4, 30, pickup="SFB", dropoff="Disney Contemporary",
                      trip="arrival", driver_id=None)
        board = _board({1: []}, now=_dt(3, 30), extra_legs=[orphan])
        self._assert_clean(_first_card(board, kind="unassigned"))

    def test_locations_are_spelled_out_not_coded(self):
        card = _first_card(_double_booked())
        blob = " ".join(_visible_strings(card))
        self.assertIn("Sanford", blob)
        self.assertIn("Port Orleans", blob)
        self.assertNotIn("SFB", blob)
        # And the address tail never reaches a timeline block.
        self.assertNotIn("Orlando, FL", blob)


# ════════════════════════════════════════════════════════════════════════════
# THE CONFLICT PICTURE
# ════════════════════════════════════════════════════════════════════════════

class ConflictTimelineTests(_PureBoardTestCase):

    def test_headline_names_the_problem_not_the_remedy(self):
        card = _first_card(_double_booked())
        self.assertEqual(card["display"]["headline"], "D1 is double-booked at 4:00 AM")
        # The engine's own headline is untouched underneath it.
        self.assertIn("110 min short", card["headline"])

    def test_story_carries_the_real_clock_times(self):
        story = _first_card(_double_booked())["display"]["story"]
        for fragment in ("3:50 AM", "4:00 AM", "5:50 AM", "1 hour 50 minutes"):
            self.assertIn(fragment, story)

    def test_prose_says_who_will_be_late_by_how_much(self):
        """Dispatchers talk about people, not turns: "George will be 13 minutes
        late", never "the turn out goes 13 min short"."""
        story = _first_card(_double_booked())["display"]["story"]
        self.assertRegex(story, r"D1 would be 1 hour 50 minutes late for the "
                                r"4:00 AM .* pickup")
        self.assertNotIn("short", story)

    def test_four_elements_are_drawn(self):
        tl = _first_card(_double_booked())["display"]["conflict"]
        kinds = [b["type"] for lane in tl["lanes"] for b in lane["blocks"]]
        self.assertEqual(kinds, ["run", "reset", "conflict"])
        self.assertIsNotNone(tl["bracket"])

    def test_bracket_is_the_engines_own_arithmetic(self):
        """The shortfall drawn == impact − slack, so the picture and the
        detector can never disagree about how short the driver is."""
        board = _double_booked()
        d = _one_card(board, "overlap")
        tl = ad.card_display(board, d)["conflict"]
        self.assertEqual(tl["bracket"]["label"],
                         f'{ad.duration(d.details["slack"])} short')
        ready = d.impact_dt - timedelta(minutes=d.details["slack"])
        self.assertIn(ad.clock(ready), tl["markers"][1]["label"])

    def test_reset_block_starts_where_the_committed_run_clears(self):
        tl = _first_card(_double_booked())["display"]["conflict"]
        run, reset = tl["lanes"][0]["blocks"]
        self.assertAlmostEqual(run["left_pct"] + run["width_pct"],
                               reset["left_pct"], places=2)

    def test_geometry_stays_inside_the_axis(self):
        for board in (_double_booked(), _tight_receiver()):
            for left, width in _all_geometry(_first_card(board)):
                self.assertGreaterEqual(left, 0)
                self.assertLessEqual(left + width, 100.001)

    def test_unassigned_draws_the_run_with_no_driver_lane_and_no_bracket(self):
        orphan = _leg(301, 4, 30, pickup="SFB", dropoff="Disney Contemporary",
                      trip="arrival", driver_id=None)
        board = _board({1: []}, now=_dt(3, 30), extra_legs=[orphan])
        tl = _first_card(board, kind="unassigned")["display"]["conflict"]
        self.assertIsNone(tl["bracket"])
        self.assertEqual(len(tl["lanes"]), 1)
        self.assertEqual(tl["lanes"][0]["blocks"][0]["type"], "conflict")

    def test_late_plane_that_breaks_the_next_pickup_is_drawn(self):
        board = _flight_break()
        card = _first_card(board, kind="flight_change")
        tl = card["display"]["conflict"]
        self.assertIsNotNone(tl)
        self.assertIn("plane lands too late", tl["bracket"]["detail"])
        kinds = [b["type"] for lane in tl["lanes"] for b in lane["blocks"]]
        self.assertIn("run", kinds)
        self.assertIn("conflict", kinds)

    def test_a_tightened_turn_invents_no_gap(self):
        """A turn thinned to 10 minutes has no shortfall to draw — drawing one
        would manufacture a problem the engine did not find.

        (A flight that moved and broke NOTHING no longer reaches the display
        at all: detection drops it, because a plane moving is not a card.
        tests_conflict_advisor.FlightChangeTests owns that rule.)"""
        board = _flight_break(next_at=(13, 20))
        card = _first_card(board, kind="flight_change")
        self.assertEqual(card["severity"], "warning")
        self.assertIsNone(card["display"]["conflict"])

    def test_timeline_title_is_english(self):
        """"This afternoon", never "Today afternoon"."""
        board = _flight_break()
        card = _first_card(board, kind="flight_change")
        title = card["display"]["conflict"]["title"]
        self.assertTrue(title.startswith("This "), title)
        self.assertNotIn("Today ", title)

    def test_a_cascade_draws_the_break_the_card_counts_down_to(self):
        """The FIRST break, not the worst. Later breaks were measured against
        the carried-forward clear of the ones before them, so pairing the
        anchor with the worst one drew a reset block straight over the job that
        actually breaks first — and named a different pickup than the card's
        own headline and countdown."""
        board = _board({1: [_leg(301, 9, 0), _leg(302, 9, 25), _leg(303, 9, 30)]},
                       now=_dt(9, 20))
        card = _first_card(board, kind="late_cascade")
        breaks = _one_card(board, "late_cascade").details["breaks"]
        self.assertGreater(len(breaks), 1, "fixture must produce >1 break")
        tl = card["display"]["conflict"]
        blocked = next(b for lane in tl["lanes"] for b in lane["blocks"]
                       if b["type"] == "conflict")
        # The drawn pickup is the one impact_at counts down to.
        self.assertEqual(blocked["time"], ad.clock(_dt(9, 25)))
        self.assertIn(card["impact_at"][-5:].replace(":", ":"),
                      card["impact_at"])
        # ...and the bracket quotes that break's slack, not a later one's.
        first_slack = next(s for lid, s in breaks if lid == 302)
        self.assertEqual(tl["bracket"]["label"],
                         f"{ad.duration(first_slack)} short")

    def test_hygiene_card_is_not_called_lateness(self):
        """"Chase the button" shares the late_cascade kind but is the opposite
        claim — nobody is late, nobody tapped the app."""
        leg = _leg(101, 3, 50, driver_id=1)
        board = _board({1: [leg]}, now=_dt(5, 0))   # past stale, inside the TTL
        card = _first_card(board, kind="late_cascade")
        disp = card["display"]
        self.assertNotIn("running late", disp["headline"])
        self.assertIn("marked", disp["headline"])
        # And it never claims a diagnosis the engine abstained from.
        self.assertIsNone(disp["scope"])
        self.assertIsNone(disp["conflict"])


# ════════════════════════════════════════════════════════════════════════════
# THE AFTER-THE-MOVE PICTURE
# ════════════════════════════════════════════════════════════════════════════

class AfterTimelineTests(_PureBoardTestCase):

    def test_old_lane_cleared_new_lane_tagged_with_the_source(self):
        card = _first_card(_double_booked())
        after = card["plans"][0]["display"]["after"]
        lanes = {lane["role"]: lane for lane in after["lanes"]}
        # `cleared` is a lane-level flag, not a positioned tag: anchored to a
        # time it rendered underneath whatever job happened to be there.
        self.assertTrue(lanes["from"]["cleared"])
        self.assertIsNone(lanes["from"]["tag"])
        self.assertFalse(lanes["to"]["cleared"])
        self.assertEqual(lanes["to"]["tag"]["text"], "moved from D1")

    def test_moved_run_lands_on_the_receiving_lane(self):
        after = _first_card(_double_booked())["plans"][0]["display"]["after"]
        to_lane = next(l for l in after["lanes"] if l["role"] == "to")
        self.assertEqual([b["type"] for b in to_lane["blocks"]
                          if b["type"] != "reset"], ["moved", "other"])

    def test_receivers_real_next_job_anchors_the_spare_time(self):
        """The green region means nothing unless the thing on its far side is a
        job the dispatcher can go and look at."""
        after = _first_card(_double_booked())["plans"][0]["display"]["after"]
        to_lane = next(l for l in after["lanes"] if l["role"] == "to")
        nxt = next(b for b in to_lane["blocks"] if b["type"] == "other")
        self.assertEqual(nxt["time"], "8:52 AM")           # the real leg 201
        room = next(g for g in to_lane["gaps"] if g["kind"] == "room")
        self.assertAlmostEqual(room["left_pct"] + room["width_pct"],
                               nxt["left_pct"], places=2)

    def test_spare_region_is_drawn_to_scale(self):
        """The green region's width must mean the same thing as its label.

        The clock gap between two jobs is not all free time — the drive back
        eats into it. If the region covered the whole gap while claiming the
        usable spare, a dispatcher eyeballing the picture would read free time
        the driver does not have.
        """
        after = _first_card(_double_booked())["plans"][0]["display"]["after"]
        to_lane = next(l for l in after["lanes"] if l["role"] == "to")
        room = next(g for g in to_lane["gaps"] if g["kind"] == "room")
        moved = next(b for b in to_lane["blocks"] if b["type"] == "moved")
        # Same minutes-per-percent as every other block on this axis.
        self.assertAlmostEqual(room["width_pct"] / room["minutes"],
                               moved["width_pct"] / moved["minutes"],
                               delta=0.01)

    def test_drive_back_is_drawn_rather_than_folded_into_the_free_time(self):
        after = _first_card(_double_booked())["plans"][0]["display"]["after"]
        to_lane = next(l for l in after["lanes"] if l["role"] == "to")
        moved = next(b for b in to_lane["blocks"] if b["type"] == "moved")
        back = next(b for b in to_lane["blocks"] if b["type"] == "reset")
        room = next(g for g in to_lane["gaps"] if g["kind"] == "room")
        nxt = next(b for b in to_lane["blocks"] if b["type"] == "other")
        # moved run -> drive back -> free time -> the next job, edge to edge.
        self.assertAlmostEqual(moved["left_pct"] + moved["width_pct"],
                               back["left_pct"], places=2)
        self.assertAlmostEqual(back["left_pct"] + back["width_pct"],
                               room["left_pct"], places=2)
        self.assertAlmostEqual(room["left_pct"] + room["width_pct"],
                               nxt["left_pct"], places=2)

    def test_tight_turn_renders_as_a_sliver_with_the_engines_minutes(self):
        board = _tight_receiver()
        card = _first_card(board)
        plan_d = card["plans"][0]["display"]
        to_lane = next(l for l in plan_d["after"]["lanes"] if l["role"] == "to")
        tight = next(g for g in to_lane["gaps"] if g["kind"] == "tight")
        self.assertLess(tight["minutes"], 15)         # pickup_policy threshold
        self.assertIn(str(tight["minutes"]), tight["label"])
        self.assertTrue(any(w["tone"] == "red" and "Tight turn" in w["text"]
                            for w in plan_d["warnings"]), plan_d["warnings"])

    def test_roomy_turn_is_never_drawn_as_tight(self):
        """Prime directive: a turn the engine calls legal must not render red."""
        after = _first_card(_double_booked())["plans"][0]["display"]["after"]
        to_lane = next(l for l in after["lanes"] if l["role"] == "to")
        self.assertFalse([g for g in to_lane["gaps"] if g["kind"] == "tight"])

    def test_outcome_sentence_matches_the_picture(self):
        plan_d = _first_card(_double_booked())["plans"][0]["display"]
        to_lane = next(l for l in plan_d["after"]["lanes"] if l["role"] == "to")
        room = next(g for g in to_lane["gaps"] if g["kind"] == "room")
        self.assertIn(ad.duration_words(room["minutes"]), plan_d["outcome"])
        self.assertIn("D1 keeps", plan_d["outcome"])

    def test_monitor_plan_draws_no_after_picture(self):
        """A plan that moves nothing has no "after" — and must not invent one."""
        plan = SimpleNamespace(
            kind="monitor", tier=0, title="Monitor — no move is warranted yet",
            moves=[], time_changes=[], why=["Signal basis: clock_only."],
            risks=[], risk_flags=[], farm_out=False, price_impact=None,
            validation=None, score=0, target_leg_id=101)
        board = _double_booked()
        d = _one_card(board, "overlap")
        disp = ad.plan_display(board, d, plan, 1)
        self.assertIsNone(disp["after"])
        self.assertEqual(disp["action_label"], "Nothing to apply")
        self.assertIn("watching", disp["outcome"])

    def test_farmed_run_shows_the_affiliate_and_invents_no_free_time(self):
        """We do not hold an affiliate's day; an empty lane would read as
        availability we cannot actually see."""
        board = _no_one_free(farm_ctx=_fake_farm_ctx())
        card = _first_card(board)
        farm = next((p for p in card["plans"] if p["farm_out"]), None)
        self.assertIsNotNone(farm, [p["title"] for p in card["plans"]])
        lanes = farm["display"]["after"]["lanes"]
        aff = next(l for l in lanes if l["role"] == "affiliate")
        self.assertEqual(aff["driver"], "Oualid")
        self.assertEqual(aff["gaps"], [])
        self.assertIn("no live tracking", aff["note"])

    def test_retimed_run_is_drawn_at_its_new_time(self):
        """A match_flight plan moves the pickup itself — drawing the old time
        would show a board the plan does not produce."""
        board = _double_booked()
        d = _one_card(board, "overlap")
        leg = board.legs_by_id[102]
        plan = SimpleNamespace(
            kind="match_flight", tier=1, title="Match the flight",
            moves=[ca.PlanMove(leg_id=102, op="retime", from_driver_id=1,
                               new_pickup_time=dt_time(6, 30))],
            time_changes=[ca.PickupTimeChange(leg_id=102,
                                              old_time=leg.pickup_time,
                                              new_time=dt_time(6, 30))],
            why=[], risks=[], risk_flags=[], farm_out=False, price_impact=None,
            validation=None, score=10, target_leg_id=102)
        after = ad.plan_display(board, d, plan, 1)["after"]
        times = [b["time"] for lane in after["lanes"] for b in lane["blocks"]
                 if b["type"] != "reset"]
        self.assertIn("6:30 AM", times)
        self.assertNotIn("4:00 AM", times)


# ════════════════════════════════════════════════════════════════════════════
# WARNINGS + THE AUDIT TRAIL
# ════════════════════════════════════════════════════════════════════════════

class WarningAndMathTests(_PureBoardTestCase):

    def test_guessed_availability_becomes_a_call_them_first_warning(self):
        board = _double_booked(window_sources={2: "stub"},
                               windows={2: {"start": 3, "end": 22}})
        warns = _first_card(board)["plans"][0]["display"]["warnings"]
        text = " ".join(w["text"] for w in warns)
        self.assertIn("Check with D2 first", text)
        self.assertIn("no set shift tomorrow", text)
        self.assertNotIn("observed-history", text)

    def test_every_engine_risk_survives_even_when_unrecognised(self):
        """An unrecognised risk line is shown verbatim, never dropped — losing a
        safety line to a presentation layer is not an acceptable trade."""
        board = _double_booked()
        d = _one_card(board, "overlap")
        plan = ca.generate_plans(board, d)[0]
        plan.risks.append("Some future risk nobody has written a phrasing for.")
        texts = [w["text"] for w in ad.plan_display(board, d, plan, 1)["warnings"]]
        self.assertIn("Some future risk nobody has written a phrasing for.",
                      texts)

    def test_show_the_math_keeps_the_engine_text_byte_for_byte(self):
        card = _first_card(_double_booked())
        plan = card["plans"][0]
        math = plan["display"]["math"]
        self.assertEqual(math["why"], plan["why"])
        self.assertEqual(math["risks"], plan["risks"])
        self.assertEqual(math["title"], plan["title"])
        self.assertEqual(math["narrative"], card["narrative"])
        facts = " ".join(math["facts"])
        self.assertIn("issue class: clock_only", facts)
        self.assertIn("spare after move", facts)

    def test_feasibility_sentinels_are_never_printed_as_minutes(self):
        """check_feasibility answers 999 for "nothing on that side" — printing
        it as a minute count is how a panel loses a dispatcher's trust."""
        board = _double_booked()
        d = _one_card(board, "overlap")
        plan = ca.generate_plans(board, d)[0]
        for m in plan.moves:
            m.resulting_slack_min = 999
        facts = " ".join(ad.plan_display(board, d, plan, 1)["math"]["facts"])
        self.assertNotIn("999 min", facts)
        self.assertIn("unbounded", facts)


# ════════════════════════════════════════════════════════════════════════════
# CONTRACT + DEGRADATION
# ════════════════════════════════════════════════════════════════════════════

class ContractTests(_PureBoardTestCase):

    def test_state_stays_json_serializable(self):
        for board in (_double_booked(), _tight_receiver(),
                      _no_one_free(farm_ctx=_fake_farm_ctx())):
            json.dumps(_state(board))

    def test_engine_fields_are_untouched_by_the_display_layer(self):
        """The redesign is additive: apply payloads, titles and ranking order
        are exactly what the engine produced."""
        board = _double_booked()
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        card = _first_card(board)
        self.assertEqual([p["rank"] for p in card["plans"]],
                         list(range(1, len(card["plans"]) + 1)))
        for serialized, plan in zip(card["plans"], plans):
            self.assertEqual(serialized["title"], plan.title)
            self.assertEqual(serialized["why"], plan.why)
            if serialized.get("apply"):
                self.assertEqual(serialized["apply"]["title"], plan.title)

    def test_a_display_failure_never_costs_the_card(self):
        board = _double_booked()
        boom = lambda *a, **k: 1 / 0
        with patch.object(ad, "card_display", boom), \
                patch.object(ad, "plan_display", boom):
            state = _state(board)
        card = state["disruptions"][0]
        self.assertIsNone(card["display"])
        self.assertTrue(card["headline"])            # engine text still there
        self.assertTrue(card["plans"])
        self.assertIsNone(card["plans"][0]["display"])
        self.assertTrue(card["plans"][0]["apply"]["actions"])

    def test_cross_midnight_committed_run_is_still_drawn(self):
        """The guard-3 overnight tail keeps the earlier leg on the PREVIOUS
        date, outside today's schedules — without it the card loses the very
        half of the picture that explains the problem."""
        from dispatching.scheduler import _make_sim_slot
        tail = _leg(101, 0, 30, trip="arrival", pickup="SFB",
                    dropoff="Disney Contemporary", driver_id=1)
        last_night = _leg(99, 23, 30, pickup="Disney Contemporary",
                          dropoff="Port Canaveral", driver_id=1, day=PREV)
        board = _board({1: [tail]}, now=_dt(20, 0, day=PREV),
                       prev_tail={1: [(_make_sim_slot(last_night, PREV), None)]})
        cards = [c for c in ca.detect_disruptions(board) if c.kind == "overlap"]
        if not cards:
            self.skipTest("fixture produced no cross-midnight overlap")
        tl = ad.card_display(board, cards[0])["conflict"]
        self.assertIsNotNone(tl)
        self.assertEqual(tl["lanes"][0]["blocks"][0]["type"], "run")


# ════════════════════════════════════════════════════════════════════════════
# PRIME DIRECTIVE — a turn the engine calls legal is never drawn as a break
# ════════════════════════════════════════════════════════════════════════════

def _plan(**kw):
    """A CandidatePlan-shaped stand-in, for pinning display behaviour against
    engine verdicts that are hard to provoke through a whole board."""
    base = dict(kind="reassign", tier=2, title="t", target_leg_id=0, moves=[],
                time_changes=[], why=[], risks=[], risk_flags=[],
                farm_out=False, price_impact=None, validation=None, score=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _validation(**kw):
    base = dict(ok=True, reason="", min_buffer_after=999, worsened_pairs=[],
                new_tight_count=0, per_driver={})
    base.update(kw)
    return SimpleNamespace(**base)


class PrimeDirectiveTests(_PureBoardTestCase):
    """A warning-band overlap has POSITIVE slack — the engine says the turn
    works, reality just thinned it. Drawing that as a shortfall would flag a
    turn the assignment engine blessed, which is the one thing this panel must
    never do (conflict_advisor.py module docstring, hazard guard 13)."""

    def _thin_turn(self):
        """A REAL warning-band overlap, straight out of the detector: an early
        recorded pickup thins a clean turn to +10 min. The warning tier is
        DEFINED by positive slack, so this is the normal case there — the card
        must never render it as a break."""
        a = _leg(101, 8, 0, pickup="Disney Contemporary",
                 dropoff="Disney Polynesian", driver_id=1)
        b = _leg(102, 8, 42, pickup="Disney Polynesian",
                 dropoff="Disney Contemporary", driver_id=1)
        board = _board({1: [a, b]}, now=_dt(7, 30), picked={101: _dt(8, 20)})
        card = _first_card(board)
        self.assertEqual(card["severity"], "warning")
        self.assertGreater(_one_card(board, "overlap").details["slack"], 0)
        return card

    def test_positive_slack_is_not_drawn_as_a_shortfall(self):
        tl = self._thin_turn()["display"]["conflict"]
        self.assertEqual(tl["bracket"]["tone"], "amber")
        self.assertIn("to spare", tl["bracket"]["label"])
        self.assertNotIn("short", tl["bracket"]["label"])
        self.assertNotIn("can't close", tl["subtitle"])

    def test_the_second_run_is_not_called_uncovered(self):
        tl = self._thin_turn()["display"]["conflict"]
        types = [b["type"] for lane in tl["lanes"] for b in lane["blocks"]]
        self.assertIn("tight", types)
        self.assertNotIn("conflict", types)
        labels = " ".join(b["label"] for lane in tl["lanes"]
                          for b in lane["blocks"])
        self.assertNotIn("no driver free", labels)
        self.assertNotIn("nobody's free", " ".join(
            l["label"] for l in tl["legend"]))

    def test_the_story_does_not_claim_the_driver_is_late(self):
        story = self._thin_turn()["display"]["story"]
        self.assertNotIn("late", story)
        self.assertIn("10 minutes to spare", story)

    def test_a_driver_running_long_into_his_own_next_job_is_not_double_booked(self):
        """He has ONE job at 12:30. His 11:00 run drops off at 12:15 and he
        can't get back — that is lateness, not a double-booking. Saying
        "double-booked" describes a board that does not exist."""
        board, card = _overrunning_turn()
        d = card["display"]
        self.assertNotIn("double-booked", d["headline"])
        self.assertRegex(d["headline"],
                         r"will be \d+ minutes? late for the 12:30 PM pickup")
        tl = d["conflict"]
        self.assertIn("doesn't clear in time", tl["bracket"]["detail"])
        self.assertNotIn("two jobs at the same time", tl["bracket"]["detail"])
        # The run IS assigned — to the driver who can't reach it.
        labels = " ".join(b["label"] for lane in tl["lanes"]
                          for b in lane["blocks"])
        self.assertNotIn("no driver free", labels)
        self.assertIn("can't make it", labels)

    def test_genuinely_simultaneous_runs_are_still_called_double_booked(self):
        card = _first_card(_double_booked())
        self.assertIn("double-booked", card["display"]["headline"])
        self.assertIn("two jobs at the same time",
                      card["display"]["conflict"]["bracket"]["detail"])

    def test_the_donor_lane_shows_where_the_run_went(self):
        """A dashed ghost sits on the donor's lane exactly where the run was,
        directly above the block that now holds it — a reassignment changes the
        driver, not the clock, so the two line up and the move is visible."""
        _board_, card = _overrunning_turn()
        after = card["plans"][0]["display"]["after"]
        donor = next(l for l in after["lanes"] if l["role"] == "from")
        receiver = next(l for l in after["lanes"] if l["role"] == "to")
        ghost = next(b for b in donor["blocks"] if b["type"] == "ghost")
        moved = next(b for b in receiver["blocks"] if b["type"] == "moved")
        self.assertEqual(ghost["leg_id"], moved["leg_id"])
        self.assertAlmostEqual(ghost["left_pct"], moved["left_pct"], places=2)
        self.assertAlmostEqual(ghost["width_pct"], moved["width_pct"], places=2)
        self.assertIn("↓ to", ghost["label"])
        self.assertEqual(receiver["tag"]["text"], "moved from D1")

    def test_a_real_break_is_still_drawn_red(self):
        tl = _first_card(_double_booked())["display"]["conflict"]
        self.assertEqual(tl["bracket"]["tone"], "red")
        types = [b["type"] for lane in tl["lanes"] for b in lane["blocks"]]
        self.assertIn("conflict", types)


# ════════════════════════════════════════════════════════════════════════════
# NO SAFETY LINE IS EVER LOST
# ════════════════════════════════════════════════════════════════════════════

class WarningFidelityTests(_PureBoardTestCase):

    def setUp(self):
        self.board = _double_booked()
        self.d = _one_card(self.board, "overlap")

    def _warn(self, plan):
        return ad.plan_display(self.board, self.d, plan, 1)["warnings"]

    def test_a_plan_that_depends_on_a_tight_turn_says_so_on_the_surface(self):
        """The engine raises depends_tight_turn for a sub-15-minute turn that
        need not be adjacent to anything drawn. If the chip is skipped, the
        line must NOT be marked handled — otherwise it vanishes from the card
        entirely and survives only inside a collapsed expander."""
        plan = _plan(
            risk_flags=["depends_tight_turn"],
            risks=["Leaves only 8 min at the tightest turn after the move."],
            validation=_validation(min_buffer_after=8))
        texts = " ".join(w["text"] for w in self._warn(plan))
        self.assertIn("8 minutes", texts)
        self.assertTrue(self._warn(plan), "the engine's warning disappeared")

    def test_every_worsened_turn_gets_its_own_chip(self):
        """Stopping at the first tight pair hid the WORST turn, on a driver the
        chip never even named."""
        plan = _plan(validation=_validation(worsened_pairs=[
            {"driver_id": 1, "prev_leg_id": 1, "next_leg_id": 2,
             "before": "", "after": "tight", "slack": 12},
            {"driver_id": 2, "prev_leg_id": 3, "next_leg_id": 4,
             "before": "", "after": "tight", "slack": 3},
        ]))
        texts = [w["text"] for w in self._warn(plan)]
        self.assertEqual(sum(1 for t in texts if "Tight turn created" in t), 2)
        self.assertTrue(any("3 minutes" in t for t in texts), texts)

    def test_no_chip_the_engine_did_not_raise(self):
        """A pure pickup-time change never trips the refund gate in the engine,
        so the panel must not assert one — a red chip above an empty "engine
        risks" list is the display inventing a hazard."""
        self.board.pending_refund_leg_ids = {101}
        plan = _plan(kind="match_flight", tier=1, moves=[
            ca.PlanMove(leg_id=101, op="retime", from_driver_id=1,
                        new_pickup_time=dt_time(6, 30))])
        texts = " ".join(w["text"] for w in self._warn(plan))
        self.assertNotIn("refund", texts)

    def test_the_refund_warning_still_fires_on_a_real_move(self):
        self.board.pending_refund_leg_ids = {101}
        plan = _plan(moves=[ca.PlanMove(leg_id=101, op="reassign",
                                        from_driver_id=1, to_driver_id=2)])
        texts = " ".join(w["text"] for w in self._warn(plan))
        self.assertIn("refund in flight", texts)


# ════════════════════════════════════════════════════════════════════════════
# ATTRIBUTION — the card must name the driver a run actually came from
# ════════════════════════════════════════════════════════════════════════════

class SwapChainTests(_PureBoardTestCase):

    def test_each_lane_names_its_own_donor(self):
        """A swap chain hands work along several drivers. A single card-wide
        donor told a dispatcher to call someone who never touched the run."""
        a = _leg(101, 4, 0, pickup="Disney Contemporary", dropoff="MCO",
                 driver_id=1)
        b = _leg(201, 9, 0, pickup="Disney Polynesian", dropoff="MCO",
                 driver_id=2)
        board = _board({1: [a], 2: [b], 3: []}, now=_dt(1, 0))
        d = ca.Disruption(
            id="overlap:101:201", kind="overlap", severity="critical",
            headline="h", narrative="n", basis="clock_only",
            leg_ids=[101], anchor_leg_id=101, driver_id=1,
            impact_dt=_dt(4, 0), details={"slack": -30})
        plan = _plan(kind="swap_chain", moves=[
            ca.PlanMove(leg_id=201, op="reassign", from_driver_id=2,
                        to_driver_id=3),
            ca.PlanMove(leg_id=101, op="reassign", from_driver_id=1,
                        to_driver_id=2),
        ])
        after = ad.plan_display(board, d, plan, 1)["after"]
        tags = {l["driver"]: (l.get("tag") or {}).get("text")
                for l in after["lanes"]}
        self.assertEqual(tags.get("D3"), "moved from D2")
        self.assertEqual(tags.get("D2"), "moved from D1")


class ClaimsWeCannotMakeTests(_PureBoardTestCase):
    """`basis` records WHICH SIGNAL found the problem. Detection never inspects
    the car, the driver's credentials or the booking, so the card must not
    report on them."""

    def test_the_scope_chip_describes_the_measurement_not_an_all_clear(self):
        scope = _first_card(_double_booked())["display"]["scope"]
        blob = f"{scope['text']} {scope['detail']}".lower()
        for claim in ("licensing", "vehicle", "everything else", "check out"):
            self.assertNotIn(claim, blob, scope)
        self.assertIn("clock", blob)

    def test_an_unassigned_run_is_never_called_a_missed_button(self):
        """A hygiene card on a driverless leg has no driver and nothing to tap.
        Reading it as "nobody pressed the button" gets an uncovered ride
        closed."""
        orphan = _leg(301, 13, 0, pickup="Universal Orlando", dropoff="MCO",
                      driver_id=None)
        board = _board({1: []}, now=_dt(14, 10), extra_legs=[orphan])
        card = _first_card(board, kind="unassigned")
        d = card["display"]
        self.assertNotIn("marked", d["headline"])
        self.assertNotIn("tapped", d["story"])
        self.assertNotIn("button", d["story"])
        self.assertIn("no driver", d["story"].lower())

    def test_an_affiliate_company_is_not_addressed_by_its_first_word(self):
        board = _no_one_free(farm_ctx=_fake_farm_ctx())
        card = _first_card(board)
        farm = next(p for p in card["plans"] if p["farm_out"])
        lanes = farm["display"]["after"]["lanes"]
        aff = next(l for l in lanes if l["role"] == "affiliate")
        self.assertIn(aff["driver"], farm["display"]["outcome"])

    def test_money_uses_one_format_everywhere(self):
        board = _no_one_free(farm_ctx=_fake_farm_ctx())
        farm = next(p for p in _first_card(board)["plans"] if p["farm_out"])
        self.assertEqual(farm["display"]["price_label"], "$120")

    def test_a_watch_card_with_nothing_broken_still_reads_plainly(self):
        """late_cascade / overrun cards with no downstream break used to fall
        straight through to raw scheduler prose ("display-grade", bare codes)."""
        leg = _leg(101, 14, 0, trip="arrival", pickup="MCO",
                   dropoff="Disney Springs", driver_id=1,
                   dispatch_risk_status="at_risk", dispatch_eta_is_fresh=True,
                   dispatch_eta_target="pickup", dispatch_eta_minutes=45,
                   dispatch_risk_reason="ETA 45 min exceeds time to pickup")
        board = _board({1: [leg]}, now=_dt(13, 30))
        cards = [c for c in _state(board)["disruptions"]
                 if c["kind"] in ("late_cascade", "overrun")
                 and not (c.get("display") or {}).get("conflict")]
        if not cards:
            self.skipTest("fixture produced no break-free watch card")
        story = cards[0]["display"]["story"]
        for jargon in ("display-grade", "chain", "MCO", "estimated end"):
            self.assertNotIn(jargon, story, story)


class SwapTesterTimelineTests(_PureBoardTestCase):
    """The Swap Tester draws its solutions with the SAME builder. It has no
    Disruption and assembles its own schedules, so `swap_board` / `swap_timeline`
    are the seam — one renderer, not two."""

    def _board_and_moves(self):
        a = _leg(101, 4, 0, pickup="Disney Contemporary", dropoff="MCO",
                 driver_id=1)
        b = _leg(201, 9, 0, pickup="Disney Polynesian", dropoff="MCO",
                 driver_id=2)
        board = _board({1: [a], 2: [b], 3: []}, now=_dt(1, 0))
        tl_board = ad.swap_board(board.schedules, board.legs_by_id, DAY)
        moves = [{"leg_id": 201, "from_driver_id": 2, "to_driver_id": 3,
                  "buffer_minutes": 120},
                 {"leg_id": 101, "from_driver_id": 1, "to_driver_id": 2,
                  "buffer_minutes": 90}]
        return tl_board, moves

    def test_a_swap_draws_the_same_lanes_as_a_recovery_plan(self):
        tl_board, moves = self._board_and_moves()
        after = ad.swap_timeline(tl_board, moves, 101)
        self.assertIsNotNone(after)
        self.assertIn("ticks", after["axis"])
        lanes = {l["driver"]: l for l in after["lanes"]}
        self.assertIn("D3", lanes)
        # Each lane names ITS OWN donor, not the chain's primary one.
        self.assertEqual(lanes["D3"]["tag"]["text"], "moved from D2")
        self.assertEqual(lanes["D2"]["tag"]["text"], "moved from D1")

    def test_a_swap_never_claims_a_conflict_was_cleared(self):
        """Nothing was in conflict on the swap tester — the donor's ghost says
        the run left, which is the true statement there."""
        tl_board, moves = self._board_and_moves()
        after = ad.swap_timeline(tl_board, moves, 101)
        self.assertFalse(any(l["cleared"] for l in after["lanes"]))

    def test_the_donor_lane_still_shows_where_the_run_went(self):
        tl_board, moves = self._board_and_moves()
        after = ad.swap_timeline(tl_board, moves, 101)
        donor = next(l for l in after["lanes"] if l["driver"] == "D1")
        ghost = next(b for b in donor["blocks"] if b["type"] == "ghost")
        self.assertEqual(ghost["leg_id"], 101)
        self.assertIn("↓ to D2", ghost["label"])

    def test_it_accepts_the_optimisers_own_move_objects(self):
        """The endpoint passes SwapMove objects straight through, so attribute
        access has to work as well as dict access."""
        tl_board, moves = self._board_and_moves()
        objs = [SimpleNamespace(**m) for m in moves]
        self.assertEqual(ad.swap_timeline(tl_board, objs, 101),
                         ad.swap_timeline(tl_board, moves, 101))

    def test_no_moves_draws_nothing(self):
        tl_board, _ = self._board_and_moves()
        self.assertIsNone(ad.swap_timeline(tl_board, [], 101))


# ════════════════════════════════════════════════════════════════════════════
# PLAIN-LANGUAGE PRIMITIVES
# ════════════════════════════════════════════════════════════════════════════

class PlainLanguageTests(_PureBoardTestCase):

    def test_place_names_read_the_way_a_dispatcher_says_them(self):
        cases = {
            "Orlando International Airport (MCO), Jeff Fuqua Blvd, Orlando, FL":
                "Orlando Airport",
            "SFB": "Sanford",
            "MCO Terminal B": "Orlando Airport",
            "Disney's Port Orleans Resort, Orlando, FL": "Port Orleans",
            "Walt Disney World Swan Reserve": "Swan Reserve",
            "Disney's Grand Floridian Resort & Spa": "Grand Floridian",
            "Port Canaveral": "Port Canaveral",
            "1234 Sand Lake Rd, Orlando, FL 32819": "1234 Sand Lake Rd",
            "": "",
            # The chain word is part of the NAME here — strip it and the
            # destination becomes somewhere that does not exist.
            "Disney Springs": "Disney Springs",
            "Universal Orlando": "Universal Orlando",
        }
        for raw, expected in cases.items():
            self.assertEqual(ad.plain_place(raw), expected, raw)

    def test_a_venue_is_never_mistaken_for_an_airport(self):
        """Three letters are a common word fragment. A code only counts when it
        opens the label and is followed by airport vocabulary."""
        for raw in ("Cafe Mia", "Mia's Kitchen", "Lal Bagh Restaurant",
                    "Jax Bar & Grill", "Dab City Lounge"):
            got = ad.plain_place(raw)
            self.assertNotIn("Airport", got, f"{raw!r} -> {got!r}")
            self.assertNotIn("Lakeland", got, f"{raw!r} -> {got!r}")
        # ...and a real terminal still resolves.
        self.assertEqual(ad.plain_place("MCO Terminal B"), "Orlando Airport")

    def test_a_code_inside_a_venue_name_is_expanded_in_place(self):
        """"Brightline MCO" is a station, not the airport — the venue keeps its
        name and only the code is spelled out. Case-sensitive, which is what
        separates it from "Cafe Mia"."""
        self.assertEqual(ad.plain_place("Brightline MCO"),
                         "Brightline Orlando Airport")
        self.assertEqual(ad.plain_place("Cafe Mia"), "Cafe Mia")

    def test_a_name_is_never_trimmed_away_to_nothing(self):
        for raw in ("Resort", "The Inn", "Hotel", "Disney"):
            self.assertTrue(ad.plain_place(raw), raw)

    def test_durations_read_as_spoken(self):
        self.assertEqual(ad.duration(3), "3 min")
        self.assertEqual(ad.duration(45), "45 min")
        self.assertEqual(ad.duration(60), "1 h")
        self.assertEqual(ad.duration(110), "1 h 50 m")
        self.assertEqual(ad.duration(-110), "1 h 50 m")   # magnitude, not sign
        self.assertEqual(ad.duration(None), "")

    def test_clock_drops_the_leading_zero(self):
        self.assertEqual(ad.clock(_dt(4, 0)), "4:00 AM")
        self.assertEqual(ad.clock(dt_time(16, 5)), "4:05 PM")
        self.assertEqual(ad.clock(None), "")

    def test_day_word_is_relative_where_a_person_would_be(self):
        self.assertEqual(ad.day_word(DAY, DAY), "today")
        self.assertEqual(ad.day_word(DAY, PREV), "tomorrow")
        self.assertEqual(ad.day_word(PREV, DAY), "yesterday")
        self.assertTrue(ad.day_word(DAY + timedelta(days=4), DAY).startswith("on "))

    def test_axis_places_a_moment_at_its_true_fraction(self):
        axis = ad.Axis(_dt(4, 0), _dt(6, 0))
        self.assertEqual(axis.pct(_dt(4, 0)), 0.0)
        self.assertEqual(axis.pct(_dt(5, 0)), 50.0)
        self.assertEqual(axis.pct(_dt(6, 0)), 100.0)
        left, width = axis.span(_dt(4, 30), _dt(5, 30))
        self.assertEqual((left, width), (25.0, 50.0))

    def test_safe_display_swallows_failures(self):
        self.assertIsNone(ad.safe_display(lambda: 1 / 0))
        self.assertEqual(ad.safe_display(lambda: "ok"), "ok")
