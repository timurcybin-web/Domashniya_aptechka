import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev_key_123'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///meds.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Настройки email
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USERNAME'] = 'domashniyaaptechka@gmail.com'
app.config['MAIL_PASSWORD'] = 'rkpnarlnokpiawxt'
app.config['MAIL_FROM'] = 'domashniyaaptechka@gmail.com'

EXPIRY_WARNING_DAYS = 30  # За сколько дней предупреждать

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


# --- МОДЕЛИ ДАННЫХ ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    birth_date = db.Column(db.Date, nullable=True)
    gender = db.Column(db.String(1), nullable=True)  # 'M' or 'F'
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    meds = db.relationship('Medication', backref='owner', lazy=True)
    sessions = db.relationship('UserSession', backref='user', lazy=True)


class Medication(db.Model):
    """Лекарства в аптечке конкретного пользователя"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    expiry_date = db.Column(db.String(10), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class Directory(db.Model):
    """Общий справочник лекарств для подсказок"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), index=True)
    description = db.Column(db.Text)


class UserSession(db.Model):
    """Сессии пользователей — для аналитики"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_token = db.Column(db.String(64), nullable=False, index=True)
    login_time = db.Column(db.DateTime, default=datetime.utcnow)
    logout_time = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    page_views = db.Column(db.Integer, default=0)
    actions_count = db.Column(db.Integer, default=0)  # добавил/удалил лекарство, отправил email и т.д.

    @property
    def duration_minutes(self):
        end = self.logout_time or datetime.utcnow()
        return int((end - self.login_time).total_seconds() / 60)

    @property
    def is_active(self):
        return self.logout_time is None


class PageView(db.Model):
    """Лог каждого посещённого URL в рамках сессии"""
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('user_session.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    page = db.Column(db.String(200), nullable=False)
    visited_at = db.Column(db.DateTime, default=datetime.utcnow)
    method = db.Column(db.String(10), default='GET')


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def admin_required(f):
    """Декоратор: доступ только для администраторов."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# --- ХЕЛПЕРЫ СЕССИЙ ---

def _get_or_create_session():
    """Получает текущую сессию из Flask-session или создаёт новую."""
    token = session.get('session_token')
    if not token:
        return None
    return UserSession.query.filter_by(session_token=token, logout_time=None).first()


def _start_user_session(user):
    """Создаёт запись о новой сессии в БД."""
    import secrets
    token = secrets.token_hex(32)
    session['session_token'] = token

    ua = request.headers.get('User-Agent', '')[:255]
    ip = request.remote_addr

    us = UserSession(
        user_id=user.id,
        session_token=token,
        ip_address=ip,
        user_agent=ua,
    )
    db.session.add(us)
    db.session.commit()
    return us


def _end_user_session():
    """Закрывает текущую сессию (выход из системы или истечение)."""
    token = session.get('session_token')
    if token:
        us = UserSession.query.filter_by(session_token=token, logout_time=None).first()
        if us:
            us.logout_time = datetime.utcnow()
            db.session.commit()
    session.pop('session_token', None)


def _log_page_view(page):
    """Записывает просмотр страницы и увеличивает счётчик."""
    us = _get_or_create_session()
    if us is None:
        return
    pv = PageView(session_id=us.id, user_id=us.user_id, page=page, method=request.method)
    db.session.add(pv)
    us.page_views += 1
    db.session.commit()


def _log_action():
    """Увеличивает счётчик действий текущей сессии."""
    us = _get_or_create_session()
    if us:
        us.actions_count += 1
        db.session.commit()


# --- ПЕРЕХВАТЧИК ЗАПРОСОВ (автоматически логируем страницы) ---

TRACKED_PAGES = {'/', '/login', '/register', '/profile', '/send_notification'}

@app.after_request
def track_page(response):
    """После каждого успешного GET-запроса логируем просмотр страницы."""
    if request.method == 'GET' and response.status_code == 200:
        path = request.path
        # Исключаем статику и AJAX-поиск
        if not path.startswith('/static') and path != '/search_meds':
            if current_user.is_authenticated:
                _log_page_view(path)
    return response


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_expiry_status(expiry_str):
    """Возвращает статус срока годности: 'expired', 'warning', 'ok'"""
    try:
        expiry = datetime.strptime(expiry_str, '%Y-%m-%d').date()
        today = date.today()
        if expiry < today:
            return 'expired'
        elif expiry <= today + timedelta(days=EXPIRY_WARNING_DAYS):
            return 'warning'
        return 'ok'
    except Exception:
        return 'ok'


def get_med_stats(meds):
    """Подсчитывает статистику по аптечке"""
    total = len(meds)
    expired = 0
    expiring_soon = 0
    for med in meds:
        status = get_expiry_status(med.expiry_date)
        if status == 'expired':
            expired += 1
        elif status == 'warning':
            expiring_soon += 1
    return {'total': total, 'expired': expired, 'expiring_soon': expiring_soon}


def send_expiry_email(user, expiring_meds):
    """Отправляет письмо пользователю со списком истекающих лекарств"""
    if not user.email:
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = '💊 Домашняя аптечка: лекарства с истекающим сроком годности'
        msg['From'] = app.config['MAIL_FROM']
        msg['To'] = user.email

        rows = ''.join(
            f'<tr><td style="padding:8px;border-bottom:1px solid #eee"><b>{m.name}</b></td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;color:{"#dc3545" if get_expiry_status(m.expiry_date)=="expired" else "#fd7e14"}">'
            f'{"⚠️ ПРОСРОЧЕНО" if get_expiry_status(m.expiry_date)=="expired" else m.expiry_date}'
            f'</td></tr>'
            for m in expiring_meds
        )

        html = f"""
        <div style="font-family:sans-serif;max-width:500px;margin:0 auto">
          <h2 style="color:#0d6efd">💊 Домашняя Аптечка</h2>
          <p>Здравствуйте, <b>{user.username}</b>!</p>
          <p>Следующие лекарства требуют вашего внимания:</p>
          <table style="width:100%;border-collapse:collapse">
            <thead><tr style="background:#f8f9fa">
              <th style="padding:8px;text-align:left">Препарат</th>
              <th style="padding:8px;text-align:left">Срок годности</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <p style="margin-top:20px;color:#6c757d;font-size:0.85rem">
            Пожалуйста, проверьте аптечку и замените просроченные препараты.
          </p>
        </div>
        """

        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP(app.config['MAIL_SERVER'], app.config['MAIL_PORT']) as server:
            server.starttls()
            server.login(app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'])
            server.sendmail(app.config['MAIL_FROM'], user.email, msg.as_string())

        return True
    except Exception as e:
        print(f"Ошибка отправки email: {e}")
        return False


# --- МАРШРУТЫ ---

@app.route('/')
@login_required
def index():
    meds = sorted(current_user.meds, key=lambda m: m.expiry_date)
    stats = get_med_stats(meds)
    meds_with_status = [(med, get_expiry_status(med.expiry_date)) for med in meds]
    return render_template('index.html',
                           meds_with_status=meds_with_status,
                           stats=stats,
                           warning_days=EXPIRY_WARNING_DAYS)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and check_password_hash(user.password, request.form['password']):
            login_user(user)
            _start_user_session(user)
            return redirect(url_for('index'))
        flash('Неверный логин или пароль')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        if User.query.filter_by(username=username).first():
            flash('Пользователь уже существует')
            return redirect(url_for('register'))

        birth_date_str = request.form.get('birth_date', '').strip()
        try:
            birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d').date() if birth_date_str else None
        except ValueError:
            birth_date = None
        gender = request.form.get('gender', '').strip() or None

        new_user = User(
            username=username,
            password=generate_password_hash(request.form['password']),
            email=request.form.get('email', '').strip() or None,
            birth_date=birth_date,
            gender=gender,
        )
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/add', methods=['POST'])
@login_required
def add_med():
    name = request.form.get('name')
    expiry = request.form.get('expiry')

    dir_entry = Directory.query.filter_by(name=name).first()
    desc = dir_entry.description if dir_entry else "Описание не найдено в справочнике"

    if name and expiry:
        new_med = Medication(name=name, expiry_date=expiry, description=desc, owner=current_user)
        db.session.add(new_med)
        db.session.commit()
        _log_action()
    return redirect(url_for('index'))


@app.route('/delete/<int:id>')
@login_required
def delete_med(id):
    med = Medication.query.get(id)
    if med and med.owner == current_user:
        db.session.delete(med)
        db.session.commit()
        _log_action()
    return redirect(url_for('index'))


@app.route('/search_meds')
@login_required
def search_meds():
    query = request.args.get('q', '')
    if len(query) < 2:
        return jsonify([])
    results = Directory.query.filter(Directory.name.ilike(f'%{query}%')).limit(10).all()
    return jsonify([{'name': r.name, 'desc': r.description} for r in results])


@app.route('/send_notification')
@login_required
def send_notification():
    meds = current_user.meds
    problem_meds = [m for m in meds if get_expiry_status(m.expiry_date) in ('expired', 'warning')]

    if not problem_meds:
        flash('Всё в порядке — нет лекарств с истекающим сроком годности.', 'success')
        return redirect(url_for('index'))

    if not current_user.email:
        flash('Укажите email в профиле для получения уведомлений.', 'warning')
        return redirect(url_for('index'))

    success = send_expiry_email(current_user, problem_meds)
    if success:
        _log_action()
        flash(f'✅ Письмо отправлено на {current_user.email}', 'success')
    else:
        flash('❌ Ошибка при отправке письма. Проверьте настройки почты в app.py.', 'danger')

    return redirect(url_for('index'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        current_user.email = email or None
        db.session.commit()
        _log_action()
        flash('Email сохранён!', 'success')
        return redirect(url_for('index'))
    return render_template('profile.html')


@app.route('/logout')
def logout():
    _end_user_session()
    logout_user()
    return redirect(url_for('login'))


# --- АНАЛИТИКА СЕССИЙ ---

@app.route('/analytics')
@login_required
def analytics():
    """Страница аналитики сессий для текущего пользователя."""
    user_sessions = (UserSession.query
                     .filter_by(user_id=current_user.id)
                     .order_by(UserSession.login_time.desc())
                     .limit(50)
                     .all())

    # --- Статистика по дням (последние 30 дней) ---
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    daily_raw = (db.session.query(
                     db.func.date(UserSession.login_time).label('day'),
                     db.func.count(UserSession.id).label('visits')
                 )
                 .filter(UserSession.user_id == current_user.id,
                         UserSession.login_time >= thirty_days_ago)
                 .group_by(db.func.date(UserSession.login_time))
                 .order_by('day')
                 .all())
    daily_stats = [{'day': str(r.day), 'visits': r.visits} for r in daily_raw]

    # --- Самые посещаемые страницы ---
    page_raw = (db.session.query(
                    PageView.page,
                    db.func.count(PageView.id).label('cnt')
                )
                .filter(PageView.user_id == current_user.id)
                .group_by(PageView.page)
                .order_by(db.desc('cnt'))
                .limit(10)
                .all())
    top_pages = [{'page': r.page, 'cnt': r.cnt} for r in page_raw]

    # --- Общие показатели ---
    total_sessions = UserSession.query.filter_by(user_id=current_user.id).count()
    total_page_views = db.session.query(db.func.sum(UserSession.page_views)).filter_by(user_id=current_user.id).scalar() or 0
    total_actions = db.session.query(db.func.sum(UserSession.actions_count)).filter_by(user_id=current_user.id).scalar() or 0
    avg_duration = (db.session.query(db.func.avg(
                        db.func.julianday(db.func.coalesce(UserSession.logout_time, db.func.datetime('now')))
                        - db.func.julianday(UserSession.login_time)
                    ) * 1440)
                    .filter(UserSession.user_id == current_user.id)
                    .scalar())
    avg_duration = round(avg_duration or 0, 1)

    return render_template('analytics.html',
                           user_sessions=user_sessions,
                           daily_stats=daily_stats,
                           top_pages=top_pages,
                           total_sessions=total_sessions,
                           total_page_views=int(total_page_views),
                           total_actions=int(total_actions),
                           avg_duration=avg_duration)


# --- АДМИН: ОБЩАЯ АНАЛИТИКА ---

@app.route('/admin/analytics')
@login_required
@admin_required
def admin_analytics():
    """Общая аналитика по всем пользователям — только для администраторов."""

    # --- фильтры ---
    selected_user_id = request.args.get('user_id', type=int)
    period_days = request.args.get('period', default=30, type=int)
    if period_days not in (7, 30, 90):
        period_days = 30
    selected_gender = request.args.get('gender', '')
    age_min = request.args.get('age_min', type=int)
    age_max = request.args.get('age_max', type=int)

    since = datetime.utcnow() - timedelta(days=period_days)
    today = date.today()

    all_users = User.query.order_by(User.username).all()

    # --- демографический фильтр: собираем user_id попавших в выборку ---
    demo_ids = None  # None = без демо-фильтра
    if selected_gender or age_min or age_max:
        uq = User.query
        if selected_gender:
            uq = uq.filter(User.gender == selected_gender)
        if age_min:
            uq = uq.filter(User.birth_date <= date(today.year - age_min, today.month, today.day))
        if age_max:
            uq = uq.filter(User.birth_date >= date(today.year - age_max, today.month, today.day))
        demo_ids = [u.id for u in uq.with_entities(User.id).all()]

    def uid_cond(col):
        """Условия фильтрации по user_id с учётом всех активных фильтров."""
        if selected_user_id:
            return [col == selected_user_id]
        if demo_ids is not None:
            return [col.in_(demo_ids if demo_ids else [-1])]
        return []

    # --- сводные карточки ---
    sf = [UserSession.login_time >= since] + uid_cond(UserSession.user_id)
    total_sessions   = UserSession.query.filter(*sf).count()
    total_page_views = db.session.query(db.func.sum(UserSession.page_views)).filter(*sf).scalar() or 0
    total_actions    = db.session.query(db.func.sum(UserSession.actions_count)).filter(*sf).scalar() or 0
    unique_users     = db.session.query(db.func.count(db.func.distinct(UserSession.user_id))).filter(*sf).scalar() or 0
    total_users      = User.query.count()

    # --- визиты по дням ---
    daily_raw = (db.session.query(
                     db.func.date(UserSession.login_time).label('day'),
                     db.func.count(UserSession.id).label('visits')
                 )
                 .filter(*sf)
                 .group_by(db.func.date(UserSession.login_time))
                 .order_by('day').all())
    daily_stats = [{'day': str(r.day), 'visits': r.visits} for r in daily_raw]

    # --- активность по пользователям ---
    uv_raw = (db.session.query(
                  User.id, User.username, User.gender, User.birth_date, User.is_admin,
                  db.func.count(UserSession.id).label('visits'),
                  db.func.sum(UserSession.page_views).label('views'),
                  db.func.sum(UserSession.actions_count).label('actions'),
                  db.func.avg(
                      (db.func.julianday(db.func.coalesce(UserSession.logout_time, db.func.datetime('now')))
                       - db.func.julianday(UserSession.login_time)) * 1440
                  ).label('avg_min')
              )
              .join(User, UserSession.user_id == User.id)
              .filter(*sf)
              .group_by(User.id)
              .order_by(db.desc('visits')).all())
    user_visits = [{
        'id': r.id,
        'username': r.username,
        'gender': r.gender or '—',
        'age': (today.year - r.birth_date.year) if r.birth_date else '—',
        'is_admin': r.is_admin,
        'visits': r.visits,
        'views': int(r.views or 0),
        'actions': int(r.actions or 0),
        'avg_min': round(r.avg_min or 0, 1),
    } for r in uv_raw]

    # --- топ страниц ---
    pf = [PageView.visited_at >= since] + uid_cond(PageView.user_id)
    page_raw = (db.session.query(PageView.page, db.func.count(PageView.id).label('cnt'))
                .filter(*pf).group_by(PageView.page).order_by(db.desc('cnt')).limit(10).all())
    top_pages = [{'page': r.page, 'cnt': r.cnt} for r in page_raw]

    # --- последние сессии ---
    recent_sessions = (UserSession.query
                       .join(User, UserSession.user_id == User.id)
                       .filter(*sf)
                       .order_by(UserSession.login_time.desc()).limit(100).all())

    # --- демография (с учётом перекрёстных фильтров) ---

    # Количество пользователей, попавших в демографический фильтр
    filtered_users_count = len(demo_ids) if demo_ids is not None else total_users

    # График пола: применяем только возрастной фильтр (пол — сама ось)
    gq = User.query
    if age_min:
        gq = gq.filter(User.birth_date <= date(today.year - age_min, today.month, today.day))
    if age_max:
        gq = gq.filter(User.birth_date >= date(today.year - age_max, today.month, today.day))
    gender_stats = {
        'F':       gq.filter(User.gender == 'F').count(),
        'M':       gq.filter(User.gender == 'M').count(),
        'unknown': gq.filter(User.gender.is_(None)).count(),
    }

    # График возраста: применяем только гендерный фильтр (возраст — сама ось)
    aq = User.query.filter(User.birth_date.isnot(None))
    if selected_gender:
        aq = aq.filter(User.gender == selected_gender)
    bucket_keys = ['20–24', '25–29', '30–34', '35–39', '40–44', '45–50', 'другой']
    age_buckets = {k: 0 for k in bucket_keys}
    ages_all = []
    for u in aq.all():
        age = today.year - u.birth_date.year
        ages_all.append(age)
        if   20 <= age <= 24: age_buckets['20–24'] += 1
        elif 25 <= age <= 29: age_buckets['25–29'] += 1
        elif 30 <= age <= 34: age_buckets['30–34'] += 1
        elif 35 <= age <= 39: age_buckets['35–39'] += 1
        elif 40 <= age <= 44: age_buckets['40–44'] += 1
        elif 45 <= age <= 50: age_buckets['45–50'] += 1
        else:                 age_buckets['другой'] += 1
    avg_age = round(sum(ages_all) / len(ages_all), 1) if ages_all else None

    # Реестр пользователей (все зарегистрированные, с демо-фильтром)
    reg_q = User.query
    if selected_gender:
        reg_q = reg_q.filter(User.gender == selected_gender)
    if age_min:
        reg_q = reg_q.filter(User.birth_date <= date(today.year - age_min, today.month, today.day))
    if age_max:
        reg_q = reg_q.filter(User.birth_date >= date(today.year - age_max, today.month, today.day))
    user_registry = []
    for u in reg_q.order_by(User.username).all():
        user_registry.append({
            'id':         u.id,
            'username':   u.username,
            'gender':     u.gender,
            'age':        (today.year - u.birth_date.year) if u.birth_date else None,
            'birth_date': str(u.birth_date) if u.birth_date else None,
            'email':      u.email,
            'is_admin':   u.is_admin,
            'meds_count': Medication.query.filter_by(user_id=u.id).count(),
        })

    return render_template('admin_analytics.html',
                           all_users=all_users,
                           selected_user_id=selected_user_id,
                           period_days=period_days,
                           selected_gender=selected_gender,
                           age_min=age_min or '',
                           age_max=age_max or '',
                           total_sessions=total_sessions,
                           total_page_views=int(total_page_views),
                           total_actions=int(total_actions),
                           unique_users=unique_users,
                           total_users=total_users,
                           daily_stats=daily_stats,
                           user_visits=user_visits,
                           top_pages=top_pages,
                           recent_sessions=recent_sessions,
                           gender_stats=gender_stats,
                           age_buckets=age_buckets,
                           avg_age=avg_age,
                           filtered_users_count=filtered_users_count,
                           user_registry=user_registry)


@app.route('/admin/make_admin/<int:user_id>')
@login_required
@admin_required
def make_admin(user_id):
    """Выдать/снять права администратора."""
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Нельзя изменить собственные права.', 'warning')
    else:
        user.is_admin = not user.is_admin
        db.session.commit()
        action = 'выданы' if user.is_admin else 'сняты'
        flash(f'Права администратора {action} пользователю {user.username}.', 'success')
    return redirect(url_for('admin_analytics'))


# --- ИНИЦИАЛИЗАЦИЯ ПРИ ЗАПУСКЕ ---
def init_db():
    with app.app_context():
        db.create_all()

        # Миграция: добавить столбец is_admin если его нет (для старых БД)
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('user')]
        if 'is_admin' not in columns:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0'))
                conn.commit()
            print("Миграция: добавлен столбец is_admin.")
        if 'birth_date' not in columns:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN birth_date DATE'))
                conn.commit()
            print("Миграция: добавлен столбец birth_date.")
        if 'gender' not in columns:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN gender VARCHAR(1)'))
                conn.commit()
            print("Миграция: добавлен столбец gender.")

        # Миграция: создать таблицы user_session и page_view если их нет
        # (они создаются через db.create_all() выше, но на всякий случай)

        # Первый зарегистрированный пользователь автоматически становится администратором
        first_user = User.query.first()
        if first_user and not first_user.is_admin:
            first_user.is_admin = True
            db.session.commit()
            print(f"Пользователь '{first_user.username}' назначен администратором.")

        if not Directory.query.first():
            test_data = [
                {'name': 'Парацетамол', 'desc': 'Жаропонижающее и обезболивающее средство.'},
                {'name': 'Ибупрофен', 'desc': 'Противовоспалительное средство, помогает от боли.'},
                {'name': 'Аспирин', 'desc': 'Обезболивающее, жаропонижающее, разжижает кровь.'},
                {'name': 'Но-шпа', 'desc': 'Снимает спазмы гладкой мускулатуры.'},
                {'name': 'Лоратадин', 'desc': 'Антигистаминное (от аллергии) средство.'}
            ]
            for item in test_data:
                db.session.add(Directory(name=item['name'], description=item['desc']))
            db.session.commit()
            print("База данных создана, справочник наполнен тестовыми данными.")


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
