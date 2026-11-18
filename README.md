# Examination Management Portal (EMP)

Flask + Jinja2 + Bootstrap 5 (local copy) + SQLite. No JavaScript is used for any core feature.

## Run

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

The database (`instance/emp.db`) and all tables are created by `db.py` on first start.
The Admin is seeded there too:

| Role  | Email           | Password |
|-------|-----------------|----------|
| Admin | admin@emp.edu   | admin123 |

Examiners and students register themselves. Examiners stay "pending" until the Admin approves them.

## Demo flow

1. **Admin**: create a course, then an examination (set the two date windows to include today).
   On the examination page add rubric criteria and assign approved examiners.
2. **Admin**: *Open slot creation* → **Examiner** creates slots → **Admin**: *Close*, then *Open student booking*.
3. **Student**: browse examinations → book a slot (cancel allowed until the booking deadline).
4. **Examiner**: open a slot → *evaluate* each booked student against the rubric.
5. **Admin**: *publish* results → **Student** sees marks and remarks.

Status flow: `Draft → Slot Creation → Closed → Booking Open → Closed → Completed`.

## Rules enforced in the backend

- Slots can be created only while status is *Slot Creation* **and** today is inside the slot-creation dates.
- Booking/cancelling only while status is *Booking Open* **and** today is inside the booking dates.
- Seats are never oversold (`BEGIN IMMEDIATE` transaction); a slot flips to *Full* automatically and back to *Available* on cancellation.
- One live booking per student per examination (also a partial unique index). Cancelled bookings stay as history.
- Examiners can edit/remove slots only before any booking exists and before booking opens; they cannot have overlapping slots.
- Examiners can evaluate only students booked into their own slots. Marks are locked once results are published.
- Admin can reschedule a booking to another open slot of the same examination and change a slot's examiner.
- Total marks = Σ(marks ÷ criterion max × weightage) ÷ Σ weightage × exam max marks (plain sum if no weightage).
- CSRF token on every POST; passwords hashed with Werkzeug.

## Layout

```
emp_project/
  app.py            app factory, CSRF, filters
  db.py             schema + admin seed + transaction helper
  logic.py          windows, seat refresh, marks, validation helpers
  routes/           auth.py  admin.py  examiner.py  student.py  api.py
  templates/        base, nav, macros + auth/ admin/ examiner/ student/
  static/css/       bootstrap.min.css  style.css
```

## Tables (for the ER diagram)

`users`(role: admin/examiner/student) · `courses` · `exams`→courses · `exam_examiners`(exam, examiner) ·
`rubrics`→exams · `slots`→exams, users · `bookings`→users, slots, exams · `evaluations`→bookings ·
`scores`(evaluation, rubric, marks)

## JSON API (login required, read-only)

| Endpoint | Who |
|---|---|
| `GET /api/exams?q=` | any (students: non-draft only) |
| `GET /api/exams/<id>` | any (includes slots) |
| `GET /api/examiners` | admin |
| `GET /api/students` | admin |
| `GET /api/bookings` | admin: all · student: own · examiner: own slots |
