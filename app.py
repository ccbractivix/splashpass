import os
import csv
import io
import uuid
import base64
from datetime import datetime, date, timedelta

import pytz
import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, Response
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///splashpass.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
EASTERN = pytz.timezone('US/Eastern')
DEFAULT_CAPACITY = 128
MAX_PARTY = 6
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_number = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    enrollment_type = db.Column(db.String(50))
    expiration_date = db.Column(db.String(50))
    membership = db.Column(db.String(20), nullable=False)
    active = db.Column(db.Boolean, default=True)
    reservations = db.relationship('Reservation', backref='member', lazy=True)

class DayType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    day_type = db.Column(db.String(20), nullable=False)
    capacity_override = db.Column(db.Integer)

class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    confirmation_code = db.Column(db.String(20), unique=True, nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    reservation_date = db.Column(db.Date, nullable=False)
    party_size = db.Column(db.Integer, nullable=False)
    arrived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(EASTERN))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def today_eastern():
    return datetime.now(EASTERN).date()

def get_day_type(d):
    dt = DayType.query.filter_by(date=d).first()
    if dt:
        return dt.day_type
    return 'Weekend' if d.weekday() >= 5 else 'Weekday'

def get_capacity(d):
    dt = DayType.query.filter_by(date=d).first()
    if dt and dt.capacity_override:
        return dt.capacity_override
    return DEFAULT_CAPACITY

def current_heads(d):
    result = db.session.query(db.func.sum(Reservation.party_size)).filter(
        Reservation.reservation_date == d
    ).scalar()
    return result or 0

def generate_confirmation():
    return 'SP-' + uuid.uuid4().hex[:8].upper()

def make_qr_base64(data):
    qr = qrcode.make(data)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            flash('Please log in.', 'danger')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/reserve', methods=['GET', 'POST'])
def reserve():
    if request.method == 'GET':
        return render_template('reserve.html')

    owner_number = request.form.get('owner_number', '').strip()
    last_name = request.form.get('last_name', '').strip()
    reservation_date_str = request.form.get('reservation_date', '')
    party_size = int(request.form.get('party_size', 1))

    # --- Validate member ---
    member = Member.query.filter(
        db.func.lower(Member.owner_number) == owner_number.lower(),
        db.func.lower(Member.last_name) == last_name.lower(),
        Member.active == True
    ).first()

    if not member:
        flash('Owner number and last name not found or membership inactive.', 'danger')
        return redirect(url_for('reserve'))

    # --- Validate date ---
    try:
        res_date = date.fromisoformat(reservation_date_str)
    except ValueError:
        flash('Invalid date.', 'danger')
        return redirect(url_for('reserve'))

    now = today_eastern()
    if res_date < now:
        flash('Cannot book in the past.', 'danger')
        return redirect(url_for('reserve'))

    # --- Membership rules ---
    days_ahead = (res_date - now).days
    day_type = get_day_type(res_date)
    tier = member.membership.strip().lower()

    if tier == 'platinum':
        if days_ahead > 6:
            flash('Platinum members can book up to 6 days in advance.', 'danger')
            return redirect(url_for('reserve'))
    elif tier == 'gold':
        if days_ahead > 0:
            flash('Gold members can only book same-day.', 'danger')
            return redirect(url_for('reserve'))
        if day_type == 'High Use':
            flash('Gold members cannot book High Use days.', 'danger')
            return redirect(url_for('reserve'))
    elif tier == 'silver':
        if days_ahead > 0:
            flash('Silver members can only book same-day.', 'danger')
            return redirect(url_for('reserve'))
        if day_type in ('Weekend', 'High Use'):
            flash('Silver members can only book Weekdays.', 'danger')
            return redirect(url_for('reserve'))
    else:
        flash('Unknown membership tier.', 'danger')
        return redirect(url_for('reserve'))

    # --- Party size ---
    if party_size < 1 or party_size > MAX_PARTY:
        flash(f'Party size must be 1-{MAX_PARTY}.', 'danger')
        return redirect(url_for('reserve'))

    # --- Capacity ---
    cap = get_capacity(res_date)
    used = current_heads(res_date)
    if used + party_size > cap:
        remaining = cap - used
        flash(f'Not enough capacity. {remaining} spots remaining.', 'danger')
        return redirect(url_for('reserve'))

    # --- Duplicate check ---
    existing = Reservation.query.filter_by(
        member_id=member.id,
        reservation_date=res_date
    ).first()
    if existing:
        flash('You already have a reservation for this date.', 'warning')
        return redirect(url_for('reserve'))

    # --- Create reservation ---
    code = generate_confirmation()
    reservation = Reservation(
        confirmation_code=code,
        member_id=member.id,
        reservation_date=res_date,
        party_size=party_size
    )
    db.session.add(reservation)
    db.session.commit()

    qr_code = make_qr_base64(code)
    return render_template('confirmation.html', reservation=reservation, qr_code=qr_code)

# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        return render_template('admin/login.html')

    password = request.form.get('password', '')
    if password == ADMIN_PASSWORD:
        session['admin'] = True
        flash('Logged in.', 'success')
        return redirect(url_for('admin_dashboard'))

    flash('Invalid password.', 'danger')
    return redirect(url_for('admin_login'))

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    flash('Logged out.', 'success')
    return redirect(url_for('index'))

# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------
@app.route('/admin')
@admin_required
def admin_dashboard():
    now = today_eastern()
    today_reservations = Reservation.query.filter_by(
        reservation_date=now
    ).order_by(Reservation.created_at).all()
    today_heads = current_heads(now)
    today_capacity = get_capacity(now)
    today_str = now.strftime('%A, %B %d, %Y')
    return render_template('admin/dashboard.html',
                           today_reservations=today_reservations,
                           today_heads=today_heads,
                           today_capacity=today_capacity,
                           today_str=today_str)

# ---------------------------------------------------------------------------
# Toggle arrived
# ---------------------------------------------------------------------------
@app.route('/admin/arrived/<int:res_id>', methods=['POST'])
@admin_required
def toggle_arrived(res_id):
    r = Reservation.query.get_or_404(res_id)
    r.arrived = not r.arrived
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

# ---------------------------------------------------------------------------
# All reservations
# ---------------------------------------------------------------------------
@app.route('/admin/reservations')
@admin_required
def admin_reservations():
    filter_date = request.args.get('date', '')
    if filter_date:
        try:
            fd = date.fromisoformat(filter_date)
            reservations = Reservation.query.filter_by(
                reservation_date=fd
            ).order_by(Reservation.reservation_date).all()
        except ValueError:
            reservations = Reservation.query.order_by(
                Reservation.reservation_date.desc()
            ).all()
            filter_date = ''
    else:
        reservations = Reservation.query.order_by(
            Reservation.reservation_date.desc()
        ).all()
    return render_template('admin/reservations.html',
                           reservations=reservations,
                           filter_date=filter_date)

# ---------------------------------------------------------------------------
# Cancel reservation
# ---------------------------------------------------------------------------
@app.route('/admin/cancel/<int:res_id>', methods=['POST'])
@admin_required
def cancel_reservation(res_id):
    r = Reservation.query.get_or_404(res_id)
    db.session.delete(r)
    db.session.commit()
    flash('Reservation cancelled.', 'success')
    return redirect(url_for('admin_reservations'))

# ---------------------------------------------------------------------------
# Export CSV
# ---------------------------------------------------------------------------
@app.route('/admin/export')
@admin_required
def export_reservations():
    reservations = Reservation.query.order_by(
        Reservation.reservation_date.desc()
    ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Confirmation', 'OwnerNumber', 'LastName', 'FirstName',
                     'Membership', 'Date', 'PartySize', 'Arrived'])
    for r in reservations:
        writer.writerow([
            r.confirmation_code,
            r.member.owner_number,
            r.member.last_name,
            r.member.first_name,
            r.member.membership,
            r.reservation_date.isoformat(),
            r.party_size,
            'Yes' if r.arrived else 'No'
        ])
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=reservations.csv'}
    )

# ---------------------------------------------------------------------------
# Calendar / Day types
# ---------------------------------------------------------------------------
@app.route('/admin/calendar', methods=['GET', 'POST'])
@admin_required
def admin_calendar():
    if request.method == 'POST':
        date_str = request.form.get('date', '')
        day_type_val = request.form.get('day_type', 'Weekday')
        cap_str = request.form.get('capacity_override', '')

        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            flash('Invalid date.', 'danger')
            return redirect(url_for('admin_calendar'))

        cap = int(cap_str) if cap_str else None

        existing = DayType.query.filter_by(date=d).first()
        if existing:
            existing.day_type = day_type_val
            existing.capacity_override = cap
        else:
            dt = DayType(date=d, day_type=day_type_val, capacity_override=cap)
            db.session.add(dt)
        db.session.commit()
        flash(f'Day type set for {d.isoformat()}.', 'success')
        return redirect(url_for('admin_calendar'))

    day_types = DayType.query.order_by(DayType.date).all()
    return render_template('admin/calendar.html', day_types=day_types)

@app.route('/admin/daytype/delete/<int:dt_id>', methods=['POST'])
@admin_required
def delete_day_type(dt_id):
    dt = DayType.query.get_or_404(dt_id)
    db.session.delete(dt)
    db.session.commit()
    flash('Day type removed.', 'success')
    return redirect(url_for('admin_calendar'))

# ---------------------------------------------------------------------------
# Members management
# ---------------------------------------------------------------------------
@app.route('/admin/members')
@admin_required
def admin_members():
    members = Member.query.order_by(Member.last_name).all()
    return render_template('admin/members.html', members=members)

@app.route('/admin/upload', methods=['POST'])
@admin_required
def upload_members():
    file = request.files.get('file')
    if not file:
        flash('No file selected.', 'danger')
        return redirect(url_for('admin_members'))

    try:
        content = file.read().decode('utf-8')
        if '\t' in content.split('\n')[0]:
            delimiter = '\t'
        else:
            delimiter = ','

        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

        count = 0
        for row in reader:
            clean = {}
            for key, val in row.items():
                clean[key.strip().lower().replace(' ', '_')] = val.strip() if val else ''

            owner_number = clean.get('ownernumber', clean.get('owner_number', ''))
            last_name = clean.get('lastname', clean.get('last_name', ''))
            first_name = clean.get('firstname', clean.get('first_name', ''))
            enrollment_type = clean.get('enrollmenttype', clean.get('enrollment_type', ''))
            expiration_date = clean.get('expirationdate', clean.get('expiration_date', ''))
            membership = clean.get('membership', '')

            if not owner_number or not enrollment_type:
                continue

            existing = Member.query.filter_by(owner_number=owner_number).first()
            if existing:
                existing.last_name = last_name
                existing.first_name = first_name
                existing.enrollment_type = enrollment_type
                existing.expiration_date = expiration_date
                existing.membership = enrollment_type
                existing.active = True
            else:
                m = Member(
                    owner_number=owner_number,
                    last_name=last_name,
                    first_name=first_name,
                    enrollment_type=enrollment_type,
                    expiration_date=expiration_date,
                    membership=enrollment_type,
                    active=True
                )
                db.session.add(m)
            count += 1

        db.session.commit()
        flash(f'Loaded {count} members.', 'success')
    except Exception as e:
        flash(f'Error processing file: {str(e)}', 'danger')

    return redirect(url_for('admin_members'))

# ---------------------------------------------------------------------------
# Init DB
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
