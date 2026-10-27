import functools
import os
import secrets
import sqlite3
from datetime import date, timedelta

from flask import Flask, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('NPI_CASE_DATA_DIR', BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, 'case_tracker.db')

app = Flask(__name__)

DEFAULT_STAGES = [
    ('開案 Kick-off', '#6b7280', '收到客戶 SWR 或開案通知，案件正式成立'),
    ('圖面 & Layout 確認', '#0ea5e9', 'BD 圖 / SBT Layout / LF 圖面經客戶書面確認'),
    ('材料到位', '#3b82f6', '基板、導線架、線材、compound 等關鍵料全數入庫'),
    ('1st ES 投料', '#8b5cf6', '工程批工單下線並實際投料'),
    ('ES 出貨', '#d946ef', '工程樣品出貨予客戶'),
    ('PKG Qual & RA', '#f59e0b', '廠內封裝可靠度測試完成並判定（MSL/TCT/HTSL/THT）'),
    ('客戶驗證 / Qual Report', '#f97316', '客戶回覆 PASS，Qual report 正式結案'),
    ('PNR / 量產轉移', '#16a34a', '量產工單投料，案件轉入量產管理'),
]
DEPARTMENTS = ['2620', '2640', '2650']
STAGE_STATUSES = ['未開始', '進行中', '延遲', '卡關', '完成', 'NA']
STAGE_STATUS_COLORS = {
    '未開始': '#9ca3af',
    '進行中': '#3b82f6',
    '延遲': '#f59e0b',
    '卡關': '#dc2626',
    '完成': '#16a34a',
    'NA': '#d1d5db',
}
STATUS_BUCKET = {'未開始': 'Wait', '進行中': 'Ongoing', '延遲': 'Ongoing', '卡關': 'Ongoing', '完成': 'Done'}
BUCKETS = ['Wait', 'Ongoing', 'Done']
BUCKET_COLORS = {'Wait': '#9ca3af', 'Ongoing': '#3b82f6', 'Done': '#16a34a'}
BASIC_FIELDS = [
    ('product_attr', '產品屬性'),
    ('product_app', '產品應用'),
    ('product_tech', 'Product / Tech'),
    ('type_application', 'Type Application'),
    ('sbt_lf_drawing_no', 'SBT/LF 圖號'),
    ('sbt_lf_part_no', 'SBT/LF 料號'),
    ('turnkey', 'Turnkey'),
    ('npdm', 'NPDM'),
    ('pd_owner', 'PD Owner'),
    ('design_owner', 'Design Owner'),
    ('forecast', 'Forecast'),
    ('tra_level', 'TRA Level'),
]
BASIC_COLS = [c for c, _ in BASIC_FIELDS]


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def _get_or_create_secret_key(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()
    if row:
        return row['value']
    key = secrets.token_hex(32)
    conn.execute("INSERT INTO settings (key, value) VALUES ('secret_key', ?)", (key,))
    conn.commit()
    return key


def _sync_case_stage_rows(conn):
    """Make sure every case has a case_stage_status row for every stage (defaults to 未開始)."""
    conn.execute('''
        INSERT OR IGNORE INTO case_stage_status (case_id, stage_id, status)
        SELECT c.id, s.id, '未開始' FROM cases c CROSS JOIN stages s
    ''')


def _log_stage_status(conn, case_id, stage_id, old_status, new_status, occurred_on, user_id):
    # A backdated change must not sit before events recorded earlier, or the weekly stats
    # would show the older (e.g. initial 未開始) event as the latest state.
    conn.execute('''
        UPDATE case_stage_status_log SET occurred_on=?
        WHERE case_id=? AND stage_id=? AND occurred_on>?
    ''', (occurred_on, case_id, stage_id, occurred_on))
    conn.execute('''
        INSERT INTO case_stage_status_log (case_id, stage_id, old_status, new_status, occurred_on, changed_by)
        VALUES (?,?,?,?,?,?)
    ''', (case_id, stage_id, old_status, new_status, occurred_on, user_id))


def _parse_occurred_on(value, today):
    """Validate a user-supplied actual-occurrence date; fall back to today, never in the future."""
    try:
        d = date.fromisoformat(value or '')
    except ValueError:
        return today
    return min(d, date.fromisoformat(today)).isoformat()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        table_names = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'statuses' in table_names and 'stages' not in table_names:
            conn.execute('ALTER TABLE statuses RENAME TO stages')

        conn.executescript('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                color TEXT NOT NULL DEFAULT '#6b7280',
                description TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                department TEXT NOT NULL DEFAULT '',
                customer TEXT,
                priority TEXT NOT NULL DEFAULT '中',
                due_date TEXT,
                progress INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS case_stage_status (
                case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT '未開始',
                PRIMARY KEY (case_id, stage_id)
            );

            CREATE TABLE IF NOT EXISTS case_stage_status_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
                old_status TEXT,
                new_status TEXT NOT NULL,
                occurred_on TEXT NOT NULL,
                changed_by INTEGER REFERENCES users(id),
                changed_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_status_log_occurred ON case_stage_status_log(occurred_on);
        ''')

        user_cols = {r['name'] for r in conn.execute('PRAGMA table_info(users)')}
        if 'department' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN department TEXT NOT NULL DEFAULT ''")
        case_cols = {r['name'] for r in conn.execute('PRAGMA table_info(cases)')}
        if 'department' not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN department TEXT NOT NULL DEFAULT ''")
        for col in BASIC_COLS:
            if col not in case_cols:
                conn.execute(f"ALTER TABLE cases ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

        if conn.execute('SELECT COUNT(*) FROM stages').fetchone()[0] == 0:
            for i, (name, color, description) in enumerate(DEFAULT_STAGES):
                conn.execute('INSERT INTO stages (name, color, description, sort_order) VALUES (?,?,?,?)',
                             (name, color, description, i))

        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            conn.execute(
                'INSERT INTO users (username, password_hash, display_name, is_admin) VALUES (?,?,?,1)',
                ('admin', generate_password_hash('admin123'), '管理員')
            )

        conn.execute('PRAGMA foreign_keys = ON')
        _sync_case_stage_rows(conn)

        # One-time upgrade: seed history for cases that predate the status log, so the
        # weekly stats aren't empty. Dates are estimates (created/last-updated day).
        if conn.execute('SELECT COUNT(*) FROM case_stage_status_log').fetchone()[0] == 0:
            conn.execute('''
                INSERT INTO case_stage_status_log (case_id, stage_id, old_status, new_status, occurred_on)
                SELECT css.case_id, css.stage_id, NULL, css.status,
                       CASE WHEN css.status = '未開始' THEN date(c.created_at, 'localtime')
                            ELSE date(c.updated_at, 'localtime') END
                FROM case_stage_status css JOIN cases c ON c.id = css.case_id
            ''')

        app.secret_key = _get_or_create_secret_key(conn)
        conn.commit()


# ── Auth ─────────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        if not session.get('is_admin'):
            return render_template('error.html', message='權限不足，僅限管理員操作'), 403
        return f(*args, **kwargs)
    return wrapper


def can_edit_case(case_row):
    if session.get('is_admin'):
        return True
    return case_row['owner_id'] == session.get('user_id')


@app.before_request
def load_current_user():
    g.current_user = None
    if 'user_id' in session:
        g.current_user = {
            'id': session['user_id'],
            'username': session['username'],
            'display_name': session['display_name'],
            'department': session.get('department', ''),
            'is_admin': bool(session.get('is_admin')),
        }


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=? AND active=1', (username,)).fetchone()
        if not user or not check_password_hash(user['password_hash'], password):
            return render_template('login.html', error='帳號或密碼錯誤')
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['display_name'] = user['display_name']
        session['department'] = user['department']
        session['is_admin'] = bool(user['is_admin'])
        return redirect(request.args.get('next') or url_for('dashboard'))
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/me/password', methods=['GET', 'POST'])
@login_required
def change_password():
    error = None
    ok = False
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not check_password_hash(user['password_hash'], current_pw):
            error = '目前密碼不正確'
        elif len(new_pw) < 4:
            error = '新密碼至少需要 4 個字元'
        else:
            conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                         (generate_password_hash(new_pw), session['user_id']))
            conn.commit()
            ok = True
    return render_template('change_password.html', error=error, ok=ok)


# ── Case progress helpers ──────────────────────────────────────────────────

def _case_stage_rows(conn, case_ids):
    """{case_id: [ {stage_id, name, color, sort_order, status}, ... ]} ordered by stage sort_order."""
    if not case_ids:
        return {}
    placeholders = ','.join('?' * len(case_ids))
    rows = conn.execute(f'''
        SELECT css.case_id, s.id AS stage_id, s.name, s.color, s.sort_order, css.status
        FROM case_stage_status css
        JOIN stages s ON s.id = css.stage_id
        WHERE css.case_id IN ({placeholders})
        ORDER BY css.case_id, s.sort_order
    ''', case_ids).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r['case_id'], []).append(r)
    return result


def _case_progress(stage_rows):
    """Summarize one case's stage rows: current stage, blocked count, overall completion."""
    current = None
    blocked = 0
    all_done = True
    for r in stage_rows:
        if r['status'] in ('卡關', '延遲'):
            blocked += 1
        if r['status'] not in ('完成', 'NA'):
            all_done = False
            if current is None:
                current = r
    if current is None and stage_rows:
        current = stage_rows[-1]
    return {
        'current_stage_id': current['stage_id'] if current else None,
        'current_stage_name': current['name'] if current else None,
        'current_stage_color': current['color'] if current else None,
        'current_status': current['status'] if current else None,
        'current_status_color': STAGE_STATUS_COLORS.get(current['status']) if current else None,
        'blocked_count': blocked,
        'all_done': all_done and bool(stage_rows),
    }


# ── Dashboard ────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    conn = get_db()
    today = date.today().isoformat()

    stages = conn.execute('SELECT * FROM stages ORDER BY sort_order').fetchall()
    all_cases = conn.execute('''
        SELECT c.*, u.display_name AS owner_name
        FROM cases c JOIN users u ON u.id = c.owner_id
    ''').fetchall()
    case_ids = [c['id'] for c in all_cases]
    stage_rows_map = _case_stage_rows(conn, case_ids)

    stage_cnt = {s['id']: 0 for s in stages}
    done_count = 0
    overdue = []
    blocked_cases = []
    progress_by_case = {}

    for c in all_cases:
        rows = stage_rows_map.get(c['id'], [])
        summary = _case_progress(rows)
        progress_by_case[c['id']] = summary
        if summary['all_done']:
            done_count += 1
        elif summary['current_stage_id'] is not None:
            stage_cnt[summary['current_stage_id']] = stage_cnt.get(summary['current_stage_id'], 0) + 1
        if c['due_date'] and not summary['all_done'] and c['due_date'] < today:
            overdue.append({**dict(c), **summary})
        for r in rows:
            if r['status'] in ('卡關', '延遲'):
                blocked_cases.append({
                    'case_id': c['id'], 'title': c['title'], 'owner_name': c['owner_name'],
                    'stage_name': r['name'], 'stage_color': r['color'],
                    'status': r['status'], 'status_color': STAGE_STATUS_COLORS.get(r['status']),
                })

    overdue.sort(key=lambda c: c['due_date'])
    blocked_case_count = len({b['case_id'] for b in blocked_cases})

    stage_counts = [{'id': s['id'], 'name': s['name'], 'color': s['color'], 'cnt': stage_cnt.get(s['id'], 0)}
                    for s in stages]

    engineer_stats = {}
    for u in conn.execute('SELECT * FROM users WHERE active=1 ORDER BY display_name').fetchall():
        engineer_stats[u['id']] = {'id': u['id'], 'display_name': u['display_name'],
                                    'total': 0, 'done_cnt': 0}
    for c in all_cases:
        st = engineer_stats.get(c['owner_id'])
        if st is None:
            continue
        st['total'] += 1
        if progress_by_case[c['id']]['all_done']:
            st['done_cnt'] += 1
    by_engineer = sorted(engineer_stats.values(), key=lambda x: (-x['total'], x['display_name']))

    recent_raw = conn.execute('''
        SELECT c.*, u.display_name AS owner_name, datetime(c.updated_at, 'localtime') AS updated_local
        FROM cases c JOIN users u ON u.id = c.owner_id
        ORDER BY c.updated_at DESC LIMIT 10
    ''').fetchall()
    recent = [{**dict(c), **progress_by_case.get(c['id'], _case_progress(stage_rows_map.get(c['id'], [])))}
              for c in recent_raw]

    return render_template('dashboard.html',
                            stage_counts=stage_counts,
                            total_cases=len(all_cases),
                            done_count=done_count,
                            overdue=overdue,
                            blocked_cases=blocked_cases,
                            blocked_case_count=blocked_case_count,
                            by_engineer=by_engineer,
                            recent=recent,
                            today=today)


# ── Cases ────────────────────────────────────────────────────────────────

@app.route('/cases')
@login_required
def case_list():
    conn = get_db()
    owner_filter = request.args.get('owner', '')
    stage_filter = request.args.get('stage', '')
    department_filter = request.args.get('department', '')

    query = '''
        SELECT c.*, u.display_name AS owner_name
        FROM cases c
        JOIN users u ON u.id = c.owner_id
        WHERE 1=1
    '''
    params = []
    if owner_filter:
        query += ' AND c.owner_id = ?'
        params.append(owner_filter)
    if department_filter:
        query += ' AND c.department = ?'
        params.append(department_filter)
    query += ' ORDER BY c.updated_at DESC'

    raw_cases = conn.execute(query, params).fetchall()
    stage_rows_map = _case_stage_rows(conn, [c['id'] for c in raw_cases])

    cases = []
    for c in raw_cases:
        summary = _case_progress(stage_rows_map.get(c['id'], []))
        if stage_filter and str(summary.get('current_stage_id')) != stage_filter:
            continue
        cases.append({**dict(c), **summary})

    users = conn.execute('SELECT * FROM users WHERE active=1 ORDER BY display_name').fetchall()
    stages = conn.execute('SELECT * FROM stages ORDER BY sort_order').fetchall()

    return render_template('case_list.html', cases=cases, users=users, stages=stages,
                            departments=DEPARTMENTS,
                            owner_filter=owner_filter, stage_filter=stage_filter,
                            department_filter=department_filter)


def _case_form_context(case=None):
    conn = get_db()
    users = conn.execute('SELECT * FROM users WHERE active=1 ORDER BY display_name').fetchall()
    stages = conn.execute('SELECT * FROM stages ORDER BY sort_order').fetchall()
    stage_status_map = {}
    if case:
        rows = conn.execute('SELECT stage_id, status FROM case_stage_status WHERE case_id=?',
                             (case['id'],)).fetchall()
        stage_status_map = {r['stage_id']: r['status'] for r in rows}
    updated_local = ''
    if case:
        updated_local = conn.execute("SELECT datetime(updated_at, 'localtime') FROM cases WHERE id=?",
                                      (case['id'],)).fetchone()[0]
    return dict(case=case, users=users, stages=stages, basic_fields=BASIC_FIELDS, updated_local=updated_local,
                stage_statuses=STAGE_STATUSES, stage_status_map=stage_status_map,
                stage_status_colors=STAGE_STATUS_COLORS, today=date.today().isoformat())


def _user_department(conn, user_id):
    row = conn.execute('SELECT department FROM users WHERE id=?', (user_id,)).fetchone()
    return row['department'] if row else ''


@app.route('/cases/new', methods=['GET', 'POST'])
@login_required
def case_new():
    conn = get_db()
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        owner_id = request.form.get('owner_id') or session['user_id']
        if not session.get('is_admin'):
            owner_id = session['user_id']  # non-admins can only create cases for themselves
        if not title:
            return render_template('case_form.html', error='案名為必填', **_case_form_context()), 400
        department = _user_department(conn, owner_id)
        cols = ['title', 'owner_id', 'department', 'customer', 'due_date', 'note', 'created_by'] + BASIC_COLS
        cur = conn.execute(
            f'INSERT INTO cases ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
            [title, owner_id, department, request.form.get('customer', ''), request.form.get('due_date') or None,
             request.form.get('note', ''), session['user_id']] + [request.form.get(c, '').strip() for c in BASIC_COLS])
        _sync_case_stage_rows(conn)
        today = date.today().isoformat()
        for s in conn.execute('SELECT id FROM stages').fetchall():
            _log_stage_status(conn, cur.lastrowid, s['id'], None, '未開始', today, session['user_id'])
        conn.commit()
        return redirect(url_for('case_edit', case_id=cur.lastrowid))
    return render_template('case_form.html', **_case_form_context())


@app.route('/cases/<int:case_id>/edit', methods=['GET', 'POST'])
@login_required
def case_edit(case_id):
    conn = get_db()
    case = conn.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    if not case:
        return render_template('error.html', message='找不到這個案件'), 404
    if not can_edit_case(case):
        return render_template('error.html', message='只有負責工程師或管理員才能修改此案件'), 403

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        owner_id = case['owner_id']
        if session.get('is_admin'):
            owner_id = request.form.get('owner_id') or case['owner_id']
        if not title:
            return render_template('case_form.html', error='案名為必填', **_case_form_context(case)), 400
        department = _user_department(conn, owner_id)
        set_cols = ['title', 'owner_id', 'department', 'customer', 'due_date', 'note'] + BASIC_COLS
        conn.execute(
            f"UPDATE cases SET {', '.join(c + '=?' for c in set_cols)}, updated_at=datetime('now') WHERE id=?",
            [title, owner_id, department, request.form.get('customer', ''), request.form.get('due_date') or None,
             request.form.get('note', '')] + [request.form.get(c, '').strip() for c in BASIC_COLS] + [case_id])

        today = date.today().isoformat()
        current = {r['stage_id']: r['status'] for r in conn.execute(
            'SELECT stage_id, status FROM case_stage_status WHERE case_id=?', (case_id,))}
        for s in conn.execute('SELECT id FROM stages').fetchall():
            value = request.form.get(f'stage_status_{s["id"]}')
            old = current.get(s['id'])
            if value not in STAGE_STATUSES or value == old:
                continue
            conn.execute('UPDATE case_stage_status SET status=? WHERE case_id=? AND stage_id=?',
                         (value, case_id, s['id']))
            occurred_on = _parse_occurred_on(request.form.get(f'stage_date_{s["id"]}'), today)
            _log_stage_status(conn, case_id, s['id'], old, value, occurred_on, session['user_id'])
        conn.commit()
        return redirect(url_for('case_list'))

    return render_template('case_form.html', **_case_form_context(case))


@app.route('/cases/<int:case_id>/delete', methods=['POST'])
@login_required
def case_delete(case_id):
    conn = get_db()
    case = conn.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    if not case:
        return render_template('error.html', message='找不到這個案件'), 404
    if not can_edit_case(case):
        return render_template('error.html', message='只有負責工程師或管理員才能刪除此案件'), 403
    conn.execute('DELETE FROM cases WHERE id=?', (case_id,))
    conn.commit()
    return redirect(url_for('case_list'))


# ── Weekly stats ─────────────────────────────────────────────────────────

def _iso_week_value(d):
    iso = d.isocalendar()
    return f'{iso.year}-W{iso.week:02d}'


@app.route('/weekly')
@login_required
def weekly():
    conn = get_db()
    today = date.today()
    cur_monday = today - timedelta(days=today.weekday())

    try:
        year, wk = request.args.get('week', '').split('-W')
        monday = date.fromisocalendar(int(year), int(wk), 1)
    except ValueError:
        monday = cur_monday
    sunday = monday + timedelta(days=6)
    week_value = _iso_week_value(monday)

    department_filter = request.args.get('department', '')
    customer_filter = request.args.get('customer', '')
    owner_filter = request.args.get('owner', '')

    query = '''
        SELECT l.case_id, l.stage_id, l.new_status, c.customer
        FROM case_stage_status_log l JOIN cases c ON c.id = l.case_id
        WHERE l.occurred_on BETWEEN ? AND ?
    '''
    params = [monday.isoformat(), sunday.isoformat()]
    if department_filter:
        query += ' AND c.department = ?'
        params.append(department_filter)
    if customer_filter:
        query += ' AND c.customer = ?'
        params.append(customer_filter)
    if owner_filter:
        query += ' AND c.owner_id = ?'
        params.append(owner_filter)
    query += ' ORDER BY l.occurred_on, l.id'

    # Only the last change per case+stage within the week counts.
    latest = {}
    for r in conn.execute(query, params):
        latest[(r['case_id'], r['stage_id'])] = r

    sections = []
    for s in conn.execute('SELECT * FROM stages ORDER BY sort_order').fetchall():
        counts = {b: 0 for b in BUCKETS}
        by_customer = {b: {} for b in BUCKETS}
        for (_, stage_id), r in latest.items():
            bucket = STATUS_BUCKET.get(r['new_status'])
            if stage_id != s['id'] or bucket is None:
                continue
            counts[bucket] += 1
            name = r['customer'] or '（未填客戶）'
            by_customer[bucket][name] = by_customer[bucket].get(name, 0) + 1
        total = sum(counts.values())
        segments = []
        offset = 25
        for b in BUCKETS:
            pct = counts[b] * 100 / total if total else 0
            segments.append({'bucket': b, 'count': counts[b], 'pct': pct, 'gap': 100 - pct,
                             'offset': offset, 'color': BUCKET_COLORS[b]})
            offset -= pct
        sections.append({
            'stage': s, 'total': total, 'segments': segments,
            'breakdown': {b: sorted(by_customer[b].items(), key=lambda kv: (-kv[1], kv[0])) for b in BUCKETS},
        })

    earliest = conn.execute('SELECT MIN(occurred_on) FROM case_stage_status_log').fetchone()[0]
    earliest_monday = cur_monday
    if earliest:
        e = date.fromisoformat(earliest)
        earliest_monday = e - timedelta(days=e.weekday())
    weeks = []
    m = cur_monday
    while m >= earliest_monday and len(weeks) < 52:
        end = m + timedelta(days=6)
        weeks.append({'value': _iso_week_value(m),
                      'label': f'{_iso_week_value(m).replace("-W", "-WK")}（{m.month}/{m.day}～{end.month}/{end.day}）'})
        m -= timedelta(days=7)
    if week_value not in {w['value'] for w in weeks}:
        weeks.append({'value': week_value,
                      'label': f'{week_value.replace("-W", "-WK")}（{monday.month}/{monday.day}～{sunday.month}/{sunday.day}）'})

    customers = [r[0] for r in conn.execute(
        "SELECT DISTINCT customer FROM cases WHERE customer IS NOT NULL AND customer != '' ORDER BY customer")]
    users = conn.execute('SELECT * FROM users WHERE active=1 ORDER BY display_name').fetchall()

    return render_template('weekly.html', sections=sections, buckets=BUCKETS, bucket_colors=BUCKET_COLORS,
                            weeks=weeks, week_value=week_value, customers=customers, users=users,
                            departments=DEPARTMENTS, department_filter=department_filter,
                            customer_filter=customer_filter, owner_filter=owner_filter)


# ── Admin: users ─────────────────────────────────────────────────────────

@app.route('/admin/users', methods=['GET', 'POST'])
@admin_required
def admin_users():
    conn = get_db()
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip()
        password = request.form.get('password', '')
        department = request.form.get('department', '')
        is_admin = 1 if request.form.get('is_admin') else 0
        if not username or not display_name or len(password) < 4:
            error = '請填寫帳號、姓名，密碼至少 4 個字元'
        elif department not in DEPARTMENTS and department != '':
            error = '部門不正確'
        else:
            try:
                conn.execute(
                    'INSERT INTO users (username, password_hash, display_name, department, is_admin) VALUES (?,?,?,?,?)',
                    (username, generate_password_hash(password), display_name, department, is_admin))
                conn.commit()
            except sqlite3.IntegrityError:
                error = '這個帳號已經存在'
    users = conn.execute('SELECT * FROM users ORDER BY active DESC, display_name').fetchall()
    return render_template('admin_users.html', users=users, error=error, departments=DEPARTMENTS)


@app.route('/admin/users/<int:user_id>/toggle_active', methods=['POST'])
@admin_required
def admin_toggle_user(user_id):
    conn = get_db()
    conn.execute('UPDATE users SET active = 1 - active WHERE id=?', (user_id,))
    conn.commit()
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_user_edit(user_id):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return render_template('error.html', message='找不到這個使用者'), 404

    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip()
        department = request.form.get('department', '')
        is_admin = 1 if request.form.get('is_admin') else 0
        if not username or not display_name:
            error = '請填寫帳號、姓名'
        elif department not in DEPARTMENTS and department != '':
            error = '部門不正確'
        else:
            try:
                conn.execute(
                    'UPDATE users SET username=?, display_name=?, department=?, is_admin=? WHERE id=?',
                    (username, display_name, department, is_admin, user_id))
                conn.execute('UPDATE cases SET department=? WHERE owner_id=?', (department, user_id))
                conn.commit()
                return redirect(url_for('admin_users'))
            except sqlite3.IntegrityError:
                error = '這個帳號已經存在'
        user = {**dict(user), 'username': username, 'display_name': display_name,
                'department': department, 'is_admin': is_admin}

    return render_template('admin_user_edit.html', user=user, error=error, departments=DEPARTMENTS)


@app.route('/admin/users/<int:user_id>/reset_password', methods=['POST'])
@admin_required
def admin_reset_password(user_id):
    conn = get_db()
    new_password = request.form.get('new_password', '')
    if len(new_password) >= 4:
        conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                     (generate_password_hash(new_password), user_id))
        conn.commit()
    return redirect(url_for('admin_users'))


# ── Admin: stages ────────────────────────────────────────────────────────

@app.route('/admin/stages', methods=['GET', 'POST'])
@admin_required
def admin_stages():
    conn = get_db()
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        color = request.form.get('color') or '#6b7280'
        description = request.form.get('description', '').strip()
        if not name:
            error = '階段名稱為必填'
        else:
            max_order = conn.execute('SELECT MAX(sort_order) AS m FROM stages').fetchone()['m'] or 0
            try:
                conn.execute('INSERT INTO stages (name, color, description, sort_order) VALUES (?,?,?,?)',
                             (name, color, description, max_order + 1))
                _sync_case_stage_rows(conn)
                conn.commit()
            except sqlite3.IntegrityError:
                error = '這個階段已經存在'
    stages = conn.execute('''
        SELECT s.*,
               COALESCE(SUM(CASE WHEN css.status != '未開始' THEN 1 ELSE 0 END), 0) AS active_case_count
        FROM stages s
        LEFT JOIN case_stage_status css ON css.stage_id = s.id
        GROUP BY s.id ORDER BY s.sort_order
    ''').fetchall()
    return render_template('admin_stages.html', stages=stages, error=error)


@app.route('/admin/stages/<int:stage_id>/edit', methods=['POST'])
@admin_required
def admin_stage_edit(stage_id):
    conn = get_db()
    name = request.form.get('name', '').strip()
    color = request.form.get('color') or '#6b7280'
    description = request.form.get('description', '').strip()
    if name:
        conn.execute('UPDATE stages SET name=?, color=?, description=? WHERE id=?',
                     (name, color, description, stage_id))
        conn.commit()
    return redirect(url_for('admin_stages'))


@app.route('/admin/stages/<int:stage_id>/delete', methods=['POST'])
@admin_required
def admin_stage_delete(stage_id):
    conn = get_db()
    in_use = conn.execute(
        "SELECT COUNT(*) FROM case_stage_status WHERE stage_id=? AND status!='未開始'",
        (stage_id,)).fetchone()[0]
    if in_use == 0:
        conn.execute('DELETE FROM stages WHERE id=?', (stage_id,))
        conn.commit()
    return redirect(url_for('admin_stages'))


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=8090)
else:
    init_db()
