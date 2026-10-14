import functools
import os
import secrets
import sqlite3
from datetime import date

from flask import Flask, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'case_tracker.db')

app = Flask(__name__)

DEFAULT_STATUSES = [
    ('開案 Kick-off', '#6b7280', '收到客戶 SWR 或開案通知，案件正式成立'),
    ('圖面 & Layout 確認', '#0ea5e9', 'BD 圖 / SBT Layout / LF 圖面經客戶書面確認'),
    ('材料到位', '#3b82f6', '基板、導線架、線材、compound 等關鍵料全數入庫'),
    ('1st ES 投料', '#8b5cf6', '工程批工單下線並實際投料'),
    ('ES 出貨', '#d946ef', '工程樣品出貨予客戶'),
    ('PKG Qual & RA', '#f59e0b', '廠內封裝可靠度測試完成並判定（MSL/TCT/HTSL/THT）'),
    ('客戶驗證 / Qual Report', '#f97316', '客戶回覆 PASS，Qual report 正式結案'),
    ('PNR / 量產轉移', '#16a34a', '量產工單投料，案件轉入量產管理'),
]
PRIORITIES = ['高', '中', '低']
DEPARTMENTS = ['2620', '2640', '2650']


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


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
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

            CREATE TABLE IF NOT EXISTS statuses (
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
                status_id INTEGER REFERENCES statuses(id),
                note TEXT,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        ''')

        user_cols = {r['name'] for r in conn.execute('PRAGMA table_info(users)')}
        if 'department' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN department TEXT NOT NULL DEFAULT ''")
        case_cols = {r['name'] for r in conn.execute('PRAGMA table_info(cases)')}
        if 'department' not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN department TEXT NOT NULL DEFAULT ''")

        if conn.execute('SELECT COUNT(*) FROM statuses').fetchone()[0] == 0:
            for i, (name, color, description) in enumerate(DEFAULT_STATUSES):
                conn.execute('INSERT INTO statuses (name, color, description, sort_order) VALUES (?,?,?,?)',
                             (name, color, description, i))

        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            conn.execute(
                'INSERT INTO users (username, password_hash, display_name, is_admin) VALUES (?,?,?,1)',
                ('admin', generate_password_hash('admin123'), '管理員')
            )

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


# ── Dashboard ────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    conn = get_db()
    today = date.today().isoformat()

    status_counts = conn.execute('''
        SELECT s.id, s.name, s.color, COUNT(c.id) AS cnt
        FROM statuses s
        LEFT JOIN cases c ON c.status_id = s.id
        GROUP BY s.id ORDER BY s.sort_order
    ''').fetchall()

    total_cases = conn.execute('SELECT COUNT(*) FROM cases').fetchone()[0]

    done_status = conn.execute("SELECT id FROM statuses WHERE name='PNR / 量產轉移'").fetchone()
    done_id = done_status['id'] if done_status else -1

    overdue = conn.execute('''
        SELECT c.*, u.display_name AS owner_name, s.name AS status_name, s.color AS status_color
        FROM cases c
        JOIN users u ON u.id = c.owner_id
        LEFT JOIN statuses s ON s.id = c.status_id
        WHERE c.due_date IS NOT NULL AND c.due_date != '' AND c.due_date < ?
          AND (c.status_id IS NULL OR c.status_id != ?)
        ORDER BY c.due_date
    ''', (today, done_id)).fetchall()

    by_engineer = conn.execute('''
        SELECT u.id, u.display_name,
               COUNT(c.id) AS total,
               SUM(CASE WHEN c.status_id = ? THEN 1 ELSE 0 END) AS done_cnt,
               ROUND(AVG(c.progress), 0) AS avg_progress
        FROM users u
        LEFT JOIN cases c ON c.owner_id = u.id
        WHERE u.active = 1
        GROUP BY u.id
        ORDER BY total DESC, u.display_name
    ''', (done_id,)).fetchall()

    recent = conn.execute('''
        SELECT c.*, u.display_name AS owner_name, s.name AS status_name, s.color AS status_color
        FROM cases c
        JOIN users u ON u.id = c.owner_id
        LEFT JOIN statuses s ON s.id = c.status_id
        ORDER BY c.updated_at DESC LIMIT 10
    ''').fetchall()

    return render_template('dashboard.html',
                            status_counts=status_counts,
                            total_cases=total_cases,
                            overdue=overdue,
                            by_engineer=by_engineer,
                            recent=recent,
                            today=today)


# ── Cases ────────────────────────────────────────────────────────────────

@app.route('/cases')
@login_required
def case_list():
    conn = get_db()
    owner_filter = request.args.get('owner', '')
    status_filter = request.args.get('status', '')
    department_filter = request.args.get('department', '')

    query = '''
        SELECT c.*, u.display_name AS owner_name, s.name AS status_name, s.color AS status_color
        FROM cases c
        JOIN users u ON u.id = c.owner_id
        LEFT JOIN statuses s ON s.id = c.status_id
        WHERE 1=1
    '''
    params = []
    if owner_filter:
        query += ' AND c.owner_id = ?'
        params.append(owner_filter)
    if status_filter:
        query += ' AND c.status_id = ?'
        params.append(status_filter)
    if department_filter:
        query += ' AND c.department = ?'
        params.append(department_filter)
    query += ' ORDER BY c.updated_at DESC'

    cases = conn.execute(query, params).fetchall()
    users = conn.execute('SELECT * FROM users WHERE active=1 ORDER BY display_name').fetchall()
    statuses = conn.execute('SELECT * FROM statuses ORDER BY sort_order').fetchall()

    return render_template('case_list.html', cases=cases, users=users, statuses=statuses,
                            departments=DEPARTMENTS,
                            owner_filter=owner_filter, status_filter=status_filter,
                            department_filter=department_filter)


def _case_form_context(case=None):
    conn = get_db()
    users = conn.execute('SELECT * FROM users WHERE active=1 ORDER BY display_name').fetchall()
    statuses = conn.execute('SELECT * FROM statuses ORDER BY sort_order').fetchall()
    return dict(case=case, users=users, statuses=statuses, priorities=PRIORITIES)


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
        conn.execute('''
            INSERT INTO cases (title, owner_id, department, customer, priority, due_date, progress, status_id, note, created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', (title, owner_id, department, request.form.get('customer', ''), request.form.get('priority', '中'),
              request.form.get('due_date') or None, int(request.form.get('progress') or 0),
              request.form.get('status_id') or None, request.form.get('note', ''), session['user_id']))
        conn.commit()
        return redirect(url_for('case_list'))
    return render_template('case_form.html', **_case_form_context())


@app.route('/cases/<int:case_id>/edit', methods=['GET', 'POST'])
@login_required
def case_edit(case_id):
    conn = get_db()
    case = conn.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    if not case:
        return render_template('error.html', message='找不到這個案子'), 404
    if not can_edit_case(case):
        return render_template('error.html', message='只有負責工程師或管理員才能修改此案子'), 403

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        owner_id = case['owner_id']
        if session.get('is_admin'):
            owner_id = request.form.get('owner_id') or case['owner_id']
        if not title:
            return render_template('case_form.html', error='案名為必填', **_case_form_context(case)), 400
        department = _user_department(conn, owner_id)
        conn.execute('''
            UPDATE cases SET title=?, owner_id=?, department=?, customer=?, priority=?, due_date=?,
                progress=?, status_id=?, note=?, updated_at=datetime('now')
            WHERE id=?
        ''', (title, owner_id, department, request.form.get('customer', ''), request.form.get('priority', '中'),
              request.form.get('due_date') or None, int(request.form.get('progress') or 0),
              request.form.get('status_id') or None, request.form.get('note', ''), case_id))
        conn.commit()
        return redirect(url_for('case_list'))

    return render_template('case_form.html', **_case_form_context(case))


@app.route('/cases/<int:case_id>/delete', methods=['POST'])
@login_required
def case_delete(case_id):
    conn = get_db()
    case = conn.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    if not case:
        return render_template('error.html', message='找不到這個案子'), 404
    if not can_edit_case(case):
        return render_template('error.html', message='只有負責工程師或管理員才能刪除此案子'), 403
    conn.execute('DELETE FROM cases WHERE id=?', (case_id,))
    conn.commit()
    return redirect(url_for('case_list'))


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


# ── Admin: statuses ──────────────────────────────────────────────────────

@app.route('/admin/statuses', methods=['GET', 'POST'])
@admin_required
def admin_statuses():
    conn = get_db()
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        color = request.form.get('color') or '#6b7280'
        description = request.form.get('description', '').strip()
        if not name:
            error = '狀態名稱為必填'
        else:
            max_order = conn.execute('SELECT MAX(sort_order) AS m FROM statuses').fetchone()['m'] or 0
            try:
                conn.execute('INSERT INTO statuses (name, color, description, sort_order) VALUES (?,?,?,?)',
                             (name, color, description, max_order + 1))
                conn.commit()
            except sqlite3.IntegrityError:
                error = '這個狀態已經存在'
    statuses = conn.execute('''
        SELECT s.*, COUNT(c.id) AS case_count
        FROM statuses s
        LEFT JOIN cases c ON c.status_id = s.id
        GROUP BY s.id ORDER BY s.sort_order
    ''').fetchall()
    return render_template('admin_statuses.html', statuses=statuses, error=error)


@app.route('/admin/statuses/<int:status_id>/edit', methods=['POST'])
@admin_required
def admin_status_edit(status_id):
    conn = get_db()
    name = request.form.get('name', '').strip()
    color = request.form.get('color') or '#6b7280'
    description = request.form.get('description', '').strip()
    if name:
        conn.execute('UPDATE statuses SET name=?, color=?, description=? WHERE id=?',
                     (name, color, description, status_id))
        conn.commit()
    return redirect(url_for('admin_statuses'))


@app.route('/admin/statuses/<int:status_id>/delete', methods=['POST'])
@admin_required
def admin_status_delete(status_id):
    conn = get_db()
    in_use = conn.execute('SELECT COUNT(*) FROM cases WHERE status_id=?', (status_id,)).fetchone()[0]
    if in_use == 0:
        conn.execute('DELETE FROM statuses WHERE id=?', (status_id,))
        conn.commit()
    return redirect(url_for('admin_statuses'))


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=8090)
else:
    init_db()
