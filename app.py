from datetime import datetime, timedelta, timezone
import calendar
import json
import os
import uuid

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, matches DB storage

# Optional Google Calendar dependencies
try:
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials as GCredentials
    from google.auth.transport.requests import Request as GRequest
    from googleapiclient.discovery import build as gcal_build
    GCAL_AVAILABLE = True
except ImportError:
    GCAL_AVAILABLE = False
import sqlite3
from pathlib import Path
import requests as http_requests

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, g, flash, jsonify, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "dg_workspace.db"
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'csv',
    'png', 'jpg', 'jpeg', 'gif', 'webp',
    'zip',
}

app = Flask(__name__)
app.secret_key = "CHANGE_THIS_TO_A_STRONG_SECRET_KEY"
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB max upload

# API token for eigent/Claude skill access (change this to a strong random string)
API_TOKEN = "dg-skill-token-CHANGE-ME"

# ---------------------------
# Notification config  (fill in when ready)
# ---------------------------

# --- Email via Brevo ---
BREVO_API_KEY      = ""                    # TODO: paste Brevo API key
BREVO_SENDER_EMAIL = "support@dggalvanizing.com"

# --- WhatsApp via Meta Cloud API (direct) ---
META_WA_TOKEN       = ""   # TODO: Meta permanent access token
META_WA_PHONE_ID    = ""   # TODO: WhatsApp Business phone number ID
META_WA_TEMPLATE    = ""   # TODO: approved message template name (or leave blank for free-form)

# --- SMS via MSG91 ---
MSG91_AUTH_KEY      = ""   # TODO: MSG91 auth key
MSG91_SENDER_ID     = ""   # TODO: 6-char sender ID (e.g. DGWRKS)
MSG91_TEMPLATE_ID   = ""   # TODO: MSG91 DLT template ID

# --- Google Calendar ---
GOOGLE_CLIENT_SECRETS_FILE = str(BASE_DIR / "google_credentials.json")
GCAL_TOKEN_FILE            = str(BASE_DIR / "gcal_token.json")
GCAL_CALENDAR_ID           = "primary"
GCAL_SCOPES                = ["https://www.googleapis.com/auth/calendar.events"]

# Employee colour palette (assigned by emp_id modulo length)
_EMP_COLORS = [
    "#4F46E5", "#0EA5E9", "#10B981", "#F59E0B", "#EF4444",
    "#8B5CF6", "#EC4899", "#14B8A6", "#F97316", "#6366F1",
]

def _emp_color(emp_id):
    return _EMP_COLORS[(int(emp_id) - 1) % len(_EMP_COLORS)]


def _next_occurrence(due_at, recurrence):
    """Return the next datetime for a recurring task."""
    if recurrence == "daily":
        return due_at + timedelta(days=1)
    if recurrence == "weekly":
        return due_at + timedelta(weeks=1)
    if recurrence == "monthly":
        month = due_at.month + 1
        year = due_at.year
        if month > 12:
            month = 1
            year += 1
        day = min(due_at.day, calendar.monthrange(year, month)[1])
        return due_at.replace(year=year, month=month, day=day)
    return None


# ---------------------------
# Database helpers
# ---------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mobile TEXT NOT NULL UNIQUE,
            email TEXT,
            role TEXT NOT NULL DEFAULT 'employee', -- employee, manager, hr, admin
            password_hash TEXT NOT NULL,
            manager_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            priority TEXT DEFAULT 'normal', -- low, normal, high
            status TEXT NOT NULL DEFAULT 'open', -- open, in_progress, done, cancelled
            due_at DATETIME NOT NULL,
            created_by INTEGER NOT NULL,
            assigned_to INTEGER NOT NULL,
            reminder_profile TEXT DEFAULT 'standard_dg',
            recurrence TEXT,                -- NULL = one-time, 'daily', 'weekly', 'monthly'
            recurrence_parent_id INTEGER,   -- NULL for root, predecessor task id for spawned instances
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES employees(id),
            FOREIGN KEY(assigned_to) REFERENCES employees(id)
        );

        -- people kept in loop
        CREATE TABLE IF NOT EXISTS task_watchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        );

        -- reminder log (what was sent, to whom, via what)
        CREATE TABLE IF NOT EXISTS task_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            channel TEXT NOT NULL, -- email, sms, whatsapp
            reminder_type TEXT NOT NULL, -- t_minus_10m, custom, overdue, etc.
            sent_at DATETIME NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        );

        -- scheduled reminder offsets per task (when to fire, relative to due_at)
        CREATE TABLE IF NOT EXISTS task_reminder_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            offset_minutes INTEGER NOT NULL,  -- minutes before due_at, e.g. 10, 60, 1440
            label TEXT,                       -- human label e.g. "1 hour before"
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );

        -- file/image attachments per task
        CREATE TABLE IF NOT EXISTS task_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            filename TEXT NOT NULL,       -- stored filename on disk (uuid-prefixed)
            original_name TEXT NOT NULL,  -- original upload filename
            mimetype TEXT,
            size INTEGER,                 -- bytes
            uploaded_by INTEGER NOT NULL,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(uploaded_by) REFERENCES employees(id)
        );

        -- office / site locations
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            city TEXT,
            weekly_off TEXT DEFAULT 'Sunday',  -- day name that is weekly off
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- leave types (annual, sick, casual …)
        CREATE TABLE IF NOT EXISTS leave_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            max_days_per_year INTEGER NOT NULL DEFAULT 0
        );

        -- leave requests
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            leave_type_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,   -- YYYY-MM-DD
            end_date TEXT NOT NULL,     -- YYYY-MM-DD
            days REAL NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, rejected
            approved_by INTEGER,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            decided_at DATETIME,
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(leave_type_id) REFERENCES leave_types(id),
            FOREIGN KEY(approved_by) REFERENCES employees(id)
        );

        -- leave balances per employee per year
        CREATE TABLE IF NOT EXISTS leave_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            leave_type_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            total_days INTEGER NOT NULL DEFAULT 0,
            used_days REAL NOT NULL DEFAULT 0,
            UNIQUE(employee_id, leave_type_id, year),
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(leave_type_id) REFERENCES leave_types(id)
        );

        -- attendance
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            date TEXT NOT NULL,          -- YYYY-MM-DD
            status TEXT NOT NULL DEFAULT 'present',  -- present, absent, half_day, leave
            check_in TEXT,               -- HH:MM
            check_out TEXT,              -- HH:MM
            notes TEXT,
            marked_by INTEGER,           -- employee_id that recorded this
            location_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id, date),
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(marked_by) REFERENCES employees(id),
            FOREIGN KEY(location_id) REFERENCES locations(id)
        );

        -- salary structure per employee (effective from a date)
        CREATE TABLE IF NOT EXISTS salary_structures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL UNIQUE,
            basic INTEGER NOT NULL DEFAULT 0,
            hra INTEGER NOT NULL DEFAULT 0,
            medical_allowance INTEGER NOT NULL DEFAULT 0,
            travel_allowance INTEGER NOT NULL DEFAULT 0,
            other_allowance INTEGER NOT NULL DEFAULT 0,
            pf_rate REAL NOT NULL DEFAULT 12.0,    -- % of basic
            esi_rate REAL NOT NULL DEFAULT 0.75,   -- % of gross
            tds INTEGER NOT NULL DEFAULT 0,        -- fixed monthly TDS
            effective_from TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        );

        -- generated salary slips
        CREATE TABLE IF NOT EXISTS salary_slips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            month INTEGER NOT NULL,   -- 1-12
            year INTEGER NOT NULL,
            working_days INTEGER NOT NULL DEFAULT 0,
            present_days REAL NOT NULL DEFAULT 0,
            basic INTEGER NOT NULL DEFAULT 0,
            hra INTEGER NOT NULL DEFAULT 0,
            medical_allowance INTEGER NOT NULL DEFAULT 0,
            travel_allowance INTEGER NOT NULL DEFAULT 0,
            other_allowance INTEGER NOT NULL DEFAULT 0,
            gross INTEGER NOT NULL DEFAULT 0,
            pf_employee INTEGER NOT NULL DEFAULT 0,
            pf_employer INTEGER NOT NULL DEFAULT 0,
            esi_employee INTEGER NOT NULL DEFAULT 0,
            esi_employer INTEGER NOT NULL DEFAULT 0,
            tds INTEGER NOT NULL DEFAULT 0,
            other_deductions INTEGER NOT NULL DEFAULT 0,
            net_pay INTEGER NOT NULL DEFAULT 0,
            generated_by INTEGER NOT NULL,
            generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id, month, year),
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(generated_by) REFERENCES employees(id)
        );
        """
    )
    db.commit()


def _migrate_db():
    """Add new tables/columns to existing DB without wiping data."""
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS task_reminder_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            offset_minutes INTEGER NOT NULL,
            label TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS gcal_sync (
            task_id INTEGER PRIMARY KEY,
            gcal_event_id TEXT NOT NULL,
            synced_at DATETIME NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )
        """
    )
    # Add columns if missing (for DBs created before these versions)
    cols = [row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()]
    if "original_due_at" not in cols:
        db.execute("ALTER TABLE tasks ADD COLUMN original_due_at DATETIME")
        db.execute("UPDATE tasks SET original_due_at = due_at WHERE original_due_at IS NULL")
    if "recurrence" not in cols:
        db.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT")
    if "recurrence_parent_id" not in cols:
        db.execute("ALTER TABLE tasks ADD COLUMN recurrence_parent_id INTEGER")
    # Add task_attachments table if missing
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS task_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            mimetype TEXT,
            size INTEGER,
            uploaded_by INTEGER NOT NULL,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(uploaded_by) REFERENCES employees(id)
        )
        """
    )
    # New module tables
    for sql in [
        """CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            city TEXT,
            weekly_off TEXT DEFAULT 'Sunday',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS leave_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            max_days_per_year INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            leave_type_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            days REAL NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            approved_by INTEGER,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            decided_at DATETIME,
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(leave_type_id) REFERENCES leave_types(id),
            FOREIGN KEY(approved_by) REFERENCES employees(id)
        )""",
        """CREATE TABLE IF NOT EXISTS leave_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            leave_type_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            total_days INTEGER NOT NULL DEFAULT 0,
            used_days REAL NOT NULL DEFAULT 0,
            UNIQUE(employee_id, leave_type_id, year),
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(leave_type_id) REFERENCES leave_types(id)
        )""",
        """CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'present',
            check_in TEXT,
            check_out TEXT,
            notes TEXT,
            marked_by INTEGER,
            location_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id, date),
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(marked_by) REFERENCES employees(id),
            FOREIGN KEY(location_id) REFERENCES locations(id)
        )""",
        """CREATE TABLE IF NOT EXISTS salary_structures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL UNIQUE,
            basic INTEGER NOT NULL DEFAULT 0,
            hra INTEGER NOT NULL DEFAULT 0,
            medical_allowance INTEGER NOT NULL DEFAULT 0,
            travel_allowance INTEGER NOT NULL DEFAULT 0,
            other_allowance INTEGER NOT NULL DEFAULT 0,
            pf_rate REAL NOT NULL DEFAULT 12.0,
            esi_rate REAL NOT NULL DEFAULT 0.75,
            tds INTEGER NOT NULL DEFAULT 0,
            effective_from TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )""",
        """CREATE TABLE IF NOT EXISTS salary_slips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            working_days INTEGER NOT NULL DEFAULT 0,
            present_days REAL NOT NULL DEFAULT 0,
            basic INTEGER NOT NULL DEFAULT 0,
            hra INTEGER NOT NULL DEFAULT 0,
            medical_allowance INTEGER NOT NULL DEFAULT 0,
            travel_allowance INTEGER NOT NULL DEFAULT 0,
            other_allowance INTEGER NOT NULL DEFAULT 0,
            gross INTEGER NOT NULL DEFAULT 0,
            pf_employee INTEGER NOT NULL DEFAULT 0,
            pf_employer INTEGER NOT NULL DEFAULT 0,
            esi_employee INTEGER NOT NULL DEFAULT 0,
            esi_employer INTEGER NOT NULL DEFAULT 0,
            tds INTEGER NOT NULL DEFAULT 0,
            other_deductions INTEGER NOT NULL DEFAULT 0,
            net_pay INTEGER NOT NULL DEFAULT 0,
            generated_by INTEGER NOT NULL,
            generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id, month, year),
            FOREIGN KEY(employee_id) REFERENCES employees(id),
            FOREIGN KEY(generated_by) REFERENCES employees(id)
        )""",
    ]:
        db.execute(sql)
    # Add new employee columns if missing
    emp_cols = [r[1] for r in db.execute("PRAGMA table_info(employees)").fetchall()]
    for col, defn in [
        ("department", "TEXT"),
        ("designation", "TEXT"),
        ("date_of_joining", "TEXT"),
        ("location_id", "INTEGER"),
        ("date_of_birth", "TEXT"),
        ("uan_number", "TEXT"),
        ("pan_number", "TEXT"),
    ]:
        if col not in emp_cols:
            db.execute(f"ALTER TABLE employees ADD COLUMN {col} {defn}")
    # Seed default leave types if table empty
    if not db.execute("SELECT 1 FROM leave_types").fetchone():
        db.executemany(
            "INSERT INTO leave_types (name, max_days_per_year) VALUES (?, ?)",
            [("Annual Leave", 12), ("Sick Leave", 7), ("Casual Leave", 7)],
        )
    db.commit()


# ---------------------------
# Authentication helpers
# ---------------------------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    row = db.execute(
        "SELECT * FROM employees WHERE id = ?", (uid,)
    ).fetchone()
    return row

def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------
# Initial admin creation (one-time)
# ---------------------------

@app.cli.command("create-admin")
def create_admin():
    """flask --app app create-admin"""
    init_db()                 # ensure tables exist
    db = get_db()
    mobile = input("Admin mobile (username): ").strip()
    name = input("Admin name: ").strip()
    email = input("Admin email: ").strip()
    password = input("Admin password: ").strip()

    password_hash = generate_password_hash(password)
    try:
        db.execute(
            "INSERT INTO employees (name, mobile, email, role, password_hash) "
            "VALUES (?, ?, ?, 'admin', ?)",
            (name, mobile, email, password_hash),
        )
        db.commit()
        print("Admin created.")
    except sqlite3.IntegrityError as e:
        print("Error creating admin:", e)


# ---------------------------
# Routes: auth
# ---------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute(
            "SELECT * FROM employees WHERE mobile = ?", (mobile,)
        ).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid mobile or password.", "error")
            return render_template("login.html")

        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------
# Routes: main pages
# ---------------------------

@app.route("/")
@login_required
def dashboard():
    user = current_user()
    db = get_db()

    # tasks visible to user: created_by, assigned_to, or watcher
    tasks = db.execute(
        """
        SELECT t.*, e.name AS assignee_name, c.name AS creator_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_to
        JOIN employees c ON c.id = t.created_by
        WHERE (
            t.assigned_to = ?
            OR t.created_by = ?
            OR t.id IN (
                SELECT task_id FROM task_watchers WHERE employee_id = ?
            )
        )
        AND (
            t.recurrence IS NULL
            OR t.status NOT IN ('done', 'cancelled')
        )
        ORDER BY t.due_at ASC
        """,
        (user["id"], user["id"], user["id"]),
    ).fetchall()

    employees = db.execute(
        "SELECT id, name, mobile FROM employees ORDER BY name ASC"
    ).fetchall()

    return render_template(
        "dashboard.html",
        user=user,
        tasks=tasks,
        employees=employees,
        now=_utcnow().strftime("%Y-%m-%d %H:%M"),
    )


@app.route("/tasks")
@login_required
def tasks_list():
    user = current_user()
    db = get_db()

    tasks = db.execute(
        """
        SELECT t.*, e.name AS assignee_name, c.name AS creator_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_to
        JOIN employees c ON c.id = t.created_by
        WHERE (
            t.assigned_to = ?
            OR t.created_by = ?
            OR t.id IN (
                SELECT task_id FROM task_watchers WHERE employee_id = ?
            )
        )
        AND (
            t.recurrence IS NULL
            OR t.status NOT IN ('done', 'cancelled')
        )
        ORDER BY t.due_at ASC
        """,
        (user["id"], user["id"], user["id"]),
    ).fetchall()

    employees = db.execute(
        "SELECT id, name, mobile FROM employees ORDER BY name ASC"
    ).fetchall()

    return render_template(
        "tasks.html",
        user=user,
        tasks=tasks,
        employees=employees,
        now=_utcnow().strftime("%Y-%m-%d %H:%M"),
    )


@app.route("/tasks/create", methods=["POST"])
@login_required
def create_task():
    user = current_user()
    db = get_db()

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    priority = request.form.get("priority", "normal")
    assigned_to = request.form.get("assigned_to")
    due_date = request.form.get("due_date")   # yyyy-mm-dd
    due_time = request.form.get("due_time")   # HH:MM
    watchers = request.form.getlist("watchers")  # list of employee IDs
    recurrence = request.form.get("recurrence") or None  # None, 'daily', 'weekly', 'monthly'
    reminder_presets = request.form.getlist("reminder_presets")

    if not title or not assigned_to or not due_date or not due_time:
        flash("Title, assignee, due date and time are required.", "error")
        return redirect(url_for("dashboard"))

    due_at_str = f"{due_date} {due_time}:00"
    try:
        due_at = datetime.strptime(due_at_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        flash("Invalid due date/time.", "error")
        return redirect(url_for("dashboard"))

    cur = db.execute(
        """
        INSERT INTO tasks (title, description, priority, due_at, original_due_at, created_by, assigned_to, recurrence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, priority, due_at.isoformat(sep=" "), due_at.isoformat(sep=" "), user["id"], int(assigned_to), recurrence),
    )
    task_id = cur.lastrowid

    # watchers
    for wid in watchers:
        if wid and int(wid) != int(assigned_to) and int(wid) != user["id"]:
            db.execute(
                "INSERT INTO task_watchers (task_id, employee_id) VALUES (?, ?)",
                (task_id, int(wid)),
            )

    db.commit()

    # reminder schedules from creation form
    PRESETS = {
        "10m": (10,   "10 minutes before"),
        "30m": (30,   "30 minutes before"),
        "1h":  (60,   "1 hour before"),
        "3h":  (180,  "3 hours before"),
        "1d":  (1440, "1 day before"),
        "2d":  (2880, "2 days before"),
    }
    for preset in reminder_presets:
        if preset in PRESETS:
            offset, label = PRESETS[preset]
            db.execute(
                "INSERT INTO task_reminder_schedules (task_id, offset_minutes, label) VALUES (?, ?, ?)",
                (task_id, offset, label),
            )
    if reminder_presets:
        db.commit()

    flash("Task created and delegated.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    user = current_user()
    db = get_db()

    task = db.execute(
        """
        SELECT t.*, e.name AS assignee_name, c.name AS creator_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_to
        JOIN employees c ON c.id = t.created_by
        WHERE t.id = ?
        """,
        (task_id,),
    ).fetchone()
    if not task:
        return "Not found", 404

    # Access check: admin/manager sees all; others only if creator, assignee, or watcher
    if user["role"] not in ("admin", "manager"):
        is_watcher = db.execute(
            "SELECT 1 FROM task_watchers WHERE task_id=? AND employee_id=?",
            (task_id, user["id"]),
        ).fetchone()
        if task["created_by"] != user["id"] and task["assigned_to"] != user["id"] and not is_watcher:
            flash("You don't have access to that task.", "error")
            return redirect(url_for("dashboard"))

    schedules = db.execute(
        "SELECT * FROM task_reminder_schedules WHERE task_id = ? ORDER BY offset_minutes DESC",
        (task_id,),
    ).fetchall()

    sent_log = db.execute(
        """
        SELECT tr.*, e.name AS emp_name
        FROM task_reminders tr
        JOIN employees e ON e.id = tr.employee_id
        WHERE tr.task_id = ?
        ORDER BY tr.sent_at DESC
        LIMIT 30
        """,
        (task_id,),
    ).fetchall()

    watchers = db.execute(
        """
        SELECT e.id, e.name FROM task_watchers tw
        JOIN employees e ON e.id = tw.employee_id
        WHERE tw.task_id = ?
        """,
        (task_id,),
    ).fetchall()

    employees = db.execute("SELECT id, name FROM employees ORDER BY name ASC").fetchall()
    watcher_ids = {w["id"] for w in watchers}

    attachments = db.execute(
        """
        SELECT ta.*, e.name AS uploader_name
        FROM task_attachments ta
        JOIN employees e ON e.id = ta.uploaded_by
        WHERE ta.task_id = ?
        ORDER BY ta.uploaded_at DESC
        """,
        (task_id,),
    ).fetchall()

    return render_template(
        "task.html",
        user=user,
        task=task,
        schedules=schedules,
        sent_log=sent_log,
        watchers=watchers,
        employees=employees,
        watcher_ids=watcher_ids,
        attachments=attachments,
    )


@app.route("/tasks/<int:task_id>/reminders/add", methods=["POST"])
@login_required
def add_reminder_schedule(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Not found", 404

    preset = request.form.get("preset", "")
    custom_min = request.form.get("custom_minutes", "").strip()

    PRESETS = {
        "10m":  (10,    "10 minutes before"),
        "30m":  (30,    "30 minutes before"),
        "1h":   (60,    "1 hour before"),
        "3h":   (180,   "3 hours before"),
        "1d":   (1440,  "1 day before"),
        "2d":   (2880,  "2 days before"),
    }

    if preset in PRESETS:
        offset, label = PRESETS[preset]
    elif custom_min:
        try:
            offset = int(custom_min)
            if offset <= 0:
                raise ValueError
            label = f"{offset} minutes before"
        except ValueError:
            flash("Enter a valid number of minutes.", "error")
            return redirect(url_for("task_detail", task_id=task_id))
    else:
        flash("Select a preset or enter custom minutes.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    # avoid duplicates
    exists = db.execute(
        "SELECT 1 FROM task_reminder_schedules WHERE task_id=? AND offset_minutes=?",
        (task_id, offset),
    ).fetchone()
    if exists:
        flash("That reminder is already scheduled.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    db.execute(
        "INSERT INTO task_reminder_schedules (task_id, offset_minutes, label) VALUES (?, ?, ?)",
        (task_id, offset, label),
    )
    db.commit()
    flash(f"Reminder added: {label}.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/<int:task_id>/reminders/<int:rid>/delete", methods=["POST"])
@login_required
def delete_reminder_schedule(task_id, rid):
    db = get_db()
    db.execute(
        "DELETE FROM task_reminder_schedules WHERE id = ? AND task_id = ?",
        (rid, task_id),
    )
    db.commit()
    flash("Reminder removed.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/<int:task_id>/send-now", methods=["POST"])
@login_required
def send_reminder_now(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Not found", 404

    msg = (
        f"Reminder: Task '{task['title']}' is due at {task['due_at']}. "
        "Please take action."
    )
    now = _utcnow()
    _notify_all_participants(db, task, msg, now, reminder_type="manual")
    db.commit()
    flash("Reminder sent to assignee and all CC contacts.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/<int:task_id>/update-status", methods=["POST"])
@login_required
def update_task_status(task_id):
    user = current_user()
    new_status = request.form.get("status", "open")

    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Not found", 404

    # Only creator or assignee can change status
    if task["created_by"] != user["id"] and task["assigned_to"] != user["id"]:
        return "Forbidden", 403

    db.execute(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (new_status, task_id),
    )
    db.commit()

    # Auto-spawn next instance when a recurring task is completed
    if new_status == "done" and task["recurrence"]:
        due_str = task["due_at"].split(".")[0]  # strip microseconds if any
        due_at = datetime.strptime(due_str, "%Y-%m-%d %H:%M:%S")
        next_due = _next_occurrence(due_at, task["recurrence"])
        if next_due:
            cur2 = db.execute(
                """
                INSERT INTO tasks
                    (title, description, priority, due_at, original_due_at,
                     created_by, assigned_to, recurrence, recurrence_parent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task["title"], task["description"], task["priority"],
                    next_due.isoformat(sep=" "), next_due.isoformat(sep=" "),
                    task["created_by"], task["assigned_to"],
                    task["recurrence"], task_id,
                ),
            )
            new_task_id = cur2.lastrowid
            # copy watchers
            for w in db.execute(
                "SELECT employee_id FROM task_watchers WHERE task_id = ?", (task_id,)
            ).fetchall():
                db.execute(
                    "INSERT INTO task_watchers (task_id, employee_id) VALUES (?, ?)",
                    (new_task_id, w["employee_id"]),
                )
            # copy reminder schedules
            for s in db.execute(
                "SELECT offset_minutes, label FROM task_reminder_schedules WHERE task_id = ?",
                (task_id,),
            ).fetchall():
                db.execute(
                    "INSERT INTO task_reminder_schedules (task_id, offset_minutes, label) VALUES (?, ?, ?)",
                    (new_task_id, s["offset_minutes"], s["label"]),
                )
            db.commit()
            cadence = task["recurrence"].capitalize()
            flash(
                f"Marked done. Next {cadence} instance created "
                f"due {next_due.strftime('%d %b %Y %H:%M')}.",
                "success",
            )
    else:
        flash("Task status updated.", "success")

    referrer = request.referrer or ""
    if f"/tasks/{task_id}" in referrer:
        return redirect(url_for("task_detail", task_id=task_id))
    return redirect(url_for("tasks_list"))


@app.route("/tasks/<int:task_id>/edit", methods=["POST"])
@login_required
def edit_task(task_id):
    user = current_user()
    db = get_db()

    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Not found", 404
    if task["created_by"] != user["id"]:
        flash("Only the task creator can edit it.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    title       = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    priority    = request.form.get("priority", "normal")
    assigned_to = request.form.get("assigned_to")
    due_date    = request.form.get("due_date")
    due_time    = request.form.get("due_time")
    watchers    = request.form.getlist("watchers")

    if not title or not assigned_to or not due_date or not due_time:
        flash("Title, assignee, due date and time are required.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    due_at_str = f"{due_date} {due_time}:00"
    try:
        due_at = datetime.strptime(due_at_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        flash("Invalid due date/time.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    db.execute(
        "UPDATE tasks SET title=?, description=?, priority=?, assigned_to=?, due_at=? WHERE id=?",
        (title, description, priority, int(assigned_to), due_at.isoformat(sep=" "), task_id),
        # NOTE: original_due_at is intentionally never updated here
    )

    # replace watchers
    db.execute("DELETE FROM task_watchers WHERE task_id = ?", (task_id,))
    for wid in watchers:
        if wid and int(wid) != int(assigned_to) and int(wid) != user["id"]:
            db.execute(
                "INSERT INTO task_watchers (task_id, employee_id) VALUES (?, ?)",
                (task_id, int(wid)),
            )

    db.commit()
    flash("Task updated.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_task(task_id):
    user = current_user()
    if user["role"] != "admin":
        flash("Only an admin can delete tasks.", "error")
        return redirect(url_for("task_detail", task_id=task_id))
    db = get_db()
    db.execute("DELETE FROM task_reminder_schedules WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM task_watchers WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM task_reminders WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM gcal_sync WHERE task_id = ?", (task_id,))
    # delete attachment files from disk
    att_rows = db.execute(
        "SELECT filename FROM task_attachments WHERE task_id = ?", (task_id,)
    ).fetchall()
    for row in att_rows:
        try:
            (UPLOAD_FOLDER / row["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    db.execute("DELETE FROM task_attachments WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    flash("Task deleted.", "success")
    return redirect(url_for("dashboard"))


# ---------------------------
# File attachment helpers & routes
# ---------------------------

def _allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


@app.route("/tasks/<int:task_id>/attachments", methods=["POST"])
@login_required
def upload_attachment(task_id):
    user = current_user()
    db = get_db()

    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Not found", 404
    # same access check as task_detail
    if user["role"] not in ("admin", "manager"):
        is_watcher = db.execute(
            "SELECT 1 FROM task_watchers WHERE task_id=? AND employee_id=?",
            (task_id, user["id"]),
        ).fetchone()
        if task["created_by"] != user["id"] and task["assigned_to"] != user["id"] and not is_watcher:
            return "Forbidden", 403

    if "file" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    file = request.files["file"]
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    original_name = secure_filename(file.filename)
    if not _allowed_file(original_name):
        flash("File type not allowed. Allowed: images, PDF, Word, Excel, CSV, TXT, ZIP.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    # store as <uuid>_<secure_filename> to avoid collisions/path traversal
    stored_name = f"{uuid.uuid4().hex}_{original_name}"
    dest = UPLOAD_FOLDER / stored_name
    file.save(str(dest))
    size = dest.stat().st_size
    mimetype = file.content_type or "application/octet-stream"

    db.execute(
        """
        INSERT INTO task_attachments (task_id, filename, original_name, mimetype, size, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, stored_name, original_name, mimetype, size, user["id"]),
    )
    db.commit()
    flash(f"'{original_name}' uploaded.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/attachments/<path:filename>")
@login_required
def serve_attachment(filename):
    """Serve an uploaded file after verifying caller has access to its task."""
    user = current_user()
    db = get_db()

    att = db.execute(
        "SELECT * FROM task_attachments WHERE filename = ?", (filename,)
    ).fetchone()
    if not att:
        return "Not found", 404

    task_id = att["task_id"]
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Not found", 404

    if user["role"] not in ("admin", "manager"):
        is_watcher = db.execute(
            "SELECT 1 FROM task_watchers WHERE task_id=? AND employee_id=?",
            (task_id, user["id"]),
        ).fetchone()
        if task["created_by"] != user["id"] and task["assigned_to"] != user["id"] and not is_watcher:
            return "Forbidden", 403

    return send_from_directory(
        str(UPLOAD_FOLDER), filename, as_attachment=False,
        download_name=att["original_name"]
    )


@app.route("/attachments/<int:att_id>/delete", methods=["POST"])
@login_required
def delete_attachment(att_id):
    user = current_user()
    db = get_db()

    att = db.execute("SELECT * FROM task_attachments WHERE id = ?", (att_id,)).fetchone()
    if not att:
        return "Not found", 404

    task_id = att["task_id"]
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Not found", 404

    # only uploader, task creator, or admin can delete
    if user["id"] != att["uploaded_by"] and user["id"] != task["created_by"] and user["role"] != "admin":
        flash("You cannot delete this attachment.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    try:
        (UPLOAD_FOLDER / att["filename"]).unlink(missing_ok=True)
    except Exception:
        pass
    db.execute("DELETE FROM task_attachments WHERE id = ?", (att_id,))
    db.commit()
    flash("Attachment deleted.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


# ---------------------------
# Employee management
# ---------------------------

@app.route("/employees")
@login_required
def employees_list():
    user = current_user()
    if user["role"] not in ("admin", "manager"):
        flash("Access denied.", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    emps = db.execute(
        "SELECT * FROM employees ORDER BY name ASC"
    ).fetchall()
    locations = db.execute(
        "SELECT id, name FROM locations WHERE is_active=1 ORDER BY name"
    ).fetchall()
    today_mmdd = datetime.now().strftime("%m-%d")
    return render_template("employees.html", user=user, employees=emps,
                           locations=locations, today_mmdd=today_mmdd)


@app.route("/employees/add", methods=["POST"])
@login_required
def add_employee():
    user = current_user()
    if user["role"] not in ("admin", "manager"):
        return "Forbidden", 403

    name            = request.form.get("name", "").strip()
    mobile          = request.form.get("mobile", "").strip()
    email           = request.form.get("email", "").strip()
    role            = request.form.get("role", "employee")
    password        = request.form.get("password", "").strip()
    designation     = request.form.get("designation", "").strip() or None
    department      = request.form.get("department", "").strip() or None
    date_of_joining = request.form.get("date_of_joining", "").strip() or None
    location_id     = request.form.get("location_id", type=int) or None
    date_of_birth   = request.form.get("date_of_birth", "").strip() or None
    uan_number      = request.form.get("uan_number", "").strip() or None
    pan_number      = request.form.get("pan_number", "").strip().upper() or None

    if not name or not mobile or not password:
        flash("Name, mobile and password are required.", "error")
        return redirect(url_for("employees_list"))

    db = get_db()
    try:
        db.execute(
            """INSERT INTO employees
               (name, mobile, email, role, password_hash,
                designation, department, date_of_joining, location_id,
                date_of_birth, uan_number, pan_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, mobile, email or None, role, generate_password_hash(password),
             designation, department, date_of_joining, location_id,
             date_of_birth, uan_number, pan_number),
        )
        db.commit()
        flash(f"Employee '{name}' added.", "success")
    except sqlite3.IntegrityError:
        flash("Mobile number already registered.", "error")
    return redirect(url_for("employees_list"))


@app.route("/employees/<int:emp_id>/edit", methods=["POST"])
@login_required
def edit_employee(emp_id):
    user = current_user()
    if user["role"] != "admin":
        return "Forbidden", 403

    name            = request.form.get("name", "").strip()
    mobile          = request.form.get("mobile", "").strip()
    email           = request.form.get("email", "").strip()
    role            = request.form.get("role", "employee")
    designation     = request.form.get("designation", "").strip() or None
    department      = request.form.get("department", "").strip() or None
    date_of_joining = request.form.get("date_of_joining", "").strip() or None
    location_id     = request.form.get("location_id", type=int) or None
    date_of_birth   = request.form.get("date_of_birth", "").strip() or None
    uan_number      = request.form.get("uan_number", "").strip() or None
    pan_number      = request.form.get("pan_number", "").strip().upper() or None
    new_password    = request.form.get("new_password", "").strip()

    if not name or not mobile:
        flash("Name and mobile are required.", "error")
        return redirect(url_for("employees_list"))

    db = get_db()
    try:
        db.execute(
            """UPDATE employees SET
               name=?, mobile=?, email=?, role=?,
               designation=?, department=?, date_of_joining=?, location_id=?,
               date_of_birth=?, uan_number=?, pan_number=?
               WHERE id=?""",
            (name, mobile, email or None, role,
             designation, department, date_of_joining, location_id,
             date_of_birth, uan_number, pan_number, emp_id),
        )
        if new_password:
            db.execute(
                "UPDATE employees SET password_hash=? WHERE id=?",
                (generate_password_hash(new_password), emp_id),
            )
        db.commit()
        flash(f"Employee '{name}' updated.", "success")
    except sqlite3.IntegrityError:
        flash("Mobile number already in use by another employee.", "error")
    return redirect(url_for("employees_list"))


@app.route("/employees/<int:emp_id>/delete", methods=["POST"])
@login_required
def delete_employee(emp_id):
    user = current_user()
    if user["role"] != "admin":
        return "Forbidden", 403
    if emp_id == user["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("employees_list"))
    db = get_db()
    db.execute("DELETE FROM employees WHERE id = ?", (emp_id,))
    db.commit()
    flash("Employee removed.", "success")
    return redirect(url_for("employees_list"))


# ---------------------------
# Reminder logic
# ---------------------------

def _notify_all_participants(db, task, msg, now, reminder_type="auto"):
    """Send WhatsApp + SMS + Email to assignee and all watchers, log each."""
    participants = [task["assigned_to"]]
    watchers = db.execute(
        "SELECT employee_id FROM task_watchers WHERE task_id = ?", (task["id"],)
    ).fetchall()
    participants += [w["employee_id"] for w in watchers]

    for emp_id in participants:
        emp = db.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
        if not emp:
            continue
        _send_whatsapp(emp["mobile"], msg)
        _send_sms(emp["mobile"], msg)
        _send_email(emp["email"], emp["name"], f"Task reminder: {task['title']}", msg)
        db.execute(
            "INSERT INTO task_reminders (task_id, employee_id, channel, reminder_type, sent_at) "
            "VALUES (?, ?, 'whatsapp+sms+email', ?, ?)",
            (task["id"], emp_id, reminder_type, now.isoformat(sep=" ")),
        )


def find_tasks_for_10min_reminder(now=None):
    """Return list of (task_row, employee_id) for which we should send 10-min reminder."""
    if now is None:
        now = _utcnow()

    db = get_db()
    window_start = now
    window_end = now + timedelta(minutes=5)  # small window

    rows = db.execute(
        """
        SELECT t.*, e.id AS assignee_id, e.email AS assignee_email, e.mobile AS assignee_mobile
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_to
        WHERE t.status IN ('open', 'in_progress')
          AND t.reminder_profile = 'standard_dg'
          AND DATETIME(t.due_at, '-10 minutes') BETWEEN ? AND ?
        """,
        (window_start.isoformat(sep=" "), window_end.isoformat(sep=" ")),
    ).fetchall()

    results = []
    for r in rows:
        results.append((r, r["assignee_id"]))

        # watchers
        watchers = db.execute(
            """
            SELECT w.employee_id, emp.email, emp.mobile
            FROM task_watchers w
            JOIN employees emp ON emp.id = w.employee_id
            WHERE w.task_id = ?
            """,
            (r["id"],),
        ).fetchall()

        for w in watchers:
            results.append((r, w["employee_id"]))

    return results


@app.route("/internal/run-reminders")
def run_reminders():
    """Called by cron/scheduler — checks all reminder schedules and fires due ones."""
    now = _utcnow()
    db = get_db()
    window_end = now + timedelta(minutes=5)

    # find all reminder schedules whose fire-time falls within the next 5-minute window
    due_schedules = db.execute(
        """
        SELECT rs.id AS schedule_id, rs.offset_minutes, rs.label,
               t.*
        FROM task_reminder_schedules rs
        JOIN tasks t ON t.id = rs.task_id
        WHERE t.status IN ('open', 'in_progress')
          AND DATETIME(t.due_at, '-' || rs.offset_minutes || ' minutes') BETWEEN ? AND ?
        """,
        (now.isoformat(sep=" "), window_end.isoformat(sep=" ")),
    ).fetchall()

    fired = []
    for row in due_schedules:
        reminder_type = f"t_minus_{row['offset_minutes']}m"
        # check if already sent for this schedule
        already = db.execute(
            "SELECT 1 FROM task_reminders WHERE task_id=? AND reminder_type=?",
            (row["id"], reminder_type),
        ).fetchone()
        if already:
            continue

        msg = (
            f"Reminder ({row['label']}): Task '{row['title']}' is due at {row['due_at']}. "
            "Please take action."
        )
        _notify_all_participants(db, row, msg, now, reminder_type=reminder_type)
        fired.append({"task_id": row["id"], "title": row["title"], "offset": row["offset_minutes"]})

    db.commit()
    return jsonify(fired)


# ---------------------------
# Notification senders
# ---------------------------

def _send_email(to_email, to_name, subject, body_text):
    """Email via Brevo SMTP API."""
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL or not to_email:
        return
    try:
        http_requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender": {"email": BREVO_SENDER_EMAIL},
                "to": [{"email": to_email, "name": to_name}],
                "subject": subject,
                "textContent": body_text,
            },
            timeout=10,
        )
    except Exception:
        pass


def _send_whatsapp(mobile, message):
    """WhatsApp via Meta Cloud API (direct). mobile = 919XXXXXXXXX (no +)."""
    if not META_WA_TOKEN or not META_WA_PHONE_ID or not mobile:
        return  # TODO: credentials not yet configured
    try:
        payload = {
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "text",
            "text": {"body": message},
        }
        # If using a pre-approved template instead of free-form text:
        # payload = {
        #     "messaging_product": "whatsapp",
        #     "to": mobile,
        #     "type": "template",
        #     "template": {
        #         "name": META_WA_TEMPLATE,
        #         "language": {"code": "en"},
        #         "components": [{"type": "body", "parameters": [{"type": "text", "text": message}]}],
        #     },
        # }
        http_requests.post(
            f"https://graph.facebook.com/v19.0/{META_WA_PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {META_WA_TOKEN}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
    except Exception:
        pass


def _send_sms(mobile, message):
    """SMS via MSG91. mobile = 919XXXXXXXXX (no +)."""
    if not MSG91_AUTH_KEY or not MSG91_SENDER_ID or not mobile:
        return  # TODO: credentials not yet configured
    try:
        http_requests.post(
            "https://api.msg91.com/api/v5/flow/",
            headers={"authkey": MSG91_AUTH_KEY, "Content-Type": "application/json"},
            json={
                "flow_id": MSG91_TEMPLATE_ID,
                "sender": MSG91_SENDER_ID,
                "mobiles": mobile,
                "body": message,      # matches your DLT template variable
            },
            timeout=10,
        )
    except Exception:
        pass


def _do_send_reminders():
    """Called by the APScheduler every 5 minutes."""
    with app.app_context():
        run_reminders()


def _send_birthday_wishes():
    """Called daily at 09:00 to wish employees whose birthday is today."""
    with app.app_context():
        today = datetime.now().strftime("%m-%d")   # MM-DD
        db = get_db()
        # date_of_birth stored as YYYY-MM-DD
        employees = db.execute(
            "SELECT * FROM employees WHERE strftime('%m-%d', date_of_birth) = ?", (today,)
        ).fetchall()
        for emp in employees:
            msg = (
                f"Dear {emp['name']},\n\n"
                f"Wishing you a very Happy Birthday! \U0001f382\n\n"
                f"Best wishes from the DG Constructions family."
            )
            _send_email(emp["email"], emp["name"], "Happy Birthday! \U0001f38a", msg)
            _send_whatsapp(emp["mobile"], msg)
            _send_sms(emp["mobile"], msg)


# ---------------------------
# Scheduler startup
# ---------------------------

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_do_send_reminders, "interval", minutes=5)
    _scheduler.add_job(_send_birthday_wishes, "cron", hour=9, minute=0)
    _scheduler.start()
except ImportError:
    pass   # apscheduler not installed — reminders won't fire automatically


# ---------------------------
# Calendar view
# ---------------------------

@app.route("/calendar")
@login_required
def calendar_view():
    user = current_user()
    db = get_db()
    # Admin/manager can see and filter all staff;
    # others only see themselves in the sidebar
    if user["role"] in ("admin", "manager"):
        employees = db.execute("SELECT id, name FROM employees ORDER BY name ASC").fetchall()
    else:
        employees = db.execute("SELECT id, name FROM employees WHERE id = ?", (user["id"],)).fetchall()
    colors = {emp["id"]: _emp_color(emp["id"]) for emp in employees}
    gcal_connected = Path(GCAL_TOKEN_FILE).exists()
    return render_template(
        "calendar.html",
        user=user,
        employees=employees,
        colors=colors,
        gcal_connected=gcal_connected,
        gcal_available=GCAL_AVAILABLE,
    )


@app.route("/api/calendar/events")
@login_required
def api_calendar_events():
    user = current_user()
    db = get_db()
    staff_param = request.args.get("staff", "all")
    show_done = request.args.get("done", "0") == "1"

    query = """
        SELECT t.id, t.title, t.description, t.due_at, t.priority, t.status,
               e.id AS emp_id, e.name AS assignee_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_to
        WHERE t.status != 'cancelled'
    """
    params = []

    if user["role"] in ("admin", "manager"):
        # Admin/manager: can filter by any staff
        if not show_done:
            query += " AND t.status != 'done'"
        if staff_param != "all":
            ids = [i.strip() for i in staff_param.split(",") if i.strip().isdigit()]
            if ids:
                query += f" AND t.assigned_to IN ({','.join('?' * len(ids))})"
                params.extend(int(i) for i in ids)
    else:
        # Regular staff: only tasks they are involved in
        if not show_done:
            query += " AND t.status != 'done'"
        query += """
          AND (
            t.assigned_to = ?
            OR t.created_by = ?
            OR t.id IN (SELECT task_id FROM task_watchers WHERE employee_id = ?)
          )
        """
        params.extend([user["id"], user["id"], user["id"]])

    rows = db.execute(query, params).fetchall()
    events = []
    for r in rows:
        color = _emp_color(r["emp_id"])
        due = r["due_at"]  # "YYYY-MM-DD HH:MM:SS"
        if not due:
            continue
        events.append({
            "id": r["id"],
            "title": r["title"],
            "start": due[:16].replace(" ", "T"),
            "backgroundColor": color,
            "borderColor": color,
            "url": f"/tasks/{r['id']}",
            "extendedProps": {
                "assignee": r["assignee_name"],
                "priority": r["priority"],
                "status": r["status"],
                "emp_id": r["emp_id"],
            },
        })
    return jsonify(events)


# ---------------------------
# Google Calendar integration
# ---------------------------

def _gcal_service():
    """Return an authorised Google Calendar service, or None."""
    if not GCAL_AVAILABLE or not Path(GCAL_TOKEN_FILE).exists():
        return None
    try:
        creds = GCredentials.from_authorized_user_file(GCAL_TOKEN_FILE, GCAL_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
            Path(GCAL_TOKEN_FILE).write_text(creds.to_json())
        return gcal_build("calendar", "v3", credentials=creds)
    except Exception:
        return None


@app.route("/calendar/google/auth")
@login_required
def gcal_auth():
    if not GCAL_AVAILABLE:
        flash("Run: pip install google-auth-oauthlib google-api-python-client", "error")
        return redirect(url_for("calendar_view"))
    if not Path(GOOGLE_CLIENT_SECRETS_FILE).exists():
        flash("Place google_credentials.json in the app folder first (see Google Cloud Console).", "error")
        return redirect(url_for("calendar_view"))
    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS_FILE,
        scopes=GCAL_SCOPES,
        redirect_uri=url_for("gcal_callback", _external=True),
    )
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["gcal_state"] = state
    return redirect(auth_url)


@app.route("/calendar/google/callback")
def gcal_callback():
    if not GCAL_AVAILABLE:
        return "Google libraries not installed.", 500
    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS_FILE,
        scopes=GCAL_SCOPES,
        state=session.get("gcal_state", ""),
        redirect_uri=url_for("gcal_callback", _external=True),
    )
    flow.fetch_token(authorization_response=request.url)
    Path(GCAL_TOKEN_FILE).write_text(flow.credentials.to_json())
    flash("Google Calendar connected successfully!", "success")
    return redirect(url_for("calendar_view"))


@app.route("/calendar/google/sync", methods=["POST"])
@login_required
def gcal_sync():
    service = _gcal_service()
    if not service:
        flash("Google Calendar not connected. Click 'Connect Google Calendar' first.", "error")
        return redirect(url_for("calendar_view"))

    db = get_db()
    tasks = db.execute(
        """
        SELECT t.*, e.name AS assignee_name
        FROM tasks t JOIN employees e ON e.id = t.assigned_to
        WHERE t.status IN ('open', 'in_progress')
        """
    ).fetchall()

    synced = 0
    for task in tasks:
        try:
            due_dt = datetime.strptime(task["due_at"][:16], "%Y-%m-%d %H:%M")
            end_dt = due_dt + timedelta(hours=1)
            body = {
                "summary": f"[DG] {task['title']}",
                "description": (
                    f"Assigned to: {task['assignee_name']}\n"
                    f"Priority: {task['priority']}\nStatus: {task['status']}\n\n"
                    f"{task['description'] or ''}"
                ),
                "start": {"dateTime": due_dt.isoformat(), "timeZone": "Asia/Kolkata"},
                "end":   {"dateTime": end_dt.isoformat(),  "timeZone": "Asia/Kolkata"},
            }
            existing = db.execute(
                "SELECT gcal_event_id FROM gcal_sync WHERE task_id = ?", (task["id"],)
            ).fetchone()
            if existing:
                service.events().update(
                    calendarId=GCAL_CALENDAR_ID,
                    eventId=existing["gcal_event_id"],
                    body=body,
                ).execute()
            else:
                result = service.events().insert(calendarId=GCAL_CALENDAR_ID, body=body).execute()
                db.execute(
                    "INSERT INTO gcal_sync (task_id, gcal_event_id, synced_at) VALUES (?, ?, ?)",
                    (task["id"], result["id"], _utcnow().isoformat(sep=" ")),
                )
            synced += 1
        except Exception:
            pass

    db.commit()
    flash(f"Synced {synced} tasks to Google Calendar.", "success")
    return redirect(url_for("calendar_view"))


@app.route("/calendar/google/disconnect", methods=["POST"])
@login_required
def gcal_disconnect():
    Path(GCAL_TOKEN_FILE).unlink(missing_ok=True)
    flash("Google Calendar disconnected.", "success")
    return redirect(url_for("calendar_view"))


# ---------------------------
# JSON API for eigent / Claude skill
# ---------------------------

def _api_auth():
    """Returns None if authorised, else a 401 JSON response."""
    token = request.headers.get("X-API-Token", "")
    if token != API_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    err = _api_auth()
    if err:
        return err
    db = get_db()
    status_filter = request.args.get("status")  # optional ?status=open
    assignee_filter = request.args.get("assignee_name")

    query = """
        SELECT t.id, t.title, t.description, t.priority, t.status,
               t.due_at, t.created_at,
               e.name AS assignee_name, c.name AS creator_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_to
        JOIN employees c ON c.id = t.created_by
        WHERE 1=1
    """
    params = []
    if status_filter:
        query += " AND t.status = ?"
        params.append(status_filter)
    if assignee_filter:
        query += " AND e.name LIKE ?"
        params.append(f"%{assignee_filter}%")
    query += " ORDER BY t.due_at ASC"

    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    err = _api_auth()
    if err:
        return err
    data = request.get_json(force=True)

    title       = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    priority    = data.get("priority", "normal")
    due_at      = (data.get("due_at") or "").strip()   # "YYYY-MM-DD HH:MM"
    assignee_name = (data.get("assignee_name") or "").strip()
    creator_name  = (data.get("creator_name") or "").strip()

    if not title or not due_at or not assignee_name or not creator_name:
        return jsonify({"error": "title, due_at, assignee_name and creator_name are required"}), 400

    try:
        datetime.strptime(due_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"error": "due_at must be YYYY-MM-DD HH:MM"}), 400

    db = get_db()
    assignee = db.execute("SELECT id FROM employees WHERE name LIKE ?", (f"%{assignee_name}%",)).fetchone()
    creator  = db.execute("SELECT id FROM employees WHERE name LIKE ?", (f"%{creator_name}%",)).fetchone()

    if not assignee:
        return jsonify({"error": f"Employee not found: {assignee_name}"}), 404
    if not creator:
        return jsonify({"error": f"Employee not found: {creator_name}"}), 404

    cur = db.execute(
        "INSERT INTO tasks (title, description, priority, due_at, original_due_at, created_by, assigned_to, recurrence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, description, priority, due_at + ":00", due_at + ":00", creator["id"], assignee["id"],
         data.get("recurrence") or None),
    )
    db.commit()
    return jsonify({"success": True, "task_id": cur.lastrowid}), 201


@app.route("/api/tasks/<int:task_id>", methods=["GET"])
def api_get_task(task_id):
    err = _api_auth()
    if err:
        return err
    db = get_db()
    row = db.execute(
        """
        SELECT t.*, e.name AS assignee_name, c.name AS creator_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_to
        JOIN employees c ON c.id = t.created_by
        WHERE t.id = ?
        """,
        (task_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@app.route("/api/tasks/<int:task_id>/status", methods=["PATCH"])
def api_update_status(task_id):
    err = _api_auth()
    if err:
        return err
    data = request.get_json(force=True)
    new_status = data.get("status", "")
    if new_status not in ("open", "in_progress", "done", "cancelled"):
        return jsonify({"error": "status must be open|in_progress|done|cancelled"}), 400
    db = get_db()
    db.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    db.commit()
    return jsonify({"success": True, "task_id": task_id, "status": new_status})


@app.route("/api/employees", methods=["GET"])
def api_list_employees():
    err = _api_auth()
    if err:
        return err
    db = get_db()
    rows = db.execute("SELECT id, name, mobile, email, role FROM employees ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


# ============================================================
# LOCATION MANAGEMENT
# ============================================================

@app.route("/locations")
@login_required
def locations_list():
    user = current_user()
    if user["role"] not in ("admin", "manager"):
        flash("Access denied.", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    locs = db.execute("SELECT * FROM locations ORDER BY name ASC").fetchall()
    return render_template("locations.html", user=user, locations=locs)


@app.route("/locations/add", methods=["POST"])
@login_required
def add_location():
    user = current_user()
    if user["role"] not in ("admin", "manager"):
        return "Forbidden", 403
    name       = request.form.get("name", "").strip()
    address    = request.form.get("address", "").strip()
    city       = request.form.get("city", "").strip()
    weekly_off = request.form.get("weekly_off", "Sunday").strip()
    if not name:
        flash("Location name is required.", "error")
        return redirect(url_for("locations_list"))
    db = get_db()
    db.execute(
        "INSERT INTO locations (name, address, city, weekly_off) VALUES (?, ?, ?, ?)",
        (name, address or None, city or None, weekly_off),
    )
    db.commit()
    flash(f"Location '{name}' added.", "success")
    return redirect(url_for("locations_list"))


@app.route("/locations/<int:loc_id>/edit", methods=["POST"])
@login_required
def edit_location(loc_id):
    user = current_user()
    if user["role"] not in ("admin", "manager"):
        return "Forbidden", 403
    name       = request.form.get("name", "").strip()
    address    = request.form.get("address", "").strip()
    city       = request.form.get("city", "").strip()
    weekly_off = request.form.get("weekly_off", "Sunday").strip()
    is_active  = 1 if request.form.get("is_active") else 0
    db = get_db()
    db.execute(
        "UPDATE locations SET name=?, address=?, city=?, weekly_off=?, is_active=? WHERE id=?",
        (name, address or None, city or None, weekly_off, is_active, loc_id),
    )
    db.commit()
    flash("Location updated.", "success")
    return redirect(url_for("locations_list"))


@app.route("/locations/<int:loc_id>/delete", methods=["POST"])
@login_required
def delete_location(loc_id):
    user = current_user()
    if user["role"] != "admin":
        return "Forbidden", 403
    db = get_db()
    db.execute("UPDATE employees SET location_id = NULL WHERE location_id = ?", (loc_id,))
    db.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    db.commit()
    flash("Location deleted.", "success")
    return redirect(url_for("locations_list"))


# ============================================================
# ATTENDANCE
# ============================================================

@app.route("/attendance")
@login_required
def attendance_view():
    user = current_user()
    db = get_db()
    today = _utcnow().date()
    month = int(request.args.get("month", today.month))
    year  = int(request.args.get("year",  today.year))
    if user["role"] in ("admin", "manager"):
        emp_id = request.args.get("emp_id", type=int) or None
    else:
        emp_id = user["id"]
    month_str = f"{year:04d}-{month:02d}"
    if emp_id:
        rows = db.execute(
            """
            SELECT a.*, e.name AS emp_name, l.name AS loc_name, m.name AS marker_name
            FROM attendance a
            JOIN employees e ON e.id = a.employee_id
            LEFT JOIN locations l ON l.id = a.location_id
            LEFT JOIN employees m ON m.id = a.marked_by
            WHERE a.employee_id = ? AND strftime('%Y-%m', a.date) = ?
            ORDER BY a.date ASC
            """,
            (emp_id, month_str),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT a.*, e.name AS emp_name, l.name AS loc_name, m.name AS marker_name
            FROM attendance a
            JOIN employees e ON e.id = a.employee_id
            LEFT JOIN locations l ON l.id = a.location_id
            LEFT JOIN employees m ON m.id = a.marked_by
            WHERE strftime('%Y-%m', a.date) = ?
            ORDER BY a.date ASC, e.name ASC
            """,
            (month_str,),
        ).fetchall()
    employees = db.execute("SELECT id, name FROM employees ORDER BY name").fetchall()
    locations = db.execute("SELECT id, name FROM locations WHERE is_active=1 ORDER BY name").fetchall()
    summary = {"present": 0, "absent": 0, "half_day": 0, "leave": 0}
    for r in rows:
        if r["status"] in summary:
            summary[r["status"]] += 1
    return render_template(
        "attendance.html",
        user=user, rows=rows, employees=employees, locations=locations,
        month=month, year=year, emp_id=emp_id, summary=summary,
        today=str(today),
    )


@app.route("/attendance/mark", methods=["POST"])
@login_required
def mark_attendance():
    user = current_user()
    db = get_db()
    if user["role"] in ("admin", "manager"):
        emp_id = int(request.form.get("employee_id", user["id"]))
    else:
        emp_id = user["id"]
    date        = request.form.get("date", "").strip()
    status      = request.form.get("status", "present")
    check_in    = request.form.get("check_in", "").strip() or None
    check_out   = request.form.get("check_out", "").strip() or None
    notes       = request.form.get("notes", "").strip() or None
    location_id = request.form.get("location_id", type=int) or None
    if not date:
        flash("Date is required.", "error")
        return redirect(url_for("attendance_view"))
    db.execute(
        """
        INSERT INTO attendance (employee_id, date, status, check_in, check_out, notes, marked_by, location_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(employee_id, date) DO UPDATE SET
            status=excluded.status, check_in=excluded.check_in,
            check_out=excluded.check_out, notes=excluded.notes,
            marked_by=excluded.marked_by, location_id=excluded.location_id
        """,
        (emp_id, date, status, check_in, check_out, notes, user["id"], location_id),
    )
    db.commit()
    flash("Attendance saved.", "success")
    return redirect(url_for("attendance_view"))


# ============================================================
# LEAVE MANAGEMENT
# ============================================================

def _ensure_leave_balances(db, employee_id, year):
    types = db.execute("SELECT * FROM leave_types").fetchall()
    for lt in types:
        db.execute(
            "INSERT OR IGNORE INTO leave_balances (employee_id, leave_type_id, year, total_days, used_days) "
            "VALUES (?, ?, ?, ?, 0)",
            (employee_id, lt["id"], year, lt["max_days_per_year"]),
        )
    db.commit()


@app.route("/leaves")
@login_required
def leaves_view():
    user = current_user()
    db = get_db()
    year = int(request.args.get("year", _utcnow().year))
    if user["role"] in ("admin", "manager"):
        emp_id = request.args.get("emp_id", type=int) or None
    else:
        emp_id = user["id"]
    pending = []
    if user["role"] in ("admin", "manager"):
        pending = db.execute(
            """
            SELECT lr.*, e.name AS emp_name, lt.name AS type_name
            FROM leave_requests lr
            JOIN employees e ON e.id = lr.employee_id
            JOIN leave_types lt ON lt.id = lr.leave_type_id
            WHERE lr.status = 'pending'
            ORDER BY lr.applied_at ASC
            """
        ).fetchall()
    if emp_id:
        _ensure_leave_balances(db, emp_id, year)
        my_requests = db.execute(
            """
            SELECT lr.*, lt.name AS type_name
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id = lr.leave_type_id
            WHERE lr.employee_id = ? AND strftime('%Y', lr.start_date) = ?
            ORDER BY lr.applied_at DESC
            """,
            (emp_id, str(year)),
        ).fetchall()
        balances = db.execute(
            """
            SELECT lb.*, lt.name AS type_name
            FROM leave_balances lb
            JOIN leave_types lt ON lt.id = lb.leave_type_id
            WHERE lb.employee_id = ? AND lb.year = ?
            """,
            (emp_id, year),
        ).fetchall()
    else:
        my_requests = []
        balances = []
    leave_types = db.execute("SELECT * FROM leave_types ORDER BY name").fetchall()
    employees   = db.execute("SELECT id, name FROM employees ORDER BY name").fetchall()
    return render_template(
        "leaves.html",
        user=user, pending=pending, my_requests=my_requests,
        balances=balances, leave_types=leave_types, employees=employees,
        emp_id=emp_id, year=year,
    )


@app.route("/leaves/apply", methods=["POST"])
@login_required
def apply_leave():
    from datetime import date as date_cls
    user = current_user()
    db = get_db()
    if user["role"] in ("admin", "manager"):
        emp_id = int(request.form.get("employee_id", user["id"]))
    else:
        emp_id = user["id"]
    leave_type_id = int(request.form.get("leave_type_id"))
    start_date    = request.form.get("start_date", "").strip()
    end_date      = request.form.get("end_date", "").strip()
    reason        = request.form.get("reason", "").strip() or None
    if not start_date or not end_date:
        flash("Start and end dates are required.", "error")
        return redirect(url_for("leaves_view"))
    try:
        s = date_cls.fromisoformat(start_date)
        e = date_cls.fromisoformat(end_date)
    except ValueError:
        flash("Invalid dates.", "error")
        return redirect(url_for("leaves_view"))
    days = (e - s).days + 1
    if days <= 0:
        flash("End date must be on or after start date.", "error")
        return redirect(url_for("leaves_view"))
    year = s.year
    _ensure_leave_balances(db, emp_id, year)
    balance = db.execute(
        "SELECT * FROM leave_balances WHERE employee_id=? AND leave_type_id=? AND year=?",
        (emp_id, leave_type_id, year),
    ).fetchone()
    remaining = (balance["total_days"] - balance["used_days"]) if balance else 0
    if days > remaining:
        flash(f"Insufficient leave balance. Available: {remaining:.0f} day(s).", "error")
        return redirect(url_for("leaves_view"))
    db.execute(
        "INSERT INTO leave_requests (employee_id, leave_type_id, start_date, end_date, days, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (emp_id, leave_type_id, start_date, end_date, days, reason),
    )
    db.commit()
    flash("Leave request submitted.", "success")
    return redirect(url_for("leaves_view"))


@app.route("/leaves/<int:req_id>/decide", methods=["POST"])
@login_required
def decide_leave(req_id):
    user = current_user()
    if user["role"] not in ("admin", "manager"):
        return "Forbidden", 403
    db = get_db()
    req = db.execute("SELECT * FROM leave_requests WHERE id=?", (req_id,)).fetchone()
    if not req or req["status"] != "pending":
        flash("Request not found or already decided.", "error")
        return redirect(url_for("leaves_view"))
    action     = request.form.get("action", "approve")
    new_status = "approved" if action == "approve" else "rejected"
    db.execute(
        "UPDATE leave_requests SET status=?, approved_by=?, decided_at=? WHERE id=?",
        (new_status, user["id"], _utcnow().isoformat(sep=" "), req_id),
    )
    if new_status == "approved":
        year = int(req["start_date"][:4])
        _ensure_leave_balances(db, req["employee_id"], year)
        db.execute(
            "UPDATE leave_balances SET used_days = used_days + ? "
            "WHERE employee_id=? AND leave_type_id=? AND year=?",
            (req["days"], req["employee_id"], req["leave_type_id"], year),
        )
    db.commit()
    flash(f"Leave request {new_status}.", "success")
    return redirect(url_for("leaves_view"))


@app.route("/leaves/<int:req_id>/cancel", methods=["POST"])
@login_required
def cancel_leave(req_id):
    user = current_user()
    db = get_db()
    req = db.execute("SELECT * FROM leave_requests WHERE id=?", (req_id,)).fetchone()
    if not req:
        return "Not found", 404
    if req["employee_id"] != user["id"] and user["role"] not in ("admin", "manager"):
        return "Forbidden", 403
    if req["status"] not in ("pending", "approved"):
        flash("Cannot cancel this leave.", "error")
        return redirect(url_for("leaves_view"))
    if req["status"] == "approved":
        year = int(req["start_date"][:4])
        db.execute(
            "UPDATE leave_balances SET used_days = MAX(0, used_days - ?) "
            "WHERE employee_id=? AND leave_type_id=? AND year=?",
            (req["days"], req["employee_id"], req["leave_type_id"], year),
        )
    db.execute("DELETE FROM leave_requests WHERE id=?", (req_id,))
    db.commit()
    flash("Leave request cancelled.", "success")
    return redirect(url_for("leaves_view"))


# ============================================================
# SALARY STRUCTURE & SLIP
# ============================================================

@app.route("/salary")
@login_required
def salary_view():
    user = current_user()
    if user["role"] not in ("admin", "manager", "hr"):
        flash("Access denied.", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    employees = db.execute(
        """
        SELECT e.*, ss.basic, ss.hra, ss.medical_allowance,
               ss.travel_allowance, ss.other_allowance,
               ss.pf_rate, ss.esi_rate, ss.tds, ss.id AS struct_id
        FROM employees e
        LEFT JOIN salary_structures ss ON ss.employee_id = e.id
        ORDER BY e.name
        """
    ).fetchall()
    slips = db.execute(
        """
        SELECT sp.*, e.name AS emp_name
        FROM salary_slips sp
        JOIN employees e ON e.id = sp.employee_id
        ORDER BY sp.year DESC, sp.month DESC, e.name ASC
        LIMIT 60
        """
    ).fetchall()
    today = _utcnow()
    return render_template("salary.html", user=user, employees=employees, slips=slips,
                           today=today)


@app.route("/salary/structure/save", methods=["POST"])
@login_required
def save_salary_structure():
    user = current_user()
    if user["role"] not in ("admin", "hr"):
        return "Forbidden", 403
    db = get_db()
    emp_id            = int(request.form.get("employee_id"))
    basic             = int(request.form.get("basic", 0))
    hra               = int(request.form.get("hra", 0))
    medical_allowance = int(request.form.get("medical_allowance", 0))
    travel_allowance  = int(request.form.get("travel_allowance", 0))
    other_allowance   = int(request.form.get("other_allowance", 0))
    pf_rate           = float(request.form.get("pf_rate", 12.0))
    esi_rate          = float(request.form.get("esi_rate", 0.75))
    tds               = int(request.form.get("tds", 0))
    effective_from    = request.form.get("effective_from", "").strip() or None
    db.execute(
        """
        INSERT INTO salary_structures
            (employee_id, basic, hra, medical_allowance, travel_allowance,
             other_allowance, pf_rate, esi_rate, tds, effective_from, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(employee_id) DO UPDATE SET
            basic=excluded.basic, hra=excluded.hra,
            medical_allowance=excluded.medical_allowance,
            travel_allowance=excluded.travel_allowance,
            other_allowance=excluded.other_allowance,
            pf_rate=excluded.pf_rate, esi_rate=excluded.esi_rate,
            tds=excluded.tds, effective_from=excluded.effective_from,
            updated_at=excluded.updated_at
        """,
        (emp_id, basic, hra, medical_allowance, travel_allowance,
         other_allowance, pf_rate, esi_rate, tds, effective_from,
         _utcnow().isoformat(sep=" ")),
    )
    db.commit()
    flash("Salary structure saved.", "success")
    return redirect(url_for("salary_view"))


@app.route("/salary/slip/generate", methods=["POST"])
@login_required
def generate_salary_slip():
    user = current_user()
    if user["role"] not in ("admin", "hr"):
        return "Forbidden", 403
    db = get_db()
    emp_id = int(request.form.get("employee_id"))
    month  = int(request.form.get("month"))
    year   = int(request.form.get("year"))
    ss = db.execute("SELECT * FROM salary_structures WHERE employee_id=?", (emp_id,)).fetchone()
    if not ss:
        flash("No salary structure defined for this employee.", "error")
        return redirect(url_for("salary_view"))
    month_str = f"{year:04d}-{month:02d}"
    att_rows = db.execute(
        "SELECT status FROM attendance WHERE employee_id=? AND strftime('%Y-%m', date)=?",
        (emp_id, month_str),
    ).fetchall()
    present_days = sum(
        1 if r["status"] == "present" else 0.5 if r["status"] == "half_day" else 0
        for r in att_rows
    )
    working_days = calendar.monthrange(year, month)[1]
    ratio   = (present_days / working_days) if working_days else 1
    basic   = round(ss["basic"]             * ratio)
    hra     = round(ss["hra"]               * ratio)
    med     = round(ss["medical_allowance"] * ratio)
    trav    = round(ss["travel_allowance"]  * ratio)
    other   = round(ss["other_allowance"]   * ratio)
    gross   = basic + hra + med + trav + other
    pf_emp  = round(basic * ss["pf_rate"] / 100)
    pf_empr = round(basic * ss["pf_rate"] / 100)
    esi_emp  = round(gross * ss["esi_rate"] / 100)
    esi_empr = round(gross * 3.25 / 100)
    tds     = ss["tds"]
    net_pay = gross - pf_emp - esi_emp - tds
    db.execute(
        """
        INSERT INTO salary_slips
            (employee_id, month, year, working_days, present_days,
             basic, hra, medical_allowance, travel_allowance, other_allowance, gross,
             pf_employee, pf_employer, esi_employee, esi_employer, tds,
             other_deductions, net_pay, generated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(employee_id, month, year) DO UPDATE SET
            working_days=excluded.working_days, present_days=excluded.present_days,
            basic=excluded.basic, hra=excluded.hra,
            medical_allowance=excluded.medical_allowance,
            travel_allowance=excluded.travel_allowance,
            other_allowance=excluded.other_allowance, gross=excluded.gross,
            pf_employee=excluded.pf_employee, pf_employer=excluded.pf_employer,
            esi_employee=excluded.esi_employee, esi_employer=excluded.esi_employer,
            tds=excluded.tds, net_pay=excluded.net_pay,
            generated_by=excluded.generated_by, generated_at=excluded.generated_at
        """,
        (emp_id, month, year, working_days, present_days,
         basic, hra, med, trav, other, gross,
         pf_emp, pf_empr, esi_emp, esi_empr, tds, net_pay, user["id"]),
    )
    db.commit()
    slip = db.execute(
        "SELECT id FROM salary_slips WHERE employee_id=? AND month=? AND year=?",
        (emp_id, month, year),
    ).fetchone()
    flash("Salary slip generated.", "success")
    return redirect(url_for("salary_slip_view", slip_id=slip["id"]))


@app.route("/salary/slip/<int:slip_id>")
@login_required
def salary_slip_view(slip_id):
    user = current_user()
    db = get_db()
    slip = db.execute(
        """
        SELECT sp.*, e.name AS emp_name, e.designation, e.department,
               e.email, e.mobile, e.date_of_joining,
               e.uan_number, e.pan_number,
               l.name AS location_name, gen.name AS generated_by_name
        FROM salary_slips sp
        JOIN employees e ON e.id = sp.employee_id
        LEFT JOIN locations l ON l.id = e.location_id
        JOIN employees gen ON gen.id = sp.generated_by
        WHERE sp.id = ?
        """,
        (slip_id,),
    ).fetchone()
    if not slip:
        return "Not found", 404
    if user["role"] not in ("admin", "manager", "hr") and slip["employee_id"] != user["id"]:
        return "Forbidden", 403
    import calendar as _cal
    month_name = _cal.month_name[slip["month"]]
    return render_template("salary_slip.html", user=user, slip=slip, month_name=month_name)


if __name__ == "__main__":
    if not DB_PATH.exists():
        with app.app_context():
            init_db()
    else:
        with app.app_context():
            _migrate_db()   # adds new tables to existing DB
    app.run(debug=True, use_reloader=False)