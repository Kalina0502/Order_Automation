"""AI integration package.

This package provides reusable AI utilities for all LLM-powered steps:
- core: High-level functions for LLM pipelines (load prompts, call APIs, parse responses)
- llm_client: Low-level OpenAI-compatible API client
- prompt_builder: Legacy prompt builder (use core.build_chat_messages instead)
- schemas: Pydantic models for validation
"""
from .llm_client import chat_completion, LLMError
from .prompt_builder import build_messages
from .schemas import parse_and_validate, ModelOutput, Order, Item
from .core import (
    load_prompt_template,
    build_chat_messages,
    save_json_atomic,
    call_llm_and_save,
    extract_assistant_content,
    parse_and_save_response,
)

__all__ = [
    # Low-level API client
    "chat_completion",
    "LLMError",
    # Legacy prompt builder
    "build_messages",
    # Schemas
    "parse_and_validate",
    "ModelOutput",
    "Order",
    "Item",
    # Core utilities (recommended for new code)
    "load_prompt_template",
    "build_chat_messages",
    "save_json_atomic",
    "call_llm_and_save",
    "extract_assistant_content",
    "parse_and_save_response",
]
