Project: Order Automation — LLM classification pipeline

Structure
---------

project/
├── email_reader.py              # Gmail + attachments + orchestration
├── ai/
│   ├── __init__.py
│   ├── llm_client.py            # OpenAI-compatible API calls
│   ├── prompt_builder.py        # Prompt construction only
│   └── schemas.py               # Expected JSON output schema (validation)
├── prompts/                     # Prompt templates used by the AI layer
├── out_step1_email_inputs/      # Extracted email data (organized by batch timestamp)
│   └── YYYYMMDD_HHMMSS/         # Each script run creates a new timestamped folder
├── out_step2_order_classification/  # Model classification results (organized by batch timestamp)
│   └── YYYYMMDD_HHMMSS/
│       ├── raw/                 # All files (request, raw response, parsed)
│       ├── order/               # Parsed JSON for confirmed orders
│       ├── not_order/           # Parsed JSON for non-orders
│       └── needs_manual/        # Parsed JSON requiring manual review
├── credentials.json
├── token.json
└── .env.example

Usage
-----

1. Create `.env` from `.env.example` and set `OPENAI_API_KEY`.
2. Ensure Python deps installed: `requests`, `pydantic`, `beautifulsoup4`, `google-api-python-client`, `openpyxl`, `pyexcel`, `pyexcel-xls`, `python-dotenv`.
3. Run `python email_reader.py` — the script will:
   - Read emails from Gmail (Primary tab only, last 7 days)
   - Extract Excel attachments in-memory (never saves to disk)
   - Save email data to `out_step1_email_inputs/TIMESTAMP/`
   - Call LLM for classification
   - Save results to `out_step2_order_classification/TIMESTAMP/`

Notes
-----
- `email_reader.py` does not perform HTTP calls to the model directly; the AI layer is in `ai/`.
- Prompts are stored in `prompts/` so you can add and version them.
- **No files are saved to disk** - Excel attachments are read in-memory only.
- Non-Excel attachments (PDF, images, etc.) are skipped entirely.
- Emails are NOT marked as read by default. They are marked only when classification != `needs_manual` and confidence >= 0.85.
- Each run creates a new timestamped folder for organization.
