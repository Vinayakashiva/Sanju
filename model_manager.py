"""
model_manager.py — Sanju AI
Central place for every Ollama call in the app. Two models are in play —
OLLAMA_CHAT_MODEL (Qwen3 4B, conversation) and OLLAMA_CODE_MODEL (Qwen3 8B,
coding only) — and this module makes sure only ONE of them is ever resident
in memory at a time.

Ollama keeps a model loaded ("warm") for a while after each call so the
next call is fast. That's great when you're only using one model, but with
two different models in rotation it means both can end up loaded
simultaneously, each eating a few GB of RAM/VRAM for no benefit. Before
every call here, we check which model was used last — if it's about to
switch, the previous one is explicitly unloaded first.
"""

import ollama
from config import OLLAMA_CHAT_MODEL, OLLAMA_CODE_MODEL

_state = {"loaded_model": None}


def _unload(model: str):
    """Tells Ollama to drop a model from memory immediately."""
    if not model:
        return
    try:
        # keep_alive=0 unloads right after this (empty) call completes —
        # the standard trick for forcing an immediate unload via the
        # Python client, which doesn't expose a separate "unload" method.
        ollama.generate(model=model, prompt="", keep_alive=0)
        print(f"[ModelManager] Unloaded {model}")
    except Exception as e:
        print(f"[ModelManager] Unload error for {model}: {e}")


def _ensure_only_this_loaded(model: str):
    """Unloads whichever OTHER model was last used, if any, before switching."""
    last = _state["loaded_model"]
    if last and last != model:
        _unload(last)
    _state["loaded_model"] = model


def chat(model: str, messages: list, think: bool = False, **kwargs) -> dict:
    """
    Drop-in replacement for ollama.chat() — same signature, same return
    shape — but routes through the single-model-loaded rule above.
    Every file that talks to Ollama should call this instead of
    ollama.chat() directly.
    """
    _ensure_only_this_loaded(model)
    return ollama.chat(model=model, messages=messages, think=think, **kwargs)


def chat_stream(model: str, messages: list, think: bool = False, **kwargs):
    """
    Streaming variant — returns Ollama's generator of partial response
    chunks (stream=True) instead of waiting for the full reply. Lets the
    caller start speaking the first sentence while the model is still
    generating the rest, instead of waiting for the whole response before
    anything happens.
    """
    _ensure_only_this_loaded(model)
    return ollama.chat(model=model, messages=messages, think=think, stream=True, **kwargs)


# Convenience constants so callers don't need to import config separately
CHAT_MODEL = OLLAMA_CHAT_MODEL
CODE_MODEL = OLLAMA_CODE_MODEL