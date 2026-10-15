# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# create a .env with MONGO_URI / MONGO_DB, e.g.:
#   MONGO_URI="mongodb://localhost:27017"
#   MONGO_DB="attendance_db"
docker run -d --name mongo -p 27017:27017 mongo:7   # or use MongoDB Atlas (M0 free tier) instead
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## What's missing

`GET /admin/explain/{endpoint}` is not implemented due to time constraing. Every other endpoint in `openapi.yaml` is: employee create/list,
punch-in, punch-out, attendance list, regularize, and all four analytics endpoints (employee monthly, department
summary, late leaderboard, department trend). `DECISIONS.md`'s indexing answer is based on reasoning about the
queries rather than on `explain` output, since the endpoint that would prove it doesn't exist here.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
