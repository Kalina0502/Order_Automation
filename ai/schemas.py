"""Pydantic models for the LLM JSON output and a parser/validator."""
from typing import List, Optional
from pydantic import BaseModel, Field, validator


class Item(BaseModel):
    product_code: str
    qty: int = Field(..., ge=0)


class ModelOutput(BaseModel):
    classification: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    vip_number: Optional[str] = ""
    items: List[Item] = []
    reasons: List[str] = []
    questions_for_human: List[str] = []

    @validator("classification")
    def classification_must_be_known(cls, v):
        if v not in {"order", "not_order", "needs_manual"}:
            raise ValueError("classification must be one of: order, not_order, needs_manual")
        return v


def parse_and_validate(raw_text: str) -> ModelOutput:
    """Parse raw assistant text (expected to be exact JSON) and validate schema."""
    import json

    try:
        obj = json.loads(raw_text)
    except Exception as e:
        raise ValueError(f"Failed to parse JSON from model output: {e}") from e

    return ModelOutput.parse_obj(obj)
