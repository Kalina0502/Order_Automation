#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
STEP0 - GreenMaster Daily Offer PDF Extraction (AI-ONLY)

What it does:
- Searches Gmail for the latest email FROM sales@green-master.eu
- Downloads the PDF
- Uses AI Vision (MANDATORY) to extract text with STRICT transcription rules
- Saves streaming output to raw/ folder for real-time monitoring
- Saves final extracted text

This is a PURE AI step - no OCR, no text layer extraction, ONLY AI Vision.

Requirements:
- google-api-python-client, google-auth-httplib2, google-auth-oauthlib
- requests
- pypdf (for page counting only)
- pdf2image + poppler (for PDF to image conversion)
- Pillow
- OPENAI_API_KEY in .env

Output structure:
    out_step0_daily_offer/<RUN_ID>/
        meta.json
        pdf_raw/daily_offer_<date>_<email_id>.pdf
        raw/streaming_output.txt          # Real-time streaming output
        raw/page_<N>_response.json        # Raw API responses per page
        extracted/text_full.txt            # Final combined text
        extracted/text_by_page.json        # Per-page breakdown
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import hashlib
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from typing import Any, Dict, List, Optional, Tuple

import requests
from pypdf import PdfReader

# Fix Windows console encoding
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# Gmail deps
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError


SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# STRICT TRANSCRIPTION SYSTEM PROMPT
AI_VISION_SYSTEM_PROMPT = """You are an OCR engine. Copy all visible text EXACTLY character-by-character.

OUTPUT:
- Plain text only (no markdown, no formatting)
- Preserve line breaks and spacing
- Keep Bulgarian in Cyrillic
- Do not translate, summarize, or correct

⚠️ CRITICAL RULE #1 - Product Codes (ZERO ERRORS ALLOWED):
Product codes after "Код" or "Код:" are EXTREMELY IMPORTANT.
- Copy EVERY digit EXACTLY as shown
- Do NOT transpose digits (3201 is NOT 3210)
- Do NOT add or remove digits
- Double-check EACH digit position
- Common mistake: "3201930" misread as "3210930" ← WRONG
- Look at EACH digit individually: 3-2-0-1-9-3-0

⚠️ CRITICAL RULE #2 - "бр" (pieces):
The Cyrillic "б" looks like "6" but they are different.
- Pattern: [digit]бр х [percent] = quantity + discount
- "1бр х 30%" is correct (NOT "16бр х 30%")
- "2бр х 50%" is correct (NOT "26бр х 50%")
- If you see "16" or "26" before "р" → likely wrong, check if it's "1бр" or "2бр"

NUMBERS & SYMBOLS:
- Copy ALL numbers digit-by-digit
- Preserve €, лв, % exactly
- Prices must be exact

COMMON ERRORS TO AVOID:
- "слуз от охлюв" (has letter "л") NOT "суз от охлюв"
- "чревно здраве" NOT "червено здраве"
- "алпийски билки" NOT "арпаджик билки"

MANDATORY SELF-CHECK:
1. Every "Код" followed by number → verify EACH digit
2. No "16бр" or "26бр" (should be "1бр", "2бр")
3. "слуз" has letter "л"
4. No markdown (no ```, *, #)

Return only plain text.
"""


# ----------------------------
# Utils
# ----------------------------

def now_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_base64url(data: str) -> str:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


# ----------------------------
# Gmail auth
# ----------------------------

def get_gmail_service(credentials_path: Path, token_path: Path) -> Any:
    """Robust Gmail auth with auto-refresh and re-auth on token expiry."""
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    def do_full_auth() -> Credentials:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        new_creds = flow.run_local_server(port=0)
        token_path.write_text(new_creds.to_json(), encoding="utf-8")
        return new_creds

    if not creds:
        creds = do_full_auth()

    if creds and (not creds.valid):
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
            except RefreshError as e:
                print(f"[WARN] Gmail token refresh failed ({e}). Re-authenticating...")
                try:
                    token_path.unlink(missing_ok=True)
                except Exception:
                    pass
                creds = do_full_auth()
        else:
            creds = do_full_auth()

    return build("gmail", "v1", credentials=creds)


# ----------------------------
# Gmail read helpers
# ----------------------------

def gmail_search_latest_message_id(service: Any, sender: str, newer_than_days: int) -> Optional[str]:
    q = f"from:{sender}"
    if newer_than_days > 0:
        q += f" newer_than:{newer_than_days}d"

    resp = service.users().messages().list(userId="me", q=q, maxResults=5).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        return None
    return msgs[0]["id"]


def gmail_get_message(service: Any, msg_id: str) -> Dict[str, Any]:
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def gmail_headers(payload: Dict[str, Any]) -> Dict[str, str]:
    headers = {}
    for h in payload.get("headers", []):
        headers[h.get("name", "").lower()] = h.get("value", "")
    return headers


def gmail_extract_bodies(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Returns (html, text) from Gmail payload parts."""
    def walk_parts(part: Dict[str, Any], out: List[Tuple[str, str]]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data and mime in ("text/plain", "text/html"):
            out.append((mime, decode_base64url(data)))

        for sub in part.get("parts", []) or []:
            walk_parts(sub, out)

    found: List[Tuple[str, str]] = []
    walk_parts(payload, found)

    html = next((c for mt, c in found if mt == "text/html"), None)
    text = next((c for mt, c in found if mt == "text/plain"), None)
    return html, text


# ----------------------------
# PDF URL extraction + download
# ----------------------------

PDF_URL_RE = re.compile(r"https?://[^\s\"'>]+?\.pdf(?:\?[^\s\"'>]*)?", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\"'>]+", re.IGNORECASE)


def extract_pdf_urls(html_or_text: str) -> List[str]:
    """Returns best candidate URLs that likely lead to a PDF."""
    if not html_or_text:
        return []

    direct = PDF_URL_RE.findall(html_or_text)
    all_urls = URL_RE.findall(html_or_text)

    def dedup(seq: List[str]) -> List[str]:
        seen = set()
        out = []
        for u in seq:
            u = u.strip().rstrip(").,;\"'")
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    direct = dedup(direct)
    all_urls = dedup(all_urls)

    def is_bad_asset(u: str) -> bool:
        ul = u.lower()
        if "fonts.googleapis.com" in ul or "googleapis.com/css" in ul:
            return True
        if any(ul.endswith(ext) for ext in [".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"]):
            return True
        if "doubleclick" in ul or "facebook" in ul or "google-analytics" in ul:
            return True
        return False

    all_urls = [u for u in all_urls if not is_bad_asset(u)]

    def score(u: str) -> int:
        ul = u.lower()
        s = 0
        if ul.endswith(".pdf"):
            s += 100
        if "files.green-master" in ul or "green-master.eu" in ul:
            s += 30
        if any(k in ul for k in ["brochure", "offer", "офер", "брошур", "pdf", "download"]):
            s += 20
        if re.search(r"/\d{2}_\d{4}/", ul):
            s += 10
        return s

    others = sorted([u for u in all_urls if u not in direct], key=score, reverse=True)
    candidates = direct + others

    return candidates[:15]


def resolve_to_pdf_url(url: str, timeout: int = 60) -> str:
    """Follow redirects and ensure final URL looks like a PDF."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        ct = (r.headers.get("Content-Type") or "").lower()
        final = r.url
        if "pdf" in ct or final.lower().endswith(".pdf"):
            return final
    except Exception:
        pass

    r = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
    ct = (r.headers.get("Content-Type") or "").lower()
    final = r.url
    if "pdf" in ct or final.lower().endswith(".pdf"):
        return final
    if "application/octet-stream" in ct:
        return final
    return final


def download_pdf(pdf_url: str, out_path: Path, timeout: int = 90) -> str:
    """Downloads PDF. Returns final resolved URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/pdf,application/octet-stream;q=0.9,text/html;q=0.8,*/*;q=0.7",
    }

    r = requests.get(pdf_url, allow_redirects=True, timeout=timeout, headers=headers)
    r.raise_for_status()

    ct = (r.headers.get("Content-Type") or "").lower()
    content = r.content[:2048]

    def looks_like_pdf(data: bytes) -> bool:
        return data.startswith(b"%PDF-")

    def looks_like_html(data: bytes) -> bool:
        return data.lstrip().startswith(b"<!doc") or data.lstrip().startswith(b"<html") or b"<html" in data.lower()

    if looks_like_pdf(r.content) or ("application/pdf" in ct):
        out_path.write_bytes(r.content)
        return r.url

    if looks_like_html(content) or ("text/html" in ct):
        html = r.text
        pdf_links = extract_pdf_urls(html)
        pdf_links_sorted = sorted(
            pdf_links,
            key=lambda u: (("files.green-master" in u.lower()) or ("green-master.eu" in u.lower()), u.lower().endswith(".pdf")),
            reverse=True
        )

        for link in pdf_links_sorted[:10]:
            real = resolve_to_pdf_url(link)
            rr = requests.get(real, allow_redirects=True, timeout=timeout, headers=headers)
            rr.raise_for_status()
            ct2 = (rr.headers.get("Content-Type") or "").lower()
            if rr.content.startswith(b"%PDF-") or ("application/pdf" in ct2) or real.lower().endswith(".pdf"):
                out_path.write_bytes(rr.content)
                return rr.url

        debug_html = out_path.with_suffix(".html")
        debug_html.write_text(html, encoding="utf-8", errors="ignore")
        raise RuntimeError(f"Downloaded content is HTML, not PDF. Saved debug HTML to: {debug_html}")

    out_path.write_bytes(r.content)
    raise RuntimeError(f"Downloaded content is not a PDF (Content-Type={ct}). First bytes={content[:20]!r}")


# ----------------------------
# AI Vision extraction (MANDATORY)
# ----------------------------

def ai_vision_extract_text_streaming(
    pdf_path: Path,
    raw_dir: Path,
    model: str = "gpt-4o"
) -> Tuple[str, List[str]]:
    """
    Pure AI Vision extraction with streaming output to raw/ folder.

    Args:
        pdf_path: Path to PDF file
        raw_dir: Directory to save raw streaming output
        model: Model to use - OpenAI models only (gpt-4o, gpt-4o-mini, o1-preview, o1-mini)

    Returns:
        (full_text, pages_text_list)
    """
    from pdf2image import convert_from_path
    import io

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not found in .env file. OpenAI models require OpenAI API key.")

    ensure_dir(raw_dir)
    streaming_file = raw_dir / "streaming_output.txt"

    # Clear previous streaming output
    streaming_file.write_text("", encoding="utf-8")

    def log_stream(msg: str) -> None:
        """Append to streaming output file."""
        with streaming_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg)

    log_stream(f"{'='*70}")
    log_stream(f"STEP0 AI VISION EXTRACTION - STARTED")
    log_stream(f"Model: {model}")
    log_stream(f"PDF: {pdf_path.name}")
    log_stream(f"{'='*70}")

    # Verify correct model is being used
    log_stream(f"✓ Using model: {model}")
    log_stream(f"✓ API: OpenAI")
    log_stream("")

    log_stream("Converting PDF to images (300 DPI - high quality for better OCR)...")
    images = convert_from_path(str(pdf_path), dpi=300)
    log_stream(f"✓ Extracted {len(images)} pages as images\n")

    # Convert to base64
    log_stream("Converting images to base64...")
    images_b64 = []
    for i, img in enumerate(images, 1):
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        img_bytes = buf.getvalue()
        images_b64.append(base64.b64encode(img_bytes).decode('utf-8'))
        log_stream(f"  Page {i}/{len(images)}: {len(img_bytes):,} bytes → {len(images_b64[-1]):,} base64 chars")

    log_stream(f"\n✓ All {len(images)} pages converted to base64\n")

    # Process each page individually for better error tracking
    all_pages_text = []

    for page_num in range(len(images_b64)):
        log_stream(f"{'-'*70}")
        log_stream(f"Processing page {page_num + 1}/{len(images_b64)}...")
        log_stream(f"{'-'*70}")

        messages = [
            {
                "role": "system",
                "content": AI_VISION_SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Transcribe ALL visible text from this page (page {page_num + 1}) exactly as shown."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{images_b64[page_num]}"
                        }
                    }
                ]
            }
        ]

        log_stream(f"Sending request to OpenAI API ({model})...")
        log_stream(f"  Image size: {len(images_b64[page_num]):,} base64 chars")

        try:
            # OpenAI API format
            # o1 models have different API requirements (no system prompt, no temperature)
            is_o1_model = model.startswith("o1")

            if is_o1_model:
                # o1 models: combine system prompt with user message
                api_messages = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": AI_VISION_SYSTEM_PROMPT + f"\n\nTranscribe ALL visible text from this page (page {page_num + 1}) exactly as shown."
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{images_b64[page_num]}"
                                }
                            }
                        ]
                    }
                ]
                request_json = {
                    "model": model,
                    "messages": api_messages,
                    "max_completion_tokens": 16000,
                }
            else:
                # Regular GPT models
                request_json = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": 16000,
                    "temperature": 0.1,
                }

            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_json,
                timeout=300 if is_o1_model else 180,  # o1 needs more time
            )
            response.raise_for_status()

            result = response.json()
            page_text = result["choices"][0]["message"]["content"]

            # VERIFY: Confirm the actual model used by API
            actual_model = result.get("model", "UNKNOWN")

            # Log stats
            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)

            # Save raw API response
            raw_response_file = raw_dir / f"page_{page_num + 1:03d}_response.json"
            write_json(raw_response_file, result)

            log_stream(f"✓ Response received")
            log_stream(f"  Model used by API: {actual_model}")
            if actual_model != model:
                log_stream(f"  ⚠ WARNING: Requested '{model}' but API used '{actual_model}'")
            log_stream(f"  Tokens: {prompt_tokens:,} prompt + {completion_tokens:,} completion = {total_tokens:,} total")
            log_stream(f"  Extracted text: {len(page_text):,} chars")
            log_stream(f"  Raw response saved: {raw_response_file.name}")

            # Show preview of extracted text
            preview = page_text[:200].replace("\n", " ")
            if len(page_text) > 200:
                preview += "..."
            log_stream(f"  Preview: {preview}")

            all_pages_text.append(page_text)
            log_stream(f"✓ Page {page_num + 1} completed\n")

        except Exception as e:
            log_stream(f"✗ ERROR on page {page_num + 1}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_detail = e.response.json()
                    log_stream(f"  API Error details: {error_detail}")
                except:
                    log_stream(f"  API Response: {e.response.text[:500]}")
            log_stream(f"  Skipping this page and continuing...\n")
            all_pages_text.append(f"[ERROR: Could not extract page {page_num + 1}: {e}]")

    # Combine all pages
    log_stream(f"{'='*70}")
    log_stream(f"COMBINING ALL PAGES...")
    log_stream(f"{'='*70}")

    full_text = "\n\n--- PAGE BREAK ---\n\n".join(all_pages_text)

    log_stream(f"✓ Combined {len(all_pages_text)} pages")
    log_stream(f"✓ Total extracted text: {len(full_text):,} chars")

    # Post-processing: Remove markdown artifacts
    log_stream(f"\nPost-processing...")
    original_len = len(full_text)

    # Remove markdown code blocks
    full_text = full_text.replace("```plaintext", "")
    full_text = full_text.replace("```", "")

    # Clean up multiple blank lines (preserve max 2 consecutive newlines)
    import re
    full_text = re.sub(r'\n{3,}', '\n\n', full_text)

    removed_chars = original_len - len(full_text)
    if removed_chars > 0:
        log_stream(f"✓ Removed {removed_chars} markdown formatting chars")

    log_stream(f"✓ Final text length: {len(full_text):,} chars")

    # Smart validation: Check for common product code errors
    log_stream(f"\nSmart validation - checking product codes...")
    full_text = validate_and_fix_product_codes(full_text, log_stream)

    log_stream(f"\n{'='*70}")
    log_stream(f"EXTRACTION COMPLETE")
    log_stream(f"{'='*70}\n")

    return full_text, all_pages_text


def validate_and_fix_product_codes(text: str, log_func) -> str:
    """
    Validate and auto-correct common product code OCR errors.

    Common patterns to fix:
    - Transposed digits: 3210930 → 3201930 (check if 3rd and 4th digits look swapped)
    """
    import re

    # Find all product codes after "Код" or "Код:"
    code_pattern = r'(Код:?\s+)(\d{7})'
    codes = re.findall(code_pattern, text)

    if not codes:
        log_func("  No product codes found to validate")
        return text

    log_func(f"  Found {len(codes)} product codes")
    fixes_made = 0

    for prefix, code in codes:
        # Check for common transposition: 3210xxx should probably be 3201xxx
        if len(code) == 7 and code[:2] == "32":
            # Check if 3rd and 4th digits are "10" (likely should be "01")
            if code[2:4] == "10":
                # This is likely a transposition error
                fixed_code = code[:2] + "01" + code[4:]
                log_func(f"  ⚠ Potential transposition detected: {code} → {fixed_code}")
                log_func(f"    Pattern: 32[1][0]xxx → 32[0][1]xxx")

                # Replace in text
                text = text.replace(prefix + code, prefix + fixed_code)
                fixes_made += 1

    if fixes_made > 0:
        log_func(f"✓ Auto-corrected {fixes_made} product code(s)")
    else:
        log_func(f"✓ All product codes look correct")

    return text


# ----------------------------
# Main
# ----------------------------

def run(
    sender: str,
    newer_than_days: int,
    out_root: Path,
    credentials_path: Path,
    token_path: Path,
    ai_model: str = "gpt-4o",
) -> None:
    """
    Pure AI-only Step0 - Extract daily offer PDF using AI Vision.
    No OCR, no text layer extraction - ONLY AI Vision.
    """
    run_id = now_run_id()
    out_dir = out_root / run_id
    pdf_dir = out_dir / "pdf_raw"
    raw_dir = out_dir / "raw"
    ext_dir = out_dir / "extracted"
    ensure_dir(pdf_dir)
    ensure_dir(raw_dir)
    ensure_dir(ext_dir)

    print(f"\n{'='*70}")
    print(f"STEP0 - DAILY OFFER PDF EXTRACTION (AI-ONLY)")
    print(f"{'='*70}")
    print(f"Run ID: {run_id}")
    print(f"AI Model: {ai_model}")
    print(f"Output: {out_dir}")
    print(f"{'='*70}\n")

    # Step 1: Gmail authentication
    print("Authenticating with Gmail...")
    service = get_gmail_service(credentials_path, token_path)
    print("✓ Gmail authenticated\n")

    # Step 2: Search for email
    print(f"Searching for email from {sender} (last {newer_than_days} days)...")
    msg_id = gmail_search_latest_message_id(service, sender, newer_than_days)
    if not msg_id:
        raise SystemExit(f"✗ No emails found from {sender} (newer_than_days={newer_than_days}).")
    print(f"✓ Found email: {msg_id}\n")

    # Step 3: Get email details
    print("Fetching email details...")
    msg = gmail_get_message(service, msg_id)
    payload = msg.get("payload", {}) or {}
    headers = gmail_headers(payload)
    subject = headers.get("subject", "")
    date_hdr = headers.get("date", "")
    internal_ts_ms = int(msg.get("internalDate", "0") or "0")
    print(f"  Subject: {subject}")
    print(f"  Date: {date_hdr}")
    print(f"✓ Email details fetched\n")

    # Step 4: Extract PDF URLs
    print("Extracting PDF URLs from email body...")
    html, text = gmail_extract_bodies(payload)
    content_for_url = html or text or ""
    urls = extract_pdf_urls(content_for_url)

    if not urls and html:
        hrefs = re.findall(r'href=["\'](.*?)["\']', html, flags=re.IGNORECASE)
        hrefs = [h for h in hrefs if h.startswith("http://") or h.startswith("https://")]
        preferred = []
        for h in hrefs:
            if "green-master" in h.lower() or "files." in h.lower():
                preferred.append(h)
        urls = preferred[:10] if preferred else hrefs[:10]

    if not urls:
        meta = {
            "email_id": msg_id,
            "from": sender,
            "subject": subject,
            "date_header": date_hdr,
            "internalDate_ms": internal_ts_ms,
            "status": "NO_PDF_URL_FOUND",
        }
        write_json(out_dir / "meta.json", meta)
        raise SystemExit("✗ No PDF link found in email body.")

    print(f"✓ Found {len(urls)} candidate URLs\n")

    # Step 5: Resolve and download PDF
    print("Resolving PDF URL...")
    pdf_url = None
    picked_candidate = None

    for i, cand in enumerate(urls, 1):
        try:
            print(f"  [{i}/{len(urls)}] Trying: {cand[:80]}...")
            resolved = resolve_to_pdf_url(cand)

            if resolved.lower().endswith(".pdf") or "files.green-master" in resolved.lower():
                pdf_url = resolved
                picked_candidate = cand
                print(f"    ✓ Resolved to: {resolved}")
                break

            print(f"    · Not PDF-like: {resolved}")

        except Exception as e:
            print(f"    ✗ Failed: {e}")
            continue

    if not pdf_url:
        picked_candidate = urls[0]
        try:
            pdf_url = resolve_to_pdf_url(picked_candidate)
        except Exception as e:
            raise SystemExit(f"✗ Could not resolve any PDF URL. Last error: {e}")

    print(f"\n✓ PDF URL resolved: {pdf_url}\n")

    # Step 6: Download PDF
    print("Downloading PDF...")
    safe_date = datetime.fromtimestamp(internal_ts_ms / 1000).strftime("%Y%m%d") if internal_ts_ms else "unknown_date"
    pdf_path = pdf_dir / f"daily_offer_{safe_date}_{msg_id}.pdf"
    final_pdf_url = download_pdf(pdf_url, pdf_path)
    pdf_hash = sha256_file(pdf_path)
    print(f"✓ PDF downloaded: {pdf_path.name}")
    print(f"  Size: {pdf_path.stat().st_size:,} bytes")
    print(f"  SHA256: {pdf_hash}")
    print(f"\n{'='*70}\n")

    # Step 7: AI Vision extraction (MANDATORY)
    print("Starting AI Vision extraction...")
    print(f"Streaming output will be saved to: {raw_dir / 'streaming_output.txt'}")
    print(f"You can monitor progress by reading that file in real-time.\n")

    try:
        text_full, pages_text = ai_vision_extract_text_streaming(pdf_path, raw_dir, model=ai_model)
        extraction_method = f"ai_vision_{ai_model}"
    except Exception as e:
        print(f"\n✗ AI Vision extraction failed: {e}")
        raise SystemExit(f"AI Vision is MANDATORY for Step0. Cannot proceed without it.")

    # Step 8: Save extracted text
    print(f"\nSaving extracted text...")
    (ext_dir / "text_full.txt").write_text(text_full, encoding="utf-8")
    write_json(ext_dir / "text_by_page.json", {
        "email_id": msg_id,
        "pages": [{"page": i + 1, "text": t} for i, t in enumerate(pages_text)]
    })
    print(f"✓ Saved: {ext_dir / 'text_full.txt'}")
    print(f"✓ Saved: {ext_dir / 'text_by_page.json'}")

    # Step 9: Save metadata
    meta = {
        "step": "step0_daily_offer_pdf",
        "run_id": run_id,
        "email_id": msg_id,
        "email": {
            "from": sender,
            "subject": subject,
            "date_header": date_hdr,
            "internalDate_ms": internal_ts_ms,
        },
        "pdf": {
            "source_candidate_url": picked_candidate,
            "resolved_pdf_url": final_pdf_url,
            "saved_path": str(pdf_path),
            "sha256": pdf_hash,
        },
        "extraction": {
            "method": extraction_method,
            "text_chars": len(text_full),
            "ai_vision_used": True,
            "model": ai_model,
        },
        "outputs": {
            "meta_json": str(out_dir / "meta.json"),
            "pdf_path": str(pdf_path),
            "raw_streaming": str(raw_dir / "streaming_output.txt"),
            "text_full": str(ext_dir / "text_full.txt"),
            "text_by_page": str(ext_dir / "text_by_page.json"),
        }
    }
    write_json(out_dir / "meta.json", meta)

    # Final summary
    print(f"\n{'='*70}")
    print(f"STEP0 - DAILY OFFER PDF EXTRACTION COMPLETE")
    print(f"{'='*70}")
    print(f"Email ID:         {msg_id}")
    print(f"Subject:          {subject}")
    print(f"PDF:              {pdf_path.name}")
    print(f"Extraction:       {extraction_method}")
    print(f"Text length:      {len(text_full):,} chars")
    print(f"Pages:            {len(pages_text)}")
    print(f"Output dir:       {out_dir}")
    print(f"Streaming log:    {raw_dir / 'streaming_output.txt'}")
    print(f"{'='*70}\n")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Step0 - GreenMaster daily offer PDF extraction (AI-ONLY)")
    p.add_argument("--sender", default="sales@green-master.eu", help="Sender email address to search")
    p.add_argument("--newer_than_days", type=int, default=14, help="Search window in days")
    p.add_argument("--out_root", default="out_step0_daily_offer", help="Output root directory")
    p.add_argument("--credentials", default="credentials.json", help="Path to Gmail OAuth credentials.json")
    p.add_argument("--token", default="token.json", help="Path to Gmail token.json")
    p.add_argument("--ai-model",
                   default="gpt-4o",
                   choices=["gpt-4o", "gpt-4o-mini", "o1-preview", "o1-mini"],
                   help="AI model for vision extraction (o1-preview = most powerful reasoning, gpt-4o = balanced)")
    return p


def main() -> None:
    args = build_argparser().parse_args()

    # Check required dependencies
    try:
        import pdf2image  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        print("ERROR: Step0 requires pdf2image and Pillow")
        print("Install with: pip install pdf2image Pillow")
        print("Also ensure poppler is installed (for pdf2image)")
        sys.exit(1)

    # Check for OpenAI API key
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not found in .env file")
        print(f"OpenAI models require OpenAI API key")
        sys.exit(1)

    run(
        sender=args.sender,
        newer_than_days=args.newer_than_days,
        out_root=Path(args.out_root),
        credentials_path=Path(args.credentials),
        token_path=Path(args.token),
        ai_model=args.ai_model,
    )


if __name__ == "__main__":
    main()
