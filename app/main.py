"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars take priority)
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()  # local .env convenience; os.environ.get below always prefers real env vars

MONGO_URI: str = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.environ.get("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(
    MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EPOCH_MS_MIN = 100_000_000_000   # 1973-03-03 — values below look like seconds
EPOCH_MS_MAX = 4_102_444_800_000  # 2100-01-01

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

EMP_CODE_RE = re.compile(r"^EMP\d{4,6}\Z")
# Emp code regex uses \Z instead of $ to reject trailing newlines/spaces (e.g. "EMP0001\n")
SHIFT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# Reject year 0 in month strings (0000-MM causes date_type ValueError)
def validate_month_year_min(value: str) -> bool:
    """Check that month year is >= 1."""
    year = int(value.split("-")[0])
    return year >= 1
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Maximum allowed page/page_size to prevent int64 overflow on skip calculation
MAX_PAGE = 10_000
MAX_PAGE_SIZE = 1000

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

_indexes_created = False


def ensure_indexes() -> None:
    """Create all required indexes idempotently.
    MongoDB's create_index is idempotent; if the index already exists, it is a no-op.
    No global flag is needed - indexes are always attempted at startup.
    """
    # employees: unique on emp_code
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="emp_code_unique"
    )
    # employees: department + emp_code for filtering/sorting
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="dept_emp_code",
    )
    # employees: joined_on for department_summary COLLSCAN avoidance
    db.employees.create_index(
        [("joined_on", ASCENDING)], name="emp_joined_on"
    )
    # attendance_logs: unique on (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_code_date_unique",
    )
    # attendance_logs: date + status for filtering
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="date_status",
    )
    # attendance_logs: date desc + emp_code asc for list sorting
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp_asc",
    )
    # attendance_logs: emp_code + date + punch_in for punch-out lookup
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)],
        name="emp_punch_in",
    )
    # attendance_logs: late_minutes for leaderboard
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="date_late_minutes",
    )


# ---------------------------------------------------------------------------
# Startup: create indexes idempotently
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    """Create all required indexes at startup, idempotently."""
    try:
        ensure_indexes()
    except Exception:
        # If MongoDB is not yet reachable during boot, do not crash startup;
        # indexes will be created upon first successful connection.
        pass


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds (int) to a UTC-aware datetime truncated to whole seconds."""
    # truncate to whole seconds
    ms_truncated = (ms // 1000) * 1000
    return datetime.fromtimestamp(ms_truncated / 1000, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC-aware (or naive-UTC) datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def now_utc_truncated() -> datetime:
    """Current UTC time truncated to whole seconds."""
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def validate_epoch_ms_value(value: Any, field_name: str = "timestamp") -> int:
    """Validate and return epoch ms; raise ValueError on bad input."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not bool")
    if isinstance(value, float):
        raise ValueError(f"{field_name} must be an integer, not float")
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < EPOCH_MS_MIN:
        raise ValueError(
            f"{field_name} {value} looks like seconds (below {EPOCH_MS_MIN}); use milliseconds"
        )
    if value > EPOCH_MS_MAX:
        raise ValueError(f"{field_name} {value} exceeds maximum {EPOCH_MS_MAX}")
    return value


# ---------------------------------------------------------------------------
# Date / shift helpers
# ---------------------------------------------------------------------------

def parse_shift_minutes(shift_time: str) -> int:
    """Return shift time as total minutes since midnight."""
    h, m = map(int, shift_time.split(":"))
    return h * 60 + m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """True when shift_end <= shift_start (overnight shift)."""
    return parse_shift_minutes(shift_end) <= parse_shift_minutes(shift_start)


def attendance_date_for_punch_in(
    punch_in_utc: datetime, shift_start: str, shift_end: str
) -> str:
    """
    Compute the attendance date (YYYY-MM-DD IST string) following R1.
    Overnight shift: punch-in whose IST time is *earlier than* shift_end
    belongs to the shift that started the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()

    if is_overnight(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        punch_time_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute
        end_minutes = end_h * 60 + end_m
        # if IST clock time is before shift_end, this punch belongs to yesterday's shift
        if punch_time_minutes < end_minutes:
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def compute_shift_end_dt(
    attendance_date_str: str, shift_start: str, shift_end: str
) -> datetime:
    """
    Return the IST datetime of shift end for a given attendance date.
    For overnight shifts, shift end is on the next calendar day.
    """
    att_date = date_type.fromisoformat(attendance_date_str)
    end_h, end_m = map(int, shift_end.split(":"))
    if is_overnight(shift_start, shift_end):
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date
    return datetime(
        shift_end_date.year, shift_end_date.month, shift_end_date.day,
        end_h, end_m, 0, tzinfo=IST,
    )


def compute_shift_start_dt(attendance_date_str: str, shift_start: str) -> datetime:
    """Return the IST datetime of shift start for a given attendance date."""
    att_date = date_type.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    return datetime(
        att_date.year, att_date.month, att_date.day,
        start_h, start_m, 0, tzinfo=IST,
    )


# ---------------------------------------------------------------------------
# Business rule calculators
# ---------------------------------------------------------------------------

def calc_late_minutes(
    punch_in_utc: datetime, attendance_date_str: str, shift_start: str
) -> int:
    """
    R2: Grace period is 10 minutes. Strictly more than 10 min late.
    late_minutes = floor minutes since shift_start (not since grace end).
    Examples (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    The grace-period check is on total seconds (>600), not on floor minutes.
    """
    shift_start_ist = compute_shift_start_dt(attendance_date_str, shift_start)
    delta_seconds = int((punch_in_utc - shift_start_ist).total_seconds())
    # Strictly more than 600 seconds (10 minutes) late
    if delta_seconds <= 600:
        return 0
    # late_minutes = floor minutes since shift_start
    return delta_seconds // 60


def calc_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Elapsed seconds / 3600, rounded to 2 decimals ROUND_HALF_UP."""
    seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calc_overtime_minutes(
    punch_out_utc: datetime,
    attendance_date_str: str,
    shift_start: str,
    shift_end: str,
) -> int:
    """R3: Floor minutes between shift_end and punch_out; only if >= 30, else 0."""
    shift_end_ist = compute_shift_end_dt(attendance_date_str, shift_start, shift_end)
    delta_seconds = int((punch_out_utc - shift_end_ist).total_seconds())
    if delta_seconds <= 0:
        return 0
    delta_minutes = delta_seconds // 60  # floor
    return delta_minutes if delta_minutes >= 30 else 0


def calc_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def round2(value: float) -> float:
    """Round to 2 decimals with ROUND_HALF_UP using Decimal for exact computation."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round4(value: float) -> float:
    """Round to 4 decimals with ROUND_HALF_UP using Decimal for exact computation."""
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def serialize_attendance(doc: dict) -> dict:
    """Convert a MongoDB attendance_log document to API response dict."""
    history = doc.get("history") or []
    serialized_history = []
    for entry in history:
        changes = entry.get("changes", {})
        serialized_changes: dict = {}
        for field, change in changes.items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                from_val = datetime_to_epoch_ms(from_val)
                to_val = datetime_to_epoch_ms(to_val)
            serialized_changes[field] = {"from": from_val, "to": to_val}
        serialized_history.append({
            "at": datetime_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": serialized_changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialized_history,
    }


def serialize_employee(doc: dict) -> dict:
    """Convert a MongoDB employees document to API response dict."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_date_str(value: str, field_name: str = "date") -> str:
    """Validate YYYY-MM-DD string; raise HTTPException 422 on failure."""
    if not DATE_RE.match(value):
        raise HTTPException(
            422, f"Invalid {field_name} format: {value!r}. Expected YYYY-MM-DD"
        )
    try:
        date_type.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"Invalid {field_name}: {value!r}")
    return value


def validate_month_str(value: str) -> str:
    """Validate YYYY-MM string; raise HTTPException 422 on failure."""
    if not MONTH_RE.match(value):
        raise HTTPException(
            422, f"Invalid month format: {value!r}. Expected YYYY-MM"
        )
    year = int(value.split("-")[0])
    if year < 1:
        raise HTTPException(
            422, f"Invalid month year: {value!r}. Year must be >= 1"
        )
    return value


def month_range(month: str) -> tuple[str, str]:
    """Return (first_day_str, last_day_str) for a YYYY-MM month."""
    year, mon = map(int, month.split("-"))
    if year < 1:
        raise HTTPException(422, f"Invalid month year: {month!r}. Year must be >= 1")
    first = date_type(year, mon, 1)
    if mon == 12:
        last = date_type(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date_type(year, mon + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def get_employee_or_404(emp_code: str) -> dict:
    """Fetch employee by emp_code; raise 404 if not found."""
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"Employee {emp_code!r} not found")
    return emp


def make_json_safe(obj: Any) -> Any:
    """Recursively convert BSON/datetime/timestamp types to JSON-safe Python types."""
    import json
    from bson import json_util
    return json.loads(json_util.dumps(obj))


# ---------------------------------------------------------------------------
# Pipeline builders (shared across endpoints & explain)
# ---------------------------------------------------------------------------


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    """Build the aggregation pipeline for GET /analytics/employees/{emp_code}/monthly."""
    from_date, last_day = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": from_date, "$lte": last_day}}},
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
                "hd": {"$ifNull": ["$half_day", False]},
                "lm": {"$ifNull": ["$late_minutes", 0]},
                "om": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$addFields": {
                "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "is_late": {"$gt": ["$lm", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present"]},
                            {"$cond": ["$hd", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes": {"$sum": "$lm"},
                "total_overtime_minutes": {"$sum": "$om"},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    """Build the aggregation pipeline for GET /analytics/departments/summary."""
    from_date, last_day = month_range(month)

    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}, "date": {"$gte": from_date, "$lte": last_day}}},
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            },
                            "hd": {"$ifNull": ["$half_day", False]},
                            "lm": {"$ifNull": ["$late_minutes", 0]},
                        }
                    },
                    {
                        "$addFields": {
                            "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$log.is_weekday", "$$log.is_present"]},
                                    {"$cond": ["$$log.hd", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$log.is_present", {"$gt": ["$$log.work_hours", None]}]},
                                    "$$log.work_hours",
                                    0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$log.is_present"]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$gt": ["$$log.lm", 0]}, 1, 0]}
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]
                            }
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]
                            }
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours_sum": 1,
                "avg_work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list:
    """Build the aggregation pipeline for GET /analytics/leaderboard/late."""
    from_date, last_day = month_range(month)

    return [
        {"$match": {
            "date": {"$gte": from_date, "$lte": last_day},
            "late_minutes": {"$gt": 0},
        }},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
        # Apply department filter if provided
        *(
            [{"$match": {"emp.department": department}}]
            if department is not None
            else []
        ),
        # Set window fields for DB-based ranking ($setWindowFields / $rank)
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1, "_id": 1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1,
            }
        },
    ]


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    """Build the aggregation pipeline for GET /analytics/departments/{department}/trend."""
    return [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$addFields": {
                "hd": {"$ifNull": ["$half_day", False]},
                "lm": {"$ifNull": ["$late_minutes", 0]},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "date_str": {"$first": "$date"},
                "raw_present_count": {
                    "$sum": {
                        "$cond": ["$is_present", {"$cond": ["$hd", 0.5, 1.0]}, 0.0]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$lm", 0]}, 1, 0]}
                },
            }
        },
        {
            "$addFields": {
                "date_str": {
                    "$ifNull": [
                        "$date_str",
                        {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                    ]
                },
                "raw_present_count": {"$ifNull": ["$raw_present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "cnt"},
                ],
                "as": "hc_result",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_result.cnt", 0]}, 0]},
                "dow": {"$dayOfWeek": "$_id"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$raw_present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": "$raw_present_count",
                "late_count": 1,
                "attendance_rate": 1,
            }
        },
    ]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def val_emp_code(cls, v: str) -> str:
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def val_name(cls, v: str) -> str:
        if not (1 <= len(v) <= 100):
            raise ValueError("name must be between 1 and 100 characters")
        return v

    @field_validator("email")
    @classmethod
    def val_email(cls, v: str) -> str:
        if len(v) > 120:
            raise ValueError("email must not exceed 120 characters")
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email format")
        return v

    @field_validator("department")
    @classmethod
    def val_department(cls, v: str) -> str:
        if not (1 <= len(v) <= 50):
            raise ValueError("department must be between 1 and 50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def val_shift_time(cls, v: str) -> str:
        if not SHIFT_TIME_RE.match(v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def val_joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on is not a valid date")
        return v

    @model_validator(mode="after")
    def val_shift_not_equal(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must not be equal")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms_value(v, "punched_at")

    @field_validator("status")
    @classmethod
    def val_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms_value(v, "punched_at")


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def val_timestamp(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms_value(v, "punch timestamp")

    @field_validator("status")
    @classmethod
    def val_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("reason")
    @classmethod
    def val_reason(cls, v: str) -> str:
        if not (5 <= len(v) <= 200):
            raise ValueError("reason must be between 5 and 200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def val_regularized_by(cls, v: str) -> str:
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by must be between 1 and 50 characters")
        return v


# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Attendance Pulse | Enterprise Analytics</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg-primary: #0a0e17;
      --bg-secondary: #111827;
      --bg-card: rgba(17, 24, 39, 0.75);
      --border-glass: rgba(255, 255, 255, 0.08);
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
      --text-dim: #6b7280;
      --primary: #6366f1;
      --primary-hover: #4f46e5;
      --accent-cyan: #06b6d4;
      --accent-emerald: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-full: 9999px;
      --font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      background-color: var(--bg-primary);
      background-image: 
        radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.12) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(6, 182, 212, 0.08) 0px, transparent 50%);
      color: var(--text-main);
      font-family: var(--font-family);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    .navbar {
      position: sticky; top: 0; z-index: 50;
      background: rgba(11, 15, 23, 0.85);
      backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--border-glass);
      padding: 0.85rem 2rem;
      display: flex; align-items: center; justify-content: space-between;
    }
    .brand { display: flex; align-items: center; gap: 0.75rem; text-decoration: none; color: #fff; cursor: pointer; }
    .brand-icon {
      width: 36px; height: 36px;
      background: linear-gradient(135deg, var(--primary), var(--accent-cyan));
      border-radius: var(--radius-sm);
      display: flex; align-items: center; justify-content: center;
      font-weight: 800; font-size: 1.1rem;
    }
    .brand-text h1 { font-size: 1.1rem; font-weight: 700; letter-spacing: -0.02em; }
    .brand-text p { font-size: 0.72rem; color: var(--text-muted); }
    .nav-links { display: flex; align-items: center; gap: 0.4rem; list-style: none; }
    .nav-link {
      padding: 0.5rem 0.9rem; border-radius: var(--radius-sm);
      text-decoration: none; color: var(--text-muted);
      font-size: 0.88rem; font-weight: 500; cursor: pointer; border: none; background: transparent;
      transition: all 0.2s ease;
    }
    .nav-link:hover { color: #fff; background: rgba(255, 255, 255, 0.05); }
    .nav-link.active { color: #fff; background: var(--primary); box-shadow: 0 4px 15px rgba(99, 102, 241, 0.35); }
    .nav-status { display: flex; align-items: center; gap: 1rem; }
    .clock-pill {
      font-size: 0.8rem; color: var(--accent-cyan);
      background: rgba(6, 182, 212, 0.1); border: 1px solid rgba(6, 182, 212, 0.25);
      padding: 0.35rem 0.75rem; border-radius: var(--radius-full);
      display: flex; align-items: center; gap: 0.4rem;
    }
    .status-badge {
      display: flex; align-items: center; gap: 0.45rem;
      font-size: 0.78rem; font-weight: 600; padding: 0.35rem 0.75rem;
      border-radius: var(--radius-full); border: 1px solid transparent;
    }
    .status-badge.online { background: rgba(16, 185, 129, 0.12); color: var(--accent-emerald); border-color: rgba(16, 185, 129, 0.25); }
    .status-badge.offline { background: rgba(244, 63, 94, 0.12); color: var(--accent-rose); border-color: rgba(244, 63, 94, 0.25); }
    .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
    
    .main-content { flex: 1; max-width: 1400px; width: 100%; margin: 0 auto; padding: 2rem; }
    .page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; flex-wrap: wrap; gap: 1rem; }
    .page-title h2 { font-size: 1.75rem; font-weight: 800; letter-spacing: -0.02em; }
    .page-title p { color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem; }
    .page-actions { display: flex; align-items: center; gap: 0.75rem; }

    .card {
      background: var(--bg-card); backdrop-filter: blur(12px);
      border: 1px solid var(--border-glass); border-radius: var(--radius-md);
      box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5); overflow: hidden; margin-bottom: 1.5rem;
    }
    .card-header {
      padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--border-glass);
      display: flex; align-items: center; justify-content: space-between;
    }
    .card-header h3 { font-size: 1.05rem; font-weight: 700; display: flex; align-items: center; gap: 0.5rem; }
    .card-body { padding: 1.5rem; }

    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1.25rem; margin-bottom: 2rem; }
    .stat-card {
      background: var(--bg-card); border: 1px solid var(--border-glass);
      border-radius: var(--radius-md); padding: 1.4rem; display: flex; align-items: center; gap: 1.2rem;
    }
    .stat-icon { width: 50px; height: 50px; border-radius: var(--radius-md); display: flex; align-items: center; justify-content: center; font-size: 1.4rem; }
    .stat-icon.indigo { background: rgba(99, 102, 241, 0.15); color: var(--primary); }
    .stat-icon.emerald { background: rgba(16, 185, 129, 0.15); color: var(--accent-emerald); }
    .stat-icon.amber { background: rgba(245, 158, 11, 0.15); color: var(--accent-amber); }
    .stat-icon.cyan { background: rgba(6, 182, 212, 0.15); color: var(--accent-cyan); }
    .stat-data h4 { font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); }
    .stat-data .val { font-size: 1.7rem; font-weight: 800; color: #fff; margin-top: 0.15rem; font-variant-numeric: tabular-nums; }

    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 0.5rem;
      padding: 0.6rem 1.15rem; border-radius: var(--radius-sm); font-size: 0.88rem; font-weight: 600;
      cursor: pointer; border: 1px solid transparent; transition: all 0.2s ease; text-decoration: none;
    }
    .btn-primary { background: var(--primary); color: #fff; box-shadow: 0 4px 14px rgba(99, 102, 241, 0.3); }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-success { background: var(--accent-emerald); color: #fff; box-shadow: 0 4px 14px rgba(16, 185, 129, 0.3); }
    .btn-success:hover { filter: brightness(1.1); }
    .btn-outline { background: transparent; border-color: var(--border-glass); color: var(--text-main); }
    .btn-outline:hover { background: rgba(255, 255, 255, 0.06); }
    .btn-sm { padding: 0.35rem 0.75rem; font-size: 0.8rem; }

    .form-group { margin-bottom: 1.15rem; }
    .form-label { display: block; font-size: 0.82rem; font-weight: 600; color: var(--text-muted); margin-bottom: 0.4rem; }
    .form-control, .form-select {
      width: 100%; padding: 0.65rem 0.95rem; background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--border-glass); border-radius: var(--radius-sm);
      color: var(--text-main); font-family: inherit; font-size: 0.9rem;
    }
    .form-control:focus, .form-select:focus { outline: none; border-color: var(--primary); }

    .filter-bar { display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; padding: 1rem 1.25rem; background: rgba(15, 23, 42, 0.4); border-bottom: 1px solid var(--border-glass); }
    .table-responsive { width: 100%; overflow-x: auto; }
    .data-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.88rem; }
    .data-table th { padding: 0.9rem 1.15rem; background: rgba(15, 23, 42, 0.8); color: var(--text-muted); font-size: 0.78rem; text-transform: uppercase; border-bottom: 1px solid var(--border-glass); }
    .data-table td { padding: 0.9rem 1.15rem; border-bottom: 1px solid rgba(255, 255, 255, 0.04); vertical-align: middle; }
    .data-table tbody tr:hover { background: rgba(255, 255, 255, 0.03); }

    .badge { display: inline-flex; align-items: center; padding: 0.25rem 0.6rem; border-radius: var(--radius-full); font-size: 0.75rem; font-weight: 700; }
    .badge-present { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .badge-wfh { background: rgba(6, 182, 212, 0.15); color: #38bdf8; }
    .badge-on-duty { background: rgba(139, 92, 246, 0.15); color: #a78bfa; }
    .badge-absent { background: rgba(244, 63, 94, 0.15); color: #fb7185; }
    .badge-leave { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
    .badge-half-day { background: rgba(251, 146, 60, 0.2); color: #fdba74; margin-left: 0.35rem; }

    .pagination { display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.5rem; border-top: 1px solid var(--border-glass); font-size: 0.85rem; color: var(--text-muted); }
    .modal-overlay {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.75); backdrop-filter: blur(8px);
      z-index: 100; display: flex; align-items: center; justify-content: center;
      opacity: 0; pointer-events: none; transition: opacity 0.25s ease; padding: 1.5rem;
    }
    .modal-overlay.active { opacity: 1; pointer-events: auto; }
    .modal-card {
      background: #111827; border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: var(--radius-md); box-shadow: 0 25px 50px -12px rgba(0,0,0,0.7);
      width: 100%; max-width: 540px; overflow: hidden;
    }
    .modal-header { padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--border-glass); display: flex; align-items: center; justify-content: space-between; }
    .modal-close { background: transparent; border: none; color: var(--text-muted); font-size: 1.4rem; cursor: pointer; }
    .modal-body { padding: 1.5rem; max-height: 75vh; overflow-y: auto; }
    .modal-footer { padding: 1rem 1.5rem; border-top: 1px solid var(--border-glass); display: flex; justify-content: flex-end; gap: 0.75rem; }

    .toast-container { position: fixed; bottom: 2rem; right: 2rem; z-index: 200; display: flex; flex-direction: column; gap: 0.75rem; }
    .toast {
      background: rgba(17, 24, 39, 0.95); backdrop-filter: blur(12px);
      border: 1px solid var(--border-glass); border-radius: var(--radius-sm);
      padding: 0.85rem 1.25rem; min-width: 300px; max-width: 420px;
      display: flex; align-items: center; gap: 0.75rem; font-size: 0.88rem;
    }
    .toast.success { border-left: 4px solid var(--accent-emerald); }
    .toast.error { border-left: 4px solid var(--accent-rose); }
    pre.json-view {
      background: #060910; border: 1px solid var(--border-glass); border-radius: var(--radius-sm);
      padding: 1rem; font-family: monospace; font-size: 0.82rem; color: #a5b4fc; overflow-x: auto; max-height: 480px;
    }
  </style>
</head>
<body>
  <!-- Navbar -->
  <nav class="navbar">
    <div class="brand" onclick="switchNav('dashboard')">
      <div class="brand-icon">⚡</div>
      <div class="brand-text">
        <h1>ATTENDANCE PULSE</h1>
        <p>Enterprise Attendance & Analytics</p>
      </div>
    </div>
    <ul class="nav-links">
      <li><button class="nav-link active" id="nav-dashboard" onclick="switchNav('dashboard')">Dashboard</button></li>
      <li><button class="nav-link" id="nav-employees" onclick="switchNav('employees')">Employees</button></li>
      <li><button class="nav-link" id="nav-attendance" onclick="switchNav('attendance')">Attendance Logs</button></li>
      <li><button class="nav-link" id="nav-analytics" onclick="switchNav('analytics')">Analytics Hub</button></li>
      <li><button class="nav-link" id="nav-explain" onclick="switchNav('explain')">Query Explain</button></li>
      <li><a href="/docs" target="_blank" class="nav-link" style="color: var(--accent-cyan);">API Docs ↗</a></li>
    </ul>
    <div class="nav-status">
      <div class="clock-pill">
        <span>🕒</span>
        <span id="live-clock">Loading IST...</span>
      </div>
      <div id="backend-status-pill" class="status-badge online">
        <span class="pulse-dot"></span>
        <span id="backend-status-text">Checking API...</span>
      </div>
    </div>
  </nav>

  <!-- Main Container -->
  <main class="main-content">

    <!-- VIEW 1: DASHBOARD -->
    <div id="view-dashboard">
      <div class="page-header">
        <div class="page-title">
          <h2>Operations Dashboard</h2>
          <p>Live status, quick punch actions, and attendance overview</p>
        </div>
        <div class="page-actions">
          <button class="btn btn-success" onclick="openModal('modal-punch-in')"><span>⚡</span> Punch In</button>
          <button class="btn btn-primary" onclick="openModal('modal-punch-out')"><span>👋</span> Punch Out</button>
        </div>
      </div>

      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-icon indigo">👥</div>
          <div class="stat-data">
            <h4>Total Employees</h4>
            <div class="val" id="dash-emp-count">-</div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon emerald">🚀</div>
          <div class="stat-data">
            <h4>API Backend</h4>
            <div class="val" id="dash-api-status" style="font-size: 1.2rem; color: var(--accent-emerald);">MongoDB Online</div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon amber">⏱️</div>
          <div class="stat-data">
            <h4>Grace Period (R2)</h4>
            <div class="val">10 Mins</div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon cyan">📊</div>
          <div class="stat-data">
            <h4>OT Threshold (R3)</h4>
            <div class="val">30 Mins</div>
          </div>
        </div>
      </div>

      <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 1.5rem; align-items: start;">
        <div class="card">
          <div class="card-header">
            <h3><span>📋</span> Recent Attendance Activity</h3>
            <button class="btn btn-outline btn-sm" onclick="switchNav('attendance')">View All Logs &rarr;</button>
          </div>
          <div class="table-responsive">
            <table class="data-table">
              <thead>
                <tr>
                  <th>Employee</th><th>Date</th><th>Status</th><th>Punch In</th><th>Punch Out</th><th>Hours</th><th>Late</th>
                </tr>
              </thead>
              <tbody id="dash-recent-tbody">
                <tr><td colspan="7" style="text-align: center; padding: 2rem;">Loading attendance...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <h3><span>📘</span> Business Rules Active</h3>
          </div>
          <div class="card-body" style="font-size: 0.85rem; line-height: 1.6; color: var(--text-muted);">
            <div style="margin-bottom: 0.75rem;"><strong style="color: #fff;">R1 Overnight Shifts:</strong> Shift times & attendance dates in IST (UTC+05:30). Punches before shift-end map to yesterday's shift.</div>
            <div style="margin-bottom: 0.75rem;"><strong style="color: #fff;">R2 Strict Grace Period:</strong> &le; 10:00 is not late (0m). &gt; 10:00 calculates floor minutes from shift start.</div>
            <div style="margin-bottom: 0.75rem;"><strong style="color: #fff;">R3 Overtime:</strong> Counted only when &ge; 30 minutes post shift end.</div>
            <div style="margin-bottom: 0.75rem;"><strong style="color: #fff;">R4 Work Hours & R5 Half Day:</strong> Rounded half-up 2 decimals. &lt; 4.50h counts as half day.</div>
            <div><strong style="color: #fff;">R7 Working Days:</strong> Monday to Friday; weekends contribute to overtime and late totals.</div>
          </div>
        </div>
      </div>
    </div>

    <!-- VIEW 2: EMPLOYEES -->
    <div id="view-employees" style="display: none;">
      <div class="page-header">
        <div class="page-title">
          <h2>Employee Directory</h2>
          <p>Manage and inspect registered company personnel</p>
        </div>
        <div class="page-actions">
          <button class="btn btn-primary" onclick="openModal('modal-add-employee')"><span>➕</span> Add Employee</button>
        </div>
      </div>

      <div class="card">
        <div class="filter-bar">
          <input type="text" id="emp-filter-dept" class="form-control" placeholder="Filter by Department..." style="max-width: 250px;" onchange="loadEmployees(1)">
          <button class="btn btn-outline btn-sm" onclick="loadEmployees(1)">Apply Filter</button>
        </div>
        <div class="table-responsive">
          <table class="data-table">
            <thead>
              <tr>
                <th>Emp Code</th><th>Full Name</th><th>Email</th><th>Department</th><th>Shift Window</th><th>Joined On</th><th>Registered</th>
              </tr>
            </thead>
            <tbody id="emp-tbody">
              <tr><td colspan="7" style="text-align: center; padding: 2rem;">Loading directory...</td></tr>
            </tbody>
          </table>
        </div>
        <div class="pagination">
          <div id="emp-page-info">Showing 0 employees</div>
          <div class="page-controls" id="emp-page-controls"></div>
        </div>
      </div>
    </div>

    <!-- VIEW 3: ATTENDANCE LOGS -->
    <div id="view-attendance" style="display: none;">
      <div class="page-header">
        <div class="page-title">
          <h2>Attendance Logs</h2>
          <p>Search, filter, inspect, and regularize employee attendance records</p>
        </div>
        <div class="page-actions">
          <button class="btn btn-success" onclick="openModal('modal-punch-in')"><span>⚡</span> Punch In</button>
          <button class="btn btn-primary" onclick="openModal('modal-punch-out')"><span>👋</span> Punch Out</button>
        </div>
      </div>

      <div class="card">
        <div class="filter-bar">
          <input type="text" id="att-filter-emp" class="form-control" placeholder="EMP Code" style="max-width: 140px;">
          <input type="date" id="att-filter-from" class="form-control" style="max-width: 150px;">
          <input type="date" id="att-filter-to" class="form-control" style="max-width: 150px;">
          <select id="att-filter-status" class="form-select" style="max-width: 150px;">
            <option value="">All Statuses</option>
            <option value="PRESENT">PRESENT</option>
            <option value="WFH">WFH</option>
            <option value="ON_DUTY">ON_DUTY</option>
            <option value="ABSENT">ABSENT</option>
            <option value="LEAVE">LEAVE</option>
          </select>
          <button class="btn btn-outline btn-sm" onclick="loadAttendance(1)">Filter</button>
          <button class="btn btn-outline btn-sm" onclick="resetAttendanceFilter()">Reset</button>
        </div>

        <div class="table-responsive">
          <table class="data-table">
            <thead>
              <tr>
                <th>Employee</th><th>Date</th><th>Status</th><th>Punch In (IST)</th><th>Punch Out (IST)</th><th>Work Hours</th><th>Late</th><th>OT</th><th>Audit</th><th>Actions</th>
              </tr>
            </thead>
            <tbody id="att-tbody">
              <tr><td colspan="10" style="text-align: center; padding: 2rem;">Loading logs...</td></tr>
            </tbody>
          </table>
        </div>
        <div class="pagination">
          <div id="att-page-info">Showing 0 logs</div>
          <div class="page-controls" id="att-page-controls"></div>
        </div>
      </div>
    </div>

    <!-- VIEW 4: ANALYTICS HUB -->
    <div id="view-analytics" style="display: none;">
      <div class="page-header">
        <div class="page-title">
          <h2>Analytics & Insights Hub</h2>
          <p>Multi-stage aggregation analytics directly from MongoDB</p>
        </div>
      </div>

      <div style="display: flex; gap: 0.5rem; margin-bottom: 1.5rem; border-bottom: 1px solid var(--border-glass); padding-bottom: 0.5rem;">
        <button class="btn btn-primary" id="btn-ana-monthly" onclick="switchAnaTab('monthly')">👤 Employee Monthly</button>
        <button class="btn btn-outline" id="btn-ana-dept" onclick="switchAnaTab('dept')">🏢 Department Summary</button>
        <button class="btn btn-outline" id="btn-ana-lead" onclick="switchAnaTab('lead')">🏆 Late Leaderboard</button>
        <button class="btn btn-outline" id="btn-ana-trend" onclick="switchAnaTab('trend')">📈 Department Trend</button>
      </div>

      <!-- Ana 1: Monthly -->
      <div id="ana-view-monthly" class="card">
        <div class="card-header"><h3>👤 Monthly Employee Attendance Breakdown</h3></div>
        <div class="card-body">
          <div style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1.5rem;">
            <div style="flex: 1; min-width: 180px;">
              <label class="form-label">Employee Code</label>
              <input type="text" id="ana-m-emp" class="form-control" value="EMP0001">
            </div>
            <div style="flex: 1; min-width: 180px;">
              <label class="form-label">Month (YYYY-MM)</label>
              <input type="month" id="ana-m-month" class="form-control" value="2026-07">
            </div>
            <button class="btn btn-primary" onclick="queryMonthlyAnalytics()">Run Aggregation</button>
          </div>
          <div id="ana-m-results" style="display: none;">
            <div class="stats-grid">
              <div class="stat-card"><div class="stat-icon emerald">🎯</div><div class="stat-data"><h4>Attendance Rate</h4><div class="val" id="m-val-pct">0%</div></div></div>
              <div class="stat-card"><div class="stat-icon indigo">📅</div><div class="stat-data"><h4>Working Days</h4><div class="val" id="m-val-work">0</div></div></div>
              <div class="stat-card"><div class="stat-icon cyan">✅</div><div class="stat-data"><h4>Present Days</h4><div class="val" id="m-val-pres">0</div></div></div>
              <div class="stat-card"><div class="stat-icon amber">⏱️</div><div class="stat-data"><h4>Total Late Mins</h4><div class="val" id="m-val-late">0m</div></div></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Ana 2: Department Summary -->
      <div id="ana-view-dept" class="card" style="display: none;">
        <div class="card-header"><h3>🏢 Department Monthly Summary</h3></div>
        <div class="card-body">
          <div style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1.5rem;">
            <div style="flex: 1; min-width: 180px;">
              <label class="form-label">Month (YYYY-MM)</label>
              <input type="month" id="ana-d-month" class="form-control" value="2026-07">
            </div>
            <div style="flex: 1; min-width: 180px;">
              <label class="form-label">Department Filter (Optional)</label>
              <input type="text" id="ana-d-dept" class="form-control" placeholder="All Departments">
            </div>
            <button class="btn btn-primary" onclick="queryDeptSummary()">Run Aggregation</button>
          </div>
          <div id="ana-d-table-wrap" class="table-responsive"></div>
        </div>
      </div>

      <!-- Ana 3: Leaderboard -->
      <div id="ana-view-lead" class="card" style="display: none;">
        <div class="card-header"><h3>🏆 Late-Comers Leaderboard (Competition Ranking)</h3></div>
        <div class="card-body">
          <div style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1.5rem;">
            <div style="flex: 1; min-width: 160px;">
              <label class="form-label">Month (YYYY-MM)</label>
              <input type="month" id="ana-l-month" class="form-control" value="2026-07">
            </div>
            <div style="flex: 1; min-width: 120px;">
              <label class="form-label">Limit (1-50)</label>
              <input type="number" id="ana-l-limit" class="form-control" value="10" min="1" max="50">
            </div>
            <button class="btn btn-primary" onclick="queryLeaderboard()">Run Aggregation</button>
          </div>
          <div id="ana-l-table-wrap" class="table-responsive"></div>
        </div>
      </div>

      <!-- Ana 4: Trend -->
      <div id="ana-view-trend" class="card" style="display: none;">
        <div class="card-header"><h3>📈 Daily Attendance Trend with 7-Day Moving Average</h3></div>
        <div class="card-body">
          <div style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1.5rem;">
            <div style="flex: 1; min-width: 160px;">
              <label class="form-label">Department *</label>
              <input type="text" id="ana-t-dept" class="form-control" value="Engineering">
            </div>
            <div style="flex: 1; min-width: 140px;">
              <label class="form-label">From Date</label>
              <input type="date" id="ana-t-from" class="form-control" value="2026-07-01">
            </div>
            <div style="flex: 1; min-width: 140px;">
              <label class="form-label">To Date</label>
              <input type="date" id="ana-t-to" class="form-control" value="2026-07-31">
            </div>
            <button class="btn btn-primary" onclick="queryTrend()">Plot Trend</button>
          </div>
          <div style="background: rgba(15,23,42,0.5); padding: 1rem; border-radius: var(--radius-sm); border: 1px solid var(--border-glass); margin-bottom: 1.5rem;">
            <canvas id="trendChartCanvas" height="85"></canvas>
          </div>
          <div id="ana-t-table-wrap" class="table-responsive"></div>
        </div>
      </div>
    </div>

    <!-- VIEW 5: QUERY EXPLAIN -->
    <div id="view-explain" style="display: none;">
      <div class="page-header">
        <div class="page-title">
          <h2>MongoDB Execution Plan Inspector</h2>
          <p>Inspect explain(executionStats) across indexed query plans</p>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><h3>🔍 Target Endpoint</h3></div>
        <div class="card-body">
          <div style="display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1.5rem;">
            <div style="flex: 2; min-width: 260px;">
              <label class="form-label">Select Endpoint</label>
              <select id="exp-select" class="form-select">
                <option value="attendance_list">attendance_list (GET /attendance)</option>
                <option value="employee_monthly">employee_monthly (GET /analytics/employees/...)</option>
                <option value="department_summary">department_summary (GET /analytics/departments/summary)</option>
                <option value="late_leaderboard">late_leaderboard (GET /analytics/leaderboard/late)</option>
                <option value="department_trend">department_trend (GET /analytics/departments/.../trend)</option>
              </select>
            </div>
            <button class="btn btn-primary" onclick="runQueryExplain()">Inspect Execution Plan</button>
          </div>
          <div id="exp-output-wrap" style="display: none;">
            <div style="margin-bottom: 1rem;">
              <span style="color: var(--text-muted);">Collection: </span>
              <strong id="exp-res-coll" style="color: var(--accent-cyan); font-size: 1.1rem;"></strong> &nbsp;|&nbsp;
              <span id="exp-res-stage" style="font-weight: 700;"></span>
            </div>
            <pre id="exp-res-json" class="json-view"></pre>
          </div>
        </div>
      </div>
    </div>

  </main>

  <!-- MODALS -->

  <!-- Modal: Punch In -->
  <div id="modal-punch-in" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>⚡ Record Punch-In</h3>
        <button class="modal-close" onclick="closeModal('modal-punch-in')">&times;</button>
      </div>
      <form onsubmit="submitPunchIn(event)">
        <div class="modal-body">
          <div class="form-group">
            <label class="form-label">Employee Code *</label>
            <input type="text" name="emp_code" class="form-control" placeholder="EMP0001" required pattern="^EMP\\d{4,6}$">
          </div>
          <div class="form-group">
            <label class="form-label">Status *</label>
            <select name="status" class="form-select">
              <option value="PRESENT" selected>PRESENT</option>
              <option value="WFH">WFH</option>
              <option value="ON_DUTY">ON_DUTY</option>
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">Custom Timestamp (Optional)</label>
            <input type="datetime-local" name="custom_time" class="form-control">
            <small style="color: var(--text-dim); font-size: 0.75rem;">Leave empty for current instant</small>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline" onclick="closeModal('modal-punch-in')">Cancel</button>
          <button type="submit" class="btn btn-success">Confirm Punch-In</button>
        </div>
      </form>
    </div>
  </div>

  <!-- Modal: Punch Out -->
  <div id="modal-punch-out" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>👋 Record Punch-Out</h3>
        <button class="modal-close" onclick="closeModal('modal-punch-out')">&times;</button>
      </div>
      <form onsubmit="submitPunchOut(event)">
        <div class="modal-body">
          <div class="form-group">
            <label class="form-label">Employee Code *</label>
            <input type="text" name="emp_code" class="form-control" placeholder="EMP0001" required pattern="^EMP\\d{4,6}$">
          </div>
          <div class="form-group">
            <label class="form-label">Custom Timestamp (Optional)</label>
            <input type="datetime-local" name="custom_time" class="form-control">
            <small style="color: var(--text-dim); font-size: 0.75rem;">Leave empty for current instant</small>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline" onclick="closeModal('modal-punch-out')">Cancel</button>
          <button type="submit" class="btn btn-primary">Confirm Punch-Out</button>
        </div>
      </form>
    </div>
  </div>

  <!-- Modal: Add Employee -->
  <div id="modal-add-employee" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>➕ Register Employee</h3>
        <button class="modal-close" onclick="closeModal('modal-add-employee')">&times;</button>
      </div>
      <form onsubmit="submitCreateEmployee(event)">
        <div class="modal-body">
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
            <div class="form-group">
              <label class="form-label">Emp Code *</label>
              <input type="text" name="emp_code" class="form-control" placeholder="EMP0001" required pattern="^EMP\\d{4,6}$">
            </div>
            <div class="form-group">
              <label class="form-label">Full Name *</label>
              <input type="text" name="name" class="form-control" placeholder="Jane Doe" required minlength="1" maxlength="100">
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Email *</label>
            <input type="email" name="email" class="form-control" placeholder="jane@example.com" required maxlength="120">
          </div>
          <div class="form-group">
            <label class="form-label">Department *</label>
            <input type="text" name="department" class="form-control" placeholder="Engineering" required maxlength="50">
          </div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
            <div class="form-group">
              <label class="form-label">Shift Start (HH:MM)</label>
              <input type="text" name="shift_start" class="form-control" value="09:30" required pattern="^([01]\\d|2[0-3]):[0-5]\\d$">
            </div>
            <div class="form-group">
              <label class="form-label">Shift End (HH:MM)</label>
              <input type="text" name="shift_end" class="form-control" value="18:30" required pattern="^([01]\\d|2[0-3]):[0-5]\\d$">
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Joining Date (YYYY-MM-DD)</label>
            <input type="date" name="joined_on" class="form-control" value="2026-01-01" required>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline" onclick="closeModal('modal-add-employee')">Cancel</button>
          <button type="submit" class="btn btn-primary">Save Employee</button>
        </div>
      </form>
    </div>
  </div>

  <!-- Modal: Regularize Attendance -->
  <div id="modal-regularize" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>✏️ Regularize Attendance</h3>
        <button class="modal-close" onclick="closeModal('modal-regularize')">&times;</button>
      </div>
      <form onsubmit="submitRegularize(event)">
        <div class="modal-body">
          <input type="hidden" id="reg-emp-code">
          <input type="hidden" id="reg-date">
          <div style="background: rgba(15,23,42,0.6); padding: 0.75rem 1rem; border-radius: var(--radius-sm); margin-bottom: 1.25rem;">
            Target: <strong id="reg-target-display" style="color: var(--accent-cyan);"></strong>
          </div>
          <div class="form-group">
            <label class="form-label">New Status</label>
            <select id="reg-status" class="form-select">
              <option value="PRESENT">PRESENT</option>
              <option value="WFH">WFH</option>
              <option value="ON_DUTY">ON_DUTY</option>
              <option value="ABSENT">ABSENT</option>
              <option value="LEAVE">LEAVE</option>
            </select>
          </div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
            <div class="form-group">
              <label class="form-label">Punch In (Optional Update)</label>
              <input type="datetime-local" id="reg-punch-in" class="form-control">
            </div>
            <div class="form-group">
              <label class="form-label">Punch Out (Optional Update)</label>
              <input type="datetime-local" id="reg-punch-out" class="form-control">
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Reason * (5-200 chars)</label>
            <textarea id="reg-reason" class="form-control" rows="2" required minlength="5" maxlength="200" placeholder="e.g. Card scanner failure"></textarea>
          </div>
          <div class="form-group">
            <label class="form-label">Regularized By * (1-50 chars)</label>
            <input type="text" id="reg-by" class="form-control" required minlength="1" maxlength="50" placeholder="HR Manager">
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline" onclick="closeModal('modal-regularize')">Cancel</button>
          <button type="submit" class="btn btn-primary">Apply Update</button>
        </div>
      </form>
    </div>
  </div>

  <!-- Modal: History Viewer -->
  <div id="modal-history" class="modal-overlay">
    <div class="modal-card" style="max-width: 650px;">
      <div class="modal-header">
        <h3>📜 Audit Trail</h3>
        <button class="modal-close" onclick="closeModal('modal-history')">&times;</button>
      </div>
      <div class="modal-body" id="history-modal-body"></div>
      <div class="modal-footer">
        <button type="button" class="btn btn-outline" onclick="closeModal('modal-history')">Close</button>
      </div>
    </div>
  </div>

  <div id="toast-container" class="toast-container"></div>

  <!-- JAVASCRIPT LOGIC -->
  <script>
    let trendChartObj = null;

    document.addEventListener("DOMContentLoaded", () => {
      initClock();
      checkBackendHealth();
      loadDashboard();
    });

    function initClock() {
      const el = document.getElementById("live-clock");
      function tick() {
        const now = new Date();
        const ist = new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 5.5 * 3600000);
        el.textContent = ist.toISOString().replace("T", " ").substring(0, 19) + " IST";
      }
      tick(); setInterval(tick, 1000);
    }

    async function checkBackendHealth() {
      const pill = document.getElementById("backend-status-pill");
      const txt = document.getElementById("backend-status-text");
      try {
        const res = await fetch("/health");
        if (res.ok) {
          pill.className = "status-badge online";
          txt.textContent = "FastAPI & DB Online";
        } else {
          pill.className = "status-badge offline";
          txt.textContent = "DB Offline (503)";
        }
      } catch (e) {
        pill.className = "status-badge offline";
        txt.textContent = "Offline";
      }
    }

    function switchNav(tab) {
      ["dashboard", "employees", "attendance", "analytics", "explain"].forEach(t => {
        document.getElementById(`view-${t}`).style.display = t === tab ? "block" : "none";
        document.getElementById(`nav-${t}`).className = `nav-link ${t === tab ? "active" : ""}`;
      });
      if (tab === "employees") loadEmployees(1);
      if (tab === "attendance") loadAttendance(1);
    }

    function switchAnaTab(t) {
      ["monthly", "dept", "lead", "trend"].forEach(name => {
        document.getElementById(`ana-view-${name}`).style.display = name === t ? "block" : "none";
        const btn = document.getElementById(`btn-ana-${name}`);
        if (name === t) { btn.className = "btn btn-primary"; } else { btn.className = "btn btn-outline"; }
      });
    }

    function showToast(msg, type = "info") {
      const container = document.getElementById("toast-container");
      const toast = document.createElement("div");
      toast.className = `toast ${type}`;
      toast.innerHTML = `<span>${type === 'success' ? '✓' : '⚠'}</span> <span>${msg}</span>`;
      container.appendChild(toast);
      setTimeout(() => { toast.remove(); }, 3500);
    }

    function openModal(id) { document.getElementById(id).classList.add("active"); }
    function closeModal(id) { document.getElementById(id).classList.remove("active"); }

    function formatEpoch(ms) {
      if (!ms) return "-";
      const ist = new Date(ms + (new Date(ms).getTimezoneOffset() * 60000) + 5.5 * 3600000);
      return ist.toISOString().replace("T", " ").substring(0, 19);
    }

    function toEpochMs(val) {
      if (!val) return null;
      return new Date(val).getTime();
    }

    // --- DASHBOARD ---
    async function loadDashboard() {
      try {
        const [empRes, attRes] = await Promise.all([
          fetch("/employees?page=1&page_size=1"),
          fetch("/attendance?page=1&page_size=8")
        ]);
        if (empRes.ok) {
          const empData = await empRes.json();
          document.getElementById("dash-emp-count").textContent = empData.total;
        }
        if (attRes.ok) {
          const attData = await attRes.json();
          renderDashTable(attData.items);
        }
      } catch (e) {}
    }

    function renderDashTable(items) {
      const tbody = document.getElementById("dash-recent-tbody");
      if (!items || items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 2rem;">No attendance records found.</td></tr>`;
        return;
      }
      tbody.innerHTML = items.map(i => `
        <tr>
          <td><strong style="color: var(--accent-cyan); font-family: monospace;">${i.emp_code}</strong></td>
          <td>${i.date}</td>
          <td><span class="badge badge-${i.status.toLowerCase().replace('_', '-')}">${i.status}</span>${i.half_day ? '<span class="badge badge-half-day">HALF-DAY</span>' : ''}</td>
          <td>${i.punch_in ? formatEpoch(i.punch_in).split(' ')[1] : '-'}</td>
          <td>${i.punch_out ? formatEpoch(i.punch_out).split(' ')[1] : '-'}</td>
          <td>${i.work_hours !== null && i.work_hours !== undefined ? i.work_hours + 'h' : '-'}</td>
          <td>${i.late_minutes > 0 ? '<span style="color: var(--accent-amber); font-weight: 700;">+' + i.late_minutes + 'm</span>' : '0'}</td>
        </tr>
      `).join("");
    }

    // --- EMPLOYEES ---
    async function loadEmployees(page = 1) {
      const dept = document.getElementById("emp-filter-dept").value.trim();
      let url = `/employees?page=${page}&page_size=20`;
      if (dept) url += `&department=${encodeURIComponent(dept)}`;
      try {
        const res = await fetch(url);
        const data = await res.json();
        const tbody = document.getElementById("emp-tbody");
        if (!data.items || data.items.length === 0) {
          tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 2rem;">No employees found.</td></tr>`;
          return;
        }
        tbody.innerHTML = data.items.map(e => `
          <tr>
            <td><strong style="color: var(--accent-cyan); font-family: monospace;">${e.emp_code}</strong></td>
            <td><strong>${e.name}</strong></td>
            <td style="color: var(--text-muted);">${e.email}</td>
            <td><span class="badge" style="background: rgba(99,102,241,0.15); color: var(--primary);">${e.department}</span></td>
            <td>${e.shift_start} &ndash; ${e.shift_end}</td>
            <td>${e.joined_on}</td>
            <td style="color: var(--text-dim); font-size: 0.8rem;">${formatEpoch(e.created_at)}</td>
          </tr>
        `).join("");
        document.getElementById("emp-page-info").textContent = `Showing ${data.items.length} of ${data.total} employees`;
        const totalP = Math.max(1, Math.ceil(data.total / 20));
        document.getElementById("emp-page-controls").innerHTML = `
          ${page > 1 ? `<button class="btn btn-outline btn-sm" onclick="loadEmployees(${page - 1})">&larr; Prev</button>` : ''}
          <span style="padding: 0.35rem 0.75rem;">Page ${page} of ${totalP}</span>
          ${page < totalP ? `<button class="btn btn-outline btn-sm" onclick="loadEmployees(${page + 1})">Next &rarr;</button>` : ''}
        `;
      } catch (e) { showToast("Failed to load employees", "error"); }
    }

    async function submitCreateEmployee(e) {
      e.preventDefault();
      const f = e.target;
      const payload = {
        emp_code: f.emp_code.value.trim().toUpperCase(),
        name: f.name.value.trim(),
        email: f.email.value.trim(),
        department: f.department.value.trim(),
        shift_start: f.shift_start.value,
        shift_end: f.shift_end.value,
        joined_on: f.joined_on.value
      };
      try {
        const res = await fetch("/employees", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Employee ${data.emp_code} created!`, "success");
          closeModal("modal-add-employee");
          loadEmployees(1);
          loadDashboard();
        } else {
          showToast(data.detail || "Error creating employee", "error");
        }
      } catch (err) { showToast("Network error", "error"); }
    }

    // --- ATTENDANCE ---
    async function loadAttendance(page = 1) {
      const emp = document.getElementById("att-filter-emp").value.trim();
      const from_d = document.getElementById("att-filter-from").value;
      const to_d = document.getElementById("att-filter-to").value;
      const status = document.getElementById("att-filter-status").value;

      let p = new URLSearchParams({ page: page, page_size: 20 });
      if (emp) p.append("emp_code", emp);
      if (from_d) p.append("date_from", from_d);
      if (to_d) p.append("date_to", to_d);
      if (status) p.append("status", status);

      try {
        const res = await fetch(`/attendance?${p.toString()}`);
        const data = await res.json();
        const tbody = document.getElementById("att-tbody");
        if (!data.items || data.items.length === 0) {
          tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; padding: 2rem;">No attendance records found.</td></tr>`;
          return;
        }
        tbody.innerHTML = data.items.map(i => `
          <tr>
            <td><strong style="color: var(--accent-cyan); font-family: monospace;">${i.emp_code}</strong></td>
            <td><strong>${i.date}</strong></td>
            <td><span class="badge badge-${i.status.toLowerCase().replace('_', '-')}">${i.status}</span>${i.half_day ? '<span class="badge badge-half-day">HALF-DAY</span>' : ''}</td>
            <td>${i.punch_in ? formatEpoch(i.punch_in) : '-'}</td>
            <td>${i.punch_out ? formatEpoch(i.punch_out) : '-'}</td>
            <td>${i.work_hours !== null && i.work_hours !== undefined ? '<strong>' + i.work_hours + ' hrs</strong>' : '-'}</td>
            <td>${i.late_minutes > 0 ? '<span style="color: var(--accent-amber); font-weight: 700;">+' + i.late_minutes + 'm</span>' : '0'}</td>
            <td>${i.overtime_minutes > 0 ? '<span style="color: var(--accent-emerald); font-weight: 700;">+' + i.overtime_minutes + 'm</span>' : '0'}</td>
            <td>${i.history && i.history.length > 0 ? `<button class="btn btn-outline btn-sm" onclick='showHistory(${JSON.stringify(i.history)})'>📜 ${i.history.length}</button>` : '<span style="color: var(--text-dim);">-</span>'}</td>
            <td><button class="btn btn-outline btn-sm" onclick='openRegularize(${JSON.stringify(i)})'>✏️ Correct</button></td>
          </tr>
        `).join("");
        document.getElementById("att-page-info").textContent = `Showing ${data.items.length} of ${data.total} logs`;
        const totalP = Math.max(1, Math.ceil(data.total / 20));
        document.getElementById("att-page-controls").innerHTML = `
          ${page > 1 ? `<button class="btn btn-outline btn-sm" onclick="loadAttendance(${page - 1})">&larr; Prev</button>` : ''}
          <span style="padding: 0.35rem 0.75rem;">Page ${page} of ${totalP}</span>
          ${page < totalP ? `<button class="btn btn-outline btn-sm" onclick="loadAttendance(${page + 1})">Next &rarr;</button>` : ''}
        `;
      } catch (e) { showToast("Error loading attendance", "error"); }
    }

    function resetAttendanceFilter() {
      document.getElementById("att-filter-emp").value = "";
      document.getElementById("att-filter-from").value = "";
      document.getElementById("att-filter-to").value = "";
      document.getElementById("att-filter-status").value = "";
      loadAttendance(1);
    }

    // --- PUNCH ACTIONS ---
    async function submitPunchIn(e) {
      e.preventDefault();
      const f = e.target;
      const payload = { emp_code: f.emp_code.value.trim().toUpperCase(), status: f.status.value };
      if (f.custom_time.value) payload.punched_at = toEpochMs(f.custom_time.value);
      try {
        const res = await fetch("/attendance/punch-in", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Punched IN: ${data.emp_code} (Date: ${data.date}, Late: ${data.late_minutes}m)`, "success");
          closeModal("modal-punch-in");
          loadDashboard();
          loadAttendance(1);
        } else { showToast(data.detail || "Punch-in failed", "error"); }
      } catch (err) { showToast("Network error", "error"); }
    }

    async function submitPunchOut(e) {
      e.preventDefault();
      const f = e.target;
      const payload = { emp_code: f.emp_code.value.trim().toUpperCase() };
      if (f.custom_time.value) payload.punched_at = toEpochMs(f.custom_time.value);
      try {
        const res = await fetch("/attendance/punch-out", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Punched OUT: ${data.emp_code} (Hours: ${data.work_hours || 0}h, OT: ${data.overtime_minutes || 0}m)`, "success");
          closeModal("modal-punch-out");
          loadDashboard();
          loadAttendance(1);
        } else { showToast(data.detail || "Punch-out failed", "error"); }
      } catch (err) { showToast("Network error", "error"); }
    }

    // --- REGULARIZE ---
    function openRegularize(log) {
      document.getElementById("reg-emp-code").value = log.emp_code;
      document.getElementById("reg-date").value = log.date;
      document.getElementById("reg-target-display").textContent = `${log.emp_code} on ${log.date}`;
      document.getElementById("reg-status").value = log.status;
      document.getElementById("reg-punch-in").value = "";
      document.getElementById("reg-punch-out").value = "";
      document.getElementById("reg-reason").value = "";
      document.getElementById("reg-by").value = "";
      openModal("modal-regularize");
    }

    async function submitRegularize(e) {
      e.preventDefault();
      const emp_code = document.getElementById("reg-emp-code").value;
      const date = document.getElementById("reg-date").value;
      const payload = {
        status: document.getElementById("reg-status").value,
        reason: document.getElementById("reg-reason").value.trim(),
        regularized_by: document.getElementById("reg-by").value.trim()
      };
      const pi = document.getElementById("reg-punch-in").value;
      const po = document.getElementById("reg-punch-out").value;
      if (pi) payload.punch_in = toEpochMs(pi);
      if (po) payload.punch_out = toEpochMs(po);

      try {
        const res = await fetch(`/attendance/${emp_code}/${date}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Record ${emp_code} (${date}) updated!`, "success");
          closeModal("modal-regularize");
          loadAttendance(1);
        } else { showToast(data.detail || "Regularization failed", "error"); }
      } catch (err) { showToast("Network error", "error"); }
    }

    function showHistory(hist) {
      const body = document.getElementById("history-modal-body");
      body.innerHTML = hist.map((h, i) => `
        <div style="background: rgba(15,23,42,0.6); border: 1px solid var(--border-glass); border-radius: var(--radius-sm); padding: 1rem; margin-bottom: 0.75rem;">
          <div style="display: flex; justify-content: space-between; margin-bottom: 0.5rem;">
            <strong style="color: var(--accent-cyan);">#${i + 1} By: ${h.by}</strong>
            <span style="font-size: 0.78rem; color: var(--text-dim);">${formatEpoch(h.at)}</span>
          </div>
          <div style="font-size: 0.85rem; margin-bottom: 0.5rem;">"${h.reason}"</div>
          <pre class="json-view" style="font-size: 0.75rem; max-height: 120px;">${JSON.stringify(h.changes, null, 2)}</pre>
        </div>
      `).join("");
      openModal("modal-history");
    }

    // --- ANALYTICS ---
    async function queryMonthlyAnalytics() {
      const emp = document.getElementById("ana-m-emp").value.trim();
      const month = document.getElementById("ana-m-month").value;
      try {
        const res = await fetch(`/analytics/employees/${emp}/monthly?month=${month}`);
        const data = await res.json();
        if (res.ok) {
          document.getElementById("ana-m-results").style.display = "block";
          document.getElementById("m-val-pct").textContent = data.attendance_pct !== null ? data.attendance_pct + '%' : 'N/A';
          document.getElementById("m-val-work").textContent = data.working_days;
          document.getElementById("m-val-pres").textContent = `${data.present_days} d (${data.leave_days} leaves)`;
          document.getElementById("m-val-late").textContent = `${data.total_late_minutes}m (${data.late_count} times)`;
          showToast("Monthly analytics loaded", "success");
        } else { showToast(data.detail || "Error", "error"); }
      } catch (e) { showToast("Server error", "error"); }
    }

    async function queryDeptSummary() {
      const month = document.getElementById("ana-d-month").value;
      const dept = document.getElementById("ana-d-dept").value.trim();
      let url = `/analytics/departments/summary?month=${month}`;
      if (dept) url += `&department=${encodeURIComponent(dept)}`;
      try {
        const res = await fetch(url);
        const data = await res.json();
        const wrap = document.getElementById("ana-d-table-wrap");
        if (!data.items || data.items.length === 0) {
          wrap.innerHTML = `<p style="padding: 1.5rem; text-align: center;">No department records.</p>`;
          return;
        }
        wrap.innerHTML = `
          <table class="data-table">
            <thead><tr><th>Department</th><th>Headcount</th><th>Present Days</th><th>Avg Work Hours</th><th>Late Count</th><th>Total Late Mins</th><th>Leaves</th></tr></thead>
            <tbody>
              ${data.items.map(d => `
                <tr>
                  <td><strong style="color: var(--accent-cyan);">${d.department}</strong></td>
                  <td><strong>${d.headcount}</strong></td>
                  <td>${d.present_days}</td>
                  <td>${d.avg_work_hours !== null ? d.avg_work_hours + 'h' : '-'}</td>
                  <td>${d.late_count}</td>
                  <td><span style="color: var(--accent-amber); font-weight: 700;">${d.total_late_minutes}m</span></td>
                  <td>${d.leave_count}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `;
        showToast("Department summary loaded", "success");
      } catch (e) { showToast("Server error", "error"); }
    }

    async function queryLeaderboard() {
      const month = document.getElementById("ana-l-month").value;
      const limit = document.getElementById("ana-l-limit").value;
      try {
        const res = await fetch(`/analytics/leaderboard/late?month=${month}&limit=${limit}`);
        const data = await res.json();
        const wrap = document.getElementById("ana-l-table-wrap");
        if (!data.items || data.items.length === 0) {
          wrap.innerHTML = `<p style="padding: 1.5rem; text-align: center;">No late records for this period.</p>`;
          return;
        }
        wrap.innerHTML = `
          <table class="data-table">
            <thead><tr><th>Rank</th><th>Employee</th><th>Name</th><th>Department</th><th>Total Late Mins</th><th>Late Days</th></tr></thead>
            <tbody>
              ${data.items.map(r => `
                <tr>
                  <td><strong>${r.rank === 1 ? '🥇 ' : r.rank === 2 ? '🥈 ' : r.rank === 3 ? '🥉 ' : ''}#${r.rank}</strong></td>
                  <td><strong style="color: var(--accent-cyan); font-family: monospace;">${r.emp_code}</strong></td>
                  <td>${r.name}</td>
                  <td><span class="badge" style="background: rgba(99,102,241,0.15); color: var(--primary);">${r.department}</span></td>
                  <td><strong style="color: var(--accent-rose);">${r.total_late_minutes}m</strong></td>
                  <td>${r.late_count}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `;
        showToast("Leaderboard loaded", "success");
      } catch (e) { showToast("Server error", "error"); }
    }

    async function queryTrend() {
      const dept = document.getElementById("ana-t-dept").value.trim();
      const from_d = document.getElementById("ana-t-from").value;
      const to_d = document.getElementById("ana-t-to").value;
      try {
        const res = await fetch(`/analytics/departments/${encodeURIComponent(dept)}/trend?from=${from_d}&to=${to_d}`);
        const data = await res.json();
        if (!res.ok) return showToast(data.detail || "Error", "error");

        const labels = data.items.map(i => i.date);
        const rates = data.items.map(i => i.attendance_rate !== null ? i.attendance_rate * 100 : null);
        const movAvgs = data.items.map(i => i.moving_avg_7d !== null ? i.moving_avg_7d * 100 : null);

        if (trendChartObj) trendChartObj.destroy();
        const ctx = document.getElementById("trendChartCanvas").getContext("2d");
        trendChartObj = new Chart(ctx, {
          type: "line",
          data: {
            labels: labels,
            datasets: [
              { label: "Daily Rate (%)", data: rates, borderColor: "#06b6d4", backgroundColor: "rgba(6,182,212,0.1)", fill: true, tension: 0.2 },
              { label: "7-Day Moving Avg (%)", data: movAvgs, borderColor: "#6366f1", borderDash: [5, 5], fill: false, tension: 0.3 }
            ]
          },
          options: {
            responsive: true,
            plugins: { legend: { labels: { color: "#cbd5e1" } } },
            scales: {
              x: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(255,255,255,0.05)" } },
              y: { min: 0, max: 100, ticks: { color: "#94a3b8" }, grid: { color: "rgba(255,255,255,0.05)" } }
            }
          }
        });

        const wrap = document.getElementById("ana-t-table-wrap");
        wrap.innerHTML = `
          <table class="data-table">
            <thead><tr><th>Date</th><th>Working Day</th><th>Headcount</th><th>Present Count</th><th>Rate</th><th>7d Moving Avg</th></tr></thead>
            <tbody>
              ${data.items.map(i => `
                <tr>
                  <td><strong>${i.date}</strong></td>
                  <td>${i.is_working_day ? '✅ Yes' : '<span style="color: var(--text-dim);">Weekend</span>'}</td>
                  <td>${i.headcount}</td>
                  <td>${i.present_count}</td>
                  <td>${i.attendance_rate !== null ? (i.attendance_rate * 100).toFixed(2) + '%' : '-'}</td>
                  <td><strong style="color: var(--primary);">${i.moving_avg_7d !== null ? (i.moving_avg_7d * 100).toFixed(2) + '%' : '-'}</strong></td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `;
        showToast("Trend analysis plotted", "success");
      } catch (e) { showToast("Server error", "error"); }
    }

    // --- QUERY EXPLAIN ---
    async function runQueryExplain() {
      const ep = document.getElementById("exp-select").value;
      let url = `/admin/explain/${ep}`;
      if (ep === "employee_monthly") url += "?emp_code=EMP0001&month=2026-07";
      else if (ep === "department_summary") url += "?month=2026-07";
      else if (ep === "late_leaderboard") url += "?month=2026-07";
      else if (ep === "department_trend") url += "?department=Engineering&from=2026-07-01&to=2026-07-31";

      try {
        const res = await fetch(url);
        const data = await res.json();
        if (res.ok) {
          document.getElementById("exp-output-wrap").style.display = "block";
          document.getElementById("exp-res-coll").textContent = data.collection;
          const jsonStr = JSON.stringify(data.explain, null, 2);
          document.getElementById("exp-res-json").textContent = jsonStr;
          const stageEl = document.getElementById("exp-res-stage");
          if (jsonStr.includes("IXSCAN")) {
            stageEl.innerHTML = `<span style="color: var(--accent-emerald);">IXSCAN (Indexed Plan - High Performance)</span>`;
          } else if (jsonStr.includes("COLLSCAN")) {
            stageEl.innerHTML = `<span style="color: var(--accent-amber);">COLLSCAN (Collection Scan)</span>`;
          } else {
            stageEl.innerHTML = `<span style="color: var(--accent-cyan);">Aggregation Pipeline</span>`;
          }
          showToast("Explain executed", "success");
        } else { showToast(data.detail || "Error", "error"); }
      } catch (e) { showToast("Server error", "error"); }
    }
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    """Serve the complete interactive Web UI directly on port 8000."""
    return HTMLResponse(content=INDEX_HTML)


# 1. GET /health
@app.get("/health")
def health() -> dict:
    """Readiness probe: returns 200 only if MongoDB responds to ping."""
    try:
        db.command("ping")
        ensure_indexes()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


# 2. POST /employees
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict:
    """Create a new employee. emp_code uniqueness enforced by DB unique index."""
    now = now_utc_truncated()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409, f"Employee with emp_code {body.emp_code!r} already exists"
        )
    return serialize_employee(doc)


# 3. GET /employees
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
) -> dict:
    """List employees, sorted by emp_code ascending, with optional department filter."""
    query: dict = {}
    if department is not None:
        query["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# 4. POST /attendance/punch-in
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict:
    """
    Record a punch-in. Uses unique (emp_code, date) index to prevent duplicates atomically.
    """
    emp = get_employee_or_404(body.emp_code)

    # Resolve timestamp
    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_utc = now_utc_truncated()

    # Compute attendance date (R1, IST, overnight handling)
    att_date = attendance_date_for_punch_in(
        punch_in_utc, emp["shift_start"], emp["shift_end"]
    )

    # Compute late_minutes (R2)
    late_min = calc_late_minutes(punch_in_utc, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            f"Attendance record already exists for {body.emp_code!r} on {att_date}",
        )

    return serialize_attendance(doc)


# 5. GET /attendance
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
) -> dict:
    """List attendance records with filters. All filtering/sorting done in MongoDB."""
    if date_from is not None:
        validate_date_str(date_from, "date_from")
    if date_to is not None:
        validate_date_str(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(ALL_STATUSES)}")

    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# 6. POST /attendance/punch-out
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict:
    """
    Close the most recent open punch-in record for the employee.
    Atomically updates using a filter that requires punch_out to be null.
    """
    emp = get_employee_or_404(body.emp_code)

    # Resolve timestamp
    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_utc = now_utc_truncated()

    # Find the most recent open punch-in record (punch_out is null, punch_in <= punch_out_utc)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENCE_STATUSES)},
            "punch_in": {"$ne": None, "$lte": punch_out_utc},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        # Distinguish between no punch-in at all vs already punched out
        latest = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": {"$in": list(PRESENCE_STATUSES)},
                "punch_in": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if latest is None:
            raise HTTPException(404, f"No punch-in record found for {body.emp_code!r}")
        if latest.get("punch_out") is not None:
            raise HTTPException(409, "Record is already punched out")
        # punch_in is after punch_out_utc
        raise HTTPException(
            404,
            f"No open punch-in record found for {body.emp_code!r} at or before the given time",
        )

    punch_in_utc: datetime = record["punch_in"]
    att_date: str = record["date"]

    # Validate punch_out > punch_in
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, "punch_out must be after punch_in")

    # Validate punch_out within 24 hours of punch_in
    if (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Compute derived fields
    wh = calc_work_hours(punch_in_utc, punch_out_utc)
    ot = calc_overtime_minutes(
        punch_out_utc, att_date, emp["shift_start"], emp["shift_end"]
    )
    hd = calc_half_day(wh)

    # Atomic update: only succeed if punch_out is still null (prevents double punch-out)
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,  # atomic guard against concurrent punch-out
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=True,
        projection={"_id": 0},
    )

    if updated is None:
        raise HTTPException(409, "Record was already punched out by a concurrent request")

    return serialize_attendance(updated)


# 7. PATCH /attendance/{emp_code}/{date}
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
) -> dict:
    """
    Manually correct one attendance record. Appends a history entry.
    Uses optimistic concurrency via history length check.
    """
    validate_date_str(date, "date")
    emp = get_employee_or_404(emp_code)

    # Fetch current record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(
            404, f"No attendance record for {emp_code!r} on {date!r}"
        )

    orig_status: str = record["status"]
    orig_punch_in: Optional[datetime] = record.get("punch_in")
    orig_punch_out: Optional[datetime] = record.get("punch_out")
    orig_work_hours: Optional[float] = record.get("work_hours")
    orig_late_minutes: int = record.get("late_minutes", 0)
    orig_overtime_minutes: int = record.get("overtime_minutes", 0)
    orig_half_day: bool = record.get("half_day", False)
    orig_history_len: int = len(record.get("history") or [])

    # Determine new values (start with originals, apply any changes from request)
    new_status: str = body.status if body.status is not None else orig_status

    # --- Validate ABSENT/LEAVE rules ---
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None:
            raise HTTPException(
                422, f"punch_in cannot be set when status is {new_status}"
            )
        if body.punch_out is not None:
            raise HTTPException(
                422, f"punch_out cannot be set when status is {new_status}"
            )
        new_punch_in: Optional[datetime] = None
        new_punch_out: Optional[datetime] = None
    else:
        # Presence status — resolve punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            new_punch_in = orig_punch_in

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            new_punch_out = orig_punch_out

        # Presence status requires punch_in
        if new_punch_in is None:
            raise HTTPException(
                422, f"punch_in is required for presence status {new_status!r}"
            )

        # Validate punch_in is on the attendance date (R1)
        expected_date = attendance_date_for_punch_in(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if expected_date != date:
            raise HTTPException(
                422,
                f"punch_in maps to date {expected_date!r} but attendance date is {date!r} (R1)",
            )

        # Validate punch_out constraints
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    422, "punch_out must be within 24 hours of punch_in"
                )

    # Recompute derived fields from final values
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes: int = 0
        new_work_hours: Optional[float] = None
        new_overtime_minutes: int = 0
        new_half_day: bool = False
    else:
        new_late_minutes = calc_late_minutes(new_punch_in, date, emp["shift_start"])
        if new_punch_out is not None:
            new_work_hours = calc_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = calc_overtime_minutes(
                new_punch_out, date, emp["shift_start"], emp["shift_end"]
            )
            new_half_day = calc_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Build changes dict — only fields that actually changed
    changes: dict = {}

    if new_status != orig_status:
        changes["status"] = {"from": orig_status, "to": new_status}

    if new_punch_in != orig_punch_in:
        changes["punch_in"] = {
            "from": datetime_to_epoch_ms(orig_punch_in),
            "to": datetime_to_epoch_ms(new_punch_in),
        }

    if new_punch_out != orig_punch_out:
        changes["punch_out"] = {
            "from": datetime_to_epoch_ms(orig_punch_out),
            "to": datetime_to_epoch_ms(new_punch_out),
        }

    if new_work_hours != orig_work_hours:
        changes["work_hours"] = {"from": orig_work_hours, "to": new_work_hours}

    if new_late_minutes != orig_late_minutes:
        changes["late_minutes"] = {"from": orig_late_minutes, "to": new_late_minutes}

    if new_overtime_minutes != orig_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": orig_overtime_minutes, "to": new_overtime_minutes
        }

    if new_half_day != orig_half_day:
        changes["half_day"] = {"from": orig_half_day, "to": new_half_day}

    # Reject no-op
    if not changes:
        raise HTTPException(422, "Request would not change any field")

    # Build history entry — store punch timestamps as BSON datetime in MongoDB
    history_changes_db: dict = {}
    for field, change in changes.items():
        if field in ("punch_in", "punch_out"):
            # Convert epoch ms back to datetime for storage
            from_val = (
                datetime.fromtimestamp(change["from"] / 1000, tz=timezone.utc)
                if change["from"] is not None
                else None
            )
            to_val = (
                datetime.fromtimestamp(change["to"] / 1000, tz=timezone.utc)
                if change["to"] is not None
                else None
            )
            history_changes_db[field] = {"from": from_val, "to": to_val}
        else:
            history_changes_db[field] = change

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes_db,
    }

    # Atomic update with optimistic concurrency check on history length
    update_filter = {
        "emp_code": emp_code,
        "date": date,
        "$expr": {
            "$eq": [
                {"$size": {"$ifNull": ["$history", []]}},
                orig_history_len,
            ]
        },
    }
    set_doc: dict = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": set_doc,
            "$push": {"history": history_entry},
        },
        return_document=True,
        projection={"_id": 0},
    )

    if updated is None:
        raise HTTPException(
            409, "Concurrent regularization detected; please retry"
        )

    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def weekday_count_from_date(from_date_str: str, to_date_str: str) -> int:
    """Count Mon-Fri weekdays between from_date and to_date, inclusive."""
    from_d = date_type.fromisoformat(from_date_str)
    to_d = date_type.fromisoformat(to_date_str)
    if to_d < from_d:
        return 0
    count = 0
    current = from_d
    while current <= to_d:
        if current.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        current += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# 8. GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
) -> dict:
    """Monthly attendance summary for one employee. Uses MongoDB aggregation."""
    validate_month_str(month)
    emp = get_employee_or_404(emp_code)

    pipeline = build_employee_monthly_pipeline(emp_code, month)

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        row = results[0]
        present_days = round2(row["present_days"])
        leave_days = int(row["leave_days"])
        late_count = int(row["late_count"])
        total_late_minutes = int(row["total_late_minutes"])
        total_overtime_minutes = int(row["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round2(present_days / working_days * 100)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# 9. GET /analytics/departments/summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict:
    """Department-level monthly summary. Cross-collection aggregation from employees."""
    validate_month_str(month)
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for row in results:
        if row["headcount"] == 0:
            continue
        cnt = row["avg_work_hours_count"]
        avg_wh = round2(row["avg_work_hours_sum"] / cnt) if cnt > 0 else None
        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": round2(row["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# 10. GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict:
    """Late-comers leaderboard with standard competition ranking."""
    validate_month_str(month)
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    rows = list(db.attendance_logs.aggregate(pipeline))

    # Rank is computed by $setWindowFields/$rank in the pipeline;
    # filter rows where rank <= limit
    items = [r for r in rows if r.get("rank", 1) <= limit]

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# 11. GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> dict:
    """
    Daily attendance trend for a department, one row per calendar day.
    Uses $densify for gap-filling. Moving average computed via $setWindowFields in the pipeline.
    """
    validate_date_str(from_date, "from")
    validate_date_str(to_date, "to")

    from_d = date_type.fromisoformat(from_date)
    to_d = date_type.fromisoformat(to_date)

    if to_d < from_d:
        raise HTTPException(422, "to must not be before from")
    if (to_d - from_d).days >= 92:
        raise HTTPException(422, "Date range must not exceed 92 days")

    # Verify department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, f"Department {department!r} not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)

    rows = list(db.attendance_logs.aggregate(pipeline))

    return {"department": department, "items": [
        {
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": row.get("attendance_rate"),
            "moving_avg_7d": row.get("moving_avg_7d"),
        }
        for row in rows
    ]}


# ---------------------------------------------------------------------------
# 12. GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------
VALID_EXPLAIN_ENDPOINTS = frozenset({
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Return MongoDB explain(executionStats) for the named endpoint's main query."""
    if endpoint not in VALID_EXPLAIN_ENDPOINTS:
        raise HTTPException(
            422,
            f"endpoint must be one of {sorted(VALID_EXPLAIN_ENDPOINTS)}",
        )

    if endpoint == "attendance_list":
        query: dict = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                query["date"]["$gte"] = date_from
            if date_to is not None:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status

        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_result = db.command(
            "explain", command, verbosity="executionStats"
        )
        return make_json_safe({
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        })

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        validate_month_str(month)
        first_day, last_day = month_range(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day, "$lte": last_day},
                }
            },
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return make_json_safe({
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        })

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        validate_month_str(month)
        first_day, last_day = month_range(month)
        emp_match: dict = {"joined_on": {"$lte": last_day}}
        if department is not None:
            emp_match["department"] = department
        pipeline = [{"$match": emp_match}]
        explain_result = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return make_json_safe({
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_result,
        })

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        validate_month_str(month)
        first_day, last_day = month_range(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day, "$lte": last_day},
                    "late_minutes": {"$gt": 0},
                }
            },
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return make_json_safe({
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        })

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_date:
            raise HTTPException(422, "from is required for department_trend")
        if not to_date:
            raise HTTPException(422, "to is required for department_trend")
        validate_date_str(from_date, "from")
        validate_date_str(to_date, "to")
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return make_json_safe({
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        })

    raise HTTPException(422, "Unhandled endpoint")
