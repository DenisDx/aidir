"""Tests for the aiset settings backend of aidir.

These cover the host adapter only: the library has its own suite. What is
verified here is the glue that must not break the rest of the project.

Run:
    AIDIR_SETTINGS_BACKEND=aiset venv/bin/python -m unittest -v test_settings_backend.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.settings.aiset_path import ensure_aiset_importable  # noqa: E402

ensure_aiset_importable()

from core.settings.documents import (  # noqa: E402
    discover_worker_documents,
    merge_worker_entry,
    parse_worker_document_id,
    worker_document_id,
)
from core.settings.facade import dotted_to_pointer, pointer_to_dotted, value_to_text  # noqa: E402
from core.settings.runtime import (  # noqa: E402
    SettingsRuntime,
    build_resolver,
    read_dotenv,
)
from core.settings.wsgi import (  # noqa: E402
    ANONYMOUS,
    ASSET_BASE,
    FULL_PERMISSIONS,
    READ_ONLY_PERMISSIONS,
    SESSION_PERMISSIONS,
    SETTINGS_BASE,
    build_asgi_app,
    principal_for,
)

TYPES = ROOT / "config.types.json5"
# Grammatically valid and structurally empty: every key of any document is
# therefore undeclared, which is the failure this is here to prove.
EMPTY_DESCRIPTOR = '{"descriptor_version": "1.0", "root": {"type": "object", "children": {}}}' 
DESCRIPTOR = ROOT / "config.descriptor.json5"


class PointerTests(unittest.TestCase):
    """The facade is the only place that knows dotted paths and pointers differ."""

    def test_dotted_to_pointer(self):
        self.assertEqual(dotted_to_pointer("logging.level"), "/logging/level")
        self.assertEqual(dotted_to_pointer(""), "")
        self.assertEqual(dotted_to_pointer("/logging/level"), "/logging/level")

    def test_dotted_to_pointer_escapes(self):
        self.assertEqual(dotted_to_pointer("a.b/c"), "/a/b~1c")
        self.assertEqual(dotted_to_pointer("a~b"), "/a~0b")

    def test_pointer_to_dotted_round_trip(self):
        for key in ("logging.level", "workers.items.web_search.providers", "a~b.c"):
            self.assertEqual(pointer_to_dotted(dotted_to_pointer(key)), key)

    def test_value_to_text_quotes_strings(self):
        self.assertEqual(value_to_text("info"), '"info"')
        self.assertEqual(value_to_text(3), "3")
        self.assertEqual(value_to_text(True), "true")


class DotenvTests(unittest.TestCase):
    """The .env mapping is resolver input, not a process-wide side effect."""

    def test_reads_plain_and_commented_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# a comment\n"
                "PLAIN=value\n"
                'QUOTED="quoted value"\n'
                "SINGLE='single'\n"
                "export EXPORTED=yes\n"
                "EMPTY=\n"
                "NOT_A_PAIR\n",
                encoding="utf-8",
            )
            values = read_dotenv(path)
        self.assertEqual(values["PLAIN"], "value")
        self.assertEqual(values["QUOTED"], "quoted value")
        self.assertEqual(values["SINGLE"], "single")
        self.assertEqual(values["EXPORTED"], "yes")
        self.assertEqual(values["EMPTY"], "")
        self.assertNotIn("NOT_A_PAIR", values)

    def test_missing_file_is_empty(self):
        self.assertEqual(read_dotenv(Path("/nonexistent/.env")), {})


class SingleBackendTests(unittest.TestCase):
    """There is one settings backend; a configuration file must match its descriptor."""

    def test_the_backend_flag_is_gone(self):
        """Regression guard: the old flag selected a loader that no longer exists."""
        from core.settings import runtime

        for name in ("BACKEND_ENV", "backend_name", "is_aiset_backend", "LEGACY_BACKEND"):
            self.assertFalse(hasattr(runtime, name), f"{name} still exists")

    def test_the_module_exposes_the_facade(self):
        from core.config import Config, config

        self.assertIsInstance(config, Config)
        self.assertEqual(config.get("logging.level"), config.raw()["logging"]["level"])

    def test_a_document_that_does_not_match_its_descriptor_is_refused(self):
        """A stale descriptor must stop the load, not serve an empty configuration."""
        from core.settings.runtime import SettingsError, SettingsRuntime

        runtime = SettingsRuntime(root=ROOT)
        broken = ROOT / "config.json5.example"
        import shutil
        import tempfile

        temp = Path(tempfile.mkdtemp(prefix="aidir-broken-"))
        self.addCleanup(shutil.rmtree, temp, ignore_errors=True)
        copy = temp / "config.json5"
        shutil.copyfile(broken, copy)
        runtime.root = temp
        # A descriptor that declares nothing is still valid grammar, so the
        # mismatch shows up as an unloadable document rather than as a crash.
        runtime.service.register("main", copy, EMPTY_DESCRIPTOR)
        with self.assertRaises(SettingsError):
            runtime.load("main")


class WorkerDocumentTests(unittest.TestCase):
    """Every worker-local file that ships with aidir becomes a document."""

    def test_discovery_finds_the_shipped_worker_files(self):
        documents = discover_worker_documents(ROOT)
        ids = sorted(document_id for document_id, _config, _descriptor in documents)
        self.assertEqual(
            ids,
            [
                "worker:agent:call_ollama",
                "worker:agent:openaix",
                "worker:context:context_builder",
                "worker:tool:http_api",
                "worker:tool:web_fetch",
                "worker:tool:web_search",
            ],
        )

    def test_document_id_round_trip(self):
        self.assertEqual(worker_document_id("tool", "web_search"), "worker:tool:web_search")
        self.assertEqual(parse_worker_document_id("worker:tool:web_search"), ("tool", "web_search"))
        self.assertIsNone(parse_worker_document_id("main"))

    def test_merge_lets_the_root_entry_win(self):
        merged = merge_worker_entry({"enabled": False, "request_timeout": 100},
                                    {"enabled": True, "request_timeout": 30, "user_agent": "local"})
        self.assertFalse(merged["enabled"])
        self.assertEqual(merged["request_timeout"], 100)
        self.assertEqual(merged["user_agent"], "local")

    def test_merge_keeps_local_only_keys_and_root_shared_keys(self):
        """The rule the deleted local merge implemented, stated as a contract."""
        local = {"enabled": True, "logging": {"level": "debug"}, "providers": [{"id": "a"}]}
        root = {"enabled": False, "logging": {"level": "info", "alllevel": "warning"}}
        merged = merge_worker_entry(root, local)
        self.assertFalse(merged["enabled"])            # root wins on a shared key
        self.assertEqual(merged["logging"]["level"], "info")
        self.assertEqual(merged["logging"]["alllevel"], "warning")  # root only
        self.assertEqual(merged["providers"], [{"id": "a"}])         # local only

    def test_a_worker_without_a_local_file_still_loads_from_the_root(self):
        from core.settings.documents import discover_worker_documents

        self.assertEqual(len(discover_worker_documents(ROOT)), 6)


class PrincipalTests(unittest.TestCase):
    """A WebUI account maps onto an aiset principal, and nothing else does."""

    def test_no_session_has_no_permissions(self):
        self.assertEqual(principal_for(None, "").permissions, frozenset())
        self.assertEqual(principal_for([], "admin").permissions, frozenset())

    def test_all_grants_every_capability_including_secrets(self):
        principal = principal_for(["all"], "admin")
        self.assertEqual(principal.permissions, FULL_PERMISSIONS)
        self.assertTrue(principal.has("settings_read_secret"))

    def test_unknown_group_grants_read_only(self):
        principal = principal_for(["viewer"], "bob")
        self.assertTrue(principal.has("settings_read_source"))
        self.assertFalse(principal.has("settings_write"))
        self.assertFalse(principal.has("settings_read_secret"))

    def test_permissions_are_a_subset_of_the_session_set(self):
        principal = principal_for(["settings_write"], "bob")
        self.assertTrue(principal.permissions <= SESSION_PERMISSIONS | READ_ONLY_PERMISSIONS)
        self.assertTrue(principal.has("settings_write"))
        self.assertFalse(principal.has("settings_read_secret"))

    def test_anonymous_is_used_when_nothing_was_injected(self):
        self.assertEqual(ANONYMOUS.permissions, frozenset())


class AsgiTransportTests(unittest.TestCase):
    """The library's ASGI adapter, mounted the way the WebUI mounts it."""

    def setUp(self):
        from aiset.service import Service
        from aiset.store import Settings as AisetStoreSettings

        self._temp = Path(tempfile.mkdtemp(prefix="aidir-settings-bridge-"))
        self.addCleanup(shutil.rmtree, self._temp, ignore_errors=True)
        self.target = self._temp / "config.json5"
        shutil.copyfile(ROOT / "config.json5", self.target)

        service = Service(AisetStoreSettings())
        service.register(
            "main",
            self.target,
            DESCRIPTOR.read_text(encoding="utf-8"),
            typedef_sources=[TYPES.read_text(encoding="utf-8")],
            resolver=build_resolver(),
        )
        self.service = service
        self.app = build_asgi_app(service)

    def _call(self, method, url, payload=None, cookie=None):
        """Send one request through the mounted application."""
        import anyio

        raw_path, _, query = url.partition("?")
        body = json.dumps(payload or {}).encode() if payload is not None else b""
        headers = [(b"host", b"testserver")]
        if body:
            headers.append((b"content-type", b"application/json"))
            headers.append((b"content-length", str(len(body)).encode()))
        if cookie:
            headers.append((b"cookie", f"aidir_token={cookie}".encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": headers,
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 5555),
        }
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        anyio.run(self.app, scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        response = next(m for m in sent if m["type"] == "http.response.body")
        return start["status"], json.loads(response["body"] or b"{}")

    def test_health_is_served(self):
        status, payload = self._call("GET", f"{SETTINGS_BASE}/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_documents_lists_the_registered_document(self):
        status, payload = self._call("GET", f"{SETTINGS_BASE}/documents")
        self.assertEqual(status, 200)
        self.assertEqual(payload["documents"], ["main"])

    def test_tree_returns_a_snapshot(self):
        status, payload = self._call("GET", f"{SETTINGS_BASE}/tree?document=main&path=&view=source")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertIn("node", payload)

    def test_operation_without_a_revision_is_refused(self):
        status, payload = self._call(
            "POST",
            f"{SETTINGS_BASE}/operations",
            {"document": "main", "operations": [{"op": "set", "path": "/logging/level", "value_text": '"debug"'}]},
        )
        self.assertEqual(status, 428, payload)
        self.assertEqual(payload["error"]["code"], "REVISION_REQUIRED")

    def test_full_write_cycle_through_the_api(self):
        status, tree = self._call("GET", f"{SETTINGS_BASE}/tree?document=main")
        self.assertEqual(status, 200, tree)
        status, applied = self._call(
            "POST",
            f"{SETTINGS_BASE}/operations",
            {
                "document": "main",
                "operations": [{"op": "set", "path": "/logging/level", "value_text": '"debug"'}],
                "expected_revision": tree["revision"],
                "expected_descriptor_revision": tree["descriptor_revision"],
            },
        )
        self.assertEqual(status, 200, applied)
        self.assertTrue(applied["stored"])
        self.assertIn('"level": "debug"', self.target.read_text(encoding="utf-8"))

    def test_the_library_serves_its_own_assets_at_the_app_root(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http",
            "path": f"{ASSET_BASE}/aiset.css", "raw_path": f"{ASSET_BASE}/aiset.css".encode(),
            "root_path": "", "query_string": b"", "headers": [],
            "server": ("testserver", 80), "client": ("127.0.0.1", 5555),
        }
        status, headers, body = self._raw(scope)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-cache, must-revalidate")
        self.assertIn(b"aiset", body)

    def _raw(self, scope):
        import anyio

        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        anyio.run(self.app, scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        response = next(m for m in sent if m["type"] == "http.response.body")
        headers = {name.decode(): value.decode() for name, value in start["headers"]}
        return start["status"], headers, response["body"]


class WebUIMountTests(unittest.TestCase):
    """The mounts as the WebUI installs them, not the bare library application.

    Two regressions in this class were invisible to a bare-library test:

    1. The assets live at the application root, so a browser request for
       /assets/aiset.css never reached the library when only /api/settings was
       mounted - the editor loaded nothing and the page came up blank.
    2. The library now serves them below the mount prefix as well, and the page
       uses that path, because the root path is not forwarded by the reverse
       proxy.
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from webui.backend.app import create_app

        class FakeRedis:
            async def get(self, key):
                return None

        class FakeCore:
            redis = FakeRedis()

            def raw(self):
                return {}

            def reload_config(self):
                return None

            def embedded_cron_enabled(self):
                return False

        cls.client = TestClient(create_app(FakeCore()))

    def test_the_three_editor_assets_are_served(self):
        for name, content_type in (
            ("aiset.css", "text/css"),
            ("aiset.js", "application/javascript"),
            ("aiset-tree.js", "application/javascript"),
        ):
            with self.subTest(asset=name):
                response = self.client.get(f"/assets/{name}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.headers["content-type"].startswith(content_type))
                self.assertEqual(response.headers["cache-control"], "no-cache, must-revalidate")
                self.assertIn("aiset", response.text)

    def test_an_unknown_asset_is_not_served_from_the_library(self):
        self.assertEqual(self.client.get("/assets/nope.js").status_code, 404)

    def test_the_api_is_still_mounted_under_its_own_prefix(self):
        self.assertEqual(self.client.get("/api/settings/health").status_code, 200)

    def test_the_frontend_itself_is_still_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/api/settings/assets/aiset-tree.js", response.text)

    def test_every_local_asset_the_settings_page_names_is_served(self):
        """The page and the routes must agree, checked from the page's side.

        Regression class: a URL that index.html requests but nothing serves. The
        API can answer 200 for every route while the editor still never loads,
        so the references are taken from the page rather than from the server.
        """
        import re

        page = (ROOT / "webui" / "frontend" / "index.html").read_text(encoding="utf-8")
        urls = set(re.findall(r'(?:src|href)="(/[^"#?]*)"', page))
        self.assertTrue(urls, "no local asset references found in index.html")
        for url in sorted(urls):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200, f"{url} is not served")

    def test_the_assets_are_loaded_in_dependency_order(self):
        """aiset.js must define the namespace aiset-tree.js extends; app.js uses both."""
        page = (ROOT / "webui" / "frontend" / "index.html").read_text(encoding="utf-8")
        order = [
            page.index("/api/settings/assets/aiset.js"),
            page.index("/api/settings/assets/aiset-tree.js"),
            page.index('src="app.js"'),
        ]
        self.assertEqual(order, sorted(order), "assets are loaded in the wrong order")

    def test_the_editor_assets_survive_the_reverse_proxy(self):
        """Regression: the editor never loaded through the nginx container.

        The library serves its assets at the application root and below the mount
        prefix. The nginx container forwards only `/api/` and `/ws/` to aidir and
        answers everything else from its own static directory with the SPA
        fallback, so a request for `/assets/aiset.js` arrived as `index.html`
        with status 200. The backend answered correctly the whole time, which is
        why every test here passed while the browser had no `window.aiset` at
        all.

        So the check is not "does the backend serve it" but "is every asset the
        page names under a prefix the deployment forwards". The forwarded
        prefixes are the ones nginx/nginx.conf proxies.
        """
        page = (ROOT / "webui" / "frontend" / "index.html").read_text(encoding="utf-8")
        forwarded = ("/api/", "/ws/")
        asset_urls = re.findall(
            r'(?:src|href)="(/(?:api/settings/)?assets/[^"#?]*)"', page
        )
        self.assertTrue(asset_urls, "the page names no library assets at all")
        for url in sorted(set(asset_urls)):
            with self.subTest(url=url):
                self.assertTrue(
                    url.startswith(forwarded),
                    f"{url} is outside every prefix the reverse proxy forwards",
                )

    def test_the_settings_page_has_the_container_the_editor_mounts_into(self):
        page = (ROOT / "webui" / "frontend" / "index.html").read_text(encoding="utf-8")
        for element_id in ("page-settings", "settings-tree", "cfg-document",
                           "cfg-show-absent", "cfg-values-as-text",
                           "cfg-reload-btn", "cfg-restart-btn", "cfg-aiset-status"):
            self.assertIn(f'id="{element_id}"', page, f"{element_id} is missing")

    def test_the_settings_api_requires_a_session(self):
        """The reason the editor cannot be initialised before signing in."""
        self.assertEqual(self.client.get("/api/settings-info").status_code, 401)
        self.assertEqual(self.client.get("/api/settings/tree?document=main").status_code, 403)

    def test_the_document_list_needs_read_permission(self):
        """Fixed upstream in aiset (public_documents defaults to False).

        Every settings route used to resolve the principal and check a
        permission except `/documents`, which answered an unauthenticated caller
        with every configuration document id. It was reported in
        aiset/proposal_improvement3.md and is now closed there; the library
        enforces `settings_read_source` like any other read, and a host that
        wants a public list opts in with `public_documents=True`.
        """
        self.assertEqual(self.client.get("/api/settings/documents").status_code, 403)

    def test_the_editor_is_not_initialised_before_authentication(self):
        """Regression: an empty document selector on a normal sign-in.

        The editor was initialised at script load, before the login or the
        session probe. /api/settings-info answered 401, the document list stayed
        empty, and nothing retried it. An HTTP test cannot see that ordering; the
        contract is asserted on the source instead.
        """
        script = (ROOT / "webui" / "frontend" / "app.js").read_text(encoding="utf-8")
        boot = script[script.index("// ── Boot ──"):script.index("// ── TEST LLM ──")]
        code = "\n".join(
            line for line in boot.splitlines() if not line.strip().startswith("//")
        )
        self.assertNotIn("initSettingsBackend()", code,
                         "the editor must not be initialised before authentication")

        after_login = script[script.index("function onLoggedIn()"):]
        after_login = after_login[:after_login.index("\n}")]
        self.assertIn("initSettingsBackend()", after_login,
                      "the editor must be initialised after a successful sign-in")

    def test_opening_the_settings_page_retries_the_initialisation(self):
        """A failed attempt must not leave a permanently empty selector."""
        script = (ROOT / "webui" / "frontend" / "app.js").read_text(encoding="utf-8")
        show_page = script[script.index("function showPage(name) {"):]
        show_page = show_page[:show_page.index("\n}")]
        self.assertIn("initSettingsBackend()", show_page)

        init = script[script.index("async function initSettingsBackend() {"):]
        init = init[:init.index("\n}")]
        self.assertIn("if (settingsReady) return;", init)
        self.assertIn("settingsReady = true;", init)

    def test_signing_out_discards_the_editor(self):
        """The next sign-in may be another account."""
        script = (ROOT / "webui" / "frontend" / "app.js").read_text(encoding="utf-8")
        logout = script[script.index("function doLogout() {"):]
        logout = logout[:logout.index("\n}")]
        self.assertIn("resetSettingsEditor()", logout)
        reset = script[script.index("function resetSettingsEditor() {"):]
        reset = reset[:reset.index("\n}")]
        self.assertIn("settingsReady = false;", reset)
        self.assertIn("$('cfg-document').innerHTML = '';", reset)

    def test_the_settings_page_mounts_only_when_the_backend_is_present(self):
        """app.js must not assume window.aiset exists."""
        script = (ROOT / "webui" / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("typeof window.aiset.mountTree", script)
        # The mount is guarded, not unconditional.
        guard = script.index("typeof window.aiset.mountTree")
        mount = script.index("window.aiset.mountTree('#settings-tree'")
        self.assertLess(guard, mount)


class EditorContentTests(unittest.TestCase):
    """What the editor will actually show: a snapshot per document, with help.

    Runs against the real runtime, so all seven documents are registered and
    the document selector of the Settings page has something to offer for each.
    """

    @classmethod
    def setUpClass(cls):
        from core.settings.runtime import get_runtime
        from core.settings.wsgi import build_asgi_app

        cls.runtime = get_runtime()
        # staticmethod: a plain function stored as a class attribute becomes a
        # bound method, and the ASGI app would lose its first argument.
        cls.app = staticmethod(build_asgi_app(cls.runtime.service))

    def _tree(self, document_id: str, path: str = "") -> tuple[int, dict]:
        import anyio

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": f"{SETTINGS_BASE}/tree",
            "raw_path": b"/api/settings/tree", "root_path": "",
            "query_string": f"document={document_id}&path={path}&view=source".encode(),
            "headers": [], "server": ("testserver", 80), "client": ("127.0.0.1", 1),
        }
        messages = [{"type": "http.request", "body": b"", "more_body": False}]
        sent: list[dict] = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        async def call():
            await self.app(scope, receive, send)

        anyio.run(call)
        status = next(m for m in sent if m["type"] == "http.response.start")["status"]
        body = next(m for m in sent if m["type"] == "http.response.body")["body"]
        return status, json.loads(body or b"{}")

    def test_every_registered_document_has_a_tree(self):
        for document_id in self.runtime.document_ids():
            with self.subTest(document=document_id):
                status, payload = self._tree(document_id)
                self.assertEqual(status, 200, payload)
                self.assertFalse(payload["invalid_on_disk"])
                self.assertTrue(payload["node"]["children"], f"{document_id} rendered nothing")

    def test_the_document_selector_offers_all_of_them(self):
        self.assertEqual(len(self.runtime.document_ids()), 7)
        self.assertIn("main", self.runtime.document_ids())

    def test_a_snapshot_node_carries_the_help_text_the_editor_shows(self):
        status, payload = self._tree("main", "/logging")
        self.assertEqual(status, 200, payload)
        children = {c["path"]: c for c in payload["node"]["children"]}
        level = children["/logging/level"]
        self.assertEqual(level["field"]["type"], "union")
        self.assertIn("journald", level["field"]["description"])
        self.assertEqual(level["apply"], "live")
        self.assertTrue(level["comments"], "the file comments of the node are not reported")

    def test_a_secret_node_is_marked_secret_in_the_snapshot(self):
        status, payload = self._tree("main", "/redis")
        self.assertEqual(status, 200, payload)
        children = {c["path"]: c for c in payload["node"]["children"]}
        self.assertTrue(children["/redis/password"]["field"]["secret"])
        self.assertFalse(children["/redis/host"]["field"]["secret"])

    def test_a_declared_but_unwritten_setting_is_reported_as_absent(self):
        """The `showAbsent` switch in the editor depends on this."""
        status, payload = self._tree("main", "/logging/audit")
        self.assertEqual(status, 200, payload)
        directory = next(c for c in payload["node"]["children"] if c["path"].endswith("/directory"))
        self.assertFalse(directory["exists"])
        self.assertEqual(directory["field"]["type"], "string")


class SessionPrincipalTests(unittest.TestCase):
    """The WebUI session decides who the caller is; aiset decides what it may do."""

    def _resolver(self, sessions):
        """A resolver whose session store answers from a plain dict."""
        import json as _json

        from webui.backend.app import SettingsPrincipalResolver

        class FakeRedis:
            async def get(self, key):
                payload = sessions.get(key.rsplit(":", 1)[-1])
                return _json.dumps(payload).encode() if payload is not None else None

        class FakeCore:
            redis = FakeRedis()

        return SettingsPrincipalResolver(FakeCore()).resolve

    def _scope(self, cookie=None, authorization=None):
        headers = []
        if cookie:
            headers.append((b"cookie", f"aidir_token={cookie}; other=1".encode()))
        if authorization:
            headers.append((b"authorization", f"Bearer {authorization}".encode()))
        return {"type": "http", "method": "GET", "path": "/api/settings/tree", "headers": headers}

    def test_an_all_session_gets_every_permission(self):
        import anyio

        resolve = self._resolver({"tok": {"login": "admin", "permissions": ["all"]}})
        principal = anyio.run(resolve, self._scope(cookie="tok"))
        self.assertEqual(principal.permissions, FULL_PERMISSIONS)
        self.assertEqual(principal.id, "admin")

    def test_a_session_without_permissions_is_refused(self):
        import anyio

        resolve = self._resolver({"tok": {"login": "bob", "permissions": []}})
        principal = anyio.run(resolve, self._scope(cookie="tok"))
        self.assertEqual(principal.permissions, frozenset())

    def test_an_unknown_token_is_anonymous(self):
        import anyio

        resolve = self._resolver({})
        principal = anyio.run(resolve, self._scope(cookie="nope"))
        self.assertIs(principal, ANONYMOUS)

    def test_a_missing_token_is_anonymous(self):
        import anyio

        resolve = self._resolver({"tok": {"login": "admin", "permissions": ["all"]}})
        self.assertIs(anyio.run(resolve, self._scope()), ANONYMOUS)

    def test_a_bearer_header_is_accepted(self):
        import anyio

        resolve = self._resolver({"tok": {"login": "admin", "permissions": ["all"]}})
        principal = anyio.run(resolve, self._scope(authorization="tok"))
        self.assertEqual(principal.id, "admin")


class RuntimeTests(unittest.TestCase):
    """The runtime registers every document of the real installation."""

    def test_build_reports_every_document(self):
        runtime = SettingsRuntime().build()
        self.assertIn("main", runtime.document_ids())
        self.assertEqual(len(runtime.document_ids()), 7)
        self.assertIn("logging", runtime.effective("main"))

    def test_worker_document_lookup(self):
        runtime = SettingsRuntime().build()
        self.assertEqual(runtime.worker_document("tool", "web_search"), "worker:tool:web_search")
        self.assertIsNone(runtime.worker_document("tool", "does_not_exist"))


if __name__ == "__main__":
    unittest.main(verbosity=2)