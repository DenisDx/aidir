"""The Settings assistant: construction, tools, safety and routes.

The assistant is the one component of the settings page that talks to a model.
Its contract is narrow and worth pinning hard:

  * it never writes. Every change leaves as a validated operation list the
    browser stages on the tree, and the user applies it;
  * it never sees a secret. Its principal carries read and validate
    permissions only, so a tool result cannot carry a secret value;
  * a provider failure is an explicit error, never a success-shaped answer;
  * a disabled or half-configured assistant degrades to "off", not to a crash.

The provider itself is stubbed: a test must not depend on a running model, and a
model must never decide whether these rules hold.
"""

import json
import pathlib
import unittest
import urllib.error
import urllib.request
from unittest import mock

from core.settings.agent import (
    AGENT_ERROR_STATUS,
    AgentError,
    ConfigurationAgent,
    _bounded_node,
    _parse_arguments,
    _path,
    _provider_message,
    _relaxed_json,
    _safe_arguments,
    _tool_content,
    build_agent,
    describe,
)

ROOT = pathlib.Path(__file__).resolve().parent


class FakeProvider:
    """Stands in for an OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(json.loads(request.data.decode("utf-8")))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return mock.MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda s, *a: False,
            read=lambda: json.dumps(reply).encode("utf-8"),
        )


def tool_round(name, arguments, call_id="call-1"):
    """One assistant turn that asks for a tool."""
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }}]}


def text_answer(content):
    """One assistant turn with a final answer."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class PathNormalizationTests(unittest.TestCase):
    """A model writes "/" for the root; the Service must still understand it."""

    def test_the_root_has_several_spellings(self):
        for spelling in ("", "/", "#", "root", "/root", None):
            with self.subTest(path=spelling):
                self.assertEqual(_path({"path": spelling}), "")

    def test_a_bare_path_is_made_absolute(self):
        self.assertEqual(_path({"path": "webui/port"}), "/webui/port")

    def test_a_trailing_slash_is_dropped(self):
        self.assertEqual(_path({"path": "/webui/port/"}), "/webui/port")


class ArgumentParsingTests(unittest.TestCase):
    """Models emit sloppy JSON; the loop must survive it."""

    def test_a_dict_passes_through(self):
        self.assertEqual(_parse_arguments({"path": "/a"}), {"path": "/a"})

    def test_a_fenced_block_is_accepted(self):
        self.assertEqual(_parse_arguments('```json\n{"path": "/a"}\n```'), {"path": "/a"})

    def test_single_quotes_are_repaired(self):
        self.assertEqual(_parse_arguments(_relaxed_json("{'path': '/a'}")), {"path": "/a"})
        self.assertEqual(_parse_arguments("{'path': '/a'}"), {"path": "/a"})

    def test_nonsense_raises_a_recoverable_error(self):
        with self.assertRaises(AgentError) as caught:
            _parse_arguments("not json at all ]")
        self.assertEqual(caught.exception.code, "AGENT_BAD_ARGUMENTS")


class SecretSafetyTests(unittest.TestCase):
    """The assistant has no way to see a secret, even if it asks."""

    def test_the_principal_has_no_secret_permission(self):
        agent = ConfigurationAgent()
        permissions = agent.principal.permissions
        self.assertFalse([p for p in permissions if "secret" in p], permissions)
        self.assertEqual(
            set(permissions),
            {"settings_read_source", "settings_read_effective", "settings_validate"},
        )

    def test_no_write_permission_is_granted(self):
        permissions = ConfigurationAgent().principal.permissions
        self.assertNotIn("settings_write", permissions)

    def test_arguments_are_scrubbed_before_they_are_logged(self):
        self.assertEqual(
            _safe_arguments({"path": "/ai/provider/api_key", "api_key": "leaked"}),
            {"path": "/ai/provider/api_key"},
        )


class ToolContentTests(unittest.TestCase):
    """A tool result must never reach the model as half a JSON document."""

    def test_a_tree_is_bounded_by_depth_and_marked_truncated(self):
        """MAX_TREE_DEPTH is 3, so the fourth level must be summarised, not walked."""
        from core.settings.agent import MAX_TREE_DEPTH

        def nest(path, depth):
            node = {"path": path, "type": "object", "kind": "object", "exists": True, "children": []}
            if depth >= MAX_TREE_DEPTH + 1:
                node["type"], node["kind"] = "string", "leaf"
                node["value_text"] = "leaf at the bottom"
                return node
            node["children"] = [nest(f"{path}/d{depth}", depth + 1)]
            return node

        payload = json.loads(_tool_content("settings_get_tree", {"ok": True, "node": nest("/", 0)}))
        node = payload["node"]
        # Walk down to the deepest level the walker is willing to reach: the
        # flag belongs to the node whose children it refused to expand.
        depth = 0
        while node.get("children") and isinstance(node["children"][0], dict) and "path" in node["children"][0]:
            node = node["children"][0]
            depth += 1
        self.assertEqual(depth, MAX_TREE_DEPTH)
        self.assertTrue(node["truncated"])
        self.assertEqual(node["children"], [{"remaining": 1}])

    def test_an_oversized_result_is_summarised_not_cut(self):
        payload = json.loads(_tool_content("settings_get", {"blob": "x" * 40000}))
        self.assertTrue(payload["truncated"])
        self.assertIn("preview", payload)

    def test_a_small_result_is_returned_whole(self):
        payload = json.loads(_tool_content("settings_get", {"value_text": "1"}))
        self.assertEqual(payload["value_text"], "1")
        self.assertNotIn("truncated", payload)

    def test_the_bounded_node_keeps_the_fields_a_model_needs(self):
        node = _bounded_node({"path": "/a", "type": "string", "kind": "leaf",
                              "exists": False, "value_text": None}, [1000])
        for key in ("path", "type", "kind", "exists", "value_text", "children"):
            self.assertIn(key, node)


class ProviderErrorTests(unittest.TestCase):
    """Provider failures are explicit; nothing is reported as success."""

    def _agent(self, provider):
        return ConfigurationAgent(service=object(), base_url="http://provider/v1",
                                  model="local-only")

    def test_an_unreachable_provider_is_reported(self):
        agent = ConfigurationAgent(base_url="http://provider/v1", model="m")
        error = urllib.error.URLError("connection refused")
        with mock.patch.object(urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(AgentError) as caught:
                agent._complete([])
        self.assertEqual(caught.exception.code, "PROVIDER_UNREACHABLE")

    def test_an_http_error_carries_the_provider_message(self):
        agent = ConfigurationAgent(base_url="http://provider/v1", model="m")
        http_error = urllib.error.HTTPError(
            "http://provider/v1/chat/completions", 404, "Not Found", {}, None
        )
        http_error.read = lambda: json.dumps(
            {"error": {"message": "model not found"}}
        ).encode("utf-8")
        try:
            with mock.patch.object(urllib.request, "urlopen", side_effect=http_error):
                with self.assertRaises(AgentError) as caught:
                    agent._complete([])
        finally:
            # Same leak class the logger had: an unclosed file object warns.
            http_error.close()
        self.assertEqual(caught.exception.code, "PROVIDER_HTTP_ERROR")
        self.assertIn("model not found", str(caught.exception))

    def test_a_response_without_choices_is_a_protocol_error(self):
        agent = ConfigurationAgent(base_url="http://provider/v1", model="m")
        provider = FakeProvider([{"id": "x"}])
        with mock.patch.object(urllib.request, "urlopen", provider):
            with self.assertRaises(AgentError) as caught:
                agent._complete([])
        self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")

    def test_every_error_code_maps_to_a_status(self):
        for code in ("BAD_REQUEST", "PROVIDER_UNREACHABLE", "PROVIDER_TIMEOUT"):
            self.assertIn(code, AGENT_ERROR_STATUS)

    def test_the_provider_message_is_extracted_from_json(self):
        self.assertEqual(
            _provider_message('{"error": {"message": "no such model"}}'),
            "no such model",
        )
        self.assertEqual(_provider_message("plain text failure"), "plain text failure")


class BuildAgentTests(unittest.TestCase):
    """Configuration decides whether the assistant runs, and on what."""

    def _effective(self, **ai):
        return {"ai": {"enabled": True, "model": "local-only", "document": "main",
                       "provider": {"type": "openai", "base_url": "http://127.0.0.1:21434/v1",
                                    "api_key": "k"}, **ai}}

    def test_a_complete_section_builds_an_agent(self):
        agent = build_agent(object(), self._effective())
        self.assertIsNotNone(agent)
        self.assertEqual(agent.model, "local-only")
        self.assertEqual(agent.base_url, "http://127.0.0.1:21434/v1")
        self.assertEqual(agent.document, "main")

    def test_disabled_yields_no_agent(self):
        self.assertIsNone(build_agent(object(), {"ai": {"enabled": False}}))

    def test_a_missing_section_yields_no_agent(self):
        self.assertIsNone(build_agent(object(), {}))

    def test_a_half_configured_section_yields_no_agent_not_an_error(self):
        """The pane reports "off"; the rest of the WebUI is unaffected."""
        self.assertIsNone(build_agent(object(), {"ai": {"enabled": True, "model": "m"}}))
        self.assertIsNone(build_agent(object(), {"ai": {
            "enabled": True, "model": "m", "provider": {"base_url": ""}}}))

    def test_the_key_reaches_the_agent_and_never_the_summary(self):
        """The key must be read from provider, and must not travel back out.

        An earlier version of this test put the secret at the top level of the
        `ai` section, where `build_agent` never looks, so it passed even when
        `describe` leaked the key. The secret belongs under `provider`.
        """
        secret = "super-secret-key"
        effective = self._effective()
        effective["ai"]["provider"]["api_key"] = secret
        agent = build_agent(object(), effective)
        self.assertEqual(agent.api_key, secret, "the agent must reach the provider key")
        self.assertNotIn(secret, json.dumps(describe(agent)))
        self.assertNotIn(secret, json.dumps(describe(None)))
        self.assertEqual(describe(agent)["enabled"], True)

    def test_describe_reports_a_disabled_assistant(self):
        self.assertEqual(describe(None), {"enabled": False, "model": None})


class ConversationTests(unittest.TestCase):
    """The loop bounds itself, and never turns a failure into an answer."""

    def test_an_empty_message_is_rejected(self):
        with self.assertRaises(AgentError) as caught:
            ConfigurationAgent().handle({"message": "   "})
        self.assertEqual(caught.exception.code, "BAD_REQUEST")

    def test_a_final_answer_ends_the_loop(self):
        agent = ConfigurationAgent(service=object(), base_url="http://p/v1", model="m")
        provider = FakeProvider([text_answer("Use /webui/port")])
        with mock.patch.object(urllib.request, "urlopen", provider):
            result = agent.handle({"message": "where is the port?", "session": "u1"})
        self.assertEqual(result["message"], "Use /webui/port")
        self.assertEqual(result["staged_operations"], [])
        self.assertEqual(result["rounds"], 1)

    def test_the_request_carries_the_tools_and_never_streams(self):
        agent = ConfigurationAgent(service=object(), base_url="http://p/v1", model="m")
        provider = FakeProvider([text_answer("ok")])
        with mock.patch.object(urllib.request, "urlopen", provider):
            agent.handle({"message": "hi"})
        sent = provider.requests[0]
        self.assertEqual(sent["tool_choice"], "auto")
        self.assertEqual(len(sent["tools"]), 4)
        self.assertNotIn("stream", sent)
        self.assertEqual(sent["messages"][0]["role"], "system")

    def test_the_conversation_is_kept_per_session(self):
        agent = ConfigurationAgent(service=object(), base_url="http://p/v1", model="m")
        provider = FakeProvider([text_answer("one"), text_answer("two")])
        with mock.patch.object(urllib.request, "urlopen", provider):
            agent.handle({"message": "first", "session": "alice"})
            agent.handle({"message": "again", "session": "bob"})
        self.assertEqual([m["content"] for m in agent.history("alice")], ["first", "one"])
        self.assertEqual([m["content"] for m in agent.history("bob")], ["again", "two"])

    def test_forgetting_clears_only_that_session(self):
        agent = ConfigurationAgent()
        agent.sessions.update({"a": [{"role": "user", "content": "x"}], "b": []})
        agent.forget("a")
        self.assertEqual(agent.history("a"), [])
        self.assertEqual(agent.history("b"), [])

    def test_a_malformed_tool_call_is_answered_not_fatal(self):
        """The model is told to retry; the turn still reaches a final answer."""
        agent = ConfigurationAgent(service=object(), base_url="http://p/v1", model="m")
        broken = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "settings_get", "arguments": "]] not json"}}]}}]}
        provider = FakeProvider([broken, text_answer("Sorry, what path?")])
        with mock.patch.object(urllib.request, "urlopen", provider):
            result = agent.handle({"message": "read it"})
        self.assertEqual(result["message"], "Sorry, what path?")
        # The tool message told the model what went wrong.
        second_request = provider.requests[1]["messages"]
        self.assertEqual(second_request[-1]["role"], "tool")
        self.assertIn("AGENT_BAD_ARGUMENTS", second_request[-1]["content"])

    def test_the_round_limit_is_bounded(self):
        """A model that keeps calling tools stops instead of looping forever."""
        agent = ConfigurationAgent(service=object(), base_url="http://p/v1",
                                   model="m", max_tool_rounds=2)
        agent._run_tool = lambda *a: "{}"          # a tool that always succeeds
        loop = tool_round("settings_get", {"path": "/a"})
        provider = FakeProvider([loop, loop, loop, loop])
        with mock.patch.object(urllib.request, "urlopen", provider):
            result = agent.handle({"message": "go"})
        self.assertEqual(result["rounds"], 2)
        self.assertIn("round limit", result["message"])
        self.assertEqual(len(provider.requests), 2, "the loop exceeded its bound")

    def test_the_history_is_trimmed_to_the_configured_length(self):
        agent = ConfigurationAgent(history_messages=4)
        history = [{"role": "user", "content": str(i)} for i in range(10)]
        agent._trim(history)
        self.assertEqual(len(history), 4)

    def test_the_history_is_trimmed_by_size_too(self):
        agent = ConfigurationAgent(history_messages=100)
        history = [{"role": "user", "content": "x" * 9000} for _ in range(10)]
        agent._trim(history)
        self.assertLessEqual(sum(len(m["content"]) for m in history), 24000)


class RouteTests(unittest.TestCase):
    """The chat routes require a session and never answer for a disabled agent."""

    def setUp(self):
        import test_settings_backend as mount_tests

        mount_tests.WebUIMountTests.setUpClass()
        self.client = mount_tests.WebUIMountTests.client

    def test_every_chat_route_requires_a_session(self):
        for method, path in (
            ("get", "/api/settings/chat/history"),
            ("post", "/api/settings/chat"),
            ("delete", "/api/settings/chat"),
        ):
            with self.subTest(route=path):
                call = getattr(self.client, method)
                response = call(path) if method != "post" else call(path, json={"message": "hi"})
                self.assertEqual(response.status_code, 401)

    def test_settings_info_reports_the_assistant(self):
        """The pane builds its header from this, so the key must never appear."""
        response = self.client.get("/api/settings-info")
        self.assertEqual(response.status_code, 401)   # no session: nothing is disclosed


if __name__ == "__main__":
    unittest.main()
