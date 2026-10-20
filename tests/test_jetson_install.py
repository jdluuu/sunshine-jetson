"""Installer regression tests; no Jetson, sudo, network, or real services needed."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/jetson"))
import install
import manage

XRANDR = """Screen 0: minimum 8 x 8, current 1920 x 1080
DP-0 disconnected (normal left inverted right x axis y axis)
DP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis)
   1920x1080     60.00*+
HDMI-0 connected 1280x720-1280+0 (normal left inverted right x axis y axis)
HDMI-1 connected (normal left inverted right x axis y axis)
"""


class DisplayTests(unittest.TestCase):
    def test_disconnected_connector_still_counts_in_sunshine_index(self):
        self.assertEqual(manage.choose_output(XRANDR), "1")

    def test_negative_position_and_explicit_selection(self):
        self.assertEqual(manage.choose_output(XRANDR, "HDMI-0"), "2")
        self.assertEqual(manage.choose_output(XRANDR, "2"), "2")

    def test_unknown_connector_still_counts(self):
        self.assertEqual(manage.choose_output("Virtual-0 unknown connection\n" + XRANDR), "2")

    def test_disabled_missing_and_disconnected_outputs_are_rejected(self):
        for name in ("DP-0", "HDMI-1", "missing"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                manage.choose_output(XRANDR, name)
        with self.assertRaises(RuntimeError):
            manage.choose_output("")

    def test_no_primary_falls_back_to_first_active(self):
        self.assertEqual(manage.choose_output(XRANDR.replace(" primary", "")), "1")

    def test_configuration_preserves_unrelated_user_settings(self):
        original = "# encoder = software\n encoder = nvenc\nencoder=software\nbitrate = 10000\n"
        updated = manage.update_config(original, {"encoder": "jetson", "capture": "x11"})
        self.assertIn("# encoder = software", updated)
        self.assertIn("bitrate = 10000", updated)
        self.assertEqual(updated.count("\nencoder = jetson\n"), 1)
        self.assertNotIn("encoder=software", updated)
        self.assertEqual(manage.update_config(updated, {"encoder": "jetson", "capture": "x11"}), updated)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sunshine installer ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "installation"
        self.stage = self.root / "releases/new"
        self.stage.mkdir(parents=True)
        self.old = self.root / "releases/old"
        self.old.mkdir()
        (self.root / "current").symlink_to(self.old)
        (self.root / "config").mkdir()
        (self.root / "config/sunshine_state.json").write_text("synthetic-pairing-fixture")
        (self.root / "config/sunshine.conf").write_text("audio_sink = user-choice\n")
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_upgrade_preserves_pairing_and_backs_up_configuration(self):
        with patch.dict(os.environ, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}), \
                patch.object(install, "stop"), patch.object(install, "start") as start, \
                patch.object(install, "own_pid", return_value=None), \
                patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            install.deploy(self.stage, self.root, "DP-1", False)
        self.assertEqual((self.root / "current").resolve(), self.stage)
        self.assertEqual((self.root / "previous").resolve(), self.old)
        self.assertEqual((self.root / "config/sunshine_state.json").read_text(), "synthetic-pairing-fixture")
        self.assertEqual((self.root / "backups/new/sunshine.conf").read_text(), "audio_sink = user-choice\n")
        self.assertEqual(json.loads((self.root / "preferences.json").read_text())["output"], "DP-1")
        start.assert_called_once_with(self.root)

    def test_failed_launch_restores_previous_program(self):
        with patch.dict(os.environ, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}), \
                patch.object(install, "stop"), \
                patch.object(install, "start", side_effect=RuntimeError("no encoder")), \
                patch.object(install, "own_pid", return_value=None), \
                patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(RuntimeError, "no encoder"):
                install.deploy(self.stage, self.root, None, False)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertEqual((self.root / "config/sunshine_state.json").read_text(), "synthetic-pairing-fixture")

    def test_existing_other_sunshine_is_not_stopped_or_autostarted(self):
        with patch.dict(os.environ, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
                                    "XDG_CONFIG_HOME": str(self.root / "xdg")}), \
                patch.object(install, "stop"), patch.object(install, "start") as start, \
                patch.object(install, "own_pid", return_value=None), \
                patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            install.deploy(self.stage, self.root, None, True)
        start.assert_not_called()
        self.assertFalse((self.root / "xdg/autostart/sunshine-jetson.desktop").exists())

    def test_rollback_swaps_versions_without_replacing_credentials(self):
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(self.stage)
        (self.root / "previous").symlink_to(self.old)
        with patch.object(manage, "stop"):
            manage.rollback(self.root)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertEqual((self.root / "previous").resolve(), self.stage)
        self.assertEqual((self.root / "config/sunshine_state.json").read_text(), "synthetic-pairing-fixture")


if __name__ == "__main__":
    unittest.main()
