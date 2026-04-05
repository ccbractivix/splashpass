import os
import calendar as cal_mod
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
import base64
import csv
import secrets

from flask import Flask, render_template, request, redirect, url_for, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import pytz
import qrcode

from config import Config
from models import db, Member, Reservation, DayType, Admin

# ── App factory ──────────────────────────────────────────────────
app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin_login'

ET = pytz.timezone('US/Eastern')

CAPACITY = 128
MAX_PARTY = 6


@login_manager.user_loader
def load_user(user_id):
    return Admin.query.get(int(user_id))


# ── Helpers ──────────────────────────────────────────────────────
def now_et():
    return datetime.now(ET)

def today_et():
    return now_et().date()

def get_day_type(d):
    override = DayType.query.filter_by(date=d).first()
    if override:
        return override.day_type
    if d.weekday() >= 5:
        return 'Weekend'
    return 'Weekday'

def heads_for_date(d):
    result = db.session.query(db.func.coalesce(db.func.sum(Reservation.party_size), 0))\
        .filter(Reservation.reservation_date == d).scalar()
    return result

def arrived_heads_for_date(d):
    result = db.session.query(db.func.coalesce(db.func.sum(Reservation.party_size), 0))\
        .filter(Reservation.reservation_date == d, Reservation.arrived == True).scalar()
    return result

def generate_code():
    return secrets.token_hex(4).upper()

def make_qr_b64(data):
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

def available_dates_for_member(member):
    t = today_et()
    n = now_et()
    tier = member.enrollment_type

    if tier == 'Platinum':
        dates = [t + timedelta(days=i) for i in range(7)]
    else:
        hour = n.hour
        if 7 <= hour < 20:
            dates = [t]
        else:
            return []

    allowed = []
    for d in dates:
        dt = get_day_type(d)
        if tier == 'Silver' and dt != 'Weekday':
            continue
        if tier == 'Gold' and dt == 'High Use':
            continue
        allowed.append(d)
    return allowed


def build_months(year):
    months = []
    for m in range(1, 13):
        first_weekday = cal_mod.monthrange(year, m)[0]
        first_weekday = (first_weekday + 1) % 7
        num_days = cal_mod.monthrange(year, m)[1]
        days = []
        for d in range(1, num_days + 1):
            dt = date(year, m, d)
            days.append({
                'day': d,
                'date': dt,
                'day_type': get_day_type(dt),
            })
        months.append({
            'name': cal_mod.month_name[m],
            'month': m,
            'first_weekday': first_weekday,
            'days': days,
        })
    return months


# ══════════════════════════════════════════════════════════════════
#  PUBLIC ROUTES
# ══════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/reserve', methods=['GET', 'POST'])
def reserve():
    if request.method == 'GET':
        return render_template('reserve.html', step='identify')

    step = request.form.get('step', 'identify')

    if step == 'identify':
        owner = request.form.get('owner_number', '').strip()
        member = Member.query.filter_by(owner_number=owner, active=True).first()
        if not member:
            flash('Owner number not found or membership is inactive.', 'error')
            return render_template('reserve.html', step='identify')

        if member.expiration_date:
            try:
                if isinstance(member.expiration_date, str):
                    exp = datetime.strptime(member.expiration_date, '%Y-%m-%d').date()
                else:
                    exp = member.expiration_date
                if exp < today_et():
                    flash('Your membership has expired.', 'error')
                    return render_template('reserve.html', step='identify')
            except (ValueError, TypeError):
                pass

        dates = available_dates_for_member(member)
        if not dates:
            flash('No dates are available for your membership tier right now.', 'warning')
            return render_template('reserve.html', step='identify')

        date_info = []
        for d in dates:
            h = heads_for_date(d)
            date_info.append({
                'date': d,
                'day_type': get_day_type(d),
                'remaining': CAPACITY - h,
                'full': h >= CAPACITY,
            })

        return render_template('reserve.html', step='select',
                               member=member, date_info=date_info, max_party=MAX_PARTY)

    if step == 'confirm':
        owner = request.form.get('owner_number', '').strip()
        member = Member.query.filter_by(owner_number=owner, active=True).first()
        if not member:
            flash('Session error. Please start over.', 'error')
            return redirect(url_for('reserve'))

        date_str = request.form.get('reservation_date', '')
        try:
            res_date = date.fromisoformat(date_str)
        except (ValueError, TypeError):
            flash('Invalid date selected.', 'error')
            return redirect(url_for('reserve'))

        party_size = int(request.form.get('party_size', 1))
        party_size = max(1, min(party_size, MAX_PARTY))

        allowed = available_dates_for_member(member)
        if res_date not in allowed:
            flash('That date is not available for your tier.', 'error')
            return redirect(url_for('reserve'))

        current_heads = heads_for_date(res_date)
        if current_heads + party_size > CAPACITY:
            flash(f'Not enough capacity. Only {CAPACITY - current_heads} spots remain.', 'error')
            return redirect(url_for('reserve'))

        existing = Reservation.query.filter_by(
            member_id=member.id, reservation_date=res_date).first()
        if existing:
            flash('You already have a reservation for this date.', 'warning')
            return redirect(url_for('reserve'))

        code = generate_code()
        while Reservation.query.filter_by(confirmation_code=code).first():
            code = generate_code()

        reservation = Reservation(
            member_id=member.id,
            reservation_date=res_date,
            party_size=party_size,
            confirmation_code=code,
        )
        db.session.add(reservation)
        db.session.commit()

        qr_data = f"SPLASHPASS:{code}"
        qr_b64 = make_qr_b64(qr_data)

        return render_template('confirmation.html',
                               reservation=reservation, member=member, qr_b64=qr_b64)

    return redirect(url_for('reserve'))


# ══════════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        user = Admin.query.filter_by(username=username).first()
        if user and user.password_hash == password:
            login_user(user)
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials.', 'error')
    return render_template('admin/login.html')

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/admin')
@login_required
def admin_dashboard():
    t = today_et()
    reservations = Reservation.query.filter_by(reservation_date=t)\
        .join(Member).order_by(Member.last_name).all()
    total = heads_for_date(t)
    arrived = arrived_heads_for_date(t)
    day_type = get_day_type(t)

    upcoming = []
    for i in range(7):
        d = t + timedelta(days=i)
        upcoming.append({
            'date': d,
            'day_type': get_day_type(d),
            'heads': heads_for_date(d),
            'capacity': CAPACITY,
        })

    return render_template('admin/dashboard.html',
                           reservations=reservations,
                           total_heads=total,
                           arrived_heads=arrived,
                           today=t,
                           today_type=day_type,
                           capacity=CAPACITY,
                           upcoming=upcoming)

@app.route('/admin/reservations/<date_str>')
@login_required
def admin_reservations(date_str):
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        flash('Invalid date.', 'error')
        return redirect(url_for('admin_dashboard'))

    reservations = Reservation.query.filter_by(reservation_date=target)\
        .join(Member).order_by(Member.last_name).all()

    return render_template('admin/reservations.html',
                           reservations=reservations,
                           target_date=target,
                           day_type=get_day_type(target),
                           total_heads=heads_for_date(target),
                           arrived_heads=arrived_heads_for_date(target),
                           capacity=CAPACITY)

@app.route('/admin/toggle_arrived/<int:res_id>', methods=['POST'])
@login_required
def toggle_arrived(res_id):
    r = Reservation.query.get_or_404(res_id)
    r.arrived = not r.arrived
    db.session.commit()
    ref = request.referrer or url_for('admin_dashboard')
    return redirect(ref)

@app.route('/admin/export/<date_str>')
@login_required
def export_reservations(date_str):
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        flash('Invalid date.', 'error')
        return redirect(url_for('admin_dashboard'))

    reservations = Reservation.query.filter_by(reservation_date=target)\
        .join(Member).order_by(Member.last_name).all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Confirmation', 'LastName', 'FirstName', 'OwnerNumber',
                     'Tier', 'PartySize', 'Arrived'])
    for r in reservations:
        writer.writerow([r.confirmation_code, r.member.last_name, r.member.first_name,
                         r.member.owner_number, r.member.enrollment_type,
                         r.party_size, 'Yes' if r.arrived else 'No'])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=reservations_{date_str}.csv'}
    )


# ── Member management ────────────────────────────────────────────
@app.route('/admin/members')
@login_required
def admin_members():
    search = request.args.get('search', '').strip()
    tier_filter = request.args.get('tier', '').strip()

    query = Member.query.filter_by(active=True)
    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                Member.owner_number.ilike(like),
                Member.last_name.ilike(like),
                Member.first_name.ilike(like),
            )
        )
    if tier_filter:
        query = query.filter_by(enrollment_type=tier_filter)

    members = query.order_by(Member.last_name, Member.first_name).limit(200).all()
    total_count = Member.query.filter_by(active=True).count()

    return render_template('admin/members.html',
                           members=members, search=search,
                           tier_filter=tier_filter, total_count=total_count)

@app.route('/admin/upload_members', methods=['POST'])
@login_required
def upload_members():
    file = request.files.get('csv_file')
    if not file:
        flash('No file uploaded.', 'error')
        return redirect(url_for('admin_members'))

    try:
        content = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        content = file.read().decode('latin-1')

    if '\t' in content[:500]:
        delimiter = '\t'
    else:
        delimiter = ','

    reader = csv.DictReader(StringIO(content), delimiter=delimiter)

    if reader.fieldnames:
        reader.fieldnames = [h.strip() for h in reader.fieldnames]

    seen_owners = set()
    added = 0
    updated = 0

    for row in reader:
        owner = row.get('OwnerNumber', '').strip()
        if not owner:
            continue
        seen_owners.add(owner)

        last_name = row.get('LastName', '').strip()
        first_name = row.get('FirstName', '').strip()
        enrollment = row.get('EnrollmentType', '').strip()
        membership = row.get('Membership', '').strip()
        exp_str = row.get('ExpirationDate', '').strip()

        existing = Member.query.filter_by(owner_number=owner).first()
        if existing:
            existing.last_name = last_name
            existing.first_name = first_name
            existing.enrollment_type = enrollment
            existing.membership = membership
            existing.expiration_date = exp_str if exp_str else None
            existing.active = True
            updated += 1
        else:
            m = Member(
                owner_number=owner,
                last_name=last_name,
                first_name=first_name,
                enrollment_type=enrollment,
                membership=membership,
                expiration_date=exp_str if exp_str else None,
                active=True,
            )
            db.session.add(m)
            added += 1

    if seen_owners:
        Member.query.filter(~Member.owner_number.in_(seen_owners))\
            .update({Member.active: False}, synchronize_session='fetch')

    db.session.commit()
    flash(f'Import complete: {added} added, {updated} updated, '
          f'{Member.query.filter_by(active=False).count()} deactivated.', 'success')
    return redirect(url_for('admin_members'))


# ── Calendar management ──────────────────────────────────────────
@app.route('/admin/calendar')
@app.route('/admin/calendar/<int:year>')
@login_required
def admin_calendar_editor(year=None):
    if year is None:
        year = today_et().year
    months = build_months(year)
    return render_template('admin/calendar_editor.html', year=year, months=months)

@app.route('/admin/calendar/set_day', methods=['POST'])
@login_required
def admin_calendar_set_day():
    date_str = request.form.get('date', '')
    day_type = request.form.get('day_type', '')

    try:
        d = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return ('Bad date', 400)

    if day_type not in ('Weekday', 'Weekend', 'High Use'):
        return ('Bad type', 400)

    default = 'Weekend' if d.weekday() >= 5 else 'Weekday'

    override = DayType.query.filter_by(date=d).first()
    if day_type == default:
        if override:
            db.session.delete(override)
    else:
        if override:
            override.day_type = day_type
        else:
            override = DayType(date=d, day_type=day_type)
            db.session.add(override)

    db.session.commit()
    return ('OK', 200)

@app.route('/admin/calendar/print/<int:year>')
@login_required
def admin_calendar_print(year):
    months = build_months(year)
    qr_b64 = make_qr_b64('https://ccbrsplashpass.com')
    return render_template('admin/calendar_print.html', year=year, months=months, qr_b64=qr_b64)


# ══════════════════════════════════════════════════════════════════
#  INIT
# ══════════════════════════════════════════════════════════════════
def init_db():
    db.create_all()
    if not Admin.query.first():
        admin = Admin(
            username='admin',
            password_hash=os.environ.get('ADMIN_PASSWORD', 'changeme')
        )
        db.session.add(admin)
        db.session.commit()
        print('Default admin user created (username: admin)')

with app.app_context():
    init_db()


if __name__ == '__main__':
    app.run(debug=True)
