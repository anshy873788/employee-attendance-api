# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.
| 1 | `list_employees` | Page 1 skipped the first `page_size` records, and `total` counted all employees even when a department filter was applied. | `GET /employees?page=1&page_size=20` returned an empty `items` list even though the database had employees. | Changed the skip calculation to `(page - 1) * page_size` and counted documents using `q`. |
| 2 | `list_attendance` | It loaded every matching attendance record into memory and sorted only by date. This would be slow for large datasets and did not guarantee the required emp_code tie-breaker. | The API contract requires performance with about 100,000 records and sorting by date descending, then emp_code ascending. | Used MongoDB `count_documents`, `sort`, `skip`, and `limit`; added the required date-range validation. |
| 3 | `punch_in` | An unknown employee caused a 500 Internal Server Error instead of the required 404 response. | Sending `POST /attendance/punch-in` with `"emp_code": "UNKNOWN"` returned 500. | Added an employee-exists check and return 404 before accessing the employee shift. |
| 4 | `punch_in` | It accepted `ABSENT` and `LEAVE` statuses even though a punch-in is allowed only for PRESENT, WFH, or ON_DUTY. | Sending a punch-in with `"status": "ABSENT"` should return 422. | Added validation that accepts only PRESENT, WFH, and ON_DUTY. |
| 5 | `list_employees` and `list_attendance` | Pagination accepted invalid page numbers and page sizes above the contract maximum. | `GET /employees?page_size=101` must return 422. | Added FastAPI query validation: page must be at least 1, and page_size must be 1–100. |
| 6 | `list_employees` | Results were not sorted by `emp_code` ascending as required by the contract. | The API contract for `GET /employees` requires ascending emp_code order. | Added MongoDB sorting by `emp_code` ascending. |
| 7 | `punch_in` | Timestamps used the computer’s local time and the attendance date was simply the punch date, which breaks IST and overnight-shift rules. | An overnight-shift punch-in before shift end must belong to the previous attendance date. | Converted epoch timestamps to IST and added attendance-date logic for overnight shifts. |
| 8 | `compute_late_minutes` | It rounded down before checking the 10-minute grace period, so 09:40:01 was incorrectly treated as not late; it also did not support overnight shifts. | For a 09:30 shift, a 09:40:01 punch-in must return 10 late minutes. | Compare elapsed seconds to the grace period first, then floor the late minutes; added overnight handling. |
| 9 | `compute_work_hours` | Python `round()` uses banker's rounding, but the contract requires half-up rounding. | A value exactly halfway between two hundredths can round differently. | Added Decimal half-up rounding. |
| 10 | `compute_overtime` | It awarded overtime for less than 30 minutes and did not calculate overnight shift ends on the following day. | The contract says overtime is zero below 30 minutes and overnight shifts end the next day. | Added the 30-minute minimum and overnight end-time handling. |
| 11 | `create_employee` and `punch_in` | The check-then-insert approach was not safe when identical requests arrived at the same time. Two requests could both pass the initial lookup. | Send two requests concurrently with the same employee code or the same emp_code/date attendance key. | Added MongoDB unique indexes and converted `DuplicateKeyError` into HTTP 409 responses. |