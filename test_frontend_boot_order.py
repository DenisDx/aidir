"""Boot-order regression test for the settings editor.

The reported symptom was a document selector with no options, so nothing could be
chosen. No HTTP test could see it: every request such a test makes is already
authenticated, so `/api/settings-info` always answers 200. Three defects sat
behind the symptom:

  1. `bindTaskViewerUi()` was called at script load but no longer existed, because
     the legacy editor removal took its definition with it. The ReferenceError
     aborted app.js at that line.
  2. The editor was initialised at script load, before the login and before the
     `/api/auth/me` probe, so `/api/settings-info` answered 401 with no retry.
  3. `apiGet` resolves the parsed body itself and returns null on failure, so the
     document list has to be read from the body, not from a `.data` envelope.

The probe in test_frontend_boot_probe.js runs the real front-end sources under a
minimal DOM, in the order index.html loads them, and reports what the page shows
before and after authentication.
"""

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
PROBE = ROOT / "test_frontend_boot_probe.js"


@unittest.skipUnless(shutil.which("node"), "node is required for the boot probe")
class FrontendBootOrderTests(unittest.TestCase):
    """The settings page must end up populated, and must not be asked for too early."""

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
        """Defect 1: a ReferenceError at load aborts every later statement."""
        self.assertIsNone(
            self.report["bootError"],
            f"app.js aborted while loading: {self.report['bootError']}",
        )

    def test_every_boot_helper_is_defined(self):
        """Each function the boot block calls must actually exist."""
        source = (ROOT / "webui" / "frontend" / "app.js").read_text(encoding="utf-8")
        boot_start = source.index("// ── Boot ──")
        boot_end = source.index("// ── TEST LLM ──")
        block = "\n".join(
            line for line in source[boot_start:boot_end].splitlines()
            if not line.strip().startswith("//")
        )
        called = [line.strip().rstrip("();").strip() for line in block.splitlines()
                  if line.strip().endswith("();")]
        self.assertTrue(called, "the boot block calls nothing, which cannot be right")
        for name in called:
            self.assertIn(
                f"function {name}", source,
                f"the boot block calls {name}() but defines no such function",
            )

    def test_the_editor_is_not_initialised_before_authentication(self):
        """Defect 2: an empty selector on a normal sign-in."""
        self.assertFalse(
            self.report["beforeLogin"]["requestedSettingsInfo"],
            "/api/settings-info was requested before the user signed in",
        )

    def test_the_editor_is_not_rendered_before_authentication(self):
        self.assertIsNone(self.report["beforeLogin"]["mountedOn"])
        self.assertEqual(self.report["beforeLogin"]["treeHtml"], "")

    def test_the_document_selector_is_populated_after_authentication(self):
        """The reported symptom, asserted on the rendered result."""
        self.assertEqual(
            self.report["afterLogin"]["documentOptions"],
            ["main", "worker:tool:web_search"],
        )

    def test_the_editor_is_mounted_after_authentication(self):
        after = self.report["afterLogin"]
        self.assertEqual(after["mountedOn"], "#settings-tree")
        self.assertNotEqual(after["treeHtml"], "")

    def test_no_error_is_reported_once_the_editor_is_up(self):
        self.assertEqual(self.report["afterLogin"]["statusLine"], "")
        self.assertNotIn("afterLoginError", self.report)

    def test_host_checkboxes_follow_the_editor_view_state(self):
        """Search can enable show-absent by itself; the host must reflect that."""
        self.assertTrue(self.report["afterViewChange"]["showAbsent"])
        self.assertTrue(self.report["afterViewChange"]["valuesAsText"])


class SettingsPageMarkupTests(unittest.TestCase):
    """The markup the editor binds to must still be present."""

    def setUp(self):
        self.html = (ROOT / "webui" / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_the_document_selector_and_editor_host_exist(self):
        for element_id in ("cfg-document", "settings-tree", "cfg-aiset-status"):
            self.assertIn(f'id="{element_id}"', self.html, element_id)

    def test_the_library_assets_load_before_app_js(self):
        """A classic script sets window.aiset; a deferred one would arrive too late."""
        def position(source):
            index = self.html.index(source)
            return index

        assets = [position('/api/settings/assets/aiset.js'),
                  position('/api/settings/assets/aiset-tree.js')]
        app = position('src="app.js"')
        for offset in assets:
            self.assertLess(offset, app, "the assets must load before app.js")

    def test_the_assets_are_not_module_scripts(self):
        for line in self.html.splitlines():
            if "/api/settings/assets/aiset" in line and "<script" in line:
                self.assertNotIn("type=", line, "a module script is deferred and would race")
