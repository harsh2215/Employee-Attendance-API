"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
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


def compute_overtime(punch_out_ist: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """R3: compute overtime as is mentioned in the R3"""
    h, m = map(int, shift_end.split(":"))
    end_date = attendance_date
    if shift_end <= shift_start:
        end_date = (date.fromisoformat(attendance_date) + timedelta(days=1)).isoformat()
    end = datetime.fromisoformat(end_date).replace(hour=h, minute=m, tzinfo=punch_out_ist.tzinfo)
    minutes = max(0, int((punch_out_ist - end).total_seconds() // 60))
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half day when the rounded work_hours is below 4.50."""
    return work_hours < 4.50


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """A naive datetime (what PyMongo returns by default) is treated as UTC."""
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def round_half_up(value: float, decimals: int = 2) -> float:
    """R8: reported numbers are rounded half-up, not Python's round()."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[str, str]:
    """'YYYY-MM' -> (first day, last day) as 'YYYY-MM-DD' strings."""
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def count_working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri only, no holiday calendar; counted from joined_on for a mid-month joiner."""
    month_start_str, month_end_str = month_bounds(month)
    month_end = date.fromisoformat(month_end_str)
    start = max(date.fromisoformat(month_start_str), date.fromisoformat(joined_on))
    if start > month_end:
        return 0
    days = (month_end - start).days + 1
    return sum(1 for offset in range(days) if (start + timedelta(days=offset)).weekday() < 5)


def to_epoch_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds. A naive value (what PyMongo returns by default)
    is treated as UTC, since that's what BSON dates actually store."""
    return int(as_utc(dt).timestamp() * 1000)


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


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


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


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    punched_at_utc = ms_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at_utc}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        if db.attendance_logs.find_one({"emp_code": body.emp_code}):
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found for this employee")

    if record["punch_out"] is not None:
        raise HTTPException(409, "already punched out")

    punch_in_utc = as_utc(record["punch_in"])
    if punched_at_utc == punch_in_utc or punched_at_utc - punch_in_utc > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    updates = {
        "punch_out": punched_at_utc,
        "work_hours": compute_work_hours(punch_in_utc, punched_at_utc),
        "overtime_minutes": compute_overtime(to_ist(punched_at_utc), emp["shift_start"], emp["shift_end"], record["date"]),
    }
    updates["half_day"] = compute_half_day(updates["work_hours"])

    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": updates})
    if result.modified_count == 0:
        raise HTTPException(409, "already punched out")

    record.update(updates)
    return serialize_attendance(record)


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


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    body: RegularizeIn,
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status if body.status is not None else record["status"]
    clearing = new_status in ("ABSENT", "LEAVE")

    if clearing and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "cannot set punch_in/punch_out when status is ABSENT or LEAVE")

    if clearing:
        new_punch_in, new_punch_out = None, None
    else:
        new_punch_in = ms_to_utc(body.punch_in) if body.punch_in is not None else as_utc(record.get("punch_in"))
        new_punch_out = ms_to_utc(body.punch_out) if body.punch_out is not None else as_utc(record.get("punch_out"))
        if new_punch_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if resolve_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on this record's attendance date")
        if new_punch_out is not None and (new_punch_out <= new_punch_in or new_punch_out - new_punch_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours of it")

    if clearing:
        new_late, new_overtime, new_work_hours, new_half_day = 0, 0, None, False
    else:
        new_late = compute_late_minutes(to_ist(new_punch_in), emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime(to_ist(new_punch_out), emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours, new_overtime, new_half_day = None, 0, False

    old_values = {
        "status": record["status"],
        "punch_in": as_utc(record.get("punch_in")),
        "punch_out": as_utc(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }
    changes = {field: {"from": old, "to": new_values[field]} for field, old in old_values.items() if old != new_values[field]}
    if not changes:
        raise HTTPException(422, "request does not change anything")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    history_len = len(record.get("history", []))
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]}},
        {"$set": new_values, "$push": {"history": history_entry}},
    )
    if result.modified_count == 0:
        raise HTTPException(409, "record was modified concurrently, retry")

    record.update(new_values)
    record["history"] = record.get("history", []) + [history_entry]
    return serialize_attendance(record)


MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    month_start, month_end = month_bounds(month)
    working_days = count_working_days(month, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$addFields": {"weekday": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, {"$lte": ["$weekday", 5]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    result = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    present_days = result.get("present_days", 0)

    attendance_pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    month_start, month_end = month_bounds(month)
    dept_filter = {"department": department} if department else {}

    headcount_pipeline = [
        {"$match": {"joined_on": {"$lte": month_end}, **dept_filter}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
    ]
    headcount_by_dept = {row["_id"]: row["headcount"] for row in db.employees.aggregate(headcount_pipeline)}

    log_pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": month_end}, **{f"emp.{k}": v for k, v in dept_filter.items()}}},
        {"$addFields": {"weekday": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": "$emp.department",
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, {"$lte": ["$weekday", 5]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, {"$ne": ["$work_hours", None]}]},
                "$work_hours", 0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, {"$ne": ["$work_hours", None]}]},
                1, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    stats_by_dept = {row["_id"]: row for row in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept in sorted(headcount_by_dept):
        stats = stats_by_dept.get(dept, {})
        work_hours_count = stats.get("work_hours_count", 0)
        avg_work_hours = (
            round_half_up(stats["work_hours_sum"] / work_hours_count, 2) if work_hours_count > 0 else None
        )
        items.append({
            "department": dept,
            "headcount": headcount_by_dept[dept],
            "present_days": stats.get("present_days", 0),
            "avg_work_hours": avg_work_hours,
            "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0),
            "leave_count": stats.get("leave_count", 0),
            "on_duty_count": stats.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    from_date = datetime.fromisoformat(from_)
    to_date = datetime.fromisoformat(to)
    if to_date < from_date:
        raise HTTPException(422, "to must not be before from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "the range must not exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")

    pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENCE_STATUSES]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"date": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$project": {"_id": 0, "date": 1, "present_count": 1, "late_count": 1}},
        {"$densify": {
            "field": "date",
            "range": {"step": 1, "unit": "day", "bounds": [from_date, to_date + timedelta(days=1)]},
        }},
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}},
        },
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]
    rows = db.attendance_logs.aggregate(pipeline)

    items = [{
        "date": row["date"].strftime("%Y-%m-%d"),
        "is_working_day": row["is_working_day"],
        "headcount": row["headcount"],
        "present_count": row["present_count"],
        "late_count": row["late_count"],
        "attendance_rate": round_half_up(row["attendance_rate"], 4) if row["attendance_rate"] is not None else None,
        "moving_avg_7d": round_half_up(row["moving_avg_7d"], 4) if row["moving_avg_7d"] is not None else None,
    } for row in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
