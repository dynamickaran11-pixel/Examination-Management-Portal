"""SQLite access + programmatic schema creation. No manual DB work needed."""
import os
import sqlite3
from contextlib import contextmanager

from flask import current_app, g
from werkzeug.security import generate_password_hash

ADMIN_EMAIL = "admin@emp.edu"
ADMIN_PASSWORD = "admin123"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','examiner','student')),
    full_name     TEXT NOT NULL,
    phone         TEXT,
    department    TEXT,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','pending','inactive')),
    created_on    TEXT NOT NULL DEFAULT (date('now','localtime'))
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'Active' CHECK (status IN ('Active','Inactive'))
);

CREATE TABLE IF NOT EXISTS exams (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id         INTEGER NOT NULL REFERENCES courses(id),
    name              TEXT NOT NULL,
    type              TEXT NOT NULL
                      CHECK (type IN ('Viva','Practical','Project Demo','Assessment')),
    duration          INTEGER NOT NULL,              -- minutes
    max_marks         INTEGER NOT NULL,
    slot_create_start TEXT NOT NULL,
    slot_create_end   TEXT NOT NULL,
    book_start        TEXT NOT NULL,
    book_end          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'Draft'
                      CHECK (status IN ('Draft','Slot Creation','Booking Open','Closed','Completed')),
    results_published INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exam_examiners (
    exam_id     INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    examiner_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (exam_id, examiner_id)
);

CREATE TABLE IF NOT EXISTS rubrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id     INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    criterion   TEXT NOT NULL,
    max_marks   REAL NOT NULL CHECK (max_marks > 0),
    weightage   REAL NOT NULL DEFAULT 0,
    description TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id         INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    examiner_id     INTEGER NOT NULL REFERENCES users(id),
    date            TEXT NOT NULL,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    capacity        INTEGER NOT NULL CHECK (capacity > 0),
    available_seats INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'Available'
                    CHECK (status IN ('Available','Full','Cancelled','Completed'))
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id   INTEGER NOT NULL REFERENCES users(id),
    slot_id      INTEGER NOT NULL REFERENCES slots(id),
    exam_id      INTEGER NOT NULL REFERENCES exams(id),
    booking_date TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status       TEXT NOT NULL DEFAULT 'Booked'
                 CHECK (status IN ('Booked','Cancelled','Completed'))
);
-- one live booking per student per examination; cancelled rows stay as history
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_booking
    ON bookings(student_id, exam_id) WHERE status IN ('Booked','Completed');

CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id   INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    total_marks  REAL NOT NULL,
    remarks      TEXT,
    evaluated_on TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS scores (
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    rubric_id     INTEGER NOT NULL REFERENCES rubrics(id) ON DELETE CASCADE,
    marks         REAL NOT NULL,
    PRIMARY KEY (evaluation_id, rubric_id)
);
"""


def _connect(path):
    con = sqlite3.connect(path, isolation_level=None)  # we manage transactions
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


@contextmanager
def tx(con):
    """Write transaction. Raise inside the block to roll back."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def init_db(app):
    """Create tables and the pre-existing Admin account."""
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    con = _connect(app.config["DATABASE"])
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        con.execute(
            "INSERT INTO users (email, password_hash, role, full_name, status) "
            "VALUES (?,?,?,?,?)",
            (ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD), "admin",
             "Administrator", "active"),
        )
    con.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    init_db(app)
