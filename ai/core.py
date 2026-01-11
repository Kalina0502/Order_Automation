"""Core AI utilities for multi-step LLM pipelines.

This module provides reusable functions for:
- Loading prompt templates
- Building chat messages
- Calling LLM APIs
- Saving requests/responses with proper organization

Used across all AI-powered steps (step2, step3, etc.)
"""
from typing import Dict, List, Any, Optional
import os
import json
from .llm_client import chat_completion
from .schemas import parse_and_validate


def load_prompt_template(name: str, prompts_dir: Optional[str] = None) -> str:
    """Load a prompt template from the prompts directory.

    Args:
        name: Filename of the prompt template (e.g., "order_classification.md")
        prompts_dir: Optional custom prompts directory (defaults to PROMPTS_DIR env var)

    Returns:
        The prompt template content as string

    Raises:
        FileNotFoundError: If the prompt template doesn't exist
    """
    if prompts_dir is None:
        prompts_dir = os.getenv("PROMPTS_DIR", "prompts")

    path = os.path.join(prompts_dir, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt template not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_chat_messages(system_prompt: str, user_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build chat messages array for LLM API.

    Args:
        system_prompt: The system prompt (instructions for the model)
        user_payload: User data to send (will be JSON-serialized)

    Returns:
        List of message dicts with role and content
    """
    user_content = json.dumps(user_payload, ensure_ascii=False, indent=2)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def save_json_atomic(obj: Any, path: str):
    """Save JSON to file atomically (creates parent dirs if needed).

    Args:
        obj: Python object to serialize
        path: Destination file path
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def call_llm_and_save(
    messages: List[Dict[str, str]],
    output_dir: str,
    base_filename: str,
    model: Optional[str] = None,
    temperature: float = 0.0,
    timeout: int = 60,
) -> Dict[str, Any]:
    """Call LLM API and save request/response to output directory.

    Saves:
    - {base_filename}_request.json: The messages sent to the API
    - {base_filename}_raw.json: The full API response

    Args:
        messages: Chat messages to send
        output_dir: Directory where to save files
        base_filename: Base name for output files (without extension)
        model: Model name (defaults to OPENAI_MODEL env var)
        temperature: Sampling temperature (0.0 = deterministic)
        timeout: Request timeout in seconds

    Returns:
        The full API response dict

    Raises:
        LLMError: If the API call fails
    """
    os.makedirs(output_dir, exist_ok=True)

    # Save request
    req_path = os.path.join(output_dir, f"{base_filename}_request.json")
    save_json_atomic(messages, req_path)

    # Call LLM
    if model is None:
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    response = chat_completion(
        messages=messages,
        model=model,
        temperature=temperature,
        timeout=timeout
    )

    # Save raw response
    raw_path = os.path.join(output_dir, f"{base_filename}_raw.json")
    save_json_atomic(response, raw_path)

    return response


def extract_assistant_content(api_response: Dict[str, Any]) -> str:
    """Extract the assistant's text from API response.

    Args:
        api_response: Full API response dict

    Returns:
        The assistant's message content, or empty string if not found
    """
    try:
        choices = api_response.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return msg.get("content", "")
    except Exception:
        pass
    return ""


def parse_and_save_response(
    assistant_text: str,
    output_dir: str,
    base_filename: str,
) -> Optional[Any]:
    """Parse and validate LLM response, saving both parsed and error states.

    Saves:
    - {base_filename}_parsed.json: Successfully parsed Pydantic model
    - {base_filename}_parse_error.txt: Parse error + raw text (if parsing fails)

    Args:
        assistant_text: The raw assistant response text
        output_dir: Directory where to save files
        base_filename: Base name for output files (without extension)

    Returns:
        Parsed Pydantic model instance, or None if parsing failed
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        parsed = parse_and_validate(assistant_text)
        parsed_dict = parsed.model_dump()

        parsed_path = os.path.join(output_dir, f"{base_filename}_parsed.json")
        save_json_atomic(parsed_dict, parsed_path)

        return parsed

    except Exception as e:
        # Save error for debugging
        err_path = os.path.join(output_dir, f"{base_filename}_parse_error.txt")
        with open(err_path, "w", encoding="utf-8") as ef:
            ef.write("--- ASSISTANT TEXT ---\n")
            ef.write(assistant_text or "")
            ef.write("\n\n--- PARSE ERROR ---\n")
            ef.write(repr(e))

        return None
