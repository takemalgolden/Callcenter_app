#!/usr/bin/env python3
"""Call Center Local Scheduler - standard-library LAN server with SQLite."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json, sqlite3, os, secrets, hashlib, hmac, time, threading, socket, sys, base64, tempfile, csv, io, zipfile, re
import xml.etree.ElementTree as ET
# Share the exact primary-skill/short-term-support algorithm with shift planning.
# The bundled copy also keeps the standalone break app functional.
try:
    import flexible_optimizer as flex
except ModuleNotFoundError:
    # A standalone break_engine is imported directly in integration tests.
    import importlib.util as _importlib_util
    _spec = _importlib_util.spec_from_file_location(
        'break_primary_optimizer', os.path.join(os.path.dirname(__file__), 'flexible_optimizer.py'))
    flex = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(flex)
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))) if getattr(sys, 'frozen', False) else BASE_DIR
if getattr(sys, 'frozen', False):
    DATA_DIR = os.path.join(os.environ.get('LOCALAPPDATA', BASE_DIR), 'CallCenterLocal', 'data')
else:
    DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.environ.get('UNIFIED_DB_PATH', os.path.join(DATA_DIR, 'callcenter.sqlite3'))
HOST = os.environ.get('CALLCENTER_HOST', '0.0.0.0')
PORT = int(os.environ.get('CALLCENTER_PORT', '8765'))
SKILLS = ['Consumer', 'Merchant', 'PP']
SESSIONS = {}
EVENTS = []
EVENT_LOCK = threading.Lock()

os.makedirs(DATA_DIR, exist_ok=True)

def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=15000')
    return con

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 240000).hex()
    return salt, digest

def verify_password(password, salt, digest):
    return hmac.compare_digest(hash_password(password, salt)[1], digest)

def valid_password(password):
    """Require 8-16 chars and at least three of uppercase/lowercase/digit/symbol."""
    if not isinstance(password, str) or not 8 <= len(password) <= 16:
        return False
    categories = (
        any('A' <= c <= 'Z' for c in password),
        any('a' <= c <= 'z' for c in password),
        any('0' <= c <= '9' for c in password),
        any(not (('A' <= c <= 'Z') or ('a' <= c <= 'z') or ('0' <= c <= '9')) for c in password),
    )
    return sum(categories) >= 3

PASSWORD_RULE_MESSAGE = 'パスワードは8〜16文字で、大文字・小文字・数字・記号の4種類のうち3種類以上を含めてください。'

def emit(kind='update'):
    with EVENT_LOCK:
        EVENTS.append({'id': time.time_ns(), 'kind': kind, 'time': time.time()})
        del EVENTS[:-100]

def init_db():
    con = db()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS cc_users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','operator')),
      salt TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      skills TEXT NOT NULL DEFAULT '[]', shift_start TEXT NOT NULL DEFAULT '09:00',
      shift_end TEXT NOT NULL DEFAULT '18:00', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS cc_demand(
      id INTEGER PRIMARY KEY AUTOINCREMENT, skill TEXT NOT NULL,
      start_time TEXT NOT NULL, end_time TEXT NOT NULL, min_staff INTEGER NOT NULL CHECK(min_staff>=0),
      UNIQUE(skill,start_time,end_time)
    );
    CREATE TABLE IF NOT EXISTS cc_attendance(
      attendance_date TEXT NOT NULL, user_id INTEGER NOT NULL, present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0,1)),
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_by INTEGER,
      PRIMARY KEY(attendance_date,user_id),
      FOREIGN KEY(user_id) REFERENCES cc_users(id), FOREIGN KEY(updated_by) REFERENCES cc_users(id)
    );
    CREATE TABLE IF NOT EXISTS cc_shift_overrides(
      work_date TEXT NOT NULL, user_id INTEGER NOT NULL,
      shift_start TEXT NOT NULL, shift_end TEXT NOT NULL,
      reason TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_by INTEGER,
      PRIMARY KEY(work_date,user_id),
      FOREIGN KEY(user_id) REFERENCES cc_users(id),
      FOREIGN KEY(updated_by) REFERENCES cc_users(id)
    );
    CREATE TABLE IF NOT EXISTS cc_schedule_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_date TEXT NOT NULL,
      status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      created_by INTEGER, FOREIGN KEY(created_by) REFERENCES cc_users(id)
    );
    CREATE TABLE IF NOT EXISTS cc_audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL,
      details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    ''')
    count = con.execute('SELECT COUNT(*) n FROM cc_users').fetchone()['n']
    if count == 0:
        salt, digest = hash_password('ChangeMe123!')
        con.execute('INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills) VALUES(?,?,?,?,?,?)',
                    ('admin','Administrator','admin',salt,digest,json.dumps(SKILLS)))
        # Sample data is deliberately small and clearly editable.
        demo = [
          ('operator1','Operator 1',['Consumer','Merchant','PP']),
          ('operator2','Operator 2',['Consumer']),
          ('operator3','Operator 3',['Merchant']),
          ('operator4','Operator 4',['PP']),
          ('operator5','Operator 5',['Consumer','Merchant']),
          ('operator6','Operator 6',['Merchant','PP']),
        ]
        for username, name, skills in demo:
            salt, digest = hash_password('Welcome123!')
            con.execute('INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills) VALUES(?,?,?,?,?,?)',
                        (username,name,'operator',salt,digest,json.dumps(skills)))
        for skill in SKILLS:
            con.execute('INSERT OR IGNORE INTO cc_demand(skill,start_time,end_time,min_staff) VALUES(?,?,?,?)',
                        (skill,'09:00','18:00',1))
    con.commit(); con.close()

def audit(user_id, action, details=None):
    con = db()
    con.execute('INSERT INTO cc_audit_log(user_id,action,details) VALUES(?,?,?)',
                (user_id, action, json.dumps(details or {}, ensure_ascii=False)))
    con.commit(); con.close()

def as_user(row):
    return {'id':row['id'],'username':row['username'],'display_name':row['display_name'],
            'role':row['role'],'active':bool(row['active']),'skills':json.loads(row['skills']),
            'shift_start':row['shift_start'],'shift_end':row['shift_end']}

def mins(t):
    h,m = map(int,t.split(':')); return h*60+m

def valid_time(t):
    try:
        return isinstance(t, str) and len(t)==5 and mins(t)%10==0 and 0<=mins(t)<24*60
    except Exception:
        return False

def hhmm(m): return f'{m//60:02d}:{m%60:02d}'

def dated_operators(con, date):
    """Effective work hours for one day: override > finalized shift > fallback.

    Once a month has been finalized, operators without a finalized assignment
    are off by default. Overrides are operational changes only; monthly shift
    assignments are kept intact for history/export.
    """
    # Admin-privileged accounts can ALSO be employees. Such employees must
    # appear as operators in the daily break attendance list, without losing
    # administrative permissions. Standalone break management retains its
    # traditional operator-only behavior.
    available_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'users','employees'}.issubset(available_tables):
        rows = [as_user(r) for r in con.execute("""SELECT cc.* FROM cc_users cc
            JOIN users su ON su.username=cc.username AND su.employee_id IS NOT NULL
                AND su.role IN ('operator','admin') AND su.approved=1
            JOIN employees e ON e.id=su.employee_id AND e.active=1
            WHERE cc.active=1 ORDER BY cc.display_name,cc.id""")]
    else:
        rows = [as_user(r) for r in con.execute(
            "SELECT * FROM cc_users WHERE role='operator' AND active=1 ORDER BY display_name,id")]
    finalized = {}
    # Standalone break-engine builds do not have shift-planner tables.
    shift_tables = {'users','shift_patterns','assignments','approvals'}
    integrated = shift_tables.issubset(available_tables)
    # Older databases / independent tests may not yet expose assignments.skill.
    has_primary = integrated and 'skill' in {r[1] for r in con.execute('PRAGMA table_info(assignments)')}
    skill_sql = 'a.skill' if has_primary else "''"
    has_break_minutes = integrated and 'break_minutes' in {r[1] for r in con.execute('PRAGMA table_info(shift_patterns)')}
    break_sql = 'p.break_minutes' if has_break_minutes else 'NULL'
    has_shift_type = integrated and 'shift_type' in {r[1] for r in con.execute('PRAGMA table_info(shift_patterns)')}
    shift_type_sql = 'p.shift_type' if has_shift_type else "'auto'"
    has_pattern_name = integrated and 'name' in {r[1] for r in con.execute('PRAGMA table_info(shift_patterns)')}
    pattern_name_sql = 'p.name' if has_pattern_name else "''"
    for r in (con.execute(f"""SELECT cc.id uid,p.start_time,p.end_time,{skill_sql} AS primary_skill,
               {break_sql} AS break_minutes, {pattern_name_sql} AS pattern_name, {shift_type_sql} AS shift_type
         FROM assignments a JOIN shift_patterns p ON p.id=a.pattern_id
         JOIN users su ON su.employee_id=a.employee_id
         JOIN cc_users cc ON cc.username=su.username AND cc.active=1
         WHERE a.work_date=? AND a.status='finalized'""", (date,)) if integrated else ()):
        finalized[r['uid']] = (r['start_time'],r['end_time'],r['primary_skill'],r['break_minutes'],r['pattern_name'],r['shift_type'])
    month_finalized = integrated and bool(con.execute(
        "SELECT 1 FROM approvals WHERE work_month=? AND action='最終確定' LIMIT 1",
        (date[:7],)).fetchone())
    overrides = {r['user_id']:r for r in con.execute(
        'SELECT * FROM cc_shift_overrides WHERE work_date=?', (date,))}
    saved = {r['user_id']:bool(r['present']) for r in con.execute(
        'SELECT user_id,present FROM cc_attendance WHERE attendance_date=?', (date,))}
    for u in rows:
        uid = u['id']
        pattern = finalized.get(uid)
        if pattern:
            planned_start, planned_end, primary, break_minutes, pattern_name, shift_type = pattern
            source = 'finalized'
        elif month_finalized:
            planned_start, planned_end = '', ''
            primary = None
            break_minutes = 0
            source = 'off'
            pattern_name, shift_type = '', 'auto'
        else:
            planned_start, planned_end = u['shift_start'], u['shift_end']
            primary = None
            break_minutes = None
            source = 'default'
            pattern_name, shift_type = '', 'auto'
        override = overrides.get(uid)
        u['primary_skill'] = primary if primary in flex.skills_of(u['skills']) else None
        u['break_minutes'] = break_minutes
        u['pattern_name'] = pattern_name
        u['shift_type'] = shift_type
        u['planned_start'] = planned_start
        u['planned_end'] = planned_end
        u['shift_start'] = override['shift_start'] if override else planned_start
        u['shift_end'] = override['shift_end'] if override else planned_end
        u['source'] = 'override' if override else source
        u['override_reason'] = override['reason'] if override else ''
        u['override_note'] = override['note'] if override else ''
        u['present'] = bool(u['shift_start'] and u['shift_end']) and saved.get(uid, True)
    return rows

def manual_break_options(user):
    """Break start options consistent with the automatic break optimizer.

    Each returned value is a ten-minute aligned local time. A missing type
    means that break is not required for this shift (e.g. a short half-day).
    """
    options = {'morning': [], 'lunch': [], 'afternoon': []}
    start, end = user.get('shift_start'), user.get('shift_end')
    if not start or not end or mins(start) >= mins(end):
        return options
    minutes = (int(user['break_minutes']) if user.get('break_minutes') is not None else
               60 if mins(end)-mins(start)>=360 else 0)
    person = {'start_time':start,'end_time':end,'break_minutes':minutes,
              'pattern_name':user.get('pattern_name'), 'shift_type':user.get('shift_type')}
    mapping={'前半10分休憩':'morning','昼休憩':'lunch','後半10分休憩':'afternoon'}
    for name,duration,_ in flex._break_specs(person):
        options[mapping[name]]=[flex.clock(v) for v in flex._break_candidates(person,name,duration)]
    return options


def _manual_skill_overrides(con, work_date):
    """Translate shift-manager temporary overrides to the break user IDs."""
    available={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'daily_skill_overrides','users','cc_users'}.issubset(available):return []
    rows=con.execute("""SELECT cc.id employee_id,o.start_time,o.end_time,o.skill
        FROM daily_skill_overrides o JOIN users su ON su.employee_id=o.employee_id
        JOIN cc_users cc ON cc.username=su.username AND cc.active=1
        WHERE o.work_date=?""",(work_date,))
    return [dict(r) for r in rows]


def _planner_inputs(cc_users, demands, work_date):
    """Normalize capitalized break skills to the shared 10-minute planner."""
    weekday = datetime.strptime(work_date, '%Y-%m-%d').weekday()
    rules=[{'weekday':weekday,'start_time':d['start_time'],'end_time':d['end_time'],
            'skill':d['skill'],'min_count':int(d['min_staff'])} for d in demands]
    people=[{'employee_id':u['id'],'name':u['display_name'],
            'start_time':u['shift_start'],'end_time':u['shift_end'],
            'skills':u['skills'], 'primary_skill':u.get('primary_skill'),
            'pattern_name':u.get('pattern_name'), 'shift_type':u.get('shift_type'),
            'break_minutes': (int(u['break_minutes']) if u.get('break_minutes') is not None else
                              60 if flex.minute(u['shift_end'])-flex.minute(u['shift_start'])>=360 else 0)}
            for u in cc_users]
    # When no finalized primary exists, assign one for this date and keep it
    # stable throughout the entire computed break plan.
    req=flex.demands_for_day(rules,weekday)
    base=flex.choose_primary_skills(people,req)
    for p in people:p['primary_skill']=base.get(p['employee_id'])
    return people,rules,req


def _break_result_from_plan(work_date,cc_users,absent,plan):
    primary=plan.get('primary_skills',{})
    schedule=[]
    lookup={p['employee_id']:p for p in plan['people']}
    segments={uid:[] for uid in lookup}
    for seg in plan['segments']:
        segments[seg['employee_id']].append(seg)
    for u in cc_users:
        uid=u['id']
        for t in flex.SLOTS:
            if not (flex.minute(u['shift_start'])<=t<flex.minute(u['shift_end'])):continue
            seg=next((x for x in segments.get(uid,()) if flex.minute(x['start'])<=t<flex.minute(x['end'])),None)
            if seg is None:continue
            resting=seg['skill']=='休憩'
            queue_skills=[] if resting else list(seg.get('skills') or flex.skills_of(seg['skill']))
            label=' / '.join({'consumer':'Consumer','merchant':'Merchant','PP':'PP'}[sk]
                           for sk in queue_skills) if queue_skills else '待機'
            schedule.append({'time':flex.clock(t),'operator_id':uid,'operator':u['display_name'],
                             'skill':label,'skills':queue_skills,
                             'preferred_skill':seg.get('preferred_skill'),
                             'on_break':resting,
                             'temporary':bool(seg.get('temporary')) and not resting})
    result_breaks={str(uid):[] for uid in lookup}
    for uid,breaks in plan['breaks'].items():
        result_breaks[uid]=[{'type':('morning' if x['type']=='前半10分休憩' else
                                      'afternoon' if x['type']=='後半10分休憩' else 'lunch'),
                            'start':x['start'],'end':x['end'],'paid':x['paid']} for x in breaks]
    shortage=[{'time':item['time'],'skill':{'consumer':'Consumer','merchant':'Merchant',
                    'PP':'PP'}[item['skill']], 'required':item['required'],
               'eligible_available':item['assigned'],'shortage':item['shortage'],
               'warning_type':'queue_minimum'} for item in plan['shortages']]
    shortage.extend({'time':item['time'],'skill':'同時着信の処理枠',
             'required':item['required_total'],'eligible_available':item['distinct_people'],
             'shortage':item['shortage'],'warning_type':'simultaneous_capacity'}
             for item in plan.get('capacity_warnings',[]))
    shortage.extend({'time':'—','skill':'休憩条件','required':'—',
        'eligible_available':'—','shortage':1,'warning_type':'break_unplaced','detail':msg}
        for msg in plan['issues'])
    return {'date':work_date,'breaks':result_breaks,'schedule':schedule,
            'slots':len(flex.SLOTS),'generated_at':datetime.now().isoformat(timespec='seconds'),
            'attendance':{'present':[{'id':u['id'],'name':u['display_name']} for u in cc_users],
                          'absent':[{'id':u['id'],'name':u['display_name']} for u in absent]},
            'primary_skills':primary,'skill_switches':plan['skill_switches'],
            'minimums_met':not bool(shortage or plan['issues']),
            'shortages':shortage[:100],
            'capacity_warnings':plan.get('capacity_warnings',[]),
            'issues':plan['issues'],
            'note':'複数スキル保持者は同時に全対応キューの受電待機対象です。受電可能人数はスキル間で重複しますが、1人が同時に2件の通話を処理できるとは計算しません。同時着信時の処理枠不足は別途警告します。休憩中はすべてのキューから外れます。'}


def operator_schedule_for_date(con, date, operator_id):
    """Read only this operator's breaks for the requested date.

    Confirmed break schedules take priority. Before break confirmation, show
    the finalized shift's *provisional* break suggestion, clearly distinguished
    from a confirmed break schedule. Never expose another operator's plan.
    """
    uid = int(operator_id)
    row = con.execute("""SELECT id, schedule_date, result_json FROM cc_schedule_runs
        WHERE schedule_date=? AND status='confirmed' ORDER BY id DESC LIMIT 1""",
        (date,)).fetchone()
    if row:
        result = json.loads(row['result_json'])
        own_breaks = result.get('breaks', {}).get(str(uid), [])
        own_schedule = [item for item in result.get('schedule', [])
                        if int(item.get('operator_id', -1)) == uid]
        return {'id': row['id'], 'date': date, 'status': 'confirmed',
                'result': {'breaks': {str(uid): own_breaks},
                           'schedule': own_schedule,
                           'note': '管理者が確定した休憩・配置予定です。'}}

    # Integrated shift mode only: this display is informational, not a
    # substitute for the administrator's separate break confirmation.
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'users', 'assignments', 'daily_skill_plans'}.issubset(tables):
        return None
    employee = con.execute("""SELECT su.employee_id FROM users su
        JOIN cc_users cu ON cu.username=su.username
        WHERE cu.id=? AND su.role IN ('operator','admin') AND su.employee_id IS NOT NULL""", (uid,)).fetchone()
    if not employee or employee['employee_id'] is None:
        return None
    employee_id = employee['employee_id']
    finalized = con.execute("""SELECT 1 FROM assignments
        WHERE employee_id=? AND work_date=? AND status='finalized' LIMIT 1""",
        (employee_id, date)).fetchone()
    if not finalized:
        return None
    planned = con.execute("SELECT plan_json FROM daily_skill_plans WHERE work_date=?",
                          (date,)).fetchone()
    if not planned:
        return {'id': None, 'date': date, 'status': 'provisional',
                'result': {'breaks': {str(uid): []}, 'schedule': [],
                           'note': '勤務シフトは確定していますが、休憩案はまだありません。管理者に作成を依頼してください。'}}
    plan = json.loads(planned['plan_json'])
    own_breaks = []
    for item in plan.get('breaks', {}).get(str(employee_id), []):
        label = str(item.get('type', ''))
        kind = ('morning' if '前半' in label else
                'afternoon' if '後半' in label else 'lunch')
        own_breaks.append({'type': kind, 'start': item['start'],
                           'end': item['end'], 'paid': bool(item.get('paid', False))})
    own_schedule = []
    for segment in plan.get('segments', []):
        if int(segment.get('employee_id', -1)) != employee_id:
            continue
        start, end = mins(segment['start']), mins(segment['end'])
        if end <= start:
            continue
        resting = segment['skill'] == '休憩'
        queue_skills=[] if resting else list(segment.get('skills') or flex.skills_of(segment['skill']))
        label=' / '.join({'consumer':'Consumer','merchant':'Merchant','PP':'PP'}[sk]
                       for sk in queue_skills) if queue_skills else '待機'
        for minute in range(start, end, 10):
            own_schedule.append({'time': hhmm(minute), 'operator_id': uid,
                                 'skill': label,'skills':queue_skills,
                                 'preferred_skill':segment.get('preferred_skill'),
                                 'on_break': resting,
                                 'temporary': bool(segment.get('temporary', False)) and not resting})
    own_schedule.sort(key=lambda item: item['time'])
    return {'id': None, 'date': date, 'status': 'provisional',
            'result': {'breaks': {str(uid): own_breaks},
                       'schedule': own_schedule,
                       'note': 'シフト確定後の休憩仮案です。休憩管理者が確定するまでは変更される場合があります。'}}


def generate_schedule(date, actor_id):
    """Break drafts use the same primary-first staffing model as monthly shifts."""
    con=db()
    operators=dated_operators(con,date)
    present=[u for u in operators if u['present'] and u['shift_start'] and u['shift_end']]
    absent=[u for u in operators if u not in present]
    demands=[dict(r) for r in con.execute('SELECT * FROM cc_demand ORDER BY start_time,skill')]
    overrides=_manual_skill_overrides(con,date)
    con.close()
    if not present:
        return {'ok':False,'errors':['出勤者がいないため、配置案を作成できません。']}
    people,rules,req=_planner_inputs(present,demands,date)
    plan=flex.build_day_plan(date,people,rules,overrides)
    result=_break_result_from_plan(date,present,absent,plan)
    con=db()
    cur=con.execute('INSERT INTO cc_schedule_runs(schedule_date,status,result_json,created_by) VALUES(?,?,?,?)',
                    (date,'draft',json.dumps(result,ensure_ascii=False),actor_id))
    run_id=cur.lastrowid
    con.commit();con.close()
    audit(actor_id,'schedule_generated',{'run_id':run_id,'primary_preference':True,'all_skills_eligible':True})
    emit('schedule')
    return {'ok':True,'run_id':run_id,'result':result}

def create_manual_schedule(date, actor_id, submitted_breaks):
    """Validate a user-authored break plan and save it only if every staffing minimum is met."""
    con = db()
    all_users = dated_operators(con, date)
    cc_users = [u for u in all_users if u['present'] and u['shift_start'] and u['shift_end']]
    absent_users = [u for u in all_users if u not in cc_users]
    demands = [dict(r) for r in con.execute('SELECT * FROM cc_demand ORDER BY start_time,skill')]
    overrides = _manual_skill_overrides(con,date)
    con.close()
    if not cc_users:
        return {'ok': False, 'errors': ['出勤者がいないため、配置案を作成できません。']}

    by_id = {u['id']: u for u in cc_users}
    if not isinstance(submitted_breaks, list) or {int(x.get('user_id', -1)) for x in submitted_breaks if isinstance(x, dict) and str(x.get('user_id', '')).isdigit()} != set(by_id) or len(submitted_breaks) != len(by_id):
        return {'ok': False, 'errors': ['出勤者全員の午前・昼・午後の休憩時刻を指定してください。']}

    breaks = {u['id']: [] for u in cc_users}
    occupied = {u['id']: set() for u in cc_users}
    slots = set(flex.SLOTS)  # Includes evening coverage through 23:00.
    labels={'morning':'午前休憩','lunch':'昼休憩','afternoon':'午後休憩'}
    for item in submitted_breaks:
        uid = int(item['user_id']); user = by_id[uid]
        options = manual_break_options(user)
        person={'start_time':user['shift_start'],'end_time':user['shift_end'],
                'break_minutes':(int(user['break_minutes']) if user.get('break_minutes') is not None
                                 else 60 if mins(user['shift_end'])-mins(user['shift_start'])>=360 else 0),
                'pattern_name':user.get('pattern_name'), 'shift_type':user.get('shift_type')}
        durations={('morning' if name=='前半10分休憩' else
                    'afternoon' if name=='後半10分休憩' else 'lunch'):dur
                   for name,dur,_ in flex._break_specs(person)}
        for kind in ('morning', 'lunch', 'afternoon'):
            start_text = item.get(kind, '') or ''
            if kind not in durations:
                if start_text:
                    return {'ok':False,'errors':[f"{user['display_name']}には{labels[kind]}が設定されていません。"]}
                continue
            valid_options=options[kind]
            if not valid_options:
                return {'ok':False,'errors':[f"{user['display_name']}の{labels[kind]}を勤務時間内の許可範囲に配置できません。勤務パターンを確認してください。"]}
            if not valid_time(start_text) or start_text not in valid_options:
                window_message=('早番11:30〜13:00／中番13:00〜15:30／遅番15:00〜17:30の開始範囲'
                                if kind=='lunch' else '勤務時間と休憩順序に応じた10分刻み')
                return {'ok':False,'errors':[f"{user['display_name']}の{labels[kind]}は{window_message}時刻を選択してください。"]}
            start = mins(start_text)
            duration=durations[kind]
            block = set(range(start,start+duration,10))
            if any(s not in slots or s < mins(user['shift_start']) or s >= mins(user['shift_end']) for s in block):
                return {'ok':False,'errors':[f"{user['display_name']}の休憩が勤務時間外に設定されています。"]}
            if occupied[uid] & block:
                return {'ok':False,'errors':[f"{user['display_name']}の休憩時間が重複しています。"]}
            occupied[uid].update(block)
            breaks[uid].append({'type':kind,'start':start_text,'end':hhmm(start+duration)})
        # A late shift may be lunch-first, followed by BOTH paid 10-minute
        # breaks. Early/middle shifts keep morning > lunch > afternoon order.
        by_kind={row['type']:mins(row['start']) for row in breaks[uid]}
        if not flex.valid_break_sequence(person,by_kind):
            return {'ok':False,'errors':[
                f"{user['display_name']}の休憩順序が正しくありません。通常は前半10分→昼→後半10分、遅番は昼→前半10分→後半10分も選べます（休憩間は20分以上）。"]}
        # The actual timeline, not the form field order, is what operators see.
        breaks[uid].sort(key=lambda row: row['start'])

    # Manual breaks keep the daily primary as a preference, but every skill
    # the operator is qualified for remains eligible throughout the shift.
    people,rules,req=_planner_inputs(cc_users,demands,date)
    forced={t:{} for t in flex.SLOTS}
    for o in overrides:
        if o['skill'] not in flex.SKILLS:continue
        for t in flex.SLOTS:
            if flex.minute(o['start_time'])<=t<flex.minute(o['end_time']):
                forced[t][o['employee_id']]=o['skill']
    decisions,auto_slots=flex.plan_temporary_switches(people,req,occupied,forced)
    schedule=[];shortages=[];capacity_warnings=[]
    for s in flex.SLOTS:
        chosen=decisions[s]
        ready,physical_gaps,distinct=flex.simultaneous_queue_coverage(people,s,req[s],occupied)
        for sk in flex.SKILLS:
            if ready[sk]<req[s][sk]:
                shortages.append({'time':flex.clock(s),'skill':{'consumer':'Consumer','merchant':'Merchant','PP':'PP'}[sk],
                    'required':req[s][sk],'eligible_available':ready[sk],
                    'shortage':req[s][sk]-ready[sk],'warning_type':'queue_minimum'})
        if any(physical_gaps.values()):
            gap=sum(physical_gaps.values())
            capacity_warnings.append({'time':flex.clock(s),'required_total':sum(req[s].values()),
                                      'distinct_people':distinct,'shortage':gap,'by_skill':physical_gaps})
            shortages.append({'time':flex.clock(s),'skill':'同時着信の処理枠',
                'required':sum(req[s].values()),'eligible_available':distinct,
                'shortage':gap,'warning_type':'simultaneous_capacity'})
        for u in cc_users:
            uid=u['id']
            if not (flex.minute(u['shift_start'])<=s<flex.minute(u['shift_end'])):continue
            rest=s in occupied[uid]
            queue_skills=[] if rest else list(flex.skills_of(u['skills']))
            label=' / '.join({'consumer':'Consumer','merchant':'Merchant','PP':'PP'}[sk]
                           for sk in queue_skills) if queue_skills else '待機'
            preferred=chosen.get(uid)
            schedule.append({'time':flex.clock(s),'operator_id':uid,'operator':u['display_name'],
                             'skill':label,'skills':queue_skills,
                             'preferred_skill':preferred,
                             'on_break':rest,'temporary':not rest and preferred is not None and
                                 preferred != next(p['primary_skill'] for p in people if p['employee_id']==uid)})
    result={'date':date,'breaks':{str(k):v for k,v in breaks.items()},'schedule':schedule,
            'slots':len(flex.SLOTS),'generated_at':datetime.now().isoformat(timespec='seconds'),
            'attendance':{'present':[{'id':u['id'],'name':u['display_name']} for u in cc_users],
                          'absent':[{'id':u['id'],'name':u['display_name']} for u in absent_users]},
            'primary_skills':{str(p['employee_id']):p['primary_skill'] for p in people},
            'minimums_met':not bool(shortages),'shortages':shortages[:100],
            'capacity_warnings':capacity_warnings,
            'note':'管理者指定の休憩を使用。複数スキル保持者は全対応キューで同時受電待機可能です。重複する受電待機人数とは別に、1人1通話で同時着信を処理する際の不足を警告します。'}
    con = db()
    cur = con.execute('INSERT INTO cc_schedule_runs(schedule_date,status,result_json,created_by) VALUES(?,?,?,?)',
                       (date, 'draft', json.dumps(result, ensure_ascii=False), actor_id))
    run_id = cur.lastrowid
    con.commit(); con.close()
    audit(actor_id, 'manual_schedule_generated', {'run_id': run_id, 'date': date})
    emit('schedule')
    return {'ok': True, 'run_id': run_id, 'result': result}

def assign_slot_partial(cc_users, slot, requirements, occupied):
    """Build the best possible single-skill assignment and return explicit unmet minima."""
    available = [u for u in cc_users if mins(u['shift_start']) <= slot < mins(u['shift_end']) and slot not in occupied[u['id']]]
    remaining = list(available); chosen = {}; shortages = []
    for skill in sorted(SKILLS, key=lambda sk: sum(sk in u['skills'] for u in remaining)):
        need = int(requirements.get(skill, 0))
        candidates = [u for u in remaining if skill in u['skills']]
        candidates.sort(key=lambda u: (sum(x in u['skills'] for x in SKILLS), u['id']))
        for u in candidates[:need]:
            chosen[u['id']] = skill; remaining.remove(u)
        if len(candidates) < need:
            shortages.append({'time': hhmm(slot), 'skill': skill, 'required': need, 'eligible_available': len(candidates), 'shortage': need-len(candidates)})
    for u in remaining:
        possible = [sk for sk in SKILLS if sk in u['skills']]
        if possible:
            counts = {sk: sum(1 for x in chosen.values() if x == sk) for sk in SKILLS}
            chosen[u['id']] = min(possible, key=lambda sk: counts[sk])
    return chosen, shortages

def find_shortages(cc_users, slots, req, occupied):
    out=[]
    for s in slots:
        available=[u for u in cc_users if mins(u['shift_start'])<=s<mins(u['shift_end']) and s not in occupied[u['id']]]
        for skill in SKILLS:
            have=sum(1 for u in available if skill in u['skills'])
            if have<req[s][skill]: out.append({'time':hhmm(s),'skill':skill,'required':req[s][skill],'eligible_available':have})
    return out[:30]

def parse_employee_file(filename, payload):
    """Read first worksheet from .xlsx or CSV, no external dependencies."""
    if len(payload)>2_000_000: raise ValueError('ファイルは2MB以下にしてください。')
    lower=filename.lower()
    if lower.endswith('.csv'):
        content=None
        for encoding in ('utf-8-sig','cp932'):
            try: content=payload.decode(encoding); break
            except UnicodeDecodeError: pass
        if content is None: raise ValueError('CSVの文字コードを読み取れません。')
        rows=list(csv.reader(io.StringIO(content)))
    elif lower.endswith('.xlsx'):
        ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main', 'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                if len(z.namelist())>1500 or sum(i.file_size for i in z.infolist())>20_000_000: raise ValueError('Excelファイルが大きすぎます。')
                shared=[]
                if 'xl/sharedStrings.xml' in z.namelist():
                    root=ET.fromstring(z.read('xl/sharedStrings.xml'))
                    shared=[''.join(t.text or '' for t in si.findall('.//m:t',ns)) for si in root.findall('m:si',ns)]
                root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                rows=[]
                for r in root.findall('.//m:sheetData/m:row',ns):
                    values={}
                    for c in r.findall('m:c',ns):
                        ref=c.get('r',''); m=re.match(r'[A-Z]+',ref)
                        if not m:continue
                        col=0
                        for letter in m.group():col=col*26+ord(letter)-64
                        if col>30:continue
                        v=c.find('m:v',ns)
                        if c.get('t')=='inlineStr': value=''.join(x.text or '' for x in c.findall('.//m:t',ns))
                        elif v is None:value=''
                        elif c.get('t')=='s':value=shared[int(v.text)]
                        else:value=v.text or ''
                        values[col-1]=value
                    rows.append([values.get(i,'') for i in range(max(values.keys(),default=-1)+1)])
        except (zipfile.BadZipFile,KeyError,ET.ParseError,IndexError,TypeError) as e: raise ValueError('Excelファイルを読み取れません。1枚目のシートを確認してください。') from e
    else: raise ValueError('対応形式は .xlsx または .csv です。')
    rows=[r for r in rows if any(str(v).strip() for v in r)]
    if not rows:raise ValueError('ファイルにデータがありません。')
    headers=[str(x).strip().lstrip('\ufeff') for x in rows[0]]
    aliases={'ユーザー名':'username','ログインID':'username','username':'username','表示名':'display_name','氏名':'display_name','display_name':'display_name','パスワード':'password','password':'password','スキル':'skills','対応スキル':'skills','skills':'skills','勤務開始':'shift_start','shift_start':'shift_start','勤務終了':'shift_end','shift_end':'shift_end'}
    mapped=[aliases.get(h) for h in headers]
    if len(set(x for x in mapped if x))!=len([x for x in mapped if x]):raise ValueError('重複する列名があります。')
    required={'username','display_name','password','skills','shift_start','shift_end'}
    if not required.issubset(set(mapped)):raise ValueError('必須列：ユーザー名、表示名、パスワード、スキル、勤務開始、勤務終了')
    if len(rows)-1>300:raise ValueError('一度に登録できるのは300人までです。')
    employees=[]; errors=[]; seen=set()
    for idx,row in enumerate(rows[1:],start=2):
        record={key:str(row[i]).strip() if i<len(row) else '' for i,key in enumerate(mapped) if key}
        username=record['username']; name=record['display_name']; password=record['password']
        skilltext=record['skills'].replace('、',',').replace('／',',').replace('/',',').replace(';',',')
        skills=[x.strip() for x in skilltext.split(',') if x.strip()]
        start=record['shift_start']; end=record['shift_end']
        if not username or not name or len(username)>100 or len(name)>100: errors.append(f'{idx}行目：ユーザー名・表示名を確認してください。')
        if username in seen:errors.append(f'{idx}行目：ファイル内でユーザー名が重複しています。')
        seen.add(username)
        if not valid_password(password):errors.append(f'{idx}行目：パスワードの条件を満たしていません。')
        if not skills or len(skills)!=len(set(skills)) or any(x not in SKILLS for x in skills):errors.append(f'{idx}行目：スキルは Consumer, Merchant, PP をカンマ区切りで指定してください。')
        if not valid_time(start) or not valid_time(end) or (valid_time(start) and valid_time(end) and mins(start)>=mins(end)):errors.append(f'{idx}行目：勤務時刻を10分刻みで正しく指定してください。')
        employees.append((username,name,password,skills,start,end))
    if not employees:raise ValueError('登録対象がありません。')
    if errors:raise ValueError('\n'.join(errors[:15]))
    return employees

class Handler(BaseHTTPRequestHandler):
    server_version='CallCenterLocal/1.0'
    def log_message(self, fmt, *args):
        # Keep logs concise and avoid leaking request bodies or credentials.
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt%args))
    def send_json(self, data, status=200):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>100_000_000: raise ValueError('request too large')
        return json.loads(self.rfile.read(n).decode() or '{}')
    def current_user(self):
        token=self.headers.get('X-Session-Token') or parse_qs(urlparse(self.path).query).get('token',[''])[0]
        sess=SESSIONS.get(token)
        if not sess or sess['expires']<time.time(): return None
        con=db(); row=con.execute('SELECT * FROM cc_users WHERE id=? AND active=1',(sess['user_id'],)).fetchone(); con.close()
        return as_user(row) if row else None
    def require(self, admin=False):
        user=self.current_user()
        if not user: self.send_json({'error':'ログインしてください。'},401); return None
        if admin and user['role']!='admin': self.send_json({'error':'管理者権限が必要です。'},403); return None
        return user
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/' or path=='/index.html':
            f=open(os.path.join(RESOURCE_DIR,'static','index.html'),'rb').read(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(f))); self.end_headers(); self.wfile.write(f); return
        if path=='/api/health': self.send_json({'ok':True,'app':'Call Center Local','version':'0.1.0'}); return
        if path=='/api/me':
            u=self.require();
            if u: self.send_json({'user':u})
            return
        if path=='/api/users':
            u=self.require(admin=True)
            if not u:return
            con=db(); rows=con.execute('SELECT * FROM cc_users WHERE active=1 ORDER BY role DESC,display_name').fetchall(); con.close(); self.send_json({'users':[as_user(r) for r in rows]}); return
        if path=='/api/my-work-hours':
            u=self.require()
            if not u:return
            date=parse_qs(urlparse(self.path).query).get('date',[''])[0]
            try: datetime.strptime(date,'%Y-%m-%d')
            except ValueError: self.send_json({'error':'日付の形式が正しくありません。'},400);return
            con=db()
            my_hours=next((r for r in dated_operators(con,date) if r['id']==u['id']),None)
            con.close()
            self.send_json({'date':date,'work_hours':my_hours});return
        if path=='/api/attendance':
            u=self.require(admin=True)
            if not u:return
            date=parse_qs(urlparse(self.path).query).get('date',[''])[0]
            try: datetime.strptime(date,'%Y-%m-%d')
            except ValueError: self.send_json({'error':'日付の形式が正しくありません。'},400); return
            con=db()
            rows=dated_operators(con,date)
            con.close()
            for operator in rows:
                operator['break_options'] = manual_break_options(operator)
                shift_start,shift_end=operator.get('shift_start'),operator.get('shift_end')
                if shift_start and shift_end and mins(shift_end)>mins(shift_start):
                    rest=(int(operator['break_minutes']) if operator.get('break_minutes') is not None else
                          60 if mins(shift_end)-mins(shift_start)>=360 else 0)
                    specs=flex._break_specs({'start_time':shift_start,'end_time':shift_end,'break_minutes':rest})
                    mapping={'前半10分休憩':'morning','昼休憩':'lunch','後半10分休憩':'afternoon'}
                    needed={mapping[name] for name,_,_ in specs}
                else:
                    needed=set()
                operator['break_required']={kind:kind in needed for kind in ('morning','lunch','afternoon')}
            self.send_json({'date':date,'operators':rows}); return
        if path=='/api/demand':
            u=self.require(admin=True)
            if not u:return
            con=db(); rows=[dict(r) for r in con.execute('SELECT * FROM cc_demand ORDER BY start_time,skill')]; con.close(); self.send_json({'demand':rows,'skills':SKILLS}); return
        if path=='/api/schedule/latest':
            u=self.require()
            if not u:return
            date=parse_qs(urlparse(self.path).query).get('date',[''])[0]
            if date:
                try:
                    if datetime.strptime(date,'%Y-%m-%d').date().isoformat()!=date:
                        raise ValueError()
                except ValueError:
                    self.send_json({'error':'日付の形式が正しくありません。'},400);return
            if u['role']!='admin':
                # The operator must never get the globally latest *other day*.
                # Unspecified dates default to today, not an arbitrary result.
                date=date or datetime.now().date().isoformat()
                con=db()
                try: run=operator_schedule_for_date(con,date,u['id'])
                finally: con.close()
                self.send_json({'run':run});return
            con=db()
            if date:
                row=con.execute('SELECT * FROM cc_schedule_runs WHERE schedule_date=? ORDER BY id DESC LIMIT 1',(date,)).fetchone()
            else:
                row=con.execute('SELECT * FROM cc_schedule_runs ORDER BY id DESC LIMIT 1').fetchone()
            con.close()
            if not row:self.send_json({'run':None}); return
            self.send_json({'run':{'id':row['id'],'date':row['schedule_date'],
                'status':row['status'],'result':json.loads(row['result_json'])}});return
        if path=='/api/audit':
            u=self.require(admin=True)
            if not u:return
            con=db(); rows=[dict(r) for r in con.execute('SELECT a.id,a.action,a.details,a.created_at,u.username FROM cc_audit_log a LEFT JOIN cc_users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100')]; con.close(); self.send_json({'logs':rows}); return
        if path=='/api/events':
            u=self.require()
            if not u:return
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Cache-Control','no-cache'); self.send_header('Connection','keep-alive'); self.end_headers()
            last=time.time(); end=time.time()+20
            try:
                while time.time()<end:
                    with EVENT_LOCK: fresh=[e for e in EVENTS if e['time']>last]
                    if fresh:
                        for e in fresh: self.wfile.write(('data: '+json.dumps(e)+'\n\n').encode())
                        self.wfile.flush(); last=max(e['time'] for e in fresh)
                    else:
                        self.wfile.write(b': ping\n\n'); self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError,ConnectionResetError): pass
            return
        self.send_json({'error':'not found'},404)
    def do_POST(self):
        path=urlparse(self.path).path
        try: data=self.body()
        except Exception as e: self.send_json({'error':'不正なリクエストです。'},400); return
        if path=='/api/register':
            # Public self-registration is always an inactive operator until an admin approves it.
            username=str(data.get('username','')).strip(); name=str(data.get('display_name','')).strip()
            password=data.get('password',''); skills=data.get('skills',[])
            start=data.get('shift_start','09:00'); end=data.get('shift_end','18:00')
            if not username or len(username)>100 or not name or len(name)>100 or not valid_password(password) or not isinstance(skills,list) or not skills or any(x not in SKILLS for x in skills) or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end):
                self.send_json({'error':'登録情報を確認してください。パスワードは8〜16文字・3種類以上の文字種が必要です。'},400); return
            salt,digest=hash_password(password)
            try:
                con=db(); cur=con.execute('INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end,active) VALUES(?,?,?,?,?,?,?,?,0)',(username,name,'operator',salt,digest,json.dumps(skills),start,end)); con.commit(); uid=cur.lastrowid; con.close()
            except sqlite3.IntegrityError:
                self.send_json({'error':'このユーザー名は既に使われています。'},409); return
            audit(None,'operator_registration_requested',{'user_id':uid}); emit('users')
            self.send_json({'ok':True,'message':'登録申請を受け付けました。管理者の承認後にログインできます。'},201); return
        if path=='/api/login':
            username=str(data.get('username',''))[:100]; password=str(data.get('password',''))
            con=db(); row=con.execute('SELECT * FROM cc_users WHERE username=? AND active=1',(username,)).fetchone(); con.close()
            if not row or not verify_password(password,row['salt'],row['password_hash']): self.send_json({'error':'ユーザー名またはパスワードが違います。'},401); return
            token=secrets.token_urlsafe(32); SESSIONS[token]={'user_id':row['id'],'expires':time.time()+12*3600}
            audit(row['id'],'login'); self.send_json({'token':token,'user':as_user(row)}); return
        if path=='/api/logout':
            token=self.headers.get('X-Session-Token',''); user=self.current_user()
            if token in SESSIONS: del SESSIONS[token]
            if user:audit(user['id'],'logout')
            self.send_json({'ok':True}); return
        if path=='/api/password':
            actor=self.require()
            if not actor:return
            old=str(data.get('old_password','')); new=str(data.get('new_password',''))
            if not valid_password(new):
                self.send_json({'error':PASSWORD_RULE_MESSAGE},400); return
            con=db(); row=con.execute('SELECT * FROM cc_users WHERE id=?',(actor['id'],)).fetchone()
            if not row or not verify_password(old,row['salt'],row['password_hash']):
                con.close(); self.send_json({'error':'現在のパスワードが違います。'},403); return
            salt,digest=hash_password(new)
            con.execute('UPDATE cc_users SET salt=?,password_hash=? WHERE id=?',(salt,digest,actor['id'])); con.commit(); con.close()
            audit(actor['id'],'password_changed'); emit('users'); self.send_json({'ok':True}); return
        if path=='/api/restore':
            actor=self.require(admin=True)
            if not actor:return
            try:
                raw=base64.b64decode(data.get('backup_base64',''),validate=True)
                if not raw or len(raw)>80_000_000: raise ValueError('invalid backup size')
                with tempfile.NamedTemporaryFile(prefix='restore-',suffix='.sqlite3',dir=DATA_DIR,delete=False) as tmp:
                    tmp.write(raw); tmp_path=tmp.name
                try:
                    source=sqlite3.connect(tmp_path)
                    check=source.execute('PRAGMA integrity_check').fetchone()[0]
                    tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    required={'users','demand','schedule_runs','audit_log'}
                    if check!='ok' or not required.issubset(tables): raise ValueError('invalid database')
                    dest=db()
                    source.backup(dest)
                    dest.execute('CREATE TABLE IF NOT EXISTS cc_attendance(attendance_date TEXT NOT NULL,user_id INTEGER NOT NULL,present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0,1)),updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_by INTEGER,PRIMARY KEY(attendance_date,user_id),FOREIGN KEY(user_id) REFERENCES cc_users(id),FOREIGN KEY(updated_by) REFERENCES cc_users(id))')
                    dest.commit(); dest.close(); source.close()
                finally:
                    try: os.remove(tmp_path)
                    except OSError: pass
            except Exception:
                self.send_json({'error':'バックアップを復元できません。ファイル形式と内容を確認してください。'},400); return
            audit(actor['id'],'database_restored'); emit('restore'); self.send_json({'ok':True}); return
        if path=='/api/backup':
            actor=self.require(admin=True)
            if not actor:return
            import io
            con=db(); target=sqlite3.connect(':memory:')
            con.backup(target); target.commit(); payload=target.serialize(); target.close(); con.close()
            self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Disposition','attachment; filename=callcenter-backup.sqlite3'); self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
            audit(actor['id'],'database_backup'); return
        if path=='/api/users/import':
            actor=self.require(admin=True)
            if not actor:return
            try:
                filename=str(data.get('filename',''))
                encoded=str(data.get('file_base64',''))
                if len(encoded)>2_700_000:raise ValueError('ファイルは2MB以下にしてください。')
                payload=base64.b64decode(encoded,validate=True)
                employees=parse_employee_file(filename,payload)
                con=db()
                try:
                    con.execute('BEGIN IMMEDIATE')
                    names=[u[0] for u in employees]
                    existing={r['username'] for r in con.execute('SELECT username FROM cc_users WHERE username IN ('+','.join('?' for _ in names)+')',names)}
                    if existing:raise ValueError('登録済みユーザー名：'+', '.join(sorted(existing)[:10]))
                    for username,name,password,skills,start,end in employees:
                        salt,digest=hash_password(password)
                        con.execute('INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end) VALUES(?,?,?,?,?,?,?,?)',(username,name,'operator',salt,digest,json.dumps(skills),start,end))
                    con.commit()
                except Exception:
                    con.rollback();raise
                finally:con.close()
                audit(actor['id'],'operators_imported',{'count':len(employees)})
                emit('users');self.send_json({'ok':True,'created':len(employees)},201)
            except (ValueError,sqlite3.IntegrityError,base64.binascii.Error) as e:self.send_json({'error':str(e)},400)
            return
        if path=='/api/users':
            actor=self.require(admin=True)
            if not actor:return
            username=str(data.get('username','')).strip(); name=str(data.get('display_name','')).strip(); role=data.get('role','operator')
            skills=data.get('skills',[]); start=data.get('shift_start','09:00'); end=data.get('shift_end','18:00'); password=str(data.get('password',''))
            if not username or not name or role not in ('admin','operator') or not valid_password(password) or not isinstance(skills,list) or any(s not in SKILLS for s in skills) or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end): self.send_json({'error':('入力を確認してください。' if valid_password(password) else PASSWORD_RULE_MESSAGE)},400); return
            salt,digest=hash_password(password)
            try:
                con=db(); cur=con.execute('INSERT INTO cc_users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end) VALUES(?,?,?,?,?,?,?,?)',(username,name,role,salt,digest,json.dumps(skills),start,end)); con.commit(); uid=cur.lastrowid; con.close()
            except sqlite3.IntegrityError: self.send_json({'error':'このユーザー名は既に使われています。'},409); return
            audit(actor['id'],'user_created',{'user_id':uid}); emit('users'); self.send_json({'ok':True,'id':uid},201); return
        if path=='/api/demand':
            actor=self.require(admin=True)
            if not actor:return
            skill=data.get('skill'); start=data.get('start_time'); end=data.get('end_time'); count=data.get('min_staff')
            try:
                if skill not in SKILLS or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end) or int(count)<0 or int(count)>200: raise ValueError()
                con=db(); con.execute('INSERT INTO cc_demand(skill,start_time,end_time,min_staff) VALUES(?,?,?,?) ON CONFLICT(skill,start_time,end_time) DO UPDATE SET min_staff=excluded.min_staff',(skill,start,end,int(count))); con.commit(); con.close()
            except Exception: self.send_json({'error':'スキル・時間帯・人数を確認してください。'},400); return
            audit(actor['id'],'demand_updated',data); emit('demand'); self.send_json({'ok':True}); return
        if path=='/api/work-hours':
            actor=self.require(admin=True)
            if not actor:return
            date=str(data.get('date',''))
            try:
                datetime.strptime(date,'%Y-%m-%d')
                uid=int(data.get('user_id'))
                start=data.get('shift_start'); end=data.get('shift_end')
                reason=str(data.get('reason','')).strip()
                note=str(data.get('note','')).strip()
                if not valid_time(start) or not valid_time(end) or mins(start)>=mins(end): raise ValueError('勤務時間は10分刻み、終了は開始より後にしてください。')
                if reason not in ('遅刻','早退','遅刻・早退','勤務時間変更','臨時出勤','その他'):
                    raise ValueError('変更理由を選択してください。')
                if len(note)>200:raise ValueError('備考は200文字以内にしてください。')
                con=db()
                active=next((r for r in dated_operators(con,date) if r['id']==uid),None)
                if not active:
                    con.close(); self.send_json({'error':'有効なオペレーターが見つかりません。'},404);return
                con.execute("""INSERT INTO cc_shift_overrides(work_date,user_id,shift_start,shift_end,reason,note,updated_at,updated_by)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(work_date,user_id) DO UPDATE SET
                   shift_start=excluded.shift_start,shift_end=excluded.shift_end,
                   reason=excluded.reason,note=excluded.note,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                   (date,uid,start,end,reason,note,datetime.now().isoformat(timespec='seconds'),actor['id']))
                # An operational adjustment may turn a day off into a workday.
                con.execute("""INSERT INTO cc_attendance(attendance_date,user_id,present,updated_at,updated_by)
                   VALUES(?,?,1,?,?) ON CONFLICT(attendance_date,user_id) DO UPDATE SET
                   present=1,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                   (date,uid,datetime.now().isoformat(timespec='seconds'),actor['id']))
                con.commit();con.close()
            except (ValueError,TypeError) as e:
                self.send_json({'error':str(e) or '変更内容を確認してください。'},400);return
            audit(actor['id'],'daily_work_hours_updated',{'date':date,'user_id':uid,'start':start,'end':end,'reason':reason,'note':note})
            emit('attendance');self.send_json({'ok':True});return
        if path=='/api/attendance':
            actor=self.require(admin=True)
            if not actor:return
            date=str(data.get('date',''))
            entries=data.get('attendance')
            try:
                datetime.strptime(date,'%Y-%m-%d')
                if not isinstance(entries,list): raise ValueError()
                con=db()
                operator_rows=dated_operators(con,date)
                active_ids={r['id'] for r in operator_rows}
                allowed_present={r['id'] for r in operator_rows if r['shift_start'] and r['shift_end']}
                submitted=set()
                for item in entries:
                    uid=int(item['user_id'])
                    if uid not in active_ids or uid in submitted or not isinstance(item.get('present'),bool) or (item['present'] and uid not in allowed_present): raise ValueError()
                    submitted.add(uid)
                if submitted != active_ids: raise ValueError()
                con.execute('BEGIN IMMEDIATE')
                for item in entries:
                    con.execute('INSERT INTO cc_attendance(attendance_date,user_id,present,updated_at,updated_by) VALUES(?,?,?,?,?) ON CONFLICT(attendance_date,user_id) DO UPDATE SET present=excluded.present,updated_at=excluded.updated_at,updated_by=excluded.updated_by', (date,int(item['user_id']),1 if item['present'] else 0,datetime.now().isoformat(timespec='seconds'),actor['id']))
                con.commit(); con.close()
            except Exception:
                try: con.close()
                except Exception: pass
                self.send_json({'error':'出勤状況を保存できません。日付と全オペレーターのチェック状態を確認してください。'},400); return
            audit(actor['id'],'attendance_updated',{'date':date,'present_count':sum(1 for x in entries if x['present']),'total':len(entries)}); emit('attendance'); self.send_json({'ok':True,'date':date}); return
        if path=='/api/schedule/generate':
            actor=self.require(admin=True)
            if not actor:return
            date=str(data.get('date',datetime.now().date().isoformat()))
            try: datetime.strptime(date,'%Y-%m-%d')
            except ValueError: self.send_json({'error':'日付の形式が正しくありません。'},400); return
            result=generate_schedule(date,actor['id']); self.send_json(result,200 if result.get('ok') else 422); return
        if path=='/api/schedule/manual':
            actor=self.require(admin=True)
            if not actor:return
            date=str(data.get('date',''))
            try: datetime.strptime(date,'%Y-%m-%d')
            except ValueError: self.send_json({'error':'日付の形式が正しくありません。'},400); return
            result=create_manual_schedule(date,actor['id'],data.get('breaks'))
            self.send_json(result,200 if result.get('ok') else 422); return
        if path=='/api/schedule/confirm':
            actor=self.require(admin=True)
            if not actor:return
            run_id=data.get('run_id'); stage=data.get('override_stage')
            con=db(); row=con.execute("SELECT * FROM cc_schedule_runs WHERE id=? AND status='draft'",(run_id,)).fetchone()
            if not row: con.close(); self.send_json({'error':'確定できる下書きが見つかりません。'},404); return
            result=json.loads(row['result_json']); warnings=result.get('shortages',[])
            if warnings:
                if stage == 1:
                    result['override_stage1_by']=actor['id']; result['override_stage1_at']=datetime.now().isoformat(timespec='seconds')
                    con.execute('UPDATE cc_schedule_runs SET result_json=? WHERE id=?',(json.dumps(result,ensure_ascii=False),run_id)); con.commit(); con.close()
                    audit(actor['id'],'schedule_override_stage1',{'run_id':run_id,'shortage_count':len(warnings)})
                    self.send_json({'ok':True,'needs_second_confirmation':True,'shortages':warnings[:100]}); return
                if stage != 2 or result.get('override_stage1_by') != actor['id']:
                    con.close(); self.send_json({'error':'最低稼働人数不足があります。1段階目の確認を先に完了してください。','needs_first_confirmation':True,'shortages':warnings[:100]},409); return
                result['override_stage2_by']=actor['id']; result['override_stage2_at']=datetime.now().isoformat(timespec='seconds')
                con.execute("UPDATE cc_schedule_runs SET status='confirmed',result_json=? WHERE id=?",(json.dumps(result,ensure_ascii=False),run_id))
                con.commit(); con.close(); audit(actor['id'],'schedule_confirmed_with_staffing_override',{'run_id':run_id,'shortages':warnings[:100]}); emit('schedule')
                self.send_json({'ok':True,'override':True}); return
            con.execute("UPDATE cc_schedule_runs SET status='confirmed' WHERE id=?",(run_id,)); con.commit(); con.close(); audit(actor['id'],'schedule_confirmed',{'run_id':run_id}); emit('schedule'); self.send_json({'ok':True}); return
        self.send_json({'error':'not found'},404)
    def do_PUT(self):
        path=urlparse(self.path).path
        try: data=self.body()
        except Exception: self.send_json({'error':'不正なリクエストです。'},400); return
        if path.startswith('/api/users/') and path.endswith('/reset-password'):
            actor=self.require(admin=True)
            if not actor: return
            try: uid=int(path.split('/')[3])
            except (ValueError, IndexError): self.send_json({'error':'ユーザーIDが不正です。'},400); return
            password=data.get('new_password')
            if not valid_password(password): self.send_json({'error':PASSWORD_RULE_MESSAGE},400); return
            con=db(); row=con.execute('SELECT id,role FROM cc_users WHERE id=? AND active=1',(uid,)).fetchone()
            if not row or row['role']!='operator':
                con.close(); self.send_json({'error':'有効なオペレーターが見つかりません。'},404); return
            salt,digest=hash_password(password)
            con.execute('UPDATE cc_users SET salt=?,password_hash=? WHERE id=?',(salt,digest,uid)); con.commit(); con.close()
            for key, session in list(SESSIONS.items()):
                if session['user_id']==uid: SESSIONS.pop(key,None)
            audit(actor['id'],'operator_password_reset',{'user_id':uid}); emit('users')
            self.send_json({'ok':True}); return
        if path.startswith('/api/users/'):
            actor=self.require(admin=True)
            if not actor:return
            try: uid=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'ユーザーIDが不正です。'},400); return
            con=db(); row=con.execute('SELECT * FROM cc_users WHERE id=?',(uid,)).fetchone()
            if not row: con.close(); self.send_json({'error':'ユーザーが見つかりません。'},404); return
            if set(data.keys())=={'active'}:
                if uid==actor['id'] and not bool(data['active']): con.close(); self.send_json({'error':'自分自身を無効化できません。'},400); return
                con.execute('UPDATE cc_users SET active=? WHERE id=?',(1 if data['active'] else 0,uid)); con.commit(); con.close()
                audit(actor['id'],'user_activated' if data['active'] else 'user_deactivated',{'user_id':uid}); emit('users'); self.send_json({'ok':True}); return
            role=data.get('role',row['role'])
            if role not in ('operator','admin') or (uid==actor['id'] and role!='admin'):
                con.close(); self.send_json({'error':'権限の変更が許可されません。'},400); return
            username=str(data.get('username','')).strip(); name=str(data.get('display_name','')).strip(); skills=data.get('skills',[])
            start=data.get('shift_start','09:00'); end=data.get('shift_end','18:00'); password=str(data.get('password',''))
            if not username or not name or not isinstance(skills,list) or any(x not in SKILLS for x in skills) or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end) or (password and not valid_password(password)):
                con.close(); self.send_json({'error':(PASSWORD_RULE_MESSAGE if password and not valid_password(password) else '入力を確認してください。勤務時間は10分刻み、終了は開始より後です。')},400); return
            try:
                if password:
                    salt,digest=hash_password(password)
                    con.execute('UPDATE cc_users SET username=?,display_name=?,role=?,skills=?,shift_start=?,shift_end=?,salt=?,password_hash=? WHERE id=?',(username,name,role,json.dumps(skills),start,end,salt,digest,uid))
                else:
                    con.execute('UPDATE cc_users SET username=?,display_name=?,role=?,skills=?,shift_start=?,shift_end=? WHERE id=?',(username,name,role,json.dumps(skills),start,end,uid))
                con.commit(); con.close()
            except sqlite3.IntegrityError:
                con.close(); self.send_json({'error':'このユーザー名は既に使われています。'},409); return
            
            if row['role']!=role:
                for key,session in list(SESSIONS.items()):
                    if session['user_id']==uid: SESSIONS.pop(key,None)
                audit(actor['id'],'user_role_changed',{'user_id':uid,'old_role':row['role'],'new_role':role})
            audit(actor['id'],'user_updated',{'user_id':uid}); emit('users'); self.send_json({'ok':True}); return
        if path.startswith('/api/demand/'):
            actor=self.require(admin=True)
            if not actor:return
            try: did=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'設定IDが不正です。'},400); return
            skill=data.get('skill'); start=data.get('start_time'); end=data.get('end_time'); count=data.get('min_staff')
            try:
                if skill not in SKILLS or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end) or int(count)<0 or int(count)>200: raise ValueError()
                con=db(); exists=con.execute('SELECT id FROM cc_demand WHERE id=?',(did,)).fetchone()
                if not exists: con.close(); self.send_json({'error':'設定が見つかりません。'},404); return
                con.execute('UPDATE cc_demand SET skill=?,start_time=?,end_time=?,min_staff=? WHERE id=?',(skill,start,end,int(count),did)); con.commit(); con.close()
            except sqlite3.IntegrityError:
                self.send_json({'error':'同じスキル・開始・終了の設定が既にあります。'},409); return
            except Exception:
                self.send_json({'error':'スキル・時間帯・人数を確認してください。'},400); return
            audit(actor['id'],'demand_updated',data); emit('demand'); self.send_json({'ok':True}); return
        self.send_json({'error':'not found'},404)

    def do_DELETE(self):
        path=urlparse(self.path).path
        if path=='/api/work-hours':
            actor=self.require(admin=True)
            if not actor:return
            # Body includes date and ID; only the daily override is removed.
            try:
                data=self.body()
                date=str(data.get('date',''))
                datetime.strptime(date,'%Y-%m-%d')
                uid=int(data.get('user_id'))
            except (ValueError,TypeError,KeyError):
                self.send_json({'error':'対象日・オペレーターを確認してください。'},400);return
            con=db()
            existing=con.execute('SELECT 1 FROM cc_shift_overrides WHERE work_date=? AND user_id=?',(date,uid)).fetchone()
            if not existing:
                con.close();self.send_json({'error':'当日の勤務時間変更がありません。'},404);return
            con.execute('DELETE FROM cc_shift_overrides WHERE work_date=? AND user_id=?',(date,uid))
            # Restore attendance default from the finalized shift (unless the
            # administrator explicitly re-saves attendance afterwards).
            con.execute('DELETE FROM cc_attendance WHERE attendance_date=? AND user_id=?',(date,uid))
            con.commit();con.close()
            audit(actor['id'],'daily_work_hours_reset',{'date':date,'user_id':uid})
            emit('attendance');self.send_json({'ok':True});return
        if path.startswith('/api/users/'):
            actor=self.require(admin=True)
            if not actor:return
            try: uid=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'invalid id'},400); return
            if uid==actor['id']: self.send_json({'error':'自分自身は無効化できません。'},400); return
            con=db(); con.execute("UPDATE cc_users SET active=0 WHERE id=? AND role='operator'",(uid,)); changed=con.total_changes; con.commit(); con.close()
            if not changed: self.send_json({'error':'対象のオペレーターが見つかりません。'},404); return
            audit(actor['id'],'user_deactivated',{'user_id':uid}); emit('users'); self.send_json({'ok':True}); return
        if path.startswith('/api/demand/'):
            actor=self.require(admin=True)
            if not actor:return
            try: did=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'invalid id'},400); return
            con=db(); cur=con.execute('DELETE FROM cc_demand WHERE id=?',(did,)); con.commit(); con.close()
            if not cur.rowcount: self.send_json({'error':'設定が見つかりません。'},404); return
            audit(actor['id'],'demand_deleted',{'demand_id':did}); emit('demand'); self.send_json({'ok':True}); return
        self.send_json({'error':'not found'},404)

def main():
    init_db()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    server.daemon_threads=True
    print('='*62)
    print('コールセンター稼働最適化アプリ（ローカルLAN版）')
    print(f'管理者PC内の確認用URL: http://127.0.0.1:{PORT}')
    computer_name = socket.gethostname()
    print(f'社内LAN向けURL候補（PC名での名前解決が有効な場合）: http://{computer_name}:{PORT}/')
    print('この画面にはLAN内のIPアドレスを表示しません。')
    print('初期アカウント情報はREADME.mdの「初期アカウント」を確認し、初回ログイン後すぐに変更してください。')
    print('停止するには Ctrl+C を押してください。')
    print('='*62)
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nサーバーを停止します。')
    finally: server.server_close()

if __name__=='__main__': main()
