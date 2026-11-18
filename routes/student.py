from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)
from werkzeug.security import generate_password_hash

import re

from db import get_db, tx
from logic import EXAM_TYPES, booking_open, refresh_slot, to_int, today

bp = Blueprint("student", __name__, url_prefix="/student")


@bp.before_request
def guard():
    if not g.user:
        return redirect(url_for("auth.login"))
    if g.user["role"] != "student":
        abort(403)


EXAM_SQL = (
    "SELECT e.*, c.code, c.name AS course_name, "
    "(SELECT COUNT(*) FROM slots s WHERE s.exam_id=e.id AND s.status='Available') AS open_slots, "
    "(SELECT b.status FROM bookings b WHERE b.exam_id=e.id AND b.student_id=:me "
    "AND b.status IN ('Booked','Completed')) AS my_status "
    "FROM exams e JOIN courses c ON c.id=e.course_id WHERE e.status!='Draft'")


@bp.route("/")
def dashboard():
    db, me = get_db(), g.user["id"]
    exams = db.execute(EXAM_SQL + " ORDER BY e.id DESC", {"me": me}).fetchall()
    open_exams = [e for e in exams if booking_open(e) and not e["my_status"] and e["open_slots"]]
    upcoming = db.execute(
        "SELECT b.*, e.name AS exam, s.date, s.start_time, s.end_time, u.full_name AS examiner "
        "FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id "
        "JOIN users u ON u.id=s.examiner_id WHERE b.student_id=? AND b.status='Booked' "
        "ORDER BY s.date, s.start_time", (me,)).fetchall()
    results = _results(me)
    stats = [("Open for booking", len(open_exams)), ("Booked", len(upcoming)),
             ("Results out", len(results))]
    return render_template("student/dashboard.html", stats=stats, open_exams=open_exams[:5],
                           upcoming=upcoming, results=results[:3])


@bp.route("/exams")
def exams():
    q, tp = request.args.get("q", "").strip(), request.args.get("type", "")
    only_open = request.args.get("open") == "1"
    rows = get_db().execute(EXAM_SQL + " ORDER BY e.id DESC", {"me": g.user["id"]}).fetchall()
    out = []
    for e in rows:
        hay = f"{e['name']} {e['code']} {e['course_name']}".lower()
        if q and q.lower() not in hay and q != str(e["id"]):
            continue
        if tp in EXAM_TYPES and e["type"] != tp:
            continue
        if only_open and not booking_open(e):
            continue
        out.append(e)
    return render_template("student/exams.html", rows=out, q=q, tp=tp, only_open=only_open,
                           types=EXAM_TYPES, booking_open=booking_open)


@bp.route("/exams/<int:eid>")
def exam(eid):
    db = get_db()
    e = db.execute(EXAM_SQL + " AND e.id=:id", {"me": g.user["id"], "id": eid}).fetchone()
    if not e:
        abort(404)
    slots = db.execute(
        "SELECT s.*, u.full_name AS examiner FROM slots s JOIN users u ON u.id=s.examiner_id "
        "WHERE s.exam_id=? AND s.status!='Cancelled' ORDER BY s.date, s.start_time", (eid,)).fetchall()
    rubrics = db.execute("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (eid,)).fetchall()
    mine = db.execute("SELECT * FROM bookings WHERE exam_id=? AND student_id=? "
                      "AND status IN ('Booked','Completed')", (eid, g.user["id"])).fetchone()
    return render_template("student/exam.html", e=e, slots=slots, rubrics=rubrics, mine=mine,
                           is_open=booking_open(e))


@bp.route("/slots/<int:sid>/book", methods=["POST"])
def book(sid):
    db = get_db()
    eid = None
    try:
        with tx(db):  # BEGIN IMMEDIATE serialises writers, so seats cannot be oversold
            s = db.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
            if not s:
                raise ValueError("That slot does not exist.")
            eid = s["exam_id"]
            e = db.execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
            if not booking_open(e):
                raise ValueError("Booking is not open for this examination.")
            if db.execute("SELECT 1 FROM bookings WHERE student_id=? AND exam_id=? "
                          "AND status IN ('Booked','Completed')", (g.user["id"], eid)).fetchone():
                raise ValueError("You already hold a booking for this examination.")
            if s["status"] != "Available" or s["available_seats"] <= 0:
                raise ValueError("That slot is full.")
            db.execute("INSERT INTO bookings (student_id,slot_id,exam_id) VALUES (?,?,?)",
                       (g.user["id"], sid, eid))
            refresh_slot(db, sid)
        flash("Slot booked.", "ok")
    except ValueError as ex:
        flash(str(ex), "error")
    return redirect(url_for("student.exam", eid=eid) if eid else url_for("student.exams"))


@bp.route("/bookings")
def bookings():
    rows = get_db().execute(
        "SELECT b.*, e.name AS exam, e.status AS exam_status, e.book_start, e.book_end, "
        "s.date, s.start_time, s.end_time, u.full_name AS examiner "
        "FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id "
        "JOIN users u ON u.id=s.examiner_id WHERE b.student_id=? ORDER BY b.id DESC",
        (g.user["id"],)).fetchall()
    return render_template("student/bookings.html", rows=rows, booking_open=booking_open)


@bp.route("/bookings/<int:bid>/cancel", methods=["POST"])
def cancel(bid):
    db = get_db()
    try:
        with tx(db):
            b = db.execute("SELECT b.*, e.status AS es, e.book_start, e.book_end FROM bookings b "
                           "JOIN exams e ON e.id=b.exam_id WHERE b.id=? AND b.student_id=?",
                           (bid, g.user["id"])).fetchone()
            if not b:
                abort(404)
            if b["status"] != "Booked":
                raise ValueError("Only active bookings can be cancelled.")
            if not (b["es"] == "Booking Open" and today() <= b["book_end"]):
                raise ValueError("The booking deadline has passed; cancellation is closed.")
            db.execute("UPDATE bookings SET status='Cancelled' WHERE id=?", (bid,))
            refresh_slot(db, b["slot_id"])
        flash("Booking cancelled.", "ok")
    except ValueError as ex:
        flash(str(ex), "error")
    return redirect(url_for("student.bookings"))


def _results(me):
    db = get_db()
    rows = db.execute(
        "SELECT b.id, e.id AS exam_id, e.name AS exam, e.max_marks, s.date, ev.id AS ev_id, "
        "ev.total_marks, ev.remarks, u.full_name AS examiner FROM bookings b "
        "JOIN exams e ON e.id=b.exam_id JOIN slots s ON s.id=b.slot_id "
        "JOIN users u ON u.id=s.examiner_id JOIN evaluations ev ON ev.booking_id=b.id "
        "WHERE b.student_id=? AND e.results_published=1 ORDER BY ev.evaluated_on DESC", (me,)).fetchall()
    out = []
    for r in rows:
        parts = db.execute(
            "SELECT ru.criterion, ru.max_marks, sc.marks FROM scores sc "
            "JOIN rubrics ru ON ru.id=sc.rubric_id WHERE sc.evaluation_id=? ORDER BY ru.id",
            (r["ev_id"],)).fetchall()
        out.append({**dict(r), "parts": parts})
    return out


@bp.route("/results")
def results():
    return render_template("student/results.html", rows=_results(g.user["id"]))


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    db, f = get_db(), request.form
    if request.method == "POST":
        name, phone, dept = f.get("full_name", "").strip(), f.get("phone", "").strip(), f.get("department", "").strip()
        pw = f.get("password", "")
        errors = []
        if len(name) < 2:
            errors.append("Name is required.")
        if phone and not re.fullmatch(r"[0-9+\-\s]{7,15}", phone):
            errors.append("Phone number looks invalid.")
        if pw and len(pw) < 6:
            errors.append("New password needs at least 6 characters.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with tx(db):
                db.execute("UPDATE users SET full_name=?, phone=?, department=? WHERE id=?",
                           (name, phone, dept, g.user["id"]))
                if pw:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(pw), g.user["id"]))
            flash("Profile updated.", "ok")
            return redirect(url_for("student.profile"))
    return render_template("student/profile.html", u=f if request.method == "POST" else g.user)
