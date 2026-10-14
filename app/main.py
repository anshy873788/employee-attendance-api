"""
Employee Attendance & Analytics API - STARTER

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


def split_shift_time(value: str) -> tuple[int, int]:
    return tuple(map(int, value.split(":")))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return split_shift_time(shift_end) <= split_shift_time(shift_start)


def ist_from_timestamp(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).astimezone(IST).replace(microsecond=0)


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(IST)


def attendance_date(
    punched_at: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    local_time = punched_at.astimezone(IST)
    attendance_day = local_time.date()

    if is_overnight_shift(shift_start, shift_end):
        end_hour, end_minute = split_shift_time(shift_end)

        if (local_time.hour, local_time.minute) < (end_hour, end_minute):
            attendance_day -= timedelta(days=1)

    return attendance_day.isoformat()


def round_half_up(value: float, places: int = 2) -> float:
    precision = Decimal("1").scaleb(-places)

    return float(
        Decimal(str(value)).quantize(
            precision,
            rounding=ROUND_HALF_UP,
        )
    )


def month_bounds(month: str) -> tuple[str, str]:
    try:
        year, month_number = map(int, month.split("-"))
        first_day = date(year, month_number, 1)

        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)

        return first_day.isoformat(), (next_month - timedelta(days=1)).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(422, "month must be YYYY-MM")


def working_days_in_period(start_date: str, end_date: str) -> int:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    total = 0

    while current <= end:
        if current.weekday() < 5:
            total += 1

        current += timedelta(days=1)

    return total


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    hour, minute = split_shift_time(shift_start)

    start = punch_in.astimezone(IST).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    if is_overnight_shift(shift_start, shift_end):
        end_hour, end_minute = split_shift_time(shift_end)

        if (punch_in.hour, punch_in.minute) < (end_hour, end_minute):
            start -= timedelta(days=1)

    elapsed_seconds = (punch_in - start).total_seconds()

    if elapsed_seconds <= 10 * 60:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(hours)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    end_hour, end_minute = split_shift_time(shift_end)

    shift_end_time = datetime.fromisoformat(date_str).replace(
        hour=end_hour,
        minute=end_minute,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )

    if is_overnight_shift(shift_start, shift_end):
        shift_end_time += timedelta(days=1)

    overtime_seconds = (punch_out - shift_end_time).total_seconds()

    if overtime_seconds < 30 * 60:
        return 0

    return int(overtime_seconds // 60)


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(IST).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)

    return doc
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "status must be PRESENT, WFH, or ON_DUTY")

    punched_at = (
        ist_from_timestamp(body.punched_at)
        if body.punched_at
        else datetime.now(IST).replace(microsecond=0)
    )

    attendance_day = attendance_date(
        punched_at,
        emp["shift_start"],
        emp["shift_end"],
    )

    if db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "date": attendance_day}
    ):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
         result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)

    return doc

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    punched_at = (
        ist_from_timestamp(body.punched_at)
        if body.punched_at
        else datetime.now(IST).replace(microsecond=0)
    )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")

    punch_in_time = as_ist(record["punch_in"])

    if punched_at <= punch_in_time:
        raise HTTPException(422, "punch-out must be after punch-in")

    if punched_at > punch_in_time + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be within 24 hours of punch-in")

    work_hours = compute_work_hours(punch_in_time, punched_at)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": compute_overtime(
                    punched_at,
                    emp["shift_start"],
                    emp["shift_end"],
                    record["date"],
                ),
                "half_day": work_hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, "attendance record is already punched out")

    updated["id"] = str(updated.pop("_id"))
    return updated


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )

    if not record:
        raise HTTPException(404, "attendance record not found")

    provided = body.model_fields_set
    status = body.status if "status" in provided else record["status"]

    if status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
        raise HTTPException(422, "invalid status")

    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in provided or "punch_out" in provided:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot include punch times",
            )

        values = {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        if "punch_in" in provided:
            if body.punch_in is None:
                raise HTTPException(422, "presence status requires punch_in")

            punch_in_time = ist_from_timestamp(body.punch_in)
        elif record.get("punch_in") is not None:
            punch_in_time = as_ist(record["punch_in"])
        else:
            raise HTTPException(422, "presence status requires punch_in")

        if attendance_date(
            punch_in_time,
            emp["shift_start"],
            emp["shift_end"],
        ) != date:
            raise HTTPException(
                422,
                "punch_in must belong to the attendance date",
            )

        if "punch_out" in provided:
            punch_out_time = (
                ist_from_timestamp(body.punch_out)
                if body.punch_out is not None
                else None
            )
        elif record.get("punch_out") is not None:
            punch_out_time = as_ist(record["punch_out"])
        else:
            punch_out_time = None

        if punch_out_time is not None:
            if punch_out_time <= punch_in_time:
                raise HTTPException(422, "punch_out must be after punch_in")

            if punch_out_time > punch_in_time + timedelta(hours=24):
                raise HTTPException(
                    422,
                    "punch_out must be within 24 hours of punch_in",
                )

            work_hours = compute_work_hours(punch_in_time, punch_out_time)
            overtime_minutes = compute_overtime(
                punch_out_time,
                emp["shift_start"],
                emp["shift_end"],
                date,
            )
            half_day = work_hours < 4.50
        else:
            work_hours = None
            overtime_minutes = 0
            half_day = False

        values = {
            "status": status,
            "punch_in": punch_in_time,
            "punch_out": punch_out_time,
            "work_hours": work_hours,
            "late_minutes": compute_late_minutes(
                punch_in_time,
                emp["shift_start"],
                emp["shift_end"],
            ),
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }

    changes = {}

    for field, new_value in values.items():
        old_value = record.get(field)

        if isinstance(old_value, datetime):
            old_value_for_compare = as_ist(old_value)
        else:
            old_value_for_compare = old_value

        if old_value_for_compare != new_value:
            changes[field] = {
                "from": record.get(field),
                "to": new_value,
            }

    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(IST).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    updated["id"] = str(updated.pop("_id"))
    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        query["status"] = status

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")

    total = db.attendance_logs.count_documents(query)

    items = list(
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        item["id"] = str(item.pop("_id"))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:
        raise HTTPException(404, "employee not found")

    if len(month) != 7 or month[4] != "-":
        raise HTTPException(422, "month must be YYYY-MM")

    month_start, month_end = month_bounds(month)

    working_start = max(month_start, employee["joined_on"])

    if working_start > month_end:
        working_days = 0
    else:
        working_days = working_days_in_period(
            working_start,
            month_end,
        )

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start,
                    "$lte": month_end,
                },
            }
        },
        {
            "$addFields": {
                "weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$weekday", 2]},
                                    {"$lte": ["$weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
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
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    totals = result[0] if result else {}

    present_days = totals.get("present_days", 0)

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 4)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get(
            "total_overtime_minutes",
            0,
        ),
        "attendance_pct": attendance_pct,
    }
@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(422, "month must be YYYY-MM")

    month_start, month_end = month_bounds(month)

    employee_match = {
        "joined_on": {"$lte": month_end},
    }

    if department:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {
                                "$gte": month_start,
                                "$lte": month_end,
                            },
                        }
                    },
                    {
                        "$addFields": {
                            "weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$logs.weekday", 2]},
                                    {"$lte": ["$logs.weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {
                                                "$ifNull": [
                                                    "$logs.half_day",
                                                    False,
                                                ]
                                            },
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$logs.late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$logs.late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
                "work_hours_total": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {
                    "$sum": "$total_late_minutes"
                },
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hours_total": {
                    "$sum": "$work_hours_total"
                },
                "work_hours_count": {
                    "$sum": "$work_hours_count"
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                "$work_hours_total",
                                "$work_hours_count",
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(
                item["avg_work_hours"]
            )

    return {
        "month": month,
        "items": items,
    }
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(422, "month must be YYYY-MM")

    month_start, month_end = month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start,
                    "$lte": month_end,
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend(
        [
            {
                "$group": {
                    "_id": {
                        "emp_code": "$emp_code",
                        "name": "$employee.name",
                        "department": "$employee.department",
                    },
                    "total_late_minutes": {
                        "$sum": "$late_minutes"
                    },
                    "late_count": {"$sum": 1},
                }
            },
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
                    "emp_code": "$_id.emp_code",
                    "name": "$_id.name",
                    "department": "$_id.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ]
    )

    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    try:
        start_day = date.fromisoformat(from_)
        end_day = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")

    if end_day < start_day:
        raise HTTPException(422, "to must be on or after from")

    if (end_day - start_day).days > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    start_datetime = datetime.combine(
        start_day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    end_exclusive = datetime.combine(
        end_day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": from_,
                    "$lte": to,
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$project": {
                "day": {
                    "$dateFromString": {
                        "dateString": "$date",
                    }
                },
                "present_count": {
                    "$cond": [
                        {
                            "$in": [
                                "$status",
                                ["PRESENT", "WFH", "ON_DUTY"],
                            ]
                        },
                        {
                            "$cond": [
                                {
                                    "$eq": [
                                        {
                                            "$ifNull": [
                                                "$half_day",
                                                False,
                                            ]
                                        },
                                        True,
                                    ]
                                },
                                0.5,
                                1,
                            ]
                        },
                        0,
                    ]
                },
                "late_count": {
                    "$cond": [
                        {
                            "$gt": [
                                {
                                    "$ifNull": [
                                        "$late_minutes",
                                        0,
                                    ]
                                },
                                0,
                            ]
                        },
                        1,
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$day",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "day": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "day": {"$literal": start_datetime},
                            "present_count": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$day",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "day": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_datetime, end_exclusive],
                },
            }
        },
        {
            "$fill": {
                "sortBy": {"day": 1},
                "output": {
                    "present_count": {"value": 0},
                    "late_count": {"value": 0},
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "day_text": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$day",
                            "timezone": "UTC",
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$department",
                                            department,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$$day_text",
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
                "as": "headcount_rows",
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$headcount_rows"},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                        "timezone": "UTC",
                    }
                },
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

    items = list(db.attendance_logs.aggregate(pipeline))

    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(
                item["attendance_rate"],
                4,
            )

        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(
                item["moving_avg_7d"],
                4,
            )

    return {
        "department": department,
        "items": items,
    }
db = client[os.getenv("MONGO_DB", "attendance_db")]
db.employees.create_index(
    [("emp_code", 1)],
    unique=True,
    name="employees_emp_code_unique",
)

db.employees.create_index(
    [("department", 1), ("joined_on", 1)],
    name="employees_department_joined_on",
)

db.attendance_logs.create_index(
    [("emp_code", 1), ("date", 1)],
    unique=True,
    name="attendance_emp_code_date_unique",
)

db.attendance_logs.create_index(
    [("date", -1), ("emp_code", 1)],
    name="attendance_date_emp_code",
)

db.attendance_logs.create_index(
    [("emp_code", 1), ("punch_in", -1)],
    name="attendance_emp_code_punch_in",
)

db.attendance_logs.create_index(
    [("date", 1), ("late_minutes", -1)],
    name="attendance_date_late_minutes",
)
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in valid_endpoints:
        raise HTTPException(422, "unknown endpoint")

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        if status:
            query["status"] = status

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {
                    "date": -1,
                    "emp_code": 1,
                },
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422,
                "emp_code and month are required",
            )

        month_start, month_end = month_bounds(month)

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "emp_code": emp_code,
                            "date": {
                                "$gte": month_start,
                                "$lte": month_end,
                            },
                        }
                    }
                ],
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        _, month_end = month_bounds(month)

        query = {
            "joined_on": {"$lte": month_end},
        }

        if department:
            query["department"] = department

        explain = db.command(
            "explain",
            {
                "find": "employees",
                "filter": query,
                "sort": {"department": 1},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain,
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        month_start, month_end = month_bounds(month)

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": month_start,
                                "$lte": month_end,
                            },
                            "late_minutes": {"$gt": 0},
                        }
                    }
                ],
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if not department or not from_ or not to:
        raise HTTPException(
            422,
            "department, from, and to are required",
        )

    explain = db.command(
        "explain",
        {
            "find": "attendance_logs",
            "filter": {
                "date": {
                    "$gte": from_,
                    "$lte": to,
                }
            },
        },
        verbosity="executionStats",
    )

    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": explain,
    }   
# --------------------------------------------------------------------------- #
# TODO:
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #