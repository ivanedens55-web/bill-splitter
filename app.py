import os
import csv
import io
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, flash, jsonify, Response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-do-not-use-in-production")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access your bills."

# ── DB ────────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # Fail at startup with a clear message instead of a 500 on every page.
    raise RuntimeError(
        "DATABASE_URL is not set. Add your Supabase connection string "
        "(Session pooler URL) as an environment variable."
    )

# Supabase needs SSL. Set DB_SSLMODE=disable only for a local Postgres without SSL.
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require")


@contextmanager
def get_db():
    """Open a connection, commit on success, roll back on error, and ALWAYS close it.

    psycopg2's own `with conn:` commits but does not close the connection,
    which leaks connections until Supabase refuses new ones.
    """
    conn = psycopg2.connect(
        DATABASE_URL,
        sslmode=DB_SSLMODE,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create the tables if they don't exist yet (they were dropped in the SQLite -> Postgres switch)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id       SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bills (
                    id       SERIAL PRIMARY KEY,
                    user_id  INTEGER NOT NULL REFERENCES users(id),
                    name     TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'KSH',
                    created  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id      SERIAL PRIMARY KEY,
                    bill_id INTEGER NOT NULL REFERENCES bills(id),
                    name    TEXT NOT NULL,
                    amount  NUMERIC(12, 2) NOT NULL
                );
            """)


init_db()

# ── Auth helpers ───────────────────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
    if row:
        return User(row["id"], row["username"])
    return None

# ── Bill splitting logic ───────────────────────────────────────────────────────

def calculate(payments):
    if not payments:
        return 0, 0, []
    total = sum(p["amount"] for p in payments)
    share = round(total / len(payments), 2)
    balances  = {p["name"]: round(p["amount"] - share, 2) for p in payments}
    creditors = {k: v  for k, v in balances.items() if v > 0}
    debtors   = {k: -v for k, v in balances.items() if v < 0}
    transactions = []
    while creditors and debtors:
        c, ca = max(creditors.items(), key=lambda x: x[1])
        d, da = max(debtors.items(),   key=lambda x: x[1])
        pay = round(min(ca, da), 2)
        transactions.append({"from": d, "to": c, "amount": pay})
        creditors[c] = round(creditors[c] - pay, 2)
        debtors[d]   = round(debtors[d]   - pay, 2)
        if creditors[c] == 0: del creditors[c]
        if debtors[d]   == 0: del debtors[d]
    return total, share, transactions

# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        if not username or not password:
            flash("Username and password are required.")
            return redirect("/register")
        hashed = generate_password_hash(password)
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO users (username, password) VALUES (%s, %s)",
                        (username, hashed)
                    )
            flash("Account created! Please log in.")
            return redirect("/login")
        except psycopg2.errors.UniqueViolation:
            flash("Username already taken.")
            return redirect("/register")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, username, password FROM users WHERE username=%s",
                    (username,)
                )
                row = cur.fetchone()
        if row and check_password_hash(row["password"], password):
            login_user(User(row["id"], row["username"]))
            return redirect("/")
        flash("Invalid username or password.")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")

# ── Bill routes ────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, currency, created FROM bills WHERE user_id=%s ORDER BY created DESC",
                (current_user.id,)
            )
            bills = cur.fetchall()
    return render_template("index.html", bills=bills)

@app.route("/bill/new", methods=["GET", "POST"])
@login_required
def new_bill():
    if request.method == "POST":
        name     = request.form["name"].strip()
        currency = request.form.get("currency", "KSH")
        if not name:
            flash("Bill name is required.")
            return redirect("/bill/new")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bills (user_id, name, currency) VALUES (%s, %s, %s) RETURNING id",
                    (current_user.id, name, currency)
                )
                bill_id = cur.fetchone()["id"]
        return redirect(f"/bill/{bill_id}")
    return render_template("new_bill.html")

@app.route("/bill/<int:bill_id>")
@login_required
def bill(bill_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, currency FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id)
            )
            b = cur.fetchone()
            if not b:
                flash("Bill not found.")
                return redirect("/")
            cur.execute(
                "SELECT id, name, amount FROM payments WHERE bill_id=%s", (bill_id,)
            )
            payments = cur.fetchall()
    payments_list = [{"id": p["id"], "name": p["name"], "amount": float(p["amount"])} for p in payments]
    total, share, transactions = calculate(payments_list)
    tx_strings = [f"{t['from']} pays {t['to']} {b['currency']} {t['amount']:.2f}" for t in transactions]
    return render_template("bill.html",
        bill={"id": b["id"], "name": b["name"], "currency": b["currency"]},
        payments=payments_list,
        total=total, share=share,
        transactions=tx_strings
    )

@app.route("/bill/<int:bill_id>/add", methods=["POST"])
@login_required
def add_payment(bill_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id)
            )
            b = cur.fetchone()
    if not b:
        flash("Bill not found.")
        return redirect("/")
    name   = request.form["name"].strip()
    amount = request.form["amount"]
    if not name:
        flash("Name is required.")
        return redirect(f"/bill/{bill_id}")
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Amount must be a positive number.")
        return redirect(f"/bill/{bill_id}")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO payments (bill_id, name, amount) VALUES (%s, %s, %s)",
                (bill_id, name, amount)
            )
    flash(f"Payment for {name} added.")
    return redirect(f"/bill/{bill_id}")

@app.route("/bill/<int:bill_id>/edit/<int:payment_id>", methods=["GET", "POST"])
@login_required
def edit_payment(bill_id, payment_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, currency FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id)
            )
            b = cur.fetchone()
            if not b:
                flash("Bill not found.")
                return redirect("/")
            cur.execute(
                "SELECT id, name, amount FROM payments WHERE id=%s AND bill_id=%s",
                (payment_id, bill_id)
            )
            payment = cur.fetchone()
            if not payment:
                flash("Payment not found.")
                return redirect(f"/bill/{bill_id}")
    if request.method == "POST":
        name   = request.form["name"].strip()
        amount = request.form["amount"]
        if not name:
            flash("Name is required.")
            return redirect(f"/bill/{bill_id}/edit/{payment_id}")
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
            flash("Amount must be a positive number.")
            return redirect(f"/bill/{bill_id}/edit/{payment_id}")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE payments SET name=%s, amount=%s WHERE id=%s",
                    (name, amount, payment_id)
                )
        flash("Payment updated.")
        return redirect(f"/bill/{bill_id}")
    return render_template("edit_payment.html",
        bill={"id": bill_id, "currency": b["currency"]},
        payment={"id": payment["id"], "name": payment["name"], "amount": float(payment["amount"])}
    )

@app.route("/bill/<int:bill_id>/delete/<int:payment_id>", methods=["POST"])
@login_required
def delete_payment(bill_id, payment_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id)
            )
            b = cur.fetchone()
    if not b:
        flash("Bill not found.")
        return redirect("/")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM payments WHERE id=%s AND bill_id=%s",
                (payment_id, bill_id)
            )
    flash("Payment removed.")
    return redirect(f"/bill/{bill_id}")

@app.route("/bill/<int:bill_id>/delete", methods=["POST"])
@login_required
def delete_bill(bill_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id)
            )
            b = cur.fetchone()
    if not b:
        flash("Bill not found.")
        return redirect("/")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM payments WHERE bill_id=%s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE id=%s", (bill_id,))
    flash("Bill deleted.")
    return redirect("/")

# ── Export ────────────────────────────────────────────────────────────────────

@app.route("/bill/<int:bill_id>/export")
@login_required
def export_csv(bill_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, currency FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id)
            )
            b = cur.fetchone()
            if not b:
                flash("Bill not found.")
                return redirect("/")
            cur.execute(
                "SELECT name, amount FROM payments WHERE bill_id=%s", (bill_id,)
            )
            rows = cur.fetchall()
    payments = [{"name": r["name"], "amount": float(r["amount"])} for r in rows]
    total, share, transactions = calculate(payments)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", f"Amount ({b['currency']})"])
    for p in payments:
        writer.writerow([p["name"], f"{p['amount']:.2f}"])
    writer.writerow([])
    writer.writerow(["Total", f"{total:.2f}"])
    writer.writerow(["Each pays", f"{share:.2f}"])
    writer.writerow([])
    writer.writerow(["Settlements"])
    for t in transactions:
        writer.writerow([f"{t['from']} pays {t['to']}", f"{t['amount']:.2f}"])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={b['name'].replace(' ','_')}.csv"}
    )

# ── JSON API ───────────────────────────────────────────────────────────────────

@app.route("/api/bill/<int:bill_id>")
@login_required
def api_bill(bill_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, currency FROM bills WHERE id=%s AND user_id=%s",
                (bill_id, current_user.id)
            )
            b = cur.fetchone()
            if not b:
                return jsonify({"error": "Not found"}), 404
            cur.execute(
                "SELECT id, name, amount FROM payments WHERE bill_id=%s", (bill_id,)
            )
            rows = cur.fetchall()
    payments = [{"id": r["id"], "name": r["name"], "amount": float(r["amount"])} for r in rows]
    total, share, transactions = calculate(payments)
    return jsonify({
        "bill":         {"id": b["id"], "name": b["name"], "currency": b["currency"]},
        "payments":     payments,
        "total":        total,
        "share":        share,
        "transactions": transactions
    })

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")