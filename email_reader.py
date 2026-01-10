import os
import re
import json
import base64
from datetime import datetime
from typing import Dict, Any, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from bs4 import BeautifulSoup  # pip install beautifulsoup4
import logging
import json
from typing import Any

from dotenv import load_dotenv
load_dotenv()

# AI integration imports (kept minimal)
try:
    from ai.prompt_builder import build_messages
    from ai.llm_client import chat_completion
    from ai.schemas import parse_and_validate
except Exception:
    # If ai package missing, we'll still keep the rest of functionality working.
    build_messages = None  # type: ignore
    chat_completion = None  # type: ignore
    parse_and_validate = None  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# -----------------------------
# CONFIG
# -----------------------------

# Gmail scope за четене + маркиране като прочетено
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Четем само главната поща (Primary tab), само непрочетени имейли
GMAIL_QUERY = "is:unread newer_than:1d -in:spam -in:trash category:primary"
MAX_EMAILS_PER_RUN = 20

# Step 1: Extracted email data (organized by batch run timestamp)
STEP1_DIR = os.getenv("STEP1_DIR", "out_step1_email_inputs")
os.makedirs(STEP1_DIR, exist_ok=True)

# Step 2: Model classification (organized by batch run timestamp)
STEP2_DIR = os.getenv("STEP2_DIR", "out_step2_order_classification")
os.makedirs(STEP2_DIR, exist_ok=True)

# Ако е True -> след като запишем payload-а, маркираме имейла като прочетен
# По подразбиране НЕ маркираме имейлите като прочетени — маркиране става
# само след успешно класифициране от модела според правилата.
MARK_AS_READ_AFTER_SAVE = False

# Колко от текста да принтираме в конзолата (за debug)
PRINT_BODY_CHARS = 200


# -----------------------------
# AUTH / SERVICE
# -----------------------------
def get_gmail_service():
    """
    OAuth login чрез локален сървър на фиксиран host/port (по-стабилно от port=0).
    След успешен login създава token.json (за да не се логваш всеки път).
    """
    if not os.path.exists("credentials.json"):
        raise FileNotFoundError(
            "Липсва credentials.json в текущата папка. "
            "Свали OAuth client (Desktop app) от Google Cloud и го запази като credentials.json до скрипта."
        )

    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(host="127.0.0.1", port=8080, open_browser=True)

        with open("token.json", "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# -----------------------------
# HELPERS: parsing message parts
# -----------------------------
def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def iter_parts(payload: Dict[str, Any]):
    """Рекурсивно обхожда всички parts в payload."""
    yield payload
    for p in payload.get("parts", []) or []:
        yield from iter_parts(p)


def extract_text_plain(payload: Dict[str, Any]) -> str:
    """Вади първия text/plain, ако го има."""
    for part in iter_parts(payload):
        if part.get("mimeType") == "text/plain":
            body = part.get("body", {}) or {}
            data = body.get("data")
            if data:
                return b64url_decode(data).decode("utf-8", errors="replace")
    return ""


def extract_text_html(payload: Dict[str, Any]) -> str:
    """Вади първия text/html, ако го има."""
    for part in iter_parts(payload):
        if part.get("mimeType") == "text/html":
            body = part.get("body", {}) or {}
            data = body.get("data")
            if data:
                return b64url_decode(data).decode("utf-8", errors="replace")
    return ""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_headers(payload: Dict[str, Any]) -> Dict[str, str]:
    headers = payload.get("headers", []) or []
    out = {}
    for h in headers:
        name = h.get("name")
        value = h.get("value")
        if name and value:
            out[name] = value
    return out


# -----------------------------
# GMAIL: list/read/modify
# -----------------------------
def list_message_ids(service, max_results: int = 10, query: str = "") -> List[str]:
    resp = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    msgs = resp.get("messages", []) or []
    return [m["id"] for m in msgs]


def read_message_full(service, message_id: str) -> Dict[str, Any]:
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()


def mark_as_read(service, message_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


# -----------------------------
# ATTACHMENTS: find/download
# -----------------------------
def list_attachments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Връща списък с attachments:
    [{filename, mimeType, attachmentId, partId}]
    """
    out = []
    for part in iter_parts(payload):
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        att_id = body.get("attachmentId")
        mime = part.get("mimeType", "")
        if filename and att_id:
            out.append(
                {
                    "filename": filename,
                    "mimeType": mime,
                    "attachmentId": att_id,
                    "partId": part.get("partId"),
                }
            )
    return out


def safe_filename(s: str) -> str:
    s = s or "empty"
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s[:180]


def extract_sender_name(from_header: str) -> str:
    """
    Extract clean sender name from 'From' header.
    Examples:
      'John Doe <john@example.com>' -> 'john_doe'
      'john@example.com' -> 'john'
      'Иван Петров <ivan@mail.bg>' -> 'ivan_petrov'
    """
    from_header = from_header or ""

    # Try to extract name before email (e.g., "John Doe <email>")
    match = re.match(r'([^<]+)<', from_header)
    if match:
        name = match.group(1).strip().strip('"').strip("'")
        # Clean and normalize
        name = name.lower()
        name = re.sub(r'[^\w\s-]', '', name)  # remove special chars
        name = re.sub(r'\s+', '_', name)  # spaces to underscores
        if name:
            return name[:50]

    # Fallback: extract email username
    email_match = re.search(r'([a-zA-Z0-9._-]+)@', from_header)
    if email_match:
        return email_match.group(1).lower()[:50]

    return "unknown_sender"


def parse_email_date(date_header: str) -> str:
    """
    Parse email Date header and return timestamp YYYYMMDD_HHMMSS.
    If parsing fails, return current time.
    """
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(date_header)
        return dt.strftime("%Y%m%d_%H%M%S")
    except Exception:
        # Fallback to current time
        return datetime.now().strftime("%Y%m%d_%H%M%S")


from io import BytesIO

def get_attachment_bytes(service, message_id: str, attachment_id: str) -> bytes:
    """Fetch attachment data from Gmail and return raw bytes (no disk write)."""
    att = service.users().messages().attachments().get(
        userId="me",
        messageId=message_id,
        id=attachment_id,
    ).execute()

    data = att.get("data")
    if not data:
        raise RuntimeError("Attachment няма data поле.")

    return b64url_decode(data)


# -----------------------------
# EXCEL: read .xlsx / .xls from bytes (in-memory)
# -----------------------------


def read_excel_bytes(content: bytes, filename: str = "") -> List[List[Any]]:
    """Read Excel file from raw bytes and return rows. Works for .xlsx and .xls."""
    from io import BytesIO

    ext = os.path.splitext(filename)[1].lower()

    # Prefer openpyxl for .xlsx
    if ext == ".xlsx":
        import openpyxl  # pip install openpyxl

        f = BytesIO(content)
        wb = openpyxl.load_workbook(f, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
        return rows

    if ext == ".xls":
        # pip install pyexcel pyexcel-xls
        import pyexcel
        from io import BytesIO

        f = BytesIO(content)
        sheet = pyexcel.get_sheet(file_content=content, file_type='xls')
        return [list(r) for r in sheet.rows()]

    # Unknown extension: try both
    try:
        return read_excel_any_bytes_try_both(content)
    except Exception:
        return []


def read_excel_any_bytes_try_both(content: bytes) -> List[List[Any]]:
    """Best-effort attempt to read Excel bytes (try xlsx then xls)."""
    try:
        return read_excel_bytes(content, filename="file.xlsx")
    except Exception:
        try:
            return read_excel_bytes(content, filename="file.xls")
        except Exception:
            return []


def _as_str(x) -> str:
    if x is None:
        return ""
    s = str(x).strip()
    # Excel понякога дава 33616.0
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s

def _as_int(x):
    if x is None:
        return None
    if isinstance(x, (int, float)) and x == x:
        # 33616.0 -> 33616
        return int(x)
    s = _as_str(x)
    if not s:
        return None
    if s.isdigit():
        return int(s)
    return None

def extract_orders_from_blanka_rows(rows: List[List[Any]]) -> Dict[str, Any]:
    """
    Екстрактва множество поръчки (може да има няколко VIP-а в една бланка).
    Връща:
    {
      "orders": [
        {"vip_number": "33616", "items":[{"product_code":"3072630","qty":1}, ...]},
        ...
      ],
      "uncertainties": [...]
    }
    """
    uncertainties = []

    # 1) намираме header реда по ключови думи
    header_idx = None
    for i, r in enumerate(rows[:200]):
        line = " ".join(_as_str(c).lower() for c in r[:20])
        if ("вип" in line or "vip" in line) and "код" in line and ("бр" in line or "бр." in line):
            header_idx = i
            break

    if header_idx is None:
        return {
            "orders": [],
            "uncertainties": ["Header row not found in blanka (cannot locate VIP/Kod/BR.)."]
        }

    # 2) За твоя файл колоните са стабилни:
    VIP_COL = 5
    CODE_COL = 7
    QTY_COL = 9

    current_vip = ""
    orders_map = {}  # vip -> {product_code: qty}

    # 3) четем редовете след header
    for r in rows[header_idx + 1 : header_idx + 1 + 500]:
        # VIP (може да липсва на реда)
        vip_val = _as_str(r[VIP_COL]) if VIP_COL < len(r) else ""
        # ако клетката е текст "ВИП№" я игнорираме
        if vip_val.lower().startswith(("vip", "вип")):
            vip_val = ""

        if vip_val.isdigit():
            current_vip = vip_val

        # Код
        code_val = _as_str(r[CODE_COL]) if CODE_COL < len(r) else ""
        if not code_val:
            continue

        # “Въведете код” / празни / не-цифрови -> skip
        if not code_val.isdigit():
            continue

        # Количество
        qty_val = _as_int(r[QTY_COL]) if QTY_COL < len(r) else None
        qty = qty_val if qty_val and qty_val > 0 else 1

        if not current_vip:
            uncertainties.append(f"Found product code {code_val} but VIP is missing (row after header).")
            continue

        if current_vip not in orders_map:
            orders_map[current_vip] = {}
        orders_map[current_vip][code_val] = orders_map[current_vip].get(code_val, 0) + qty

    # 4) изход
    orders = []
    for vip, items_map in orders_map.items():
        items = [{"product_code": code, "qty": qty} for code, qty in items_map.items()]
        orders.append({"vip_number": vip, "items": items})

    if not orders:
        uncertainties.append("No order lines found after header (no numeric product codes).")

    return {"orders": orders, "uncertainties": uncertainties, "debug": {"header_row_index": header_idx}}


def excel_rows_to_text(rows: List[List[Any]], max_rows: int = 50, max_cols: int = 12) -> str:
    """
    Превръща Excel rows в текстова таблица (удобна за LLM).
    """
    out_lines = []
    for r in rows[:max_rows]:
        cells = []
        for c in r[:max_cols]:
            if c is None:
                cells.append("")
            else:
                cells.append(str(c).strip())
        out_lines.append("\t".join(cells).rstrip())
    return "\n".join(out_lines).strip()


# -----------------------------
# SAVE "MODEL INPUT" PAYLOAD
# -----------------------------
def _save_json_atomic(obj: Any, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def create_minimal_payload_for_model(full_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a minimal payload for the model with only essential information:
    - Email body text
    - Extracted orders (VIP + products)

    Removes all metadata, headers, raw Excel rows, etc.
    """
    minimal = {
        "body_text": full_payload.get("body_text", ""),
        "extracted_orders": full_payload.get("extracted_orders", [])
    }

    return minimal


# -----------------------------
# MAIN
# -----------------------------
def main():
    # Create batch timestamp for this run (used for organizing all emails processed in this execution)
    batch_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Step 1 directory: timestamp-based batch folder
    step1_batch_dir = os.path.join(STEP1_DIR, batch_timestamp)
    os.makedirs(step1_batch_dir, exist_ok=True)

    # Step 2 directory structure
    step2_batch_dir = os.path.join(STEP2_DIR, batch_timestamp)
    step2_raw_dir = os.path.join(step2_batch_dir, "raw")
    step2_order_dir = os.path.join(step2_batch_dir, "order")
    step2_not_order_dir = os.path.join(step2_batch_dir, "not_order")
    step2_needs_manual_dir = os.path.join(step2_batch_dir, "needs_manual")

    os.makedirs(step2_raw_dir, exist_ok=True)
    os.makedirs(step2_order_dir, exist_ok=True)
    os.makedirs(step2_not_order_dir, exist_ok=True)
    os.makedirs(step2_needs_manual_dir, exist_ok=True)
    service = get_gmail_service()

    message_ids = list_message_ids(service, max_results=MAX_EMAILS_PER_RUN, query=GMAIL_QUERY)
    if not message_ids:
        print("Няма непрочетени имейли в INBOX.")
        return

    for mid in message_ids:
        msg = read_message_full(service, mid)
        payload = msg.get("payload", {}) or {}
        headers = get_headers(payload)

        subject = headers.get("Subject", "(no subject)")
        sender = headers.get("From", "(unknown)")
        date = headers.get("Date", "(unknown)")

        # 1) Body: предпочитаме text/plain; ако няма -> html->text
        text_plain = extract_text_plain(payload).strip()
        if not text_plain:
            html = extract_text_html(payload).strip()
            text_plain = html_to_text(html) if html else ""

        # 2) Attachments: сваляме и (ако е excel) правим preview
        atts = list_attachments(payload)

        model_payload: Dict[str, Any] = {
            "message_id": msg.get("id", mid),
            "thread_id": msg.get("threadId", ""),
            "headers": {"From": sender, "Subject": subject, "Date": date},
            "body_text": text_plain or "",
            "attachments": [],
            "excel_previews": [],
        }

        # Конзолен debug
        print("========================================")
        print("From:", sender)
        print("Subject:", subject)
        print("Date:", date)
        print("--- BODY (as text) ---")
        print((text_plain or "")[:PRINT_BODY_CHARS])

        if atts:
            print(f"--- ATTACHMENTS ({len(atts)}) ---")

        for a in atts:
            filename = a["filename"]
            mime = a.get("mimeType", "")
            ext = os.path.splitext(filename)[1].lower()
            print(f"- {filename} ({mime})")

            # Only process Excel files (read in-memory, never save to disk)
            if ext in (".xls", ".xlsx"):
                try:
                    # Read excel in-memory (no disk write)
                    content = get_attachment_bytes(service, mid, a["attachmentId"])
                    rows = read_excel_bytes(content, filename=filename)

                    model_payload["attachments"].append(
                        {"filename": filename, "mimeType": mime, "in_memory": True}
                    )

                    extracted = extract_orders_from_blanka_rows(rows)

                    # Store only extracted orders (VIP + products), not raw Excel rows
                    if extracted.get("orders"):
                        for order in extracted["orders"]:
                            model_payload.setdefault("extracted_orders", []).append({
                                "vip_number": order.get("vip_number", ""),
                                "items": order.get("items", [])
                            })

                    print("📦 Extracted orders:", extracted)

                    print(f"  excel rows: {len(rows)}")
                    # покажи първите 5 реда за диагностика
                    for r in rows[:5]:
                        print("  ", r)

                except Exception as e:
                    model_payload["excel_previews"].append(
                        {"filename": filename, "in_memory": True, "error": repr(e)}
                    )
                    print("  ⚠️ excel read error:", repr(e))
            else:
                # Non-Excel attachments: just record metadata, don't download
                model_payload["attachments"].append(
                    {"filename": filename, "mimeType": mime, "skipped": True, "reason": "not_excel"}
                )
                print(f"  ⏭️ skipped (not Excel)")

        # 3) Save the email input payload to step1
        # Use email received date + sender name for readable filenames
        email_timestamp = parse_email_date(date)
        sender_name = extract_sender_name(sender)
        # Add short message ID suffix to ensure uniqueness
        msg_id_short = model_payload['message_id'][:8] if model_payload.get('message_id') else "unknown"

        base_filename = f"{email_timestamp}_{sender_name}_{msg_id_short}"
        step1_json = os.path.join(step1_batch_dir, f"{base_filename}.json")

        # Handle duplicate filenames (shouldn't happen but just in case)
        if os.path.exists(step1_json):
            counter = 1
            while os.path.exists(os.path.join(step1_batch_dir, f"{base_filename}_{counter}.json")):
                counter += 1
            base_filename = f"{base_filename}_{counter}"
            step1_json = os.path.join(step1_batch_dir, f"{base_filename}.json")

        _save_json_atomic(model_payload, step1_json)
        logging.info("Saved email input to step1: %s", step1_json)

        # 4) If LLM support is available, run the classification pipeline
        if build_messages and chat_completion and parse_and_validate:
            try:
                # Create minimal payload for model (only body + extracted orders)
                minimal_payload = create_minimal_payload_for_model(model_payload)
                messages = build_messages(minimal_payload)

                # Save request to step2/raw/
                req_path = os.path.join(step2_raw_dir, f"{base_filename}_request.json")
                _save_json_atomic(messages, req_path)

                # Call LLM
                model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
                logging.info("Calling LLM model=%s for %s", model_name, base_filename)
                raw = chat_completion(messages=messages, model=model_name, temperature=0.0, timeout=int(os.getenv("OPENAI_TIMEOUT", "60")))

                # Save raw response to step2/raw/
                raw_path = os.path.join(step2_raw_dir, f"{base_filename}_raw.json")
                _save_json_atomic(raw, raw_path)

                # Extract assistant content (first choice)
                try:
                    assistant_text = ""
                    choices = raw.get("choices") or []
                    if choices:
                        msg = choices[0].get("message") or {}
                        assistant_text = msg.get("content", "")
                except Exception:
                    assistant_text = ""

                parsed = None
                parsed_path_raw = os.path.join(step2_raw_dir, f"{base_filename}_parsed.json")
                try:
                    parsed = parse_and_validate(assistant_text)
                    parsed_dict = parsed.model_dump()
                    # Save to raw folder
                    _save_json_atomic(parsed_dict, parsed_path_raw)

                    # Also save to classification-specific folder (order/not_order/needs_manual)
                    classification = parsed.classification
                    if classification == "order":
                        classified_path = os.path.join(step2_order_dir, f"{base_filename}.json")
                    elif classification == "not_order":
                        classified_path = os.path.join(step2_not_order_dir, f"{base_filename}.json")
                    else:  # needs_manual
                        classified_path = os.path.join(step2_needs_manual_dir, f"{base_filename}.json")

                    _save_json_atomic(parsed_dict, classified_path)
                    logging.info("Classification: %s → saved to %s", classification, classified_path)

                except Exception as e:
                    # Save parsing error and assistant text for manual review
                    err_path = os.path.join(step2_raw_dir, f"{base_filename}_parse_error.txt")
                    with open(err_path, "w", encoding="utf-8") as ef:
                        ef.write("--- ASSISTANT TEXT ---\n")
                        ef.write(assistant_text or "")
                        ef.write("\n--- PARSE ERROR ---\n")
                        ef.write(repr(e))
                    logging.error("Failed to parse response for %s: %s", base_filename, e)

                # Decide marking as read
                mark_read = False
                if parsed is not None:
                    try:
                        # parsed is a pydantic model
                        cls = parsed.classification
                        conf = float(parsed.confidence or 0.0)
                        if cls != "needs_manual" and conf >= 0.85:
                            mark_read = True
                    except Exception:
                        mark_read = False

                if mark_read:
                    try:
                        mark_as_read(service, mid)
                        logging.info("Marked message %s as read (classification=%s).", mid, getattr(parsed, "classification", None))
                    except Exception as e:
                        logging.warning("Failed to mark as read: %s", e)
                else:
                    logging.info("Left message %s unread (mark_read=%s).", mid, mark_read)

            except Exception as e:
                logging.exception("LLM pipeline failed for message %s: %s", mid, e)
                # Save the exception
                errf = os.path.join(step2_raw_dir, f"{base_filename}_llm_error.txt")
                with open(errf, "w", encoding="utf-8") as ef:
                    ef.write(repr(e))
        else:
            logging.info("AI pipeline not available — skipped LLM call for %s.", base_filename)

    logging.info("Done.")


if __name__ == "__main__":
    main()
