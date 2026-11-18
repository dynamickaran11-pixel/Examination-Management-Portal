import re
import secrets

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db, tx

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def home_for(role):
    return url_for(f"{role}.dashboard")


@bp.route("/")
def index():
    return redirect(home_for(g.user["role"]) if g.user else url_for("auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(home_for(g.user["role"]))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        u = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and check_password_hash(u["password_hash"], pw):
            session.clear()
            session["uid"] = u["id"]
            session["_csrf"] = secrets.token_hex(16)
            return redirect(home_for(u["role"]))
        flash("Email or password is incorrect.", "error")
    return render_template("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(home_for(g.user["role"]))
    f = request.form
    if request.method == "POST":
        name, email = f.get("full_name", "").strip(), f.get("email", "").strip().lower()
        pw, role = f.get("password", ""), f.get("role", "student")
        phone, dept = f.get("phone", "").strip(), f.get("department", "").strip()
        errors = []
        if len(name) < 2:
            errors.append("Please enter your full name.")
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        if len(pw) < 6:
            errors.append("Password needs at least 6 characters.")
        if role not in ("student", "examiner"):
            errors.append("Choose student or examiner.")
        if phone and not re.fullmatch(r"[0-9+\-\s]{7,15}", phone):
            errors.append("Phone number looks invalid.")
        if role == "examiner" and not dept:
            errors.append("Examiners must give a department.")
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            errors.append("That email is already registered.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            status = "pending" if role == "examiner" else "active"
            with tx(db):
                db.execute(
                    "INSERT INTO users (email,password_hash,role,full_name,phone,department,status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (email, generate_password_hash(pw), role, name, phone, dept, status))
            flash("Registered. Wait for Admin approval before using the examiner dashboard."
                  if role == "examiner" else "Registered. You can sign in now.", "ok")
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", f=f)


@bp.route("/pending")
def pending():
    if not g.user:
        return redirect(url_for("auth.login"))
    if g.user["role"] != "examiner" or g.user["status"] == "active":
        return redirect(home_for(g.user["role"]))
    return render_template("auth/pending.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
