#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Install it with:")
    print("  pip install pandas openpyxl xlrd")
    sys.exit(1)


# ----------------------------
# Utils
# ----------------------------

def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _normalize_digits(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s == "":
        return None
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    digits = re.sub(r"\D+", "", s)
    return digits if digits else None


def _safe_slug(s: str) -> str:
    s = str(s).strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-zA-Z0-9_\-]+", "", s)
    return s[:80] if s else "order"


# ----------------------------
# Reference extraction
# ----------------------------

def extract_vip_reference(vip_xlsx: Path) -> pd.DataFrame:
    df = pd.read_excel(vip_xlsx)
    vip_col_candidates = [c for c in df.columns if re.search(r"\bVIP\b", str(c), re.IGNORECASE)]
    if not vip_col_candidates:
        raise ValueError("VIP.xlsx: не намирам колона съдържаща 'VIP' (напр. 'VIP №').")

    vip_col = vip_col_candidates[0]
    out = df[[vip_col]].copy()
    out.columns = ["vip_number_raw"]
    out["vip_number"] = out["vip_number_raw"].apply(_normalize_digits)
    out = out.dropna(subset=["vip_number"]).drop_duplicates(subset=["vip_number"]).reset_index(drop=True)
    return out[["vip_number"]]


def _read_product_xls_or_xlsx(path: Path, sheet_name: str) -> pd.DataFrame:
    """
    Reads xlsx normally.
    Reads xls via xlrd (requires: pip install xlrd==2.0.1).
    """
    if path.suffix.lower() == ".xls":
        try:
            return pd.read_excel(path, sheet_name=sheet_name, engine="xlrd")
        except ImportError:
            raise RuntimeError(
                "product_codes.xls: липсва xlrd. Инсталирай:\n"
                "pip install xlrd==2.0.1\n"
                "или конвертирай файла до .xlsx и го подай пак."
            )
    return pd.read_excel(path, sheet_name=sheet_name)


def extract_product_codes_reference(product_file: Path) -> pd.DataFrame:
    # твоят файл: sheet 'service', колона 'Код'
    try:
        service_df = _read_product_xls_or_xlsx(product_file, sheet_name="service")
    except Exception:
        # fallback: първи sheet
        if product_file.suffix.lower() == ".xls":
            service_df = pd.read_excel(product_file, engine="xlrd")
        else:
            service_df = pd.read_excel(product_file)

    code_col = None
    for c in service_df.columns:
        if str(c).strip().lower() in {"код", "code", "product_code", "sku"}:
            code_col = c
            break
    if code_col is None:
        for c in service_df.columns:
            if "код" in str(c).strip().lower():
                code_col = c
                break
    if code_col is None:
        raise ValueError("product_codes: не намирам колона за код (очаквам 'Код' в sheet 'service').")

    out = service_df[[code_col]].copy()
    out.columns = ["product_code_raw"]
    out["product_code"] = out["product_code_raw"].apply(_normalize_digits)
    out = out.dropna(subset=["product_code"]).drop_duplicates(subset=["product_code"]).reset_index(drop=True)
    return out[["product_code"]]


# ----------------------------
# Step2 load + normalize
# ----------------------------

VIP_KEYS = ["vip", "vip_no", "vip_number", "vip№", "vip_num", "vip_id", "vipNo", "vip_№"]
PRODUCT_KEYS = ["product_code", "product_codes", "code", "codes", "sku", "item_code", "products", "product"]


def _find_key(d: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    lower_map = {str(k).strip().lower(): k for k in d.keys()}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    for lk, orig in lower_map.items():
        for cand in candidates:
            if cand.lower() in lk:
                return orig
    return None


def _load_json_file(p: Path) -> List[Dict[str, Any]]:
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # common wrappers
        for k in ["order", "orders", "data", "items", "rows"]:
            if k in data and isinstance(data[k], list):
                return data[k]
            if k in data and isinstance(data[k], dict):
                return [data[k]]
        return [data]
    return []


def load_orders(step2_input: Path) -> List[Dict[str, Any]]:
    """
    Accepts:
    - a directory containing *.json orders (your case)
    - a single .json file (list/dict)
    - a single .csv file
    """
    if step2_input.is_dir():
        json_files = sorted(step2_input.glob("*.json"))
        orders: List[Dict[str, Any]] = []
        for jf in json_files:
            orders.extend(_load_json_file(jf))
        return orders

    if step2_input.suffix.lower() == ".json":
        return _load_json_file(step2_input)

    if step2_input.suffix.lower() == ".csv":
        df = pd.read_csv(step2_input)
        return df.to_dict(orient="records")

    raise ValueError("Step2 input трябва да е папка, .json или .csv")


def normalize_order(order: Dict[str, Any], fallback_index: int) -> Dict[str, Any]:
    vip_key = _find_key(order, VIP_KEYS)
    prod_key = _find_key(order, PRODUCT_KEYS)

    vip_val = order.get(vip_key) if vip_key else None
    vip_number = _normalize_digits(vip_val)

    # Extract email_id if present
    email_id = order.get("email_id", None)

    product_codes: List[str] = []

    # 1) Try top-level product field(s)
    prod_val = order.get(prod_key) if prod_key else None

    def _add_code(x: Any) -> None:
        n = _normalize_digits(x)
        if n:
            product_codes.append(n)

    if isinstance(prod_val, list):
        # list can be list[str] OR list[dict]
        for x in prod_val:
            if isinstance(x, dict):
                k = _find_key(x, ["product_code", "code", "sku", "item_code"])
                if k:
                    _add_code(x.get(k))
            else:
                _add_code(x)

    elif isinstance(prod_val, str):
        for part in re.split(r"[,\n;\|]+", prod_val):
            _add_code(part)

    elif prod_val is not None:
        _add_code(prod_val)

    # 2) If still empty, try nested items (your Step2 structure)
    if not product_codes:
        items = order.get("items")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    k = _find_key(it, ["product_code", "code", "sku", "item_code"])
                    if k:
                        _add_code(it.get(k))

    # de-dup while preserving order
    seen = set()
    product_codes = [c for c in product_codes if not (c in seen or seen.add(c))]

    id_key = _find_key(order, ["order_id", "id", "orderId", "номер", "number"])
    order_id = order.get(id_key) if id_key else None
    if order_id is None or str(order_id).strip() == "":
        order_id = f"order_{fallback_index+1:05d}"

    result = {
        "order_id": str(order_id),
        "vip_number": vip_number,
        "product_codes": product_codes,
        "_raw": order,
    }

    # Add email_id if present
    if email_id:
        result["email_id"] = email_id

    return result
    

# ----------------------------
# Validation
# ----------------------------

@dataclass
class ValidationResult:
    ok: bool
    reason: str
    vip_ok: bool
    missing_vip: Optional[str]
    missing_product_codes: List[str]


def validate_one(order_norm: Dict[str, Any], vip_set: set, prod_set: set) -> ValidationResult:
    vip = order_norm.get("vip_number")
    codes = order_norm.get("product_codes") or []

    vip_ok = bool(vip) and (vip in vip_set)
    missing_vip = None if vip_ok else (vip or "MISSING_VIP_FIELD")

    missing_codes = [c for c in codes if c not in prod_set]
    if not codes:
        missing_codes = ["MISSING_PRODUCT_CODE_FIELD"]

    ok = vip_ok and (len(missing_codes) == 0)
    if ok:
        return ValidationResult(True, "OK", True, None, [])

    reasons = []
    if not vip_ok:
        reasons.append("VIP_NOT_FOUND")
    if missing_codes:
        reasons.append("PRODUCT_CODE_NOT_FOUND")

    return ValidationResult(False, "+".join(reasons), vip_ok, missing_vip, missing_codes)


# ----------------------------
# Main run
# ----------------------------

def run(step2_input: Path, vip_file: Path, product_file: Path, out_dir: Path) -> None:
    # Create batch timestamp for this run
    batch_timestamp = _now_stamp()

    # Create timestamped batch directory
    batch_dir = out_dir / batch_timestamp
    extracted_dir = batch_dir / "extracted_reference_data"
    valid_dir = batch_dir / "valid_data"
    invalid_dir = batch_dir / "invalid_data"

    for d in [batch_dir, extracted_dir, valid_dir, invalid_dir]:
        _ensure_dir(d)

    vip_ref = extract_vip_reference(vip_file)
    prod_ref = extract_product_codes_reference(product_file)

    vip_ref.to_csv(extracted_dir / "vip_reference.csv", index=False, encoding="utf-8-sig")
    prod_ref.to_csv(extracted_dir / "product_codes_reference.csv", index=False, encoding="utf-8-sig")
    _write_json(extracted_dir / "vip_reference.json", {"vip_numbers": vip_ref["vip_number"].tolist()})
    _write_json(extracted_dir / "product_codes_reference.json", {"product_codes": prod_ref["product_code"].tolist()})

    vip_set = set(vip_ref["vip_number"].astype(str).tolist())
    prod_set = set(prod_ref["product_code"].astype(str).tolist())

    orders_raw = load_orders(step2_input)
    normalized = [normalize_order(o, i) for i, o in enumerate(orders_raw)]

    valid_rows: List[Dict[str, Any]] = []
    invalid_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []

    for o in normalized:
        res = validate_one(o, vip_set, prod_set)
        payload = {
            "order_id": o["order_id"],
            "vip_number": o["vip_number"],
            "product_codes": o["product_codes"],
            "validation": {
                "ok": res.ok,
                "reason": res.reason,
                "vip_ok": res.vip_ok,
                "missing_vip": res.missing_vip,
                "missing_product_codes": res.missing_product_codes,
            },
            "raw": o["_raw"],
        }

        # Add email_id if present
        if "email_id" in o:
            payload["email_id"] = o["email_id"]

        file_name = _safe_slug(o["order_id"]) + ".json"
        if res.ok:
            _write_json(valid_dir / file_name, payload)
            valid_rows.append(payload)
        else:
            _write_json(invalid_dir / file_name, payload)
            invalid_rows.append(payload)

        summary_row = {
            "order_id": o["order_id"],
            "email_id": o.get("email_id", ""),
            "ok": res.ok,
            "reason": res.reason,
            "vip_number": o["vip_number"] or "",
            "missing_vip": res.missing_vip or "",
            "product_codes": ",".join(o["product_codes"] or []),
            "missing_product_codes": ",".join(res.missing_product_codes or []),
        }
        summary_rows.append(summary_row)

    _write_jsonl(valid_dir / "orders_valid.jsonl", valid_rows)
    _write_jsonl(invalid_dir / "orders_invalid.jsonl", invalid_rows)

    report = {
        "step": "step_3_reference_validation",
        "created_at": _now_stamp(),
        "inputs": {
            "step2_input": str(step2_input),
            "vip_file": str(vip_file),
            "product_file": str(product_file),
        }, 
        "counts": {
            "orders_total": len(normalized),
            "orders_valid": len(valid_rows),
            "orders_invalid": len(invalid_rows),
            "vip_reference_count": int(vip_ref.shape[0]),
            "product_codes_reference_count": int(prod_ref.shape[0]),
        },
    }
    _write_json(batch_dir / "step3_report.json", report)
    pd.DataFrame(summary_rows).to_csv(batch_dir / "step3_summary.csv", index=False, encoding="utf-8-sig")

    # Print summary
    print("\n" + "=" * 60)
    print("STEP 3 - REFERENCE VALIDATION COMPLETE")
    print("=" * 60)
    print(f"Batch timestamp:     {batch_timestamp}")
    print(f"Total orders:        {len(normalized)}")
    print(f"Valid orders:        {len(valid_rows)} → {valid_dir}")
    print(f"Invalid orders:      {len(invalid_rows)} → {invalid_dir}")
    print(f"\nReference data:")
    print(f"  VIP count:         {int(vip_ref.shape[0])}")
    print(f"  Product codes:     {int(prod_ref.shape[0])}")
    print(f"\nOutput files:")
    print(f"  Report:            {batch_dir / 'step3_report.json'}")
    print(f"  Summary CSV:       {batch_dir / 'step3_summary.csv'}")
    print(f"  Valid JSONL:       {valid_dir / 'orders_valid.jsonl'}")
    print(f"  Invalid JSONL:     {invalid_dir / 'orders_invalid.jsonl'}")
    print("=" * 60)


def find_latest_step2_orders() -> Optional[Path]:
    """Find the latest step2 batch with orders."""
    step2_root = Path("out_step2_ai_order_validation")
    if not step2_root.exists():
        return None

    # Find all batch directories
    batches = [d for d in step2_root.iterdir() if d.is_dir()]
    if not batches:
        return None

    # Sort by name (timestamp format ensures chronological order)
    batches.sort(reverse=True)

    # Find first batch with an order/ subdirectory
    for batch in batches:
        order_dir = batch / "order"
        if order_dir.exists() and list(order_dir.glob("*.json")):
            return order_dir

    return None


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Step 3 - VIP & Product Code reference validation")
    p.add_argument(
        "--step2_input",
        help="Path to Step2 orders folder OR .json OR .csv (default: auto-detect latest batch from out_step2_ai_order_validation/*/order)"
    )
    p.add_argument(
        "--vip_file",
        default="reference_data/VIP.xlsx",
        help="Path to VIP.xlsx (default: reference_data/VIP.xlsx)"
    )
    p.add_argument(
        "--product_file",
        default="reference_data/product_codes.xls",
        help="Path to product_codes.xls or .xlsx (default: reference_data/product_codes.xls)"
    )
    p.add_argument(
        "--out_dir",
        default="out_step3_reference_validation",
        help="Output directory (default: out_step3_reference_validation)"
    )
    return p


def main() -> None:
    args = build_argparser().parse_args()

    # Auto-detect step2 input if not provided
    if args.step2_input:
        step2_path = Path(args.step2_input)
    else:
        print("No --step2_input provided, auto-detecting latest batch...")
        step2_path = find_latest_step2_orders()
        if step2_path is None:
            print("ERROR: Could not find any step2 order batches in out_step2_ai_order_validation/")
            print("Run step2_ai_order_validation.py first, or specify --step2_input manually")
            sys.exit(1)
        print(f"  → Found: {step2_path}")

    vip_path = Path(args.vip_file)
    product_path = Path(args.product_file)

    if not step2_path.exists():
        print(f"ERROR: Step2 input not found: {step2_path}")
        sys.exit(1)

    if not vip_path.exists():
        print(f"ERROR: VIP file not found: {vip_path}")
        sys.exit(1)

    if not product_path.exists():
        print(f"ERROR: Product file not found: {product_path}")
        sys.exit(1)

    try:
        run(
            step2_input=step2_path,
            vip_file=vip_path,
            product_file=product_path,
            out_dir=Path(args.out_dir),
        )
        print(f"\n✓ Step 3 completed successfully!")
        print(f"  Output directory: {Path(args.out_dir).absolute()}")
    except Exception as e:
        print(f"\nERROR: Step 3 failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
