"""The AI assistant ships switched off.

Run with:  ./manage.py test ai_assistant.tests_settings
"""
from django.apps import apps
from django.conf import settings
from django.test import TestCase


class SettingsDefaults(TestCase):
    def test_the_app_is_installed(self):
        self.assertTrue(apps.is_installed("ai_assistant"))

    def test_both_switches_are_off_by_default(self):
        # Installing this code must change nothing until someone turns it on.
        self.assertFalse(settings.AI_ASSISTANT_ENABLED)
        self.assertFalse(settings.AI_ASSISTANT_LIVE_SEND)

    def test_the_model_is_pinned(self):
        self.assertEqual(settings.AI_ASSISTANT_MODEL, "claude-haiku-4-5")

    def test_the_operational_defaults_are_set(self):
        self.assertEqual(settings.AI_PROMPT_VERSION, "v1")
        self.assertEqual(settings.AI_HUMAN_TAKEOVER_MINUTES, 15)
        self.assertEqual(settings.AI_HISTORY_TURNS, 10)
        self.assertEqual(settings.AI_DAILY_SPEND_CAP_USD, 10)
