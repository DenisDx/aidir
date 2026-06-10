CHARS_PER_TOKEN = 4
# Run: ./venv/bin/python test_context_overflow.py MODEL_NAME CONTEXT_TOKENS
# Example: ./venv/bin/python test_context_overflow.py smart_glm-4.7 32000
# The script checks /v1/models/{model}/queue-state and sends one /api/chat request only when can_run_now=true.

import argparse
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from core.config import Config


OVERSHOOT_RATIO = 1.08
DEFAULT_PRIORITY = 5
HTTP_TIMEOUT_SECONDS = 900.0
LONGEST_WORD = "pneumonoultramicroscopicsilicovolcanoconiosis"
WORD_POOL = [
    "amber", "anchor", "anvil", "apricot", "atlas", "aurora", "autumn", "badger", "bamboo", "barley",
    "beacon", "birch", "blossom", "boron", "breeze", "bronze", "cactus", "cannon", "caper", "caramel",
    "cedar", "ceramic", "charcoal", "citadel", "cobalt", "comet", "copper", "coral", "cosmos", "crater",
    "crimson", "cumin", "current", "cypress", "dahlia", "dawn", "delta", "desert", "diamond", "drizzle",
    "ember", "emerald", "falcon", "fathom", "fennel", "ferret", "fjord", "flint", "forest", "fossil",
    "galaxy", "garnet", "glacier", "granite", "harbor", "hazel", "helium", "horizon", "hyssop", "indigo",
    "iris", "ivory", "jasper", "juniper", "kestrel", "lagoon", "lantern", "laurel", "lavender", "ledger",
    "lemon", "lichen", "lilac", "linen", "lotus", "magnet", "maple", "marble", "meadow", "mercury",
    "meteor", "mint", "mirage", "monsoon", "mosaic", "mulberry", "nectar", "nickel", "northwind", "nutmeg",
    "oak", "obsidian", "ocean", "olive", "onyx", "opal", "orbit", "orchid", "origin", "otter",
    "paprika", "parchment", "pebble", "pepper", "peridot", "petal", "phoenix", "pioneer", "planet", "plasma",
    "plum", "pollen", "prairie", "prism", "quartz", "quill", "radar", "rainfall", "raptor", "reef",
    "resin", "ripple", "river", "robin", "saffron", "sailor", "sandstone", "satin", "savanna", "scarlet",
    "shadow", "signal", "silica", "silver", "skyline", "solstice", "sparrow", "spectrum", "spruce", "stalwart",
    "starling", "storm", "sunrise", "tangent", "teal", "temple", "thistle", "timber", "topaz", "torrent",
    "trident", "tundra", "turmeric", "ultraviolet", "umbra", "valley", "velvet", "verdant", "vertex", "violet",
    "walnut", "waterfall", "whisper", "willow", "winter", "wolfram", "xenon", "yarrow", "zephyr", "zircon",
]


def parse_args() -> argparse.Namespace:
    """Parse positional CLI arguments for model id and target overflow size."""
    parser = argparse.ArgumentParser(
        description=(
            "Send one oversized /api/chat request to the selected model after checking "
            "that the model can run immediately via queue-state."
        )
    )
    parser.add_argument("model", help="Model id or alias, including smart aliases such as smart_glm-4.7")
    parser.add_argument("context_tokens", type=int, help="Approximate context size in tokens used only to build the oversized prompt")
    return parser.parse_args()


def print_run_instructions(script_name: str) -> None:
    """Print the exact command-line shape for this script."""
    print(f"Run: ./venv/bin/python {script_name} MODEL_NAME CONTEXT_TOKENS")
    print(f"Example: ./venv/bin/python {script_name} smart_glm-4.7 32000")
    print(f"Approximate conversion: 1 token ~= {CHARS_PER_TOKEN} chars; overshoot={OVERSHOOT_RATIO:.2f}x")
    print()


def load_runtime_config() -> Config:
    """Load config.json5 from the repository root."""
    return Config(Path(__file__).with_name("config.json5"))


def resolve_openaix_base_url(config: Config) -> str:
    """Resolve local OpenAIx endpoint base URL from config.json5."""
    raw_config = config.raw()
    endpoints = raw_config.get("endpoints") if isinstance(raw_config, dict) else None
    if not isinstance(endpoints, list):
        raise ValueError("config.json5 does not contain an endpoints list")

    for endpoint_cfg in endpoints:
        if not isinstance(endpoint_cfg, dict):
            continue
        if str(endpoint_cfg.get("api") or "").strip() != "openaix":
            continue

        host = str(endpoint_cfg.get("bindAddress") or "127.0.0.1").strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        port = int(endpoint_cfg.get("port") or 21434)
        return f"http://{host}:{port}"

    raise ValueError("OpenAIx endpoint config not found in config.json5")


def resolve_auth_headers(config: Config) -> dict[str, str]:
    """Use the first configured API token when available; otherwise send no auth."""
    raw_config = config.raw()
    users_cfg = raw_config.get("users") if isinstance(raw_config, dict) else None
    items = users_cfg.get("items") if isinstance(users_cfg, dict) else None
    if not isinstance(items, list):
        return {}

    for item in items:
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "").strip()
        if token:
            return {"Authorization": f"Bearer {token}"}
    return {}


def build_overflow_text(context_tokens: int) -> str:
    """Build a large user text that slightly exceeds the requested context budget."""
    target_chars = max(len(LONGEST_WORD) * 2, int(context_tokens * CHARS_PER_TOKEN * OVERSHOOT_RATIO))
    randomizer = random.Random(context_tokens)
    parts: list[str] = []
    current_chars = 0
    inserted_longest = False

    while current_chars < target_chars:
        if not inserted_longest and current_chars >= target_chars * 0.8:
            word = LONGEST_WORD
            inserted_longest = True
        else:
            word = randomizer.choice(WORD_POOL)
        parts.append(word)
        current_chars += len(word) + 1

    if not inserted_longest:
        parts.append(LONGEST_WORD)

    return " ".join(parts)


def build_payload(model_name: str, context_tokens: int) -> tuple[dict, int, int]:
    """Build one oversized Ollama-compatible /api/chat payload."""
    overflow_text = build_overflow_text(context_tokens)
    system_prompt = (
        "Find the longest word in the user's text. "
        "Reply with compact JSON containing longest_word and length only."
    )
    user_prompt = f"Text to inspect:\n{overflow_text}\nReturn only JSON."
    approx_chars = len(system_prompt) + len(user_prompt)
    approx_tokens = max(1, approx_chars // CHARS_PER_TOKEN)
    payload = {
        "model": model_name,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "num_predict": 32,
            "temperature": 0,
        },
    }
    return payload, approx_chars, approx_tokens


def fetch_queue_state(client: httpx.Client, base_url: str, model_name: str, headers: dict[str, str]) -> dict:
    """Return model-only queue-state, which also resolves smart aliases."""
    encoded_model = quote(model_name, safe="")
    response = client.get(
        f"{base_url}/v1/models/{encoded_model}/queue-state",
        params={"priority": DEFAULT_PRIORITY},
        headers=headers,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("queue-state response is not a JSON object")
    return data


def print_queue_state(queue_state: dict) -> None:
    """Print a compact queue-state summary for the selected model."""
    print(
        "Queue state: "
        f"provider={queue_state.get('provider')} "
        f"model={queue_state.get('model')} "
        f"can_run_now={queue_state.get('can_run_now')} "
        f"queued_total={queue_state.get('queued_count_total')} "
        f"queued_below_priority={queue_state.get('queued_count_below_priority')}"
    )
    priority_counts = queue_state.get("priority_counts")
    if isinstance(priority_counts, list) and priority_counts:
        print(f"Priority counts: {json.dumps(priority_counts, ensure_ascii=False)}")


def main() -> int:
    """Check queue-state and send one oversized chat request to probe context overflow."""
    args = parse_args()
    if args.context_tokens <= 0:
        print("CONTEXT_TOKENS must be a positive integer", file=sys.stderr)
        return 1

    print_run_instructions(Path(__file__).name)
    config = load_runtime_config()
    base_url = resolve_openaix_base_url(config)
    headers = resolve_auth_headers(config)
    payload, approx_chars, approx_tokens = build_payload(args.model, args.context_tokens)

    print(f"Endpoint: {base_url}")
    print(f"Model: {args.model}")
    print(f"Target overflow size (tokens): {args.context_tokens}")
    print(f"Approximate prompt chars: {approx_chars}")
    print(f"Approximate prompt tokens: {approx_tokens}")
    print(f"Auth header present: {'yes' if headers else 'no'}")
    print()

    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=10.0)
    with httpx.Client(timeout=timeout) as client:
        try:
            queue_state = fetch_queue_state(client, base_url, args.model, headers)
        except Exception as exc:
            print(f"Queue-state request failed: {exc}", file=sys.stderr)
            return 1

        print_queue_state(queue_state)
        if not bool(queue_state.get("can_run_now")):
            print("Model is busy or blocked by queue state; request was not sent.", file=sys.stderr)
            return 2

        print("Sending oversized /api/chat request...")
        started = time.perf_counter()
        try:
            response = client.post(f"{base_url}/api/chat", json=payload, headers=headers)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            print(f"Request failed after {elapsed:.2f}s: {exc}", file=sys.stderr)
            return 1

    elapsed = time.perf_counter() - started
    print(f"HTTP status: {response.status_code}")
    print(f"Elapsed seconds: {elapsed:.2f}")

    try:
        data = response.json()
    except Exception:
        preview = response.text[:2000]
        print("Response is not JSON.")
        print(preview)
        return 0 if response.is_success else 1

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())