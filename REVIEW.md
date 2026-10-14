# REVIEW.md

Problems I found in the starter `app/main.py`. Line numbers are from the starter. "(run)" means I ran the starter code and saw it.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (L33-37) | It checks the 10-minute grace on whole minutes, so 10:00:59 after start still counts as on time. The rule is strictly more than 10:00. | Shift 09:30, punch-in 09:40:01: should be 10, starter gives 0 (run). | Work in seconds (`> 600`), then divide by 60 and round down. |
| 2 | `compute_late_minutes` (L34-35) | It builds the shift start with `punch_in.replace(...)`, so it uses whatever timezone the server has, not IST. It also puts the start on the punch-in's own day, which breaks overnight shifts. | The same punch-in gives different late values on a UTC server and an IST one. | Build the shift start in IST from the record's date (`shift_bounds`). |
| 3 | `compute_work_hours` (L40-41) | Python `round()` on a float, so 0.125 becomes 0.12. The rule says round half up. Times also aren't cut to whole seconds. | 450 seconds: should be 0.13, starter gives 0.12 (run). | `Decimal` with half-up rounding; times cut to whole seconds when they come in. |
| 4 | `compute_overtime` (L44-47) | No 30-minute minimum. The shift end is built from the same date, so overnight shifts are a day off. It mixes aware and naive datetimes and crashes. It also floors via a float. | Shift ends 18:30, out at 18:40: should be 0, starter gives 10 (run). Overnight 22:00-06:00, out 06:40: gives 1480 instead of 40 (run). | End time comes from `shift_bounds` (next day for overnight), whole-second maths, 0 if under 30. |
| 5 | `health` (L72-74) | Always says ok, never talks to MongoDB. | Stop Mongo and `/health` still returns 200. | It pings Mongo and returns 503 if that fails. |
| 6 | `create_employee` (L78-85) | Looks first, inserts second, with no unique index behind it. | Two parallel POSTs with the same code can both succeed. | Unique index on `emp_code`; insert and turn `DuplicateKeyError` into 409. |
| 7 | `EmployeeIn` / `create_employee` (L53-60, L82) | Nothing is validated (code format, email, shift times, real dates). `created_at` is a naive local time and isn't epoch ms. | `joined_on: "banana"` is accepted. | Proper validation, and `created_at` stored in UTC and returned as epoch ms. |
| 8 | `list_employees` (L89-96) | `skip = page * page_size` skips page one. `total` ignores the department filter. No sort. `page_size=0` becomes `limit(0)`, which means no limit, and a negative skip crashes. | `page=1` never shows the first rows; `?department=X` shows everyone's total. | `(page-1) * page_size`, filtered count, sorted by `emp_code`, page and page_size validated. |
| 9 | `punch_in` (L100-101) | Unknown employee: `emp` is `None` so it crashes with a 500. | POST for `EMP9999`. | Returns 404. |
| 10 | `punch_in` / `PunchInIn` (L63-66, L102) | `if body.punched_at` treats 0 as missing. Numeric strings, floats and seconds are accepted. Any `status` is accepted, even `ABSENT`. | `status: "ABSENT"` gives 201; a time in seconds lands in 1970. | Strict int in the epoch-ms range; status limited to PRESENT, WFH, ON_DUTY. |
| 11 | `punch_in` (L102-103) | `fromtimestamp(ms / 1000)` uses the server's local zone, so the date isn't IST. Overnight shifts are ignored. | On a UTC server, 00:30 IST gets yesterday's date. | Date comes from the IST time with the overnight rule (`attendance_date`). |
| 12 | `punch_in` (L104-105) | Looks first, inserts second again. | Two identical parallel punch-ins both succeed. | Unique `(emp_code, date)` index and 409. Tested with 10 parallel requests: one 201, nine 409. |
| 13 | `punch_in` (L99-121) | The response has an `id` (the ObjectId) and raw datetimes. | `id` appears and times aren't epoch ms. | `serialize_record` returns exactly the contract shape. |
| 14 | `list_attendance` (L125-150) | Loads every record into Python, then sorts and pages there. Sorts by date only, so same-day order is random. | Slow and heavy at 100k records. | Sorting, skipping and limiting happen in MongoDB, with indexes behind them. |
| 15 | `list_attendance` (L125-150) | `page=0` gives a negative slice and returns the tail. `page_size` has no limit. Dates and status aren't checked, and `date_from > date_to` just returns nothing. | `?date_from=banana` returns 200 with nothing. | Bad values give 422, including `date_from > date_to`. |
| 16 | `list_attendance` (L148-150) | Leaks `_id` as `id`, returns raw datetimes, and doesn't fill in missing `history` or `half_day`. | A record without `history` comes back without it. | Same serializer as punch-in, with defaults filled in. |
| 17 | module level (L22-23) | The client returns naive datetimes (the cause of the mix-ups above), waits 30 seconds if Mongo is down, and no indexes are created anywhere. | `TypeError` mixing naive and aware datetimes; slow `/health`. | `tz_aware=True`, a 5 second timeout, indexes created at startup, database errors become 503. |

## Looked at, but it's fine
- `load_dotenv()` (L20) doesn't override variables that are already set, so real environment variables win, as required.
- `total` in `list_attendance` (L146) is correct, since it counts the filtered list. The problem was how it was built (see #14).
- `doc.pop("_id")` in `create_employee` hides the id. I replaced it with a serializer anyway.
- `half_day: False` and `overtime_minutes: 0` at punch-in are correct, because they're only worked out at punch-out.