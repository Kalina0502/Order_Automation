#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
STEP0 - GreenMaster Daily Offer PDF ingestion

What it does:
- Searches Gmail for the latest email FROM sales@green-master.eu (optionally within N days)
- Extracts the PDF URL from HTML/text (supports redirects)
- Downloads PDF
- Extracts text:
    - First tries embedded PDF text (fast)
    - If empty and --ocr is enabled, does OCR via pdf2image + pytesseract (slower, but works for image-only PDFs)
- Saves:
    out_step0_daily_offer/<RUN_ID>/
        meta.json
        pdf_raw/daily_offer_<date>_<email_id>.pdf
        extracted/text_full.txt
        extracted/text_by_page.json
        extracted/ocr_used.txt (if OCR)

Requirements (minimal):
- google-api-python-client
- google-auth-httplib2
- google-auth-oauthlib
- requests
- pypdf

Optional OCR:
- pdf2image + poppler installed
- pytesseract + Tesseract installed

If you already have Step1 Gmail OAuth working (credentials.json + token.json),
this Step0 should work out of the box.
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


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# Gmail deps
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request


SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # Same as step1 to reuse token.json


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
    # Gmail returns urlsafe base64 without padding
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


# ----------------------------
# Gmail auth
# ----------------------------

def get_gmail_service(credentials_path: Path, token_path: Path) -> Any:
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


# ----------------------------
# Gmail read helpers
# ----------------------------

def gmail_search_latest_message_id(service: Any, sender: str, newer_than_days: int) -> Optional[str]:
    # Example query: from:sales@green-master.eu newer_than:7d
    q = f"from:{sender}"
    if newer_than_days > 0:
        q += f" newer_than:{newer_than_days}d"

    resp = service.users().messages().list(userId="me", q=q, maxResults=5).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        return None
    # Gmail returns newest first in most cases; but not guaranteed by doc.
    return msgs[0]["id"]


def gmail_get_message(service: Any, msg_id: str) -> Dict[str, Any]:
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def gmail_headers(payload: Dict[str, Any]) -> Dict[str, str]:
    headers = {}
    for h in payload.get("headers", []):
        headers[h.get("name", "").lower()] = h.get("value", "")
    return headers


def gmail_extract_bodies(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (html, text) from Gmail payload parts.
    """
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
    """
    Returns best candidate URLs that likely lead to a PDF.
    Priority:
      1) direct .pdf links
      2) "files.green-master" links (often redirect to pdf)
      3) links containing pdf-ish hints (download, brochure, offer, 01_2026, etc.)
    Excludes CSS/fonts/images.
    """
    if not html_or_text:
        return []

    direct = PDF_URL_RE.findall(html_or_text)
    all_urls = URL_RE.findall(html_or_text)

    # normalize + de-dup preserve order
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

    # filters: remove obvious non-pdf assets
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

    # scoring for non-direct urls
    def score(u: str) -> int:
        ul = u.lower()
        s = 0
        if ul.endswith(".pdf"):
            s += 100
        if "files.green-master" in ul or "green-master.eu" in ul:
            s += 30
        if any(k in ul for k in ["brochure", "offer", "офер", "брошур", "pdf", "download"]):
            s += 20
        if re.search(r"/\d{2}_\d{4}/", ul):  # like /01_2026/
            s += 10
        return s

    # merge: direct first, then best scored others
    others = sorted([u for u in all_urls if u not in direct], key=score, reverse=True)
    candidates = direct + others

    return candidates[:15]


def resolve_to_pdf_url(url: str, timeout: int = 60) -> str:
    """
    Follow redirects and ensure final URL looks like a PDF or content-type is PDF.
    Increased timeout to 60s for slow tracking/redirect links.
    """
    # HEAD first
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        ct = (r.headers.get("Content-Type") or "").lower()
        final = r.url
        if "pdf" in ct or final.lower().endswith(".pdf"):
            return final
    except Exception:
        pass

    # fallback GET
    r = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
    ct = (r.headers.get("Content-Type") or "").lower()
    final = r.url
    if "pdf" in ct or final.lower().endswith(".pdf"):
        return final
    # some servers return octet-stream
    if "application/octet-stream" in ct:
        return final
    return final


def download_pdf(pdf_url: str, out_path: Path, timeout: int = 90) -> str:
    """
    Downloads a URL that SHOULD point to a PDF.
    If response is HTML (e.g., preview/redirect page), tries to extract a real .pdf link from it and downloads again.
    Returns the final resolved pdf url used for the successful download.
    """
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

    # Case 1: already PDF
    if looks_like_pdf(r.content) or ("application/pdf" in ct):
        out_path.write_bytes(r.content)
        return r.url

    # Case 2: HTML wrapper -> try to find the real pdf url inside
    if looks_like_html(content) or ("text/html" in ct):
        html = r.text

        # Try to extract direct .pdf links
        pdf_links = extract_pdf_urls(html)  # uses your improved extractor
        # prefer files.green-master links
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

        # If we reached here, we did not find a pdf
        # Save the html for debugging
        debug_html = out_path.with_suffix(".html")
        debug_html.write_text(html, encoding="utf-8", errors="ignore")
        raise RuntimeError(
            f"Downloaded content is HTML, not PDF. Saved debug HTML to: {debug_html}"
        )

    # Case 3: unknown binary
    # Save anyway for debugging, but raise
    out_path.write_bytes(r.content)
    raise RuntimeError(
        f"Downloaded content is not a PDF (Content-Type={ct}). "
        f"First bytes={content[:20]!r}"
    )



# ----------------------------
# PDF text extraction
# ----------------------------

def extract_pdf_text_layer(pdf_path: Path) -> Tuple[str, List[str]]:
    reader = PdfReader(str(pdf_path))
    pages_text: List[str] = []
    for p in reader.pages:
        t = p.extract_text() or ""
        pages_text.append(t)
    full = "\n\n".join(pages_text).strip()
    return full, pages_text


def ocr_pdf_to_text(pdf_path: Path) -> Tuple[str, List[str]]:
    """
    OCR fallback for image-only PDFs.
    Needs:
      pip install pdf2image pytesseract
    And system deps:
      - poppler
      - tesseract
    """
    from pdf2image import convert_from_path
    import pytesseract

    images = convert_from_path(str(pdf_path), dpi=300)
    pages_text: List[str] = []
    for img in images:
        # Bulgarian + English; adjust as needed
        t = pytesseract.image_to_string(img, lang="bul+eng")
        pages_text.append(t)
    full = "\n\n".join(pages_text).strip()
    return full, pages_text


def ai_vision_extract_text(pdf_path: Path, model: str = "gpt-4o-mini") -> Tuple[str, List[str]]:
    """
    Use AI Vision (GPT-4o) to extract text from PDF by reading it as images.

    This is the BEST option for PDFs with:
    - Custom font encoding (like /uni0412 instead of Cyrillic)
    - Complex layouts
    - Mixed Bulgarian/English text

    Needs: pip install pdf2image Pillow

    Returns: (full_text, pages_text_list)
    """
    from pdf2image import convert_from_path
    import base64
    import io
    import os

    # Check for API key
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not found in .env file. AI Vision requires OpenAI API key.")

    print(f"Converting PDF to images...")
    images = convert_from_path(str(pdf_path), dpi=200)

    print(f"Extracted {len(images)} pages as images")

    # Convert to base64
    images_b64 = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        img_bytes = buf.getvalue()
        images_b64.append(base64.b64encode(img_bytes).decode('utf-8'))

    # Process in batches (max 10 images per request to avoid token limits)
    batch_size = 10
    all_pages_text = []

    for batch_start in range(0, len(images_b64), batch_size):
        batch_end = min(batch_start + batch_size, len(images_b64))
        batch = images_b64[batch_start:batch_end]

        print(f"Processing pages {batch_start+1}-{batch_end} with {model}...")
        messages = [
    {
        "role": "system",
        "content": (
            "You are a strict OCR and transcription engine. "
            "Your ONLY task is to transcribe the visible text from the provided image EXACTLY as it appears. "

            "ABSOLUTE RULES (NO EXCEPTIONS): "
            "Output PLAIN TEXT ONLY. "
            "DO NOT summarize. "
            "DO NOT rephrase. "
            "DO NOT interpret. "
            "DO NOT translate. "
            "DO NOT explain. "
            "DO NOT fix grammar. "
            "DO NOT guess missing text. "
            "DO NOT merge or split lines. "
            "DO NOT reorder content. "
            "DO NOT normalize numbers. "
            "DO NOT normalize prices or currencies. "
            "DO NOT change punctuation. "
            "DO NOT intentionally change spacing. "

            "NUMBERS & PRICES (CRITICAL): "
            "ALL numbers must be copied EXACTLY as shown. "
            "Preserve decimal separators (comma vs dot). "
            "Preserve currency symbols (€, лв., %, etc.). "
            "Preserve product codes exactly, including leading zeros. "
            "Preserve quantities, limits, dates, and conditions EXACTLY. "

            "TEXT FIDELITY: "
            "Keep Bulgarian text in Cyrillic. "
            "Preserve line breaks as seen. "
            "Preserve capitalization. "
            "Preserve lists, spacing, and layout as closely as possible. "

            "IF TEXT IS UNCLEAR OR CUT OFF: "
            "Transcribe only what is visible. "
            "If part of a word or line is missing, write the visible part and append '[…]'. "
            "NEVER invent missing characters or words. "

            "FINAL CHECK BEFORE ANSWERING: "
            "Verify that EVERY visible number, price, product code, date, and percentage "
            "from the image is present in the output. "
            "If anything is missing or altered, the output is INVALID. "
            "Return the FULL transcription and NOTHING ELSE."
        )
    },
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "Transcribe ALL text from this single-page daily offer image exactly as shown."
            },
            *[
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{img}"
                    }
                }
                for img in batch
            ]
        ]
    }
]

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 16000,
                "temperature": 0.1,
            },
            timeout=180,
        )
        response.raise_for_status()

        result = response.json()
        batch_text = result["choices"][0]["message"]["content"]

        # Split by pages if model provided page markers, otherwise treat as one chunk
        all_pages_text.append(batch_text)

    # Combine all batches
    full_text = "\n\n=== NEW BATCH ===\n\n".join(all_pages_text)

    # For pages_text, just split by batch (not perfect but workable)
    pages_text = all_pages_text

    return full_text, pages_text


# ----------------------------
# Main
# ----------------------------

def run(
    sender: str,
    newer_than_days: int,
    out_root: Path,
    credentials_path: Path,
    token_path: Path,
    enable_ocr: bool,
    use_ai_vision: bool = False,
    ai_model: str = "gpt-4o-mini",
) -> None:
    run_id = now_run_id()
    out_dir = out_root / run_id
    pdf_dir = out_dir / "pdf_raw"
    ext_dir = out_dir / "extracted"
    ensure_dir(pdf_dir)
    ensure_dir(ext_dir)

    service = get_gmail_service(credentials_path, token_path)
    msg_id = gmail_search_latest_message_id(service, sender, newer_than_days)
    if not msg_id:
        raise SystemExit(f"No emails found from {sender} (newer_than_days={newer_than_days}).")

    msg = gmail_get_message(service, msg_id)
    payload = msg.get("payload", {}) or {}
    headers = gmail_headers(payload)
    subject = headers.get("subject", "")
    date_hdr = headers.get("date", "")
    internal_ts_ms = int(msg.get("internalDate", "0") or "0")

    html, text = gmail_extract_bodies(payload)
    content_for_url = html or text or ""

    urls = extract_pdf_urls(content_for_url)

    # If no direct .pdf links found, try to catch any links that might redirect (basic fallback)
    if not urls and html:
        # crude href extraction
        hrefs = re.findall(r'href=["\'](.*?)["\']', html, flags=re.IGNORECASE)
        # keep only http(s)
        hrefs = [h for h in hrefs if h.startswith("http://") or h.startswith("https://")]
        # try candidates that look like files.green-master or contain date-like tokens
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
        raise SystemExit("No PDF link found in the email body. Check meta.json for details.")

    # pick first candidate, resolve redirects
    pdf_url = None
    picked_candidate = None

    for cand in urls:
        try:
            print(f"Trying URL: {cand[:80]}...")
            resolved = resolve_to_pdf_url(cand)

            # ACCEPT only if it looks like a PDF endpoint (not just a site landing page)
            if resolved.lower().endswith(".pdf") or "files.green-master" in resolved.lower():
                pdf_url = resolved
                picked_candidate = cand
                print(f"  ✓ Resolved to (PDF-like): {resolved}")
                break

            # Otherwise keep scanning (many links redirect to homepage first)
            print(f"  · Not PDF-like after resolve: {resolved}")

        except Exception as e:
            print(f"  ✗ Failed to resolve: {e}")
            continue


    if not pdf_url:
        # fallback to first and let it fail with meta
        picked_candidate = urls[0]
        try:
            pdf_url = resolve_to_pdf_url(picked_candidate)
        except Exception as e:
            raise SystemExit(f"Could not resolve any PDF URL. Last error: {e}")

    # download
    safe_date = datetime.fromtimestamp(internal_ts_ms / 1000).strftime("%Y%m%d") if internal_ts_ms else "unknown_date"
    pdf_path = pdf_dir / f"daily_offer_{safe_date}_{msg_id}.pdf"
    final_pdf_url = download_pdf(pdf_url, pdf_path)


    pdf_hash = sha256_file(pdf_path)

    # Choose extraction method
    extraction_method = "pdf_text_layer"
    ocr_used = False
    ai_vision_used = False

    if use_ai_vision:
        # Priority 1: AI Vision (best for complex PDFs with encoding issues)
        print(f"\nUsing AI Vision ({ai_model}) to extract text...")
        try:
            text_full, pages_text = ai_vision_extract_text(pdf_path, model=ai_model)
            ai_vision_used = True
            extraction_method = f"ai_vision_{ai_model}"
            (ext_dir / "ai_vision_used.txt").write_text(f"AI_VISION_USED=1\nMODEL={ai_model}\n", encoding="utf-8")
        except Exception as e:
            print(f"AI Vision failed: {e}")
            (ext_dir / "ai_vision_used.txt").write_text(f"AI_VISION_FAILED: {e}\n", encoding="utf-8")
            # Fallback to regular extraction
            text_full, pages_text = extract_pdf_text_layer(pdf_path)
    else:
        # Priority 2: Regular PDF text layer extraction
        text_full, pages_text = extract_pdf_text_layer(pdf_path)

        # Priority 3: OCR fallback if text is almost empty
        if enable_ocr and len(re.sub(r"\s+", "", text_full)) < 30:
            print("\nPDF text layer is empty, using OCR fallback...")
            try:
                text_full, pages_text = ocr_pdf_to_text(pdf_path)
                ocr_used = True
                extraction_method = "ocr"
                (ext_dir / "ocr_used.txt").write_text("OCR_USED=1\n", encoding="utf-8")
            except Exception as e:
                print(f"OCR failed: {e}")
                (ext_dir / "ocr_used.txt").write_text(f"OCR_FAILED: {e}\n", encoding="utf-8")

    # save extracted
    (ext_dir / "text_full.txt").write_text(text_full, encoding="utf-8")
    write_json(ext_dir / "text_by_page.json", {
        "email_id": msg_id,
        "pages": [{"page": i + 1, "text": t} for i, t in enumerate(pages_text)]
    })

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
            "ocr_used": ocr_used,
            "ai_vision_used": ai_vision_used,
        },
        "outputs": {
            "meta_json": str(out_dir / "meta.json"),
            "pdf_path": str(pdf_path),
            "text_full": str(ext_dir / "text_full.txt"),
            "text_by_page": str(ext_dir / "text_by_page.json"),
        }
    }
    write_json(out_dir / "meta.json", meta)

    print("\n" + "=" * 70)
    print("STEP0 - DAILY OFFER PDF INGESTION COMPLETE")
    print("=" * 70)
    print(f"Email id:         {msg_id}")
    print(f"Subject:          {subject}")
    print(f"PDF:              {pdf_path}")
    print(f"Extraction:       {extraction_method}")
    print(f"Text length:      {len(text_full):,} chars")
    print(f"AI Vision used:   {ai_vision_used}")
    print(f"OCR used:         {ocr_used}")
    print(f"Output dir:       {out_dir}")
    print("=" * 70 + "\n")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Step0 - GreenMaster daily offer PDF ingestion")
    p.add_argument("--sender", default="sales@green-master.eu", help="Sender email address to search")
    p.add_argument("--newer_than_days", type=int, default=14, help="Search window in days (Gmail query)")
    p.add_argument("--out_root", default="out_step0_daily_offer", help="Output root directory")
    p.add_argument("--credentials", default="credentials.json", help="Path to Gmail OAuth credentials.json")
    p.add_argument("--token", default="token.json", help="Path to Gmail token.json")
    p.add_argument("--ocr", action="store_true", help="Enable OCR fallback for image-only PDFs (requires Tesseract)")
    p.add_argument("--ai-vision", action="store_true", help="Use AI Vision to extract text (recommended for complex PDFs)")
    p.add_argument("--ai-model", default="gpt-4o-mini", help="AI model for vision extraction (gpt-4o, gpt-4o-mini)")
    return p


def main() -> None:
    args = build_argparser().parse_args()

    # Check dependencies if AI Vision is requested
    if args.ai_vision:
        try:
            import pdf2image  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            print("ERROR: AI Vision requires pdf2image and Pillow")
            print("Install with: pip install pdf2image Pillow")
            print("Also ensure poppler is installed (for pdf2image)")
            sys.exit(1)

    run(
        sender=args.sender,
        newer_than_days=args.newer_than_days,
        out_root=Path(args.out_root),
        credentials_path=Path(args.credentials),
        token_path=Path(args.token),
        enable_ocr=bool(args.ocr),
        use_ai_vision=bool(args.ai_vision),
        ai_model=args.ai_model,
    )


if __name__ == "__main__":
    main()
