# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the single-file FastAPI application. All code (12 endpoints, models, aggregation pipelines, HTML UI) must live in this one file. Run with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in and fix.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is much larger and contains cases they do not show.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit

A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.

No `.env`, no secrets, no Dockerfile.

**Extra files strictly against the "single file" rule:**
- `frontend/` — Flask app with embedded 1,200-line HTML/dashboard using CDN fonts/chart.js
- `test_server.py` — hard-coded `E:\` paths, not a valid test runner
- Any standalone dashboard or UI separate from `app/main.py`

All functionality including the interactive Web UI is served directly from `app.main:app` on port 8000.
