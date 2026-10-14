# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (no duplicates), `(department, emp_code)` (department filter, trend), `joined_on` (headcount). `attendance_logs`: unique `(emp_code, date)` (one record per person per day; also speeds up punch-out, PATCH and monthly) and `(date desc, emp_code)` (default list order, leaderboard dates). I skipped one on `status`: only five values, so it narrows almost nothing.

2. **Punch-in race.** Both requests find the employee, work out the same date and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one and reject the other with `DuplicateKeyError`, which I return as 409. I never check first and insert second.

3. **Ties.** I rank everyone with `$rank` first and only then keep `rank <= limit`. So everyone tied at the cutoff comes back, and the list can be longer than `limit`. Cutting by position would randomly drop one of two equal people.

4. **Headcount.** The summary starts from `employees` and attaches logs with `$lookup`, keeping employees whose log list is empty. Starting from logs would lose them.

5. **At 100x.** I'd keep daily per-department totals and stop re-reading a month of raw logs on every call, updated on write or rebuilt with `$merge`.