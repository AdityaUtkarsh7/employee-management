# Employee Attendance & Analytics API

FastAPI backend for employee attendance, punch management, audit corrections, and MongoDB aggregation analytics.

## Running the Service

### Prerequisites
- Python 3.10+
- MongoDB 6.0+ (Tested on MongoDB 7.0)

### Setup & Startup
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment (optional, defaults to localhost:27017 and attendance_db)
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# 3. Seed sample data (optional)
python sample_seed.py

# 4. Start the application
uvicorn app.main:app --port 8000
```

## Features Implemented
- **System**:
  - `GET /health`: Readiness probe testing live MongoDB connection.
- **Employees**:
  - `POST /employees`: Create employee with unique index enforcement on `emp_code` and shift validation.
  - `GET /employees`: Filter by department, paginated by `page` and `page_size`, sorted by `emp_code`.
- **Attendance & Corrections**:
  - `POST /attendance/punch-in`: Race-safe daily record creation following IST calendar date (R1) and late minutes calculation (R2).
  - `POST /attendance/punch-out`: Atomically closes open punch, calculating work hours (R4), overtime (R3), and half-day (R5) across midnight.
  - `GET /attendance`: Filter by employee, date range, and status; paginated; sorted by date descending.
  - `PATCH /attendance/{emp_code}/{date}`: Manual regularization with optimistic concurrency and complete history audit trail.
- **Analytics (MongoDB Aggregation Pipelines)**:
  - `GET /analytics/employees/{emp_code}/monthly`: Monthly summary with weekday working days, present days, leave days, and attendance percentage.
  - `GET /analytics/departments/summary`: Department-level summary across collections, including zero-log employees in headcount.
  - `GET /analytics/leaderboard/late`: Late-arrival leaderboard using MongoDB standard competition ranking window functions (`$rank`).
  - `GET /analytics/departments/{department}/trend`: Daily trend series using `$densify` gap filling and 7-day preceding moving averages.
- **Explainability**:
  - `GET /admin/explain/{endpoint}`: MongoDB execution stats query planner verification (all endpoints backed by `IXSCAN` with zero `COLLSCAN`).

## Documentation
- `REVIEW.md`: Detailed audit of defects identified and resolved from the starter.
- `DECISIONS.md`: Explanations of indexing strategy, concurrency handling, tie-breaking, headcount calculation, and scale adaptations.
