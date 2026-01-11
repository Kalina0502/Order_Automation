"""Step 2: AI Order Validation

Reads email data from step1 output and classifies orders using LLM.

Input: out_step1_email_inputs/TIMESTAMP/*.json
Output: out_step2_order_classification/TIMESTAMP/{order,not_order,needs_manual}/*.json

This script can be run independently after step1 to process emails in batch.
"""
import os
import sys
import json
import logging
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# Import AI utilities
from ai.core import (
    load_prompt_template,
    build_chat_messages,
    call_llm_and_save,
    extract_assistant_content,
    parse_and_save_response,
    save_json_atomic
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# -----------------------------
# CONFIG
# -----------------------------
STEP1_DIR = os.getenv("STEP1_DIR", "out_step1_email_inputs")
STEP2_DIR = os.getenv("STEP2_DIR", "out_step2_ai_order_validation")
PROMPTS_DIR = os.getenv("PROMPTS_DIR", "prompts")

# LLM settings
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
TEMPERATURE = 0.1
TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))

# Classification thresholds for auto-marking emails as read
AUTO_MARK_READ_MIN_CONFIDENCE = 0.85


def create_minimal_payload_for_model(full_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a minimal payload for the model with only essential information:
    - Email body text
    - Extracted orders (VIP + products)

    Removes all metadata, headers, raw Excel rows, etc.
    """
    return {
        "body_text": full_payload.get("body_text", ""),
        "extracted_orders": full_payload.get("extracted_orders", [])
    }


def classify_email(
    email_data: Dict[str, Any],
    base_filename: str,
    output_dirs: Dict[str, str],
    email_id: str,
) -> Optional[Any]:
    """Classify a single email using LLM.

    Args:
        email_data: Full email payload from step1
        base_filename: Base filename (without extension) for outputs
        output_dirs: Dict with keys: raw, order, not_order, needs_manual

    Returns:
        Parsed classification result (Pydantic model), or None if failed
    """
    try:
        # 1) Load prompt and create minimal payload
        system_prompt = load_prompt_template("order_classification.md", PROMPTS_DIR)
        minimal_payload = create_minimal_payload_for_model(email_data)

        # 2) Build chat messages
        messages = build_chat_messages(system_prompt, minimal_payload)

        # 3) Call LLM and save request/response to raw/
        logging.info("Calling LLM model=%s for %s", MODEL_NAME, base_filename)
        api_response = call_llm_and_save(
            messages=messages,
            output_dir=output_dirs["raw"],
            base_filename=base_filename,
            model=MODEL_NAME,
            temperature=TEMPERATURE,
            timeout=TIMEOUT,
        )

        # 4) Extract assistant text
        assistant_text = extract_assistant_content(api_response)

        # 5) Parse and save to raw/
        parsed = parse_and_save_response(
            assistant_text=assistant_text,
            output_dir=output_dirs["raw"],
            base_filename=base_filename,
        )

        if parsed is None:
            logging.error("Failed to parse response for %s", base_filename)
            return None

        # 6) Add email_id to each order in the parsed result
        parsed_dict = parsed.model_dump()

        # Add email_id to top-level
        parsed_dict["email_id"] = email_id

        # Add email_id to each order
        if "orders" in parsed_dict:
            for order in parsed_dict["orders"]:
                order["email_id"] = email_id

        # 7) Save to classification-specific folder
        classification = parsed.classification
        if classification == "order":
            target_dir = output_dirs["order"]
        elif classification == "not_order":
            target_dir = output_dirs["not_order"]
        else:  # needs_manual
            target_dir = output_dirs["needs_manual"]

        classified_path = os.path.join(target_dir, f"{base_filename}.json")
        save_json_atomic(parsed_dict, classified_path)
        logging.info("Classification: %s (confidence: %.2f) → %s",
                     classification, parsed.confidence, classified_path)

        return parsed

    except Exception as e:
        logging.exception("Classification failed for %s: %s", base_filename, e)
        # Save error file
        err_path = os.path.join(output_dirs["raw"], f"{base_filename}_error.txt")
        with open(err_path, "w", encoding="utf-8") as ef:
            ef.write(f"Classification error:\n{repr(e)}")
        return None


def should_mark_as_read(parsed_result: Any) -> bool:
    """Determine if email should be marked as read based on classification.

    Args:
        parsed_result: Parsed Pydantic model from LLM

    Returns:
        True if email should be marked as read
    """
    if parsed_result is None:
        return False

    try:
        classification = parsed_result.classification
        confidence = float(parsed_result.confidence or 0.0)

        # Only mark as read if:
        # - Classification is NOT needs_manual
        # - Confidence is high enough
        return classification != "needs_manual" and confidence >= AUTO_MARK_READ_MIN_CONFIDENCE

    except Exception:
        return False


def process_batch(batch_dir: str) -> Dict[str, int]:
    """Process all emails in a step1 batch directory.

    Args:
        batch_dir: Path to step1 batch directory (e.g., out_step1_email_inputs/20260110_130000)

    Returns:
        Dict with classification counts
    """
    if not os.path.exists(batch_dir):
        logging.error("Batch directory does not exist: %s", batch_dir)
        return {}

    # Extract batch timestamp from directory name
    batch_timestamp = os.path.basename(batch_dir)

    # Create step2 output directories
    step2_batch_dir = os.path.join(STEP2_DIR, batch_timestamp)
    output_dirs = {
        "raw": os.path.join(step2_batch_dir, "raw"),
        "order": os.path.join(step2_batch_dir, "order"),
        "not_order": os.path.join(step2_batch_dir, "not_order"),
        "needs_manual": os.path.join(step2_batch_dir, "needs_manual"),
    }

    for dir_path in output_dirs.values():
        os.makedirs(dir_path, exist_ok=True)

    # Find all JSON files in batch directory
    email_files = [f for f in os.listdir(batch_dir) if f.endswith(".json")]
    if not email_files:
        logging.warning("No email files found in %s", batch_dir)
        return {}

    logging.info("Processing %d emails from %s", len(email_files), batch_dir)

    # Track results
    stats = {
        "order": 0,
        "not_order": 0,
        "needs_manual": 0,
        "failed": 0,
        "should_mark_read": 0,
    }

    for email_file in email_files:
        email_path = os.path.join(batch_dir, email_file)
        base_filename = os.path.splitext(email_file)[0]

        logging.info("Processing: %s", email_file)

        try:
            # Load email data
            with open(email_path, "r", encoding="utf-8") as f:
                email_data = json.load(f)

            # Extract email_id (use message_id from Gmail)
            email_id = email_data.get("message_id", base_filename)

            # Classify
            parsed = classify_email(email_data, base_filename, output_dirs, email_id)

            if parsed:
                stats[parsed.classification] += 1

                if should_mark_as_read(parsed):
                    stats["should_mark_read"] += 1
                    logging.info("  → Should mark as read (classification=%s, confidence=%.2f)",
                                 parsed.classification, parsed.confidence)
            else:
                stats["failed"] += 1

        except Exception as e:
            logging.exception("Failed to process %s: %s", email_file, e)
            stats["failed"] += 1

    return stats


def main():
    """Main entry point - process latest batch or specified batch."""
    import argparse

    parser = argparse.ArgumentParser(description="Step 2: AI Order Validation")
    parser.add_argument(
        "--batch",
        help="Specific batch timestamp to process (e.g., 20260110_130000). If not specified, processes the latest batch.",
    )
    args = parser.parse_args()

    # Find batch to process
    if args.batch:
        batch_dir = os.path.join(STEP1_DIR, args.batch)
        if not os.path.exists(batch_dir):
            logging.error("Batch directory not found: %s", batch_dir)
            sys.exit(1)
    else:
        # Find latest batch
        if not os.path.exists(STEP1_DIR):
            logging.error("Step1 directory not found: %s", STEP1_DIR)
            sys.exit(1)

        batches = [
            d for d in os.listdir(STEP1_DIR)
            if os.path.isdir(os.path.join(STEP1_DIR, d))
        ]
        if not batches:
            logging.error("No batches found in %s", STEP1_DIR)
            sys.exit(1)

        # Sort by timestamp (newest first)
        batches.sort(reverse=True)
        batch_dir = os.path.join(STEP1_DIR, batches[0])
        logging.info("Processing latest batch: %s", batches[0])

    # Process batch
    stats = process_batch(batch_dir)

    # Print summary
    logging.info("=" * 60)
    logging.info("CLASSIFICATION SUMMARY:")
    logging.info("  Order: %d", stats.get("order", 0))
    logging.info("  Not Order: %d", stats.get("not_order", 0))
    logging.info("  Needs Manual: %d", stats.get("needs_manual", 0))
    logging.info("  Failed: %d", stats.get("failed", 0))
    logging.info("  Should Mark Read: %d", stats.get("should_mark_read", 0))
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
