import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, datetime, timedelta
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify
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
    email = db.Column(db.String(120), nullable=True)  # Новое поле для email
    meds = db.relationship('Medication', backref='owner', lazy=True)


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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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
    today = date.today()
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


# --- МАРШРУТЫ (ROUTES) ---

@app.route('/')
@login_required
def index():
    meds = sorted(current_user.meds, key=lambda m: m.expiry_date)
    stats = get_med_stats(meds)

    # Добавляем статус к каждому лекарству для шаблона
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

        new_user = User(
            username=username,
            password=generate_password_hash(request.form['password']),
            email=request.form.get('email', '').strip() or None
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
    return redirect(url_for('index'))


@app.route('/delete/<int:id>')
@login_required
def delete_med(id):
    med = Medication.query.get(id)
    if med and med.owner == current_user:
        db.session.delete(med)
        db.session.commit()
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
    """Отправляет email с просроченными и скоро истекающими лекарствами"""
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
        flash('Email сохранён!', 'success')
        return redirect(url_for('index'))
    return render_template('profile.html')


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))


# --- ИНИЦИАЛИЗАЦИЯ ПРИ ЗАПУСКЕ ---
def init_db():
    with app.app_context():
        db.create_all()

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