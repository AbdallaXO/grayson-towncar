"""
Tests for Small World Big Fun (VIP) confirmation handling:
- VIP detection by agency name (linked Agency or free-text agency_name)
- VIP confirmations omit the automated "do not reply" notice (a dispatcher sends
  those by hand as a RingCentral group), but keep the office-contact line.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from dispatching.confirmation_sms import (
    _agent_is_vip,
    is_vip_leg,
    get_confirmation_message,
    OFFICE_PHONE,
)


def _agent(agency_name=None, linked_agency_name=None):
    """Build a stub TravelAgent. linked_agency_name → has an Agency FK."""
    agency = SimpleNamespace(name=linked_agency_name) if linked_agency_name is not None else None
    return SimpleNamespace(
        agency_id=1 if linked_agency_name is not None else None,
        agency=agency,
        agency_name=agency_name,
    )


class VipDetectionTests(SimpleTestCase):
    def test_linked_agency_match(self):
        self.assertTrue(_agent_is_vip(_agent(linked_agency_name="Small World Big Fun")))

    def test_free_text_agency_match(self):
        self.assertTrue(_agent_is_vip(_agent(agency_name="Small World Big Fun")))

    def test_case_insensitive_and_suffix(self):
        self.assertTrue(_agent_is_vip(_agent(agency_name="  small world big fun travel ")))

    def test_linked_agency_precedence(self):
        # Linked agency name wins (matches leg_to_row display logic) and matches.
        self.assertTrue(_agent_is_vip(_agent(agency_name="x", linked_agency_name="Small World Big Fun")))

    def test_non_vip_agency(self):
        self.assertFalse(_agent_is_vip(_agent(agency_name="Acme Travel")))

    def test_none_agent(self):
        self.assertFalse(_agent_is_vip(None))

    def test_blank_agency(self):
        self.assertFalse(_agent_is_vip(_agent(agency_name="")))

    def test_is_vip_leg(self):
        vip = SimpleNamespace(reservation=SimpleNamespace(travel_agent=_agent(agency_name="Small World Big Fun")))
        self.assertTrue(is_vip_leg(vip))
        non = SimpleNamespace(reservation=SimpleNamespace(travel_agent=_agent(agency_name="Other")))
        self.assertFalse(is_vip_leg(non))
        self.assertFalse(is_vip_leg(SimpleNamespace(reservation=None)))


class FooterTests(SimpleTestCase):
    def _leg(self, trip_type="return", override=""):
        return SimpleNamespace(
            confirmation_sms_override=override,
            get_trip_type=lambda: trip_type,
        )

    def _row(self, is_vip):
        return {
            "first_name": "Jane",
            "pickup_time": "9:00 AM",
            "pickup_location": "Hyatt Regency, Orlando",
            "dropoff_location": "Orlando International Airport (MCO)",
            "is_vip": is_vip,
        }

    def test_regular_includes_automated_notice(self):
        msg = get_confirmation_message(self._leg(), self._row(is_vip=False))
        self.assertIn("automated message", msg)
        self.assertIn("do not reply", msg)
        self.assertIn(OFFICE_PHONE, msg)

    def test_vip_omits_automated_notice_but_keeps_office(self):
        msg = get_confirmation_message(self._leg(), self._row(is_vip=True))
        self.assertNotIn("automated message", msg)
        self.assertNotIn("do not reply", msg)
        self.assertIn(OFFICE_PHONE, msg)  # office-contact line stays

    def test_vip_override_also_omits_notice(self):
        msg = get_confirmation_message(self._leg(override="Custom body here"), self._row(is_vip=True))
        self.assertIn("Custom body here", msg)
        self.assertNotIn("automated message", msg)
        self.assertIn(OFFICE_PHONE, msg)
