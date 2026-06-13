"""http_api tool worker.

Stage 2 implements real outbound HTTP calls, auth handling, response hooks,
and normalized envelope output.
"""
from __future__ import annotations

import importlib.util
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from core.task import Task
from core.worker import BaseToolWorker, WorkerResult


_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class HttpApiWorker(BaseToolWorker):
    """Tool worker for configured HTTP API connectors."""

    task_type = "tool"
    _ALLOWED_METHODS = {"GET", "POST"}
    _MAX_AUTO_PAGES = 20
    _DEFAULT_AUTO_PAGES = 5

    def __init__(self) -> None:
        super().__init__()
        self._request_timeout = 30
        self._user_agent = "aidir-http-api/1.0"
        self._max_response_chars = 50000
        self._connectors: dict[str, dict[str, Any]] = {}

    def get_tool_description(self) -> list[dict[str, Any]]:
        """Return MCP-style tool schema for http_api."""
        return [{
            "name": "http_api",
            "description": "Call a configured token-authenticated HTTP API connector and return normalized structured data.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector": {"type": "string"},
                    "operation": {"type": "string"},
                    "params": {"type": "object"},
                    "page_token": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["connector", "operation"],
            },
        }]

    async def initialize(self, config: dict) -> None:
        """Load worker defaults and connectors from config."""
        self._request_timeout = self._safe_int(config.get("request_timeout"), 30, min_value=1)
        self._user_agent = str(config.get("user_agent") or "aidir-http-api/1.0").strip() or "aidir-http-api/1.0"
        self._max_response_chars = self._safe_int(config.get("max_response_chars"), 50000, min_value=1024)
        connectors = config.get("connectors")
        self._connectors = dict(connectors) if isinstance(connectors, dict) else {}

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Execute configured connector operation and return normalized envelope."""
        payload = task.payload if isinstance(task.payload, dict) else {}
        args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}

        connector_name = str(args.get("connector") or "").strip()
        operation_name = str(args.get("operation") or "").strip()
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        page_token = args.get("page_token")

        if not connector_name:
            return self._error("HTTP_API_INVALID_ARGUMENT", "connector is required")
        if not operation_name:
            return self._error("HTTP_API_INVALID_ARGUMENT", "operation is required")

        connector_cfg = self._connectors.get(connector_name)
        if not isinstance(connector_cfg, dict):
            return self._error("HTTP_API_UNKNOWN_CONNECTOR", f"Unknown connector: {connector_name}")
        if connector_cfg.get("enabled") is False:
            return self._error("HTTP_API_CONNECTOR_DISABLED", f"Connector disabled: {connector_name}")

        operations = connector_cfg.get("operations") if isinstance(connector_cfg.get("operations"), dict) else {}
        operation_cfg = operations.get(operation_name)
        if not isinstance(operation_cfg, dict):
            return self._error("HTTP_API_UNKNOWN_OPERATION", f"Unknown operation: {operation_name}")

        allowed_params = operation_cfg.get("allowed_params")
        if isinstance(allowed_params, list):
            allowed_set = {str(item) for item in allowed_params}
            unknown = sorted([key for key in params.keys() if key not in allowed_set])
            if unknown:
                return self._error("HTTP_API_INVALID_ARGUMENT", f"Unknown params: {', '.join(unknown)}")

        method = str(operation_cfg.get("method") or "GET").upper()
        if method not in self._ALLOWED_METHODS:
            return self._error("HTTP_API_METHOD_NOT_ALLOWED", f"Unsupported method: {method}")

        template_values = self._build_template_values(params, connector_cfg)
        if page_token is not None and page_token != "":
            template_values.setdefault("page_token", page_token)

        try:
            rendered_query = self._render_structure(operation_cfg.get("query"), template_values)
            rendered_headers = self._render_structure(operation_cfg.get("headers"), template_values)
            rendered_json = self._render_structure(operation_cfg.get("json"), template_values)
            rendered_path = self._render_path(str(operation_cfg.get("path") or ""), template_values)
        except ValueError as exc:
            return self._error("HTTP_API_TEMPLATE_ERROR", str(exc))

        query = rendered_query if isinstance(rendered_query, dict) else {}
        headers = rendered_headers if isinstance(rendered_headers, dict) else {}

        base_url = str(connector_cfg.get("base_url") or "").rstrip("/")
        if not base_url:
            return self._error("HTTP_API_INVALID_CONFIG", f"connector {connector_name} has no base_url")

        pagination_cfg = operation_cfg.get("pagination") if isinstance(operation_cfg.get("pagination"), dict) else {}
        paging_type = str(pagination_cfg.get("type") or "none").strip().lower()
        if paging_type not in {"none", "cursor", "page", "offset_limit"}:
            return self._error("HTTP_API_PAGINATION_UNSUPPORTED", f"Unsupported pagination type: {paging_type}")

        timeout_seconds = self._safe_int(
            operation_cfg.get("timeout_seconds") if operation_cfg.get("timeout_seconds") is not None else self._request_timeout,
            self._request_timeout,
            min_value=1,
        )

        auth_headers, auth_query, auth_error = self._build_auth_parts(connector_cfg.get("auth"))
        if auth_error is not None:
            return WorkerResult(ok=False, error=auth_error)
        headers.update(auth_headers)
        query.update(auth_query)

        if "User-Agent" not in {str(k): v for k, v in headers.items()}:
            headers["User-Agent"] = self._user_agent

        url = f"{base_url}{rendered_path}"
        response_hook_file = operation_cfg.get("response_hook_file")
        hook_transform: Callable[[dict, dict], dict] | None = None
        if isinstance(response_hook_file, str) and response_hook_file.strip():
            hook_transform, hook_error = self._load_hook_transform(response_hook_file.strip(), connector_name, operation_name)
            if hook_error is not None:
                return WorkerResult(ok=False, error=hook_error)

        requested_limit = self._safe_optional_int(args.get("limit"), min_value=1)
        requested_max_pages = self._safe_optional_int(args.get("max_pages"), min_value=1)
        cfg_max_pages = self._safe_optional_int(pagination_cfg.get("max_pages"), min_value=1)

        if paging_type == "none":
            effective_max_pages = 1
        else:
            effective_max_pages = requested_max_pages or cfg_max_pages or self._DEFAULT_AUTO_PAGES
            effective_max_pages = max(1, min(self._MAX_AUTO_PAGES, effective_max_pages))

        mode = str(operation_cfg.get("mode") or "list").strip().lower()
        result_path = str(operation_cfg.get("result_path") or "").strip()

        request_param = str(pagination_cfg.get("request_param") or "").strip()
        cursor_token = str(page_token) if page_token not in {None, ""} else str(pagination_cfg.get("initial_page_token") or "")
        page_index = self._safe_int(args.get("page"), self._safe_int(pagination_cfg.get("start_page"), 1, min_value=1), min_value=1)
        offset_value = self._safe_int(args.get("offset"), self._safe_int(pagination_cfg.get("start_offset"), 0, min_value=0), min_value=0)

        offset_param = str(pagination_cfg.get("offset_param") or "offset").strip() or "offset"
        offset_limit_param = str(pagination_cfg.get("limit_param") or "limit").strip() or "limit"
        offset_page_size = self._safe_int(
            pagination_cfg.get("page_size"),
            requested_limit or self._safe_int(args.get("page_size"), 50, min_value=1),
            min_value=1,
        )

        total_duration_ms = 0
        pages_fetched = 0
        last_request_id = ""
        last_content_type = ""
        last_raw_preview = ""
        last_payload: Any = {}
        last_next_token: Any = None
        has_more = False

        item: dict[str, Any] | None = None
        items: list[Any] = []

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                for _ in range(effective_max_pages):
                    page_query = dict(query)

                    remaining = None
                    if requested_limit is not None:
                        remaining = max(0, requested_limit - len(items))
                        if remaining == 0 and mode != "item":
                            has_more = False
                            break

                    current_offset_limit = offset_page_size
                    if remaining is not None and remaining > 0:
                        current_offset_limit = max(1, min(offset_page_size, remaining))

                    if paging_type == "cursor" and request_param and cursor_token:
                        page_query[request_param] = cursor_token
                    elif paging_type == "page":
                        page_param = request_param or "page"
                        page_query[page_param] = page_index
                    elif paging_type == "offset_limit":
                        page_query[offset_param] = offset_value
                        page_query[offset_limit_param] = current_offset_limit

                    request_started = time.perf_counter()
                    if method == "GET":
                        response = await client.get(url, params=page_query, headers=headers)
                    else:
                        response = await client.post(
                            url,
                            params=page_query,
                            json=rendered_json if isinstance(rendered_json, (dict, list)) else None,
                            headers=headers,
                        )
                    page_duration_ms = int((time.perf_counter() - request_started) * 1000)
                    total_duration_ms += page_duration_ms

                    last_content_type = str(response.headers.get("content-type") or "")
                    last_request_id = response.headers.get("x-request-id") or response.headers.get("x-amzn-requestid") or ""

                    parsed_body, raw_preview = self._parse_response_body(response)
                    last_raw_preview = raw_preview

                    if hook_transform is not None:
                        parsed_body = hook_transform(
                            parsed_body,
                            {
                                "connector": connector_name,
                                "operation": operation_name,
                                "status_code": response.status_code,
                                "request": {
                                    "method": method,
                                    "url": url,
                                    "query": page_query,
                                },
                                "paging": {
                                    "type": paging_type,
                                    "page_index": page_index,
                                    "offset": offset_value,
                                },
                            },
                        )

                    if response.status_code >= 400:
                        return WorkerResult(
                            ok=False,
                            error={
                                "code": "HTTP_API_HTTP_ERROR",
                                "message": f"Remote API returned HTTP {response.status_code}",
                                "status_code": response.status_code,
                                "details": raw_preview,
                            },
                        )

                    selected_payload = self._extract_result_path(parsed_body, result_path)
                    last_payload = parsed_body
                    pages_fetched += 1

                    if mode == "item":
                        item = selected_payload if isinstance(selected_payload, dict) else None
                        has_more = False
                        break

                    page_items = self._coerce_items(selected_payload)
                    if page_items:
                        items.extend(page_items)
                        if requested_limit is not None and len(items) > requested_limit:
                            items = items[:requested_limit]

                    last_next_token = self._extract_result_path(parsed_body, str(pagination_cfg.get("response_field") or "").strip())

                    if paging_type == "none":
                        has_more = False
                        break

                    if paging_type == "cursor":
                        cursor_token = str(last_next_token) if last_next_token not in {None, ""} else ""
                        has_more = bool(cursor_token)
                    elif paging_type == "page":
                        if str(pagination_cfg.get("response_field") or "").strip():
                            has_more = bool(last_next_token)
                        else:
                            has_more = len(page_items) > 0
                        page_index += 1
                    else:  # offset_limit
                        if str(pagination_cfg.get("response_field") or "").strip():
                            if isinstance(last_next_token, int):
                                offset_value = max(0, int(last_next_token))
                                has_more = True
                            elif isinstance(last_next_token, str) and last_next_token.strip().isdigit():
                                offset_value = max(0, int(last_next_token.strip()))
                                has_more = True
                            else:
                                has_more = bool(last_next_token)
                                offset_value += current_offset_limit
                        else:
                            has_more = len(page_items) >= current_offset_limit
                            offset_value += current_offset_limit

                    if requested_limit is not None and len(items) >= requested_limit:
                        has_more = has_more or (paging_type != "none")
                        break

                    if not has_more:
                        break
        except Exception as exc:
            return self._error("HTTP_API_REQUEST_FAILED", f"Request failed: {type(exc).__name__}")
        except BaseException:
            return self._error("HTTP_API_REQUEST_FAILED", "Request failed: unknown")

        paging = {
            "type": paging_type,
            "next_page_token": str(last_next_token) if last_next_token not in {None, ""} else None,
            "has_more": bool(has_more),
            "pages_fetched": pages_fetched,
            "items_returned": int(1 if item is not None else len(items)),
            "input_page_token": str(page_token) if page_token not in {None, ""} else None,
        }

        if paging_type == "page" and has_more:
            paging["next_page"] = page_index
        if paging_type == "offset_limit" and has_more:
            paging["next_offset"] = offset_value

        envelope = {
            "ok": True,
            "connector": connector_name,
            "operation": operation_name,
            "status_code": 200,
            "item": item,
            "items": items,
            "paging": paging,
            "meta": {
                "content_type": last_content_type,
                "duration_ms": total_duration_ms,
                "request_id": last_request_id,
            },
        }

        if not isinstance(last_payload, (dict, list)) and last_raw_preview:
            envelope["meta"]["response_preview"] = last_raw_preview

        return WorkerResult(ok=True, data=envelope)

    @staticmethod
    def _safe_int(value: Any, default: int, min_value: int = 0) -> int:
        """Convert values to bounded int with fallback."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(min_value, parsed)

    @staticmethod
    def _safe_optional_int(value: Any, min_value: int = 0) -> int | None:
        """Parse optional integer with lower bound; return None on invalid/missing values."""
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return max(min_value, parsed)

    @staticmethod
    def _build_template_values(params: dict[str, Any], connector_cfg: dict[str, Any]) -> dict[str, Any]:
        """Build template values from params and connector defaults."""
        values = dict(connector_cfg.get("defaults") or {}) if isinstance(connector_cfg.get("defaults"), dict) else {}
        values.update(params)
        return values

    @staticmethod
    def _render_path(path: str, values: dict[str, Any]) -> str:
        """Render path with flat placeholder replacement."""
        if not path:
            return ""
        if not path.startswith("/"):
            path = f"/{path}"
        return HttpApiWorker._render_string(path, values)

    @staticmethod
    def _render_structure(node: Any, values: dict[str, Any]) -> Any:
        """Render placeholders in dict/list/string structures."""
        if isinstance(node, str):
            return HttpApiWorker._render_string(node, values)
        if isinstance(node, list):
            return [HttpApiWorker._render_structure(item, values) for item in node]
        if isinstance(node, dict):
            return {key: HttpApiWorker._render_structure(val, values) for key, val in node.items()}
        return node

    @staticmethod
    def _render_string(template: str, values: dict[str, Any]) -> str:
        """Replace placeholders like {name} using provided values."""
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                raise ValueError(f"Missing template variable: {key}")
            return str(values.get(key, ""))

        return _TEMPLATE_RE.sub(replace, template)

    def _load_hook_transform(
        self,
        file_path: str,
        connector: str,
        operation: str,
    ) -> tuple[Callable[[dict, dict], dict] | None, dict[str, Any] | None]:
        """Load hook module and return transform callable or normalized error."""
        resolved = Path(file_path).expanduser()
        if not resolved.is_absolute():
            resolved = (Path.cwd() / resolved).resolve()

        if not resolved.exists() or not resolved.is_file():
            return None, {
                "code": "HTTP_API_HOOK_NOT_FOUND",
                "message": f"response_hook_file not found: {resolved}",
            }

        try:
            module_name = f"http_api_hook_{connector}_{operation}".replace("-", "_")
            spec = importlib.util.spec_from_file_location(module_name, str(resolved))
            if spec is None or spec.loader is None:
                raise RuntimeError("cannot create module spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            transform = getattr(module, "transform_response", None)
            if not callable(transform):
                raise RuntimeError("transform_response(response, context) callable is required")
        except Exception as exc:
            return None, {
                "code": "HTTP_API_HOOK_INVALID",
                "message": str(exc) or "invalid hook module",
            }

        return transform, None

    def _build_auth_parts(self, auth_cfg: Any) -> tuple[dict[str, str], dict[str, str], dict[str, Any] | None]:
        """Build auth headers/query fragments from connector auth config."""
        if not isinstance(auth_cfg, dict):
            return {}, {}, None

        auth_type = str(auth_cfg.get("type") or "none").strip().lower()
        if auth_type == "none":
            return {}, {}, None

        inline_secret = str(
            auth_cfg.get("key")
            or auth_cfg.get("apikey")
            or auth_cfg.get("apiKey")
            or auth_cfg.get("token")
            or ""
        ).strip()

        if auth_type in {"bearer", "header", "query"}:
            secret = inline_secret
            if not secret:
                return {}, {}, {
                    "code": "HTTP_API_AUTH_MISSING",
                    "message": "missing auth key/apikey/token",
                }

        elif auth_type in {"bearer_file", "header_file"}:
            path_raw = str(auth_cfg.get("path") or "").strip()
            if not path_raw:
                return {}, {}, {"code": "HTTP_API_AUTH_MISSING", "message": "missing auth file path"}
            path = Path(path_raw).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            try:
                secret = path.read_text(encoding="utf-8").strip()
            except Exception:
                return {}, {}, {
                    "code": "HTTP_API_AUTH_MISSING",
                    "message": f"unable to read auth file: {path}",
                }
            if not secret:
                return {}, {}, {
                    "code": "HTTP_API_AUTH_MISSING",
                    "message": f"empty auth file: {path}",
                }
        else:
            return {}, {}, {
                "code": "HTTP_API_AUTH_UNSUPPORTED",
                "message": f"unsupported auth type: {auth_type}",
            }

        if auth_type in {"bearer", "bearer_file"}:
            return {"Authorization": f"Bearer {secret}"}, {}, None
        if auth_type in {"header", "header_file"}:
            header_name = str(auth_cfg.get("header") or "").strip()
            if not header_name:
                return {}, {}, {"code": "HTTP_API_AUTH_INVALID", "message": "auth header name is required"}
            return {header_name: secret}, {}, None
        if auth_type == "query":
            param_name = str(auth_cfg.get("param") or "api_key").strip() or "api_key"
            return {}, {param_name: secret}, None
        return {}, {}, None

    def _parse_response_body(self, response: httpx.Response) -> tuple[Any, str]:
        """Parse response body as JSON when possible and return safe text preview."""
        text = response.text or ""
        preview = self._truncate_text(text)

        content_type = str(response.headers.get("content-type") or "").lower()
        if "json" in content_type:
            try:
                return response.json(), preview
            except Exception:
                return {"raw_text": preview}, preview

        try:
            return response.json(), preview
        except Exception:
            return {"raw_text": preview}, preview

    def _extract_result_path(self, payload: Any, result_path: str) -> Any:
        """Extract data from payload using dot-separated result_path."""
        if not result_path:
            return payload

        current = payload
        for token in result_path.split("."):
            token = token.strip()
            if not token:
                continue
            if isinstance(current, dict) and token in current:
                current = current[token]
                continue
            return None
        return current

    @staticmethod
    def _coerce_items(payload: Any) -> list[Any]:
        """Normalize payload slice to list form for list-mode envelope."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return [payload]
        return []

    def _extract_paging(
        self,
        payload: Any,
        pagination_cfg: dict[str, Any],
        items_returned: int,
        page_token: Any,
    ) -> dict[str, Any]:
        """Build normalized paging metadata for current response."""
        paging_type = str(pagination_cfg.get("type") or "none").strip().lower() if isinstance(pagination_cfg, dict) else "none"
        response_field = str(pagination_cfg.get("response_field") or "").strip() if isinstance(pagination_cfg, dict) else ""

        next_page_token = None
        if response_field:
            next_page_token = self._extract_result_path(payload, response_field)

        return {
            "type": paging_type,
            "next_page_token": str(next_page_token) if next_page_token not in {None, ""} else None,
            "has_more": bool(next_page_token),
            "pages_fetched": 1,
            "items_returned": int(items_returned),
            "input_page_token": str(page_token) if page_token not in {None, ""} else None,
        }

    def _truncate_text(self, value: str) -> str:
        """Truncate text to worker-level safe response preview size."""
        if len(value) <= self._max_response_chars:
            return value
        return value[: self._max_response_chars]

    @staticmethod
    def _error(code: str, message: str) -> WorkerResult:
        """Return standardized worker error."""
        return WorkerResult(ok=False, error={"code": code, "message": message})


worker = HttpApiWorker()
