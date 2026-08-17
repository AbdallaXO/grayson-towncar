"""Bulk vehicle editing from the fleet list.

Run with:  ./manage.py test dispatching.tests_fleet_bulk

This endpoint exists for data entry — a stack of permit paperwork, a policy that
renewed for the whole fleet on one date — so the properties worth protecting are
the ones that make it safe to point at every car at once:

  * ONLY WHAT WAS ASKED FOR: a key absent from the payload is not written. The
    failure this prevents is a blank form input quietly clearing a date, or a
    permit, on twelve cars nobody was looking at.
  * ALL OR NOTHING: if any selected unit fails validation, nothing is written.
    Half a batch applied is the worst outcome for someone typing from paper —
    they can't tell what landed and what didn't.
  * THE ERROR NAMES THE UNIT: "#12: the out-of-service end date is before the
    start date" is actionable. "Invalid input" is not.
  * PER-CAR IDENTITY IS NOT BULK-EDITABLE: one transponder number stamped onto
    twelve cars is twelve wrong toll attributions, and a bulk note overwrite
    destroys writing nobody can get back. Both are refused BY NAME rather than
    silently dropped.
"""
import json
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import FleetVehicle

TODAY = timezone.localdate()


class _BulkFixture(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            "bulk_dispatcher", "b@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)
        self.a = self.unit("001")
        self.b = self.unit("002")
        self.c = self.unit("003")

    def unit(self, number, **kw):
        return FleetVehicle.objects.create(
            vehicle_number=number, year=2022, make="Chevrolet", model="Suburban",
            **kw)

    def bulk(self, vehicles, fields):
        return self.client.post(
            reverse("fleet_bulk_update"),
            data=json.dumps({
                "vehicle_ids": [v.pk for v in vehicles],
                "fields": fields,
            }),
            content_type="application/json",
        )


class BulkPermitTests(_BulkFixture):
    """The case the feature was built for: a decal run bought for several cars."""

    def test_grants_one_permit_to_every_selected_vehicle(self):
        resp = self.bulk([self.a, self.b], {
            "permit_mco": True, "permit_mco_expires_on": "2027-01-31",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["updated"], 2)
        for vehicle in (self.a, self.b):
            vehicle.refresh_from_db()
            self.assertTrue(vehicle.permit_mco)
            self.assertEqual(vehicle.permit_mco_expires_on, date(2027, 1, 31))

    def test_an_unselected_vehicle_is_never_touched(self):
        self.bulk([self.a, self.b], {"permit_mco": True})
        self.c.refresh_from_db()
        self.assertFalse(self.c.permit_mco)

    def test_a_permit_not_in_the_payload_is_left_alone(self):
        """The core safety property: bulk-setting MCO must not clear Sanford."""
        self.a.permit_sanford = True
        self.a.permit_sanford_expires_on = date(2026, 11, 1)
        self.a.save()

        self.bulk([self.a], {"permit_mco": True})

        self.a.refresh_from_db()
        self.assertTrue(self.a.permit_mco)
        self.assertTrue(self.a.permit_sanford)
        self.assertEqual(self.a.permit_sanford_expires_on, date(2026, 11, 1))

    def test_revoking_a_permit_clears_its_expiry_too(self):
        """A permit that comes back must not inherit the old decal's date."""
        for vehicle in (self.a, self.b):
            vehicle.permit_port_canaveral = True
            vehicle.permit_port_canaveral_expires_on = date(2026, 9, 30)
            vehicle.save()

        self.bulk([self.a, self.b], {"permit_port_canaveral": False})

        for vehicle in (self.a, self.b):
            vehicle.refresh_from_db()
            self.assertFalse(vehicle.permit_port_canaveral)
            self.assertIsNone(vehicle.permit_port_canaveral_expires_on)

    def test_a_permit_can_be_granted_with_no_expiry(self):
        self.bulk([self.a], {"permit_sanford": True, "permit_sanford_expires_on": ""})
        self.a.refresh_from_db()
        self.assertTrue(self.a.permit_sanford)
        self.assertIsNone(self.a.permit_sanford_expires_on)

    def test_several_permits_in_one_pass(self):
        self.bulk([self.a, self.b, self.c], {
            "permit_mco": True, "permit_mco_expires_on": "2027-03-01",
            "permit_sanford": True, "permit_sanford_expires_on": "",
        })
        for vehicle in (self.a, self.b, self.c):
            vehicle.refresh_from_db()
            self.assertTrue(vehicle.permit_mco)
            self.assertTrue(vehicle.permit_sanford)
            self.assertEqual(vehicle.permit_mco_expires_on, date(2027, 3, 1))

    def test_an_expired_bulk_grant_still_reads_as_no_permit(self):
        """The grant writes what it was told; permits() decides what it means."""
        self.bulk([self.a], {
            "permit_mco": True,
            "permit_mco_expires_on": (TODAY - timedelta(days=1)).isoformat(),
        })
        self.a.refresh_from_db()
        row = self.a.permit_for_location("MCO Terminal")
        self.assertTrue(row["expired"])
        self.assertFalse(row["valid"])

    def test_a_bad_date_is_refused_and_names_the_permit(self):
        resp = self.bulk([self.a], {
            "permit_mco": True, "permit_mco_expires_on": "31/01/2027"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("MCO permit expiry", resp.json()["error"])
        self.a.refresh_from_db()
        self.assertFalse(self.a.permit_mco)


class BulkComplianceTests(_BulkFixture):
    def test_sets_one_renewal_date_across_the_batch(self):
        self.bulk([self.a, self.b, self.c],
                  {"insurance_expires_on": "2027-06-30"})
        for vehicle in (self.a, self.b, self.c):
            vehicle.refresh_from_db()
            self.assertEqual(vehicle.insurance_expires_on, date(2027, 6, 30))

    def test_an_empty_value_clears_the_date(self):
        """Explicit clearing is legitimate — it's the ABSENT key that's a no-op."""
        self.a.next_inspection_on = date(2026, 10, 1)
        self.a.save()
        self.bulk([self.a], {"next_inspection_on": ""})
        self.a.refresh_from_db()
        self.assertIsNone(self.a.next_inspection_on)

    def test_setting_insurance_leaves_registration_alone(self):
        self.a.registration_expires_on = date(2026, 12, 15)
        self.a.save()
        self.bulk([self.a], {"insurance_expires_on": "2027-06-30"})
        self.a.refresh_from_db()
        self.assertEqual(self.a.registration_expires_on, date(2026, 12, 15))

    def test_toll_network_applies_to_the_batch(self):
        self.bulk([self.a, self.b], {"transponder_type": "epass"})
        for vehicle in (self.a, self.b):
            vehicle.refresh_from_db()
            self.assertEqual(vehicle.transponder_type, "epass")

    def test_an_unknown_toll_network_is_refused(self):
        resp = self.bulk([self.a], {"transponder_type": "ezpass"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transponder type", resp.json()["error"])


class BulkOutOfServiceTests(_BulkFixture):
    """The one bulk field that removes units from the scheduling pool."""

    def test_takes_a_batch_out_of_service_with_a_reason(self):
        self.bulk([self.a, self.b], {
            "out_of_service_from": TODAY.isoformat(),
            "out_of_service_until": (TODAY + timedelta(days=3)).isoformat(),
            "out_of_service_reason": "Recall — dealer",
        })
        for vehicle in (self.a, self.b):
            vehicle.refresh_from_db()
            self.assertTrue(vehicle.is_out_of_service_on(TODAY))
            self.assertIn("Recall", vehicle.out_of_service_label(TODAY))

    def test_putting_a_batch_back_in_service_clears_the_whole_window(self):
        for vehicle in (self.a, self.b):
            vehicle.out_of_service_from = TODAY
            vehicle.out_of_service_until = TODAY + timedelta(days=5)
            vehicle.out_of_service_reason = "Transmission"
            vehicle.save()

        self.bulk([self.a, self.b], {
            "out_of_service_from": "", "out_of_service_until": "",
            "out_of_service_reason": "",
        })

        for vehicle in (self.a, self.b):
            vehicle.refresh_from_db()
            self.assertFalse(vehicle.is_out_of_service_on(TODAY))
            self.assertEqual(vehicle.out_of_service_reason, "")

    def test_a_backwards_window_is_refused_and_nothing_is_written(self):
        resp = self.bulk([self.a, self.b], {
            "out_of_service_from": (TODAY + timedelta(days=5)).isoformat(),
            "out_of_service_until": TODAY.isoformat(),
        })
        self.assertEqual(resp.status_code, 400)
        for vehicle in (self.a, self.b):
            vehicle.refresh_from_db()
            self.assertIsNone(vehicle.out_of_service_from)

    def test_the_refusal_names_the_offending_unit(self):
        """One bad car in a batch of twelve has to be findable.

        Both units get an end date; only #002's existing start date sits after
        it, so #002 is the one that must be named.
        """
        self.a.out_of_service_from = TODAY
        self.a.save()
        self.b.out_of_service_from = TODAY + timedelta(days=10)
        self.b.save()

        resp = self.bulk([self.a, self.b], {
            "out_of_service_until": (TODAY + timedelta(days=2)).isoformat(),
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("#002", resp.json()["error"])
        # ...and the unit that was fine is still untouched.
        self.a.refresh_from_db()
        self.assertIsNone(self.a.out_of_service_until)

    def test_one_bad_unit_blocks_the_whole_batch(self):
        """Atomicity. A half-applied batch is unrecoverable by eye."""
        self.a.out_of_service_from = TODAY
        self.a.save()
        resp = self.bulk([self.a, self.b], {
            "out_of_service_until": (TODAY + timedelta(days=4)).isoformat(),
        })
        # #002 has no start date, so the end date would gate nothing.
        self.assertEqual(resp.status_code, 400)
        self.a.refresh_from_db()
        self.assertIsNone(self.a.out_of_service_until)
        self.assertIn("Nothing was changed", resp.json()["error"])


class BulkRefusalTests(_BulkFixture):
    """What bulk editing is deliberately not allowed to do."""

    def test_the_transponder_number_cannot_be_bulk_set(self):
        resp = self.bulk([self.a, self.b], {"transponder_number": "0093412"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transponder_number", resp.json()["error"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.transponder_number, "")

    def test_notes_cannot_be_bulk_overwritten(self):
        self.a.notes = "Cracked windscreen, booked in"
        self.a.save()
        resp = self.bulk([self.a, self.b], {"notes": "checked"})
        self.assertEqual(resp.status_code, 400)
        self.a.refresh_from_db()
        self.assertEqual(self.a.notes, "Cracked windscreen, booked in")

    def test_samsara_owned_columns_are_refused(self):
        resp = self.bulk([self.a], {"vin": "1GNSKJKC5PR123456"})
        self.assertEqual(resp.status_code, 400)
        self.a.refresh_from_db()
        self.assertEqual(self.a.vin, "")

    def test_an_empty_selection_is_refused(self):
        resp = self.bulk([], {"permit_mco": True})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least one vehicle", resp.json()["error"])

    def test_an_empty_field_set_is_refused(self):
        resp = self.bulk([self.a], {})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Nothing to change", resp.json()["error"])

    def test_a_selection_of_stale_ids_is_refused_rather_than_silently_ok(self):
        pk = self.a.pk
        self.a.delete()
        resp = self.client.post(
            reverse("fleet_bulk_update"),
            data=json.dumps({"vehicle_ids": [pk], "fields": {"permit_mco": True}}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Reload", resp.json()["error"])

    def test_a_vanished_vehicle_is_reported_not_absorbed(self):
        gone = self.unit("099").pk
        FleetVehicle.objects.filter(pk=gone).delete()
        resp = self.client.post(
            reverse("fleet_bulk_update"),
            data=json.dumps({
                "vehicle_ids": [self.a.pk, gone],
                "fields": {"permit_mco": True},
            }),
            content_type="application/json")
        body = resp.json()
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["missing"], 1)

    def test_a_selection_larger_than_any_fleet_is_refused(self):
        resp = self.client.post(
            reverse("fleet_bulk_update"),
            data=json.dumps({
                "vehicle_ids": list(range(1, 700)),
                "fields": {"permit_mco": True},
            }),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_junk_json_never_500s(self):
        resp = self.client.post(
            reverse("fleet_bulk_update"), data="{not json",
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_a_non_staff_user_cannot_bulk_edit(self):
        self.client.force_login(
            User.objects.create_user("driver_joe", "j@example.com", "pw"))
        resp = self.bulk([self.a], {"permit_mco": True})
        self.assertNotEqual(resp.status_code, 200)
        self.a.refresh_from_db()
        self.assertFalse(self.a.permit_mco)

    def test_get_is_not_allowed(self):
        self.assertEqual(
            self.client.get(reverse("fleet_bulk_update")).status_code, 405)


class BulkEditorRenderTests(_BulkFixture):
    """The list page has to actually offer the thing."""

    def test_the_list_offers_a_checkbox_per_vehicle(self):
        resp = self.client.get(reverse("fleet_list"))
        self.assertContains(resp, 'class="form-check-input fleet-pick"', count=3)

    def test_every_permit_appears_in_the_bulk_form(self):
        """Driven by FleetVehicle.PERMITS, so a fourth permit needs no edit here."""
        resp = self.client.get(reverse("fleet_list"))
        for key, _label, _category in FleetVehicle.PERMITS:
            self.assertContains(resp, f'id="b_permit_{key}"')

    def test_the_single_vehicle_form_still_saves_after_the_refactor(self):
        """_collect_vehicle_fields is shared — this is the other caller."""
        resp = self.client.post(
            reverse("fleet_update_details", args=[self.a.pk]),
            data=json.dumps({
                "notes": "Kept working",
                "transponder_number": "0093412",
                "permit_mco": True,
                "permit_mco_expires_on": "2027-02-28",
            }),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.a.refresh_from_db()
        self.assertEqual(self.a.notes, "Kept working")
        self.assertEqual(self.a.transponder_number, "0093412")
        self.assertEqual(self.a.permit_mco_expires_on, date(2027, 2, 28))
