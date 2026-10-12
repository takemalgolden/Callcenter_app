from flask import Flask, request, redirect, url_for, render_template, flash, session, send_file
import sqlite3
import realtime_sync
import flexible_optimizer as flex
import bulk_shift_delete
import operator_shift_export
import operator_shift_visibility
import monthly_workdays
import attendance_sync
import re, os, csv, io, json, calendar, hashlib, hmac, secrets
from datetime import date, datetime, timedelta
from functools import wraps
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("UNIFIED_DB_PATH", os.path.join(APP_DIR, "shift_planner.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("SHIFT_APP_SECRET", "change-this-secret-before-use")

DEFAULT_SHIFTS = [
    ("早番", "09:20", "18:00", 60),
    ("中番", "11:00", "20:00", 60),
    ("遅番", "14:00", "23:00", 60),
    ("半日午前", "09:20", "13:00", 0),
    ("半日午後", "18:00", "23:00", 0),
]

def valid_password(value):
    if not 8 <= len(value) <= 16:
        return False
    return sum((any(ch.isupper() for ch in value), any(ch.islower() for ch in value),
                any(ch.isdigit() for ch in value), any(not ch.isalnum() for ch in value))) >= 3

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'operator', employee_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS employees(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          skills TEXT NOT NULL DEFAULT 'consumer', monthly_days INTEGER DEFAULT 20,
          weekly_max_days INTEGER NOT NULL DEFAULT 5,
          monthly_max_days INTEGER DEFAULT NULL,
          min_hours REAL DEFAULT 4, max_hours REAL DEFAULT 9, max_consecutive INTEGER DEFAULT 5,
          early_ok INTEGER DEFAULT 1, mid_ok INTEGER DEFAULT 1, late_ok INTEGER DEFAULT 1,
          notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS shift_patterns(
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, start_time TEXT NOT NULL,
          end_time TEXT NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 60, active INTEGER DEFAULT 1,
          shift_type TEXT NOT NULL DEFAULT 'auto'
        );
        CREATE TABLE IF NOT EXISTS coverage(
          id INTEGER PRIMARY KEY, weekday INTEGER NOT NULL, start_time TEXT NOT NULL,
          end_time TEXT NOT NULL, skill TEXT NOT NULL, min_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS requests(
          id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, request_date TEXT NOT NULL,
          request_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', note TEXT DEFAULT '',
          FOREIGN KEY(employee_id) REFERENCES employees(id)
        );
        CREATE TABLE IF NOT EXISTS assignments(
          id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, work_date TEXT NOT NULL,
          pattern_id INTEGER, skill TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
          note TEXT DEFAULT '', UNIQUE(employee_id, work_date),
          FOREIGN KEY(employee_id) REFERENCES employees(id),
          FOREIGN KEY(pattern_id) REFERENCES shift_patterns(id)
        );
        CREATE TABLE IF NOT EXISTS approvals(
          id INTEGER PRIMARY KEY, work_month TEXT NOT NULL, admin_username TEXT NOT NULL,
          action TEXT NOT NULL, reason TEXT DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_skill_plans(
          work_date TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'draft',
          plan_json TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_skill_overrides(
          id INTEGER PRIMARY KEY, work_date TEXT NOT NULL, employee_id INTEGER NOT NULL,
          start_time TEXT NOT NULL, end_time TEXT NOT NULL, skill TEXT NOT NULL,
          FOREIGN KEY(employee_id) REFERENCES employees(id));
        CREATE INDEX IF NOT EXISTS idx_daily_skill_overrides_date ON daily_skill_overrides(work_date);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        user_columns = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        if "approved" not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
        if "must_change_password" not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        c.execute("""CREATE TABLE IF NOT EXISTS password_reset_audit (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, username_at_reset TEXT NOT NULL,
            reset_by TEXT NOT NULL, reset_at TEXT NOT NULL
        )""")
        # Explicit shift category makes half-day patterns obey the employee's
        # allowed early/mid/late flags even when their names begin with "半日".
        # Auto preserves existing patterns without data loss.
        pattern_columns = {row[1] for row in c.execute("PRAGMA table_info(shift_patterns)").fetchall()}
        if "shift_type" not in pattern_columns:
            c.execute("ALTER TABLE shift_patterns ADD COLUMN shift_type TEXT NOT NULL DEFAULT 'auto'")
        # Migration for call-center employee linkage.
        employee_columns = {row[1] for row in c.execute("PRAGMA table_info(employees)").fetchall()}
        if "weekly_max_days" not in employee_columns:
            # Preserve existing employee records and monthly target values.
            c.execute("ALTER TABLE employees ADD COLUMN weekly_max_days INTEGER NOT NULL DEFAULT 5")
        if "monthly_max_days" not in employee_columns:
            # Preserve all existing employees/shifts and old monthly_days values.
            # NULL means no extra cap beyond calendar days; current monthly
            # target is dynamically derived from weekly_max_days.
            c.execute("ALTER TABLE employees ADD COLUMN monthly_max_days INTEGER DEFAULT NULL")
        if "source_user_id" not in employee_columns:
            c.execute("ALTER TABLE employees ADD COLUMN source_user_id INTEGER")
        if "source_username" not in employee_columns:
            c.execute("ALTER TABLE employees ADD COLUMN source_username TEXT")
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                      ("admin", generate_password_hash("Admin123!"), "admin"))
        if c.execute("SELECT COUNT(*) FROM shift_patterns").fetchone()[0] == 0:
            c.executemany("INSERT INTO shift_patterns(name,start_time,end_time,break_minutes) VALUES(?,?,?,?)", DEFAULT_SHIFTS)
        if c.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] == 0:
            # Example defaults; admins should replace these with actual staffing rules.
            c.executemany("INSERT INTO coverage(weekday,start_time,end_time,skill,min_count) VALUES(?,?,?,?,?)",
                          [(wd,"09:20","23:00",skill,1) for wd in range(7) for skill in ("merchant","consumer","PP")])

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("管理者権限が必要です。", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapped

def month_days(month):
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return [date(y, m, d) for d in range(1, last+1)]

def mins(t):
    h, m = map(int, t.split(":"))
    return h*60+m

def _hours_as_minutes(value, fallback):
    """Convert stored hours to integer minutes without float boundary errors.

    The same conversion is shared by generation and the diagnosis panel.
    Equal min/max values are deliberately accepted (fixed-length shifts).
    """
    if value is None:
        value = fallback
    try:
        return int((Decimal(str(value)) * 60).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"勤務時間の設定が不正です: {value!r}")


def _pattern_total_minutes(pattern):
    """Scheduled shift duration INCLUDING breaks (end - start).

    Employee min_hours/max_hours represent total time on the shift, not
    the net hours spent working. Keep break_minutes for break allocation.
    """
    return mins(pattern["end_time"]) - mins(pattern["start_time"])


def _pattern_work_minutes(pattern):
    """Net working duration (for display only; never used in shift matching)."""
    return _pattern_total_minutes(pattern) - int(pattern["break_minutes"] or 0)


def _pattern_shift_allowed(employee, pattern):
    """Honor any combination of early/mid/late shift permissions.

    Explicit pattern categories are authoritative. Pre-upgrade records retain
    auto mode; recognize keywords *anywhere* in their names, including
    "半日・早番". Legacy names beginning with 早/中/遅 still work.
    An uncategorized pattern can be used by any employee, so administrators
    should explicitly categorize new half-day patterns.
    """
    fields = pattern.keys() if hasattr(pattern, "keys") else ()
    selected = pattern["shift_type"] if "shift_type" in fields else "auto"
    name = str(pattern["name"] or "")
    flags = {"早番": "early_ok", "中番": "mid_ok", "遅番": "late_ok"}
    if selected in flags:
        return bool(employee[flags[selected]])
    if selected == "共通":
        return True
    found = [key for key in flags if key in name]
    if not found:
        legacy = {"早": "早番", "中": "中番", "遅": "遅番"}
        if name and name[0] in legacy:
            found = [legacy[name[0]]]
    # Ambiguous names must be explicitly categorized, rather than bypass flags.
    if len(found) > 1:
        return False
    return bool(employee[flags[found[0]]]) if found else True


def _pattern_hours_match(employee, pattern):
    """Inclusive interval; min==max permits a pattern with that exact duration."""
    total_minutes = _pattern_total_minutes(pattern)
    lower = _hours_as_minutes(employee["min_hours"], 0)
    upper = _hours_as_minutes(employee["max_hours"], 24)
    return 0 <= total_minutes and lower <= total_minutes <= upper


def _duration_label(total_minutes):
    return f"{total_minutes // 60}時間{total_minutes % 60:02d}分"


def overlaps(a1, a2, b1, b2):
    return max(mins(a1), mins(b1)) < min(mins(a2), mins(b2))

def get_month():
    return request.args.get("month") or date.today().strftime("%Y-%m")

def _callcenter_db_path():
    configured = os.environ.get("CALLCENTER_DB_PATH")
    if configured:
        return os.path.abspath(configured)
    try:
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key='callcenter_db_path'").fetchone()
        if row and os.path.isfile(row["value"]):
            return row["value"]
    except sqlite3.Error:
        pass
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local_app_data:
        candidates.append(os.path.abspath(os.path.join(local_app_data, "CallCenterLocal", "data", "callcenter.sqlite3")))
    candidates.extend([
        os.path.abspath(os.path.join(APP_DIR, "..", "callcenter_local_app", "data", "callcenter.sqlite3")),
        os.path.abspath(os.path.join(APP_DIR, "callcenter_local_app", "data", "callcenter.sqlite3")),
    ])
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _employee_is_linked(employee_id):
    with db() as c:
        row = c.execute("SELECT source_user_id FROM employees WHERE id=?", (employee_id,)).fetchone()
    return bool(row and row["source_user_id"] is not None)


def _authenticate_callcenter_user(username, password):
    path = _callcenter_db_path()
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id,username,role,salt,password_hash,active FROM users WHERE username=?", (username,)).fetchone()
        con.close()
        if not row or not row["active"] or row["role"] != "operator":
            return None
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(row["salt"]), 240000).hex()
        return dict(row) if hmac.compare_digest(digest, row["password_hash"]) else None
    except (sqlite3.Error, OSError, ValueError, KeyError):
        return None


@app.before_request
def enforce_password_change():
    if not session.get("username") or request.endpoint in ("password_page", "logout", "static", "login", "signup"):
        return None
    with db() as c:
        row = c.execute("SELECT must_change_password FROM users WHERE username=?", (session["username"],)).fetchone()
    if row and row["must_change_password"]:
        return redirect(url_for("password_page"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        with db() as c:
            u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if u and u["employee_id"]:
            with db() as c:
                emp_active = c.execute("SELECT active FROM employees WHERE id=?", (u["employee_id"],)).fetchone()
            if not emp_active or not emp_active["active"]:
                u = None
        if u and u["approved"] and check_password_hash(u["password_hash"], password):
            session["username"], session["role"], session["employee_id"] = u["username"], u["role"], u["employee_id"]
            session["callcenter_linked"] = False  # canonical employee manager owns all login credentials
            if u["must_change_password"]:
                return redirect(url_for("password_page"))
            return redirect(url_for("index"))
        # Linked operators may authenticate with the account used in the break-placement app.
        linked = _authenticate_callcenter_user(username, password)
        if linked:
            with db() as c:
                emp = c.execute("SELECT id FROM employees WHERE source_user_id=? AND active=1", (linked["id"],)).fetchone()
                local_user = c.execute("SELECT * FROM users WHERE username=? AND role='operator'", (username,)).fetchone()
            if emp and local_user and local_user["approved"]:
                session["username"], session["role"], session["employee_id"] = username, "operator", emp["id"]
                session["callcenter_linked"] = True
                if local_user["must_change_password"]:
                    flash("管理者に従業員管理画面からパスワードを再設定してもらってください。", "error")
                    session.clear()
                    return redirect(url_for("login"))
                return redirect(url_for("index"))
        flash("ユーザー名またはパスワードが違います。", "error")
    return render_template("login.html")

@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method=="POST":
        username=request.form.get("username","").strip()
        name=request.form.get("name","").strip()
        password=request.form.get("password","")
        confirm=request.form.get("confirm_password","")
        skills=[x for x in request.form.getlist("skills") if x in ("consumer","merchant","PP")]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}",username) or not name or len(name)>100:
            flash("氏名またはログインIDを確認してください。","error")
        elif not valid_password(password) or password!=confirm or not skills:
            flash("パスワード条件・確認入力・スキルを確認してください。","error")
        else:
            try:
                with db() as c:
                    cur=c.execute("INSERT INTO employees(name,skills,active) VALUES(?,?,0)",(name,",".join(skills)))
                    c.execute("INSERT INTO users(username,password_hash,role,employee_id,approved) VALUES(?,?, 'operator',?,0)",
                              (username,generate_password_hash(password),cur.lastrowid))
                flash("登録申請を受け付けました。管理者の承認後にログインできます。")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("このログインIDは既に使用されています。","error")
    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    month = get_month()
    # A distinct random token protects the destructive admin-only POST route.
    if session.get("role") == "admin" and "shift_delete_token" not in session:
        session["shift_delete_token"] = secrets.token_urlsafe(32)
    with db() as c:
        employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall() if session.get("role") == "admin" else []
        patterns = c.execute("SELECT * FROM shift_patterns WHERE active=1 ORDER BY start_time").fetchall() if session.get("role") == "admin" else []
        export_operators = c.execute("""SELECT e.id,e.name,COUNT(*) AS finalized_count
            FROM assignments a JOIN employees e ON e.id=a.employee_id
            WHERE substr(a.work_date,1,7)=? AND a.status='finalized'
            GROUP BY e.id,e.name ORDER BY e.name,e.id""", (month,)).fetchall() if session.get("role") == "admin" else []
        # Security boundary: never load another employee's assignments into
        # the operator's response. Ignore any employee_id provided in the URL.
        assignments = operator_shift_visibility.calendar_assignments(
            c, month, session.get("role"), session.get("employee_id")
        )
        if session.get("role") == "admin":
            requests = c.execute("""SELECT r.*,e.name FROM requests r JOIN employees e ON e.id=r.employee_id
              WHERE substr(r.request_date,1,7)=? ORDER BY r.request_date,e.name""", (month,)).fetchall()
        else:
            requests = c.execute("""SELECT r.*,e.name FROM requests r JOIN employees e ON e.id=r.employee_id
              WHERE substr(r.request_date,1,7)=? AND r.employee_id=? ORDER BY r.request_date,e.name""",
              (month, session.get("employee_id"))).fetchall()
    # Daily staffing previews contain names/assignments for the whole team.
    # Only managers may retrieve them; personal schedules need none.
    with db() as c:
        day_previews = operator_shift_visibility.admin_day_previews(c, month, session.get("role"))
    shift_counts = {"draft": 0, "finalized": 0}
    for a in assignments:
        if a["status"] in shift_counts:
            shift_counts[a["status"]] += 1
    by_day = {}
    for a in assignments:
        by_day.setdefault(a["work_date"], []).append(a)
    warnings = validate_month(month) if session.get("role") == "admin" else []
    with db() as c:
        first = c.execute("SELECT * FROM approvals WHERE work_month=? AND action='警告確認' ORDER BY id DESC LIMIT 1", (month,)).fetchone() if session.get("role") == "admin" else None
    active_count = len(employees)
    monthly_workday_summary=[]
    if session.get('role') == 'admin':
        by_employee={}
        for row in assignments:
            by_employee[row['employee_id']]=by_employee.get(row['employee_id'],0)+1
        for person in employees:
            actual=by_employee.get(person['id'],0)
            auto=monthly_workdays.automatic_monthly_days(month,int(person['weekly_max_days'] or 5))
            cap=person['monthly_max_days']
            target=monthly_workdays.monthly_target(month,int(person['weekly_max_days'] or 5),cap)
            monthly_workday_summary.append({'name':person['name'],'auto':auto,'cap':cap,
                'target':target,'actual':actual,'difference':actual-target,
                'over_max':cap is not None and actual>cap,
                'weekly_max':person['weekly_max_days']})
    readiness = shift_generation_readiness(employees, patterns) if session.get("role") == "admin" else None
    return render_template("index.html", month=month, employees=employees, patterns=patterns,
                           active_count=active_count, readiness=readiness,
                           by_day=by_day, days=month_days(month), requests=requests,
                           warnings=warnings, first=first, shift_counts=shift_counts,
                           day_previews=day_previews, export_operators=export_operators,
                           monthly_workday_summary=monthly_workday_summary,
                           shift_delete_token=session.get("shift_delete_token", ""))

@app.route("/employees", methods=["GET", "POST"])
@login_required
@admin_required
def employees_page():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            with db() as c:
                if action == "set_role":
                    uid=int(request.form["user_id"])
                    role=request.form.get("role")
                    if role not in ("admin","operator"): raise ValueError("権限が不正です。")
                    target=c.execute("SELECT id,role FROM users WHERE id=?",(uid,)).fetchone()
                    if not target: raise ValueError("アカウントが見つかりません。")
                    if target["role"]=="admin" and role=="operator" and c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND approved=1").fetchone()[0]<=1:
                        raise ValueError("最後の管理者は変更できません。")
                    c.execute("UPDATE users SET role=? WHERE id=?",(role,uid))
                    flash("権限を変更しました。")
                elif action == "approve":
                    uid=int(request.form["user_id"])
                    c.execute("UPDATE users SET approved=1 WHERE id=?",(uid,))
                    c.execute("UPDATE employees SET active=1 WHERE id=(SELECT employee_id FROM users WHERE id=?)",(uid,))
                    flash("登録申請を承認しました。")
                elif action == "reset_password":
                    eid = int(request.form["employee_id"])
                    target = c.execute("""SELECT u.id,u.username,e.source_user_id
                        FROM users u JOIN employees e ON e.id=u.employee_id
                        WHERE e.id=?""", (eid,)).fetchone()
                    if not target:
                        raise ValueError("従業員アカウントが見つかりません。")
                    new = request.form.get("new_password", "")
                    if not valid_password(new):
                        raise ValueError("パスワードは8〜16文字、大文字・小文字・数字・記号のうち3種類以上を含めてください。")
                    c.execute("UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?",
                              (generate_password_hash(new), target["id"]))
                    c.execute("""INSERT INTO password_reset_audit(user_id,username_at_reset,reset_by,reset_at)
                                 VALUES(?,?,?,?)""", (target["id"],target["username"],session["username"],datetime.now().isoformat(timespec="seconds")))
                    flash("パスワードを再設定しました。対象従業員は次回ログイン時に変更が必要です。")
                elif action in ("add", "edit"):
                    name = request.form.get("name", "").strip()
                    skills_list = [x for x in request.form.getlist("skills") if x in ("consumer", "merchant", "PP")]
                    allowed = request.form.getlist("allowed")
                    month_cap_raw = request.form.get("monthly_max_days", "").strip()
                    monthly_cap = int(month_cap_raw) if month_cap_raw else None
                    minimum = float(request.form.get("min_hours", 4))
                    maximum = float(request.form.get("max_hours", 9))
                    consecutive = int(request.form.get("max_consecutive", 5))
                    weekly_max = int(request.form.get("weekly_max_days", 5))
                    if not name or not skills_list:
                        raise ValueError("氏名と保有スキルを指定してください。")
                    if not (0 <= minimum <= maximum <= 24 and 1 <= consecutive <= 31 and 1 <= weekly_max <= 7
                            and (monthly_cap is None or 0 <= monthly_cap <= 31)):
                        raise ValueError("勤務条件の数値を確認してください。")
                    # legacy monthly_days retained for old integrations only.
                    auto_now=monthly_workdays.monthly_target(date.today().strftime('%Y-%m'), weekly_max, monthly_cap)
                    values = (name, ",".join(skills_list), auto_now, minimum, maximum, consecutive, weekly_max,
                              monthly_cap, int("早番" in allowed), int("中番" in allowed), int("遅番" in allowed))
                    username = request.form.get("username", "").strip()
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                        raise ValueError("ログインIDは半角英数字・_・.・- の3～32文字で指定してください。")
                    existing = c.execute("SELECT employee_id FROM users WHERE username=?", (username,)).fetchone()
                    if existing and (action == "add" or existing["employee_id"] != int(request.form["employee_id"])):
                        raise ValueError("このログインIDはすでに使用されています。")
                    if action == "add":
                        cur = c.execute("""INSERT INTO employees
                            (name,skills,monthly_days,min_hours,max_hours,max_consecutive,weekly_max_days,monthly_max_days,early_ok,mid_ok,late_ok)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", values)
                        eid = cur.lastrowid
                        c.execute("INSERT INTO users(username,password_hash,role,employee_id) VALUES(?,?,?,?)",
                                  (username, generate_password_hash("ChangeMe123!"), "operator", eid))
                        c.execute("UPDATE users SET must_change_password=1 WHERE employee_id=?", (eid,))
                        flash(f"{name}さんを登録しました。ログインID: {username}（初期パスワードは管理者が再設定してください）")
                    else:
                        eid = int(request.form["employee_id"])
                        old = c.execute("SELECT id,source_user_id FROM employees WHERE id=?", (eid,)).fetchone()
                        if old is None:
                            raise ValueError("対象の従業員が見つかりません。")
                        user_row = c.execute("SELECT id,username FROM users WHERE employee_id=?", (eid,)).fetchone()
                        if user_row is None:
                            raise ValueError("対象従業員のログインアカウントが見つかりません。")
                        c.execute("""UPDATE employees SET name=?,skills=?,monthly_days=?,min_hours=?,max_hours=?,
                                     max_consecutive=?,weekly_max_days=?,monthly_max_days=?,early_ok=?,mid_ok=?,late_ok=? WHERE id=?""", values + (eid,))
                        c.execute("UPDATE users SET username=? WHERE id=?", (username, user_row["id"]))
                        flash("従業員情報とログインIDを更新しました。" +
                              (" ※連携元の氏名・スキルは次回同期で上書きされます。" if old["source_user_id"] is not None else ""))
                elif action in ("delete", "restore"):
                    eid = int(request.form["employee_id"])
                    row = c.execute("SELECT id,source_user_id FROM employees WHERE id=?", (eid,)).fetchone()
                    if row is None:
                        raise ValueError("対象の従業員が見つかりません。")
                    # Re-activation is an explicit administrator approval as well.
                    # Imported and self-registered accounts can have approved=0;
                    # merely setting employees.active=1 leaves those accounts unable to log in.
                    account = c.execute("SELECT id,approved FROM users WHERE employee_id=?", (eid,)).fetchone()
                    if account is None:
                        raise ValueError("ログインアカウントがありません。従業員の登録情報を確認してください。")
                    if action == "restore":
                        c.execute("UPDATE employees SET active=1 WHERE id=?", (eid,))
                        c.execute("UPDATE users SET approved=1 WHERE employee_id=?", (eid,))
                        flash("従業員を再有効化しました。ログイン承認も有効にしました。")
                    else:
                        c.execute("UPDATE employees SET active=0 WHERE id=?", (eid,))
                        flash("従業員を無効化しました。シフト・希望休・ログイン情報は保持されます。")
                else:
                    raise ValueError("不明な操作です。")
        except (ValueError, KeyError, sqlite3.Error) as exc:
            flash(f"操作できませんでした: {exc}", "error")
        return redirect(url_for("employees_page"))
    with db() as c:
        rows = c.execute("""SELECT e.*, (SELECT u.username FROM users u WHERE u.employee_id=e.id ORDER BY u.id LIMIT 1) AS login_username
                            FROM employees e ORDER BY e.active DESC,e.name""").fetchall()
        accounts=c.execute("SELECT u.id,u.username,u.role,u.approved,e.name FROM users u LEFT JOIN employees e ON e.id=u.employee_id ORDER BY u.approved,u.username").fetchall()
    selected_month=get_month()
    enhanced=[]
    for employee in rows:
        item=dict(employee)
        item['auto_monthly_days']=monthly_workdays.automatic_monthly_days(selected_month,int(item['weekly_max_days'] or 5))
        item['effective_monthly_days']=monthly_workdays.monthly_target(selected_month,int(item['weekly_max_days'] or 5),item['monthly_max_days'])
        enhanced.append(item)
    return render_template("employees.html", employees=enhanced, accounts=accounts, selected_month=selected_month)


IMPORT_COLUMNS = ["ログインID", "氏名", "スキル", "最小勤務時間", "最大勤務時間", "最大連勤", "週最大勤務日数", "月間最大勤務日数", "早番", "中番", "遅番"]

def parse_import_file(upload):
    filename = (upload.filename or "").lower()
    data = upload.stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("ファイルは2MB以下にしてください。")
    if filename.endswith(".csv"):
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = data.decode("cp932")
        records = list(csv.reader(io.StringIO(content)))
    elif filename.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            records = list(wb.active.iter_rows(values_only=True, max_row=502))
        finally:
            wb.close()
    else:
        raise ValueError(".xlsx または .csv ファイルを選択してください。")
    if not records:
        raise ValueError("ファイルが空です。")
    header = [str(v).strip() if v is not None else "" for v in records[0]]
    required = ["ログインID", "氏名", "スキル"]
    if any(h not in header for h in required):
        raise ValueError("必須列がありません: " + ", ".join(required))
    if len(records) > 501:
        raise ValueError("一度に取り込めるのは500名までです。")
    results = []
    for lineno, record in enumerate(records[1:], 2):
        if not any(v is not None and str(v).strip() for v in record):
            continue
        raw = {h: record[i] if i < len(record) else None for i,h in enumerate(header) if h}
        try:
            username = str(raw.get("ログインID") or "").strip()
            name = str(raw.get("氏名") or "").strip()
            skills = [x.strip() for x in re.split(r"[,、，/]+", str(raw.get("スキル") or "")) if x.strip()]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                raise ValueError("ログインIDは半角英数字・_.- の3〜32文字")
            if not name or len(name) > 100:
                raise ValueError("氏名は1〜100文字")
            if not skills or any(x not in ("consumer", "merchant", "PP") for x in skills):
                raise ValueError("スキルはconsumer,merchant,PPのいずれか")
            def number(field, default, typ):
                value = raw.get(field)
                return typ(value) if value is not None and str(value).strip() else default
            # The former 月間勤務日数 column is accepted but ignored.
            # Monthly targets are always derived from the current month and weekly cap.
            cap_supplied = "月間最大勤務日数" in header
            monthly_cap = number("月間最大勤務日数", None, int)
            min_h = number("最小勤務時間", 4, float)
            max_h = number("最大勤務時間", 9, float)
            consecutive = number("最大連勤", 5, int)
            weekly_max = number("週最大勤務日数", 5, int)
            if not (0 <= min_h <= max_h <= 24 and 1 <= consecutive <= 31 and 1 <= weekly_max <= 7
                    and (monthly_cap is None or 0 <= monthly_cap <= 31)):
                raise ValueError("勤務条件の範囲が不正")
            def flag(field):
                value = str(raw.get(field) if raw.get(field) is not None else "1").strip().lower()
                if value in ("1", "yes", "true", "可", "○", "〇", "はい"): return 1
                if value in ("0", "no", "false", "不可", "×", "いいえ"): return 0
                raise ValueError(field + "は1/0または可/不可")
            values = [username, name, ",".join(dict.fromkeys(skills)), min_h, max_h, consecutive, weekly_max,
                      monthly_cap, cap_supplied, flag("早番"), flag("中番"), flag("遅番")]
            results.append({"line":lineno,"values":values,"error":""})
        except (ValueError, TypeError) as exc:
            results.append({"line":lineno,"values":[],"error":str(exc)})
    if not results:
        raise ValueError("従業員データがありません。")
    seen=set()
    for item in results:
        if item["values"]:
            key=item["values"][0]
            if key in seen: item["error"]="ファイル内でログインIDが重複"
            seen.add(key)
    return results

@app.route("/employees/import", methods=["GET", "POST"])
@login_required
@admin_required
def employees_import_page():
    rows=[]; token=None; mode="new"
    signer=URLSafeTimedSerializer(app.secret_key, salt="employee-import-v1")
    if request.method == "POST":
        mode=request.form.get("mode", "new")
        if mode not in ("new", "update"): mode="new"
        if request.form.get("action")=="preview":
            try:
                rows=parse_import_file(request.files["file"])
                with db() as c:
                    existing={r["username"]:r for r in c.execute("SELECT username,employee_id FROM users")}
                for row in rows:
                    if row["error"]: continue
                    username=row["values"][0]
                    user=existing.get(username)
                    row["status"]="更新" if user else "新規"
                    if user and user["employee_id"] is None: row["error"]="管理者ID等は更新不可"
                    elif user and mode=="new": row["error"]="登録済みID（新規登録モード）"
                    elif not user and mode=="update": row["error"]="未登録ID（更新モード）"
                token=signer.dumps({"rows":rows,"mode":mode})
            except Exception as exc:
                flash("ファイルを読み込めませんでした: " + str(exc), "error")
        elif request.form.get("action")=="commit":
            try:
                payload=signer.loads(request.form.get("token", ""), max_age=900)
                rows=payload["rows"]; mode=payload["mode"]
                if len(rows)>500 or mode not in ("new", "update") or any(r["error"] for r in rows):
                    raise ValueError("エラー行があります。ファイルを修正して再度プレビューしてください。")
                with db() as c:
                    for row in rows:
                        username,name,skills,min_h,max_h,consecutive,weekly_max,monthly_cap,cap_supplied,early,mid,late=row["values"]
                        days=monthly_workdays.monthly_target(date.today().strftime('%Y-%m'),weekly_max,monthly_cap)
                        existing=c.execute("SELECT employee_id FROM users WHERE username=?",(username,)).fetchone()
                        if mode=="new":
                            if existing: raise ValueError(username+" は既に登録されています。")
                            cur=c.execute("""INSERT INTO employees(name,skills,monthly_days,min_hours,max_hours,max_consecutive,weekly_max_days,monthly_max_days,early_ok,mid_ok,late_ok)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(name,skills,days,min_h,max_h,consecutive,weekly_max,monthly_cap,early,mid,late))
                            c.execute("INSERT INTO users(username,password_hash,role,employee_id,must_change_password) VALUES(?,?,?,?,1)",
                                      (username,generate_password_hash("ChangeMe123!"),"operator",cur.lastrowid))
                        else:
                            if not existing or existing["employee_id"] is None: raise ValueError(username+" は更新できません。")
                            # Preserve a manually set monthly cap when the input file
                            # lacks the new optional column altogether.
                            if cap_supplied:
                                c.execute("""UPDATE employees SET name=?,skills=?,monthly_days=?,min_hours=?,max_hours=?,max_consecutive=?,weekly_max_days=?,monthly_max_days=?,
                                  early_ok=?,mid_ok=?,late_ok=? WHERE id=?""",
                                  (name,skills,days,min_h,max_h,consecutive,weekly_max,monthly_cap,early,mid,late,existing["employee_id"]))
                            else:
                                current=c.execute("SELECT monthly_max_days FROM employees WHERE id=?",(existing["employee_id"],)).fetchone()
                                saved_cap=current['monthly_max_days'] if current else None
                                days=monthly_workdays.monthly_target(date.today().strftime('%Y-%m'),weekly_max,saved_cap)
                                c.execute("""UPDATE employees SET name=?,skills=?,monthly_days=?,min_hours=?,max_hours=?,max_consecutive=?,weekly_max_days=?,
                                  early_ok=?,mid_ok=?,late_ok=? WHERE id=?""",
                                  (name,skills,days,min_h,max_h,consecutive,weekly_max,early,mid,late,existing["employee_id"]))
                flash(f"{len(rows)}件の{'新規登録' if mode=='new' else '更新'}が完了しました。")
                return redirect(url_for("employees_page"))
            except (BadSignature, SignatureExpired):
                flash("確認データの期限が切れました。再度ファイルを読み込んでください。", "error")
            except (ValueError, sqlite3.Error, KeyError, TypeError) as exc:
                flash("取り込みを中止しました（データは変更されていません）: " + str(exc), "error")
    return render_template("employees_import.html", rows=rows, token=token, mode=mode)

@app.route("/employees/import/template.csv")
@login_required
@admin_required
def employees_import_template():
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(IMPORT_COLUMNS)
    writer.writerow(["tanaka01","田中太郎","consumer,PP",4,9,5,5,"",1,1,1])
    content=io.BytesIO(("\ufeff"+out.getvalue()).encode("utf-8"))
    return send_file(content, mimetype="text/csv", as_attachment=True, download_name="employees_template.csv")

@app.route("/patterns", methods=["GET", "POST"])
@login_required
@admin_required
def patterns_page():
    if request.method == "POST":
        try:
            action = request.form.get("action")
            if action not in ("add", "update", "delete"):
                raise ValueError("操作が不正です。")
            with db() as c:
                if action == "delete":
                    pid = int(request.form["pattern_id"])
                    if not c.execute("SELECT id FROM shift_patterns WHERE id=?", (pid,)).fetchone():
                        raise ValueError("勤務パターンが見つかりません。")
                    c.execute("UPDATE shift_patterns SET active=0 WHERE id=?", (pid,))
                    flash("勤務パターンを無効化しました。過去のシフトは保持されます。")
                else:
                    name = request.form.get("name", "").strip()
                    start_time = request.form.get("start_time", "")
                    end_time = request.form.get("end_time", "")
                    minutes = int(request.form.get("break_minutes", "60"))
                    shift_type = request.form.get("shift_type", "auto").strip()
                    if shift_type not in ("auto", "早番", "中番", "遅番", "共通"):
                        raise ValueError("勤務区分は早番・中番・遅番・共通・名称から判定のいずれかを選んでください。")
                    if not name or len(name)>100 or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", end_time):
                        raise ValueError("名称と開始・終了時刻を確認してください。")
                    if mins(end_time) <= mins(start_time) or not (0 <= minutes < mins(end_time)-mins(start_time)):
                        raise ValueError("終了時刻は開始時刻より後にし、休憩時間は勤務時間未満にしてください。")
                    if action == "add":
                        c.execute("INSERT INTO shift_patterns(name,start_time,end_time,break_minutes,shift_type) VALUES(?,?,?,?,?)", (name,start_time,end_time,minutes,shift_type))
                        flash("勤務パターンを追加しました。")
                    else:
                        pid=int(request.form["pattern_id"])
                        active=int(request.form.get("active", "1"))
                        if active not in (0,1): raise ValueError("状態が不正です。")
                        if not c.execute("SELECT id FROM shift_patterns WHERE id=?",(pid,)).fetchone():
                            raise ValueError("勤務パターンが見つかりません。")
                        c.execute("UPDATE shift_patterns SET name=?,start_time=?,end_time=?,break_minutes=?,shift_type=?,active=? WHERE id=?", (name,start_time,end_time,minutes,shift_type,active,pid))
                        flash("勤務パターンの変更を保存しました。")
        except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
            msg = "同じ名称の勤務パターンが既にあります。" if isinstance(exc,sqlite3.IntegrityError) else str(exc)
            flash(msg, "error")
        return redirect(url_for("patterns_page"))
    with db() as c:
        patterns = c.execute("SELECT * FROM shift_patterns ORDER BY active DESC,start_time").fetchall()
    return render_template("patterns.html", patterns=patterns)

@app.route("/coverage", methods=["GET","POST"])
@login_required
@admin_required
def coverage_page():
    if request.method == "POST":
        with db() as c:
            action = request.form.get("action")
            if action == "add":
                c.execute("INSERT INTO coverage(weekday,start_time,end_time,skill,min_count) VALUES(?,?,?,?,?)",
                          (int(request.form["weekday"]), request.form["start_time"], request.form["end_time"],
                           request.form["skill"], int(request.form["min_count"])))
                flash("必要人数ルールを追加しました。")
            elif action == "delete":
                c.execute("DELETE FROM coverage WHERE id=?", (int(request.form["coverage_id"]),))
                flash("ルールを削除しました。")
    with db() as c:
        rows = c.execute("SELECT * FROM coverage ORDER BY weekday,start_time,skill").fetchall()
    return render_template("coverage.html", rows=rows, weekdays=["月","火","水","木","金","土","日"])

def sync_operator_records(path, direction):
    """Synchronize operator profiles in one selected direction, never overwrite passwords.

    The source of truth is selected explicitly on every run. Existing usernames
    are matched only when not already linked to a different employee.
    """
    if direction not in ("break_to_shift", "shift_to_break"):
        raise ValueError("同期方向が正しくありません。")
    if os.path.realpath(path) == os.path.realpath(DB_PATH):
        raise ValueError("異なるデータベースを指定してください。")
    # Attach the other DB so both profile tables are modified in one connection.
    # Existing SQLite WAL databases are supported; password changes are excluded.
    with db() as c:
        c.execute("ATTACH DATABASE ? AS breakdb", (path,))
        try:
            cols = {r[1] for r in c.execute("PRAGMA breakdb.table_info(users)")}
            required = {"id", "username", "display_name", "role", "active", "skills", "shift_start", "shift_end", "salt", "password_hash"}
            if not required.issubset(cols):
                raise ValueError("休憩管理アプリのDB形式が一致しません。")
            c.execute("SELECT 1 FROM breakdb.users LIMIT 1")
            stats = {"created": 0, "updated": 0, "skipped": 0, "credentials": []}
            if direction == "break_to_shift":
                source = c.execute("SELECT * FROM breakdb.users WHERE role='operator' ORDER BY id").fetchall()
                for su in source:
                    username = su["username"]
                    matched = c.execute("SELECT * FROM employees WHERE source_user_id=?", (su["id"],)).fetchone()
                    account = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                    if account and account["role"] != "operator":
                        raise ValueError(f"管理者IDとの衝突: {username}")
                    if account and matched and account["employee_id"] not in (None, matched["id"]):
                        raise ValueError(f"IDの紐付けが競合: {username}")
                    if account and not matched and account["employee_id"]:
                        matched = c.execute("SELECT * FROM employees WHERE id=?", (account["employee_id"],)).fetchone()
                        if matched and matched["source_user_id"] not in (None, su["id"]):
                            raise ValueError(f"別のアカウントに紐付け済み: {username}")
                    skills = json.loads(su["skills"] or "[]")
                    mapped = [x for x in (dict(Consumer="consumer", Merchant="merchant", PP="PP").get(sk) for sk in skills) if x]
                    mapped = list(dict.fromkeys(mapped)) or ["consumer"]
                    if matched:
                        eid = matched["id"]
                        c.execute("UPDATE employees SET name=?,active=?,skills=?,source_user_id=?,source_username=? WHERE id=?",
                                  (su["display_name"], su["active"], ",".join(mapped), su["id"], username, eid))
                        stats["updated"] += 1
                    else:
                        cur = c.execute("INSERT INTO employees(name,active,skills,source_user_id,source_username) VALUES(?,?,?,?,?)",
                                        (su["display_name"], su["active"], ",".join(mapped), su["id"], username))
                        eid = cur.lastrowid
                        stats["created"] += 1
                    if account:
                        c.execute("UPDATE users SET employee_id=? WHERE id=?", (eid, account["id"]))
                    else:
                        c.execute("INSERT INTO users(username,password_hash,role,employee_id) VALUES(?,?, 'operator', ?)",
                                  (username, generate_password_hash(secrets.token_urlsafe(20)), eid))
            else:
                source = c.execute("""SELECT e.*, u.username AS login_username FROM employees e
                    JOIN users u ON u.employee_id=e.id AND u.role='operator' ORDER BY e.id""").fetchall()
                for emp in source:
                    username = emp["login_username"]
                    by_link = (c.execute("SELECT * FROM breakdb.users WHERE id=? AND role='operator'", (emp["source_user_id"],)).fetchone()
                               if emp["source_user_id"] is not None else None)
                    by_name = c.execute("SELECT * FROM breakdb.users WHERE username=?", (username,)).fetchone()
                    if by_name and by_name["role"] != "operator":
                        raise ValueError(f"休憩アプリ管理者IDとの衝突: {username}")
                    if by_link and by_name and by_link["id"] != by_name["id"]:
                        raise ValueError(f"リンク先とログインIDが競合: {username}")
                    target = by_link or by_name
                    skills = [dict(consumer="Consumer", merchant="Merchant", PP="PP").get(sk.strip())
                              for sk in (emp["skills"] or "").split(",")]
                    skills = list(dict.fromkeys(x for x in skills if x)) or ["Consumer"]
                    if target:
                        c.execute("""UPDATE breakdb.users SET username=?,display_name=?,active=?,skills=? WHERE id=?""",
                                  (username, emp["name"], emp["active"], json.dumps(skills), target["id"]))
                        uid = target["id"]
                        stats["updated"] += 1
                    else:
                        # New accounts receive a random temporary password. No existing password is overwritten.
                        password = "T" + secrets.token_urlsafe(9)[:11] + "7!"
                        salt = secrets.token_hex(16)
                        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240000).hex()
                        cur = c.execute("""INSERT INTO breakdb.users
                          (username,display_name,role,salt,password_hash,active,skills,shift_start,shift_end)
                          VALUES(?,?,'operator',?,?,?,?,?,?)""",
                          (username,emp["name"],salt,digest,emp["active"],json.dumps(skills),"09:00","18:00"))
                        uid = cur.lastrowid
                        stats["credentials"].append((username, password))
                        stats["created"] += 1
                    c.execute("UPDATE employees SET source_user_id=?,source_username=? WHERE id=?", (uid, username, emp["id"]))
            c.execute("INSERT INTO settings(key,value) VALUES('callcenter_db_path',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (path,))
            return stats
        finally:
            # ATTACH is closed when the context-managed connection is garbage-collected.
            pass


@app.route("/integration", methods=["GET", "POST"])
@login_required
@admin_required
def integration_page():
    path = _callcenter_db_path()
    credentials = []
    if request.method == "POST":
        raw_path = request.form.get("db_path", "").strip()
        path = os.path.abspath(os.path.expandvars(raw_path)) if raw_path else ""
        if not os.path.isfile(path):
            flash("休憩管理アプリのSQLiteファイルが見つかりません。", "error")
        else:
            try:
                action = request.form.get("action")
                if action == "save_path":
                    with db() as c:
                        c.execute("INSERT INTO settings(key,value) VALUES('callcenter_db_path',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (path,))
                    flash("接続先を保存しました。")
                elif action in ("break_to_shift", "shift_to_break"):
                    result = sync_operator_records(path, action)
                    credentials = result["credentials"]
                    flash(f"同期完了：新規 {result['created']}名・更新 {result['updated']}名。既存のパスワードは変更していません。")
                else:
                    flash("同期方向を選択してください。", "error")
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
                flash(f"同期に失敗しました: {exc}", "error")
    preview_count = None
    realtime_status = realtime_sync.status()
    if path and os.path.isfile(path):
        try:
            with sqlite3.connect(path) as src:
                preview_count = src.execute("SELECT COUNT(*) FROM users WHERE role='operator'").fetchone()[0]
        except sqlite3.Error:
            pass
    return render_template("integration.html", db_path=path, preview_count=preview_count, credentials=credentials, realtime_status=realtime_status)

@app.route("/requests", methods=["GET","POST"])
@login_required
def requests_page():
    if session.get("role") != "admin":
        return redirect(url_for("my_requests_page"))
    if request.method == "POST":
        action = request.form.get("action", "add")
        with db() as c:
            if action == "status":
                status = request.form.get("status", "pending")
                if status in ("pending", "approved", "rejected"):
                    c.execute("UPDATE requests SET status=? WHERE id=?", (status, int(request.form["request_id"])))
                    flash("希望休の審査状態を更新しました。")
            else:
                c.execute("INSERT INTO requests(employee_id,request_date,request_type,status,note) VALUES(?,?,?,?,?)",
                          (int(request.form["employee_id"]), request.form["request_date"], request.form["request_type"],
                           request.form.get("status","pending"), request.form.get("note","").strip()))
                flash("休暇・休み希望を登録しました。")
        return redirect(url_for("requests_page"))
    with db() as c:
        emps = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
        rows = c.execute("""SELECT r.*,e.name FROM requests r JOIN employees e ON e.id=r.employee_id
                            ORDER BY request_date DESC,e.name LIMIT 500""").fetchall()
    return render_template("requests.html", employees=emps, rows=rows)


FULL_DAY_LEAVE_TYPES = ("有給休暇", "確定休", "通常の休み希望", "できれば休み", "申請中")
HALF_DAY_LEAVE_TYPES = ("午前休", "午後休")
OPERATOR_REQUEST_TYPES = FULL_DAY_LEAVE_TYPES[:4] + HALF_DAY_LEAVE_TYPES
HALF_DAY_SPLIT = "13:00"  # 午前休は13:00以降、午後休は13:00まで勤務


def _parse_selected_leave_dates(values):
    """Strict multi-date validation; rejects duplicate and malformed dates."""
    if not values or len(values) > 93:
        raise ValueError("希望日は1～93日を選択してください。")
    unique = set()
    for value in values:
        try:
            day = date.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValueError("日付の形式が不正です。") from None
        if day.isoformat() != value or value in unique:
            raise ValueError("希望日に重複または不正な値があります。")
        unique.add(value)
    return sorted(unique)


def _leave_day_shortfalls(monthly_days, existing_requests, selected_dates, request_type, weekly_max_days=None):
    """Conservative check: every pending/approved full-day wish is treated as off.

    Half days count as one working day for the monthly DAYS test. This does not
    imply that a specific working pattern can actually be scheduled.
    """
    months = {d[:7] for d in selected_dates}
    unavailable = {month: set() for month in months}
    for r in existing_requests:
        if r["status"] != "rejected" and r["request_type"] in FULL_DAY_LEAVE_TYPES:
            month = r["request_date"][:7]
            if month in unavailable:
                unavailable[month].add(r["request_date"])
    # Accept the old single-category form as well as per-day categories from
    # the inline calendar menu. Half-day requests still count as work-capable days.
    for day in selected_dates:
        kind = request_type.get(day) if isinstance(request_type, dict) else request_type
        if kind in FULL_DAY_LEAVE_TYPES:
            unavailable[day[:7]].add(day)
    problems = []
    for month in sorted(months):
        year, number = map(int, month.split("-"))
        possible = calendar.monthrange(year, number)[1] - len(unavailable[month])
        if weekly_max_days is not None:
            # A calendar month may include 5 or 6 partial Sunday–Saturday weeks.
            # Count the remaining work-capable days per week, subject to the weekly cap.
            workweeks = {}
            for day in month_days(month):
                if day.isoformat() in unavailable[month]:
                    continue
                sunday = day - timedelta(days=(day.weekday()+1)%7)
                workweeks[sunday] = workweeks.get(sunday, 0) + 1
            possible = sum(min(n, int(weekly_max_days)) for n in workweeks.values())
        target = monthly_days.get(month, 0) if isinstance(monthly_days, dict) else int(monthly_days or 0)
        if possible < target:
            problems.append({"month": month, "possible": possible, "target": target})
    return problems


@app.route("/my-requests", methods=["GET", "POST"])
@login_required
def my_requests_page():
    if session.get("role") == "admin":
        return redirect(url_for("requests_page"))
    employee_id = session.get("employee_id")
    if not employee_id:
        flash("このアカウントに従業員情報が紐づいていません。管理者に連携設定を依頼してください。", "error")
        return redirect(url_for("index"))
    # Preserve the user's calendar choices if the server rejects a submission.
    draft_selections = {}
    draft_note = ""
    draft_reason = ""
    if request.method == "POST":
        draft_note = request.form.get("note", "")[:500]
        draft_reason = request.form.get("shortfall_reason", "")[:500]
        raw_dates = request.form.getlist("request_dates")
        raw_types = request.form.getlist("request_types")
        if len(raw_dates) == len(raw_types):
            draft_selections = {d: t for d, t in zip(raw_dates, raw_types)
                                if t in OPERATOR_REQUEST_TYPES and len(d) == 10}
        try:
            submitted_dates = request.form.getlist("request_dates")
            dates = _parse_selected_leave_dates(submitted_dates)
            submitted_types = request.form.getlist("request_types")
            if submitted_types:
                # There must be exactly one category for *every* date; partial
                # mappings must not silently fall back to the global category.
                if len(submitted_dates) != len(submitted_types):
                    raise ValueError("希望日ごとの希望区分が不足しています。")
                kinds_by_date = dict(zip(submitted_dates, (kind.strip() for kind in submitted_types)))
            else:
                # Keep backward compatibility for clients of the previous form.
                kind = request.form.get("request_type", "").strip()
                kinds_by_date = {day: kind for day in dates}
            if any(kind not in OPERATOR_REQUEST_TYPES for kind in kinds_by_date.values()):
                raise ValueError("希望区分を確認してください。")
            note = request.form.get("note", "").strip()
            reason = request.form.get("shortfall_reason", "").strip()
            if len(note) > 500 or len(reason) > 500:
                raise ValueError("備考・理由は500文字以下で入力してください。")
            with db() as conn:
                employee = conn.execute("SELECT id,monthly_days,weekly_max_days,monthly_max_days FROM employees WHERE id=? AND active=1",
                                        (employee_id,)).fetchone()
                if not employee:
                    raise ValueError("従業員が無効、または登録されていません。")
                months = sorted({d[:7] for d in dates})
                existing = conn.execute(
                    "SELECT request_date,request_type,status FROM requests WHERE employee_id=? "
                    "AND substr(request_date,1,7) IN (" + ",".join("?" for _ in months) + ")",
                    (employee_id, *months)).fetchall()
                # Reject reapplication for an already pending/approved day.
                # Rejected records stay in history and may be resubmitted.
                conflicts = {r["request_date"] for r in existing if r["status"] != "rejected"}
                duplicates = [d for d in dates if d in conflicts]
                if duplicates:
                    raise ValueError("既に申請中・承認済みの日付があります: " + "、".join(duplicates[:8]))
                targets={m:monthly_workdays.monthly_target(m,int(employee['weekly_max_days'] or 5),employee['monthly_max_days']) for m in months}
                shortfalls = _leave_day_shortfalls(targets, existing, dates, kinds_by_date, employee["weekly_max_days"])
                if shortfalls and not reason:
                    details = "、".join(f"{r['month']}（最大{r['possible']}日／登録{r['target']}日）" for r in shortfalls)
                    raise ValueError("登録上の月間勤務日数を下回る可能性があります。理由を入力してください: " + details)
                if shortfalls:
                    reason_label = "【月間勤務日数不足の理由】" + reason
                    stored_note = (note + "\n" if note else "") + reason_label
                else:
                    stored_note = note
                conn.executemany(
                    "INSERT INTO requests(employee_id,request_date,request_type,status,note) "
                    "VALUES(?,?,?,'pending',?)",
                    [(employee_id, day, kinds_by_date[day], stored_note) for day in dates])
            flash(f"{len(dates)}日分の希望をまとめて提出しました。管理者の確認をお待ちください。")
            if shortfalls:
                flash("月間勤務日数の不足理由も管理者へ提出しました。")
            return redirect(url_for("my_requests_page"))
        except (ValueError, sqlite3.Error) as exc:
            flash(f"希望休を提出できませんでした: {exc}", "error")
    with db() as conn:
        rows = conn.execute("SELECT * FROM requests WHERE employee_id=? "
                            "ORDER BY request_date DESC,id DESC LIMIT 250", (employee_id,)).fetchall()
        employee = conn.execute("SELECT name,monthly_days,weekly_max_days,monthly_max_days FROM employees WHERE id=?", (employee_id,)).fetchone()
        # Calendar occupancy covers submitted history beyond the table limit.
        active = conn.execute("SELECT request_date,request_type,status FROM requests WHERE employee_id=? "
                              "AND status IN ('pending','approved')", (employee_id,)).fetchall()
    today = date.today()
    return render_template("my_requests.html", rows=rows,
                           employee_name=employee["name"] if employee else session["username"],
                           monthly_days=monthly_workdays.monthly_target(today.strftime('%Y-%m'),int(employee['weekly_max_days'] or 5),employee['monthly_max_days']) if employee else 0,
                           weekly_max_days=int(employee['weekly_max_days'] or 5) if employee else 5,
                           monthly_max_days=employee['monthly_max_days'] if employee else None,
                           active_requests=[dict(r) for r in active], current_month=today.strftime("%Y-%m"),
                           draft_selections=draft_selections, draft_note=draft_note,
                           draft_reason=draft_reason)

@app.route("/requests/export", methods=["GET"])
@login_required
@admin_required
def export_requests_csv():
    """Export requests to an Excel-friendly UTF-8 BOM CSV, with optional month/status filters."""
    month = request.args.get("month", "").strip()
    status = request.args.get("status", "all").strip()
    if month:
        try:
            datetime.strptime(month, "%Y-%m")
            if len(month) != 7:
                raise ValueError()
        except ValueError:
            flash("対象月の形式を確認してください。", "error")
            return redirect(url_for("import_page"))
    if status not in ("all", "pending", "approved", "rejected"):
        flash("申請状態が不正です。", "error")
        return redirect(url_for("import_page"))
    sql = """SELECT r.request_date, e.name, r.request_type, r.status, r.note,
                    u.username AS login_id
             FROM requests r
             JOIN employees e ON e.id = r.employee_id
             LEFT JOIN users u ON u.employee_id = e.id AND u.role = 'operator'
             WHERE 1=1"""
    params = []
    if month:
        sql += " AND substr(r.request_date,1,7)=?"
        params.append(month)
    if status != "all":
        sql += " AND r.status=?"
        params.append(status)
    sql += " ORDER BY r.request_date,e.name,r.id"
    with db() as c:
        records = c.execute(sql, params).fetchall()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["従業員名", "日付", "休暇区分", "状態", "備考", "ログインID"])
    def safe_cell(value):
        value = "" if value is None else str(value)
        # Prevent spreadsheet formula injection in user-entered cells.
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
        return value
    for r in records:
        writer.writerow([safe_cell(r["name"]), r["request_date"], safe_cell(r["request_type"]),
                         r["status"], safe_cell(r["note"]), safe_cell(r["login_id"])])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    filename = f"leave_requests_{month or 'all'}.csv"
    return send_file(data, mimetype="text/csv; charset=utf-8", as_attachment=True,
                     download_name=filename)


@app.route("/import", methods=["GET","POST"])
@login_required
@admin_required
def import_page():
    preview = None
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("CSVファイルを選択してください。", "error")
        else:
            try:
                raw = file.stream.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(raw))
                headers = reader.fieldnames or []
                rows = list(reader)
                preview = {"headers": headers, "rows": rows[:20], "count": len(rows)}
                session["import_rows"] = rows[:5000]
                session["import_headers"] = headers
            except Exception as e:
                flash("CSVを読み込めませんでした。UTF-8またはExcelでCSV UTF-8保存をお試しください。", "error")
    if request.method == "POST" and request.form.get("import_action") == "apply":
        rows = session.get("import_rows", [])
        name_col, date_col, type_col = request.form.get("name_col"), request.form.get("date_col"), request.form.get("type_col")
        status_col = request.form.get("status_col")
        added, errors = 0, []
        with db() as c:
            emp_map = {r["name"]:r["id"] for r in c.execute("SELECT id,name FROM employees WHERE active=1")}
            for i, row in enumerate(rows, start=2):
                name = (row.get(name_col) or "").strip()
                dt = (row.get(date_col) or "").strip()
                typ = (row.get(type_col) or "通常の休み希望").strip()
                stat = (row.get(status_col) or "pending").strip() if status_col else "pending"
                # Accept ISO dates or Japanese slash dates.
                parsed = None
                for fmt in ("%Y-%m-%d","%Y/%m/%d","%Y年%m月%d日"):
                    try:
                        parsed = datetime.strptime(dt, fmt).date().isoformat(); break
                    except ValueError:
                        pass
                if name not in emp_map:
                    errors.append(f"{i}行目: 未登録の従業員「{name}」")
                elif not parsed:
                    errors.append(f"{i}行目: 日付形式を確認してください（{dt}）")
                else:
                    allowed_types = ["有給休暇","確定休","通常の休み希望","できれば休み","午前休","午後休","申請中"]
                    if typ not in allowed_types: typ = "通常の休み希望"
                    if stat not in ["pending","approved","rejected"]: stat = "pending"
                    c.execute("INSERT INTO requests(employee_id,request_date,request_type,status,note) VALUES(?,?,?,?,?)",
                              (emp_map[name], parsed, typ, stat, "CSV取込"))
                    added += 1
        flash(f"{added}件を取り込みました。エラー {len(errors)}件。")
        if errors:
            session["import_errors"] = errors[:200]
        return redirect(url_for("import_page"))
    errors = session.pop("import_errors", [])
    with db() as c:
        employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
    return render_template("import.html", preview=preview, employees=employees, errors=errors,
                           headers=session.get("import_headers", []))

def shift_generation_readiness(employees, patterns):
    """Explain employee/pattern compatibility before auto-generation.

    This is a diagnostics-only preflight; it does not modify the database.
    """
    summary = {"active_employees": len(employees), "active_patterns": len(patterns),
               "compatible_employees": 0, "reasons": [], "employees": []}
    if not employees:
        summary["reasons"].append("有効なオペレーターが0名です。従業員管理の有効状態を確認してください。")
    if not patterns:
        summary["reasons"].append("有効な勤務パターンが0件です。勤務パターン画面の状態を確認してください。")
    for e in employees:
        reasons = []
        # Modern records use a dynamic monthly target. The fallback keeps
        # diagnostics compatible with imported legacy dictionaries/tests.
        keys = e.keys() if hasattr(e, 'keys') else []
        if 'weekly_max_days' in keys:
            monthly_target_now=monthly_workdays.monthly_target(
                date.today().strftime('%Y-%m'),int(e['weekly_max_days'] or 5),
                e['monthly_max_days'] if 'monthly_max_days' in keys else None)
        else:
            monthly_target_now=int(e['monthly_days'] or 0)
        if monthly_target_now <= 0:
            reasons.append("当月の基準勤務日数が0日（月間最大勤務日数の設定を確認）")
        if not any(x.strip() for x in (e["skills"] or "").split(",")):
            reasons.append("担当可能なスキルが未設定")
        matched = []
        blocked_by_shift = 0
        blocked_by_hours = 0
        for p in patterns:
            if not _pattern_shift_allowed(e, p):
                blocked_by_shift += 1
                continue
            try:
                if not _pattern_hours_match(e, p):
                    blocked_by_hours += 1
                    continue
            except (ValueError, TypeError):
                blocked_by_hours += 1
                continue
            matched.append(p["name"])
        if patterns and not matched:
            if blocked_by_shift == len(patterns):
                reasons.append("早番・中番・遅番の勤務可否とパターン名が一致しない")
            elif blocked_by_hours:
                min_minutes = _hours_as_minutes(e["min_hours"], 0)
                max_minutes = _hours_as_minutes(e["max_hours"], 24)
                if min_minutes == max_minutes:
                    available = [f"{p['name']}（拘束{_duration_label(_pattern_total_minutes(p))}・休憩{int(p['break_minutes'] or 0)}分）"
                                 for p in patterns if _pattern_shift_allowed(e, p)]
                    details = "、".join(available) if available else "勤務可のパターンなし"
                    reasons.append(
                        f"固定勤務時間（休憩込み・{_duration_label(min_minutes)}）に一致する勤務パターンがありません。"
                        f"使用可能なパターン: {details}。勤務パターンの開始・終了時刻（休憩込みの拘束時間）を確認してください。"
                    )
                else:
                    reasons.append(f"勤務パターンの勤務時間（休憩込み）が設定範囲（{e['min_hours']}〜{e['max_hours']}時間）に収まらない")
            else:
                reasons.append("勤務できるパターンがない")
        if not reasons and patterns:
            summary["compatible_employees"] += 1
        summary["employees"].append({"name": e["name"], "reasons": reasons,
                                     "matching_patterns": matched})
    # Show theoretical opening/closing candidates separately. An employee
    # needs at least one eligible time-compatible pattern to be counted.
    # These are NOT committed roster counts: weekly caps, leave and breaks
    # are not incorporated here. Multi-skilled employees appear in every
    # registered queue but are still one physical worker.
    critical=[]
    for slot_time, title in (("09:20", "9:20始業時"),("22:50", "23:00終業直前")):
        slot=mins(slot_time)
        counts={sk:0 for sk in ('consumer','merchant','PP')}
        for e in employees:
            if not any(_pattern_shift_allowed(e,p) and
                       _pattern_hours_match(e,p) and
                       mins(p['start_time'])<=slot<mins(p['end_time'])
                       for p in patterns):
                continue
            registered={token.strip().lower() for token in (e['skills'] or '').split(',')}
            for sk in counts:
                if sk.lower() in registered:
                    counts[sk]+=1
        critical.append({'label':title,'counts':counts})
    summary['critical_candidates']=critical
    if employees and patterns and summary["compatible_employees"] == 0:
        summary["reasons"].append("登録はありますが、勤務条件に合う従業員とパターンの組み合わせが0件です。")
    return summary


def _day_people(conn, day):
    """Finalized and draft shifts both count, but no employee is double-counted."""
    rows=conn.execute("""SELECT a.employee_id,e.name,e.skills,a.skill AS primary_skill,p.start_time,p.end_time,p.break_minutes,
        p.name AS pattern_name,p.shift_type
        FROM assignments a JOIN employees e ON e.id=a.employee_id
        JOIN shift_patterns p ON p.id=a.pattern_id WHERE a.work_date=?
        ORDER BY e.name""",(day,)).fetchall()
    return [dict(row) for row in rows]


def _refresh_day_plan(conn, day, status='draft'):
    people=_day_people(conn,day)
    rules=[dict(r) for r in conn.execute('SELECT * FROM coverage WHERE weekday=?',(date.fromisoformat(day).weekday(),))]
    overrides=[dict(r) for r in conn.execute('SELECT * FROM daily_skill_overrides WHERE work_date=?',(day,))]
    result=flex.build_day_plan(day,people,rules,overrides)
    conn.execute("""INSERT INTO daily_skill_plans(work_date,status,plan_json,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(work_date) DO UPDATE SET
        status=excluded.status,plan_json=excluded.plan_json,updated_at=excluded.updated_at""",
        (day,status,json.dumps(result,ensure_ascii=False),datetime.now().isoformat(timespec='seconds')))
    return result


def _available_candidates(e,patterns):
    return [dict(p) for p in patterns if _pattern_shift_allowed(e,p) and _pattern_hours_match(e,p)]


def _half_day_pattern_match(employee, pattern, kinds):
    """A half-day request relaxes MIN hours, but respects MAX and working window.

    We deliberately require an actual registered half-day pattern, rather than
    truncate a full-day pattern without the administrator's knowledge.
    """
    if not _pattern_shift_allowed(employee, pattern):
        return False
    if _pattern_total_minutes(pattern) <= 0 or _pattern_total_minutes(pattern) > _hours_as_minutes(employee["max_hours"], 24):
        return False
    if "午前休" in kinds and mins(pattern["start_time"]) < mins(HALF_DAY_SPLIT):
        return False
    if "午後休" in kinds and mins(pattern["end_time"]) > mins(HALF_DAY_SPLIT):
        return False
    return True


def _day_candidate_patterns(employee, patterns, requests, day):
    """Approved half days are hard constraints; pending wishes are preferred."""
    relevant = [r for r in requests if r["employee_id"] == employee["id"]
                and r["request_date"] == day and r["status"] != "rejected"
                and r["request_type"] in HALF_DAY_LEAVE_TYPES]
    approved = {r["request_type"] for r in relevant if r["status"] == "approved"}
    if approved:
        return [dict(p) for p in patterns if _half_day_pattern_match(employee, p, approved)]
    pending = {r["request_type"] for r in relevant}
    if pending:
        preferred = [dict(p) for p in patterns if _half_day_pattern_match(employee, p, pending)]
        if preferred:
            return preferred
    return _available_candidates(employee, patterns)


def _request_penalty(employee_id, day, requests):
    penalty=0
    for r in requests:
        if r['employee_id']!=employee_id or r['request_date']!=day or r['status']=='rejected':continue
        if r['request_type'] in FULL_DAY_LEAVE_TYPES:
            if r['status']=='approved':return None
            if r['request_type'] in ('有給休暇','確定休'):
                penalty+=300
            elif r['request_type']=='通常の休み希望':penalty+=120
            elif r['request_type']=='できれば休み':penalty+=35
            else:penalty+=60
    return penalty


def _consecutive_days(emp_id, day, occupied_dates):
    count=0; prev=date.fromisoformat(day)
    while prev-timedelta(days=count+1) in occupied_dates[emp_id]:
        count+=1
    return count


@app.route("/generate", methods=["POST"])
@login_required
@admin_required
def generate():
    """Monthly greedy coverage optimization, followed by 10-minute skill/break plans.

    Preserve finalized shifts, hard approved leave and daily manual skill overrides.
    The optimizer is heuristic; exact shortages are computed and shown afterwards.
    """
    month=request.form.get('month') or date.today().strftime('%Y-%m')
    days=month_days(month)
    with db() as c:
        employees=[dict(e) for e in c.execute('SELECT * FROM employees WHERE active=1 ORDER BY name')]
        patterns=[dict(p) for p in c.execute('SELECT * FROM shift_patterns WHERE active=1 ORDER BY start_time')]
        reqs=[dict(r) for r in c.execute('SELECT * FROM requests WHERE substr(request_date,1,7)=?',(month,))]
        fixed=[dict(a) for a in c.execute("SELECT * FROM assignments WHERE substr(work_date,1,7)=? AND status='finalized'",(month,))]
        readiness=shift_generation_readiness(employees,patterns)
        # Preview all assignments in memory first: never erase a usable draft on no result.
        planned={d.isoformat():[] for d in days}
        counted={e['id']:0 for e in employees}
        occupied_dates={e['id']:set() for e in employees}
        for a in fixed:
            uid=a['employee_id']
            if uid in counted:
                counted[uid]+=1
                occupied_dates[uid].add(date.fromisoformat(a['work_date']))
        days_by_id={d.isoformat():d for d in days}
        cached_patterns={e['id']:_available_candidates(e,patterns) for e in employees}
        pbyid={p['id']:dict(p) for p in c.execute('SELECT * FROM shift_patterns')}
        ebyid={e['id']:e for e in employees}
        for a in fixed:
            if a['work_date'] not in planned or a['employee_id'] not in ebyid: continue
            pattern=pbyid.get(a['pattern_id'])
            if pattern:
                employee=ebyid[a['employee_id']]
                planned[a['work_date']].append({'employee_id':employee['id'],'name':employee['name'],
                    'skills':employee['skills'],'start_time':pattern['start_time'],
                    'end_time':pattern['end_time'],'break_minutes':pattern['break_minutes'],
                    'pattern_id':pattern['id'],'primary_skill':a['skill'],'finalized':True})
        # Monthly working-day targets are HARD. Approved days off and weekly maximum
        # are checked first. No partial draft is persisted if any target is infeasible.
        start_day, end_day = days[0], days[-1]
        external = {}
        for row in c.execute("""SELECT employee_id,work_date FROM assignments
            WHERE work_date BETWEEN ? AND ? AND (work_date < ? OR work_date > ?)""",
            ((start_day-timedelta(days=38)).isoformat(),(end_day+timedelta(days=38)).isoformat(),
             start_day.isoformat(),end_day.isoformat())):
            external.setdefault(row['employee_id'],set()).add(date.fromisoformat(row['work_date']))
        requests_by_employee = {e['id']: [r for r in reqs if r['employee_id']==e['id']] for e in employees}
        # The most constrained operators go first, then optimize remaining coverage.
        employees_in_order = sorted(employees,key=lambda e:(
            len([d for d in days if _request_penalty(e['id'],d.isoformat(),reqs) is not None
                 and _day_candidate_patterns(e,patterns,requests_by_employee[e['id']],d.isoformat())]),
            -monthly_workdays.monthly_target(month,int(e['weekly_max_days'] or 5),e['monthly_max_days']),
            len(flex.skills_of(e['skills'])),e['id']))
        generation_failures=[]
        for a in fixed:
            eid=a['employee_id'];day=a['work_date']
            if _request_penalty(eid,day,requests_by_employee.get(eid,[])) is None:
                generation_failures.append(f"{ebyid.get(eid,{}).get('name','従業員')}：{day}の確定シフトが承認済み終日希望休と重複しています。")
            pattern=pbyid.get(a['pattern_id'])
            person=ebyid.get(eid)
            if person and pattern and any(
                r['status']=='approved' and r['request_type'] in HALF_DAY_LEAVE_TYPES and r['request_date']==day
                and not _half_day_pattern_match(person,pattern,{r['request_type']})
                for r in requests_by_employee[eid]):
                generation_failures.append(f"{person['name']}：{day}の確定シフトが承認済み半休と重複しています。")
        for e in employees_in_order:
            uid=e['id']
            candidate_by_date={}
            for d in days:
                key=d.isoformat()
                if any(row['employee_id']==uid for row in planned[key]):
                    continue
                penalty=_request_penalty(uid,key,requests_by_employee[uid])
                if penalty is None:continue
                options=_day_candidate_patterns(e,patterns,requests_by_employee[uid],key)
                if not options:continue
                demand=flex.demands_for_day(c.execute('SELECT * FROM coverage WHERE weekday=?',(d.weekday(),)).fetchall(),d.weekday())
                selected=planned[key]
                slot_status={t:flex.simultaneous_queue_coverage(selected,t,demand[t]) for t in flex.SLOTS}
                missing={t:{sk:max(0,demand[t][sk]-slot_status[t][0][sk]) for sk in flex.SKILLS}
                         for t in flex.SLOTS}
                physical_missing={t:slot_status[t][1] for t in flex.SLOTS}
                eligible_skills=flex.skills_of(e['skills'])
                scored=[]
                for p in options:
                    start_p=flex.minute(p['start_time']);end_p=flex.minute(p['end_time'])
                    gain=sum(1 for t in flex.SLOTS if start_p<=t<end_p and
                             any(missing[t].get(skill,0)>0 for skill in eligible_skills))
                    physical_gain=sum(1 for t in flex.SLOTS if start_p<=t<end_p and
                             any(physical_missing[t].get(skill,0)>0 for skill in eligible_skills))
                    # Break time lowers the effective covering value, but never
                    # changes workday counts or min/max registered shift duration.
                    break_discount=max(0, (int(p['break_minutes'] or 0)//10))
                    score=gain*5+physical_gain*3-break_discount-penalty*2-len(selected)*5
                    scored.append((score,-p['id'],p))
                score,_,chosen=max(scored,key=lambda z:(z[0],z[1]))
                candidate_by_date[d]=(chosen,score)
            fixed_days={date.fromisoformat(a['work_date']) for a in fixed if a['employee_id']==uid}
            target=monthly_workdays.monthly_target(month,int(e['weekly_max_days'] or 5),e['monthly_max_days'])
            selected_dates, diagnostic=monthly_workdays.select_workdays(
                days,candidate_by_date,fixed_days,external.get(uid,set()),
                target,int(e.get('weekly_max_days',5) or 5),
                int(e['max_consecutive'] or 5),e['monthly_max_days'])
            if diagnostic:
                generation_failures.append(f"{e['name']}：{diagnostic}")
            for d in selected_dates - fixed_days:
                chosen,score=candidate_by_date[d]
                planned[d.isoformat()].append({'employee_id':uid,'name':e['name'],'skills':e['skills'],
                    'start_time':chosen['start_time'],'end_time':chosen['end_time'],
                    'break_minutes':chosen['break_minutes'],'pattern_id':chosen['id'],'finalized':False})
                counted[uid]+=1;occupied_dates[uid].add(d)
        if generation_failures:
            flash('月間勤務日数を満たすシフトを作成できませんでした。既存のシフトは変更していません。','error')
            for detail in generation_failures[:20]:flash(detail,'error')
            if len(generation_failures)>20:flash(f'ほか{len(generation_failures)-20}名の条件不整合があります。','error')
            return redirect(url_for('index',month=month))
        # Assign ONE primary skill per date before any break/temporary-support plan.
        for d in days:
            selected=planned[d.isoformat()]
            demand=flex.demands_for_day(c.execute('SELECT * FROM coverage WHERE weekday=?',(d.weekday(),)).fetchall(),d.weekday())
            for person in selected:
                if not person['finalized']:
                    person.pop('primary_skill',None)
            chosen_primary=flex.choose_primary_skills(selected,demand)
            for person in selected:
                if not person['finalized']:
                    person['primary_skill']=chosen_primary.get(person['employee_id'])
        # First rebalance EARLY/MID/LATE coverage with a cheap 10-minute
        # multi-skill-aware surplus score. The monthly workdays, approved
        # leave, finalized assignments and employee limits never change.
        # Accept the candidate only if the full break-aware evaluation improves.
        coverage_rebalanced=0
        for d in days:
            day=d.isoformat()
            current=planned[day]
            if not current:
                continue
            rules=c.execute('SELECT * FROM coverage WHERE weekday=?',(d.weekday(),)).fetchall()
            existing=flex.build_day_plan(day,current,rules)
            base_cost=(10*sum(x['shortage'] for x in existing['shortages'])
                       +2*sum(x['shortage'] for x in existing.get('capacity_warnings',[]))
                       +100*len(existing['issues']))
            if base_cost==0:
                continue
            allowed={p['employee_id']:_day_candidate_patterns(
                ebyid[p['employee_id']],patterns,requests_by_employee[p['employee_id']],day)
                for p in current if not p['finalized']}
            demand=flex.demands_for_day(rules,d.weekday())
            trial,steps,_score=flex.rebalance_work_patterns(current,allowed,demand)
            if steps==0:
                continue
            # Re-evaluate primary skill preferences after rebalancing; queue
            # eligibility is still all registered skills, never exclusive.
            for p in trial:
                if not p['finalized']:
                    p.pop('primary_skill',None)
            preferred=flex.choose_primary_skills(trial,demand)
            for p in trial:
                if not p['finalized']:
                    p['primary_skill']=preferred.get(p['employee_id'])
            proposed=flex.build_day_plan(day,trial,rules)
            new_cost=(10*sum(x['shortage'] for x in proposed['shortages'])
                      +2*sum(x['shortage'] for x in proposed.get('capacity_warnings',[]))
                      +100*len(proposed['issues']))
            if new_cost<base_cost:
                planned[day]=trial
                coverage_rebalanced+=sum(a['pattern_id']!=b['pattern_id']
                                        for a,b in zip(current,trial))
        # Joint local refinement: reassess alternative working patterns WITH the actual
        # 10-minute skill-matching and break schedule. Limit evaluations per day so
        # 31+ operator monthly generation remains responsive without OR-Tools.
        refinement_count=0
        for d in days:
            day=d.isoformat()
            chosen=planned[day]
            if not chosen:continue
            rules=c.execute('SELECT * FROM coverage WHERE weekday=?',(d.weekday(),)).fetchall()
            baseline=flex.build_day_plan(day,chosen,rules)
            current_cost=(10*sum(x['shortage'] for x in baseline['shortages'])
                          +2*sum(x['shortage'] for x in baseline.get('capacity_warnings',[]))
                          +100*len(baseline['issues']))
            if current_cost==0:continue
            # Rank alternatives by coverage on currently unfilled 10-minute slots;
            # test the top three alternatives with the full break-aware model.
            rough=[]
            shortage_by_slot={}
            for x in baseline['shortages']:
                shortage_by_slot.setdefault(flex.minute(x['time']),set()).add(x['skill'])
            for x in baseline.get('capacity_warnings',[]):
                shortage_by_slot.setdefault(flex.minute(x['time']),set()).update(
                    sk for sk,gap in x['by_skill'].items() if gap)
            for idx,person in enumerate(chosen):
                if person['finalized']:continue
                for p in _day_candidate_patterns(ebyid[person['employee_id']],patterns,reqs,day):
                    if p['id']==person['pattern_id']:continue
                    old_start=flex.minute(person['start_time']);old_end=flex.minute(person['end_time'])
                    new_start=flex.minute(p['start_time']);new_end=flex.minute(p['end_time'])
                    relevant=flex.skills_of(person['skills'])
                    added=sum(1 for t in flex.SLOTS if new_start<=t<new_end and not(old_start<=t<old_end)
                        and any(sk in shortage_by_slot.get(t,set()) for sk in relevant))
                    lost=sum(1 for t in flex.SLOTS if old_start<=t<old_end and not(new_start<=t<new_end)
                        and any(sk in shortage_by_slot.get(t,set()) for sk in relevant))
                    rough.append((added-lost,idx,p))
            rough.sort(key=lambda x:(-x[0],x[1],x[2]['id']))
            best=None
            for _,idx,p in rough[:3]:
                replaced=chosen[idx]
                choice=dict(replaced,start_time=p['start_time'],end_time=p['end_time'],
                            break_minutes=p['break_minutes'],pattern_id=p['id'])
                test_people=chosen.copy();test_people[idx]=choice
                # Once a start/end changes, allow primary rebalancing among DRAFT
                # operators, never changing a finalized primary.
                for entry in test_people:
                    if not entry['finalized']:
                        entry.pop('primary_skill',None)
                test_demand=flex.demands_for_day(rules,d.weekday())
                pmap=flex.choose_primary_skills(test_people,test_demand)
                for entry in test_people:
                    if not entry['finalized']:
                        entry['primary_skill']=pmap.get(entry['employee_id'])
                proposed=flex.build_day_plan(day,test_people,rules)
                cost=(10*sum(x['shortage'] for x in proposed['shortages'])
                      +2*sum(x['shortage'] for x in proposed.get('capacity_warnings',[]))
                      +100*len(proposed['issues']))
                if cost<current_cost and (best is None or cost<best[0]):best=(cost,idx,choice)
            if best is not None:
                chosen[best[1]]=best[2]
                for entry in chosen:
                    if not entry['finalized']:
                        entry.pop('primary_skill',None)
                demand=flex.demands_for_day(rules,d.weekday())
                pmap=flex.choose_primary_skills(chosen,demand)
                for entry in chosen:
                    if not entry['finalized']:
                        entry['primary_skill']=pmap.get(entry['employee_id'])
                refinement_count+=1
        rows=[(person['employee_id'],key,person['pattern_id'],person.get('primary_skill') or flex.skills_of(person['skills'])[0], 'draft')
              for key,people in planned.items() for person in people
              if not person['finalized'] and flex.skills_of(person['skills'])]
        if not rows:
            flash('仮シフトは0件でした。既存データは変更していません。自動作成の診断を確認してください。','error')
            for reason in readiness['reasons'][:3]:flash(reason,'error')
            return redirect(url_for('index',month=month))
        c.execute("DELETE FROM assignments WHERE substr(work_date,1,7)=? AND status='draft'",(month,))
        c.executemany('INSERT INTO assignments(employee_id,work_date,pattern_id,skill,status) VALUES(?,?,?,?,?)',rows)
        shortage_slots=0;total_switches=0
        for d in days:
            plan=_refresh_day_plan(c,d.isoformat(),'draft')
            shortage_slots+=len(plan['shortages'])
            total_switches+=plan['skill_switches']
        flash(f'{month}の仮シフトを{len(rows)}件生成しました。各日主担当スキル固定・休憩・必要時のみ短時間応援を検討しました。勤務パターン調整：{refinement_count+coverage_rebalanced}件。')
        if shortage_slots:
            flash(f'注意：必要人数不足が{shortage_slots}スキル×10分枠残っています。日別配置案で確認してください。','error')
    return redirect(url_for('index',month=month))


def _compact_shortages(shortages):
    """Merge consecutive 10-minute deficit slots per skill and equal counts."""
    out=[]
    for rec in sorted(shortages,key=lambda r:(r['skill'],r['time'])):
        if out and out[-1]['skill']==rec['skill'] and out[-1]['required']==rec['required'] \
                and out[-1]['assigned']==rec['assigned'] and out[-1]['end']==rec['time']:
            out[-1]['end']=flex.clock(flex.minute(rec['time'])+10)
        else:
            out.append({'start':rec['time'],'end':flex.clock(flex.minute(rec['time'])+10),
                'skill':rec['skill'],'required':rec['required'],'assigned':rec['assigned']})
    return sorted(out,key=lambda a:(a['start'],a['skill']))


def validate_month(month):
    warnings=[]
    with db() as c:
        reqs=c.execute('SELECT * FROM requests WHERE substr(request_date,1,7)=?',(month,)).fetchall()
        shifts=c.execute('SELECT employee_id,work_date FROM assignments WHERE substr(work_date,1,7)=?',(month,)).fetchall()
        rules=c.execute('SELECT * FROM coverage').fetchall()
        for day in month_days(month):
            day_key=day.isoformat()
            people=_day_people(c,day_key)
            manual=[dict(r) for r in c.execute('SELECT * FROM daily_skill_overrides WHERE work_date=?',(day_key,))]
            plan=flex.build_day_plan(day_key,people,rules,manual)
            for issue in plan['issues']:
                warnings.append({'date':day_key,'kind':'休憩配置不可','detail':issue})
            for item in _compact_shortages(plan['shortages']):
                warnings.append({'date':day_key,'kind':'受電待機人数不足','detail':
                    f"{item['skill']} {item['start']}–{item['end']}: 必要{item['required']}名 / 受電可能{item['assigned']}名（兼務者は各キューに表示）"})
            if plan.get('capacity_warnings'):
                first=plan['capacity_warnings'][0]
                warnings.append({'date':day_key,'kind':'同時着信時の実人数不足','detail':
                    f"{len(plan['capacity_warnings'])}枠で複数キューの同時対応に不足があります。例：{first['time']}、必要合計{first['required_total']}枠 / 実人数{first['distinct_people']}名。複数スキル兼務は同時受電待機できますが、1人で2通話は処理できません。"})
    assigned={(a['employee_id'],a['work_date']) for a in shifts}
    # Map the actual assigned working windows, not just the presence of an assignment.
    with db() as conn:
        shift_windows={(r['employee_id'],r['work_date']):(r['start_time'],r['end_time'])
            for r in conn.execute("""SELECT a.employee_id,a.work_date,p.start_time,p.end_time
                FROM assignments a JOIN shift_patterns p ON p.id=a.pattern_id
                WHERE substr(a.work_date,1,7)=?""",(month,))}
    for r in reqs:
        if r['status']=='rejected':continue
        key=(r['employee_id'],r['request_date'])
        kind=r['request_type']
        if kind in HALF_DAY_LEAVE_TYPES:
            hours=shift_windows.get(key)
            if hours is None:
                if r['status']=='approved':
                    warnings.append({'date':r['request_date'],'kind':'半休の勤務未配置',
                                     'detail':f'{kind}は承認済みですが、勤務側の半日シフトがありません。勤務パターンを確認してください。'})
                continue
            overlap = (kind=='午前休' and mins(hours[0]) < mins(HALF_DAY_SPLIT)) or \
                      (kind=='午後休' and mins(hours[1]) > mins(HALF_DAY_SPLIT))
            if overlap:
                warnings.append({'date':r['request_date'],
                    'kind':'確定休と勤務が重複' if r['status']=='approved' else '休み希望未反映',
                    'detail':f'{kind}（13:00区切り）と勤務時間{hours[0]}～{hours[1]}が重複しています'})
        elif kind in FULL_DAY_LEAVE_TYPES and key in assigned:
            if r['status']=='approved':
                warnings.append({'date':r['request_date'],'kind':'確定休と勤務が重複','detail':'確定済み休暇とシフトが重複しています'})
            else:
                warnings.append({'date':r['request_date'],'kind':'休み希望未反映','detail':f'{kind}が未反映です'})
    # An operator's contractual monthly workday target and weekly maximum are
    # checked even after manual edits or deletions. Weeks run Sunday–Saturday.
    with db() as conn:
        for employee in conn.execute('SELECT id,name,monthly_days,weekly_max_days,monthly_max_days FROM employees WHERE active=1'):
            actual=sum(1 for a in shifts if a['employee_id']==employee['id'])
            target=monthly_workdays.monthly_target(month,int(employee['weekly_max_days'] or 5),employee['monthly_max_days'])
            if actual < target:
                warnings.append({'date':month,'kind':'月間勤務日数不足',
                    'detail':f"{employee['name']}：週{employee['weekly_max_days']}日契約から算出した基準{target}日／配置{actual}日（不足{target-actual}日）。"})
            if employee['monthly_max_days'] is not None and actual > int(employee['monthly_max_days']):
                warnings.append({'date':month,'kind':'月間最大勤務日数超過',
                    'detail':f"{employee['name']}：配置{actual}日が月間上限{employee['monthly_max_days']}日を超えています。"})
        first=month_days(month)[0];last=month_days(month)[-1]
        week_start=first-timedelta(days=(first.weekday()+1)%7)
        week_end=last+timedelta(days=(5-last.weekday())%7)
        week_counts={}
        for row in conn.execute("""SELECT a.employee_id,e.name,e.weekly_max_days,a.work_date
            FROM assignments a JOIN employees e ON e.id=a.employee_id
            WHERE e.active=1 AND a.work_date BETWEEN ? AND ?""",
            (week_start.isoformat(),week_end.isoformat())):
            # Count for each (employee, Sunday-starting week) across month boundaries.
            d=date.fromisoformat(row['work_date']);sunday=d-timedelta(days=(d.weekday()+1)%7)
            key=(row['employee_id'],sunday)
            if key not in week_counts:week_counts[key]=[row['name'],int(row['weekly_max_days'] or 5),0]
            week_counts[key][2]+=1
        for (_,sunday),(name,limit,used) in week_counts.items():
            if used>limit:
                warnings.append({'date':sunday.isoformat(),'kind':'週最大勤務日数超過',
                    'detail':f'{name}：{sunday.isoformat()}開始の週に{used}日（上限{limit}日）。'})
    return warnings

@app.route('/daily-plan/<work_date>')
@login_required
@admin_required
def daily_plan_page(work_date):
    try:
        when=date.fromisoformat(work_date)
        if when.isoformat()!=work_date:raise ValueError()
    except ValueError:
        return ('日付の形式が不正です',400)
    with db() as c:
        row=c.execute('SELECT * FROM daily_skill_plans WHERE work_date=?',(work_date,)).fetchone()
        people=_day_people(c,work_date)
        if not row:
            plan=_refresh_day_plan(c,work_date)
            status='draft'
        else:
            plan=json.loads(row['plan_json']);status=row['status']
        overrides=[dict(x) for x in c.execute('SELECT o.*,e.name FROM daily_skill_overrides o JOIN employees e ON e.id=o.employee_id WHERE o.work_date=?',(work_date,))]
        shifts=c.execute('SELECT a.status FROM assignments a WHERE a.work_date=?',(work_date,)).fetchall()
    return render_template('daily_plan.html',day=work_date,month=work_date[:7],
            people=people,plan=plan,status=status,overrides=overrides,shifts=shifts,
            skills=flex.SKILLS)


@app.route('/daily-plan/<work_date>/refresh',methods=['POST'])
@login_required
@admin_required
def refresh_day(work_date):
    try:date.fromisoformat(work_date)
    except ValueError:return ('日付の形式が不正です',400)
    with db() as c:
        current=c.execute('SELECT status FROM daily_skill_plans WHERE work_date=?',(work_date,)).fetchone()
        _refresh_day_plan(c,work_date,current['status'] if current else 'draft')
    flash('当日の主担当を維持したまま、休憩と必要時の一時応援案を再計算しました。')
    return redirect(url_for('daily_plan_page',work_date=work_date))


@app.route('/daily-plan/<work_date>/primary',methods=['POST'])
@login_required
@admin_required
def edit_daily_primary(work_date):
    """Change the daily HOME skill without changing an operator's qualifications."""
    try:
        if date.fromisoformat(work_date).isoformat()!=work_date:raise ValueError('日付が不正です')
        uid=int(request.form['employee_id']);skill=request.form['skill']
        with db() as c:
            emp=c.execute("""SELECT e.skills FROM assignments a
                  JOIN employees e ON e.id=a.employee_id
                  WHERE a.employee_id=? AND a.work_date=?""",(uid,work_date)).fetchone()
            if not emp or skill not in flex.skills_of(emp['skills']):
                raise ValueError('対象オペレーターの担当可能スキルを指定してください')
            c.execute('UPDATE assignments SET skill=? WHERE work_date=? AND employee_id=?',
                      (skill,work_date,uid))
            # An already-finalized shift's primary can be adjusted separately.
            status=c.execute('SELECT status FROM daily_skill_plans WHERE work_date=?',(work_date,)).fetchone()
            _refresh_day_plan(c,work_date,status['status'] if status else 'draft')
        flash(f'{work_date}の基本担当スキルを{skill}に設定しました。勤務開始・終了は変更していません。')
    except (ValueError,KeyError,sqlite3.Error) as exc:
        flash(f'基本担当を変更できませんでした: {exc}','error')
    return redirect(url_for('daily_plan_page',work_date=work_date))


@app.route('/daily-plan/<work_date>/skill',methods=['POST'])
@login_required
@admin_required
def edit_daily_skill(work_date):
    try:
        date.fromisoformat(work_date)
        uid=int(request.form['employee_id'])
        start=request.form['start_time'];end=request.form['end_time']
        skill=request.form['skill']
        if skill not in flex.SKILLS or not (flex.OPEN <= flex.minute(start)<flex.minute(end)<=flex.CLOSE) or \
                flex.minute(start)%10 or flex.minute(end)%10:raise ValueError()
        with db() as c:
            p=c.execute("""SELECT p.start_time,p.end_time,e.skills FROM assignments a
              JOIN shift_patterns p ON p.id=a.pattern_id JOIN employees e ON e.id=a.employee_id
              WHERE a.employee_id=? AND a.work_date=?""",(uid,work_date)).fetchone()
            if not p or skill not in flex.skills_of(p['skills']) or start<p['start_time'] or end>p['end_time']:
                raise ValueError('対象従業員の対応スキル・勤務時間に一致しません')
            c.execute('DELETE FROM daily_skill_overrides WHERE work_date=? AND employee_id=? AND NOT(end_time<=? OR start_time>=?)',
                      (work_date,uid,start,end))
            c.execute('INSERT INTO daily_skill_overrides(work_date,employee_id,start_time,end_time,skill) VALUES(?,?,?,?,?)',
                      (work_date,uid,start,end,skill))
            old=c.execute('SELECT status FROM daily_skill_plans WHERE work_date=?',(work_date,)).fetchone()
            _refresh_day_plan(c,work_date,old['status'] if old else 'draft')
        flash(f'{work_date}の{start}〜{end}を{skill}に変更しました。配置人数の警告を確認してください。')
    except (KeyError,ValueError,sqlite3.Error) as exc: flash(f'スキル変更できませんでした: {exc}','error')
    return redirect(url_for('daily_plan_page',work_date=work_date))


@app.route('/daily-plan/<work_date>/skill/<int:override_id>/delete',methods=['POST'])
@login_required
@admin_required
def delete_daily_skill(work_date,override_id):
    with db() as c:
        c.execute('DELETE FROM daily_skill_overrides WHERE id=? AND work_date=?',(override_id,work_date))
        old=c.execute('SELECT status FROM daily_skill_plans WHERE work_date=?',(work_date,)).fetchone()
        _refresh_day_plan(c,work_date,old['status'] if old else 'draft')
    flash('一時的な担当スキル指定を解除し、主担当を維持して再計算しました。')
    return redirect(url_for('daily_plan_page',work_date=work_date))

@app.route('/daily-plan/<work_date>/send-to-break',methods=['POST'])
@login_required
@admin_required
def send_day_plan_to_break(work_date):
    """Publish finalized day's draft suggestion to break management, not auto-confirm."""
    try:
        if date.fromisoformat(work_date).isoformat()!=work_date:
            raise ValueError('日付が不正です')
        with db() as c:
            final=c.execute("SELECT COUNT(*) FROM assignments WHERE work_date=? AND status='finalized'",(work_date,)).fetchone()[0]
            draft=c.execute("SELECT COUNT(*) FROM assignments WHERE work_date=? AND status='draft'",(work_date,)).fetchone()[0]
            if not final or draft:
                raise ValueError('すべてのシフトを最終確定してから休憩管理へ送信してください。')
            changed=c.execute('SELECT COUNT(*) FROM cc_shift_overrides WHERE work_date=?',(work_date,)).fetchone()[0]
            if changed:
                raise ValueError('当日の遅刻・早退による勤務時間変更が記録されています。休憩管理画面から配置案を再生成してください。')
            result=_refresh_day_plan(c,work_date,'finalized')
            if result['issues']:
                raise ValueError('配置できていない休憩があります。日別配置案の警告を修正してから送信してください。')
            rows=c.execute("SELECT a.employee_id,e.name,cc.id AS cc_id FROM assignments a JOIN employees e ON e.id=a.employee_id JOIN users su ON su.employee_id=e.id AND su.role IN ('operator','admin') JOIN cc_users cc ON cc.username=su.username AND cc.active=1 WHERE a.work_date=? AND a.status='finalized'",(work_date,)).fetchall()
            people=result['people']
            mapping={row['employee_id']:row['cc_id'] for row in rows}
            if set(mapping)!={p['employee_id'] for p in people}:
                raise ValueError('オペレーター連携が完了していません。休憩管理画面を開き、再度この操作を行ってください。')
            admin=c.execute('SELECT id FROM cc_users WHERE username=?',(session['username'],)).fetchone()
            if not admin:raise ValueError('管理者アカウントが休憩管理と同期されていません。')
            break_map={str(mapping[int(uid)]):[{'type':('morning' if '前半' in b['type'] else
                    'afternoon' if '後半' in b['type'] else 'lunch'),
                    'start':b['start'],'end':b['end'],'paid':b['paid']} for b in items]
                for uid,items in result['breaks'].items() if int(uid) in mapping}
            segments={uid:[] for uid in mapping}
            for seg in result['segments']:
                if seg['employee_id'] in segments:segments[seg['employee_id']].append(seg)
            schedule=[]
            for person in people:
                uid=person['employee_id']
                for slot in flex.SLOTS:
                    if not flex.minute(person['start'])<=slot<flex.minute(person['end']):continue
                    seg=next((v for v in segments[uid] if flex.minute(v['start'])<=slot<flex.minute(v['end'])),None)
                    if not seg:continue
                    resting=seg['skill']=='休憩'
                    queue_skills=[] if resting else list(seg.get('skills') or flex.skills_of(seg['skill']))
                    displayed=' / '.join({'consumer':'Consumer','merchant':'Merchant','PP':'PP'}[x]
                                       for x in queue_skills) if queue_skills else '待機'
                    schedule.append({'time':flex.clock(slot),'operator_id':mapping[uid],
                       'operator':person['name'],'skill':displayed,'skills':queue_skills,
                       'preferred_skill':seg.get('preferred_skill'),
                       'on_break':resting})
            shortages=[{'time':x['time'],'skill':{'consumer':'Consumer','merchant':'Merchant','PP':'PP'}[x['skill']],
                   'required':x['required'],'eligible_available':x['assigned'],
                   'shortage':x['shortage'],'warning_type':'queue_minimum'}
                   for x in result['shortages']]
            shortages.extend({'time':x['time'],'skill':'同時着信の処理枠',
                   'required':x['required_total'],'eligible_available':x['distinct_people'],
                   'shortage':x['shortage'],'warning_type':'simultaneous_capacity'}
                   for x in result.get('capacity_warnings',[]))
            present=[{'id':mapping[p['employee_id']],'name':p['name']} for p in people]
            payload={'date':work_date,'breaks':break_map,'schedule':schedule,'slots':len(flex.SLOTS),
                     'generated_at':datetime.now().isoformat(timespec='seconds'),
                     'attendance':{'present':present,'absent':[]},
                     'minimums_met':not bool(shortages or result['issues']),
                     'shortages':shortages[:100],
                     'capacity_warnings':result.get('capacity_warnings',[]),
                     'note':'複数スキル保持者は勤務中に全対応キューで同時受電待機。1人が複数通話を同時処理できるとは計算せず、処理能力不足は別途警告。休憩管理での確認・確定が必要です。'}
            c.execute('INSERT INTO cc_schedule_runs(schedule_date,status,result_json,created_by) VALUES(?,?,?,?)',
                      (work_date,'draft',json.dumps(payload,ensure_ascii=False),admin['id']))
        flash(f'{work_date}のスキル・休憩配置案を休憩管理の下書きへ送信しました。確定はまだされていません。')
        if shortages: flash(f'{len(shortages)}枠に必要人数の警告があります。休憩管理で2段階確認が必要です。','error')
        return redirect('/break/')
    except (ValueError,sqlite3.Error) as exc:
        flash(f'休憩管理に送信できませんでした: {exc}','error')
        return redirect(url_for('daily_plan_page',work_date=work_date))


@app.route("/validate")
@login_required
def validate():
    month = get_month()
    warnings = validate_month(month)
    return redirect(url_for("index", month=month, modal="warnings"))

@app.route("/finalize", methods=["POST"])
@login_required
@admin_required
def finalize():
    month = request.form.get("month")
    stage = request.form.get("stage")
    reason = request.form.get("reason","").strip()
    warnings = validate_month(month)
    hard = [w for w in warnings if w["kind"] in ("確定休と勤務が重複", "月間勤務日数不足", "月間最大勤務日数超過", "週最大勤務日数超過")]
    if hard:
        flash("承認済み休暇・月間基準勤務日数・月間最大勤務日数・週最大勤務日数の必須条件を満たしていません。警告内容を修正してください。", "error")
        return redirect(url_for("index", month=month, modal="warnings"))
    try:
        with db() as c:
            if stage == "review":
                c.execute("INSERT INTO approvals(work_month,admin_username,action,reason,created_at) VALUES(?,?,?,?,?)",
                          (month,session["username"],"警告確認",reason,datetime.now().isoformat(timespec="seconds")))
                flash("1段階目の確認を記録しました。内容を再確認し、確定する場合は2段階目へ進んでください。")
                return redirect(url_for("index", month=month, modal="final"))
            if stage != "final":
                flash("不正な確定操作です。", "error")
                return redirect(url_for("index", month=month, modal="warnings"))
            first = c.execute("SELECT id FROM approvals WHERE work_month=? AND action='警告確認' ORDER BY id DESC LIMIT 1", (month,)).fetchone()
            if not first:
                flash("先に1段階目の確認を行ってください。", "error")
                return redirect(url_for("index", month=month, modal="warnings"))
            if not reason:
                flash("確定理由を入力してください。", "error")
                return redirect(url_for("index", month=month, modal="final"))
            c.execute("UPDATE assignments SET status='finalized' WHERE substr(work_date,1,7)=?", (month,))
            for work_day in month_days(month):
                _refresh_day_plan(c,work_day.isoformat(),'finalized')
            c.execute("INSERT INTO approvals(work_month,admin_username,action,reason,created_at) VALUES(?,?,?,?,?)",
                      (month,session["username"],"最終確定",reason,datetime.now().isoformat(timespec="seconds")))
            # The attendance checkbox was historically independent of assignments:
            # a previously saved absence survived shift finalization for only some
            # operators. Synchronize every day, in the SAME SQLite transaction.
            report = attendance_sync.sync_month(c, month, session["username"])
            if report['available']:
                flash(f"シフトを確定し、休憩管理の出勤状態も{report['days']}日分同期しました（出勤{report['present']}件）。")
            else:
                flash("シフトを確定しました。休憩管理のデータが未初期化のため、出勤同期は行われませんでした。", "error")
    except (ValueError, sqlite3.Error) as exc:
        flash(f"シフト確定・出勤同期を完了できませんでした: {exc}", "error")
        return redirect(url_for("index", month=month, modal="warnings"))
    return redirect(url_for("index", month=month, modal="result"))

@app.route("/finalize")
@login_required
@admin_required
def finalize_page():
    month = get_month()
    with db() as c:
        first = c.execute("SELECT * FROM approvals WHERE work_month=? AND action='警告確認' ORDER BY id DESC LIMIT 1", (month,)).fetchone()
    return redirect(url_for("index", month=month, modal="final"))

@app.route("/export")
@login_required
@admin_required
def export_csv():
    """Export only finalized assignments for the selected month."""
    month = get_month()
    file_format = request.args.get("format", "xlsx").lower()
    if file_format not in ("xlsx", "csv"):
        flash("出力形式を確認してください。", "error")
        return redirect(url_for("index", month=month))
    with db() as c:
        rows = c.execute("""SELECT a.work_date, e.name, COALESCE(u.username, '') AS login_id,
            p.name AS pattern_name, p.start_time, p.end_time, a.skill, a.note
            FROM assignments a
            JOIN employees e ON e.id=a.employee_id
            JOIN shift_patterns p ON p.id=a.pattern_id
            LEFT JOIN users u ON u.employee_id=e.id AND u.role='operator'
            WHERE substr(a.work_date,1,7)=? AND a.status='finalized'
            ORDER BY a.work_date,p.start_time,e.name,a.id""", (month,)).fetchall()
    if not rows:
        flash("対象月に確定済みシフトがありません。シフトを確定してから出力してください。", "error")
        return redirect(url_for("index", month=month))
    headers = ["勤務日", "従業員名", "ログインID", "勤務パターン", "開始時刻", "終了時刻", "当日の主担当スキル", "備考"]
    values = [[r[k] or "" for k in ("work_date", "name", "login_id", "pattern_name", "start_time", "end_time", "skill", "note")] for r in rows]
    if file_format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(values)
        data = io.BytesIO(("\ufeff" + output.getvalue()).encode("utf-8"))
        return send_file(data, mimetype="text/csv; charset=utf-8", as_attachment=True,
                         download_name=f"finalized_shifts_{month}.csv")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "確定シフト"
    ws.append(headers)
    for row in values:
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{len(values)+1}"
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")
    for i, width in enumerate((16, 20, 19, 19, 14, 14, 19, 36), 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="center")
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     download_name=f"finalized_shifts_{month}.xlsx")

@app.route("/export/operators", methods=["GET"])
@login_required
@admin_required
def export_operator_shifts():
    """Download a finalized monthly schedule per operator, or a ZIP per operator."""
    month = get_month()
    file_format = request.args.get("format", "xlsx").lower()
    employee_id_param = request.args.get("employee_id", "")
    try:
        operator_shift_export.validate_month(month)
        if file_format not in ("xlsx", "csv"):
            raise ValueError("ExcelまたはCSVを選択してください。")
        if employee_id_param == "all":
            employee_id = None
        else:
            if not employee_id_param.isascii() or not employee_id_param.isdecimal() or int(employee_id_param) <= 0:
                raise ValueError("オペレーターを選択してください。")
            employee_id = int(employee_id_param)
        with db() as c:
            grouped = operator_shift_export.collect_finalized(c, month, employee_id)
        if not grouped:
            raise ValueError("選択した月・オペレーターには確定済みシフトがありません。")
        if employee_id is None:
            data = operator_shift_export.build_zip(grouped, month, file_format)
            file_name = f"confirmed_shifts_by_operator_{month}_{file_format}.zip"
            mime = operator_shift_export.ZIP_MIME
        else:
            employee = grouped[employee_id]
            data = (operator_shift_export.build_xlsx(employee, month)
                    if file_format == "xlsx" else operator_shift_export.build_csv(employee, month))
            file_name = operator_shift_export.export_filename(employee, month, file_format)
            mime = (operator_shift_export.EXCEL_MIME if file_format == "xlsx"
                    else operator_shift_export.CSV_MIME)
        return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True,
                         download_name=file_name)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index", month=month if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) else None))


@app.route("/assignments/<int:assignment_id>/edit", methods=["GET","POST"])
@login_required
@admin_required
def edit_assignment(assignment_id):
    with db() as c:
        a = c.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
        patterns = c.execute("SELECT * FROM shift_patterns WHERE active=1 ORDER BY start_time").fetchall()
        if not a: return redirect(url_for("index"))
        if request.method == "POST":
            eid=int(request.form['employee_id'])
            selected_skill=request.form['skill']
            employee=c.execute('SELECT skills FROM employees WHERE id=?',(eid,)).fetchone()
            if not employee or selected_skill not in flex.skills_of(employee['skills']):
                flash('指定した主担当スキルはオペレーターの対応可能スキルに含まれません。','error')
                return redirect(url_for('edit_assignment',assignment_id=assignment_id))
            c.execute("UPDATE assignments SET employee_id=?,pattern_id=?,skill=?,note=?,status='draft' WHERE id=?",
                      (int(request.form["employee_id"]), int(request.form["pattern_id"]), request.form["skill"],
                       request.form.get("note",""), assignment_id))
            c.execute('DELETE FROM daily_skill_overrides WHERE work_date=? AND employee_id=?',(a['work_date'],a['employee_id']))
            _refresh_day_plan(c,a['work_date'])
            flash("シフトを更新しました。日別のスキル・休憩案も再計算しました。")
            return redirect(url_for("index", month=a["work_date"][:7]))
    return render_template("edit_assignment.html", a=a, employees=employees, patterns=patterns)


@app.route("/shifts/bulk-delete", methods=["POST"])
@login_required
@admin_required
def bulk_delete_shifts():
    """Delete monthly draft shifts, or explicitly include finalized shifts."""
    month = request.form.get("month", "")
    # Use the rendered month, not a free-form redirect target.
    try:
        bulk_shift_delete.validate_month(month)
    except bulk_shift_delete.BulkShiftDeleteError:
        flash("対象月が正しくありません。", "error")
        return redirect(url_for("index"))
    if (not session.get("shift_delete_token") or
            not hmac.compare_digest(request.form.get("csrf_token", ""),
                                    session["shift_delete_token"])):
        flash("確認画面の有効期限が切れました。カレンダーを再表示してください。", "error")
        return redirect(url_for("index", month=month))
    if (request.form.get("confirm_month") != month or
            request.form.get("understood") != "yes"):
        flash("対象月と削除内容を確認し、確認欄にチェックを入れてください。", "error")
        return redirect(url_for("index", month=month))
    try:
        with db() as c:
            result = bulk_shift_delete.remove_month(
                c, month=month, scope=request.form.get("scope", ""),
                admin_username=session["username"],
                expected_draft=request.form.get("expected_draft"),
                expected_finalized=request.form.get("expected_finalized"),
                refresh_day=_refresh_day_plan)
    except (bulk_shift_delete.BulkShiftDeleteError, sqlite3.Error) as exc:
        flash(f"シフトを一括削除できませんでした: {exc}", "error")
        return redirect(url_for("index", month=month))
    flash(f"{month}のシフトを削除しました：仮 {result['draft']}件、確定済み {result['finalized']}件。"
          f"関連する休憩案 {result['invalidated_break_runs']}件を無効化しました。")
    return redirect(url_for("index", month=month))

@app.route("/assignments/<int:assignment_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_assignment(assignment_id):
    with db() as c:
        a = c.execute("SELECT work_date FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        c.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))
        if a:
            _refresh_day_plan(c,a['work_date'])
    flash("シフトを削除しました。")
    return redirect(url_for("index", month=a["work_date"][:7] if a else date.today().strftime("%Y-%m")))

@app.route("/password", methods=["GET","POST"])
@login_required
def password_page():
    forced = False
    with db() as c:
        account = c.execute("SELECT * FROM users WHERE username=?", (session["username"],)).fetchone()
    if account is None:
        session.clear()
        return redirect(url_for("login"))
    forced = bool(account["must_change_password"])
    if request.method == "POST":
        old, new = request.form.get("old_password", ""), request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not valid_password(new):
            flash("新しいパスワードは8〜16文字で、4種類の文字種のうち3種類以上を含めてください。", "error")
        elif new != confirm:
            flash("新しいパスワードの確認が一致しません。", "error")
        elif not check_password_hash(account["password_hash"], old):
            flash("現在のパスワードが違います。", "error")
        elif check_password_hash(account["password_hash"], new):
            flash("現在とは異なるパスワードを設定してください。", "error")
        else:
            with db() as c:
                c.execute("UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
                          (generate_password_hash(new),account["id"]))
            flash("パスワードを変更しました。")
            return redirect(url_for("index"))
    return render_template("password.html", forced=forced)

if __name__ == "__main__":
    init_db()
    # Single-database unified edition: legacy cross-database worker disabled.
    # Bind only to LAN interfaces when explicitly configured; default local-only for safe first run.
    host = os.environ.get("SHIFT_HOST", "0.0.0.0")
    port = int(os.environ.get("SHIFT_PORT", "5050"))
    app.run(host=host, port=port, debug=False)
