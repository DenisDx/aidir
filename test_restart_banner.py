"""Regression test for the restart banner of the WebUI.

The reported symptom: the yellow "the service is restarting" banner appeared and
then disappeared while the service had not restarted yet.

Two causes, both in the front end:

  1. `apiGet` answered 401 -> `doLogout()` -> the interface switched to the
     login screen, and the banner lives in the application screen.
  2. The banner was derived from the last *successful* poll, so a service that
     stopped answering - or a poll that was already in flight when the operator
     clicked - cleared it.

The probe in test_restart_banner.js runs the real front-end sources under a
minimal DOM, drives a restart through every state the service can be in, and
reports what the banner shows.
"""

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
PROBE = ROOT / "test_restart_banner.js"


@unittest.skipUnless(shutil.which("node"), "node is required for the banner probe")
class RestartBannerTests(unittest.TestCase):
    """The banner must survive the whole restart and only then go away."""

    report = None

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ["node", str(PROBE)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(f"probe failed: {proc.stderr.strip()[:800]}")
        cls.report = json.loads(proc.stdout)

    def test_app_js_evaluates_completely(self):
        """A ReferenceError at load would abort every later statement."""
        self.assertIsNone(self.report.get("bootError"))

    def test_the_banner_appears_when_the_restart_is_requested(self):
        """The operator sees that the service is restarting."""
        step = self.report["steps"]["afterRequest"]
        self.assertTrue(step["visible"])
        self.assertTrue(step["busy"])

    def test_the_banner_survives_an_unreachable_service(self):
        """The service is down while it restarts: the banner must stay."""
        step = self.report["steps"]["afterUnreachable"]
        self.assertTrue(step["visible"])
        self.assertTrue(step["busy"])

    def test_a_401_during_the_restart_does_not_log_the_operator_out(self):
        """The session may not be lost in the middle of a restart."""
        step = self.report["steps"]["afterUnauthorized"]
        self.assertTrue(step["screen"], "the operator was sent to the login screen")
        self.assertTrue(step["visible"], "the banner disappeared during the restart")
        self.assertTrue(step["busy"])

    def test_the_banner_goes_away_when_the_service_is_back(self):
        """A restart that finished must not leave a stale banner."""
        step = self.report["steps"]["afterRecovery"]
        self.assertFalse(step["visible"])
        self.assertTrue(step["screen"], "the operator was sent to the login screen")
        self.assertFalse(step["busy"])


if __name__ == "__main__":
    unittest.main()
