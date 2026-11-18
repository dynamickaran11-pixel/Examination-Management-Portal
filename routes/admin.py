from datetime import date, timedelta

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)

from db import get_db, tx
from logic import (EXAM_TYPES, STATUSES, examiner_conflict, refresh_slot,
                   to_float, to_int, valid_date)

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
def guard():
    if not g.user:
        return redirect(url_for("auth.login"))
    if g.user["role"] != "admin":
        abort(403)


def like(q):
    return f"%{q}%"


# ───────────────────────── dashboard ─────────────────────────
@bp.route("/")
def dashboard():
    db = get_db()
    n = lambda sql: db.execute(sql).fetchone()[0]
    stats = [
        ("Courses", n("SELECT COUNT(*) FROM courses")),
        ("Examinations", n("SELECT COUNT(*) FROM exams")),
        ("Examiners", n("SELECT COUNT(*) FROM users WHERE role='examiner'")),
        ("Students", n("SELECT COUNT(*) FROM users WHERE role='student'")),
        ("Slots", n("SELECT COUNT(*) FROM slots")),
        ("Bookings", n("SELECT COUNT(*) FROM bookings")),
    ]
    pending = db.execute(
        "SELECT * FROM users WHERE role='examiner' AND status='pending' ORDER BY id").fetchall()
    exams = db.execute(
        "SELECT e.*, c.code FROM exams e JOIN courses c ON c.id=e.course_id "
        "ORDER BY e.id DESC LIMIT 6").fetchall()
    return render_template("admin/dashboard.html", stats=stats, pending=pending, exams=exams)


# ───────────────────────── courses ─────────────────────────
@bp.route("/courses")
def courses():
    q = request.args.get("q", "").strip()
    sql = ("SELECT c.*, (SELECT COUNT(*) FROM exams e WHERE e.course_id=c.id) AS n_exams "
           "FROM courses c")
    params = []
    if q:
        sql += " WHERE c.name LIKE ? OR c.code LIKE ? OR CAST(c.id AS TEXT)=?"
        params = [like(q), like(q), q]
    rows = get_db().execute(sql + " ORDER BY c.id DESC", params).fetchall()
    return render_template("admin/courses.html", rows=rows, q=q)


def _course_save(cid):
    db, f = get_db(), request.form
    code, name = f.get("code", "").strip().upper(), f.get("name", "").strip()
    desc, status = f.get("description", "").strip(), f.get("status", "Active")
    errors = []
    if not code:
        errors.append("Course code is required.")
    if not name:
        errors.append("Course name is required.")
    if status not in ("Active", "Inactive"):
        errors.append("Invalid status.")
    if code and db.execute("SELECT 1 FROM courses WHERE code=? AND id IS NOT ?",
                           (code, cid)).fetchone():
        errors.append("That course code already exists.")
    if errors:
        for e in errors:
            flash(e, "error")
        return False
    with tx(db):
        if cid:
            db.execute("UPDATE courses SET code=?,name=?,description=?,status=? WHERE id=?",
                       (code, name, desc, status, cid))
        else:
            db.execute("INSERT INTO courses (code,name,description,status) VALUES (?,?,?,?)",
                       (code, name, desc, status))
    flash("Course saved.", "ok")
    return True


@bp.route("/courses/new", methods=["GET", "POST"])
def course_new():
    if request.method == "POST" and _course_save(None):
        return redirect(url_for("admin.courses"))
    return render_template("admin/course_form.html", c=request.form, editing=False)


@bp.route("/courses/<int:cid>/edit", methods=["GET", "POST"])
def course_edit(cid):
    c = get_db().execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    if request.method == "POST":
        if _course_save(cid):
            return redirect(url_for("admin.courses"))
        c = request.form
    return render_template("admin/course_form.html", c=c, editing=True, cid=cid)


@bp.route("/courses/<int:cid>/delete", methods=["POST"])
def course_delete(cid):
    db = get_db()
    if db.execute("SELECT 1 FROM exams WHERE course_id=?", (cid,)).fetchone():
        flash("This course has examinations. Mark it Inactive instead of deleting.", "error")
    else:
        with tx(db):
            db.execute("DELETE FROM courses WHERE id=?", (cid,))
        flash("Course removed.", "ok")
    return redirect(url_for("admin.courses"))


# ───────────────────────── examinations ─────────────────────────
@bp.route("/exams")
def exams():
    q = request.args.get("q", "").strip()
    st = request.args.get("status", "")
    sql = ("SELECT e.*, c.code, c.name AS course_name, "
           "(SELECT COUNT(*) FROM slots s WHERE s.exam_id=e.id) AS n_slots, "
           "(SELECT COUNT(*) FROM bookings b WHERE b.exam_id=e.id AND b.status!='Cancelled') AS n_book "
           "FROM exams e JOIN courses c ON c.id=e.course_id WHERE 1=1")
    params = []
    if q:
        sql += " AND (e.name LIKE ? OR c.code LIKE ? OR c.name LIKE ? OR CAST(e.id AS TEXT)=?)"
        params += [like(q), like(q), like(q), q]
    if st in STATUSES:
        sql += " AND e.status=?"
        params.append(st)
    rows = get_db().execute(sql + " ORDER BY e.id DESC", params).fetchall()
    return render_template("admin/exams.html", rows=rows, q=q, st_=st, statuses=STATUSES)


def _exam_save(eid):
    db, f = get_db(), request.form
    d = dict(
        course_id=to_int(f.get("course_id")), name=f.get("name", "").strip(),
        type=f.get("type"), duration=to_int(f.get("duration")),
        max_marks=to_int(f.get("max_marks")),
        slot_create_start=f.get("slot_create_start"), slot_create_end=f.get("slot_create_end"),
        book_start=f.get("book_start"), book_end=f.get("book_end"))
    errors = []
    if not d["course_id"] or not db.execute("SELECT 1 FROM courses WHERE id=?",
                                            (d["course_id"],)).fetchone():
        errors.append("Choose a course.")
    if not d["name"]:
        errors.append("Examination name is required.")
    if d["type"] not in EXAM_TYPES:
        errors.append("Choose an examination type.")
    if not d["duration"] or d["duration"] <= 0:
        errors.append("Duration must be a positive number of minutes.")
    if not d["max_marks"] or d["max_marks"] <= 0:
        errors.append("Maximum marks must be positive.")
    for k in ("slot_create_start", "slot_create_end", "book_start", "book_end"):
        if not valid_date(d[k]):
            errors.append("All four dates are required.")
            break
    else:
        if d["slot_create_start"] > d["slot_create_end"]:
            errors.append("Slot creation must end on or after it starts.")
        if d["book_start"] > d["book_end"]:
            errors.append("Booking must end on or after it starts.")
    if errors:
        for e in errors:
            flash(e, "error")
        return None
    with tx(db):
        if eid:
            db.execute(
                "UPDATE exams SET course_id=:course_id,name=:name,type=:type,duration=:duration,"
                "max_marks=:max_marks,slot_create_start=:slot_create_start,"
                "slot_create_end=:slot_create_end,book_start=:book_start,book_end=:book_end "
                "WHERE id=:id", {**d, "id": eid})
            new_id = eid
        else:
            cur = db.execute(
                "INSERT INTO exams (course_id,name,type,duration,max_marks,slot_create_start,"
                "slot_create_end,book_start,book_end) VALUES (:course_id,:name,:type,:duration,"
                ":max_marks,:slot_create_start,:slot_create_end,:book_start,:book_end)", d)
            new_id = cur.lastrowid
    flash("Examination saved.", "ok")
    return new_id


def _exam_form_defaults():
    t = date.today()
    return {"slot_create_start": t.isoformat(),
            "slot_create_end": (t + timedelta(days=7)).isoformat(),
            "book_start": t.isoformat(),
            "book_end": (t + timedelta(days=14)).isoformat(),
            "duration": 30, "max_marks": 100}


@bp.route("/exams/new", methods=["GET", "POST"])
def exam_new():
    if request.method == "POST":
        eid = _exam_save(None)
        if eid:
            return redirect(url_for("admin.exam_detail", eid=eid))
    courses = get_db().execute("SELECT * FROM courses WHERE status='Active' ORDER BY code").fetchall()
    e = request.form if request.method == "POST" else _exam_form_defaults()
    return render_template("admin/exam_form.html", e=e, courses=courses, types=EXAM_TYPES,
                           editing=False)


@bp.route("/exams/<int:eid>/edit", methods=["GET", "POST"])
def exam_edit(eid):
    db = get_db()
    e = db.execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    if request.method == "POST":
        if _exam_save(eid):
            return redirect(url_for("admin.exam_detail", eid=eid))
        e = request.form
    courses = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    return render_template("admin/exam_form.html", e=e, courses=courses, types=EXAM_TYPES,
                           editing=True, eid=eid)


@bp.route("/exams/<int:eid>/delete", methods=["POST"])
def exam_delete(eid):
    db = get_db()
    if db.execute("SELECT 1 FROM bookings WHERE exam_id=?", (eid,)).fetchone():
        flash("This examination already has bookings. Close or complete it instead.", "error")
        return redirect(url_for("admin.exam_detail", eid=eid))
    with tx(db):
        db.execute("DELETE FROM exams WHERE id=?", (eid,))
    flash("Examination removed.", "ok")
    return redirect(url_for("admin.exams"))


ACTIONS = {  # action -> (new status, label)
    "open_slots": ("Slot Creation", "Open slot creation"),
    "open_booking": ("Booking Open", "Open student booking"),
    "close": ("Closed", "Close"),
    "complete": ("Completed", "Mark completed"),
}
NEXT = {  # current status -> available actions
    "Draft": ["open_slots"],
    "Slot Creation": ["close"],
    "Closed": ["open_slots", "open_booking", "complete"],
    "Booking Open": ["close"],
    "Completed": ["close"],
}


@bp.route("/exams/<int:eid>")
def exam_detail(eid):
    db = get_db()
    e = db.execute("SELECT e.*, c.code, c.name AS course_name FROM exams e "
                   "JOIN courses c ON c.id=e.course_id WHERE e.id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    rubrics = db.execute("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (eid,)).fetchall()
    assigned = db.execute(
        "SELECT u.* FROM exam_examiners x JOIN users u ON u.id=x.examiner_id "
        "WHERE x.exam_id=? ORDER BY u.full_name", (eid,)).fetchall()
    available = db.execute(
        "SELECT * FROM users WHERE role='examiner' AND status='active' AND id NOT IN "
        "(SELECT examiner_id FROM exam_examiners WHERE exam_id=?) ORDER BY full_name",
        (eid,)).fetchall()
    slots = db.execute(
        "SELECT s.*, u.full_name AS examiner FROM slots s JOIN users u ON u.id=s.examiner_id "
        "WHERE s.exam_id=? ORDER BY s.date, s.start_time", (eid,)).fetchall()
    bookings = db.execute(
        "SELECT b.*, u.full_name AS student, s.date, s.start_time FROM bookings b "
        "JOIN users u ON u.id=b.student_id JOIN slots s ON s.id=b.slot_id "
        "WHERE b.exam_id=? ORDER BY b.id DESC", (eid,)).fetchall()
    return render_template(
        "admin/exam_detail.html", e=e, rubrics=rubrics, assigned=assigned, available=available,
        slots=slots, bookings=bookings, actions=[(a, ACTIONS[a][1]) for a in NEXT[e["status"]]],
        w_sum=sum(r["weightage"] for r in rubrics), m_sum=sum(r["max_marks"] for r in rubrics))


@bp.route("/exams/<int:eid>/status", methods=["POST"])
def exam_status(eid):
    db = get_db()
    e = db.execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
    action = request.form.get("action")
    if not e or action not in ACTIONS or action not in NEXT[e["status"]]:
        abort(400)
    if action == "open_booking" and not db.execute(
            "SELECT 1 FROM slots WHERE exam_id=?", (eid,)).fetchone():
        flash("No slots exist yet. Examiners must create slots before booking can open.", "error")
        return redirect(url_for("admin.exam_detail", eid=eid))
    with tx(db):
        db.execute("UPDATE exams SET status=? WHERE id=?", (ACTIONS[action][0], eid))
    flash(f"Status is now {ACTIONS[action][0]}.", "ok")
    return redirect(url_for("admin.exam_detail", eid=eid))


@bp.route("/exams/<int:eid>/publish", methods=["POST"])
def exam_publish(eid):
    db = get_db()
    e = db.execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    new = 0 if e["results_published"] else 1
    with tx(db):
        db.execute("UPDATE exams SET results_published=? WHERE id=?", (new, eid))
    flash("Results published to students." if new else "Results hidden from students.", "ok")
    return redirect(url_for("admin.exam_detail", eid=eid))


# ── rubrics ──
def _rubric_values():
    f = request.form
    crit, mx, wt = f.get("criterion", "").strip(), to_float(f.get("max_marks")), to_float(f.get("weightage"))
    errors = []
    if not crit:
        errors.append("Criterion name is required.")
    if mx is None or mx <= 0:
        errors.append("Maximum marks must be above zero.")
    if wt is None or wt < 0 or wt > 100:
        errors.append("Weightage must be between 0 and 100.")
    for e in errors:
        flash(e, "error")
    return None if errors else (crit, mx, wt, f.get("description", "").strip())


@bp.route("/exams/<int:eid>/rubrics", methods=["POST"])
def rubric_add(eid):
    db = get_db()
    if not db.execute("SELECT 1 FROM exams WHERE id=?", (eid,)).fetchone():
        abort(404)
    v = _rubric_values()
    if v:
        with tx(db):
            db.execute("INSERT INTO rubrics (exam_id,criterion,max_marks,weightage,description) "
                       "VALUES (?,?,?,?,?)", (eid, *v))
        flash("Criterion added.", "ok")
    return redirect(url_for("admin.exam_detail", eid=eid) + "#rubrics")


@bp.route("/rubrics/<int:rid>/edit", methods=["GET", "POST"])
def rubric_edit(rid):
    db = get_db()
    r = db.execute("SELECT * FROM rubrics WHERE id=?", (rid,)).fetchone()
    if not r:
        abort(404)
    if request.method == "POST":
        v = _rubric_values()
        if v:
            with tx(db):
                db.execute("UPDATE rubrics SET criterion=?,max_marks=?,weightage=?,description=? "
                           "WHERE id=?", (*v, rid))
            flash("Criterion updated.", "ok")
            return redirect(url_for("admin.exam_detail", eid=r["exam_id"]) + "#rubrics")
        r = request.form
    return render_template("admin/rubric_form.html", r=r, rid=rid,
                           eid=db.execute("SELECT exam_id FROM rubrics WHERE id=?", (rid,)).fetchone()[0])


@bp.route("/rubrics/<int:rid>/delete", methods=["POST"])
def rubric_delete(rid):
    db = get_db()
    r = db.execute("SELECT * FROM rubrics WHERE id=?", (rid,)).fetchone()
    if not r:
        abort(404)
    if db.execute("SELECT 1 FROM scores WHERE rubric_id=?", (rid,)).fetchone():
        flash("Marks were already awarded on this criterion, so it cannot be removed.", "error")
    else:
        with tx(db):
            db.execute("DELETE FROM rubrics WHERE id=?", (rid,))
        flash("Criterion removed.", "ok")
    return redirect(url_for("admin.exam_detail", eid=r["exam_id"]) + "#rubrics")


# ── assigned examiners ──
@bp.route("/exams/<int:eid>/examiners", methods=["POST"])
def exam_assign(eid):
    db = get_db()
    xid = to_int(request.form.get("examiner_id"))
    ok = db.execute("SELECT 1 FROM users WHERE id=? AND role='examiner' AND status='active'",
                    (xid,)).fetchone()
    if not ok or not db.execute("SELECT 1 FROM exams WHERE id=?", (eid,)).fetchone():
        flash("Choose an approved examiner.", "error")
    else:
        with tx(db):
            db.execute("INSERT OR IGNORE INTO exam_examiners VALUES (?,?)", (eid, xid))
        flash("Examiner assigned.", "ok")
    return redirect(url_for("admin.exam_detail", eid=eid) + "#examiners")


@bp.route("/exams/<int:eid>/examiners/<int:xid>/remove", methods=["POST"])
def exam_unassign(eid, xid):
    db = get_db()
    if db.execute("SELECT 1 FROM slots WHERE exam_id=? AND examiner_id=?", (eid, xid)).fetchone():
        flash("That examiner already owns slots here. Change the slot's examiner first.", "error")
    else:
        with tx(db):
            db.execute("DELETE FROM exam_examiners WHERE exam_id=? AND examiner_id=?", (eid, xid))
        flash("Examiner removed from this examination.", "ok")
    return redirect(url_for("admin.exam_detail", eid=eid) + "#examiners")


# ───────────────────────── examiners & students ─────────────────────────
@bp.route("/examiners")
def examiners():
    q, st = request.args.get("q", "").strip(), request.args.get("status", "")
    sql = ("SELECT u.*, (SELECT COUNT(*) FROM slots s WHERE s.examiner_id=u.id) AS n_slots, "
           "(SELECT COUNT(*) FROM exam_examiners x WHERE x.examiner_id=u.id) AS n_exams "
           "FROM users u WHERE role='examiner'")
    params = []
    if q:
        sql += " AND (u.full_name LIKE ? OR u.email LIKE ? OR u.department LIKE ? OR CAST(u.id AS TEXT)=?)"
        params += [like(q), like(q), like(q), q]
    if st in ("pending", "active", "inactive"):
        sql += " AND u.status=?"
        params.append(st)
    rows = get_db().execute(sql + " ORDER BY u.id DESC", params).fetchall()
    return render_template("admin/examiners.html", rows=rows, q=q, st_=st)


@bp.route("/examiners/<int:uid>/status", methods=["POST"])
def examiner_status(uid):
    db = get_db()
    new = request.form.get("status")
    u = db.execute("SELECT * FROM users WHERE id=? AND role='examiner'", (uid,)).fetchone()
    if not u or new not in ("active", "inactive"):
        abort(400)
    with tx(db):
        db.execute("UPDATE users SET status=? WHERE id=?", (new, uid))
    flash(f"{u['full_name']} is now {new}.", "ok")
    return redirect(request.referrer or url_for("admin.examiners"))


@bp.route("/students")
def students():
    q = request.args.get("q", "").strip()
    sql = ("SELECT u.*, (SELECT COUNT(*) FROM bookings b WHERE b.student_id=u.id) AS n_book "
           "FROM users u WHERE role='student'")
    params = []
    if q:
        sql += " AND (u.full_name LIKE ? OR u.email LIKE ? OR u.department LIKE ? OR CAST(u.id AS TEXT)=?)"
        params = [like(q), like(q), like(q), q]
    rows = get_db().execute(sql + " ORDER BY u.id DESC", params).fetchall()
    return render_template("admin/students.html", rows=rows, q=q)


# ───────────────────────── slots ─────────────────────────
@bp.route("/slots")
def slots():
    q = request.args.get("q", "").strip()
    sql = ("SELECT s.*, e.name AS exam, u.full_name AS examiner FROM slots s "
           "JOIN exams e ON e.id=s.exam_id JOIN users u ON u.id=s.examiner_id WHERE 1=1")
    params = []
    if q:
        sql += " AND (e.name LIKE ? OR u.full_name LIKE ? OR CAST(s.id AS TEXT)=? OR CAST(e.id AS TEXT)=?)"
        params = [like(q), like(q), q, q]
    rows = get_db().execute(sql + " ORDER BY s.date DESC, s.start_time", params).fetchall()
    return render_template("admin/slots.html", rows=rows, q=q)


@bp.route("/slots/<int:sid>/examiner", methods=["GET", "POST"])
def slot_examiner(sid):
    db = get_db()
    s = db.execute("SELECT s.*, e.name AS exam FROM slots s JOIN exams e ON e.id=s.exam_id "
                   "WHERE s.id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    options = db.execute("SELECT * FROM users WHERE role='examiner' AND status='active' "
                         "AND id != ? ORDER BY full_name", (s["examiner_id"],)).fetchall()
    if request.method == "POST":
        xid = to_int(request.form.get("examiner_id"))
        if xid not in [o["id"] for o in options]:
            flash("Choose an approved examiner.", "error")
        elif examiner_conflict(db, xid, s["date"], s["start_time"], s["end_time"], sid):
            flash("That examiner already has an overlapping slot at this time.", "error")
        else:
            with tx(db):
                db.execute("UPDATE slots SET examiner_id=? WHERE id=?", (xid, sid))
                db.execute("INSERT OR IGNORE INTO exam_examiners VALUES (?,?)", (s["exam_id"], xid))
            flash("Examiner changed. Their students stay booked.", "ok")
            return redirect(url_for("admin.slots"))
    cur = db.execute("SELECT full_name FROM users WHERE id=?", (s["examiner_id"],)).fetchone()[0]
    return render_template("admin/slot_examiner.html", s=s, options=options, cur=cur)


# ───────────────────────── bookings ─────────────────────────
@bp.route("/bookings")
def bookings():
    q, st = request.args.get("q", "").strip(), request.args.get("status", "")
    sql = ("SELECT b.*, u.full_name AS student, e.name AS exam, s.date, s.start_time, s.end_time, "
           "x.full_name AS examiner FROM bookings b JOIN users u ON u.id=b.student_id "
           "JOIN exams e ON e.id=b.exam_id JOIN slots s ON s.id=b.slot_id "
           "JOIN users x ON x.id=s.examiner_id WHERE 1=1")
    params = []
    if q:
        sql += (" AND (u.full_name LIKE ? OR e.name LIKE ? OR CAST(b.id AS TEXT)=? "
                "OR CAST(u.id AS TEXT)=? OR CAST(e.id AS TEXT)=?)")
        params = [like(q), like(q), q, q, q]
    if st in ("Booked", "Cancelled", "Completed"):
        sql += " AND b.status=?"
        params.append(st)
    rows = get_db().execute(sql + " ORDER BY b.id DESC", params).fetchall()
    return render_template("admin/bookings.html", rows=rows, q=q, st_=st)


@bp.route("/bookings/<int:bid>/reschedule", methods=["GET", "POST"])
def reschedule(bid):
    db = get_db()
    b = db.execute(
        "SELECT b.*, u.full_name AS student, e.name AS exam, s.date, s.start_time "
        "FROM bookings b JOIN users u ON u.id=b.student_id JOIN exams e ON e.id=b.exam_id "
        "JOIN slots s ON s.id=b.slot_id WHERE b.id=?", (bid,)).fetchone()
    if not b:
        abort(404)
    if b["status"] != "Booked":
        flash("Only active bookings can be rescheduled.", "error")
        return redirect(url_for("admin.bookings"))
    options = db.execute(
        "SELECT s.*, u.full_name AS examiner FROM slots s JOIN users u ON u.id=s.examiner_id "
        "WHERE s.exam_id=? AND s.status='Available' AND s.available_seats>0 AND s.id!=? "
        "ORDER BY s.date, s.start_time", (b["exam_id"], b["slot_id"])).fetchall()
    if request.method == "POST":
        new = to_int(request.form.get("slot_id"))
        try:
            with tx(db):
                s = db.execute("SELECT * FROM slots WHERE id=? AND exam_id=?",
                               (new, b["exam_id"])).fetchone()
                if not s or s["status"] != "Available" or s["available_seats"] <= 0:
                    raise ValueError("That slot is not available.")
                db.execute("UPDATE bookings SET slot_id=? WHERE id=? AND status='Booked'", (new, bid))
                refresh_slot(db, b["slot_id"])
                refresh_slot(db, new)
            flash("Booking moved.", "ok")
            return redirect(url_for("admin.bookings"))
        except ValueError as ex:
            flash(str(ex), "error")
    return render_template("admin/reschedule.html", b=b, options=options)
