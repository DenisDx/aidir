"""Retry one empty OpenAI-compatible llama.cpp response after a restart."""

from core import log

ENABLED = True

_hooks = None
_ATTEMPT_KEY = "llama_cpp_empty_response_recovery_attempted"


async def on_llm_response_complete(task, response):
    """Restart and retry one invalid llama.cpp response."""
    if not isinstance(response, dict) or not _is_openai_empty_response(response):
        return
    provider_id = task.route_provider_id
    if not provider_id or not _hooks.is_owned_local_llama_cpp(provider_id):
        return
    if task.hook_metadata.get(_ATTEMPT_KEY):
        return

    task.hook_metadata[_ATTEMPT_KEY] = True
    await _hooks.restart_local_llama_cpp(provider_id)
    log(
        "system",
        "info",
        f"llama_cpp_empty_response_recovery restarted {provider_id} for task {task.id}",
    )
    await _hooks.retry_task(task, provider_id=provider_id)


def register(hooks):
    """Register the recovery handler when enabled."""
    global _hooks
    if not ENABLED:
        return
    _hooks = hooks
    hooks.on("llm_response_complete", on_llm_response_complete)


def _is_openai_empty_response(response):
    """Return whether OpenAI content is empty and reasoning is slash-only."""
    message = response.get("message")
    if isinstance(message, dict):
        messages = [message]
    else:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        messages = [choice.get("message") for choice in choices if isinstance(choice, dict)]
        if len(messages) != len(choices) or not all(isinstance(item, dict) for item in messages):
            return False

    content = [str(message.get("content") or "") for message in messages]
    reasoning = [
        str(message.get(key) or "")
        for message in messages
        for key in ("reasoning", "reasoning_content", "thinking")
    ]

    return not any(value.strip() for value in content) and all(
        not value.strip() or set(value.strip()) == {"/"} for value in reasoning
    )
