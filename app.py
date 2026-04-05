import os
import io
import csv
import pytz
import qrcode
import base64
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, make_response, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')

EASTERN = pytz.timezone('US/Eastern')

if os.environ.get('DATABASE_URL'):
    uri = os.environ['DATABASE_URL']
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = uri
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///splashpass.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

MAX_CAPACITY = 128
MAX_PARTY = 6

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_number = db.Column(db.String(20), unique=True, nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(100), nullable=True)
    enrollment_type = db.Column(db.String(50), nullable=True)
    expiration_date = db.Column(db.String(20), nullable=True)
    membership = db.Column(db.String(20), nullable=False)
    active = db.Column(db.Boolean, default=True)


class DayType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    day_type = db.Column(db.String(20), nullable=False)
    capacity_override = db.Column(db.Integer, nullable=True)


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_number = db.Column(db.String(20), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(100), nullable=True)
    membership = db.Column(db.String(20), nullable=False)
    date = db.Column(db.Date, nullable=False)
    party_size = db.Column(db.Integer, nullable=False)
    confirmation = db.Column(db.String(20), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(EASTERN))
    arrived = db.Column(db.Boolean, default=False)
    cancelled = db.Column(db.Boolean, default=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def get_today():
    return datetime.now(EASTERN).date()


def get_day_type(date):
    dt = DayType.query.filter_by(date=date).first()
    if dt:
        return dt.day_type
    dow = date.weekday()
    if dow < 5:
        return 'Weekday'
    return 'Weekend'


def get_capacity(date):
    dt = DayType.query.filter_by(date=date).first()
    if dt and dt.capacity_override:
        return dt.capacity_override
    return MAX_CAPACITY


def get_booked_count(date):
    result = db.session.query(
        db.func.coalesce(db.func.sum(Reservation.party_size), 0)
    ).filter(
        Reservation.date == date,
        Reservation.cancelled == False
    ).scalar()
    return int(result)


def generate_confirmation():
    import random
    import string
    prefix = 'SP'
    chars = string.ascii_uppercase + string.digits
    code = ''.join(random.choices(chars, k=6))
    return f'{prefix}{code}'


def make_qr_base64(data):
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def get_available_dates(tier):
    today = get_today()
    dates = []

    if tier == 'Platinum':
        for i in range(0, 7):
            d = today + timedelta(days=i)
            day_type = get_day_type(d)
            cap = get_capacity(d)
            booked = get_booked_count(d)
            if booked < cap:
                dates.append({
                    'date': d,
                    'day_type': day_type,
                    'available': cap - booked
                })

    elif tier == 'Gold':
        d = today
        day_type = get_day_type(d)
        if day_type != 'High Use':
            cap = get_capacity(d)
            booked = get_booked_count(d)
            if booked < cap:
                dates.append({
                    'date': d,
                    'day_type': day_type,
                    'available': cap - booked
                })

    elif tier == 'Silver':
        d = today
        day_type = get_day_type(d)
        if day_type == 'Weekday':
            cap = get_capacity(d)
            booked = get_booked_count(d)
            if booked < cap:
                dates.append({
                    'date': d,
                    'day_type': day_type,
                    'available': cap - booked
                })

    return dates


# ---------------------------------------------------------------------------
# Database init
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    if not Admin.query.filter_by(username='admin').first():
        a = Admin(username='admin')
        a.set_password('admin123')
        db.session.add(a)
        db.session.commit()


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/verify', methods=['POST'])
def verify():
    owner_number = request.form.get('owner_number', '').strip()
    last_name = request.form.get('last_name', '').strip()

    if not owner_number or not last_name:
        flash('Please enter both Owner Number and Last Name.', 'danger')
        return redirect(url_for('index'))

    member = Member.query.filter_by(
        owner_number=owner_number,
        active=True
    ).first()

    if not member or member.last_name.lower() != last_name.lower():
        flash('Member not found. Check your Owner Number and Last Name.', 'danger')
        return redirect(url_for('index'))

    tier = member.membership.strip().capitalize() if member.membership else ''

    if tier not in ('Platinum', 'Gold', 'Silver'):
        flash(f'Unknown membership tier: [{tier}]. EnrollmentType=[{member.enrollment_type}]. Please contact admin.', 'danger')
        return redirect(url_for('index'))

    session['member_id'] = member.id
    session['owner_number'] = member.owner_number
    session['last_name'] = member.last_name
    session['first_name'] = member.first_name or ''
    session['tier'] = tier

    return redirect(url_for('book'))


@app.route('/book')
def book():
    if not session.get('member_id'):
        return redirect(url_for('index'))

    tier = session.get('tier')
    dates = get_available_dates(tier)

    return render_template('book.html',
                           tier=tier,
                           dates=dates,
                           max_party=MAX_PARTY,
                           owner_number=session.get('owner_number'),
                           first_name=session.get('first_name'),
                           last_name=session.get('last_name'))


@app.route('/reserve', methods=['POST'])
def reserve():
    if not session.get('member_id'):
        return redirect(url_for('index'))

    date_str = request.form.get('date')
    party_size = request.form.get('party_size', '1')

    try:
        res_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        flash('Invalid date.', 'danger')
        return redirect(url_for('book'))

    try:
        party_size = int(party_size)
    except ValueError:
        flash('Invalid party size.', 'danger')
        return redirect(url_for('book'))

    if party_size < 1 or party_size > MAX_PARTY:
        flash(f'Party size must be 1–{MAX_PARTY}.', 'danger')
        return redirect(url_for('book'))

    tier = session.get('tier')
    today = get_today()

    # Validate date for tier
    if tier == 'Platinum':
        if res_date < today or res_date > today + timedelta(days=6):
            flash('Platinum members can book up to 6 days in advance.', 'danger')
            return redirect(url_for('book'))
    elif tier == 'Gold':
        if res_date != today:
            flash('Gold members can only book same-day.', 'danger')
            return redirect(url_for('book'))
        if get_day_type(res_date) == 'High Use':
            flash('Gold members cannot book on High Use days.', 'danger')
            return redirect(url_for('book'))
    elif tier == 'Silver':
        if res_date != today:
            flash('Silver members can only book same-day.', 'danger')
            return redirect(url_for('book'))
        if get_day_type(res_date) != 'Weekday':
            flash('Silver members can only book on Weekdays.', 'danger')
            return redirect(url_for('book'))

    # Check capacity
    cap = get_capacity(res_date)
    booked = get_booked_count(res_date)
    if booked + party_size > cap:
        flash(f'Not enough capacity. {cap - booked} spots remaining.', 'danger')
        return redirect(url_for('book'))

    # Check duplicate
    existing = Reservation.query.filter_by(
        owner_number=session['owner_number'],
        date=res_date,
        cancelled=False
    ).first()
    if existing:
        flash('You already have a reservation for this date.', 'warning')
        return redirect(url_for('book'))

    confirmation = generate_confirmation()
    while Reservation.query.filter_by(confirmation=confirmation).first():
        confirmation = generate_confirmation()

    res = Reservation(
        owner_number=session['owner_number'],
        last_name=session['last_name'],
        first_name=session.get('first_name', ''),
        membership=tier,
        date=res_date,
        party_size=party_size,
        confirmation=confirmation
    )
    db.session.add(res)
    db.session.commit()

    return redirect(url_for('confirmation', conf=confirmation))


@app.route('/confirmation/<conf>')
def confirmation(conf):
    res = Reservation.query.filter_by(confirmation=conf).first()
    if not res:
        flash('Reservation not found.', 'danger')
        return redirect(url_for('index'))

    qr_data = f'SPLASHPASS|{res.confirmation}|{res.owner_number}|{res.date}|{res.party_size}'
    qr_b64 = make_qr_base64(qr_data)

    return render_template('confirmation.html', res=res, qr_b64=qr_b64)


@app.route('/lookup', methods=['GET', 'POST'])
def lookup():
    if request.method == 'POST':
        conf = request.form.get('confirmation', '').strip().upper()
        res = Reservation.query.filter_by(confirmation=conf, cancelled=False).first()
        if not res:
            flash('Reservation not found.', 'danger')
            return render_template('lookup.html')
        return redirect(url_for('confirmation', conf=res.confirmation))
    return render_template('lookup.html')


@app.route('/logout')
def logout():
    session.pop('member_id', None)
    session.pop('owner_number', None)
    session.pop('last_name', None)
    session.pop('first_name', None)
    session.pop('tier', None)
    flash('Logged out.', 'info')
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        admin = Admin.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            session['admin'] = True
            session['admin_user'] = username
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials.', 'danger')
    return render_template('admin_login.html')


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    today = get_today()
    view_date_str = request.args.get('date')
    if view_date_str:
        try:
            view_date = datetime.strptime(view_date_str, '%Y-%m-%d').date()
        except ValueError:
            view_date = today
    else:
        view_date = today

    day_type = get_day_type(view_date)
    cap = get_capacity(view_date)
    booked = get_booked_count(view_date)

    reservations = Reservation.query.filter_by(
        date=view_date, cancelled=False
    ).order_by(Reservation.created_at).all()

    return render_template('admin_dashboard.html',
                           today=today,
                           view_date=view_date,
                           day_type=day_type,
                           capacity=cap,
                           booked=booked,
                           reservations=reservations)


@app.route('/admin/toggle_arrived/<int:res_id>', methods=['POST'])
@admin_required
def toggle_arrived(res_id):
    res = Reservation.query.get_or_404(res_id)
    res.arrived = not res.arrived
    db.session.commit()
    return redirect(request.referrer or url_for('admin_dashboard'))


@app.route('/admin/cancel/<int:res_id>', methods=['POST'])
@admin_required
def cancel_reservation(res_id):
    res = Reservation.query.get_or_404(res_id)
    res.cancelled = True
    db.session.commit()
    flash(f'Reservation {res.confirmation} cancelled.', 'success')
    return redirect(request.referrer or url_for('admin_dashboard'))


@app.route('/admin/reservations')
@admin_required
def admin_reservations():
    date_str = request.args.get('date')
    query = Reservation.query.filter_by(cancelled=False)
    if date_str:
        try:
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            query = query.filter_by(date=filter_date)
        except ValueError:
            pass
    reservations = query.order_by(Reservation.date.desc(), Reservation.created_at).all()
    return render_template('admin_reservations.html', reservations=reservations)


@app.route('/admin/export')
@admin_required
def export_reservations():
    date_str = request.args.get('date')
    query = Reservation.query.filter_by(cancelled=False)
    if date_str:
        try:
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            query = query.filter_by(date=filter_date)
        except ValueError:
            pass
    reservations = query.order_by(Reservation.date, Reservation.created_at).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Confirmation', 'Date', 'OwnerNumber', 'LastName', 'FirstName',
        'Membership', 'PartySize', 'Arrived', 'CreatedAt'
    ])
    for r in reservations:
        writer.writerow([
            r.confirmation, r.date, r.owner_number, r.last_name,
            r.first_name, r.membership, r.party_size, r.arrived, r.created_at
        ])

    resp = make_response(output.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    fname = f'reservations_{date_str or "all"}.csv'
    resp.headers['Content-Disposition'] = f'attachment; filename={fname}'
    return resp


# ---------------------------------------------------------------------------
# Admin calendar
# ---------------------------------------------------------------------------
@app.route('/admin/calendar')
@admin_required
def admin_calendar():
    year = int(request.args.get('year', get_today().year))
    month = int(request.args.get('month', get_today().month))

    import calendar
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(year, month)

    day_types = {}
    overrides = DayType.query.filter(
        db.extract('year', DayType.date) == year,
        db.extract('month', DayType.date) == month
    ).all()
    for dt in overrides:
        day_types[dt.date] = dt

    month_name = calendar.month_name[month]

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return render_template('admin_calendar.html',
                           year=year, month=month, month_name=month_name,
                           weeks=weeks, day_types=day_types,
                           today=get_today(),
                           prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month)


@app.route('/admin/set_day_type', methods=['POST'])
@admin_required
def set_day_type():
    date_str = request.form.get('date')
    day_type = request.form.get('day_type')
    capacity = request.form.get('capacity')

    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        flash('Invalid date.', 'danger')
        return redirect(url_for('admin_calendar'))

    if day_type not in ('Weekday', 'Weekend', 'High Use'):
        flash('Invalid day type.', 'danger')
        return redirect(url_for('admin_calendar'))

    cap_val = None
    if capacity:
        try:
            cap_val = int(capacity)
        except ValueError:
            pass

    existing = DayType.query.filter_by(date=d).first()
    if existing:
        existing.day_type = day_type
        existing.capacity_override = cap_val
    else:
        dt = DayType(date=d, day_type=day_type, capacity_override=cap_val)
        db.session.add(dt)

    db.session.commit()
    flash(f'{d} set to {day_type}.', 'success')
    return redirect(url_for('admin_calendar', year=d.year, month=d.month))


@app.route('/admin/remove_day_type', methods=['POST'])
@admin_required
def remove_day_type():
    date_str = request.form.get('date')
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        flash('Invalid date.', 'danger')
        return redirect(url_for('admin_calendar'))

    existing = DayType.query.filter_by(date=d).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        flash(f'Override removed for {d}.', 'success')

    return redirect(url_for('admin_calendar', year=d.year, month=d.month))


# ---------------------------------------------------------------------------
# Admin member management
# ---------------------------------------------------------------------------
@app.route('/admin/members')
@admin_required
def admin_members():
    search = request.args.get('search', '').strip()
    if search:
        members = Member.query.filter(
            db.or_(
                Member.owner_number.ilike(f'%{search}%'),
                Member.last_name.ilike(f'%{search}%'),
                Member.first_name.ilike(f'%{search}%')
            )
        ).order_by(Member.last_name).all()
    else:
        members = Member.query.order_by(Member.last_name).limit(100).all()

    total = Member.query.count()
    return render_template('admin_members.html',
                           members=members, total=total, search=search)


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
        flash(f'CSV headers: {reader.fieldnames}', 'info')

        count = 0
        skipped = 0
        for row in reader:
            clean = {}
            for key, val in row.items():
                if key is None:
                    continue
                clean_key = key.strip().lower().replace(' ', '_').replace('\ufeff', '')
                clean[clean_key] = val.strip() if val else ''

            if count == 0 and skipped == 0:
                flash(f'First row keys: {list(clean.keys())}', 'info')
                flash(f'First row vals: {list(clean.values())}', 'info')

            owner_number = (
                clean.get('ownernumber') or
                clean.get('owner_number') or ''
            )
            last_name = (
                clean.get('lastname') or
                clean.get('last_name') or ''
            )
            first_name = (
                clean.get('firstname') or
                clean.get('first_name') or ''
            )
            enrollment_type = (
                clean.get('enrollmenttype') or
                clean.get('enrollment_type') or ''
            )
            expiration_date = (
                clean.get('expirationdate') or
                clean.get('expiration_date') or ''
            )
            membership = (
                clean.get('membership') or ''
            )

            if not owner_number:
                skipped += 1
                continue

            # Determine tier: prefer Membership column, fall back to EnrollmentType
            tier = membership if membership else enrollment_type
            tier = tier.strip()

            # Validate tier
            valid_tiers = ('platinum', 'gold', 'silver')
            if tier.lower() not in valid_tiers:
                if count < 3:
                    flash(f'Row {count+1}: unexpected tier [{tier}] for owner {owner_number}', 'warning')
                skipped += 1
                continue

            tier = tier.strip().capitalize()

            existing = Member.query.filter_by(owner_number=owner_number).first()
            if existing:
                existing.last_name = last_name
                existing.first_name = first_name
                existing.enrollment_type = enrollment_type
                existing.expiration_date = expiration_date
                existing.membership = tier
                existing.active = True
            else:
                m = Member(
                    owner_number=owner_number,
                    last_name=last_name,
                    first_name=first_name,
                    enrollment_type=enrollment_type,
                    expiration_date=expiration_date,
                    membership=tier,
                    active=True
                )
                db.session.add(m)
            count += 1

        db.session.commit()
        flash(f'Loaded {count} members. Skipped {skipped} rows.', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')

    return redirect(url_for('admin_members'))


@app.route('/admin/toggle_member/<int:member_id>', methods=['POST'])
@admin_required
def toggle_member(member_id):
    m = Member.query.get_or_404(member_id)
    m.active = not m.active
    db.session.commit()
    status = 'activated' if m.active else 'deactivated'
    flash(f'{m.owner_number} {m.last_name} {status}.', 'success')
    return redirect(request.referrer or url_for('admin_members'))


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    session.pop('admin_user', None)
    flash('Admin logged out.', 'info')
    return redirect(url_for('admin_login'))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
