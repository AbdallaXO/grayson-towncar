"""Fold-Out Advisor tests — demand-aware staffing arc.

Mirrors tests_shift_advisor.py: in-memory boards via tests_span_caps fixtures, real
DB rows only where the module reads them (DriverVehicleAssignment for the candidate's
vehicle + dvtypes). Every gate in build_fold_out_proposals gets a test that fails
when the gate is removed.
"""
import json
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from dispatching.fold_advisor import build_fold_out_proposals
from dispatching.tests_span_caps import _slot, _sched, _FakeLeg
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Vehicle

User = get_user_model()
TARGET = date(2026, 6, 9)   # must match tests_span_caps.D — _slot() builds end times on it

W_RIGID = {"start": None, "end": None, "max_hours": 17.0, "flexible": False,
           "night_exempt": False}
W_FLEX = {"start": None, "end": None, "max_hours": 17.0, "flexible": True,
          "night_exempt": False}
ALL_DAY = (4, 23)


def _mk_driver(username, certified=None):
    u = User.objects.create_user(username=username, password="x")
    d = Driver.objects.create(profile=u, driver_type="inhouse", is_active=True)
    if certified:
        d.certified_vehicle_types.add(certified)
    return d


def _leg(leg_id, pickup_h, pickup_m=0, vtype="suv", revenue=100):
    return _FakeLeg(
        id=leg_id, pickup_time=time(pickup_h, pickup_m),
        pickup_location="Disney Resort", dropoff_location="MCO Terminal",
        effective_vehicle_type=vtype, revenue_share=revenue,
        driver=None, driver_id=None, reservation_id=1, status="pending",
        flight_information=None, trip_type="return",
    )


class FoldAdvisorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.vt_van14 = Vehicle.objects.create(vehicle_type="Van(14 Pax)", capacity=14,
                                              luggage_capacity=14, requires_certification=True)
        cls.unit_thin = FleetVehicle.objects.create(vehicle_number="003", vehicle_type=cls.vt_suv,
                                                    year=2023, make="Chevy", model="Suburban")
        cls.unit_recv = FleetVehicle.objects.create(vehicle_number="006", vehicle_type=cls.vt_suv,
                                                    year=2023, make="Chevy", model="Suburban")
        cls.thin = _mk_driver("thin")
        cls.recv = _mk_driver("recv")
        DriverVehicleAssignment.objects.create(driver=cls.thin, date=TARGET, vehicle=cls.unit_thin)
        DriverVehicleAssignment.objects.create(driver=cls.recv, date=TARGET, vehicle=cls.unit_recv)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _ctx(self, thin_slots, recv_slots, **over):
        """Standard two-driver context: thin (candidate) + recv (receiver)."""
        legs = {s.leg_id: _leg(s.leg_id, s.pickup_time.hour, s.pickup_time.minute)
                for s in thin_slots}
        legs.update(over.pop("extra_legs", {}))
        ctx = dict(
            target_date=TARGET,
            proposed_schedules={self.thin.id: _sched(self.thin.id, list(thin_slots), name="thin"),
                                self.recv.id: _sched(self.recv.id, list(recv_slots), name="recv")},
            final_assignments={s.leg_id: self.thin.id for s in thin_slots},
            locked_leg_ids=set(),
            driver_hours={self.thin.id: ALL_DAY, self.recv.id: ALL_DAY},
            flexible_drivers=set(),
            capped_windows={self.thin.id: dict(W_RIGID), self.recv.id: dict(W_RIGID)},
            sharer_partners={},
            legs_by_id=legs,
            drivers_by_id={self.thin.id: self.thin, self.recv.id: self.recv},
        )
        ctx.update(over)
        return ctx

    def _folds(self, ctx):
        return [p for p in build_fold_out_proposals(**ctx) if p["kind"] == "fold_out"]

    # ── the happy path ───────────────────────────────────────────────────────
    def test_thin_driver_folds_when_all_legs_fit(self):
        ctx = self._ctx([_slot(11, 9), _slot(12, 11)], [_slot(1, 6)])
        cards = self._folds(ctx)
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["signature"], f"fold-{self.thin.id}")
        self.assertEqual(c["driver_id"], self.thin.id)
        self.assertEqual(c["vehicle_id"], self.unit_thin.id)
        self.assertEqual(c["leg_count"], 2)
        self.assertEqual(len(c["relocations"]), 2)
        self.assertTrue(all(r["to_driver_id"] == self.recv.id for r in c["relocations"]))
        self.assertIn("003", c["freed_note"])
        self.assertEqual(len(c["receivers"]), 1)
        self.assertGreater(c["receivers"][0]["eff_after"], c["receivers"][0]["eff_before"])

    def test_empty_day_candidate_zero_relocations(self):
        ctx = self._ctx([], [_slot(1, 6)])
        cards = self._folds(ctx)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["leg_count"], 0)
        self.assertEqual(cards[0]["relocations"], [])
        self.assertIn("003", cards[0]["freed_note"])

    # ── candidate gates ──────────────────────────────────────────────────────
    def test_four_legs_not_a_candidate(self):
        ctx = self._ctx([_slot(11, 8), _slot(12, 10), _slot(13, 12), _slot(14, 14)],
                        [_slot(1, 6)])
        self.assertEqual(self._folds(ctx), [])

    def test_locked_leg_disqualifies(self):
        ctx = self._ctx([_slot(11, 9), _slot(12, 11)], [_slot(1, 6)],
                        locked_leg_ids={12})
        self.assertEqual(self._folds(ctx), [])

    def test_preexisting_db_leg_disqualifies(self):
        # Leg 12 is on thin's board but NOT in final_assignments — a pre-existing
        # (dispatcher-made) assignment. Manual-sovereign: never fold him out.
        ctx = self._ctx([_slot(11, 9), _slot(12, 11)], [_slot(1, 6)])
        del ctx["final_assignments"][12]
        self.assertEqual(self._folds(ctx), [])

    def test_build_first_driver_excluded(self):
        ctx = self._ctx([_slot(11, 9)], [_slot(1, 6)],
                        build_first_ids={self.thin.id})
        self.assertEqual(self._folds(ctx), [])

    def test_sharer_candidate_excluded(self):
        ctx = self._ctx([_slot(11, 9)], [_slot(1, 6)],
                        sharer_partners={self.thin.id: {self.recv.id}})
        self.assertEqual(self._folds(ctx), [])

    def test_no_vehicle_candidate_skipped(self):
        DriverVehicleAssignment.objects.filter(driver=self.thin).delete()
        ctx = self._ctx([_slot(11, 9)], [_slot(1, 6)])
        self.assertEqual(self._folds(ctx), [])

    def test_suppressed_when_residuals_exist(self):
        ctx = self._ctx([_slot(11, 9)], [_slot(1, 6)], residual_count=1)
        self.assertEqual(build_fold_out_proposals(**ctx), [])

    # ── receiver gates ───────────────────────────────────────────────────────
    def test_all_or_nothing_no_partial_card(self):
        # Leg 11 (10:00) fits recv's 6-12 window; leg 12 (14:00) does not -> NO card,
        # never a partial fold.
        ctx = self._ctx([_slot(11, 10), _slot(12, 14)], [_slot(1, 6)],
                        driver_hours={self.thin.id: ALL_DAY, self.recv.id: (6, 12)})
        self.assertEqual(self._folds(ctx), [])

    def test_modal_window_parity(self):
        # Stub/capped window is permissive (start/end None) — the MODAL hours must
        # still gate the receiver, exactly like the greedy/trim/gap passes.
        ctx = self._ctx([_slot(11, 14)], [_slot(1, 6)],
                        driver_hours={self.thin.id: ALL_DAY, self.recv.id: (6, 12)})
        self.assertEqual(self._folds(ctx), [])

    def test_receiver_effective_span_gate(self):
        # recv 04:00-13:00 compact (no break credit). Adding an 18:00 leg -> ~14.5h
        # effective > 13.5 target. Raw stays under the 15h gate; eff must block.
        recv_slots = [_slot(i, h) for i, h in enumerate([4, 6, 8, 10, 12], 1)]
        ctx = self._ctx([_slot(11, 18)], recv_slots)
        self.assertEqual(self._folds(ctx), [])

    def test_receiver_raw_span_gate(self):
        # recv 04:00-12:00 with a 6h break -> credit caps at 5h, eff stays low.
        # Adding a 19:30 leg -> raw 16.5h > 15 must block even though eff ~11.5 passes
        # and the 17h max_hours cap would allow it.
        recv_slots = [_slot(1, 4), _slot(2, 11)]
        ctx = self._ctx([_slot(11, 19, 30)], recv_slots)
        self.assertEqual(self._folds(ctx), [])

    def test_occupancy_gate_blocks_share_partner_overlap(self):
        # recv shares his unit with partner (id 9999) whose 11:30 job overlaps the
        # 11:00 leg inside the 60-min pad -> sharers_conflict must block.
        partner_sched = _sched(9999, [_slot(50, 11, 30)], name="partner")
        ctx = self._ctx([_slot(11, 11)], [_slot(1, 6)],
                        sharer_partners={self.recv.id: {9999}})
        ctx["proposed_schedules"][9999] = partner_sched
        self.assertEqual(self._folds(ctx), [])

    def test_night_leg_never_folds_onto_flexible_receiver(self):
        # 00:30 pickup; recv is Flexible with no explicit night start -> night rule
        # (NIGHT_LEG_FLEX_BLOCK) must block the relocation -> no card.
        ctx = self._ctx([_slot(11, 0, 30)], [_slot(1, 4)],
                        flexible_drivers={self.recv.id},
                        capped_windows={self.thin.id: dict(W_RIGID),
                                        self.recv.id: dict(W_FLEX)})
        self.assertEqual(self._folds(ctx), [])

    def test_night_leg_folds_onto_explicit_night_start(self):
        # Same 00:30 leg; recv has an EXPLICIT start that covers hour 0 — explicitness
        # beats the night rule (founder boards legitimately start pre-3 AM).
        night_w = {"start": 0, "end": 23, "max_hours": 17.0, "flexible": False,
                   "night_exempt": False}
        ctx = self._ctx([_slot(11, 0, 30)], [_slot(1, 4)],
                        driver_hours={self.thin.id: (0, 23), self.recv.id: (0, 23)},
                        capped_windows={self.thin.id: dict(W_RIGID),
                                        self.recv.id: night_w})
        cards = self._folds(ctx)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["relocations"][0]["to_driver_id"], self.recv.id)

    def test_idle_receiver_excluded(self):
        # The only other working driver has NO legs — moving thin's day onto an idle
        # body just swaps who gets released, so THIN must not fold. (The idle
        # vehicle-holder himself legitimately gets a zero-relocation empty-day card.)
        ctx = self._ctx([_slot(11, 9)], [])
        cards = self._folds(ctx)
        self.assertNotIn(self.thin.id, [c["driver_id"] for c in cards])
        self.assertTrue(all(c["relocations"] == [] for c in cards))

    def test_tier_matched_receiver_preferred(self):
        # suv leg; both an suv receiver and a Van(14 Pax) receiver are feasible —
        # the tier-matched suv receiver must win (founder calibration).
        van_unit = FleetVehicle.objects.create(vehicle_number="004", vehicle_type=self.vt_van14,
                                               year=2022, make="Mercedes", model="Sprinter")
        recv_van = _mk_driver("recvvan", certified=self.vt_van14)
        DriverVehicleAssignment.objects.create(driver=recv_van, date=TARGET, vehicle=van_unit)
        ctx = self._ctx([_slot(11, 9)], [_slot(1, 6)])
        ctx["proposed_schedules"][recv_van.id] = _sched(recv_van.id, [_slot(2, 6)], name="recvvan")
        ctx["driver_hours"][recv_van.id] = ALL_DAY
        ctx["capped_windows"][recv_van.id] = dict(W_RIGID)
        ctx["drivers_by_id"][recv_van.id] = recv_van
        cards = self._folds(ctx)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["relocations"][0]["to_driver_id"], self.recv.id)

    # ── ranking / caps / determinism ─────────────────────────────────────────
    def test_max_proposals_and_ranking(self):
        # Three foldable thin drivers -> 2 cards (fewest legs / least revenue first)
        # + the "_fold_more" info card.
        thin2 = _mk_driver("thin2")
        thin3 = _mk_driver("thin3")
        u2 = FleetVehicle.objects.create(vehicle_number="007", vehicle_type=self.vt_suv,
                                         year=2023, make="Chevy", model="Suburban")
        u3 = FleetVehicle.objects.create(vehicle_number="008", vehicle_type=self.vt_suv,
                                         year=2023, make="Chevy", model="Suburban")
        DriverVehicleAssignment.objects.create(driver=thin2, date=TARGET, vehicle=u2)
        DriverVehicleAssignment.objects.create(driver=thin3, date=TARGET, vehicle=u3)
        ctx = self._ctx([_slot(11, 9)], [_slot(1, 5)],
                        extra_legs={21: _leg(21, 11), 31: _leg(31, 13), 32: _leg(32, 15)})
        ctx["proposed_schedules"][thin2.id] = _sched(thin2.id, [_slot(21, 11)], name="thin2")
        ctx["proposed_schedules"][thin3.id] = _sched(thin3.id, [_slot(31, 13), _slot(32, 15)],
                                                     name="thin3")
        ctx["final_assignments"].update({21: thin2.id, 31: thin3.id, 32: thin3.id})
        for d in (thin2, thin3):
            ctx["driver_hours"][d.id] = ALL_DAY
            ctx["capped_windows"][d.id] = dict(W_RIGID)
            ctx["drivers_by_id"][d.id] = d
        props = build_fold_out_proposals(**ctx)
        folds = [p for p in props if p["kind"] == "fold_out"]
        infos = [p for p in props if p["kind"] == "info"]
        self.assertEqual(len(folds), 2)
        self.assertEqual([f["leg_count"] for f in folds], [1, 1])   # 1-leg days fold first
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0]["signature"], "_fold_more")

    def test_deterministic(self):
        ctx = self._ctx([_slot(11, 9), _slot(12, 11)], [_slot(1, 6)])
        a = build_fold_out_proposals(**ctx)
        ctx2 = self._ctx([_slot(11, 9), _slot(12, 11)], [_slot(1, 6)])
        b = build_fold_out_proposals(**ctx2)
        self.assertEqual(a, b)

    def test_disabled_flag(self):
        from unittest.mock import patch
        import dispatching.fold_advisor as fa
        ctx = self._ctx([_slot(11, 9)], [_slot(1, 6)])
        with patch.object(fa, "FOLD_OUT_ENABLED", False):
            self.assertEqual(build_fold_out_proposals(**ctx), [])


class FoldAcceptApplyTests(TestCase):
    """The fold-out accept path's only DB write: clearing (and re-creating on undo)
    the folded driver's vehicle row through update_inhouse_vehicle_assignment.
    Pins the previously-untested vehicle_id=null delete branch."""

    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.unit = FleetVehicle.objects.create(vehicle_number="003", vehicle_type=cls.vt_suv,
                                               year=2023, make="Chevy", model="Suburban")
        cls.driver = _mk_driver("foldme")
        cls.staff = User.objects.create_user("boss3", password="x", is_staff=True)
        DriverVehicleAssignment.objects.create(driver=cls.driver, date=TARGET, vehicle=cls.unit)

    def _post(self, vehicle_id):
        self.client.force_login(self.staff)
        return self.client.post(
            reverse("update_inhouse_vehicle_assignment"),
            data=json.dumps({"driver_id": self.driver.id, "date": TARGET.isoformat(),
                             "vehicle_id": vehicle_id}),
            content_type="application/json")

    def test_accept_clears_dva_row(self):
        r = self._post(None)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["cleared"])
        self.assertFalse(DriverVehicleAssignment.objects.filter(
            driver=self.driver, date=TARGET).exists())

    def test_undo_recreates_dva_row(self):
        self._post(None)
        r = self._post(self.unit.id)
        self.assertEqual(r.status_code, 200)
        row = DriverVehicleAssignment.objects.get(driver=self.driver, date=TARGET)
        self.assertEqual(row.vehicle_id, self.unit.id)
