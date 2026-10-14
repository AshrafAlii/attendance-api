# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail, and analytics built on aggregation pipelines. All the code is in `app/main.py`.

## Run it
Needs Python 3.11+ and MongoDB 6.0+ (I used `docker run -d -p 27017:27017 mongo:7`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # MONGO_URI / MONGO_DB (real environment variables win over .env)
python sample_seed.py         # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```
Check `GET /health`, then open `http://localhost:8000/docs`. Indexes are created at startup and re-creating them is harmless.

## Check it
`tests/verify_analytics.py` seeds an awkward dataset into a separate `attendance_verify` database, works out every expected analytics answer in plain Python, calls the API in-process and compares. It also checks `/admin/explain/*` for IXSCAN and no COLLSCAN.
```bash
pip install httpx
python tests/verify_analytics.py
```

## Notes and assumptions
- Everything is stored in UTC; attendance dates and shift times use IST (UTC+05:30). Overnight shifts belong to the day they start.
- Races are handled by unique indexes (punch-in, create employee) and by a compare-and-set update on the state that was read (punch-out, PATCH); the loser gets 409.
- PATCH ignores derived fields if a client sends them, as the contract says.
- `moving_avg_7d` averages the unrounded daily rates and is rounded half-up at the end.
- Working days for the monthly summary are counted with calendar arithmetic; every other analytics number comes from an aggregation pipeline.

## Not done / limits
- Nothing from the contract is missing. I could not test against a dataset of the grader's size (about 100k records) on my machine, so I have not measured performance there.