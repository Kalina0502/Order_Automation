"""Simple OpenAI-compatible Chat Completions client.

Uses `OPENAI_API_KEY` from environment and `OPENAI_API_BASE` optionally.
"""
from typing import Any, Dict, List, Optional
import os
import requests

class LLMError(RuntimeError):
    pass


def chat_completion(messages: List[Dict[str, Any]], model: Optional[str] = None, temperature: float = 0.0, timeout: int = 60) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set in environment")

    base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    if not model:
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    url = f"{base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.Timeout as e:
        raise LLMError(f"Request timed out: {e}") from e
    except Exception as e:
        raise LLMError(f"Request failed: {e}") from e

    try:
        data = resp.json()
    except Exception:
        raise LLMError(f"Non-JSON response: {resp.status_code} {resp.text}")

    if not resp.ok:
        # Bubble up error details when possible
        err = data if isinstance(data, dict) else {"status": resp.status_code, "text": resp.text}
        raise LLMError(f"LLM API error: {err}")

    return data
