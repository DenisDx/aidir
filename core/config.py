"""
Configuration management.
Loads config.json (JSON5) with ${VAR} / ${VAR:-default} substitution from .env.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import json5
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

_ENV_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}')
_KEY_RE = re.compile(r'^\s*("(?:\\.|[^"])+"|[A-Za-z_][A-Za-z0-9_]*)\s*[:=]')


def _substitute(text: str) -> str:
    """Replace ${VAR} and ${VAR:-default} with env values."""
    def _replace(m: re.Match) -> str:
        var, default = m.group(1), m.group(2) if m.group(2) is not None else ""
        return os.environ.get(var, default)
    return _ENV_RE.sub(_replace, text)


def _split_code_and_comment(line: str) -> tuple[str, str | None]:
    """Split line into code and //comment parts, ignoring // inside strings."""
    in_string = False
    escaped = False
    for i in range(len(line) - 1):
        ch = line[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "/" and line[i + 1] == "/":
            return line[:i], line[i:]

    return line, None


def _leading_close_count(code: str) -> int:
    """Count leading object-closing braces in a line (`}` prefixes)."""
    i = 0
    count = 0
    while i < len(code):
        ch = code[i]
        if ch in " \t,]":
            i += 1
            continue
        if ch == "}":
            count += 1
            i += 1
            continue
        break
    return count


def _count_braces(code: str) -> tuple[int, int]:
    """Count { and } in code, ignoring string literals."""
    opens = 0
    closes = 0
    in_string = False
    escaped = False
    for ch in code:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            opens += 1
        elif ch == "}":
            closes += 1
    return opens, closes


def _extract_key(code: str) -> str | None:
    """Extract object key from line if it starts with key/value pair."""
    m = _KEY_RE.match(code)
    if not m:
        return None
    key = m.group(1)
    if key.startswith('"') and key.endswith('"'):
        return key[1:-1]
    return key


def _current_named_path(stack: list[str | None]) -> list[str]:
    """Return only named path parts from mixed named/anonymous stack."""
    return [p for p in stack if p is not None]


def _collect_key_positions(text: str) -> dict[str, int]:
    """Collect line index for each config key path in text."""
    positions: dict[str, int] = {}
    stack: list[str | None] = []

    for i, line in enumerate(text.splitlines()):
        code, _ = _split_code_and_comment(line)
        stripped = code.strip()
        if not stripped:
            continue

        for _ in range(_leading_close_count(code)):
            if stack:
                stack.pop()

        key = _extract_key(code)
        if key is not None:
            path = ".".join(_current_named_path(stack) + [key])
            positions[path] = i
            if re.search(r'[:=]\s*\{', code):
                stack.append(key)
            else:
                opens, closes = _count_braces(code)
                delta = opens - closes
                for _ in range(max(0, delta)):
                    stack.append(None)
        else:
            opens, closes = _count_braces(code)
            delta = opens - closes
            for _ in range(max(0, delta)):
                stack.append(None)

        # Extra trailing closes in this line (beyond prefix) collapse stack.
        if key is not None and re.search(r'[:=]\s*\{', code):
            opens, closes = _count_braces(code)
            extra = max(0, closes - 1)
            for _ in range(extra):
                if stack:
                    stack.pop()

    return positions


def _extract_comments_by_path(text: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Extract before-line and inline comments keyed by config path."""
    before: dict[str, list[str]] = {}
    inline: dict[str, str] = {}
    stack: list[str | None] = []
    pending_before: list[str] = []

    for line in text.splitlines():
        code, comment = _split_code_and_comment(line)
        stripped_code = code.strip()

        # Pure comment line (no key code): remember as "before" block.
        if not stripped_code and comment and line.lstrip().startswith("//"):
            pending_before.append(comment.strip())
            continue

        if not stripped_code:
            pending_before.clear()
            continue

        for _ in range(_leading_close_count(code)):
            if stack:
                stack.pop()

        key = _extract_key(code)
        if key is not None:
            path = ".".join(_current_named_path(stack) + [key])
            if pending_before:
                before[path] = pending_before[:]
                pending_before.clear()
            if comment:
                inline[path] = comment.strip()

            if re.search(r'[:=]\s*\{', code):
                stack.append(key)
            else:
                opens, closes = _count_braces(code)
                delta = opens - closes
                for _ in range(max(0, delta)):
                    stack.append(None)
        else:
            pending_before.clear()
            opens, closes = _count_braces(code)
            delta = opens - closes
            for _ in range(max(0, delta)):
                stack.append(None)

        if key is not None and re.search(r'[:=]\s*\{', code):
            opens, closes = _count_braces(code)
            extra = max(0, closes - 1)
            for _ in range(extra):
                if stack:
                    stack.pop()

    return before, inline


def _extract_header_comments(text: str) -> list[str]:
    """Extract leading line comments before the first non-comment content."""
    header: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if header:
                header.append("")
            continue
        if stripped.startswith("//"):
            header.append(line.rstrip())
            continue
        break
    while header and header[-1] == "":
        header.pop()
    return header


def _reapply_header_comments(text: str, header_comments: list[str]) -> str:
    """Reapply leading file header comments before the config body."""
    if not header_comments:
        return text if text.endswith("\n") else text + "\n"
    body = text.lstrip("\n")
    return "\n".join(header_comments) + "\n" + body


def _extract_raw_values_by_path(text: str) -> dict[str, str]:
    """Extract raw scalar RHS values by key path from original text."""
    raw_values: dict[str, str] = {}
    stack: list[str | None] = []

    for line in text.splitlines():
        code, _ = _split_code_and_comment(line)
        stripped_code = code.strip()
        if not stripped_code:
            continue

        for _ in range(_leading_close_count(code)):
            if stack:
                stack.pop()

        key = _extract_key(code)
        if key is not None:
            path = ".".join(_current_named_path(stack) + [key])
            m = re.match(
                r'^\s*("(?:\\.|[^"])+"|[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*?)\s*,?\s*$',
                code,
            )
            if m:
                rhs = m.group(2).strip()
                # Keep only scalar placeholder-backed values for re-application.
                if rhs and not rhs.startswith("{") and not rhs.startswith("[") and "${" in rhs:
                    raw_values[path] = rhs

            if re.search(r'[:=]\s*\{', code):
                stack.append(key)
            else:
                opens, closes = _count_braces(code)
                delta = opens - closes
                for _ in range(max(0, delta)):
                    stack.append(None)
        else:
            opens, closes = _count_braces(code)
            delta = opens - closes
            for _ in range(max(0, delta)):
                stack.append(None)

        if key is not None and re.search(r'[:=]\s*\{', code):
            opens, closes = _count_braces(code)
            extra = max(0, closes - 1)
            for _ in range(extra):
                if stack:
                    stack.pop()

    return raw_values


def _extract_scalar_rhs_by_path(text: str) -> dict[str, str]:
    """Extract scalar RHS text by key path from original config file."""
    values: dict[str, str] = {}
    stack: list[str | None] = []

    for line in text.splitlines():
        code, _ = _split_code_and_comment(line)
        stripped_code = code.strip()
        if not stripped_code:
            continue

        for _ in range(_leading_close_count(code)):
            if stack:
                stack.pop()

        key = _extract_key(code)
        if key is not None:
            path = ".".join(_current_named_path(stack) + [key])
            m = re.match(
                r'^\s*("(?:\\.|[^"])+"|[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*?)\s*,?\s*$',
                code,
            )
            if m:
                rhs = m.group(2).strip()
                if rhs and not rhs.startswith("{") and not rhs.startswith("["):
                    values[path] = rhs

            if re.search(r'[:=]\s*\{', code):
                stack.append(key)
            else:
                opens, closes = _count_braces(code)
                delta = opens - closes
                for _ in range(max(0, delta)):
                    stack.append(None)
        else:
            opens, closes = _count_braces(code)
            delta = opens - closes
            for _ in range(max(0, delta)):
                stack.append(None)

        if key is not None and re.search(r'[:=]\s*\{', code):
            opens, closes = _count_braces(code)
            extra = max(0, closes - 1)
            for _ in range(extra):
                if stack:
                    stack.pop()

    return values


def _reapply_comments(text: str, before: dict[str, list[str]], inline: dict[str, str]) -> str:
    """Reapply extracted comments to regenerated config text by key paths."""
    lines = text.splitlines()
    positions = _collect_key_positions(text)

    # Inline comments: mutate target key lines in place.
    for path, comment in inline.items():
        idx = positions.get(path)
        if idx is None:
            continue
        if "//" in lines[idx]:
            continue
        lines[idx] = f"{lines[idx]} {comment}"

    # Before comments: insert in reverse line order to keep indexes stable.
    inserts: list[tuple[int, list[str]]] = []
    for path, comment_lines in before.items():
        idx = positions.get(path)
        if idx is None or not comment_lines:
            continue
        indent = re.match(r'^\s*', lines[idx]).group(0)
        rendered = [f"{indent}{c.strip()}" for c in comment_lines]
        inserts.append((idx, rendered))

    for idx, rendered in sorted(inserts, key=lambda x: x[0], reverse=True):
        lines[idx:idx] = rendered

    return "\n".join(lines) + "\n"


def _reapply_raw_values(text: str, raw_values: dict[str, str], skip_paths: set[str]) -> str:
    """Reapply original raw RHS values (e.g. ${ENV}) for untouched key paths."""
    lines = text.splitlines()
    positions = _collect_key_positions(text)

    for path, rhs in raw_values.items():
        if path in skip_paths:
            continue
        idx = positions.get(path)
        if idx is None:
            continue

        code, comment = _split_code_and_comment(lines[idx])
        m = re.match(r'^(\s*"(?:\\.|[^"])+"\s*:\s*)(.*?)(\s*,?\s*)$', code)
        if not m:
            continue

        prefix, _, suffix = m.groups()
        new_line = f"{prefix}{rhs}{suffix}"
        if comment:
            new_line = f"{new_line.rstrip()} {comment}"
        lines[idx] = new_line

    return "\n".join(lines) + "\n"


class Config:
    """
    Loads and caches config.json; supports dot-path access and hot reload.
    Substitutes ${ENV_VAR} tokens from environment / .env before parsing.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (_ROOT / "config.json")
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        """Load (or reload) config from disk."""
        raw = self._path.read_text(encoding="utf-8")
        raw = _substitute(raw)
        self._data = json5.loads(raw)

    def validate_config(self, config_text: str) -> tuple[bool, str]:
        """Validate config text syntax and required version; returns (ok, message)."""
        try:
            parsed = json5.loads(_substitute(config_text))
        except Exception as exc:
            return False, f"Config syntax error: {exc}"

        version = parsed.get("version") if isinstance(parsed, dict) else None
        if version != "1.0":
            return False, "Invalid config version: expected '1.0'"
        return True, "ok"

    def save_config_text(self, config_text: str) -> None:
        """Validate and persist full config text, then reload in-memory cache."""
        ok, message = self.validate_config(config_text)
        if not ok:
            raise ValueError(message)
        self._path.write_text(config_text, encoding="utf-8")
        self.load()

    def update_key(self, key: str, value) -> None:
        """Update one dot-path key in config file, validate, save and reload."""
        raw = self._path.read_text(encoding="utf-8")
        header_comments = _extract_header_comments(raw)
        before_comments, inline_comments = _extract_comments_by_path(raw)
        raw_values = _extract_raw_values_by_path(raw)

        parsed = json.loads(json.dumps(self._data))

        node = parsed
        parts = key.split(".")
        if not parts:
            raise ValueError("Key path is empty")

        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"Unknown config key path: {key}")
            node = node[part]

        if not isinstance(node, dict):
            raise ValueError(f"Invalid config key path: {key}")

        node[parts[-1]] = value
        new_text = json.dumps(parsed, ensure_ascii=True, indent=2)
        new_text = _reapply_raw_values(new_text, raw_values, {key})
        new_text = _reapply_comments(new_text, before_comments, inline_comments)
        new_text = _reapply_header_comments(new_text, header_comments)
        self.save_config_text(new_text)

    def get_key_text(self, key: str) -> str:
        """Return key value as raw text from file (for GUI field editing)."""
        raw = self._path.read_text(encoding="utf-8")
        values = _extract_scalar_rhs_by_path(raw)
        if key in values:
            return values[key]

        value = self.get(key, None)
        if value is None:
            raise ValueError(f"Unknown config key path: {key}")
        return json.dumps(value, ensure_ascii=True)

    def get_key_text_or_none(self, key: str) -> str | None:
        """Return key value text or None when key does not exist."""
        raw = self._path.read_text(encoding="utf-8")
        values = _extract_scalar_rhs_by_path(raw)
        if key in values:
            return values[key]

        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            return None
        return json.dumps(value, ensure_ascii=True)

    def update_key_text(self, key: str, value_text: str) -> None:
        """Update one key using raw text value (supports placeholders like ${VAR})."""
        if not isinstance(value_text, str):
            raise ValueError("value_text must be a string")

        value_text = value_text.strip()
        if not value_text:
            raise ValueError("value_text must not be empty")

        # Parse to runtime value for internal validation and data update.
        substituted = _substitute(value_text)
        parsed_value = None
        try:
            parsed_value = json5.loads(f"{{v: {substituted}}}")["v"]
            rhs_for_write = value_text
        except Exception:
            # Plain text fallback for string-like fields (e.g., info without quotes).
            parsed_value = value_text
            rhs_for_write = json.dumps(value_text, ensure_ascii=True)

        raw = self._path.read_text(encoding="utf-8")
        header_comments = _extract_header_comments(raw)
        before_comments, inline_comments = _extract_comments_by_path(raw)
        raw_values = _extract_raw_values_by_path(raw)

        parsed = json.loads(json.dumps(self._data))
        node = parsed
        parts = key.split(".")
        if not parts:
            raise ValueError("Key path is empty")

        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"Unknown config key path: {key}")
            node = node[part]

        if not isinstance(node, dict):
            raise ValueError(f"Invalid config key path: {key}")

        node[parts[-1]] = parsed_value

        new_text = json.dumps(parsed, ensure_ascii=True, indent=2)
        raw_values[key] = rhs_for_write
        new_text = _reapply_raw_values(new_text, raw_values, set())
        new_text = _reapply_comments(new_text, before_comments, inline_comments)
        new_text = _reapply_header_comments(new_text, header_comments)
        self.save_config_text(new_text)

    def delete_key(self, key: str) -> None:
        """Delete one dot-path key from config file, preserve comments/raw values for others."""
        raw = self._path.read_text(encoding="utf-8")
        header_comments = _extract_header_comments(raw)
        before_comments, inline_comments = _extract_comments_by_path(raw)
        raw_values = _extract_raw_values_by_path(raw)

        parsed = json.loads(json.dumps(self._data))
        node = parsed
        parts = key.split(".")
        if not parts:
            raise ValueError("Key path is empty")

        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return
            node = node[part]

        if not isinstance(node, dict):
            raise ValueError(f"Invalid config key path: {key}")

        node.pop(parts[-1], None)

        new_text = json.dumps(parsed, ensure_ascii=True, indent=2)
        filtered_raw_values = {
            path: rhs for path, rhs in raw_values.items()
            if path != key and not path.startswith(f"{key}.")
        }
        new_text = _reapply_raw_values(new_text, filtered_raw_values, set())
        new_text = _reapply_comments(new_text, before_comments, inline_comments)
        new_text = _reapply_header_comments(new_text, header_comments)
        self.save_config_text(new_text)

    def get(self, key: str, default=None):
        """Dot-path lookup: config.get('logging.level') → value or default."""
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def __getitem__(self, key: str):
        return self._data[key]

    def raw(self) -> dict:
        """Return the full config dict."""
        return self._data

    def raw_text(self) -> str:
        """Return config file text as stored on disk."""
        return self._path.read_text(encoding="utf-8")


# Global singleton – imported by other modules
config = Config()
