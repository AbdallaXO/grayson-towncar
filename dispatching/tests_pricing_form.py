"""Pricing step (step 5) of the dispatcher booking wizard.

Dispatchers routinely clear the optional money boxes instead of typing 0. An
emptied optional DecimalField arrives in cleaned_data as None -- the key exists,
so a dict default never fires -- which used to blow up the total with a
TypeError and 500 the whole reservation submission.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from dispatching.forms import DispatcherPricingForm


class DispatcherPricingFormTotalTests(SimpleTestCase):
    def test_cleared_optional_boxes_are_treated_as_zero(self):
        form = DispatcherPricingForm({
            "manual_base_price": "125.00",
            "additional_charges": "",
            "gratuity_option": "none",
            "gratuity_amount": "",
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["additional_charges"], Decimal("0.00"))
        self.assertEqual(form.cleaned_data["gratuity_amount"], Decimal("0.00"))
        self.assertEqual(form.cleaned_data["total_price"], Decimal("125.00"))

    def test_omitted_optional_fields_are_treated_as_zero(self):
        form = DispatcherPricingForm({"manual_base_price": "100"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["total_price"], Decimal("100.00"))

    def test_total_sums_base_extras_and_gratuity(self):
        form = DispatcherPricingForm({
            "manual_base_price": "125.00",
            "additional_charges": "20.00",
            "gratuity_option": "20",
            "gratuity_amount": "25.00",
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["total_price"], Decimal("170.00"))

    def test_missing_base_price_is_a_field_error_not_a_crash(self):
        form = DispatcherPricingForm({"additional_charges": "", "gratuity_amount": ""})

        self.assertFalse(form.is_valid())
        self.assertIn("manual_base_price", form.errors)
