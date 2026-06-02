"""Replay one raw Ollama request under load and flag suspicious response corruption."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from core.config import Config


@dataclass(slots=True)
class AttemptResult:
    """Store one replay attempt result and derived diagnostics."""

    index: int
    status_code: int
    duration_ms: int
    response_text: str
    response_json: dict[str, Any] | None
    invalid_json: bool
    suspicious_overlap: bool
    overlap_offset: int
    overlap_length: int
    suspicious_prefix: bool
    suspicion_reasons: list[str]
    error: str = ""


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the interference diagnostic."""
    parser = argparse.ArgumentParser(
        description=(
            "Replay one raw Ollama request with configurable concurrency and "
            "flag suspicious assistant.content prefixes that look like a mid-string overlap "
            "with the last user message."
        )
    )
    parser.add_argument("--raw-log", default="logs/openaix_call_raw_log.jsonl", help="Path to raw request/response log")
    parser.add_argument("--request-line", type=int, required=True, help="1-based raw-log line containing the request body to replay")
    parser.add_argument("--provider", default="ollama_remote", help="Provider id used to resolve baseUrl from config")
    parser.add_argument("--url", default="", help="Explicit Ollama /api/chat URL override")
    parser.add_argument("--count", type=int, default=20, help="Total number of replay requests")
    parser.add_argument("--concurrency", type=int, default=4, help="Maximum concurrent requests")
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-request timeout seconds")
    parser.add_argument("--prefix-window", type=int, default=160, help="Assistant content prefix length checked for overlap")
    parser.add_argument("--min-overlap", type=int, default=24, help="Minimum overlap length required to flag suspicious content")
    parser.add_argument("--out", default="logs/ollama_interference_results.jsonl", help="JSONL file for replay results")
    parser.add_argument("--ps-snapshot", action="store_true", help="Capture /api/ps before and after the run")
    parser.add_argument("--prepare-only", action="store_true", help="Parse the request and print derived settings without sending requests")
    return parser.parse_args()


def load_request(raw_log_path: Path, request_line: int) -> dict[str, Any]:
    """Load one request JSON object from the raw log by 1-based line number."""
    lines = raw_log_path.read_text(encoding="utf-8").splitlines()
    if request_line < 1 or request_line > len(lines):
        raise ValueError(f"request line {request_line} out of range (1..{len(lines)})")

    request = json.loads(lines[request_line - 1])
    if not isinstance(request, dict):
        raise ValueError("raw-log line does not contain a JSON object")
    return request


def resolve_url(config_path: Path, provider_id: str, explicit_url: str) -> str:
    """Resolve replay target URL from CLI override or provider baseUrl."""
    if explicit_url.strip():
        return explicit_url.strip()

    cfg = Config(config_path)
    base_url = str(cfg.get(f"models.providers.{provider_id}.baseUrl") or "").rstrip("/")
    if not base_url:
        raise ValueError(f"provider {provider_id!r} has no baseUrl")
    return f"{base_url}/api/chat"


def extract_last_user_content(request_payload: dict[str, Any]) -> str:
    """Return the last user message content from the replay payload."""
    messages = request_payload.get("messages")
    if not isinstance(messages, list):
        return ""

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        return content if isinstance(content, str) else str(content or "")
    return ""


def find_prefix_overlap(source_text: str, target_prefix: str, *, min_overlap: int) -> tuple[bool, int, int]:
    """Detect whether target prefix matches a non-zero-offset suffix of source text."""
    if not source_text or not target_prefix:
        return False, -1, 0

    best_offset = -1
    best_length = 0
    max_check = min(len(source_text), len(target_prefix))
    for overlap_len in range(max_check, min_overlap - 1, -1):
        suffix = source_text[-overlap_len:]
        if not target_prefix.startswith(suffix):
            continue
        offset = len(source_text) - overlap_len
        if offset <= 0:
            continue
        best_offset = offset
        best_length = overlap_len
        break
    return best_offset >= 0, best_offset, best_length


def find_suspicious_prefix_markers(content: str) -> list[str]:
    """Return generic malformed-prefix markers seen in reproduced corrupted responses."""
    if not content:
        return []

    reasons: list[str] = []
    prefix = content[:64]
    if prefix.startswith("ness_id:"):
        reasons.append("starts_mid_message_id")
    if prefix.startswith("thought\nThe user"):
        reasons.append("starts_mid_thinking_token")
    if prefix.startswith("ness_id: 2thought"):
        reasons.append("starts_mid_message_id_and_mid_thought")
    return reasons


async def capture_ps_snapshot(client: httpx.AsyncClient, api_chat_url: str) -> dict[str, Any]:
    """Fetch /api/ps from the same Ollama base URL when requested."""
    base_url = api_chat_url.rsplit("/api/chat", 1)[0]
    try:
        response = await client.get(f"{base_url}/api/ps")
        return {
            "status_code": response.status_code,
            "body": response.text,
        }
    except Exception as exc:
        return {
            "status_code": 0,
            "error": str(exc),
        }


async def run_attempt(
    index: int,
    *,
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    last_user_content: str,
    prefix_window: int,
    min_overlap: int,
) -> AttemptResult:
    """Send one replay request and derive corruption heuristics from the response."""
    started = time.perf_counter()
    try:
        response = await client.post(url, json={**payload, "stream": False})
        response_text = response.text
        status_code = response.status_code
    except Exception as exc:
        return AttemptResult(
            index=index,
            status_code=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            response_text="",
            response_json=None,
            invalid_json=False,
            suspicious_overlap=False,
            overlap_offset=-1,
            overlap_length=0,
            error=str(exc),
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    try:
        response_json = response.json()
        invalid_json = False
    except Exception:
        response_json = None
        invalid_json = True

    assistant_content = ""
    if isinstance(response_json, dict):
        message = response_json.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            assistant_content = content if isinstance(content, str) else str(content or "")

    suspicious_overlap, overlap_offset, overlap_length = find_prefix_overlap(
        last_user_content,
        assistant_content[:prefix_window],
        min_overlap=min_overlap,
    )
    suspicion_reasons = find_suspicious_prefix_markers(assistant_content)
    suspicious_prefix = bool(suspicion_reasons)
    return AttemptResult(
        index=index,
        status_code=status_code,
        duration_ms=duration_ms,
        response_text=response_text,
        response_json=response_json if isinstance(response_json, dict) else None,
        invalid_json=invalid_json,
        suspicious_overlap=suspicious_overlap,
        overlap_offset=overlap_offset,
        overlap_length=overlap_length,
        suspicious_prefix=suspicious_prefix,
        suspicion_reasons=suspicion_reasons,
    )


async def run_replay(args: argparse.Namespace) -> int:
    """Run the configured replay workload and persist JSONL results."""
    raw_log_path = Path(args.raw_log)
    request_payload = load_request(raw_log_path, args.request_line)
    url = resolve_url(Path("config.json5"), args.provider, args.url)
    last_user_content = extract_last_user_content(request_payload)

    print(f"target_url={url}")
    print(f"request_line={args.request_line}")
    print(f"request_model={request_payload.get('model')}")
    print(f"request_messages={len(request_payload.get('messages') or [])}")
    print(f"last_user_len={len(last_user_content)}")

    if args.prepare_only:
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    timeout = httpx.Timeout(args.timeout)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if args.ps_snapshot:
            before = await capture_ps_snapshot(client, url)
            print("ps_before=", json.dumps(before, ensure_ascii=False))

        semaphore = asyncio.Semaphore(max(1, args.concurrency))

        async def guarded_attempt(index: int) -> AttemptResult:
            async with semaphore:
                return await run_attempt(
                    index,
                    client=client,
                    url=url,
                    payload=request_payload,
                    last_user_content=last_user_content,
                    prefix_window=max(1, args.prefix_window),
                    min_overlap=max(1, args.min_overlap),
                )

        results = await asyncio.gather(*(guarded_attempt(index) for index in range(1, args.count + 1)))

        if args.ps_snapshot:
            after = await capture_ps_snapshot(client, url)
            print("ps_after=", json.dumps(after, ensure_ascii=False))

    suspicious_count = 0
    invalid_json_count = 0
    error_count = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for result in results:
            if result.suspicious_overlap or result.suspicious_prefix:
                suspicious_count += 1
            if result.invalid_json:
                invalid_json_count += 1
            if result.error:
                error_count += 1
            record = {
                "index": result.index,
                "status_code": result.status_code,
                "duration_ms": result.duration_ms,
                "invalid_json": result.invalid_json,
                "suspicious_overlap": result.suspicious_overlap,
                "suspicious_prefix": result.suspicious_prefix,
                "overlap_offset": result.overlap_offset,
                "overlap_length": result.overlap_length,
                "suspicion_reasons": result.suspicion_reasons,
                "error": result.error,
                "response": result.response_json,
                "response_text": result.response_text if result.response_json is None else "",
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"results_file={out_path}")
    print(f"requests_total={len(results)}")
    print(f"errors={error_count}")
    print(f"invalid_json={invalid_json_count}")
    print(f"suspicious_overlap={suspicious_count}")
    if suspicious_count:
        flagged = [
            result.index
            for result in results
            if result.suspicious_overlap or result.suspicious_prefix
        ]
        print(f"flagged_indices={flagged}")
        return 2
    return 0


def main() -> int:
    """Entrypoint for the replay diagnostic."""
    args = parse_args()
    return asyncio.run(run_replay(args))


if __name__ == "__main__":
    raise SystemExit(main())