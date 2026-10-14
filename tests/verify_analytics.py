"""
Self-check for the analytics + explain endpoints, against a REAL MongoDB.

It seeds an awkward dataset into a separate database (attendance_verify, never your real one),
computes every expected answer with plain Python (an independent oracle written from the spec),
calls the API in-process and compares.

Run from the repo root (MongoDB must be running):
    pip install httpx
    python tests/verify_analytics.py
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

os.environ["MONGO_DB"] = "attendance_verify"  # set before importing the app; .env never overrides it
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

assert m.db.name == "attendance_verify", "refusing to touch any other database"
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- seed data
def emp(code, dept, joined, start="09:30", end="18:30"):
    return dict(emp_code=code, name=f"Name {code}", email=f"{code.lower()}@x.com", department=dept,
                shift_start=start, shift_end=end, joined_on=joined, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


EMPLOYEES = [
    emp("EMP0001", "Eng", "2026-01-05"),
    emp("EMP0002", "Eng", "2026-01-05"),
    emp("EMP0003", "Eng", "2026-07-15"),            # mid-month joiner
    emp("EMP0004", "Eng", "2026-01-05"),            # no logs at all
    emp("EMP0005", "Eng", "2026-08-10"),            # joins after July
    emp("EMP0101", "Ops", "2026-03-01"),
    emp("EMP0102", "Ops", "2026-03-01"),
    emp("EMP0201", "Sales", "2026-08-01"),          # whole department joins after July
    emp("EMP0301", "Night", "2026-01-05", "22:00", "06:00"),
]
LOGS = []


def log(code, d, status="PRESENT", late=0, ot=0, wh=None, half=False, legacy=False):
    pin = pout = None
    if status in PRESENCE:
        day = date.fromisoformat(d)
        pin = datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST).astimezone(timezone.utc)
        if wh is not None:
            pout = pin + timedelta(hours=wh)
    doc = dict(emp_code=code, date=d, status=status, punch_in=pin, punch_out=pout, work_hours=wh,
               late_minutes=late, overtime_minutes=ot, half_day=half, history=[])
    if legacy:  # seeded record that omits the optional fields
        for k in ("late_minutes", "overtime_minutes", "half_day", "history"):
            del doc[k]
    LOGS.append(doc)


# Jul 2026: 1st is a Wednesday. 4th = Sat, 5th = Sun.
log("EMP0001", "2026-06-30", late=50, wh=9.0)               # previous month: must be ignored
log("EMP0001", "2026-07-01", wh=9.0)
log("EMP0001", "2026-07-02", late=35, wh=8.5)
log("EMP0001", "2026-07-03", "WFH", wh=9.01)
log("EMP0001", "2026-07-04", late=28, ot=40, wh=4.0, half=True)   # Saturday record
log("EMP0001", "2026-07-06", wh=4.2, half=True)                   # weekday half day
log("EMP0001", "2026-07-07", "LEAVE")
log("EMP0001", "2026-07-08", "ABSENT")
log("EMP0001", "2026-07-09", "ON_DUTY", late=15, ot=30, wh=9.5)
log("EMP0001", "2026-07-10", wh=None)                             # present, never punched out
log("EMP0001", "2026-07-31", late=12, wh=8.75)                    # last day of the month
log("EMP0001", "2026-08-01", late=99, wh=8.0)                     # next month: must be ignored
log("EMP0002", "2026-07-02", late=50, wh=8.0)
log("EMP0002", "2026-07-03", late=32, wh=7.0)
log("EMP0002", "2026-07-05", "ON_DUTY", wh=6.0)                   # Sunday record
log("EMP0002", "2026-07-14", wh=8.0, legacy=True)                 # omits late/overtime/half_day/history
log("EMP0002", "2026-07-15", wh=8.0)
log("EMP0003", "2026-07-16", late=11, wh=7.0)
log("EMP0003", "2026-07-17", wh=9.0)
log("EMP0101", "2026-07-01", late=40, wh=8.0)
log("EMP0101", "2026-07-02", late=42, wh=8.0)
log("EMP0102", "2026-07-03", late=80, wh=8.0)
log("EMP0301", "2026-07-02", late=45, wh=8.0)
log("EMP0301", "2026-07-03", wh=8.0)
log("EMP9999", "2026-07-02", late=500, wh=8.0)                    # employee does not exist: ignored

# --------------------------------------------------------------------------- oracle (plain Python)
def hu(x, places):
    q = Decimal(1).scaleb(-places)
    return float((Decimal(x.numerator) / Decimal(x.denominator)).quantize(q, rounding=ROUND_HALF_UP))


def pval(l):
    return Fraction(1, 2) if l.get("half_day") is True else Fraction(1)


def weekday(d):
    return date.fromisoformat(d).weekday() < 5


def month_days(month):
    y, mo = int(month[:4]), int(month[5:])
    d = date(y, mo, 1)
    out = []
    while d.month == mo:
        out.append(d)
        d += timedelta(days=1)
    return out


def exp_monthly(code, month):
    e = next(x for x in EMPLOYEES if x["emp_code"] == code)
    recs = [l for l in LOGS if l["emp_code"] == code and l["date"][:7] == month]
    wd = sum(1 for d in month_days(month) if d.weekday() < 5 and d.isoformat() >= e["joined_on"])
    present = sum((pval(l) for l in recs if l["status"] in PRESENCE and weekday(l["date"])), Fraction(0))
    return {
        "emp_code": code, "month": month, "working_days": wd, "present_days": float(present),
        "leave_days": sum(1 for l in recs if l["status"] == "LEAVE"),
        "late_count": sum(1 for l in recs if l.get("late_minutes", 0) > 0),
        "total_late_minutes": sum(l.get("late_minutes", 0) for l in recs),
        "total_overtime_minutes": sum(l.get("overtime_minutes", 0) for l in recs),
        "attendance_pct": hu(present / wd * 100, 2) if wd else None,
    }


def exp_summary(month, dept=None):
    last = month_days(month)[-1].isoformat()
    items = []
    for dname in sorted({e["department"] for e in EMPLOYEES}):
        if dept and dname != dept:
            continue
        members = [e for e in EMPLOYEES if e["department"] == dname and e["joined_on"] <= last]
        if not members:
            continue
        codes = {e["emp_code"] for e in members}
        recs = [l for l in LOGS if l["emp_code"] in codes and l["date"][:7] == month]
        hrs = [Fraction(str(l["work_hours"])) for l in recs if l["status"] in PRESENCE and l["work_hours"] is not None]
        items.append({
            "department": dname, "headcount": len(members),
            "present_days": float(sum((pval(l) for l in recs if l["status"] in PRESENCE and weekday(l["date"])), Fraction(0))),
            "avg_work_hours": hu(sum(hrs) / len(hrs), 2) if hrs else None,
            "late_count": sum(1 for l in recs if l.get("late_minutes", 0) > 0),
            "total_late_minutes": sum(l.get("late_minutes", 0) for l in recs),
            "leave_count": sum(1 for l in recs if l["status"] == "LEAVE"),
            "on_duty_count": sum(1 for l in recs if l["status"] == "ON_DUTY"),
        })
    return {"month": month, "items": items}


def exp_leaderboard(month, limit=10, dept=None):
    rows = []
    for e in EMPLOYEES:
        if dept and e["department"] != dept:
            continue
        recs = [l for l in LOGS if l["emp_code"] == e["emp_code"] and l["date"][:7] == month and l.get("late_minutes", 0) > 0]
        tot = sum(l["late_minutes"] for l in recs)
        if tot > 0:
            rows.append((e["emp_code"], e["name"], e["department"], tot, len(recs)))
    rows.sort(key=lambda r: (-r[3], r[0]))
    items = []
    for i, r in enumerate(rows):
        rank = 1 + sum(1 for x in rows if x[3] > r[3])        # standard competition ranking
        if rank <= limit:
            items.append({"rank": rank, "emp_code": r[0], "name": r[1], "department": r[2],
                          "total_late_minutes": r[3], "late_count": r[4]})
    return {"month": month, "items": items}


def exp_trend(dept, d_from, d_to):
    members = [e for e in EMPLOYEES if e["department"] == dept]
    codes = {e["emp_code"] for e in members}
    d, end, rows = date.fromisoformat(d_from), date.fromisoformat(d_to), []
    while d <= end:
        ds = d.isoformat()
        hc = sum(1 for e in members if e["joined_on"] <= ds)
        recs = [l for l in LOGS if l["emp_code"] in codes and l["date"] == ds]
        present = sum((pval(l) for l in recs if l["status"] in PRESENCE), Fraction(0))
        wd = d.weekday() < 5
        rate = present / hc if (wd and hc > 0) else None
        rows.append({"date": ds, "is_working_day": wd, "headcount": hc, "present_count": float(present),
                     "late_count": sum(1 for l in recs if l.get("late_minutes", 0) > 0), "_rate": rate})
        d += timedelta(days=1)
    for i, r in enumerate(rows):
        window = [x["_rate"] for x in rows[max(0, i - 6): i + 1] if x["_rate"] is not None]
        r["attendance_rate"] = hu(r["_rate"], 4) if r["_rate"] is not None else None
        r["moving_avg_7d"] = hu(sum(window) / len(window), 4) if window else None
    for r in rows:
        del r["_rate"]
    return {"department": dept, "items": rows}


# --------------------------------------------------------------------------- run
failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok    {name}")
        return
    failures.append(name)
    print(f"  FAIL  {name}")
    if isinstance(got, dict) and isinstance(want, dict) and "items" in got and "items" in want:
        for i, (g, w) in enumerate(zip(got["items"], want["items"])):
            if g != w:
                print(f"        row {i}:\n          got  {g}\n          want {w}")
        if len(got["items"]) != len(want["items"]):
            print(f"        row count: got {len(got['items'])}, want {len(want['items'])}")
    else:
        print(f"        got  {got}\n        want {want}")


def api(c, url):
    try:
        r = c.get(url)
    except Exception as exc:  # e.g. an unhandled server error
        return {"_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return r.json() if r.status_code == 200 else {"_status": r.status_code, "_body": r.text[:300]}


def guarded(name, fn):
    try:
        fn()
    except Exception as exc:  # report and carry on with the other checks
        failures.append(name)
        print(f"  ERROR {name}: {type(exc).__name__}: {str(exc)[:300]}")


m.client.drop_database("attendance_verify")
with TestClient(m.app) as c:  # entering the context runs the startup (index creation)
    m.db.employees.insert_many([dict(e) for e in EMPLOYEES])
    m.db.attendance_logs.insert_many([dict(l) for l in LOGS])

    print("indexes:")
    for coll in ("employees", "attendance_logs"):
        print("  ", coll, sorted(m.db[coll].index_information()))

    print("employee monthly")
    for code in ("EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0301"):
        guarded(f"monthly {code}", lambda code=code: check(f"monthly {code}", api(c, f"/analytics/employees/{code}/monthly?month=2026-07"), exp_monthly(code, "2026-07")))
    guarded("monthly empty month", lambda: check("monthly EMP0001 2026-05", api(c, "/analytics/employees/EMP0001/monthly?month=2026-05"), exp_monthly("EMP0001", "2026-05")))

    print("department summary")
    guarded("summary all", lambda: check("summary 2026-07", api(c, "/analytics/departments/summary?month=2026-07"), exp_summary("2026-07")))
    guarded("summary dept", lambda: check("summary 2026-07 Eng", api(c, "/analytics/departments/summary?month=2026-07&department=Eng"), exp_summary("2026-07", "Eng")))
    guarded("summary aug", lambda: check("summary 2026-08", api(c, "/analytics/departments/summary?month=2026-08"), exp_summary("2026-08")))

    print("late leaderboard")
    for lim in (1, 2, 3, 4, 5, 10):
        guarded(f"leaderboard {lim}", lambda lim=lim: check(f"leaderboard limit={lim}", api(c, f"/analytics/leaderboard/late?month=2026-07&limit={lim}"), exp_leaderboard("2026-07", lim)))
    guarded("leaderboard dept", lambda: check("leaderboard Ops", api(c, "/analytics/leaderboard/late?month=2026-07&department=Ops"), exp_leaderboard("2026-07", 10, "Ops")))
    guarded("leaderboard default", lambda: check("leaderboard default limit", api(c, "/analytics/leaderboard/late?month=2026-07"), exp_leaderboard("2026-07", 10)))
    print("   ranks:", [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in api(c, "/analytics/leaderboard/late?month=2026-07").get("items", [])])

    print("department trend")
    for dept, a, b in [("Eng", "2026-06-28", "2026-07-20"), ("Eng", "2026-07-13", "2026-07-17"), ("Eng", "2026-07-04", "2026-07-05"),
                       ("Ops", "2026-07-01", "2026-07-10"), ("Sales", "2026-07-20", "2026-08-05"), ("Night", "2026-07-01", "2026-07-03")]:
        guarded(f"trend {dept}", lambda dept=dept, a=a, b=b: check(f"trend {dept} {a}..{b}", api(c, f"/analytics/departments/{dept}/trend?from={a}&to={b}"), exp_trend(dept, a, b)))
    t = api(c, "/analytics/departments/Eng/trend?from=2026-06-28&to=2026-07-20")
    print("   rows:", len(t.get("items", [])), "(want 23)")

    print("explain (needs IXSCAN, no COLLSCAN)")
    cases = [
        ("attendance_list", ""),
        ("attendance_list", "emp_code=EMP0001"),
        ("attendance_list", "status=LEAVE"),
        ("attendance_list", "date_from=2026-07-01&date_to=2026-07-31&status=PRESENT"),
        ("employee_monthly", "emp_code=EMP0001&month=2026-07"),
        ("department_summary", "month=2026-07"),
        ("department_summary", "month=2026-07&department=Eng"),
        ("late_leaderboard", "month=2026-07&limit=5"),
        ("late_leaderboard", "month=2026-07&department=Ops"),
        ("department_trend", "department=Eng&from=2026-06-28&to=2026-07-20"),
    ]
    for ep, qs in cases:
        def one(ep=ep, qs=qs):
            r = c.get(f"/admin/explain/{ep}?{qs}")
            text = json.dumps(r.json()) if r.status_code == 200 else ""
            ok = r.status_code == 200 and "IXSCAN" in text and "COLLSCAN" not in text
            print(f"  {'ok  ' if ok else 'FAIL'}  {ep}?{qs}  (status {r.status_code}, IXSCAN={'IXSCAN' in text}, COLLSCAN={'COLLSCAN' in text})")
            if not ok:
                failures.append(f"explain {ep}?{qs}")
        guarded(f"explain {ep}", one)

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)