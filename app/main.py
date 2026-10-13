"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "emp-db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_utc(ms: int) -> datetime:
    """Epoch milliseconds to UTC datetime. R1: truncated to whole seconds before
    any calculation, so the sub-second remainder of ms is dropped, not rounded."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def to_ist(dt: datetime) -> datetime:
    """Any datetime -> IST wall-clock time. A naive value is treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def resolve_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: a record's date is the IST calendar date of punch_in, except for an overnight
    shift (shift_end <= shift_start): if the IST punch time is before shift_end, it
    belongs to the shift that started the day before."""
    punch_ist = to_ist(punch_in_utc)
    is_overnight = shift_end <= shift_start
    if is_overnight and punch_ist.strftime("%H:%M") < shift_end:
        punch_ist -= timedelta(days=1)
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start."""
    h, m = map(int, shift_start.split(":"))
    start = datetime.fromisoformat(attendance_date).replace(hour=h, minute=m, tzinfo=punch_in_ist.tzinfo)
    diff_seconds = (punch_in_ist - start).total_seconds()
    if diff_seconds <= 600:
        return 0
    return int(diff_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: rounded to 2 decimals half-up, not Python's round() which rounds half-to-even."""
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds / 3600))
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    """R3: compute overtime as is mentioned in the R3"""
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m)
    minutes = max(0, int((punch_out - end).total_seconds() // 60))
    return minutes if minutes >= 30 else 0


def to_epoch_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds. A naive value (what PyMongo returns by default)
    is treated as UTC, since that's what BSON dates actually store."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def serialize_attendance(doc: dict) -> dict:
    """Serialize Mongo attendance document for the API: drop _id, turn punch_in/punch_out
    (and history's dates) into epoch-ms, and fill in the defaults legacy records omit."""
    doc.pop("_id", None)
    doc["punch_in"] = to_epoch_ms(doc["punch_in"]) if doc.get("punch_in") else None
    doc["punch_out"] = to_epoch_ms(doc["punch_out"]) if doc.get("punch_out") else None
    doc["half_day"] = doc.get("half_day", False)
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            frm, to = change.get("from"), change.get("to")
            changes[field] = {
                "from": to_epoch_ms(frm) if isinstance(frm, datetime) else frm,
                "to": to_epoch_ms(to) if isinstance(to, datetime) else to,
            }
        history.append({
            "at": to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    doc["history"] = history
    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


EMP_CODE_PATTERN = r"^EMP\d{4,6}$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_PATTERN)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_a_real_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

    @field_validator("shift_end")
    @classmethod
    def shift_start_and_end_must_differ(cls, v: str, info) -> str:
        if v == info.data.get("shift_start"):
            raise ValueError("shift_start and shift_end must differ")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = []
    for doc in db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size):
        doc["created_at"] = to_epoch_ms(doc["created_at"])
        items.append(doc)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    punch_utc = ms_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    punch_ist = to_ist(punch_utc)
    attendance_date = resolve_attendance_date(punch_utc, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_ist, emp["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
