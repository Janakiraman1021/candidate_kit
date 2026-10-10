# DECISIONS.md

1. **Indexes.** Seven indexes at startup: `employees.emp_code` unique (enforces uniqueness, serves lookups); `employees.(department, emp_code)` (department filter + sort); `attendance_logs.(emp_code, date)` unique (punch-in atomicity via DuplicateKeyError); `attendance_logs.(date DESC, emp_code ASC)` (matches list sort); `attendance_logs.(date, status)` (date-range + status filter); `attendance_logs.(emp_code, punch_in)` (punch-out lookup). Rejected partial `punch_out: null` index due to write amplification on index rebuild.

2. **Punch-in race.** Unique `(emp_code, date)` index ensures atomic dedupe; concurrent requests raise DuplicateKeyError caught as 409. No application-level lock needed.

3. **Ties.** Standard competition ranking: same total_late_minutes → same rank, next rank skipped. Python assigns ranks post-aggregation; filter `rank <= limit` includes boundary ties even if count exceeds limit.

4. **Headcount.** Pipeline starts from `employees` collection (ensures zero-log employees counted). `$lookup` for month's logs; employees with zero logs still contribute 1 to headcount in `$group`.

5. **100x data optimization.** Replace per-day correlated `$lookup` in trend with single upfront `$lookup` of all relevant employees; compute headcount per day using `$filter` and `$size`. This subquery is the bottleneck at scale.
