import os
import re
import calendar
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


# ============================================================
# APPLICATION LIFESPAN AND INDEXES
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure MongoDB is responsive
    client.admin.command("ping")

    # employees indexes
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_emp_dept_joined",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_emp_joined_dept",
    )

    # attendance_logs indexes
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_emp_date_desc",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_emp_punchin",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_late_emp",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp_asc",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_emp",
    )

    try:
        yield
    finally:
        client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================
# HELPERS
# ============================================================

def round_half_up(val: float | Decimal | None, places: int = 2) -> float | None:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate_to_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def epoch_ms_to_utc(value: int) -> datetime:
    # Truncate epoch ms to whole second
    truncated_sec = value // 1000
    return datetime.fromtimestamp(truncated_sec, tz=UTC)


def utc_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def validate_epoch_ms(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 100_000_000_000
        or value > 4_102_444_800_000
    ):
        raise ValueError("Must be a valid epoch timestamp in milliseconds")
    return value


def parse_date_str(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD")


def month_bounds(month_str: str) -> tuple[int, int, str, str, datetime, datetime]:
    if not re.fullmatch(r"^\d{4}-(0[1-9]|1[0-2])$", month_str):
        raise HTTPException(status_code=422, detail="Month must be YYYY-MM")
    year = int(month_str[:4])
    m = int(month_str[5:7])
    days_in_month = calendar.monthrange(year, m)[1]

    month_start_str = f"{month_str}-01"
    month_end_str = f"{month_str}-{days_in_month:02d}"

    month_start_dt = datetime(year, m, 1, tzinfo=UTC)
    month_end_dt = datetime(year, m, days_in_month, tzinfo=UTC)

    return year, m, month_start_str, month_end_str, month_start_dt, month_end_dt


def attendance_date_for_punch(punched_at: datetime, employee: dict) -> date:
    """R1: Determine attendance date in IST.

    Overnight shifts (shift_end <= shift_start): punch-in strictly earlier than
    shift_end belongs to the shift starting the previous calendar day.
    """
    local_time = to_ist(punched_at)
    shift_start_h, shift_start_m = map(int, employee["shift_start"].split(":"))
    shift_end_h, shift_end_m = map(int, employee["shift_end"].split(":"))

    shift_start = time(shift_start_h, shift_start_m)
    shift_end = time(shift_end_h, shift_end_m)

    if shift_end <= shift_start and local_time.time() < shift_end:
        return local_time.date() - timedelta(days=1)

    return local_time.date()


def calculate_late_minutes(punch_in: datetime, attendance_date: str, employee: dict) -> int:
    """R2: Grace period 10 minutes.

    Late only if punch-in strictly > 10:00 after shift_start.
    Then whole minutes (floor) since shift_start.
    """
    local_in = to_ist(punch_in)
    shift_start_h, shift_start_m = map(int, employee["shift_start"].split(":"))

    shift_start_dt = datetime.fromisoformat(attendance_date).replace(
        hour=shift_start_h,
        minute=shift_start_m,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )

    seconds_elapsed = (local_in - shift_start_dt).total_seconds()
    if seconds_elapsed <= 600:
        return 0

    return int(seconds_elapsed // 60)


def calculate_overtime_minutes(punch_out: datetime, attendance_date: str, employee: dict) -> int:
    """R3: Whole minutes (floor) from shift_end to punch_out, only if >= 30, else 0."""
    start_date = date.fromisoformat(attendance_date)
    shift_start = employee["shift_start"]
    shift_end = employee["shift_end"]

    end_date = start_date
    if shift_end <= shift_start:
        end_date += timedelta(days=1)

    end_h, end_m = map(int, shift_end.split(":"))
    shift_end_dt = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        end_h,
        end_m,
        0,
        tzinfo=IST,
    ).astimezone(UTC)

    elapsed_seconds = (punch_out - shift_end_dt).total_seconds()
    minutes = int(elapsed_seconds // 60)
    return minutes if minutes >= 30 else 0


def calculate_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    val = Decimal(seconds) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": utc_to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for k, v in entry.get("changes", {}).items():
        c_from = v.get("from")
        c_to = v.get("to")
        if isinstance(c_from, datetime):
            c_from = utc_to_epoch_ms(c_from)
        if isinstance(c_to, datetime):
            c_to = utc_to_epoch_ms(c_to)
        changes[k] = {"from": c_from, "to": c_to}

    return {
        "at": utc_to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# ============================================================
# REQUEST MODELS
# ============================================================

class EmployeeCreate(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.fullmatch(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_format(cls, v: str) -> str:
        if not re.fullmatch(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("Shift time must be 24-hour HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        parse_date_str(v)
        return v

    @model_validator(mode="after")
    def validate_shift_range(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: int | None = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return None if v is None else validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in {"PRESENT", "WFH", "ON_DUTY"}:
            raise ValueError("Status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: int | None = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return None if v is None else validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: str | None = None
    punch_in: int | None = None
    punch_out: int | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise ValueError("Invalid attendance status")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_punch_times(cls, v):
        return None if v is None else validate_epoch_ms(v)


# ============================================================
# ENDPOINTS: SYSTEM & EMPLOYEES
# ============================================================

@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Employee code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    records = list(
        db.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(item) for item in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ============================================================
# ENDPOINTS: ATTENDANCE
# ============================================================

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        now_utc()
        if body.punched_at is None
        else epoch_ms_to_utc(body.punched_at)
    )

    att_date = attendance_date_for_punch(ts, employee).isoformat()
    late = calculate_late_minutes(ts, att_date, employee)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already exists for this employee and date",
        )

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        now_utc()
        if body.punched_at is None
        else epoch_ms_to_utc(body.punched_at)
    )

    # Closes employee's most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = calculate_work_hours(record["punch_in"], ts)
    overtime = calculate_overtime_minutes(ts, record["date"], employee)
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Record was already punched out")

    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code

    if status:
        if status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status

    date_filter = {}
    if date_from:
        parse_date_str(date_from)
        date_filter["$gte"] = date_from
    if date_to:
        parse_date_str(date_to)
        date_filter["$lte"] = date_to

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    if date_filter:
        query["date"] = date_filter

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    records = list(
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(item) for item in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., description="The attendance date, YYYY-MM-DD"),
    body: RegularizeRequest = ...,
):
    parse_date_str(date)

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = record.get("status")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_overtime = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    old_history = record.get("history", [])

    new_status = body.status if body.status is not None else old_status

    if body.punch_in is not None:
        new_in = epoch_ms_to_utc(body.punch_in)
    else:
        new_in = old_in

    if body.punch_out is not None:
        new_out = epoch_ms_to_utc(body.punch_out)
    else:
        new_out = old_out

    # Status rules
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot include punch times",
            )
        new_in = None
        new_out = None
    elif new_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires a punch_in",
        )

    if new_in is not None:
        expected_date = attendance_date_for_punch(new_in, employee).isoformat()
        if expected_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must stay on the record's attendance date",
            )

    if new_out is not None:
        if new_in is None:
            raise HTTPException(status_code=422, detail="Punch-out requires punch-in")
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # Recompute derived fields
    if new_in is None:
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        new_late = calculate_late_minutes(new_in, date, employee)
        if new_out is None:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False
        else:
            new_work_hours = calculate_work_hours(new_in, new_out)
            new_overtime = calculate_overtime_minutes(new_out, date, employee)
            new_half_day = new_work_hours < 4.50

    desired = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    changes = {}
    current_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": old_work_hours,
        "late_minutes": old_late,
        "overtime_minutes": old_overtime,
        "half_day": old_half_day,
    }

    for k, new_v in desired.items():
        curr_v = current_values[k]
        if curr_v != new_v:
            changes[k] = {"from": curr_v, "to": new_v}

    if not changes:
        raise HTTPException(status_code=422, detail="The correction changes nothing")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic compare-and-swap on history to prevent lost updates under concurrency
    filter_cond = {
        "_id": record["_id"],
        "history": old_history if "history" in record else {"$exists": False},
    }

    result = db.attendance_logs.update_one(
        filter_cond,
        {
            "$set": desired,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification detected; please retry",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


# ============================================================
# AGGREGATION PIPELINES (ANALYTICS)
# ============================================================

def build_employee_monthly_pipeline(
    emp_code: str,
    month_start_str: str,
    month_end_str: str,
    month_start_dt: datetime,
    month_end_dt: datetime,
) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$addFields": {
                "_monthStart": month_start_dt,
                "_monthEnd": month_end_dt,
                "_joined": {"$dateFromString": {"dateString": "$joined_on"}},
            }
        },
        {
            "$addFields": {
                "_periodStart": {
                    "$cond": [
                        {"$gt": ["$_joined", "$_monthStart"]},
                        "$_joined",
                        "$_monthStart",
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_logDate": {"$dateFromString": {"dateString": "$date"}}
                        }
                    },
                ],
                "as": "_logs",
            }
        },
        {
            "$addFields": {
                "_workingDayDates": {
                    "$cond": [
                        {"$gt": ["$_periodStart", "$_monthEnd"]},
                        [],
                        {
                            "$map": {
                                "input": {
                                    "$range": [
                                        0,
                                        {
                                            "$add": [
                                                {
                                                    "$dateDiff": {
                                                        "startDate": "$_periodStart",
                                                        "endDate": "$_monthEnd",
                                                        "unit": "day",
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                "as": "offset",
                                "in": {
                                    "$dateAdd": {
                                        "startDate": "$_periodStart",
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    ]
                }
            }
        },
        {
            "$addFields": {
                "_workingDayDates": {
                    "$filter": {
                        "input": "$_workingDayDates",
                        "as": "day",
                        "cond": {
                            "$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]
                        },
                    }
                },
                "_weekdayPresenceLogs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$log.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$in": [
                                        {"$dayOfWeek": "$$log._logDate"},
                                        [2, 3, 4, 5, 6],
                                    ]
                                },
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "working_days": {"$size": "$_workingDayDates"},
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$_weekdayPresenceLogs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
                        }
                    }
                },
            }
        },
    ]


def build_department_summary_pipeline(
    month_start_str: str,
    month_end_str: str,
    department: str | None = None,
) -> list[dict]:
    match_stage = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_logDate": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$l.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": "$$l._logDate"},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$pl.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$l.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "wl",
                            "in": "$$wl.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$l.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month_start_str: str,
    month_end_str: str,
    limit: int = 10,
    department: str | None = None,
) -> list[dict]:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start_str, "$lte": month_end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_str: str,
    to_str: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$dt"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "_hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$_hc.count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


# ============================================================
# ENDPOINTS: ANALYTICS
# ============================================================

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    year, m, month_start_str, month_end_str, month_start_dt, month_end_dt = month_bounds(month)

    pipeline = build_employee_monthly_pipeline(
        emp_code, month_start_str, month_end_str, month_start_dt, month_end_dt
    )

    result = list(db.employees.aggregate(pipeline))
    if not result:
        raise HTTPException(status_code=404, detail="Employee not found")

    item = result[0]
    working_days = item["working_days"]
    present_days = round_half_up(item["present_days"], 2) or 0.0

    if working_days > 0:
        att_pct = round_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days), 4)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": item["leave_days"],
        "late_count": item["late_count"],
        "total_late_minutes": item["total_late_minutes"],
        "total_overtime_minutes": item["total_overtime_minutes"],
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    year, m, month_start_str, month_end_str, _, _ = month_bounds(month)

    pipeline = build_department_summary_pipeline(
        month_start_str, month_end_str, department
    )
    records = list(db.employees.aggregate(pipeline))

    items = []
    for r in records:
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2) or 0.0,
            "avg_work_hours": round_half_up(r["avg_work_hours"], 2),
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
):
    year, m, month_start_str, month_end_str, _, _ = month_bounds(month)

    pipeline = build_late_leaderboard_pipeline(
        month_start_str, month_end_str, limit, department
    )
    records = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": records,
    }


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    # Department existence check (404)
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    start_date = parse_date_str(from_)
    end_date = parse_date_str(to)

    if end_date < start_date:
        raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")

    diff_days = (end_date - start_date).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, tzinfo=UTC) + timedelta(days=1)

    pipeline = build_department_trend_pipeline(
        department, from_, to, start_dt, end_dt
    )
    records = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in records:
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r["present_count"], 2) or 0.0,
            "late_count": r["late_count"],
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })

    return {
        "department": department,
        "items": items,
    }


# ============================================================
# ENDPOINT: ADMIN EXPLAIN
# ============================================================

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            if status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
                raise HTTPException(status_code=422, detail="Invalid status")
            query["status"] = status

        date_filter = {}
        if date_from:
            parse_date_str(date_from)
            date_filter["$gte"] = date_from
        if date_to:
            parse_date_str(date_to)
            date_filter["$lte"] = date_to

        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

        if date_filter:
            query["date"] = date_filter

        skip = (page - 1) * page_size
        expl = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(expl)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        year, m, month_start_str, month_end_str, month_start_dt, month_end_dt = month_bounds(month)
        pipeline = build_employee_monthly_pipeline(
            emp_code, month_start_str, month_end_str, month_start_dt, month_end_dt
        )
        expl = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json_util.loads(json_util.dumps(expl)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year, m, month_start_str, month_end_str, _, _ = month_bounds(month)
        pipeline = build_department_summary_pipeline(
            month_start_str, month_end_str, department
        )
        expl = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json_util.loads(json_util.dumps(expl)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year, m, month_start_str, month_end_str, _, _ = month_bounds(month)
        pipeline = build_late_leaderboard_pipeline(
            month_start_str, month_end_str, limit, department
        )
        expl = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(expl)),
        }

    elif endpoint == "department_trend":
        dept = department
        if not dept:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_:
            raise HTTPException(status_code=422, detail="'from' query param is required for department_trend")
        if not to:
            raise HTTPException(status_code=422, detail="'to' query param is required for department_trend")

        start_date = parse_date_str(from_)
        end_date = parse_date_str(to)

        if end_date < start_date:
            raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")

        diff_days = (end_date - start_date).days + 1
        if diff_days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
        end_dt = datetime(end_date.year, end_date.month, end_date.day, tzinfo=UTC) + timedelta(days=1)

        pipeline = build_department_trend_pipeline(
            dept, from_, to, start_dt, end_dt
        )
        expl = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(expl)),
        }

    raise HTTPException(status_code=422, detail="Unsupported endpoint")
