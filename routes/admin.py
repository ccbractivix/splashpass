from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, Response
from flask_login import login_user, logout_user, login_required, current_user
from models import db, Admin, Member, Reservation, DayType
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, date, timedelta
from calendar import monthrange
import pytz
import csv
import io

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


def get_now():
    tz = pytz.timezone(current_app.config['TIMEZONE'])
    return datetime.now(tz)


@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        admin = Admin.query.filter_by(username=username).first()
        if admin and check_password_hash(admin.password_hash, password):
            login_user(admin)
            return redirect(url_for('admin.dashboard'))

        flash('Invalid credentials.', 'error')

    return render_template('admin/login.html')


@admin_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('public.index'))


@admin_bp.route('/')
@login_required
def dashboard():
    today = get_now().date()
    today_type = DayType.get_type_for_date(today)

    today_reservations = Reservation.query.filter_by(
        reservation_date=today
    ).join(Member).order_by(Member.last_name).all()

    total_heads_today = sum(r.party_size for r in today_reservations)
    arrived_heads = sum(r.party_size for r in today_reservations if r.arrived)
    capacity = DayType.get_capacity_for_date(today)

    upcoming = []
    for i in range(7):
        d = today + timedelta(days=i)
        dt = DayType.get_type_for_date(d)
        heads = db.session.query(
            db.func.coalesce(db.func.sum(Reservation.party_size), 0)
        ).filter_by(reservation_date=d).scalar()
        cap = DayType.get_capacity_for_date(d)
        upcoming.append({
            'date': d,
            'day_type': dt,
            'heads': heads,
            'capacity': cap
        })

    return render_template('admin/dashboard.html',
                           today=today,
                           today_type=today_type,
                           reservations=today_reservations,
                           total_heads=total_heads_today,
                           arrived_heads=arrived_heads,
                           capacity=capacity,
                           upcoming=upcoming)


@admin_bp.route('/reservations')
@admin_bp.route('/reservations/<date_str>')
@login_required
def reservations(date_str=None):
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid date.', 'error')
            return redirect(url_for('admin.reservations'))
    else:
        target_date = get_now().date()

    day_type = DayType.get_type_for_date(target_date)
    res_list = Reservation.query.filter_by(
        reservation_date=target_date
    ).join(Member).order_by(Member.last_name).all()

    total_heads = sum(r.party_size for r in res_list)
    arrived_heads = sum(r.party_size for r in res_list if r.arrived)
    capacity = DayType.get_capacity_for_date(target_date)

    return render_template('admin/reservations.html',
                           target_date=target_date,
                           day_type=day_type,
                           reservations=res_list,
                           total_heads=total_heads,
                           arrived_heads=arrived_heads,
                           capacity=capacity)


@admin_bp.route('/toggle_arrived/<int:res_id>', methods=['POST'])
@login_required
def toggle_arrived(res_id):
    reservation = Reservation.query.get_or_404(res_id)
    reservation.arrived = not reservation.arrived
    db.session.commit()
    return redirect(request.referrer or url_for('admin.dashboard'))


@admin_bp.route('/members')
@login_required
def members():
    search = request.args.get('search', '').strip()
    tier_filter = request.args.get('tier', '')

    query = Member.query.filter_by(active=True)

    if search:
        query = query.filter(
            db.or_(
                Member.owner_number.ilike(f'%{search}%'),
                Member.last_name.ilike(f'%{search}%'),
                Member.first_name.ilike(f'%{search}%')
            )
        )
    if tier_filter:
        query = query.filter_by(enrollment_type=tier_filter)

    member_list = query.order_by(Member.last_name, Member.first_name).all()

    return render_template('admin/members.html',
                           members=member_list,
                           search=search,
                           tier_filter=tier_filter,
                           total_count=Member.query.filter_by(active=True).count())


@admin_bp.route('/members/upload', methods=['POST'])
@login_required
def upload_members():
    file = request.files.get('csv_file')
    if not file:
        flash('No file selected.', 'error')
        return redirect(url_for('admin.members'))

    try:
        content = file.read().decode('utf-8-sig')

        # Try tab-delimited first, then comma
        if '\t' in content.split('\n')[0]:
            reader = csv.DictReader(io.StringIO(content), delimiter='\t')
        else:
            reader = csv.DictReader(io.StringIO(content))

        new_count = 0
        update_count = 0
        seen_owner_numbers = set()

        for row in reader:
            owner_number = (row.get('OwnerNumber') or row.get('Owner Number') or '').strip()
            if not owner_number:
                continue

            seen_owner_numbers.add(owner_number)

            last_name = (row.get('LastName') or row.get('Last Name') or '').strip()
            first_name = (row.get('FirstName') or row.get('First Name') or '').strip()
            expiration_date = (row.get('ExpirationDate') or row.get('Expiration Date') or '').strip()
            enrollment_type = (row.get('EnrollmentType') or row.get('Enrollment Type') or '').strip()
            membership = (row.get('Membership') or '').strip()

            enrollment_type = enrollment_type.strip().title()
            if enrollment_type not in ('Platinum', 'Gold', 'Silver'):
                continue

            existing = Member.query.filter_by(owner_number=owner_number).first()
            if existing:
                existing.last_name = last_name
                existing.first_name = first_name
                existing.expiration_date = expiration_date
                existing.enrollment_type = enrollment_type
                existing.membership = membership
                existing.active = True
                update_count += 1
            else:
                member = Member(
                    owner_number=owner_number,
                    last_name=last_name,
                    first_name=first_name,
                    expiration_date=expiration_date,
                    enrollment_type=enrollment_type,
                    membership=membership,
                    active=True
                )
                db.session.add(member)
                new_count += 1

        deactivated = Member.query.filter(
            Member.owner_number.notin_(seen_owner_numbers),
            Member.active == True
        ).update({Member.active: False}, synchronize_session=False)

        db.session.commit()
        flash(f'Import complete: {new_count} added, {update_count} updated, {deactivated} deactivated.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Import error: {str(e)}', 'error')

    return redirect(url_for('admin.members'))


@admin_bp.route('/calendar')
@admin_bp.route('/calendar/<int:year>')
@login_required
def calendar_editor(year=None):
    if year is None:
        year = get_now().date().year

    months = []
    for month in range(1, 13):
        days_in_month = monthrange(year, month)[1]
        first_weekday = date(year, month, 1).weekday()
        first_weekday_sun = (first_weekday + 1) % 7

        days = []
        for day in range(1, days_in_month + 1):
            d = date(year, month, day)
            dt = DayType.get_type_for_date(d)
            days.append({'date': d, 'day': day, 'day_type': dt})

        months.append({
            'month': month,
            'name': date(year, month, 1).strftime('%B'),
            'first_weekday': first_weekday_sun,
            'days': days
        })

    return render_template('admin/calendar_editor.html',
                           year=year,
                           months=months)


@admin_bp.route('/calendar/set', methods=['POST'])
@login_required
def calendar_set_day():
    date_str = request.form.get('date')
    day_type = request.form.get('day_type')

    if day_type not in ('Weekday', 'Weekend', 'High Use'):
        flash('Invalid day type.', 'error')
        return redirect(request.referrer)

    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        flash('Invalid date.', 'error')
        return redirect(request.referrer)

    entry = DayType.query.filter_by(date=d).first()
    if entry:
        entry.day_type = day_type
    else:
        entry = DayType(date=d, day_type=day_type)
        db.session.add(entry)

    db.session.commit()

    if request.headers.get('Content-Type') == 'application/x-www-form-urlencoded':
        return '', 204

    return redirect(request.referrer or url_for('admin.calendar_editor'))


@admin_bp.route('/calendar/print/<int:year>')
@login_required
def calendar_print(year):
    months = []
    for month in range(1, 13):
        days_in_month = monthrange(year, month)[1]
        first_weekday = date(year, month, 1).weekday()
        first_weekday_sun = (first_weekday + 1) % 7

        days = []
        for day in range(1, days_in_month + 1):
            d = date(year, month, day)
            dt = DayType.get_type_for_date(d)
            days.append({'date': d, 'day': day, 'day_type': dt})

        months.append({
            'month': month,
            'name': date(year, month, 1).strftime('%B'),
            'first_weekday': first_weekday_sun,
            'days': days
        })

    import qrcode
    import io as io_module
    import base64

    qr = qrcode.make('https://ccbrsplashpass.com/reserve')
    buffer = io_module.BytesIO()
    qr.save(buffer, format='PNG')
    qr_b64 = base64.b64encode(buffer.getvalue()).decode()

    return render_template('admin/calendar_print.html',
                           year=year,
                           months=months,
                           qr_b64=qr_b64)


@admin_bp.route('/reservations/export/<date_str>')
@login_required
def export_reservations(date_str):
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date.', 'error')
        return redirect(url_for('admin.dashboard'))

    res_list = Reservation.query.filter_by(
        reservation_date=target_date
    ).join(Member).order_by(Member.last_name).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Confirmation', 'Owner #', 'Last Name', 'First Name', 'Tier', 'Party Size', 'Arrived'])
    for r in res_list:
        writer.writerow([
            r.confirmation_code,
            r.member.owner_number,
            r.member.last_name,
            r.member.first_name,
            r.member.enrollment_type,
            r.party_size,
            'Yes' if r.arrived else 'No'
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=reservations_{date_str}.csv'}
    )
