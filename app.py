import os, io, csv, base64, string, random, calendar as cal_module
from datetime import datetime, timedelta, date
from functools import wraps
from collections import OrderedDict

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, Response
)
from flask_sqlalchemy import SQLAlchemy
import pytz
import qrcode

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
app = Flask(__name__)

database_url = os.environ.get('DATABASE_URL', 'sqlite:///splashpass.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')
CHECKIN_PASSWORD = os.environ.get('CHECKIN_PASSWORD', 'checkin')
DEFAULT_CAPACITY = 128
EASTERN = pytz.timezone('US/Eastern')

db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_number = db.Column(db.String(20), unique=True, nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    membership = db.Column(db.String(20), nullable=False)
    active = db.Column(db.Boolean, default=True)
    reservations = db.relationship('Reservation', backref='member', lazy=True)

class DayType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    day_type = db.Column(db.String(20), nullable=False, default='Weekday')
    capacity_override = db.Column(db.Integer, nullable=True)

class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    confirmation_code = db.Column(db.String(8), unique=True, nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    reservation_date = db.Column(db.Date, nullable=False)
    party_size = db.Column(db.Integer, nullable=False)
    arrived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(EASTERN))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_eastern():
    return datetime.now(EASTERN)

def today_eastern():
    return now_eastern().date()

def generate_code():
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choices(chars, k=8))
        if not Reservation.query.filter_by(confirmation_code=code).first():
            return code

def get_day_info(d):
    dt = DayType.query.filter_by(date=d).first()
    if dt:
        return dt.day_type, dt.capacity_override or DEFAULT_CAPACITY
    return 'Weekday', DEFAULT_CAPACITY

def get_capacity_used(d):
    result = db.session.query(db.func.coalesce(db.func.sum(Reservation.party_size), 0))\
        .filter_by(reservation_date=d).scalar()
    return result

def make_qr_base64(data):
    qr = qrcode.make(data)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8')

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def checkin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in') and not session.get('checkin_logged_in'):
            return redirect(url_for('checkin_login'))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/book', methods=['GET', 'POST'])
def book():
    if request.method == 'GET':
        return render_template('book.html')

    owner_number = request.form.get('owner_number', '').strip()
    if not owner_number:
        flash('Please enter your Owner Number.', 'danger')
        return render_template('book.html')

    member = Member.query.filter_by(owner_number=owner_number, active=True).first()
    if not member:
        flash('Owner Number not found or membership inactive.', 'danger')
        return render_template('book.html')

    today = today_eastern()
    tier = member.membership

    if tier == 'Platinum':
        max_date = today + timedelta(days=6)
    elif tier == 'Gold':
        max_date = today
    elif tier == 'Silver':
        max_date = today
    else:
        flash('Unknown membership tier.', 'danger')
        return render_template('book.html')

    available_dates = []
    for i in range((max_date - today).days + 1):
        d = today + timedelta(days=i)
        day_type, capacity = get_day_info(d)

        if tier == 'Silver' and day_type != 'Weekday':
            continue
        if tier == 'Gold' and day_type == 'High Use':
            continue

        used = get_capacity_used(d)
        remaining = capacity - used
        if remaining > 0:
            available_dates.append({
                'date': d,
                'day_type': day_type,
                'remaining': remaining
            })

    return render_template('book.html', member=member, available_dates=available_dates)

@app.route('/reserve', methods=['POST'])
def reserve():
    owner_number = request.form.get('owner_number', '').strip()
    reservation_date_str = request.form.get('reservation_date', '').strip()
    party_size_str = request.form.get('party_size', '').strip()

    if not all([owner_number, reservation_date_str, party_size_str]):
        flash('All fields are required.', 'danger')
        return redirect(url_for('book'))

    member = Member.query.filter_by(owner_number=owner_number, active=True).first()
    if not member:
        flash('Member not found.', 'danger')
        return redirect(url_for('book'))

    try:
        res_date = datetime.strptime(reservation_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date.', 'danger')
        return redirect(url_for('book'))

    try:
        party_size = int(party_size_str)
        if party_size < 1 or party_size > 6:
            raise ValueError
    except ValueError:
        flash('Party size must be between 1 and 6.', 'danger')
        return redirect(url_for('book'))

    today = today_eastern()
    tier = member.membership

    if tier == 'Platinum':
        if res_date < today or res_date > today + timedelta(days=6):
            flash('Date outside your booking window.', 'danger')
            return redirect(url_for('book'))
    elif tier == 'Gold':
        if res_date != today:
            flash('Gold members can only book same-day.', 'danger')
            return redirect(url_for('book'))
    elif tier == 'Silver':
        if res_date != today:
            flash('Silver members can only book same-day.', 'danger')
            return redirect(url_for('book'))
    else:
        flash('Unknown membership tier.', 'danger')
        return redirect(url_for('book'))

    day_type, capacity = get_day_info(res_date)

    if tier == 'Silver' and day_type != 'Weekday':
        flash('Silver members can only book Weekday dates.', 'danger')
        return redirect(url_for('book'))
    if tier == 'Gold' and day_type == 'High Use':
        flash('Gold members cannot book High Use dates.', 'danger')
        return redirect(url_for('book'))

    used = get_capacity_used(res_date)
    if used + party_size > capacity:
        flash('Sorry, not enough availability for that date and party size.', 'danger')
        return redirect(url_for('book'))

    existing = Reservation.query.filter_by(member_id=member.id, reservation_date=res_date).first()
    if existing:
        flash('You already have a reservation for this date.', 'warning')
        return redirect(url_for('book'))

    code = generate_code()
    reservation = Reservation(
        confirmation_code=code,
        member_id=member.id,
        reservation_date=res_date,
        party_size=party_size
    )
    db.session.add(reservation)
    db.session.commit()

    qr_data = make_qr_base64(code)

    return render_template('confirmation.html',
                           reservation=reservation,
                           member=member,
                           qr_data=qr_data)

@app.route('/lookup', methods=['GET', 'POST'])
def lookup():
    if request.method == 'GET':
        return render_template('lookup.html')

    owner_number = request.form.get('owner_number', '').strip()
    if not owner_number:
        flash('Please enter your Owner Number.', 'danger')
        return render_template('lookup.html')

    member = Member.query.filter_by(owner_number=owner_number, active=True).first()
    if not member:
        flash('Owner Number not found.', 'danger')
        return render_template('lookup.html')

    today = today_eastern()
    reservations = Reservation.query.filter(
        Reservation.member_id == member.id,
        Reservation.reservation_date >= today
    ).order_by(Reservation.reservation_date).all()

    return render_template('lookup.html', member=member, reservations=reservations)

# ---------------------------------------------------------------------------
# Check-in routes
# ---------------------------------------------------------------------------
@app.route('/checkin/login', methods=['GET', 'POST'])
def checkin_login():
    if request.method == 'GET':
        return render_template('checkin/login.html')

    password = request.form.get('password', '')
    if password == CHECKIN_PASSWORD:
        session['checkin_logged_in'] = True
        return redirect(url_for('checkin_dashboard'))

    flash('Invalid password.', 'danger')
    return render_template('checkin/login.html')

@app.route('/checkin/logout')
def checkin_logout():
    session.pop('checkin_logged_in', None)
    return redirect(url_for('checkin_login'))

@app.route('/checkin')
@app.route('/checkin/dashboard')
@checkin_required
def checkin_dashboard():
    today = today_eastern()
    reservations = Reservation.query.filter_by(reservation_date=today)\
        .order_by(Reservation.created_at).all()

    day_type, capacity = get_day_info(today)
    used = get_capacity_used(today)
    arrived_count = sum(1 for r in reservations if r.arrived)
    arrived_guests = sum(r.party_size for r in reservations if r.arrived)

    return render_template('checkin/dashboard.html',
                           reservations=reservations,
                           today=today,
                           day_type=day_type,
                           capacity=capacity,
                           used=used,
                           arrived_count=arrived_count,
                           arrived_guests=arrived_guests)

@app.route('/checkin/search', methods=['POST'])
@checkin_required
def checkin_search():
    query = request.form.get('query', '').strip().upper()
    today = today_eastern()

    if not query:
        flash('Please enter a confirmation code or owner number.', 'danger')
        return redirect(url_for('checkin_dashboard'))

    reservation = Reservation.query.filter_by(
        confirmation_code=query, reservation_date=today).first()

    if reservation:
        return render_template('checkin/result.html',
                               reservations=[reservation], query=query, today=today)

    member = Member.query.filter_by(owner_number=query).first()
    if member:
        reservations = Reservation.query.filter_by(
            member_id=member.id, reservation_date=today).all()
        if reservations:
            return render_template('checkin/result.html',
                                   reservations=reservations, query=query, today=today)

    flash(f'No reservation found for today matching "{query}".', 'warning')
    return redirect(url_for('checkin_dashboard'))

@app.route('/checkin/toggle/<int:res_id>', methods=['POST'])
@checkin_required
def checkin_toggle(res_id):
    reservation = Reservation.query.get_or_404(res_id)
    reservation.arrived = not reservation.arrived
    db.session.commit()

    source = request.form.get('source', 'dashboard')
    if source == 'search':
        flash(f'{"Checked in" if reservation.arrived else "Check-in removed"}: {reservation.confirmation_code}', 'success')
    return redirect(url_for('checkin_dashboard'))

# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        return render_template('admin/login.html')

    password = request.form.get('password', '')
    if password == ADMIN_PASSWORD:
        session['admin_logged_in'] = True
        return redirect(url_for('admin_dashboard'))

    flash('Invalid password.', 'danger')
    return render_template('admin/login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    date_str = request.args.get('date', '')
    if date_str:
        try:
            view_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            view_date = today_eastern()
    else:
        view_date = today_eastern()

    reservations = Reservation.query.filter_by(reservation_date=view_date)\
        .order_by(Reservation.created_at).all()

    day_type, capacity = get_day_info(view_date)
    used = get_capacity_used(view_date)

    return render_template('admin/dashboard.html',
                           reservations=reservations,
                           view_date=view_date,
                           day_type=day_type,
                           capacity=capacity,
                           used=used)

@app.route('/admin/toggle-arrival/<int:res_id>', methods=['POST'])
@admin_required
def toggle_arrival(res_id):
    reservation = Reservation.query.get_or_404(res_id)
    reservation.arrived = not reservation.arrived
    db.session.commit()
    return redirect(url_for('admin_dashboard', date=reservation.reservation_date.isoformat()))

@app.route('/admin/export')
@admin_required
def admin_export():
    date_str = request.args.get('date', '')
    if date_str:
        try:
            export_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            export_date = today_eastern()
    else:
        export_date = today_eastern()

    reservations = Reservation.query.filter_by(reservation_date=export_date)\
        .order_by(Reservation.created_at).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Confirmation', 'Owner Number', 'Last Name', 'First Name',
                     'Membership', 'Party Size', 'Arrived'])

    for r in reservations:
        writer.writerow([
            r.confirmation_code,
            r.member.owner_number,
            r.member.last_name,
            r.member.first_name,
            r.member.membership,
            r.party_size,
            'Yes' if r.arrived else 'No'
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=reservations_{export_date}.csv'}
    )

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
        raw = file.read()
        if raw[:3] == b'\xef\xbb\xbf':
            raw = raw[3:]
        content = raw.decode('utf-8')

        first_line = content.split('\n')[0]
        delimiter = '\t' if '\t' in first_line else ','

        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

        raw_headers = reader.fieldnames or []
        header_map = {}
        for h in raw_headers:
            if h is None:
                continue
            normalized = h.strip().lower().replace(' ', '_').replace('-', '_')
            header_map[normalized] = h

        def find_col(candidates):
            for c in candidates:
                if c in header_map:
                    return header_map[c]
            return None

        owner_col = find_col([
            'owner_number', 'ownernumber', 'owner_no', 'owner_#',
            'owner#', 'owner', 'owner_num', 'ownernum'
        ])
        first_col = find_col([
            'first_name', 'firstname', 'first'
        ])
        last_col = find_col([
            'last_name', 'lastname', 'last'
        ])
        mem_col = find_col([
            'membership', 'membership_level', 'membershiplevel',
            'membership_type', 'membershiptype', 'level', 'tier',
            'type', 'member_level', 'member_type', 'pass_type',
            'passtype', 'pass', 'pass_level', 'passlevel'
        ])

        if not owner_col:
            flash(f'Could not find owner number column. Headers found: {raw_headers}', 'danger')
            return redirect(url_for('admin_members'))
        if not first_col or not last_col:
            flash(f'Could not find name columns. Headers found: {raw_headers}', 'danger')
            return redirect(url_for('admin_members'))

        count = 0
        skipped = 0
        no_tier = 0

        for row in reader:
            owner_number = (row.get(owner_col) or '').strip()
            first_name = (row.get(first_col) or '').strip()
            last_name = (row.get(last_col) or '').strip()

            if not owner_number or not first_name:
                skipped += 1
                continue

            raw_mem = (row.get(mem_col) or '').strip() if mem_col else ''
            membership = None
            raw_lower = raw_mem.lower()

            if 'plat' in raw_lower:
                membership = 'Platinum'
            elif 'gold' in raw_lower:
                membership = 'Gold'
            elif 'silv' in raw_lower:
                membership = 'Silver'
            elif raw_mem.title() in ('Platinum', 'Gold', 'Silver'):
                membership = raw_mem.title()

            if not membership:
                membership = 'Silver'
                no_tier += 1

            existing = Member.query.filter_by(owner_number=owner_number).first()
            if existing:
                existing.last_name = last_name
                existing.first_name = first_name
                existing.membership = membership
                existing.active = True
            else:
                m = Member(
                    owner_number=owner_number,
                    last_name=last_name,
                    first_name=first_name,
                    membership=membership,
                    active=True
                )
                db.session.add(m)
            count += 1

        db.session.commit()

        msg = f'Loaded {count} members.'
        if skipped:
            msg += f' Skipped {skipped} rows (missing data).'
        if mem_col:
            msg += f' Membership column: "{mem_col}".'
        else:
            msg += ' ⚠️ No membership column found — all set to Silver.'
        if no_tier and mem_col:
            msg += f' {no_tier} rows had unrecognized tier (defaulted to Silver).'

        flash(msg, 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {str(e)}', 'danger')

    return redirect(url_for('admin_members'))

# ---------------------------------------------------------------------------
# Admin calendar
# ---------------------------------------------------------------------------
@app.route('/admin/calendar')
@admin_required
def admin_calendar():
    today = today_eastern()
    year = request.args.get('year', today.year, type=int)
    month = request.args.get('month', today.month, type=int)

    if month < 1:
        month = 12
        year -= 1
    elif month > 12:
        month = 1
        year += 1

    cal = cal_module.Calendar(firstweekday=6)  # Sunday start
    month_dates = cal.monthdatescalendar(year, month)

    weeks = []
    for week in month_dates:
        week_data = []
        for d in week:
            if d.month == month:
                day_type, capacity = get_day_info(d)
                used = get_capacity_used(d)
                week_data.append({
                    'date': d,
                    'day_type': day_type,
                    'capacity': capacity,
                    'used': used,
                    'is_past': d < today,
                    'is_today': d == today
                })
            else:
                week_data.append(None)
        weeks.append(week_data)

    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    # Check if last year same month has any DayType data
    last_year_count = DayType.query.filter(
        db.extract('year', DayType.date) == year - 1,
        db.extract('month', DayType.date) == month
    ).count()

    # Also check previous month (for the second copy option)
    if month == 1:
        pm_month, pm_year = 12, year - 1
    else:
        pm_month, pm_year = month - 1, year
    prev_month_count = DayType.query.filter(
        db.extract('year', DayType.date) == pm_year,
        db.extract('month', DayType.date) == pm_month
    ).count()

    return render_template('admin/calendar.html',
                           weeks=weeks,
                           month=month,
                           year=year,
                           today=today,
                           month_name=date(year, month, 1).strftime('%B %Y'),
                           prev_year=prev_year,
                           prev_month=prev_month,
                           next_year=next_year,
                           next_month=next_month,
                           last_year_has_data=last_year_count > 0,
                           prev_month_has_data=prev_month_count > 0,
                           pm_month=pm_month,
                           pm_year=pm_year)

@app.route('/admin/calendar/copy-previous-year', methods=['POST'])
@admin_required
def admin_calendar_copy_previous_year():
    """Copy day types from the SAME MONTH in the PREVIOUS YEAR.
    Matches by week-of-month position + day-of-week so that e.g.
    the 2nd-week Friday/Saturday/Sunday keep their types even though
    the actual dates shift year to year."""
    year = request.form.get('year', type=int)
    month = request.form.get('month', type=int)

    if not year or not month:
        flash('Invalid month/year.', 'danger')
        return redirect(url_for('admin_calendar'))

    source_year = year - 1

    cal = cal_module.Calendar(firstweekday=6)  # Sunday-start

    # Build source map: (week_index, weekday) -> day_type
    source_weeks = cal.monthdatescalendar(source_year, month)
    source_map = {}  # (week_idx, weekday) -> day_type
    for wi, week in enumerate(source_weeks):
        for d in week:
            if d.month == month:
                dt = DayType.query.filter_by(date=d).first()
                if dt:
                    source_map[(wi, d.weekday())] = dt.day_type

    if not source_map:
        flash(f'No calendar data found for {date(source_year, month, 1).strftime("%B %Y")}.', 'warning')
        return redirect(url_for('admin_calendar', year=year, month=month))

    # Apply to target year at matching positions
    target_weeks = cal.monthdatescalendar(year, month)
    applied = 0
    for wi, week in enumerate(target_weeks):
        for d in week:
            if d.month == month:
                key = (wi, d.weekday())
                if key in source_map:
                    day_type = source_map[key]
                    existing = DayType.query.filter_by(date=d).first()
                    if existing:
                        existing.day_type = day_type
                    else:
                        db.session.add(DayType(date=d, day_type=day_type))
                    applied += 1

    db.session.commit()
    flash(f'Copied {applied} day types from {date(source_year, month, 1).strftime("%B %Y")} → '
          f'{date(year, month, 1).strftime("%B %Y")} (matched by week position & day of week).', 'success')
    return redirect(url_for('admin_calendar', year=year, month=month))

@app.route('/admin/calendar/copy-previous-month', methods=['POST'])
@admin_required
def admin_calendar_copy_previous_month():
    """Copy day types from the PREVIOUS MONTH (same or prior year).
    Matches by week-of-month position + day-of-week."""
    year = request.form.get('year', type=int)
    month = request.form.get('month', type=int)

    if not year or not month:
        flash('Invalid month/year.', 'danger')
        return redirect(url_for('admin_calendar'))

    if month == 1:
        src_month, src_year = 12, year - 1
    else:
        src_month, src_year = month - 1, year

    cal = cal_module.Calendar(firstweekday=6)

    source_weeks = cal.monthdatescalendar(src_year, src_month)
    source_map = {}
    for wi, week in enumerate(source_weeks):
        for d in week:
            if d.month == src_month:
                dt = DayType.query.filter_by(date=d).first()
                if dt:
                    source_map[(wi, d.weekday())] = dt.day_type

    if not source_map:
        flash(f'No calendar data found for {date(src_year, src_month, 1).strftime("%B %Y")}.', 'warning')
        return redirect(url_for('admin_calendar', year=year, month=month))

    target_weeks = cal.monthdatescalendar(year, month)
    applied = 0
    for wi, week in enumerate(target_weeks):
        for d in week:
            if d.month == month:
                key = (wi, d.weekday())
                if key in source_map:
                    day_type = source_map[key]
                    existing = DayType.query.filter_by(date=d).first()
                    if existing:
                        existing.day_type = day_type
                    else:
                        db.session.add(DayType(date=d, day_type=day_type))
                    applied += 1

    db.session.commit()
    flash(f'Copied {applied} day types from {date(src_year, src_month, 1).strftime("%B %Y")} → '
          f'{date(year, month, 1).strftime("%B %Y")} (matched by week position & day of week).', 'success')
    return redirect(url_for('admin_calendar', year=year, month=month))

@app.route('/admin/calendar/bulk', methods=['POST'])
@admin_required
def admin_calendar_bulk():
    month = request.form.get('month', type=int)
    year = request.form.get('year', type=int)

    updated = 0
    for key in request.form:
        if key.startswith('type_'):
            date_str = key.replace('type_', '')
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                continue

            day_type = request.form.get(f'type_{date_str}', 'Weekday')
            if day_type not in ('Weekday', 'Weekend', 'High Use'):
                continue

            cap_str = request.form.get(f'cap_{date_str}', '')
            try:
                capacity = int(cap_str) if cap_str else DEFAULT_CAPACITY
                if capacity < 1:
                    capacity = DEFAULT_CAPACITY
            except ValueError:
                capacity = DEFAULT_CAPACITY

            existing = DayType.query.filter_by(date=d).first()
            if existing:
                existing.day_type = day_type
                existing.capacity_override = capacity if capacity != DEFAULT_CAPACITY else None
            else:
                dt_rec = DayType(date=d, day_type=day_type,
                                 capacity_override=capacity if capacity != DEFAULT_CAPACITY else None)
                db.session.add(dt_rec)
            updated += 1

    db.session.commit()
    flash(f'Saved {updated} days for {date(year, month, 1).strftime("%B %Y")}.', 'success')
    return redirect(url_for('admin_calendar', year=year, month=month))

@app.route('/admin/set-day', methods=['POST'])
@admin_required
def set_day():
    date_str = request.form.get('date', '')
    day_type = request.form.get('day_type', 'Weekday')
    capacity_str = request.form.get('capacity', '')

    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date.', 'danger')
        return redirect(url_for('admin_calendar'))

    if day_type not in ('Weekday', 'Weekend', 'High Use'):
        flash('Invalid day type.', 'danger')
        return redirect(url_for('admin_calendar'))

    capacity = None
    if capacity_str:
        try:
            capacity = int(capacity_str)
            if capacity < 1:
                raise ValueError
        except ValueError:
            flash('Invalid capacity.', 'danger')
            return redirect(url_for('admin_calendar'))

    existing = DayType.query.filter_by(date=d).first()
    if existing:
        existing.day_type = day_type
        existing.capacity_override = capacity
    else:
        dt_rec = DayType(date=d, day_type=day_type, capacity_override=capacity)
        db.session.add(dt_rec)

    db.session.commit()
    flash(f'{d} set to {day_type}.', 'success')
    return redirect(url_for('admin_calendar', year=d.year, month=d.month))

@app.route('/admin/delete-reservation/<int:res_id>', methods=['POST'])
@admin_required
def delete_reservation(res_id):
    reservation = Reservation.query.get_or_404(res_id)
    res_date = reservation.reservation_date
    db.session.delete(reservation)
    db.session.commit()
    flash('Reservation deleted.', 'success')
    return redirect(url_for('admin_dashboard', date=res_date.isoformat()))

# ---------------------------------------------------------------------------
# Admin usage report
# ---------------------------------------------------------------------------
@app.route('/admin/report')
@admin_required
def admin_report():
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')

    report_data = None
    start_date = None
    end_date = None
    totals = None

    if start_str and end_str:
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid date format.', 'danger')
            return render_template('admin/report.html')

        if end_date < start_date:
            flash('End date must be on or after start date.', 'danger')
            return render_template('admin/report.html')

        if (end_date - start_date).days > 366:
            flash('Date range cannot exceed 366 days.', 'danger')
            return render_template('admin/report.html')

        report_data = []
        total_reservations = 0
        total_arrived = 0
        total_headcount = 0
        total_arrived_headcount = 0

        d = start_date
        while d <= end_date:
            day_type, capacity = get_day_info(d)

            day_reservations = Reservation.query.filter_by(reservation_date=d).all()
            res_count = len(day_reservations)
            arrived_count = sum(1 for r in day_reservations if r.arrived)
            headcount = sum(r.party_size for r in day_reservations)
            arrived_headcount = sum(r.party_size for r in day_reservations if r.arrived)

            report_data.append({
                'date': d,
                'day_name': d.strftime('%A'),
                'day_type': day_type,
                'capacity': capacity,
                'reservations': res_count,
                'arrived': arrived_count,
                'headcount': headcount,
                'arrived_headcount': arrived_headcount,
                'utilization': round((headcount / capacity) * 100, 1) if capacity > 0 else 0
            })

            total_reservations += res_count
            total_arrived += arrived_count
            total_headcount += headcount
            total_arrived_headcount += arrived_headcount
            d += timedelta(days=1)

        totals = {
            'reservations': total_reservations,
            'arrived': total_arrived,
            'headcount': total_headcount,
            'arrived_headcount': total_arrived_headcount
        }

    return render_template('admin/report.html',
                           report_data=report_data,
                           start_date=start_date,
                           end_date=end_date,
                           totals=totals)

@app.route('/admin/report/export')
@admin_required
def admin_report_export():
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')

    try:
        start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date format.', 'danger')
        return redirect(url_for('admin_report'))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Day', 'Day Type', 'Capacity', 'Reservations',
                     'Checked In', 'Total Headcount', 'Arrived Headcount', 'Utilization %'])

    d = start_date
    while d <= end_date:
        day_type, capacity = get_day_info(d)
        day_reservations = Reservation.query.filter_by(reservation_date=d).all()
        res_count = len(day_reservations)
        arrived_count = sum(1 for r in day_reservations if r.arrived)
        headcount = sum(r.party_size for r in day_reservations)
        arrived_headcount = sum(r.party_size for r in day_reservations if r.arrived)
        utilization = round((headcount / capacity) * 100, 1) if capacity > 0 else 0

        writer.writerow([
            d.isoformat(),
            d.strftime('%A'),
            day_type,
            capacity,
            res_count,
            arrived_count,
            headcount,
            arrived_headcount,
            utilization
        ])
        d += timedelta(days=1)

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=usage_report_{start_str}_to_{end_str}.csv'}
    )

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
