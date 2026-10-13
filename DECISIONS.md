# DECISIONS.md

1. **Indexes.**
   - `employees`: `emp_code` (unique, ensures employee identity and point lookups); `(department, joined_on)` & `(joined_on, department)` (powers department filtering and headcount date-range queries via IXSCAN).
   - `attendance_logs`: `(emp_code, date)` (unique natural key, guarantees one daily record per employee); `(date, emp_code)` (supports sorted list pagination without in-memory sort); `(emp_code, punch_in)` (fast punch-out resolution); `(date, late_minutes, emp_code)` (drives the late leaderboard index scan).
   - *Rejected*: An index on `(status, work_hours)`—low cardinality and high write churn with minimal selectivity.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `db.attendance_logs.insert_one(...)`. MongoDB's unique compound index `(emp_code, date)` enforces atomic single-record insertion. Exactly one insert commits and returns `201 Created`. The other encounters a `DuplicateKeyError`, which is trapped and returned as `409 Conflict`. Check-then-insert race windows are completely avoided.

3. **Ties.**
   Using MongoDB's `$rank` window function, tied employees share the same competition rank. The filter `rank <= limit` applies after ranking, so all employees tied at or below the cutoff are included (even if total rows exceed `limit`). Rows are tie-broken by `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline starts on `employees`, matching `joined_on <= month_end` and grouping by department to calculate true headcount before performing `$lookup` on `attendance_logs`. Employees with zero logs are retained in the base grouping.

5. **One thing you would change for 100x data.**
   Implement asynchronous daily rollup materialized collections or change streams for monthly department summaries and leaderboards, avoiding live cross-collection scans over hundreds of millions of raw punch documents.
