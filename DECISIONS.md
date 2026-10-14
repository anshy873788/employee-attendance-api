# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
I added a unique index on employees.emp_code to prevent duplicate employee codes and a unique compound index on attendance_logs (emp_code, date) to ensure one attendance record per employee per day. I also added indexes for employee department/join date, attendance listing, late-arrival reporting, and punch-out lookup. I did not add an index only on status because it has few possible values and would not be selective enough on its own.

2. **Punch-in race.**  The code first checks for an existing attendance record. If two identical punch-ins arrive together, both may pass that check, but MongoDB allows only one insert because of the unique (emp_code, date) index. The other insert raises DuplicateKeyError, which the API returns as HTTP 409.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The leaderboard uses standard competition ranking. Employees tied on late minutes receive the same rank, and everyone whose rank is within the requested limit is returned.
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
Department summary begins with employees who joined by month-end and then looks up attendance. This includes employees who have no attendance logs.
5. **One thing you would change** if this had to serve 100x the data.
I would monitor query plans, refine compound indexes, and consider precomputed monthly summaries for frequently requested reports.
