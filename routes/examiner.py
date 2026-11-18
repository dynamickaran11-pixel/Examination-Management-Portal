from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)

from db import get_db, tx
from logic import (examiner_conflict, refresh_slot, slot_creation_open, to_float,
                   to_int, valid_date, valid_time, weighted_total)

bp = Blueprint("examiner", __name__, url_prefix="/examiner")


@bp.before_request
def guard():
    if not g.user:
        return redirect(url_for("auth.login"))
    if g.user["role"] != "examiner":
        abort(403)
    if g.user["status"] != "active":  # waiting for / withdrawn Admin approval
        return redirect(url_for("auth.pending"))


def _my_exam(eid):
    db = get_db()
    e = db.execute(
        "SELECT e.*, c.code, c.name AS course_name FROM exams e JOIN courses c ON c.id=e.course_id "
        "JOIN exam_examiners x ON x.exam_id=e.id WHERE e.id=? AND x.examiner_id=?",
        (eid, g.user["id"])).fetchone()
    if not e:
        abort(404)
    return e


def _my_slot(sid):
    s = get_db().execute(
        "SELECT s.*, e.name AS exam, e.status AS exam_status, e.max_marks FROM slots s "
        "JOIN exams e ON e.id=s.exam_id WHERE s.id=? AND s.examiner_id=?",
        (sid, g.user["id"])).fetchone()
    if not s:
        abort(404)
    return s


@bp.route("/")
def dashboard():
    db, me = get_db(), g.user["id"]
    exams = db.execute(
        "SELECT e.*, c.code, (SELECT COUNT(*) FROM slots s WHERE s.exam_id=e.id AND s.examiner_id=?) "
        "AS my_slots FROM exams e JOIN courses c ON c.id=e.course_id "
        "JOIN exam_examiners x ON x.exam_id=e.id WHERE x.examiner_id=? ORDER BY e.id DESC", (me, me)).fetchall()
    slots = db.execute(
        "SELECT s.*, e.name AS exam, (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id "
        "AND b.status='Booked') AS n_booked FROM slots s JOIN exams e ON e.id=s.exam_id "
        "WHERE s.examiner_id=? ORDER BY s.date, s.start_time", (me,)).fetchall()
    pending = db.execute(
        "SELECT b.id, u.full_name AS student, e.name AS exam, s.date, s.start_time "
        "FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN users u ON u.id=b.student_id "
        "JOIN exams e ON e.id=b.exam_id WHERE s.examiner_id=? AND b.status='Booked' "
        "ORDER BY s.date, s.start_time", (me,)).fetchall()
    stats = [("Assigned exams", len(exams)), ("My slots", len(slots)),
             ("Students booked", sum(s["n_booked"] for s in slots)),
             ("Pending evaluations", len(pending))]
    return render_template("examiner/dashboard.html", stats=stats, exams=exams, slots=slots,
                           pending=pending)


# ── slots ──
@bp.route("/exams/<int:eid>")
def exam(eid):
    db = get_db()
    e = _my_exam(eid)
    slots = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id) AS n_rows, "
        "(SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status!='Cancelled') AS n_booked "
        "FROM slots s WHERE s.exam_id=? AND s.examiner_id=? ORDER BY s.date, s.start_time",
        (eid, g.user["id"])).fetchall()
    rubrics = db.execute("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (eid,)).fetchall()
    editable = e["status"] in ("Draft", "Slot Creation", "Closed")
    return render_template("examiner/exam.html", e=e, slots=slots, rubrics=rubrics,
                           can_create=slot_creation_open(e), editable=editable, f=request.form)


def _slot_values(e, exclude=None):
    f = request.form
    day, st, en, cap = f.get("date"), f.get("start_time"), f.get("end_time"), to_int(f.get("capacity"))
    errors = []
    if not valid_date(day):
        errors.append("Pick a valid date.")
    if not (valid_time(st) and valid_time(en)):
        errors.append("Start and end times are required.")
    elif st >= en:
        errors.append("End time must be after start time.")
    if not cap or cap < 1 or cap > 500:
        errors.append("Capacity must be between 1 and 500.")
    if not errors and examiner_conflict(get_db(), g.user["id"], day, st, en, exclude):
        errors.append("You already have a slot overlapping that time.")
    for er in errors:
        flash(er, "error")
    return None if errors else (day, st, en, cap)


@bp.route("/exams/<int:eid>/slots", methods=["POST"])
def slot_create(eid):
    db = get_db()
    e = _my_exam(eid)
    if not slot_creation_open(e):
        flash("Slot creation is closed for this examination.", "error")
        return redirect(url_for("examiner.exam", eid=eid))
    v = _slot_values(e)
    if v:
        with tx(db):
            db.execute("INSERT INTO slots (exam_id,examiner_id,date,start_time,end_time,capacity,"
                       "available_seats) VALUES (?,?,?,?,?,?,?)",
                       (eid, g.user["id"], v[0], v[1], v[2], v[3], v[3]))
        flash("Slot created.", "ok")
    return redirect(url_for("examiner.exam", eid=eid))


def _slot_locked(s):
    """Editable only before booking starts: exam not in Booking Open/Completed, no bookings yet."""
    db = get_db()
    if s["exam_status"] in ("Booking Open", "Completed"):
        return "Booking has started, so this slot is locked."
    if db.execute("SELECT 1 FROM bookings WHERE slot_id=?", (s["id"],)).fetchone():
        return "Students have booked this slot, so it is locked."
    return None


@bp.route("/slots/<int:sid>/edit", methods=["GET", "POST"])
def slot_edit(sid):
    db = get_db()
    s = _my_slot(sid)
    msg = _slot_locked(s)
    if msg:
        flash(msg, "error")
        return redirect(url_for("examiner.exam", eid=s["exam_id"]))
    if request.method == "POST":
        v = _slot_values(s, exclude=sid)
        if v:
            with tx(db):
                db.execute("UPDATE slots SET date=?,start_time=?,end_time=?,capacity=?,"
                           "available_seats=? WHERE id=?", (*v, v[3], sid))
            flash("Slot updated.", "ok")
            return redirect(url_for("examiner.exam", eid=s["exam_id"]))
        s = request.form
    return render_template("examiner/slot_form.html", s=s, sid=sid, eid=_my_slot(sid)["exam_id"])


@bp.route("/slots/<int:sid>/delete", methods=["POST"])
def slot_delete(sid):
    db = get_db()
    s = _my_slot(sid)
    msg = _slot_locked(s)
    if msg:
        flash(msg, "error")
    else:
        with tx(db):
            db.execute("DELETE FROM slots WHERE id=?", (sid,))
        flash("Slot removed.", "ok")
    return redirect(url_for("examiner.exam", eid=s["exam_id"]))


# ── students & evaluation ──
@bp.route("/slots/<int:sid>")
def slot(sid):
    s = _my_slot(sid)
    rows = get_db().execute(
        "SELECT b.*, u.full_name AS student, u.email, u.department, ev.total_marks "
        "FROM bookings b JOIN users u ON u.id=b.student_id "
        "LEFT JOIN evaluations ev ON ev.booking_id=b.id "
        "WHERE b.slot_id=? AND b.status!='Cancelled' ORDER BY u.full_name", (sid,)).fetchall()
    return render_template("examiner/slot.html", s=s, rows=rows)


@bp.route("/bookings/<int:bid>/evaluate", methods=["GET", "POST"])
def evaluate(bid):
    db = get_db()
    b = db.execute(  # ownership: the booking's slot must belong to this examiner
        "SELECT b.*, u.full_name AS student, e.name AS exam, e.max_marks, e.results_published, "
        "s.date, s.start_time FROM bookings b JOIN slots s ON s.id=b.slot_id "
        "JOIN users u ON u.id=b.student_id JOIN exams e ON e.id=b.exam_id "
        "WHERE b.id=? AND s.examiner_id=? AND b.status!='Cancelled'", (bid, g.user["id"])).fetchone()
    if not b:
        abort(404)
    rubrics = db.execute("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (b["exam_id"],)).fetchall()
    ev = db.execute("SELECT * FROM evaluations WHERE booking_id=?", (bid,)).fetchone()
    prev = {}
    if ev:
        prev = {r["rubric_id"]: r["marks"] for r in
                db.execute("SELECT * FROM scores WHERE evaluation_id=?", (ev["id"],))}
    if request.method == "POST":
        if b["results_published"]:
            flash("Results are published; marks can no longer change.", "error")
            return redirect(url_for("examiner.slot", sid=b["slot_id"]))
        marks, errors = {}, []
        for r in rubrics:
            m = to_float(request.form.get(f"m_{r['id']}"))
            if m is None or m < 0 or m > r["max_marks"]:
                errors.append(f"{r['criterion']}: enter marks from 0 to {r['max_marks']:g}.")
            else:
                marks[r["id"]] = m
        if not rubrics:
            errors.append("No rubric exists for this examination yet. Ask the Admin to add one.")
        remarks = request.form.get("remarks", "").strip()
        if errors:
            for er in errors:
                flash(er, "error")
            prev = {r["id"]: request.form.get(f"m_{r['id']}") for r in rubrics}
            return render_template("examiner/evaluate.html", b=b, rubrics=rubrics, prev=prev,
                                   remarks=remarks)
        total = weighted_total(b["max_marks"], rubrics, marks)
        with tx(db):
            if ev:
                db.execute("UPDATE evaluations SET total_marks=?, remarks=?, "
                           "evaluated_on=datetime('now','localtime') WHERE id=?",
                           (total, remarks, ev["id"]))
                eid_ = ev["id"]
                db.execute("DELETE FROM scores WHERE evaluation_id=?", (eid_,))
            else:
                eid_ = db.execute("INSERT INTO evaluations (booking_id,total_marks,remarks) "
                                  "VALUES (?,?,?)", (bid, total, remarks)).lastrowid
            for rid, m in marks.items():
                db.execute("INSERT INTO scores VALUES (?,?,?)", (eid_, rid, m))
            db.execute("UPDATE bookings SET status='Completed' WHERE id=?", (bid,))
            refresh_slot(db, b["slot_id"])
        flash(f"Saved. {b['student']} scored {total:g} / {b['max_marks']}.", "ok")
        return redirect(url_for("examiner.slot", sid=b["slot_id"]))
    return render_template("examiner/evaluate.html", b=b, rubrics=rubrics, prev=prev,
                           remarks=ev["remarks"] if ev else "")
