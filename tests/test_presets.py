"""The security presets as data: their shape, how a bot's permissions map to one, and what a change grants."""

from __future__ import annotations

import unittest
from unittest import mock

from alfred import bots, presets


class PresetDataTests(unittest.TestCase):
    def test_every_preset_has_text_and_only_names_real_permissions(self) -> None:
        self.assertEqual(list(presets.PRESET_PERMISSIONS), list(presets.PRESET_INFO))
        self.assertIn(presets.RECOMMENDED, presets.PRESET_PERMISSIONS)
        for key, permissions in presets.PRESET_PERMISSIONS.items():
            for name, value in permissions.items():
                self.assertIn(name, bots.PERMISSION_NAMES, key)
                self.assertNotIn(name, presets.FREEFORM, key)
                self.assertEqual(bots.clean_permission(mock.Mock(), name, value), value, (key, name))
            self.assertTrue(presets.PRESET_INFO[key].can and presets.PRESET_INFO[key].cannot)

    def test_presets_go_from_least_to_most(self) -> None:
        ordered = presets.keys()
        for lower, higher in zip(ordered, ordered[1:]):
            self.assertTrue(presets.gains(presets.PRESET_PERMISSIONS[lower], presets.PRESET_PERMISSIONS[higher]))
            self.assertEqual(presets.gains(presets.PRESET_PERMISSIONS[higher], presets.PRESET_PERMISSIONS[lower]), [])

    def test_basic_is_what_a_new_bot_gets_without_choosing(self) -> None:
        self.assertEqual(presets.detect({}), ("basic", "basic"))

    def test_the_dangerous_presets_warn(self) -> None:
        self.assertIn("⚠️", presets.card("power"))
        self.assertIn("⚠️⚠️", presets.card("full"))
        self.assertNotIn("⚠️", presets.card("basic"))
        self.assertIn("(recommended)", presets.card(presets.RECOMMENDED))

    def test_a_new_permission_falls_into_every_preset_at_its_default(self) -> None:
        names = bots.PERMISSION_NAMES + ("usage",)
        defaults = dict(bots.DEFAULT_PERMISSIONS, usage="off")
        with mock.patch.object(presets, "PERMISSION_NAMES", names), \
                mock.patch.object(presets, "DEFAULT_PERMISSIONS", defaults), \
                mock.patch.object(presets, "SWITCHES", bots.SWITCHES + ("usage",)):
            self.assertEqual(presets.resolve("power")["usage"], "off")
            self.assertEqual(presets.detect({"web": "on", "edit": "on"})[0], "assistant")
            self.assertEqual(presets.name_of({"web": "on", "edit": "on", "usage": "on"}),
                             "🎛 Custom (from ⭐ Assistant: + usage)")
            with mock.patch.dict(presets.PRESET_PERMISSIONS, {"power": dict(presets.PRESET_PERMISSIONS["power"], usage="on")}):
                self.assertEqual(presets.resolve("power")["usage"], "on")


class MatchingTests(unittest.TestCase):
    def test_bots_made_before_presets_are_recognised(self) -> None:
        # What the old Minimal, Standard and Broad buttons stored.
        self.assertEqual(presets.detect({})[0], "basic")
        self.assertEqual(presets.detect({"web": "on", "edit": "on"})[0], "assistant")
        old_broad = {"web": "on", "edit": "on", "browser": "on", "shell": "full", "mode": "acceptEdits"}
        self.assertEqual(presets.detect(old_broad)[0], "power")

    def test_extra_folders_and_rules_do_not_make_a_bot_custom(self) -> None:
        permissions = {"web": "on", "edit": "on", "folders": "/tmp/notes", "deny": "Bash(rm *)"}
        self.assertEqual(presets.name_of(permissions), "⭐ Assistant")
        applied = presets.apply("basic", permissions)
        self.assertEqual(applied, {"folders": "/tmp/notes", "deny": "Bash(rm *)"})

    def test_anything_else_is_custom_from_the_closest_preset(self) -> None:
        self.assertEqual(presets.name_of({"web": "on", "edit": "on", "browser": "on"}),
                         "🎛 Custom (from ⭐ Assistant: + browser)")
        self.assertEqual(presets.name_of({"web": "on"}), "🎛 Custom (from 🔒 Basic: + web)")
        # A known base wins over the closest one.
        self.assertEqual(presets.name_of({"web": "on"}, base="assistant"), "🎛 Custom (from ⭐ Assistant: − file edits)")

    def test_apply_stores_only_what_differs_from_the_defaults(self) -> None:
        self.assertEqual(presets.apply("assistant", {"browser": "on", "mode": "auto"}), {"web": "on", "edit": "on"})
        self.assertNotIn("sandbox", presets.apply("power", {}))  # on is the default
        self.assertEqual(presets.apply("full", {})["sandbox"], "off")

    def test_words_name_presets(self) -> None:
        for word, key in (("assistant", "assistant"), ("Full access", "full"), ("Standard", "assistant"),
                          ("mínimo", "basic"), ("broad", "power"), ("POWER", "power")):
            self.assertEqual(presets.find(word), key, word)
        self.assertIsNone(presets.find("web"))


class GainsTests(unittest.TestCase):
    def test_what_a_change_newly_allows(self) -> None:
        self.assertEqual(presets.gains({}, {"web": "on"}), ["+ web"])
        self.assertEqual(presets.gains({"web": "on"}, {}), [])
        self.assertEqual(presets.gains({"shell": "full"}, {"shell": "full", "sandbox": "off"}), ["− sandbox"])
        self.assertEqual(presets.gains({"mode": "auto"}, {"mode": "acceptEdits"}), [])
        self.assertEqual(presets.gains({}, {"mode": "bypassPermissions"}), ["mode: bypassPermissions"])
        # Custom with browser, to Assistant: only takes away.
        self.assertEqual(presets.gains({"web": "on", "edit": "on", "browser": "on"}, presets.PRESET_PERMISSIONS["assistant"]), [])


if __name__ == "__main__":
    unittest.main()
