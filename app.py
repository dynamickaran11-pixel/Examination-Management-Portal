import os
import secrets
from datetime import datetime

from flask import Flask, abort, g, render_template, request, session

import db
from logic import csrf_field, today


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("EMP_SECRET", "change-me-in-production")
    app.config["DATABASE"] = db_path or os.path.join(app.instance_path, "emp.db")
    db.init_app(app)

    from routes import admin, api, auth, examiner, student
    for m in (auth, admin, examiner, student, api):
        app.register_blueprint(m.bp)

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = db.get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if g.user is None:
                session.pop("uid", None)
        if request.method == "POST":
            sent, real = request.form.get("_csrf", ""), session.get("_csrf", "")
            if not real or not secrets.compare_digest(sent, real):
                abort(400)

    # globals (not context processors) so imported macros can see them too
    app.jinja_env.globals.update(csrf_field=csrf_field, today=today)

    @app.template_filter("slug")
    def slug(s):
        return str(s).lower().replace(" ", "-")

    @app.template_filter("dmy")
    def dmy(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%d %b %Y")
        except (TypeError, ValueError):
            return s or ""

    @app.template_filter("num")
    def num(x):
        return "—" if x is None else ("%g" % x)

    for code, msg in ((400, "That request could not be verified."),
                      (403, "This page is not meant for you."),
                      (404, "Nothing here.")):
        app.register_error_handler(
            code, lambda e, code=code, msg=msg: (render_template("error.html", code=code, msg=msg), code))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
