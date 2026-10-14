"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded; real environment variables win)
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False by default, so real environment variables take priority

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Idempotent: re-creating an index with the same keys/options is a no-op."""
    # emp_code is the business key. The unique index is what makes duplicate creates race-safe.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # GET /employees?department=X sorted by emp_code; also the first stage of the department
    # trend (match by department).
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code"
    )
    # Headcount (R9) filters on joined_on: first stage of the department summary.
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # One record per (emp_code, date). Unique index = race-safe punch-in, and serves
    # per-employee lookups, punch-out, PATCH and the monthly analytics.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    # GET /attendance default sort (date desc, emp_code asc) and date-range scans.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    # Database trouble is a 503, not an unexplained 500.
    log.error("mongo error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Time / number helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)  # R1: whole seconds


def now_bson() -> datetime:
    """Current time at BSON precision (milliseconds), for created_at / history[].at."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def unprocessable(
    field: Optional[str], msg: str, value: Any = None, where: str = "body"
) -> RequestValidationError:
    """422 with FastAPI's usual validation-error body, for rules a pydantic type can't express."""
    loc = (where,) if field is None else (where, field)
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


MAX_SHIFT_SPAN = timedelta(hours=24)


def check_punch_span(punch_in: datetime, punch_out: datetime, field: str) -> None:
    """punch_out must be after punch_in and at most 24 hours later."""
    if punch_out <= punch_in:
        raise unprocessable(field, "must be after punch_in", dt_to_ms(punch_out))
    if punch_out - punch_in > MAX_SHIFT_SPAN:
        raise unprocessable(field, "must be within 24 hours of punch_in", dt_to_ms(punch_out))


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, UTC)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch ms. Naive values (e.g. from older code) are treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def round_half_up(value, places: int) -> float:
    """R8: half-up rounding (Python's round() is banker's / float-affected)."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts that start the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    if (eh, em) <= (sh, sm) and local.time() < time(eh, em):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end of the shift that belongs to attendance date `date_str` (IST-aware).
    For an overnight shift the end falls on the next calendar day."""
    d = date.fromisoformat(date_str)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if (eh, em) <= (sh, sm):
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# Business rules R2 - R5
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    delta = int((punch_in - shift_start_dt).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals half-up. Returned as Decimal so R5 compares the rounded value."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: floor minutes after shift end, counted only if >= 30."""
    delta = int((punch_out - shift_end_dt).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_derived(
    shift_start: str,
    shift_end: str,
    date_str: str,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> dict:
    """All four derived fields, per the table in DATA_MODEL.md. Also used by punch-out / PATCH."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start_dt, end_dt = shift_bounds(date_str, shift_start, shift_end)
    late = compute_late_minutes(punch_in, start_dt)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(wh),
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, end_dt),
        "half_day": wh < Decimal("4.50"),
    }


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"  # [0-9], not \d: \d also matches non-ASCII digits


def _check_date(v: str) -> str:
    if not _DATE_RE.fullmatch(v):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a valid calendar date")
    return v


DateStr = Annotated[str, AfterValidator(_check_date)]
# Epoch milliseconds only: strict int (rejects floats/strings/bools), and a range that
# rejects seconds-looking values (< 1e11) and absurd futures.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]


def paginate(page: int, page_size: int) -> int:
    return min((page - 1) * page_size, 2**62)  # clamp so a huge page can't overflow BSON int64


# --------------------------------------------------------------------------- #
# Serialisers (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _api_value(doc.get("created_at")),
    }


def serialize_record(doc: dict) -> dict:
    """Missing legacy fields are defaulted: history -> [], half_day -> False, etc. No _id."""
    history = []
    for h in doc.get("history") or []:
        history.append(
            {
                "at": _api_value(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
                    for k, c in (h.get("changes") or {}).items()
                },
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _api_value(doc.get("punch_in")),
        "punch_out": _api_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    """Only these fields can be changed. Derived fields sent by a client are ignored (extra keys)."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_bson()
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)  # total reflects the filter
    cur = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(paginate(page, page_size))
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1}
    )
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **compute_derived(emp["shift_start"], emp["shift_end"], date_str, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


def state_guard(rec: dict) -> dict:
    """Optimistic-concurrency filter: matches the record only if it is still exactly as we read it
    (status, both punch times, and number of history entries). Used as the filter of the atomic
    update, so if anything changed in between, nothing is written and the caller answers 409."""
    n = len(rec.get("history") or [])
    guard: dict = {
        "emp_code": rec["emp_code"],
        "date": rec["date"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),  # None also matches a missing field
        "punch_out": rec.get("punch_out"),
    }
    if n == 0:
        guard["$or"] = [{"history": None}, {"history": {"$size": 0}}]  # missing, null or empty
    else:
        guard["history"] = {"$size": n}
    return guard


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1}
    )
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before the punch-out. Sorting by date (the
    # attendance day) walks the (emp_code, date) index backwards and stops at the first match;
    # this also finds the record of an overnight shift that started the previous day.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_span(rec["punch_in"], ts, "punched_at")

    d = compute_derived(
        emp["shift_start"], emp["shift_end"], rec["date"], rec["status"], rec["punch_in"], ts
    )
    # Atomic compare-and-set: of two simultaneous punch-outs exactly one matches the guard.
    updated = db.attendance_logs.find_one_and_update(
        state_guard(rec),
        {
            "$set": {
                "punch_out": ts,
                "work_hours": d["work_hours"],
                "overtime_minutes": d["overtime_minutes"],
                "half_day": d["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was punched out or changed concurrently")
    return serialize_record(updated)


def plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Validate a correction and work out the new field values and the history `changes`.
    Pure function (no database access)."""
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    status = body.status or old["status"]

    if status in PRESENCE:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise unprocessable("punch_in", f"status {status} requires a punch_in")
        if body.punch_in is not None and (
            attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]
        ):
            raise unprocessable(
                "punch_in", "must stay on the record's attendance date", body.punch_in
            )
        if new_out is not None:
            check_punch_span(new_in, new_out, "punch_out")
    else:  # ABSENT / LEAVE clear both punch times, so none may be supplied
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(
                "punch_in" if body.punch_in is not None else "punch_out",
                f"status {status} has no punch times",
            )
        new_in = new_out = None

    new = {
        "status": status,
        "punch_in": new_in,
        "punch_out": new_out,
        **compute_derived(emp["shift_start"], emp["shift_end"], rec["date"], status, new_in, new_out),
    }
    if all(new[k] == old[k] for k in ("status", "punch_in", "punch_out")):
        raise unprocessable(None, "request changes nothing")
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    # NB: `date` (the path parameter) shadows datetime.date inside this function only.
    emp = db.employees.find_one(
        {"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1}
    )
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    new_fields, changes = plan_regularization(rec, emp, body)
    entry = {"at": now_bson(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # One atomic update: new values + history entry, guarded by the state we read. A concurrent
    # edit makes the guard fail and this request gets 409 instead of overwriting its history.
    updated = db.attendance_logs.find_one_and_update(
        state_guard(rec),
        {"$set": new_fields, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return serialize_record(updated)


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    """Shared by GET /attendance and (later) its explain, so both build the same query."""
    if date_from and date_to and date_from > date_to:  # ISO dates compare correctly as strings
        raise unprocessable("date_from", "date_from must be <= date_to", date_from, where="query")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip(paginate(page, page_size))
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics: every number comes out of an aggregation pipeline
# --------------------------------------------------------------------------- #
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _check_month(v: str) -> str:
    if not _MONTH_RE.fullmatch(v) or int(v[:4]) < 1:
        raise ValueError("must be a month in YYYY-MM format")
    return v


MonthStr = Annotated[str, AfterValidator(_check_month)]
MonthParam = Annotated[MonthStr, Query()]  # required
LimitParam = Annotated[int, Query(ge=1, le=50)]
MAX_TREND_DAYS = 92


def month_bounds(month: str) -> tuple[str, str]:
    """First and last calendar day of 'YYYY-MM' as ISO strings (attendance dates are strings)."""
    year, mon = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def working_days(first: str, last: str, joined_on: str) -> int:
    """R7: Mon-Fri days from max(first, joined_on) to last. Pure calendar arithmetic, no data."""
    start = date.fromisoformat(max(first, joined_on))
    end = date.fromisoformat(last)
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# Reusable expression fragments (all refer to fields of an attendance_logs document).
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun..7=Sat
_IS_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}
_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}  # R6
_DAY_VALUE = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}  # R5: half day = 0.5 (missing = false)
_PRESENT_ANY_DAY = {"$cond": [_IS_PRESENT, _DAY_VALUE, 0]}
_PRESENT_WEEKDAY = {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _DAY_VALUE, 0]}  # R7
_LATE_FLAG = {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
_LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}
# work_hours in whole hundredths, so the department mean is exact (no float drift before R8 rounding)
_HOURS_CENTS = {"$cond": [_HAS_HOURS, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}


def monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},  # IXSCAN uniq_emp_date
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _PRESENT_WEEKDAY},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _LATE_FLAG},
                "total_late_minutes": {"$sum": _LATE_MINUTES},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Starts from `employees` so people with no logs still count (R9); their logs are summed
    inside the $lookup (one index lookup per employee), then rolled up per department."""
    match: dict = {"joined_on": {"$lte": last}}  # joined on or before the end of the month
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",  # uses uniq_emp_date
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": _PRESENT_WEEKDAY},
                            "hours_cents": {"$sum": _HOURS_CENTS},
                            "hours_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
                            "late_count": {"$sum": _LATE_FLAG},
                            "late_minutes": {"$sum": _LATE_MINUTES},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "s",
            }
        },
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},  # keep no-log employees
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
                "hours_cents": {"$sum": {"$ifNull": ["$s.hours_cents", 0]}},
                "hours_n": {"$sum": {"$ifNull": ["$s.hours_n", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
                "late_minutes": {"$sum": {"$ifNull": ["$s.late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: str, last: str, department: Optional[str], limit: int) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},  # IXSCAN date_desc_emp
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_range(d_from: str, d_to: str) -> None:
    if d_to < d_from:
        raise unprocessable("to", "to must be >= from", d_to, where="query")
    if (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1 > MAX_TREND_DAYS:
        raise unprocessable("to", f"range must not exceed {MAX_TREND_DAYS} days", d_to, where="query")


def trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    """One row per calendar day. Every employee of the department contributes small 'event' rows
    (a join row, an anchor row, one row per log); grouping by date and $densify fill the gaps;
    window functions give the running headcount and the 7-day moving average."""
    lower = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    upper = datetime.fromisoformat(d_to).replace(tzinfo=UTC) + timedelta(days=1)  # $densify: upper bound exclusive
    join_date = {"$cond": [{"$gt": ["$joined_on", d_from]}, "$joined_on", d_from]}  # joined before range -> day 1
    return [
        {"$match": {"department": department}},  # IXSCAN dept_emp_code
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",  # uses uniq_emp_date
                "pipeline": [
                    {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                    {"$project": {"_id": 0, "date": 1, "present": _PRESENT_ANY_DAY, "late": _LATE_FLAG}},
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "rows": {
                    "$concatArrays": [
                        [{"date": d_from, "joined": 0, "present": 0, "late": 0}],  # anchor: guarantees a row to densify
                        {
                            "$cond": [
                                {"$lte": ["$joined_on", d_to]},
                                [{"date": join_date, "joined": 1, "present": 0, "late": 0}],
                                [],
                            ]
                        },
                        {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"date": "$$l.date", "joined": 0, "present": "$$l.present", "late": "$$l.late"},
                            }
                        },
                    ]
                },
            }
        },
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {
            "$group": {
                "_id": "$date",
                "present": {"$sum": "$present"},
                "late": {"$sum": "$late"},
                "joined": {"$sum": "$joined"},
            }
        },
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                "present": {"$ifNull": ["$present", 0]},
                "late": {"$ifNull": ["$late", 0]},
                "joined": {"$ifNull": ["$joined", 0]},
            }
        },
        {"$sort": {"d": 1}},
        {
            "$setWindowFields": {  # R9: headcount = employees joined so far
                "sortBy": {"d": 1},
                "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {"$addFields": {"is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$d"}, 6]}]}}},
        {
            "$addFields": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {  # mean of the non-null rates of this row and up to 6 before it ($avg skips null)
                "sortBy": {"d": 1},
                "output": {"moving_avg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present": 1,
                "late": 1,
                "rate": 1,
                "moving_avg": 1,
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    agg = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)), {})
    present = agg.get("present_days", 0)
    days = working_days(first, last, emp["joined_on"])
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": float(present),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(str(present)) / Decimal(days) * 100, 2) if days else None,
    }


@app.get("/analytics/departments/summary")
def departments_summary(month: MonthParam, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(first, last, department)):
        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": float(r["present_days"]),
                # mean over records (not a mean of per-employee means), from exact hundredths
                "avg_work_hours": round_half_up(Decimal(r["hours_cents"]) / Decimal(100) / Decimal(r["hours_n"]), 2)
                if r["hours_n"]
                else None,
                "late_count": r["late_count"],
                "total_late_minutes": r["late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: LimitParam = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, department, limit)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    d_from: Annotated[DateStr, Query(alias="from")],
    d_to: Annotated[DateStr, Query(alias="to")],
):
    trend_range(d_from, d_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, d_from, d_to)):
        rate = r.get("rate")
        moving = r.get("moving_avg")
        items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": int(r["headcount"]),
                "present_count": float(r["present"]),
                "late_count": int(r["late"]),
                # exact ratio of exact values (present is a multiple of 0.5), then half-up (R8)
                "attendance_rate": None if rate is None else round_half_up(Decimal(str(r["present"])) / Decimal(int(r["headcount"])), 4),
                "moving_avg_7d": None if moving is None else round_half_up(moving, 4),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain: the same queries/pipelines the endpoints run, with executionStats
# --------------------------------------------------------------------------- #
def _require(**params) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", k), "msg": "Field required", "input": None} for k in missing]
        )


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: LimitParam = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    d_from: Annotated[Optional[DateStr], Query(alias="from")] = None,
    d_to: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    if endpoint == "attendance_list":
        command: dict = {
            "find": "attendance_logs",
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": paginate(page, page_size),
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs", "pipeline": monthly_pipeline(emp_code, first, last), "cursor": {}}
    elif endpoint == "department_summary":
        _require(month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "employees", "pipeline": summary_pipeline(first, last, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = month_bounds(month)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": leaderboard_pipeline(first, last, department, limit),
            "cursor": {},
        }
    else:  # department_trend
        _require(department=department, **{"from": d_from, "to": d_to})
        trend_range(d_from, d_to)
        command = {"aggregate": "employees", "pipeline": trend_pipeline(department, d_from, d_to), "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))  # raw explain output; BSON types made JSON-safe