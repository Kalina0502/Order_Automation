"""Pydantic models for the LLM JSON output and a parser/validator."""
from typing import List, Optional
from pydantic import BaseModel, Field, validator


class Item(BaseModel):
    product_code: str
    qty: int = Field(..., ge=0)


class Order(BaseModel):
    """Single order with VIP number and items."""
    vip_number: str = ""
    items: List[Item] = []
    order_status: str = "order"  # "order" or "needs_manual"
    reasons: List[str] = []
    questions_for_human: List[str] = []

    @validator("order_status")
    def order_status_must_be_known(cls, v):
        if v not in {"order", "needs_manual"}:
            raise ValueError("order_status must be: order or needs_manual")
        return v


class ModelOutput(BaseModel):
    """Model output with support for multiple orders."""
    classification: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    orders: List[Order] = []

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

    return ModelOutput.model_validate(obj)
