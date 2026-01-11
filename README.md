Project: Order Automation — Multi-step LLM pipeline
===================================================

Overview
--------
Automated order processing system with separate steps for email extraction and AI validation.

Architecture
-----------
The project is organized into independent steps that can be run separately:

- **Step 1** (step1_email_reader.py): Reads Gmail emails and extracts order data
- **Step 2** (step2_ai_order_validation.py): Classifies orders using LLM
- **AI package** (ai/): Reusable AI utilities for all LLM-powered steps

Structure
---------

project/
├── step1_email_reader.py        # Step 1: Gmail email extraction
├── step2_ai_order_validation.py # Step 2: LLM order classification
├── ai/                          # Reusable AI utilities
│   ├── __init__.py
│   ├── core.py                  # High-level LLM pipeline functions
│   ├── llm_client.py            # OpenAI-compatible API client
│   ├── prompt_builder.py        # Legacy prompt builder
│   └── schemas.py               # Pydantic validation schemas
├── prompts/                     # LLM prompt templates
│   └── order_classification.md
├── out_step1_email_inputs/      # Step 1 output (extracted emails)
│   └── YYYYMMDD_HHMMSS/         # Batch timestamp folder
│       └── *.json               # One JSON per email
├── out_step2_ai_order_validation/  # Step 2 output (classifications)
│   └── YYYYMMDD_HHMMSS/
│       ├── raw/                 # Request, response, parsed (all files)
│       ├── order/               # Confirmed orders
│       ├── not_order/           # Non-orders (marketing, etc.)
│       └── needs_manual/        # Requires human review
├── credentials.json             # Gmail OAuth credentials
├── token.json                   # Gmail OAuth token (auto-generated)
└── .env.example                 # Environment config template

Usage
-----

### Setup

1. Create `.env` from `.env.example` and set `OPENAI_API_KEY`
2. Install dependencies:
   ```bash
   pip install requests pydantic beautifulsoup4 google-api-python-client openpyxl pyexcel pyexcel-xls python-dotenv
   ```
3. Set up Gmail OAuth:
   - Download OAuth credentials from Google Cloud Console
   - Save as `credentials.json` in project root

### Running the Pipeline

**Option A: Run both steps together (legacy)**
```bash
python step1_email_reader.py
```
This will:
- Extract emails from Gmail → `out_step1_email_inputs/TIMESTAMP/`
- Note: Step 2 must be run separately now

**Option B: Run steps independently (recommended)**

Step 1 - Extract emails:
```bash
python step1_email_reader.py
```
- Reads unread emails from Gmail Primary tab (last 1 day)
- Extracts Excel attachments in-memory (never saves to disk)
- Saves email data to `out_step1_email_inputs/TIMESTAMP/`

Step 2 - Classify with AI:
```bash
# Process latest batch
python step2_ai_order_validation.py

# Process specific batch
python step2_ai_order_validation.py --batch 20260110_130000
```
- Reads emails from step1 output
- Calls LLM for classification
- Saves results to `out_step2_order_classification/TIMESTAMP/`

AI Package (ai/)
----------------

Reusable AI utilities for building LLM-powered steps:

### Core Functions (ai/core.py)
- `load_prompt_template()` - Load prompts from prompts/ directory
- `build_chat_messages()` - Build chat messages from system prompt + user data
- `call_llm_and_save()` - Call LLM API and save request/response
- `extract_assistant_content()` - Extract text from API response
- `parse_and_save_response()` - Parse and validate LLM output with Pydantic
- `save_json_atomic()` - Atomically save JSON files

### Example Usage
```python
from ai.core import load_prompt_template, build_chat_messages, call_llm_and_save

# Load prompt
system_prompt = load_prompt_template("order_classification.md")

# Build messages
messages = build_chat_messages(system_prompt, {"body_text": "..."})

# Call LLM
response = call_llm_and_save(
    messages=messages,
    output_dir="output/raw",
    base_filename="email_123",
    model="gpt-4o-mini"
)
```

Model Output Format
-------------------

The LLM returns structured JSON with multiple orders:

```json
{
  "classification": "order" | "not_order" | "needs_manual",
  "confidence": 0.0-1.0,
  "orders": [
    {
      "vip_number": "12345",
      "items": [
        {"product_code": "3000950", "qty": 2}
      ],
      "order_status": "order" | "needs_manual",
      "reasons": ["explanation..."],
      "questions_for_human": ["question..."]
    }
  ]
}
```

**Classification Logic:**
- `order`: All orders are valid and actionable
- `not_order`: No orders found (marketing, newsletter, etc.)
- `needs_manual`: At least one order requires human review

**Auto Mark as Read:**
Emails are marked as read when:
- `classification != "needs_manual"` AND
- `confidence >= 0.85`

Notes
-----
- **Separate steps**: Step 1 extracts emails, Step 2 classifies them
- **In-memory processing**: Excel attachments are never saved to disk
- **Multi-order support**: One email can contain multiple orders (same or different VIP numbers)
- **Batch organization**: Each run creates a timestamped folder
- **Reusable AI layer**: `ai/` package can be used for future steps (step3, step4, etc.)
- **Non-Excel attachments**: PDFs, images, etc. are skipped (metadata recorded only)

Gmail Query
-----------
Default: `is:unread newer_than:1d -in:spam -in:trash category:primary`

- Only unread emails
- From last 1 day
- Primary tab only
- Excludes spam and trash

Modify `GMAIL_QUERY` in step1_email_reader.py to change this.
