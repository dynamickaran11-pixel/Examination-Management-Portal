"""Shared rules and helpers (time windows, seats, marks, CSRF, validation)."""
import secrets
from datetime import date, datetime

from flask import session
from markupsafe import Markup

STATUSES = ["Draft", "Slot Creation", "Booking Open", "Closed", "Completed"]
EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]


# ---------- dates & windows ----------
def today():
    return date.today().isoformat()


def valid_date(s):
    try:
        datetime.strptime(s or "", "%Y-%m-%d")
        return True
    except ValueError:
        return False


def valid_time(s):
    try:
        datetime.strptime(s or "", "%H:%M")
        return True
    except ValueError:
        return False


def slot_creation_open(exam):
    """Examiners may create slots only while status is 'Slot Creation' and inside the dates."""
    return (exam["status"] == "Slot Creation"
            and exam["slot_create_start"] <= today() <= exam["slot_create_end"])


def booking_open(exam):
    """Students may book/cancel only while status is 'Booking Open' and inside the dates."""
    return (exam["status"] == "Booking Open"
            and exam["book_start"] <= today() <= exam["book_end"])


# ---------- parsing ----------
def to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ---------- slots ----------
def refresh_slot(db, slot_id):
    """Recompute available seats and status from the bookings (single source of truth)."""
    s = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    if s is None:
        return
    booked = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='Booked'", (slot_id,)
    ).fetchone()[0]
    done = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='Completed'", (slot_id,)
    ).fetchone()[0]
    seats = max(s["capacity"] - booked - done, 0)
    if s["status"] == "Cancelled":
        status = "Cancelled"
    elif booked == 0 and done > 0:
        status = "Completed"
    elif seats == 0:
        status = "Full"  # automatically closes when capacity is reached
    else:
        status = "Available"
    db.execute("UPDATE slots SET available_seats=?, status=? WHERE id=?", (seats, status, slot_id))


def examiner_conflict(db, examiner_id, day, start, end, exclude_slot=None):
    """True if the examiner already has an overlapping slot on that day."""
    row = db.execute(
        "SELECT 1 FROM slots WHERE examiner_id=? AND date=? AND status!='Cancelled' "
        "AND start_time < ? AND end_time > ? AND id != ?",
        (examiner_id, day, end, start, exclude_slot or 0),
    ).fetchone()
    return row is not None


# ---------- marks ----------
def weighted_total(exam_max, rubrics, marks):
    """Total out of exam.max_marks. Each criterion contributes (marks/max) * weightage.
    If no weightage is configured, plain sum of marks is used."""
    ws = sum(r["weightage"] for r in rubrics)
    if ws > 0:
        frac = sum(marks[r["id"]] / r["max_marks"] * r["weightage"] for r in rubrics) / ws
        return round(frac * exam_max, 2)
    return round(sum(marks.values()), 2)


# ---------- CSRF ----------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def csrf_field():
    return Markup('<input type="hidden" name="_csrf" value="%s">' % csrf_token())
