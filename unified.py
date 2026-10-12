"""Single URL, shared Flask login, single SQLite file. Evaluation build."""
import os, sys, secrets, threading, socket, sqlite3, json, re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from flask import request, session, redirect, Response, abort
from http.server import ThreadingHTTPServer
import traceback
import legacy_operator_migration
from datetime import datetime

ROOT=Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent

def _write_startup_error(exc_type, exc_value, exc_tb):
    # Also catches failures during imports/initialization, before main() runs.
    if not issubclass(exc_type, KeyboardInterrupt):
        try:
            log_dir = ROOT / 'runtime_logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / 'startup_error.log').open('a', encoding='utf-8') as logfile:
                logfile.write('\n=== Startup failure: ' + datetime.now().isoformat() + ' ===\n')
                traceback.print_exception(exc_type, exc_value, exc_tb, file=logfile)
            print('起動失敗。ログ: ' + str(log_dir / 'startup_error.log'), file=sys.stderr, flush=True)
        except OSError:
            pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _write_startup_error
DB=ROOT/'data'/'unified.sqlite3'
DB.parent.mkdir(exist_ok=True)
os.environ['UNIFIED_DB_PATH']=str(DB)
os.environ['OPERATOR_REALTIME_SYNC']='0'
os.environ['CALLCENTER_HOST']='127.0.0.1'
os.environ['CALLCENTER_PORT']='18765'
RESOURCE_ROOT=Path(getattr(sys, '_MEIPASS', ROOT))
sys.path.insert(0,str(RESOURCE_ROOT/'shift_planner_app'))
import app as shift
sys.path.insert(0,str(RESOURCE_ROOT/'break_engine'))
import server as br

app=shift.app
# PyInstaller --onedir stores bundled templates under sys._MEIPASS, not
# next to the imported app.py module. Configure Flask explicitly.
TEMPLATE_DIR = RESOURCE_ROOT / 'shift_planner_app' / 'templates'
if not TEMPLATE_DIR.is_dir():
    raise RuntimeError(f'HTML templates directory not found: {TEMPLATE_DIR}')
app.template_folder = str(TEMPLATE_DIR)
# Do not assign create_global_jinja_loader() to app.jinja_loader:
# that dispatching loader consults app.jinja_loader itself and recurses forever.
from jinja2 import FileSystemLoader
app.jinja_loader = FileSystemLoader(str(TEMPLATE_DIR))
# If Jinja was initialized earlier, refresh its cached loader as well.
if 'jinja_env' in app.__dict__:
    app.jinja_env.loader = app.create_global_jinja_loader()
app.secret_key=os.environ.get('SHIFT_APP_SECRET') or secrets.token_hex(32)
TOKENS={}
LOCK=threading.RLock()

def reconcile(date=None):
    """Copy canonical shift users to engine's derived cache inside same SQLite file."""
    with LOCK:
        with sqlite3.connect(DB,timeout=20) as c:
            c.row_factory=sqlite3.Row
            rows=c.execute('''SELECT u.username,u.role,u.password_hash,e.id eid,e.name,e.skills,e.active,u.approved,e.source_user_id
              FROM users u LEFT JOIN employees e ON e.id=u.employee_id''').fetchall()
            existing={r['username']:r for r in c.execute('SELECT id,username FROM cc_users')}
            existing_ids={r['id']:r for r in existing.values()}
            for r in rows:
                if r['role']=='operator' and not r['eid']: continue
                # Preserve PP exactly: str.capitalize() would turn 'PP'
                # into 'Pp' and silently remove that qualification.
                skills=[{'consumer':'Consumer','merchant':'Merchant','PP':'PP'}[sk]
                        for sk in shift.flex.skills_of(r['skills'] or '')]
                skills=skills or ['Consumer']
                active=(bool(r['active']) and bool(r['approved'])) if r['eid'] else bool(r['approved'])
                # Prefer the legacy account's stable ID. Otherwise a login-ID edit
                # would create a new cc_user and orphan attendance/break history.
                linked_id=r['source_user_id'] if r['eid'] and r['source_user_id'] in existing_ids else None
                matched=existing.get(r['username'])
                target_id=matched['id'] if matched else linked_id
                if target_id is not None:
                    c.execute('UPDATE cc_users SET username=?,display_name=?,role=?,active=?,skills=? WHERE id=?',
                              (r['username'],r['name'] or r['username'],r['role'],int(active),json.dumps(skills),target_id))
                else:
                    salt,digest=br.hash_password(secrets.token_urlsafe(16))
                    c.execute('''INSERT INTO cc_users(username,display_name,role,salt,password_hash,active,skills)
                      VALUES(?,?,?,?,?,?,?)''',(r['username'],r['name'] or r['username'],r['role'],salt,digest,int(active),json.dumps(skills)))
                    target_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                if r['eid']:
                    c.execute('UPDATE employees SET source_user_id=?,source_username=? WHERE id=?',
                              (target_id,r['username'],r['eid']))
            canonical={r['username'] for r in rows}
            for username in existing.keys()-canonical:
                c.execute('UPDATE cc_users SET active=0 WHERE username=?',(username,))
            # Planned and adjusted work hours are date-specific. Never copy a date's
            # pattern into cc_users.shift_start/end, which are fallback defaults.
            # The break engine reads finalized assignments and same-day overrides
            # directly from the shared SQLite database for every date.


def user_token():
    name=session.get('username')
    if not name: return None
    with LOCK:
        reconcile()
        with sqlite3.connect(DB) as c:
            r=c.execute('SELECT id,role,active FROM cc_users WHERE username=?',(name,)).fetchone()
        if not r or not r[2]: return None
        # Refresh role from canonical account on every request.
        with sqlite3.connect(DB) as c:
            role=c.execute('SELECT role FROM users WHERE username=? AND approved=1',(name,)).fetchone()
        if not role: return None
        session['role']=role[0]
        key=(name,r[0]); token=TOKENS.get(key)
        if not token:
            token=secrets.token_urlsafe(32); TOKENS[key]=token
        import time
        br.SESSIONS[token]={'user_id':r[0],'expires':time.time()+3600}
        return token

@app.route('/break/')
def break_home():
    token=user_token()
    if not token: return redirect('/login')
    html=(RESOURCE_ROOT/'break_engine/static/index.html').read_text(encoding='utf-8')
    html=html.replace('__UNIFIED_TOKEN__',token)
    html=html.replace('コールセンター稼働最適化','統合システム・休憩管理')
    html=html.replace('<main>', '<main><p><a href="/">← シフト管理へ戻る</a>　<a href="/employees">従業員管理（共通）</a></p>')
    return Response(html,content_type='text/html; charset=utf-8',headers={'Cache-Control':'no-store'})

@app.route('/break/api/<path:path>',methods=['GET','POST','PUT','DELETE'])
def break_api(path):
    token=user_token()
    if not token: abort(401)
    # Employee writes are managed by the canonical /employees page.
    # Return a helpful conflict response rather than an unexplained 403.
    if (path=='users' or path.startswith('users/')) and request.method!='GET':
        return {'error':'従業員の登録・再有効化・権限変更は「従業員管理（共通）」から操作してください。','management_url':'/employees'},409
    if path in ('register','login','logout','restore','password'): abort(403)
    if path in ('schedule/generate','schedule/manual','attendance') and request.method=='POST' or (path=='attendance' and request.method=='GET'):
        body=request.get_json(silent=True) or {}
        day=body.get('date') or request.args.get('date')
        if day and re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
            reconcile(day)
            # An upgraded database may contain old, unchecked attendance rows
            # written BEFORE the month's last shift finalization. Repair those
            # but never override a manual change made AFTER finalization.
            if (path=='attendance' and request.method=='GET') or path in ('schedule/generate','schedule/manual'):
                c=sqlite3.connect(DB,timeout=20)
                try:
                    c.row_factory=sqlite3.Row
                    shift.attendance_sync.repair_day_if_older_than_finalization(c,day)
                    c.commit()
                finally:
                    c.close()
    url='http://127.0.0.1:18765/api/'+path
    if request.query_string: url+='?'+request.query_string.decode()
    headers={'X-Session-Token':token,'Content-Type':request.content_type or 'application/json'}
    req=Request(url,data=request.get_data() if request.method in ('POST','PUT','DELETE') else None,
                headers=headers,method=request.method)
    try:
        with urlopen(req,timeout=35) as res:
            content=res.read(); status=res.status; typ=res.headers.get('Content-Type','application/json')
    except HTTPError as e:
        content=e.read(); status=e.code; typ=e.headers.get('Content-Type','application/json')
    except Exception:
        return {'error':'休憩エンジンへの接続に失敗しました'},503
    return Response(content,status=status,content_type=typ,headers={'Cache-Control':'no-store'})

@app.before_request
def unified_role_check():
    if request.path.startswith('/integration'): abort(403)
    if not session.get('username'): return
    if request.endpoint in ('login','logout','static'): return
    if request.path=='/finalize' and request.method=='POST':
        # A newly added employee may not have visited break management yet.
        # Create/update the break-side account before the attendance sync.
        reconcile()
    with shift.db() as c:
        u=c.execute('''SELECT u.role,u.employee_id,u.approved,e.active AS employee_active
                       FROM users u LEFT JOIN employees e ON e.id=u.employee_id
                       WHERE u.username=?''',(session['username'],)).fetchone()
        if not u or not u["approved"] or (u["employee_id"] is not None and not u["employee_active"]):
            session.clear();return redirect('/login')
        session['role']=u['role'];session['employee_id']=u['employee_id']


def main():
    shift.init_db();br.init_db()
    # Bring back inactive operators created in the old, separate break editor.
    # Employee records and their history remain untouched on repeat startups.
    with sqlite3.connect(DB, timeout=20) as migration_db:
        recovered = legacy_operator_migration.migrate_break_only_operators(migration_db)
    if recovered:
        print(f'従業員管理へ休憩管理の旧オペレーター {recovered} 件を取り込みました。')
    reconcile()
    server=ThreadingHTTPServer(('127.0.0.1',18765),br.Handler)
    server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    print('統合入口 http://'+socket.gethostname()+':5050/ （PC名で解決できる場合）')
    # User-facing path is deliberately shortened. DB itself remains the same
    # absolute executable-relative SQLite file; no database is moved.
    import ntpath
    shown_db = ntpath.join('シフト休憩提案アプリ統合版', 'dist', 'CallCenterUnified', 'data', 'unified.sqlite3') \
        if getattr(sys, 'frozen', False) else ntpath.join('シフト休憩提案アプリ統合版', 'data', 'unified.sqlite3')
    print('シフト・休憩は同一のSQLiteファイルを使用:', shown_db)
    print('注意: 評価用。会社のネットワーク管理者の許可なく公開しないでください。')
    # Direct EXE launch must behave just like start_built_app.bat: accept LAN clients.
    # Administrators can still override UNIFIED_HOST=127.0.0.1 to restrict to local PC.
    host = os.environ.get('UNIFIED_HOST', '0.0.0.0')
    port = int(os.environ.get('UNIFIED_PORT', '5050'))
    print(f'Webサーバー待ち受け: {host}:{port}', flush=True)
    app.run(host=host, port=port, debug=False, threaded=True)

if __name__=='__main__':main()
