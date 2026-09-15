"""CustomerForm.save(): which customer row a booking or an edit lands on.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test reservations.tests_customer_form

The production crash this pins (2026-09-15): editing a reservation 500ed with
``Customer.MultipleObjectsReturned`` because the save ran ``get()`` against a
table that already held 34 groups of byte-identical customer rows.

The other half matters more. A household books on ONE email and ONE phone under
different passenger names — 398 email+phone pairs in production look like that,
one of them carrying three different people. Narrowing the match to email+phone
would fold them into a single customer and rewrite the name on their past trips,
so the test below exists to make that regression fail loudly.
"""
from django.test import TestCase

from reservations.forms import CustomerForm
from reservations.models import Customer

FIELDS = {
    "first_name": "Miranda",
    "last_name": "Talley",
    "email": "household@example.com",
    "phone_number": "407-555-0100",
    "zipcode": "46835",
}


def _form(**overrides):
    return CustomerForm({**FIELDS, **overrides})


class CustomerFormSaveTests(TestCase):
    def test_a_new_customer_is_created(self):
        form = _form()
        self.assertTrue(form.is_valid(), form.errors)
        customer = form.save()
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(customer.first_name, "Miranda")
        self.assertFalse(customer.is_returning)

    def test_the_same_person_booking_again_reuses_their_row(self):
        first = _form()
        self.assertTrue(first.is_valid(), first.errors)
        original = first.save()

        second = _form()
        self.assertTrue(second.is_valid(), second.errors)
        again = second.save()

        self.assertEqual(again.pk, original.pk)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertTrue(again.is_returning)

    def test_duplicate_rows_no_longer_500_the_editor(self):
        """The production bug. Two byte-identical rows made get() raise, and the
        whole reservation edit came back a 500. The oldest row wins instead."""
        older = Customer.objects.create(**FIELDS)
        Customer.objects.create(**FIELDS)
        self.assertEqual(Customer.objects.count(), 2)

        form = _form()
        self.assertTrue(form.is_valid(), form.errors)
        chosen = form.save()          # used to raise MultipleObjectsReturned

        self.assertEqual(chosen.pk, older.pk)
        self.assertEqual(Customer.objects.count(), 2)

    def test_the_choice_is_deterministic_across_repeats(self):
        older = Customer.objects.create(**FIELDS)
        Customer.objects.create(**FIELDS)
        Customer.objects.create(**FIELDS)
        picked = set()
        for _ in range(4):
            form = _form()
            self.assertTrue(form.is_valid(), form.errors)
            picked.add(form.save().pk)
        self.assertEqual(picked, {older.pk})

    def test_a_household_sharing_an_email_and_phone_stays_separate_people(self):
        """Miranda and Jeff book on the same email and phone. They are two
        passengers, not one customer with a corrected name."""
        miranda = _form()
        self.assertTrue(miranda.is_valid(), miranda.errors)
        one = miranda.save()

        jeff = _form(first_name="Jeff", last_name="Munch", zipcode="93908")
        self.assertTrue(jeff.is_valid(), jeff.errors)
        two = jeff.save()

        self.assertNotEqual(one.pk, two.pk)
        self.assertEqual(Customer.objects.count(), 2)
        one.refresh_from_db()
        self.assertEqual(one.first_name, "Miranda")
        self.assertEqual(one.last_name, "Talley")

    def test_a_different_zip_on_the_same_name_is_a_different_row(self):
        """Zip+4 versus zip — the shape that produced most of the real
        duplicates. Still two rows, because collapsing them would silently
        rewrite an address."""
        base = _form()
        self.assertTrue(base.is_valid(), base.errors)
        one = base.save()

        plus_four = _form(zipcode="46835-5037")
        self.assertTrue(plus_four.is_valid(), plus_four.errors)
        two = plus_four.save()

        self.assertNotEqual(one.pk, two.pk)

    def test_editing_a_bound_customer_does_not_touch_the_stored_row(self):
        """The edit path binds the form to reservation.customer. Correcting a
        name must not rewrite that shared row under every other reservation it
        already carries — it resolves to its own customer instead."""
        stored = Customer.objects.create(**FIELDS)
        form = CustomerForm(
            {**FIELDS, "first_name": "Jeff", "last_name": "Munch"}, instance=stored)
        self.assertTrue(form.is_valid(), form.errors)
        result = form.save()

        stored.refresh_from_db()
        self.assertEqual(stored.first_name, "Miranda")
        self.assertNotEqual(result.pk, stored.pk)
