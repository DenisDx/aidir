"""Shared generation-option translations for OpenAI and Ollama payloads."""
from __future__ import annotations

# Top-level OpenAI/OpenAIx aliases mapped to canonical Ollama options. Ollama
# callers can also use these names as aidir extensions outside options.
GENERATION_OPTION_FIELDS = {
    "max_completion_tokens": "num_predict",
    "max_tokens": "num_predict",
    "num_predict": "num_predict",
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "typical_p": "typical_p",
    "seed": "seed",
    "repeat_penalty": "repeat_penalty",
    "repetition_penalty": "repeat_penalty",
    "repeat_last_n": "repeat_last_n",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "penalize_newline": "penalize_newline",
    "mirostat": "mirostat",
    "mirostat_tau": "mirostat_tau",
    "mirostat_eta": "mirostat_eta",
    "num_keep": "num_keep",
    "num_ctx": "num_ctx",
    "num_batch": "num_batch",
    "stop": "stop",
}

# Canonical Ollama options understood by OpenAI-compatible llama.cpp servers.
OLLAMA_TO_OPENAI_OPTION_FIELDS = {
    "num_predict": "max_completion_tokens",
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "typical_p": "typical_p",
    "seed": "seed",
    "repeat_penalty": "repetition_penalty",
    "repeat_last_n": "repeat_last_n",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "penalize_newline": "penalize_newline",
    "mirostat": "mirostat",
    "mirostat_tau": "mirostat_tau",
    "mirostat_eta": "mirostat_eta",
    "stop": "stop",
}
