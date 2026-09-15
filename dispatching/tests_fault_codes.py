"""Fault codes in English: can the car go out, and does it ever guess wrongly.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fault_codes

The rule this module lives or dies by: it may say "I don't know, ring the shop",
but it may never say something confident and wrong about a car carrying guests.
"""
from django.test import SimpleTestCase

from dispatching import fault_codes as fc


class KnownCodeTests(SimpleTestCase):
    def test_every_code_this_fleet_has_thrown_has_a_real_answer(self):
        """The eight codes in production on 2026-09-15. A fault arrives at 6 AM;
        the answer has to already exist."""
        for code in ("P202E", "P208E", "P20EA", "P20F4", "P0420",
                     "P0301", "P0305", "P0299"):
            meaning = fc.explain(code)
            self.assertTrue(meaning["confident"], code)
            self.assertTrue(meaning["plain"], code)
            self.assertTrue(meaning["consequence"], code)
            self.assertTrue(meaning["system"], code)

    def test_a_system_name_never_contains_a_dash(self):
        """System names land inside "#008 — {system}". One with its own dash
        renders "#008 — Ignition — misfire"."""
        for code, meaning in fc.KNOWN.items():
            self.assertNotIn("—", meaning["system"], code)
            self.assertNotIn(" - ", meaning["system"], code)

    def test_the_verdict_matches_what_the_fault_actually_does(self):
        # Emissions: runs fine, fails the test, book it.
        self.assertEqual(fc.explain("P0420")["verdict"], fc.BOOK)
        self.assertEqual(fc.explain("P202E")["verdict"], fc.BOOK)
        # A loose fuel cap does not stop a car.
        self.assertEqual(fc.explain("P0455")["verdict"], fc.DRIVE)
        # Down on power, merging onto a highway with guests.
        self.assertEqual(fc.explain("P0299")["verdict"], fc.HOLD)
        # Misfiring on every cylinder is not a "watch".
        self.assertEqual(fc.explain("P0300")["verdict"], fc.HOLD)

    def test_every_cylinder_misfire_is_named_by_its_cylinder(self):
        for n in (1, 5, 12):
            meaning = fc.explain(f"P03{n:02d}")
            self.assertTrue(meaning["confident"])
            self.assertIn(f"Cylinder {n}", meaning["plain"])
            self.assertIn("FLASHING", meaning["note"])

    def test_a_code_is_matched_regardless_of_case_or_padding(self):
        self.assertEqual(fc.explain("  p0299 ")["verdict"], fc.HOLD)
        self.assertEqual(fc.explain("p0299")["system"], fc.explain("P0299")["system"])


class UnknownCodeTests(SimpleTestCase):
    def test_an_unknown_code_is_read_from_its_shape_and_admits_it(self):
        meaning = fc.explain("P1A2B", "Some ECU string", "warning")
        self.assertFalse(meaning["confident"])
        self.assertTrue(meaning["system"])
        self.assertIn("ring the shop", meaning["consequence"])

    def test_a_chassis_code_is_assumed_serious(self):
        """Brakes, steering and suspension get the benefit of the doubt in the
        careful direction."""
        self.assertEqual(fc.explain("C0035")["verdict"], fc.HOLD)

    def test_nonsense_is_not_dressed_up_as_an_answer(self):
        meaning = fc.explain("BANANA", "whatever the car said")
        self.assertFalse(meaning["confident"])
        self.assertIn("Not a code we recognise", meaning["plain"])

    def test_an_empty_code_does_not_explode(self):
        for value in ("", None, "   "):
            meaning = fc.explain(value)
            self.assertFalse(meaning["confident"])
            self.assertIn(meaning["verdict"], (fc.DRIVE, fc.BOOK, fc.HOLD))

    def test_the_cars_own_severity_can_escalate_a_guess_but_never_soften_one(self):
        # A guess + the car shouting = treat it as serious.
        self.assertEqual(fc.explain("P1A2B", "", "critical")["verdict"], fc.HOLD)
        # A known "fine to run" stays that way whatever the vehicle flags. The
        # table was written for these vehicles; the severity flag is generic.
        self.assertEqual(fc.explain("P0455", "", "critical")["verdict"], fc.DRIVE)

    def test_the_raw_description_is_always_carried_through(self):
        """It is what gets read down the phone to the shop."""
        meaning = fc.explain("P0299", "Turbocharger/Supercharger 'A' Underboost")
        self.assertIn("Underboost", meaning["raw"])


class SeveralCodesAtOnceTests(SimpleTestCase):
    def _def_pack(self):
        return [fc.explain(c) for c in ("P202E", "P208E", "P20EA", "P20F4")]

    def test_the_worst_verdict_wins(self):
        mixed = [fc.explain("P0455"), fc.explain("P0420"), fc.explain("P0299")]
        self.assertEqual(fc.worst(mixed)["verdict"], fc.HOLD)

    def test_four_def_codes_are_one_problem(self):
        """This fleet's Sprinters throw DEF faults in packs of four. Four lines
        that are one problem is the alert-fatigue failure the desk removed."""
        line = fc.summarise(self._def_pack())
        self.assertEqual(line, "It will run, but book the shop.")
        self.assertNotIn("4", line)

    def test_a_second_system_is_named(self):
        line = fc.summarise([fc.explain("P0301"), fc.explain("P0420")])
        self.assertIn("Also", line)
        self.assertIn("catalytic converter", line.lower())

    def test_many_systems_collapse_to_a_count(self):
        line = fc.summarise([fc.explain(c) for c in ("P0301", "P0420", "P0299", "P0455")])
        self.assertIn("other systems", line)

    def test_the_lead_is_the_answer_not_the_system(self):
        """The row title already names the system. The sentence under it is
        there to answer "can it go out", so it leads with that."""
        for codes, expected in (
            (("P0299",), "Don't send it out."),
            (("P0420",), "It will run, but book the shop."),
            (("P0455",), "Fine to run."),
        ):
            line = fc.summarise([fc.explain(c) for c in codes])
            self.assertTrue(line.startswith(expected), (codes, line))

    def test_nothing_at_all_is_an_empty_string(self):
        self.assertEqual(fc.summarise([]), "")
        self.assertIsNone(fc.worst([]))
