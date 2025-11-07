import os
import secrets
import time
from functools import wraps
from typing import Optional

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# Reuse the bot's SQLite connection helper and schema
import bot as ticket_bot


def create_app() -> Flask:
    app = Flask(__name__)

    # Security: require a secret key for sessions/CSRF
    app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", secrets.token_hex(32))
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    # Auth credentials (simple session-based login)
    app.config["DASHBOARD_USERNAME"] = os.getenv("DASHBOARD_USERNAME")
    app.config["DASHBOARD_PASSWORD"] = os.getenv("DASHBOARD_PASSWORD")

    if not app.config["DASHBOARD_USERNAME"] or not app.config["DASHBOARD_PASSWORD"]:
        print(
            "[dashboard] WARNING: Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD to enable login."
        )

    # Ensure DB exists/migrated
    try:
        ticket_bot.init_db()
    except Exception:
        pass

    @app.context_processor
    def inject_globals():
        return {"DB_PATH": getattr(ticket_bot, "DB_PATH", "")}

    @app.before_request
    def _load_globals():
        g.conn = ticket_bot.get_conn()
        g.cur = g.conn.cursor()

    @app.teardown_request
    def _close_db(exc):
        try:
            if getattr(g, "conn", None):
                g.conn.commit()
                g.conn.close()
        except Exception:
            pass

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("stats", next=request.path))
            return fn(*args, **kwargs)
        return wrapper

    def require_csrf():
        token = session.get("csrf_token")
        form_token = request.form.get("csrf_token")
        if not token or not form_token or token != form_token:
            abort(400, description="Invalid CSRF token")

    @app.route("/stats", methods=["GET", "POST"])
    def stats():
        # Handle login POST first
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if (
                username == app.config.get("DASHBOARD_USERNAME")
                and password == app.config.get("DASHBOARD_PASSWORD")
            ):
                session["user"] = username
                session["csrf_token"] = secrets.token_hex(32)
                flash("Signed in", "success")
                return redirect(request.args.get("next") or url_for("stats"))
            flash("Invalid credentials", "error")

        # If not logged in, show login form
        if not session.get("user"):
            return render_template("login.html")

        # If logged in, show the same content as index (guild stats)
        guild_rows = []
        try:
            g.cur.execute(
                "SELECT guild_id FROM config UNION SELECT DISTINCT guild_id FROM tickets"
            )
            guild_ids = [r[0] for r in g.cur.fetchall()]
            for gid in guild_ids:
                g.cur.execute(
                    "SELECT COUNT(*) FROM categories WHERE guild_id = ? AND active = 1",
                    (gid,),
                )
                cat_count = g.cur.fetchone()[0]
                g.cur.execute(
                    "SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'open'",
                    (gid,),
                )
                open_count = g.cur.fetchone()[0]
                g.cur.execute(
                    "SELECT COUNT(*) FROM tickets WHERE guild_id = ?",
                    (gid,),
                )
                ticket_count = g.cur.fetchone()[0]
                guild_rows.append(
                    {
                        "guild_id": gid,
                        "categories": cat_count,
                        "open_tickets": open_count,
                        "total_tickets": ticket_count,
                    }
                )
        except Exception as e:
            flash(f"DB error: {e}", "error")
        return render_template("index.html", guilds=guild_rows)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("stats"))

    @app.route("/")
    @login_required
    def index():
        # Discover guilds from config and tickets
        guild_rows = []
        try:
            g.cur.execute(
                "SELECT guild_id FROM config UNION SELECT DISTINCT guild_id FROM tickets"
            )
            guild_ids = [r[0] for r in g.cur.fetchall()]
            for gid in guild_ids:
                # counts per guild
                g.cur.execute(
                    "SELECT COUNT(*) FROM categories WHERE guild_id = ? AND active = 1",
                    (gid,),
                )
                cat_count = g.cur.fetchone()[0]
                g.cur.execute(
                    "SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'open'",
                    (gid,),
                )
                open_count = g.cur.fetchone()[0]
                g.cur.execute(
                    "SELECT COUNT(*) FROM tickets WHERE guild_id = ?",
                    (gid,),
                )
                ticket_count = g.cur.fetchone()[0]
                guild_rows.append(
                    {
                        "guild_id": gid,
                        "categories": cat_count,
                        "open_tickets": open_count,
                        "total_tickets": ticket_count,
                    }
                )
        except Exception as e:
            flash(f"DB error: {e}", "error")
        return render_template("index.html", guilds=guild_rows)

    @app.route("/guild/<int:guild_id>")
    @login_required
    def guild_view(guild_id: int):
        # Config row
        g.cur.execute("SELECT * FROM config WHERE guild_id = ?", (guild_id,))
        config = g.cur.fetchone()
        # Categories + fields
        g.cur.execute(
            "SELECT * FROM categories WHERE guild_id = ? AND active = 1 ORDER BY name",
            (guild_id,),
        )
        categories = g.cur.fetchall()
        # Map category_id -> fields
        cat_fields = {}
        for c in categories:
            g.cur.execute(
                "SELECT * FROM fields WHERE category_id = ? ORDER BY order_index, id",
                (c["id"],),
            )
            cat_fields[c["id"]] = g.cur.fetchall()
        return render_template(
            "guild.html", guild_id=guild_id, config=config, categories=categories, cat_fields=cat_fields
        )

    @app.post("/guild/<int:guild_id>/config")
    @login_required
    def guild_update_config(guild_id: int):
        require_csrf()
        # Write selected config keys
        allowed = {
            "support_channel_id": int,
            "ticket_category_id": int,
            "staff_role_id": int,
            "panel_title": str,
            "panel_description": str,
            "contact_name": str,
            "allow_user_close": int,
        }
        updates = {}
        for k, caster in allowed.items():
            if k in request.form and request.form[k] != "":
                try:
                    updates[k] = caster(request.form[k])
                except Exception:
                    flash(f"Invalid value for {k}", "error")
        # Ensure row exists then update
        g.cur.execute("INSERT OR IGNORE INTO config(guild_id) VALUES (?)", (guild_id,))
        if updates:
            sets = ", ".join([f"{k} = ?" for k in updates])
            params = list(updates.values()) + [guild_id]
            g.cur.execute(f"UPDATE config SET {sets} WHERE guild_id = ?", params)
            flash("Configuration updated", "success")
        return redirect(url_for("guild_view", guild_id=guild_id))

    @app.post("/guild/<int:guild_id>/categories/add")
    @login_required
    def add_category(guild_id: int):
        require_csrf()
        name = (request.form.get("name") or "").strip()
        placeholder = (request.form.get("placeholder") or "").strip() or None
        if not name:
            flash("Category name is required", "error")
            return redirect(url_for("guild_view", guild_id=guild_id))
        g.cur.execute(
            "INSERT INTO categories(guild_id, name, placeholder, active) VALUES (?,?,?,1)",
            (guild_id, name, placeholder),
        )
        flash("Category added", "success")
        return redirect(url_for("guild_view", guild_id=guild_id))

    @app.post("/guild/<int:guild_id>/categories/<int:category_id>/remove")
    @login_required
    def remove_category(guild_id: int, category_id: int):
        require_csrf()
        g.cur.execute("UPDATE categories SET active = 0 WHERE id = ?", (category_id,))
        flash("Category deactivated", "success")
        return redirect(url_for("guild_view", guild_id=guild_id))

    @app.post("/guild/<int:guild_id>/categories/<int:category_id>/fields/add")
    @login_required
    def add_field(guild_id: int, category_id: int):
        require_csrf()
        name = (request.form.get("name") or "").strip()
        label = (request.form.get("label") or "").strip()
        required = 1 if request.form.get("required") == "on" else 0
        style = request.form.get("style") or "short"
        min_length = request.form.get("min_length") or None
        max_length = request.form.get("max_length") or None
        try:
            if min_length is not None and min_length != "":
                min_length = int(min_length)
            else:
                min_length = None
            if max_length is not None and max_length != "":
                max_length = int(max_length)
            else:
                max_length = None
        except Exception:
            flash("Length fields must be integers", "error")
            return redirect(url_for("guild_view", guild_id=guild_id))
        if not name or not label:
            flash("Field name and label are required", "error")
            return redirect(url_for("guild_view", guild_id=guild_id))
        g.cur.execute(
            (
                "INSERT INTO fields(category_id, name, label, required, style, min_length, max_length, order_index) "
                "VALUES (?,?,?,?,?,?,?,0)"
            ),
            (category_id, name, label, required, style, min_length, max_length),
        )
        flash("Field added", "success")
        return redirect(url_for("guild_view", guild_id=guild_id))

    @app.post("/guild/<int:guild_id>/fields/<int:field_id>/remove")
    @login_required
    def remove_field(guild_id: int, field_id: int):
        require_csrf()
        g.cur.execute("DELETE FROM fields WHERE id = ?", (field_id,))
        flash("Field removed", "success")
        return redirect(url_for("guild_view", guild_id=guild_id))

    @app.get("/guild/<int:guild_id>/tickets")
    @login_required
    def list_tickets(guild_id: int):
        g.cur.execute(
            """
            SELECT id, ticket_number, opener_id, channel_id, status, priority, created_at, closed_at
            FROM tickets WHERE guild_id = ?
            ORDER BY created_at DESC LIMIT 100
            """,
            (guild_id,),
        )
        tickets = g.cur.fetchall()
        return render_template("tickets.html", guild_id=guild_id, tickets=tickets)

    @app.get("/ticket/<int:ticket_id>")
    @login_required
    def ticket_view(ticket_id: int):
        g.cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        t = g.cur.fetchone()
        if not t:
            abort(404)
        g.cur.execute(
            "SELECT * FROM messages WHERE ticket_id = ? ORDER BY created_at ASC",
            (ticket_id,),
        )
        msgs = g.cur.fetchall()
        return render_template("ticket.html", ticket=t, messages=msgs)

    @app.get("/ticket/<int:ticket_id>/export.json")
    @login_required
    def export_ticket_json(ticket_id: int):
        g.cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        t = g.cur.fetchone()
        if not t:
            abort(404)
        g.cur.execute(
            "SELECT * FROM messages WHERE ticket_id = ? ORDER BY created_at ASC",
            (ticket_id,),
        )
        msgs = [dict(m) for m in g.cur.fetchall()]
        resp = {
            "ticket": dict(t),
            "messages": msgs,
            "exported_at": int(time.time()),
        }
        return jsonify(resp)

    return app


if __name__ == "__main__":
    app = create_app()
    bind_host = os.getenv("DASHBOARD_BIND_HOST", "127.0.0.1")
    bind_port = int(os.getenv("DASHBOARD_BIND_PORT", "8080"))
    # By default bind to localhost only to avoid exposing publicly.
    app.run(host=bind_host, port=bind_port)
