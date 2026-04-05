from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from models import db, Member, Reservation, DayType
from datetime import datetime, timedelta
import pytz
import string
import random

public_bp = Blueprint('public', __name__)


def get_now():
    tz = pytz.timezone(current_app.config['TIMEZONE'])
    return datetime.now(tz)


def generate_confirmation_code():
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choices(chars, k=8))
        if not Reservation.query.filter_by(confirmation_code=code).first():
            return code


def get_available_dates_for_tier(tier):
    now = get_now()
    today = now.date()
    current_hour = now.hour
    available = []

    if tier == 'Platinum':
        for i in range(7):
            d = today + timedelta(days=i)
            day_type = DayType.get_type_for_date(d)
            if i == 0 and (current_hour < current_app.config['POOL_OPEN_HOUR'] or
                           current_hour >= current_app.config['POOL_CLOSE_HOUR']):
                continue
            available.append((d, day_type))

    elif tier == 'Gold':
        if (current_hour >= current_app.config['POOL_OPEN_HOUR'] and
                current_hour < current_app.config['POOL_CLOSE_HOUR']):
            day_type = DayType.get_type_for_date(today)
            if day_type in ('Weekday', 'Weekend'):
                available.append((today, day_type))

    elif tier == 'Silver':
        if (current_hour >= current_app.config['POOL_OPEN_HOUR'] and
                current_hour < current_app.config['POOL_CLOSE_HOUR']):
            day_type = DayType.get_type_for_date(today)
            if day_type == 'Weekday':
                available.append((today, day_type))

    return available


def get_headcount_for_date(target_date):
    result = db.session.query(
        db.func.coalesce(db.func.sum(Reservation.party_size), 0)
    ).filter_by(reservation_date=target_date).scalar()
    return result


@public_bp.route('/')
def index():
    return render_template('index.html')


@public_bp.route('/reserve', methods=['GET', 'POST'])
def reserve():
    if request.method == 'GET':
        return render_template('reserve.html', step='identify')

    step = request.form.get('step', 'identify')

    if step == 'identify':
        owner_number = request.form.get('owner_number', '').strip()
        member = Member.query.filter_by(owner_number=owner_number, active=True).first()

        if not member:
            flash('Member ID not found. Please check your number and try again.', 'error')
            return render_template('reserve.html', step='identify')

        tier = member.enrollment_type
        available_dates = get_available_dates_for_tier(tier)

        if not available_dates:
            flash(
                f'No dates are currently available for {tier} members. '
                f'Please check the booking rules for your membership tier.',
                'error'
            )
            return render_template('reserve.html', step='identify')

        existing = Reservation.query.filter(
            Reservation.member_id == member.id,
            Reservation.reservation_date.in_([d[0] for d in available_dates])
        ).all()
        existing_dates = {r.reservation_date for r in existing}

        available_dates = [(d, dt) for d, dt in available_dates if d not in existing_dates]

        if not available_dates:
            flash('You already have reservations for all available dates.', 'info')
            return render_template('reserve.html', step='identify')

        date_info = []
        for d, dt in available_dates:
            current_heads = get_headcount_for_date(d)
            capacity = DayType.get_capacity_for_date(d)
            remaining = capacity - current_heads
            date_info.append({
                'date': d,
                'day_type': dt,
                'remaining': remaining,
                'full': remaining <= 0
            })

        return render_template('reserve.html',
                               step='select',
                               member=member,
                               date_info=date_info,
                               max_party=current_app.config['MAX_PARTY_SIZE'])

    elif step == 'confirm':
        owner_number = request.form.get('owner_number', '').strip()
        member = Member.query.filter_by(owner_number=owner_number, active=True).first()

        if not member:
            flash('Session error. Please start over.', 'error')
            return redirect(url_for('public.reserve'))

        selected_date_str = request.form.get('reservation_date')
        party_size = int(request.form.get('party_size', 1))

        if party_size < 1 or party_size > current_app.config['MAX_PARTY_SIZE']:
            flash(f'Party size must be between 1 and {current_app.config["MAX_PARTY_SIZE"]}.', 'error')
            return redirect(url_for('public.reserve'))

        try:
            selected_date = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            flash('Invalid date selected.', 'error')
            return redirect(url_for('public.reserve'))

        available_dates = get_available_dates_for_tier(member.enrollment_type)
        allowed_dates = [d[0] for d in available_dates]
        if selected_date not in allowed_dates:
            flash('That date is not available for your membership tier.', 'error')
            return redirect(url_for('public.reserve'))

        existing = Reservation.query.filter_by(
            member_id=member.id,
            reservation_date=selected_date
        ).first()
        if existing:
            flash('You already have a reservation for this date.', 'error')
            return redirect(url_for('public.reserve'))

        current_heads = get_headcount_for_date(selected_date)
        capacity = DayType.get_capacity_for_date(selected_date)
        if current_heads + party_size > capacity:
            flash('Sorry, there is not enough capacity for your party size on this date.', 'error')
            return redirect(url_for('public.reserve'))

        code = generate_confirmation_code()
        reservation = Reservation(
            confirmation_code=code,
            member_id=member.id,
            reservation_date=selected_date,
            party_size=party_size
        )
        db.session.add(reservation)
        db.session.commit()

        return redirect(url_for('public.confirmation', code=code))

    return redirect(url_for('public.reserve'))


@public_bp.route('/confirmation/<code>')
def confirmation(code):
    reservation = Reservation.query.filter_by(confirmation_code=code).first_or_404()
    member = reservation.member

    import qrcode
    import io
    import base64

    qr = qrcode.make(f'SPLASHPASS:{code}')
    buffer = io.BytesIO()
    qr.save(buffer, format='PNG')
    qr_b64 = base64.b64encode(buffer.getvalue()).decode()

    return render_template('confirmation.html',
                           reservation=reservation,
                           member=member,
                           qr_b64=qr_b64)
