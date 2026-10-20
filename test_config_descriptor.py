"""Contract tests for the aiset descriptors of the aidir configuration files.

The descriptors are not documentation: aiset treats every undeclared key as a
hard error and then refuses to load the whole document, so a stale descriptor
would silently disable every settings write in the WebUI. These tests are the
gate that keeps the descriptors honest.

What is checked:

1. every descriptor file parses and passes the strict grammar check;
2. the root document loads cleanly from config.json5 and config.json5.example;
3. every worker-local document loads cleanly;
4. no leaf of any configuration file is invisible to its descriptor;
5. a secret subtree is denied as a whole to a principal without the permission,
   and readable with it;
6. a staged write preserves the comments and the ${VAR} placeholders of the file.

Run:
    venv/bin/python -m unittest -v test_config_descriptor.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AISET_SRC = ROOT / "aiset" / "src"
if AISET_SRC.is_dir() and str(AISET_SRC) not in sys.path:
    sys.path.insert(0, str(AISET_SRC))

from aiset.descriptor import compile_descriptor, validate_descriptor  # noqa: E402
from aiset.errors import AisetError  # noqa: E402
from aiset.resolver import Resolver  # noqa: E402
from aiset.service import Principal, Service  # noqa: E402
from aiset.store import Settings  # noqa: E402

TYPES = ROOT / "config.types.json5"
ROOT_DESCRIPTOR = ROOT / "config.descriptor.json5"
ROOT_CONFIG = ROOT / "config.json5"
ROOT_EXAMPLE = ROOT / "config.json5.example"

PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")

# Environment used by the tests. It is complete on purpose: a variable that is
# missing here and has no inline default would resolve to an empty string and
# fail a length constraint, which would hide the real coverage problem behind a
# substitution problem. No value here is a real credential.
TEST_ENV = {
    "AIDIR_INSTANCE": "aidir-test",
    "AIDIR_ROOT": "/tmp/aidir-test",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6379",
    "REDIS_PASSWORD": "test-redis",
    "OPENAIX_ENDPOINT_PORT": "21434",
    "OPENAIX_ENDPOINT_HOST": "0.0.0.0",
    "MCP_ENDPOINT_PORT": "20001",
    "MCP_ENDPOINT_HOST": "0.0.0.0",
    "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
    "WEBUI_PORT": "20082",
    "WEBUI_HOST": "127.0.0.1",
    "ROOT_USER": "admin",
    "ROOT_PASSWORD": "test-password",
    "LOG_LEVEL": "error",
    "LOG_TIMEZONE": "local",
    "LOG_WIPE_PERIOD": "10000",
    "LOG_WIPE_MAX_AGE": "1000000",
    "LOG_TRIM_PERIOD": "60",
    "MAX_LOG_SIZE": "1048576",
    "CRON_PERIOD": "60",
    "REQUEST_TIMEOUT": "100",
    "TASK_RESTART_WAIT_TIMEOUT_SECONDS": "120",
    "TASK_QUEUE_TIMEOUT_SECONDS": "300",
    "TASK_RUN_TIMEOUT_SECONDS": "300",
    "EXTERNAL_TASK_LIVE_SECONDS": "2500000",
    "ENVIDS_STORE_DURATION_MINUTES": "60000",
    "ENVIDS_CHECK_PERIOD_MINUTES": "600",
    "BOOTSTRAP_MAX_CHARS": "32000",
    "BOOTSTRAP_TOTAL_MAX_CHARS": "150000",
    "WEBUI_TIMEOUT_OLLAMA_CHAT": "120",
    "WEBUI_TIMEOUT_MCP_PROXY": "60",
    "WEBUI_TIMEOUT_MODEL_LIST": "20",
    "WEBUI_TIMEOUT_AGENT_TEST": "45",
    "SEARXNG_HOST": "127.0.0.1",
    "SEARXNG_PORT": "18080",
    "BRAVE_APIKEY": "test-brave-key",
    "CRYONET_API_BASE_URL": "https://groups.io/api/v1",
    "CRYONET_API_KEY": "test-cryonet-key",
    "OPENCODE_GO_BASE_URL": "https://opencode.ai/zen/go",
    "OPENCODE_GO_API_KEY": "test-opencode-key",
    "WORKSPACE_TOKEN": "test-workspace-token",
    "API_USER_TOKEN": "test-api-token",
    "OLLAMA_REMOTE_BASE_URL": "http://127.0.0.1:11435",
    "OPENAIX_TOP_P": "0.9",
    "OPENAIX_PRESENCE_PENALTY": "1.5",
    "OPENAIX_MAX_TOKENS": "4096",
    "OPENAIX_REPEAT_LAST_N": "64",
    "OPENAIX_NUM_PREDICT": "1024",
    "EXAMPLE_API_BASE_URL": "https://example.invalid/api",
    "EXAMPLE_API_KEY": "test-example-key",
    "AIDIR_AI_ENABLED": "true",
    "AIDIR_AI_BASE_URL": "http://127.0.0.1:21434/v1",
    "AIDIR_AI_MODEL": "local-only",
    "AIDIR_AI_API_KEY": "test-assistant-key",
    "REMOTE_AIDIR_BASE_URL": "http://127.0.0.1:21434",
    "OPENAIX_TEMPERATURE": "0.7",
    "OPENAIX_TOP_P": "0.9",
    "OPENAIX_TOP_K": "40",
    "OPENAIX_MIN_P": "0.05",
    "OPENAIX_SEED": "42",
    "OPENAIX_PRESENCE_PENALTY": "1.5",
    "OPENAIX_FREQUENCY_PENALTY": "0.1",
    "OPENAIX_REPETITION_PENALTY": "1.05",
    "OPENAIX_REPEAT_PENALTY": "1.1",
    "OPENAIX_REPEAT_LAST_N": "64",
    "OPENAIX_MAX_TOKENS": "4096",
    "OPENAIX_NUM_PREDICT": "1024",
}

# Worker id -> (worker type, descriptor path, config path).
WORKER_DOCUMENTS = [
    ("call_ollama", "agent", "workers/agent/call_ollama"),
    ("openaix", "agent", "workers/agent/openaix"),
    ("context_builder", "context", "workers/context/context_builder"),
    ("http_api", "tool", "workers/tool/http_api"),
    ("web_fetch", "tool", "workers/tool/web_fetch"),
    ("web_search", "tool", "workers/tool/web_search"),
]


def _resolver() -> Resolver:
    """Resolver with an empty process environment and a complete fixture .env."""
    return Resolver(overrides={}, dotenv=TEST_ENV, environ={})


def _build_service(document_id: str, descriptor: Path, config: Path) -> Service:
    """Register one document on a throwaway service backed by a temp copy."""
    import shutil
    import tempfile

    temp_dir = Path(tempfile.mkdtemp(prefix="aidir-descriptor-test-"))
    target = temp_dir / config.name
    shutil.copyfile(config, target)
    service = Service(Settings())
    service.register(
        document_id,
        target,
        descriptor.read_text(encoding="utf-8"),
        typedef_sources=[TYPES.read_text(encoding="utf-8")],
        resolver=_resolver(),
    )
    return service


def _placeholders(path: Path) -> set[str]:
    """Variable names referenced by ${...} outside comments and strings.

    The header comment of config.json5 mentions `${VAR}` as an example, so a
    naive scan would report a variable that the file never actually uses.
    """
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        code: list[str] = []
        in_string = False
        quote = ""
        index = 0
        while index < len(line):
            char = line[index]
            if in_string:
                code.append(char)
                if char == "\\":
                    index += 1
                    if index < len(line):
                        code.append(line[index])
                    index += 2
                    continue
                if char == quote:
                    in_string = False
            elif char in "\"'`":
                in_string = True
                quote = char
                code.append(char)
            elif line.startswith("//", index):
                break
            else:
                code.append(char)
            index += 1
        names.update(PLACEHOLDER_RE.findall("".join(code)))
    return names


class DescriptorGrammarTests(unittest.TestCase):
    """Every descriptor file must be valid on its own, without tolerance."""

    def test_types_descriptor_is_strictly_valid(self):
        diagnostic = validate_descriptor(TYPES.read_text(encoding="utf-8"), mode="strict")
        self.assertTrue(diagnostic.valid, diagnostic.to_dict())

    def test_root_descriptor_compiles_strictly(self):
        compile_descriptor(
            ROOT_DESCRIPTOR.read_text(encoding="utf-8"),
            typedef_sources=[TYPES.read_text(encoding="utf-8")],
            strict=True,
        )

    def test_every_worker_descriptor_compiles_strictly(self):
        for _worker_id, _worker_type, relative in WORKER_DOCUMENTS:
            with self.subTest(worker=relative):
                compile_descriptor(
                    (ROOT / relative / "descriptor.json5").read_text(encoding="utf-8"),
                    typedef_sources=[TYPES.read_text(encoding="utf-8")],
                    strict=True,
                )


class DocumentLoadTests(unittest.TestCase):
    """The gate: a document that does not load disables every settings write."""

    def test_root_config_loads_without_issues(self):
        service = _build_service("main", ROOT_DESCRIPTOR, ROOT_CONFIG)
        result = service.load("main", principal=Principal.demo())
        self.assertFalse(result.invalid_on_disk, result.reason)
        self.assertEqual(
            [issue for issue in result.issues if issue.get("severity") == "error"],
            [],
            result.issues,
        )

    def test_root_example_loads_without_issues(self):
        service = _build_service("main", ROOT_DESCRIPTOR, ROOT_EXAMPLE)
        result = service.load("main", principal=Principal.demo())
        self.assertFalse(result.invalid_on_disk, result.reason)
        self.assertEqual(
            [issue for issue in result.issues if issue.get("severity") == "error"],
            [],
            result.issues,
        )

    def test_every_worker_config_loads_without_issues(self):
        for worker_id, worker_type, relative in WORKER_DOCUMENTS:
            with self.subTest(worker=relative):
                service = _build_service(
                    f"worker:{worker_type}:{worker_id}",
                    ROOT / relative / "descriptor.json5",
                    ROOT / relative / "config.json5",
                )
                result = service.load(
                    f"worker:{worker_type}:{worker_id}", principal=Principal.demo()
                )
                self.assertFalse(result.invalid_on_disk, result.reason)
                self.assertEqual(
                    [i for i in result.issues if i.get("severity") == "error"], [], result.issues
                )


class LegacyFieldTests(unittest.TestCase):
    """Fields kept only so an existing installation still starts."""

    def test_a_legacy_endpoint_request_timeout_still_loads(self):
        """The previous loader substituted placeholders before parsing.

        It therefore never validated `endpoints.*.request_timeout`, and older
        local configurations still carry it. The descriptor must declare it or
        such a configuration stops aidir at startup with `env_not_allowed` -
        the failure reported on a real machine after the migration.
        """
        import tempfile

        text = ROOT_CONFIG.read_text(encoding="utf-8")
        marker = '      "port": ${OPENAIX_ENDPOINT_PORT:-21434},\n'
        self.assertIn(marker, text, "the endpoint block moved; update this fixture")
        legacy = text.replace(
            marker, marker + '      "request_timeout": ${REQUEST_TIMEOUT:-100},\n', 1
        )
        with tempfile.TemporaryDirectory(prefix="aidir-legacy-") as tmp:
            path = Path(tmp) / "config.json5"
            path.write_text(legacy, encoding="utf-8")
            service = _build_service("main", ROOT_DESCRIPTOR, path)
            result = service.load("main", principal=Principal.demo())
            errors = [i for i in result.issues if i.get("severity") == "error"]
        self.assertFalse(result.invalid_on_disk, result.reason)
        self.assertEqual(errors, [], result.issues)

    def test_the_legacy_timeout_resolves_to_an_integer(self):
        import tempfile

        text = ROOT_CONFIG.read_text(encoding="utf-8")
        marker = '      "port": ${OPENAIX_ENDPOINT_PORT:-21434},\n'
        legacy = text.replace(
            marker, marker + '      "request_timeout": ${REQUEST_TIMEOUT:-100},\n', 1
        )
        with tempfile.TemporaryDirectory(prefix="aidir-legacy-") as tmp:
            path = Path(tmp) / "config.json5"
            path.write_text(legacy, encoding="utf-8")
            service = _build_service("main", ROOT_DESCRIPTOR, path)
            value = service.get(
                "main", "/endpoints/0/request_timeout",
                view="effective", principal=Principal.demo(),
            ).value_text
        # TEST_ENV sets REQUEST_TIMEOUT=100; an unquoted placeholder must become
        # a number, not the string "100".
        self.assertEqual(int(value), 100)


class InvalidDocumentReportTests(unittest.TestCase):
    """A drifted config must report every stale key in one go."""

    def test_the_failure_lists_every_undeclared_field(self):
        import tempfile

        from core.settings.runtime import format_load_failure

        text = ROOT_CONFIG.read_text(encoding="utf-8")
        marker = '      "port": ${OPENAIX_ENDPOINT_PORT:-21434},\n'
        self.assertIn(marker, text, "the endpoint block moved; update this fixture")
        drifted = text.replace(
            marker,
            marker
            + '      "context_builder_worker": "context_builder",\n'
            + '      "retired_endpoint_option": 1,\n',
            1,
        )
        with tempfile.TemporaryDirectory(prefix="aidir-drift-") as tmp:
            path = Path(tmp) / "config.json5"
            path.write_text(drifted, encoding="utf-8")
            service = _build_service("main", ROOT_DESCRIPTOR, path)
            result = service.load("main", principal=Principal.demo())

        self.assertTrue(result.invalid_on_disk)
        message = format_load_failure("main", result)
        self.assertIn("/endpoints/0/context_builder_worker", message)
        self.assertIn("/endpoints/0/retired_endpoint_option", message)
        # One line per problem, so a reader can delete them in a single pass.
        self.assertGreaterEqual(len(message.splitlines()), 3)


class CoverageTests(unittest.TestCase):
    """No leaf of a configuration file may be invisible to its descriptor."""

    def test_every_configured_path_is_declared(self):
        for config in (ROOT_CONFIG, ROOT_EXAMPLE):
            with self.subTest(config=config.name):
                service = _build_service("main", ROOT_DESCRIPTOR, config)
                result = service.load("main", principal=Principal.demo())
                self.assertFalse(result.invalid_on_disk, result.reason)
                self.assertEqual(
                    [i for i in result.issues if i.get("severity") == "error"], [], result.issues
                )

    def test_every_placeholder_variable_has_a_test_value(self):
        """A new ${VAR} in a shipped file must get a value in TEST_ENV."""
        used = set()
        for config in (ROOT_CONFIG, ROOT_EXAMPLE):
            used |= _placeholders(config)
        for _worker_id, _worker_type, relative in WORKER_DOCUMENTS:
            used |= _placeholders(ROOT / relative / "config.json5")
        self.assertEqual(
            used - set(TEST_ENV),
            set(),
            "a shipped configuration references variables that TEST_ENV does not define",
        )

    def test_root_descriptor_declares_no_required_key_it_does_not_own(self):
        """A `required` key absent from a shipped file would break every load."""
        service = _build_service("main", ROOT_DESCRIPTOR, ROOT_CONFIG)
        result = service.load("main", principal=Principal.demo())
        self.assertFalse(result.invalid_on_disk, result.reason)


class SecretTests(unittest.TestCase):
    """A secret subtree is denied as a whole, never redacted field by field."""

    def test_secret_subtree_is_denied_without_permission(self):
        service = _build_service("main", ROOT_DESCRIPTOR, ROOT_CONFIG)
        service.load("main", principal=Principal.demo())
        principal = Principal(
            id="no-secret", permissions=frozenset({"settings_read_source", "settings_read_effective"})
        )
        tree = service.tree("main", "/redis", view="source", principal=principal)
        redis = tree["node"]
        self.assertTrue(redis["access_denied"])
        self.assertIsNone(redis["value_text"])

    def test_secret_subtree_is_readable_with_permission(self):
        service = _build_service("main", ROOT_DESCRIPTOR, ROOT_CONFIG)
        service.load("main", principal=Principal.demo())
        value = service.get("main", "/redis/password", view="source", principal=Principal.demo())
        self.assertTrue(value.exists)
        self.assertIn("REDIS_PASSWORD", value.value_text)

    def test_writing_a_secret_without_permission_is_refused(self):
        service = _build_service("main", ROOT_DESCRIPTOR, ROOT_CONFIG)
        loaded = service.load("main", principal=Principal.demo())
        principal = Principal(id="no-secret", permissions=frozenset({"settings_write"}))
        with self.assertRaises(AisetError) as caught:
            service.set(
                "main",
                "/redis/password",
                '"changed"',
                expected_revision=loaded.revision,
                expected_descriptor_revision=loaded.descriptor_revision,
                principal=principal,
            )
        self.assertEqual(caught.exception.code, "SECRET_ACCESS_DENIED")


class WriteTests(unittest.TestCase):
    """A staged write keeps the file readable and keeps its placeholders."""

    def test_set_preserves_comments_and_placeholders(self):
        import shutil
        import tempfile

        temp_dir = Path(tempfile.mkdtemp(prefix="aidir-descriptor-write-"))
        target = temp_dir / "config.json5"
        shutil.copyfile(ROOT_CONFIG, target)
        service = Service(Settings())
        service.register(
            "main",
            target,
            ROOT_DESCRIPTOR.read_text(encoding="utf-8"),
            typedef_sources=[TYPES.read_text(encoding="utf-8")],
            resolver=_resolver(),
        )
        loaded = service.load("main", principal=Principal.demo())
        self.assertFalse(loaded.invalid_on_disk, loaded.reason)

        result = service.apply_operations(
            "main",
            [{"op": "set", "path": "/logging/level", "value_text": '"debug"'}],
            expected_revision=loaded.revision,
            expected_descriptor_revision=loaded.descriptor_revision,
            principal=Principal.demo(),
        )
        self.assertTrue(result.stored, result.to_dict())

        text = target.read_text(encoding="utf-8")
        self.assertIn('"level": "debug"', text)
        self.assertIn("${AIDIR_INSTANCE:-aidir111}", text)
        self.assertIn("// Run periodic maintenance", text)
        self.assertEqual(result.application_state, "applied")

        reloaded = service.load("main", principal=Principal.demo())
        self.assertFalse(reloaded.invalid_on_disk, reloaded.reason)


class WorkerMergeTests(unittest.TestCase):
    """The root entry wins over the worker-local entry, as before the migration."""

    def test_root_wins_over_worker_local(self):
        from aiset.merge import merge

        root_entry = {"enabled": False, "request_timeout": 100}
        local_entry = {"enabled": True, "request_timeout": 30, "user_agent": "local"}
        merged = merge(root_entry, local_entry, prefer="base")
        self.assertFalse(merged["enabled"])
        self.assertEqual(merged["request_timeout"], 100)
        self.assertEqual(merged["user_agent"], "local")


class DocumentShapeTests(unittest.TestCase):
    """Sanity checks on the shipped files themselves."""

    def test_every_worker_local_config_has_a_descriptor(self):
        for worker_id, worker_type, relative in WORKER_DOCUMENTS:
            with self.subTest(worker=relative):
                self.assertTrue((ROOT / relative / "config.json5").is_file())
                self.assertTrue((ROOT / relative / "descriptor.json5").is_file())

    def test_root_config_starts_with_a_comment_header(self):
        """The source-preserving editor relies on readable input; keep it that way."""
        self.assertTrue(ROOT_CONFIG.read_text(encoding="utf-8").lstrip().startswith("//"))


if __name__ == "__main__":
    unittest.main(verbosity=2)