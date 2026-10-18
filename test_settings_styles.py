"""The host stylesheet must actually design the generated editor.

The library emits the markup and ships a default theme; a host is expected to
style what it emits, and nothing forces it to. A missing rule shows up as
unstyled markup, not as an error, which is how an unreadable settings page
reached a user once while every HTTP test was green.

Since aiset 97748c8 the contract is stronger and cheaper to check:

  * the library publishes its class inventory as `aiset.classes()`, so coverage
    is checked against the contract rather than against the source;
  * the whole palette is expressed through `--aiset-*` custom properties, so the
    host overrides fourteen variables instead of forty selectors, and no
    specificity contest can be lost;
  * the structural rules a readable tree needs moved into the library.

What is asserted here:

  * every class in `aiset.classes()` is either styled by the host or already
    painted by the library;
  * every variable `aiset.css` reads is set by the host;
  * the host sheet loads after the library one, and the host reintroduces no
    literal palette that would defeat the variable mechanism;
  * no rule of the host escapes the editor, and no host control carries an inline
    style, because an inline declaration outranks any stylesheet rule.
"""

import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
AISET_ASSETS = ROOT / "aiset" / "src" / "aiset" / "assets"
LIBRARY_CSS = AISET_ASSETS / "aiset.css"
HOST_CSS = ROOT / "webui" / "frontend" / "settings.css"
INDEX = ROOT / "webui" / "frontend" / "index.html"
INVENTORY_PROBE = ROOT / "test_aiset_class_inventory.js"

# Classes of the form-based editor in aiset.js, which aidir does not mount.
# They are outside the tree inventory and outside the tree stylesheet.
FORM_EDITOR = {"aiset-editor", "aiset-label", "aiset-path", "aiset-document",
               "aiset-value", "aiset-field", "aiset-hint", "aiset-local",
               "aiset-status", "aiset-issues"}


def strip_comments(css: str) -> str:
    """Drop /* ... */ so a class named in prose is not counted as styled."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def selectors_of(css: str) -> set[str]:
    found: set[str] = set()
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", strip_comments(css)):
        for selector in block.group(1).split(","):
            selector = " ".join(selector.split())
            if selector:
                found.add(selector)
    return found


@unittest.skipUnless(shutil.which("node"), "node is required for the class inventory")
class HostStylesheetTests(unittest.TestCase):
    """webui/frontend/settings.css is the design of the settings page."""

    @classmethod
    def setUpClass(cls):
        if not LIBRARY_CSS.is_file():
            raise unittest.SkipTest(f"aiset sources are not present at {AISET_ASSETS}")
        cls.library_css = LIBRARY_CSS.read_text(encoding="utf-8")
        cls.host_css = HOST_CSS.read_text(encoding="utf-8")
        cls.host_body = strip_comments(cls.host_css)
        cls.page = INDEX.read_text(encoding="utf-8")
        cls.library_selectors = selectors_of(cls.library_css)
        cls.host_selectors = selectors_of(cls.host_css)

        proc = subprocess.run(
            ["node", str(INVENTORY_PROBE)], cwd=str(ROOT),
            capture_output=True, text=True, timeout=60,
        )
        cls.inventory_error = None
        if proc.returncode != 0:
            cls.inventory = []
            cls.inventory_error = proc.stdout.strip() or proc.stderr.strip()[:400]
        else:
            payload = json.loads(proc.stdout)
            cls.inventory = [] if isinstance(payload, dict) else payload
            if isinstance(payload, dict):
                cls.inventory_error = payload.get("error")

    # -- the library's own contract ---------------------------------------

    def test_the_library_publishes_a_class_inventory(self):
        """The whole point of the inventory is that a host can read it."""
        self.assertIsNone(
            self.inventory_error,
            f"aiset.classes() is unreachable: {self.inventory_error}",
        )
        self.assertGreater(len(self.inventory), 40, "the inventory collapsed")

    def test_the_inventory_agrees_with_the_classes_in_the_page(self):
        """A host must not be handed a contract the assets do not follow."""
        declared = {entry["name"] for entry in self.inventory}
        for name in declared:
            if name in FORM_EDITOR or name in ("aiset-tree", "aiset-form"):
                continue
            self.assertIn(
                name, "".join(self.library_css) + "".join(
                    (AISET_ASSETS / "aiset-tree.js").read_text(encoding="utf-8")
                ),
                f"{name} is declared but appears nowhere in the assets",
            )

    # -- coverage ---------------------------------------------------------

    def test_every_class_is_styled_by_the_host_or_the_library(self):
        host_names = set(re.findall(r"\.([A-Za-z][\w-]*)", self.host_body))
        library_names = set(re.findall(r"\.([A-Za-z][\w-]*)", self.library_css))
        unpainted = []
        for entry in self.inventory:
            name = entry["name"]
            if name in FORM_EDITOR:
                continue
            if name in host_names or name in library_names:
                continue
            unpainted.append(name)
        self.assertEqual(
            unpainted, [],
            "neither the host nor the library styles: " + ", ".join(unpainted),
        )

    def test_every_required_class_is_painted_by_someone(self):
        """`required` means a host that styles nothing gets a broken editor.

        Since aiset 97748c8 the structural half - row layout, the indent of
        `.aiset-tree-children`, the toggle - is painted by the library, so a
        required class is satisfied by the library too. What must never happen is
        one that nobody paints.
        """
        host_names = set(re.findall(r"\.([A-Za-z][\w-]*)", self.host_body))
        library_names = set(re.findall(r"\.([A-Za-z][\w-]*)", self.library_css))
        required = [e["name"] for e in self.inventory if e.get("required")]
        self.assertGreaterEqual(len(required), 10, "no required classes are declared")
        unpainted = [
            name for name in required
            if name not in host_names and name not in library_names
        ]
        self.assertEqual(
            unpainted, [],
            "the library calls these required, so neither may be left unpainted: "
            + ", ".join(unpainted),
        )

    def test_the_host_does_not_reimplement_the_structure_the_library_owns(self):
        """Item 5 moved the nesting rules into the library; a host copy drifts."""
        for selector in (".aiset-tree-children", ".aiset-tree-toggle"):
            blocks = [
                body for selector_text, body in re.findall(
                    r"([^{}]+)\{([^}]*)\}", self.host_body
                ) if selector in " ".join(selector_text.split())
            ]
            for body in blocks:
                if re.search(r"(padding-left|margin-left|border-left)", body):
                    self.fail(
                        f"{selector} indentation is reimplemented in the host: {body.strip()}"
                    )

    # -- the variable mechanism -------------------------------------------

    def test_the_host_sets_every_variable_the_library_reads(self):
        read = set(re.findall(r"var\(\s*(--aiset-[a-z-]+)", self.library_css))
        self.assertGreater(len(read), 10, "the library reads almost no variables")
        declared = set(re.findall(r"(--aiset-[a-z-]+)\s*:", self.host_css))
        missing = sorted(read - declared)
        self.assertEqual(
            missing, [],
            "the library reads these but the host does not set them, so they "
            "keep their light defaults: " + ", ".join(missing),
        )

    def test_the_host_reintroduces_no_literal_palette_for_the_editor(self):
        """A literal defeats the variables and starts the specificity contest."""
        for block in re.finditer(r"([^{}]+)\{([^}]*)\}", self.host_body):
            selector = " ".join(block.group(1).split())
            if not selector.startswith((".aiset-", ".cfg-", "#cfg-")):
                continue
            body = block.group(2)
            colours = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
            # The variable block itself and the two accent-on-accent cases.
            if "color: #fff" in body:
                colours = [c for c in colours if c.lower() != "#fff"]
            self.assertEqual(
                colours, [],
                f"{selector} hard-codes {colours} instead of a --aiset-* variable",
            )

    def test_the_host_shadows_the_library_variables_on_the_editor(self):
        """A `:root` block wins everywhere, which is what the palette wants."""
        block = re.search(r":root\s*\{([^}]*)\}", self.host_body)
        self.assertIsNotNone(block, "no variable block in the host stylesheet")
        variables = re.findall(r"(--aiset-[a-z-]+)\s*:", block.group(1))
        self.assertGreaterEqual(len(variables), 10, "the variable block is nearly empty")
        self.assertEqual(len(variables), len(set(variables)), "a variable is set twice")

    def test_color_scheme_is_declared(self):
        """Native popups, scrollbars and checkboxes follow the document."""
        self.assertIn("color-scheme", self.host_body)

    # -- load order and scope ---------------------------------------------

    def test_the_host_stylesheet_loads_after_the_library_one(self):
        self.assertLess(
            self.page.index("/api/settings/assets/aiset.css"),
            self.page.index("settings.css"),
            "the host must load last to win on equal specificity",
        )

    def test_no_editor_rule_escapes_the_editor(self):
        """An unscoped element rule silently restyles the host's own page."""
        for selector in re.findall(r"(?m)^([^@{}/][^{}]*)\{", self.host_body):
            selector = selector.strip()
            if not selector or selector.startswith((":root", ":", "@")):
                continue
            for part in selector.split(","):
                part = part.strip()
                if part.startswith((".aiset-", "#cfg-", ".cfg-")):
                    continue
                self.fail(f"rule escapes the editor and restyles the host: {part}")

    def test_the_settings_controls_carry_no_inline_styles(self):
        block = self.page[self.page.index('id="cfg-aiset-card"'):]
        offenders = re.findall(r'id="(cfg-[^"]+)"[^>]*\sstyle="', block)
        self.assertEqual(offenders, [], f"inline styles left on: {offenders}")

    # -- structure the library now guarantees ------------------------------

    def test_the_library_owns_the_structure_of_a_readable_tree(self):
        """The host must not have to re-derive it; that was the whole item."""
        for selector in (".aiset-tree-row", ".aiset-tree-children", ".aiset-tree-desc"):
            self.assertIn(selector, self.library_css, f"{selector} is back in host territory")
        children = re.search(r"\.aiset-tree-children\s*\{([^}]*)\}", self.library_css)
        self.assertRegex(children.group(1), r"padding-left|margin-left",
                         "nested levels would sit at the same left edge")

    def test_the_library_ships_a_focus_ring(self):
        self.assertIn(":focus-visible", self.library_css)

    def test_the_toolbar_buttons_are_named_by_role(self):
        """A host must not have to reach Apply through `:first-child`."""
        tree_js = (AISET_ASSETS / "aiset-tree.js").read_text(encoding="utf-8")
        for role in ("apply", "reload", "revert"):
            self.assertIn(f"aiset-tree-{role}", tree_js, f"no role class for {role}")
        self.assertNotIn(":first-child", self.host_body,
                         "the host still ranks buttons by position")
        for role in ("apply", "reload", "revert"):
            self.assertIn(f".aiset-tree-{role}", self.host_body, f"{role} is not styled")


if __name__ == "__main__":
    unittest.main()