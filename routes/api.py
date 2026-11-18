"""Small read-only JSON API (session login required). Admin sees everything."""
from flask import Blueprint, g, jsonify, request

from db import get_db

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.before_request
def guard():
    if not g.user:
        return jsonify(error="login required"), 401


def _admin_only():
    return None if g.user["role"] == "admin" else (jsonify(error="admin only"), 403)


def rows(sql, params=()):
    return [dict(r) for r in get_db().execute(sql, params).fetchall()]


@bp.route("/exams")
def exams():
    q = f"%{request.args.get('q', '')}%"
    hide = "" if g.user["role"] == "admin" else " AND e.status!='Draft'"
    return jsonify(rows(
        "SELECT e.id, e.name, e.type, e.duration, e.max_marks, e.status, c.code AS course_code "
        "FROM exams e JOIN courses c ON c.id=e.course_id WHERE (e.name LIKE ? OR c.code LIKE ?)"
        + hide + " ORDER BY e.id", (q, q)))


@bp.route("/exams/<int:eid>")
def exam(eid):
    r = rows("SELECT e.*, c.code AS course_code FROM exams e JOIN courses c ON c.id=e.course_id "
             "WHERE e.id=?", (eid,))
    if not r or (r[0]["status"] == "Draft" and g.user["role"] != "admin"):
        return jsonify(error="not found"), 404
    r[0]["slots"] = rows("SELECT id, date, start_time, end_time, capacity, available_seats, status "
                         "FROM slots WHERE exam_id=? ORDER BY date, start_time", (eid,))
    return jsonify(r[0])


@bp.route("/examiners")
def examiners():
    return _admin_only() or jsonify(rows(
        "SELECT id, full_name, email, phone, department, status FROM users WHERE role='examiner'"))


@bp.route("/students")
def students():
    return _admin_only() or jsonify(rows(
        "SELECT id, full_name, email, phone, department FROM users WHERE role='student'"))


@bp.route("/bookings")
def bookings():
    base = ("SELECT b.id, b.student_id, b.slot_id, b.exam_id, b.booking_date, b.status "
            "FROM bookings b")
    if g.user["role"] == "admin":
        return jsonify(rows(base + " ORDER BY b.id"))
    if g.user["role"] == "student":
        return jsonify(rows(base + " WHERE b.student_id=? ORDER BY b.id", (g.user["id"],)))
    return jsonify(rows(base + " JOIN slots s ON s.id=b.slot_id WHERE s.examiner_id=? ORDER BY b.id",
                        (g.user["id"],)))
