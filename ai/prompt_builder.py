"""Prompt builder: loads prompt templates and builds chat messages."""
from typing import Dict, List
import os
import json

PROMPTS_DIR = os.getenv("PROMPTS_DIR", "prompts")


def _load_prompt(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt template not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_messages(email_payload: Dict) -> List[Dict]:
    """Return chat messages (system + user) given the email payload.

    The system prompt lives in `prompts/` and contains strict instructions
    about the required JSON-only output schema.
    """
    system = _load_prompt("order_classification.md")
    user = json.dumps(email_payload, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "Here is the email payload as JSON:\n" + user},
    ]
    return messages
