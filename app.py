import os, io, csv, base64, string, random, re, calendar as cal_module
from datetime import datetime, timedelta, date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, Response, send_file, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
import pytz
import qrcode
import sendgrid
from sendgrid.helpers.mail import (
    Mail, Email, To, Content, Attachment, FileContent,
    FileName, FileType, Disposition
)
from PIL import Image, ImageDraw, ImageFont
import calendar as pycalendar

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
EST_MEMBERSHIP = 'Employee'
EST_MAX_ADVANCE_DAYS = 183  # ~6 months

db = SQLAlchemy(app)
csrf = CSRFProtect(app)

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
    enrollment_type = db.Column(db.String(20), nullable=False, default='Individual', server_default='Individual')
    membership = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(200), nullable=True)
    active = db.Column(db.Boolean, default=True)
    reservations = db.relationship('Reservation', backref='member', lazy=True)

class DayType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    day_type = db.Column(db.String(20), nullable=False, default='Weekday')
    capacity_override = db.Column(db.Integer, nullable=True)

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
REPORT_LINK = ' <a href="/report" class="alert-link">Report a Problem</a>'

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

def generate_est_code():
    """Generate an Employee Splash Time confirmation code: EST. + 6 chars."""
    chars = string.ascii_uppercase + string.digits
    while True:
        code = 'EST.' + ''.join(random.choices(chars, k=6))
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

def is_gold_silver_blocked():
    """Return True if current Eastern time is outside 7 AM – 8 PM."""
    hour = now_eastern().hour
    return hour < 7 or hour >= 20

def get_member_available_dates(member):
    """Return list of available dates for a verified member."""
    today = today_eastern()
    tier = member.membership

    if tier == 'Platinum':
        max_date = today + timedelta(days=6)
    elif tier in ('Gold', 'Silver'):
        if is_gold_silver_blocked():
            return []
        max_date = today
    else:
        return []

    # Pre-fetch existing reservations for this member in the date range
    existing_dates = {
        r.reservation_date for r in Reservation.query.filter(
            Reservation.member_id == member.id,
            Reservation.reservation_date >= today,
            Reservation.reservation_date <= max_date
        ).all()
    }

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
            if d in existing_dates:
                continue
            available_dates.append({
                'date': d,
                'day_type': day_type,
            })
    return available_dates

def send_problem_report_email(name, owner_number, contact, message):
    sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
    from_email = Email(os.environ.get('MAIL_FROM', 'noreply@ccbrsplashpass.com'))
    to_email = To(os.environ.get('MAIL_RECIPIENT'))
    subject = f"SplashPass Problem Report - Owner #{owner_number}"
    body = f"""New Problem Report Submitted

Name: {name}
Owner Number: {owner_number}
Contact: {contact}

Message:
{message}
"""
    content = Content("text/plain", body)
    mail = Mail(from_email, to_email, subject, content)
    response = sg.client.mail.send.post(request_body=mail.get())
    return response.status_code

def send_confirmation_email(member, reservation, qr_base64, recipient_email=None):
    """Send reservation confirmation with QR code to the given email address."""
    email_addr = recipient_email or member.email
    if not email_addr:
        return None

    api_key = os.environ.get('SENDGRID_API_KEY')
    if not api_key:
        return None

    sg = sendgrid.SendGridAPIClient(api_key=api_key)
    from_email = Email(os.environ.get('MAIL_FROM', 'noreply@ccbrsplashpass.com'))
    to_email = To(email_addr)

    subject = f"SplashPass Confirmation — {reservation.reservation_date.strftime('%A, %B %-d, %Y')}"

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #0d6efd; color: white; padding: 20px; text-align: center;">
            <h1 style="margin: 0;">🏊 SplashPass</h1>
            <p style="margin: 5px 0 0;">Reservation Confirmed</p>
        </div>

        <div style="padding: 20px; border: 1px solid #dee2e6; border-top: none;">
            <p>Hi <strong>{member.first_name}</strong>,</p>
            <p>Your reservation has been confirmed! Here are the details:</p>

            <table style="width: 100%; border-collapse: collapse; margin: 15px 0;">
                <tr>
                    <td style="padding: 8px; border: 1px solid #dee2e6; font-weight: bold; background: #f8f9fa; width: 40%;">Confirmation Code</td>
                    <td style="padding: 8px; border: 1px solid #dee2e6; font-size: 18px; font-weight: bold; letter-spacing: 2px;">{reservation.confirmation_code}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #dee2e6; font-weight: bold; background: #f8f9fa;">Date</td>
                    <td style="padding: 8px; border: 1px solid #dee2e6;">{reservation.reservation_date.strftime('%A, %B %-d, %Y')}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #dee2e6; font-weight: bold; background: #f8f9fa;">Party Size</td>
                    <td style="padding: 8px; border: 1px solid #dee2e6;">{reservation.party_size}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #dee2e6; font-weight: bold; background: #f8f9fa;">Member</td>
                    <td style="padding: 8px; border: 1px solid #dee2e6;">{member.first_name} {member.last_name} (#{member.owner_number})</td>
                </tr>
            </table>

            <div style="text-align: center; margin: 20px 0;">
                <p style="margin-bottom: 10px; font-weight: bold;">Show this QR code at check-in:</p>
                <img src="cid:qrcode" alt="QR Code" style="width: 200px; height: 200px;">
            </div>

            <div style="background: #fff3cd; border: 1px solid #ffc107; border-radius: 5px; padding: 15px; margin: 15px 0;">
                <strong>Reminders:</strong>
                <ul style="margin: 5px 0; padding-left: 20px;">
                    <li>Access hours: 8:00 AM – 10:00 PM</li>
                    <li>Bring photo ID for check-in</li>
                    <li>Bring your own towels</li>
                    <li>No outside food or beverages on pool deck</li>
                </ul>
            </div>

            <p style="color: #6c757d; font-size: 12px; margin-top: 20px;">
                You can view your reservations anytime at
                <a href="https://ccbrsplashpass.com/lookup">ccbrsplashpass.com/lookup</a>.
                If you need help, use our <a href="https://ccbrsplashpass.com/report">Report a Problem</a> form.
            </p>
        </div>
    </div>
    """

    mail = Mail()
    mail.from_email = from_email
    mail.to = [to_email]
    mail.subject = subject
    mail.content = [Content("text/html", html_body)]

    # Attach QR code as inline image
    attachment = Attachment()
    attachment.file_content = FileContent(qr_base64)
    attachment.file_name = FileName("qrcode.png")
    attachment.file_type = FileType("image/png")
    attachment.disposition = Disposition("inline")
    attachment.content_id = "qrcode"
    mail.attachment = [attachment]

    try:
        response = sg.client.mail.send.post(request_body=mail.get())
        return response.status_code
    except Exception:
        return None

def generate_calendar_png(year):
    """Generate printable and web calendar PNGs for a given year."""

    # --- Configuration ---
    FULL_W, FULL_H = 3000, 2000
    COLS, ROWS = 4, 3
    HIC_ORANGE = (227, 108, 34)
    LIGHT_GRAY = (245, 245, 245)
    HEADER_GOLD = (218, 165, 32)
    HEADER_TEXT = (255, 255, 255)
    DOW_BG = (240, 220, 160)
    DOW_TEXT = (80, 80, 80)
    DAY_TEXT = (50, 50, 50)
    DAY_TEXT_HIGH = (255, 255, 255)
    TITLE_COLOR = (40, 40, 40)
    BORDER_COLOR = (200, 200, 200)
    WHITE = (255, 255, 255)
    FOOTER_COLOR = (100, 100, 100)

    # --- Gather high-use dates ---
    high_dates = set()
    days = DayType.query.filter(
        db.extract('year', DayType.date) == year,
        DayType.day_type == 'High Use'
    ).all()
    for d in days:
        high_dates.add(d.date)

    # --- Create image ---
    img = Image.new('RGB', (FULL_W, FULL_H), WHITE)
    draw = ImageDraw.Draw(img)

    # --- Fonts ---
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
        font_month = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_dow = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        font_day = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        font_day_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        font_footer = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except OSError:
        font_title = ImageFont.load_default()
        font_month = font_title
        font_dow = font_title
        font_day = font_title
        font_day_bold = font_title
        font_footer = font_title

    # --- Layout constants ---
    TOP_MARGIN = 100
    BOTTOM_MARGIN = 80
    SIDE_MARGIN = 60
    MONTH_PAD = 12
    LOGO_SIZE = 70

    grid_w = FULL_W - 2 * SIDE_MARGIN
    grid_h = FULL_H - TOP_MARGIN - BOTTOM_MARGIN
    cell_w = grid_w // COLS
    cell_h = grid_h // ROWS

    # --- Title ---
    title_text = f"HIC — {year} Pool Calendar"
    bbox = draw.textbbox((0, 0), title_text, font=font_title)
    tw = bbox[2] - bbox[0]
    draw.text(((FULL_W - tw) // 2, 25), title_text, fill=TITLE_COLOR, font=font_title)

    # --- Logo ---
    logo_path = os.path.join('static', 'images', 'logo.png')
    if os.path.exists(logo_path):
        logo = Image.open(logo_path).convert('RGBA')
        logo = logo.resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
        img.paste(logo, (SIDE_MARGIN, 15), logo)

    # --- Draw months ---
    dow_labels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

    for month_idx in range(12):
        row = month_idx // COLS
        col = month_idx % COLS
        month_num = month_idx + 1

        mx = SIDE_MARGIN + col * cell_w
        my = TOP_MARGIN + row * cell_h

        inner_x = mx + MONTH_PAD
        inner_y = my + MONTH_PAD
        inner_w = cell_w - 2 * MONTH_PAD
        inner_h = cell_h - 2 * MONTH_PAD

        # Month header bar
        header_h = 36
        draw.rectangle([inner_x, inner_y, inner_x + inner_w, inner_y + header_h], fill=HEADER_GOLD)
        month_name = pycalendar.month_name[month_num]
        bbox = draw.textbbox((0, 0), month_name, font=font_month)
        mtw = bbox[2] - bbox[0]
        mth = bbox[3] - bbox[1]
        draw.text((inner_x + (inner_w - mtw) // 2, inner_y + (header_h - mth) // 2 - 2),
                  month_name, fill=HEADER_TEXT, font=font_month)

        # Day-of-week row
        dow_y = inner_y + header_h
        dow_h = 26
        day_col_w = inner_w / 7
        draw.rectangle([inner_x, dow_y, inner_x + inner_w, dow_y + dow_h], fill=DOW_BG)
        for di, label in enumerate(dow_labels):
            lx = inner_x + di * day_col_w
            bbox = draw.textbbox((0, 0), label, font=font_dow)
            lw = bbox[2] - bbox[0]
            draw.text((lx + (day_col_w - lw) // 2, dow_y + 3), label, fill=DOW_TEXT, font=font_dow)

        # Day grid
        day_grid_y = dow_y + dow_h
        remaining_h = inner_h - header_h - dow_h
        day_row_h = remaining_h / 6  # max 6 week rows

        # Get calendar for this month (Sunday start)
        pcal = pycalendar.Calendar(firstweekday=6)  # Sunday
        weeks = pcal.monthdayscalendar(year, month_num)

        for wi, week in enumerate(weeks):
            for di, day in enumerate(week):
                dx = inner_x + di * day_col_w
                dy = day_grid_y + wi * day_row_h

                if day == 0:
                    draw.rectangle([dx, dy, dx + day_col_w, dy + day_row_h],
                                   fill=WHITE, outline=BORDER_COLOR, width=1)
                else:
                    current_date = date(year, month_num, day)
                    is_high = current_date in high_dates

                    bg = HIC_ORANGE if is_high else LIGHT_GRAY
                    txt_color = DAY_TEXT_HIGH if is_high else DAY_TEXT
                    fnt = font_day_bold if is_high else font_day

                    draw.rectangle([dx, dy, dx + day_col_w, dy + day_row_h],
                                   fill=bg, outline=BORDER_COLOR, width=1)

                    day_str = str(day)
                    bbox = draw.textbbox((0, 0), day_str, font=fnt)
                    dw = bbox[2] - bbox[0]
                    dh = bbox[3] - bbox[1]
                    draw.text((dx + (day_col_w - dw) // 2, dy + (day_row_h - dh) // 2 - 1),
                              day_str, fill=txt_color, font=fnt)

        # Border around entire month
        draw.rectangle([inner_x, inner_y, inner_x + inner_w, inner_y + inner_h],
                       outline=BORDER_COLOR, width=2)

    # --- Footer ---
    footer_lines = [
        "High Use Days are shown in Orange. Resort Day use bands must be worn at all times while on property.",
        "Hours of usage reflect hours of operation 8am - 10pm."
    ]
    fy = FULL_H - BOTTOM_MARGIN + 10
    for line in footer_lines:
        bbox = draw.textbbox((0, 0), line, font=font_footer)
        flw = bbox[2] - bbox[0]
        draw.text(((FULL_W - flw) // 2, fy), line, fill=FOOTER_COLOR, font=font_footer)
        fy += 28

    # --- Save ---
    cal_dir = os.path.join('static', 'calendars')
    os.makedirs(cal_dir, exist_ok=True)

    full_path = os.path.join(cal_dir, f'{year}_full.png')
    web_path = os.path.join(cal_dir, f'{year}_web.png')

    img.save(full_path, 'PNG', dpi=(300, 300))

    web_img = img.resize((400, 267), Image.LANCZOS)
    web_img.save(web_path, 'PNG')

    return full_path, web_path

# ---------------------------------------------------------------------------
# Context processor — injects calendar flags and login state into all templates
# ---------------------------------------------------------------------------
@app.context_processor
def inject_global_context():
    now = datetime.now(EASTERN)
    year = now.year
    web_path = os.path.join('static', 'calendars', f'{year}_web.png')
    full_path = os.path.join('static', 'calendars', f'{year}_full.png')

    nav_member = None
    member_id = session.get('member_id')
    if member_id:
        nav_member = Member.query.get(member_id)
        if nav_member and not nav_member.active:
            nav_member = None

    return dict(
        calendar_exists=os.path.exists(web_path),
        full_calendar_exists=os.path.exists(full_path),
        current_year=year,
        nav_member=nav_member,
        nav_admin=session.get('admin_logged_in', False),
        nav_checkin=session.get('checkin_logged_in', False),
    )

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    member = None
    member_id = session.get('member_id')
    if member_id:
        member = Member.query.get(member_id)
        if member and not member.active:
            session.pop('member_id', None)
            member = None
    return render_template('index.html', member=member)

@app.route('/book', methods=['GET', 'POST'])
def book():
    member_id = session.get('member_id')
    if not member_id:
        flash('Please log in first.', 'warning')
        return redirect(url_for('index'))

    member = Member.query.get(member_id)
    if not member or not member.active:
        session.pop('member_id', None)
        flash('Please log in first.', 'warning')
        return redirect(url_for('index'))

    available_dates = get_member_available_dates(member)
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
        flash('Member number not found. Please check your number and try again.' + REPORT_LINK, 'danger')
        return redirect(url_for('book'))

    try:
        res_date = datetime.strptime(reservation_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date selected.' + REPORT_LINK, 'danger')
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
            flash('Date outside your booking window.' + REPORT_LINK, 'danger')
            return redirect(url_for('book'))
    elif tier == 'Gold':
        if res_date != today:
            flash('Gold members can only book same-day.' + REPORT_LINK, 'danger')
            return redirect(url_for('book'))
        if is_gold_silver_blocked():
            flash('Gold members can only book between 7:00 AM and 8:00 PM.' + REPORT_LINK, 'danger')
            return redirect(url_for('book'))
    elif tier == 'Silver':
        if res_date != today:
            flash('Silver members can only book same-day.' + REPORT_LINK, 'danger')
            return redirect(url_for('book'))
        if is_gold_silver_blocked():
            flash('Silver members can only book between 7:00 AM and 8:00 PM.' + REPORT_LINK, 'danger')
            return redirect(url_for('book'))
    else:
        flash('Unknown membership tier.' + REPORT_LINK, 'danger')
        return redirect(url_for('book'))

    day_type, capacity = get_day_info(res_date)

    if tier == 'Silver' and day_type != 'Weekday':
        flash('Silver members can only book Weekday dates.' + REPORT_LINK, 'danger')
        return redirect(url_for('book'))
    if tier == 'Gold' and day_type == 'High Use':
        flash('Gold members cannot book High Use dates.' + REPORT_LINK, 'danger')
        return redirect(url_for('book'))

    used = get_capacity_used(res_date)
    if used + party_size > capacity:
        flash('Sorry, not enough availability for that date and party size.' + REPORT_LINK, 'danger')
        return redirect(url_for('book'))

    existing = Reservation.query.filter_by(member_id=member.id, reservation_date=res_date).first()
    if existing:
        flash(f'You already have a reservation for {res_date.strftime("%A, %B %-d, %Y")} '
              f'(Confirmation: {existing.confirmation_code}). '
              f'Only one reservation per date is allowed.', 'warning')
        return redirect(url_for('book'))

    session['pending_reservation'] = {
        'owner_number': owner_number,
        'reservation_date': reservation_date_str,
        'party_size': party_size
    }
    return redirect(url_for('terms'))

@app.route('/terms', methods=['GET', 'POST'])
def terms():
    pending = session.get('pending_reservation')
    if not pending:
        flash('Your session has expired. Please start a new booking.', 'warning')
        return redirect(url_for('book'))

    member = Member.query.filter_by(owner_number=pending['owner_number'], active=True).first()
    if not member:
        session.pop('pending_reservation', None)
        flash('Member not found. Please start a new booking.' + REPORT_LINK, 'danger')
        return redirect(url_for('book'))

    try:
        res_date = datetime.strptime(pending['reservation_date'], '%Y-%m-%d').date()
    except (ValueError, KeyError):
        session.pop('pending_reservation', None)
        flash('Invalid reservation data. Please start a new booking.', 'danger')
        return redirect(url_for('book'))

    party_size = pending.get('party_size')
    if not party_size or not isinstance(party_size, int) or party_size < 1 or party_size > 6:
        session.pop('pending_reservation', None)
        flash('Invalid reservation data. Please start a new booking.', 'danger')
        return redirect(url_for('book'))

    if request.method == 'GET':
        return render_template('terms.html',
                               member=member,
                               reservation_date=res_date,
                               party_size=party_size)

    if not request.form.get('agree'):
        flash('You must agree to the terms of use to complete your reservation.', 'danger')
        return render_template('terms.html',
                               member=member,
                               reservation_date=res_date,
                               party_size=party_size)

    day_type, capacity = get_day_info(res_date)
    used = get_capacity_used(res_date)
    if used + party_size > capacity:
        session.pop('pending_reservation', None)
        flash('Sorry, availability changed while you were reviewing the terms. Please try again.' + REPORT_LINK, 'danger')
        return redirect(url_for('book'))

    existing = Reservation.query.filter_by(member_id=member.id, reservation_date=res_date).first()
    if existing:
        session.pop('pending_reservation', None)
        flash(f'You already have a reservation for {res_date.strftime("%A, %B %-d, %Y")} '
              f'(Confirmation: {existing.confirmation_code}).', 'warning')
        return redirect(url_for('book'))

    today = today_eastern()
    tier = member.membership
    if tier == 'Platinum':
        if res_date < today or res_date > today + timedelta(days=6):
            session.pop('pending_reservation', None)
            flash('This date is no longer within your booking window. Please start over.', 'danger')
            return redirect(url_for('book'))
    elif tier in ('Gold', 'Silver'):
        if res_date != today:
            session.pop('pending_reservation', None)
            flash('This date is no longer available for same-day booking. Please start over.', 'danger')
            return redirect(url_for('book'))
        if is_gold_silver_blocked():
            session.pop('pending_reservation', None)
            flash('Reservations for Gold and Silver members are only available between 7:00 AM and 8:00 PM. Please try again later.', 'danger')
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

    session.pop('pending_reservation', None)

    qr_data = make_qr_base64(code)

    return render_template('confirmation.html',
                           reservation=reservation,
                           member=member,
                           qr_data=qr_data)

@app.route('/lookup', methods=['GET', 'POST'])
def lookup():
    member_id = session.get('member_id')
    if not member_id:
        flash('Please log in first.', 'warning')
        return redirect(url_for('index'))

    member = Member.query.get(member_id)
    if not member or not member.active:
        session.pop('member_id', None)
        flash('Please log in first.', 'warning')
        return redirect(url_for('index'))

    today = today_eastern()
    reservations = Reservation.query.filter(
        Reservation.member_id == member.id,
        Reservation.reservation_date >= today
    ).order_by(Reservation.reservation_date).all()

    return render_template('lookup.html', member=member, reservations=reservations)

@app.route('/cancel/<int:res_id>', methods=['POST'])
def cancel_reservation(res_id):
    reservation = Reservation.query.get_or_404(res_id)
    today = today_eastern()

    if reservation.reservation_date < today:
        flash('Cannot cancel past reservations.', 'danger')
        return redirect(url_for('lookup'))

    db.session.delete(reservation)
    db.session.commit()
    flash('Reservation cancelled.', 'success')
    return redirect(url_for('lookup'))

@app.route('/member/login', methods=['POST'])
def member_login():
    owner_number = request.form.get('owner_number', '').strip()
    last_name = request.form.get('last_name', '').strip()

    if not owner_number or not last_name:
        return jsonify({'success': False, 'message': 'Please enter both your Owner Number and Last Name.'}), 400

    member = Member.query.filter_by(owner_number=owner_number, active=True).first()
    if not member or member.last_name.strip().lower() != last_name.lower():
        return jsonify({'success': False, 'message': 'Account not found. Please check your Owner Number and Last Name and try again.'}), 401

    session['member_id'] = member.id
    return jsonify({'success': True, 'name': f'{member.first_name} {member.last_name}'})

@app.route('/member/logout')
def member_logout():
    session.pop('member_id', None)
    session.pop('pending_reservation', None)
    return redirect(url_for('index'))

@app.route('/member/beacon-logout', methods=['POST'])
@csrf.exempt
def member_beacon_logout():
    session.pop('member_id', None)
    session.pop('pending_reservation', None)
    return '', 204

@app.route('/send-confirmation-email', methods=['POST'])
def send_confirmation_email_route():
    """Send confirmation email with QR code to a user-provided email address."""
    member_id = session.get('member_id')
    if not member_id:
        return jsonify({'success': False, 'message': 'Session expired. Please log in again.'}), 401

    member = Member.query.get(member_id)
    if not member or not member.active:
        return jsonify({'success': False, 'message': 'Member not found.'}), 404

    email = (request.form.get('email') or '').strip()
    confirmation_code = (request.form.get('confirmation_code') or '').strip()

    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400
    if not confirmation_code:
        return jsonify({'success': False, 'message': 'Missing confirmation code.'}), 400

    reservation = Reservation.query.filter_by(
        confirmation_code=confirmation_code,
        member_id=member.id
    ).first()
    if not reservation:
        return jsonify({'success': False, 'message': 'Reservation not found.'}), 404

    qr_data = make_qr_base64(confirmation_code)
    status = send_confirmation_email(member, reservation, qr_data, recipient_email=email)

    if status and 200 <= status < 300:
        return jsonify({'success': True, 'message': 'Confirmation email sent! Check your inbox.'})
    else:
        return jsonify({'success': False, 'message': 'Unable to send email. Please save your confirmation code and QR code from this screen.'}), 500

@app.route('/report', methods=['GET'])
def report_form():
    return render_template('report.html')

@app.route('/report', methods=['POST'])
def report_submit():
    name = request.form.get('name', '').strip()
    owner_number = request.form.get('owner_number', '').strip()
    contact = request.form.get('contact', '').strip()
    message = request.form.get('message', '').strip()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not name or not owner_number or not contact or not message:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Please fill out all fields.'}), 400
        flash('Please fill out all fields.', 'danger')
        return redirect(url_for('report_form'))

    try:
        send_problem_report_email(name, owner_number, contact, message)
        if is_ajax:
            session.pop('member_id', None)
            session.pop('pending_reservation', None)
            return jsonify({'success': True, 'message': 'Your report has been submitted. We will be in touch soon. You will be logged out shortly.'})
        flash('Your report has been submitted. We will be in touch soon.', 'success')
    except Exception:
        if is_ajax:
            session.pop('member_id', None)
            session.pop('pending_reservation', None)
            return jsonify({'success': False, 'message': 'There was a problem sending your report. Please try again later.'}), 500
        flash('There was a problem sending your report. Please try again later.', 'danger')

    return redirect(url_for('report_form'))

# ---------------------------------------------------------------------------
# Pool FAQ
# ---------------------------------------------------------------------------
@app.route('/pool-faq')
def pool_faq():
    return render_template('pool_faq.html')

# ---------------------------------------------------------------------------
# Public calendar routes
# ---------------------------------------------------------------------------
@app.route('/calendar')
@app.route('/calendar/<int:year>/<int:month>')
def calendar_view(year=None, month=None):
    today = today_eastern()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    if month < 1:
        month = 12
        year -= 1
    elif month > 12:
        month = 1
        year += 1

    cal = cal_module.Calendar(firstweekday=6)
    weeks = cal.monthdayscalendar(year, month)
    month_name = cal_module.month_name[month]

    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)

    high_use_days = {
        dt.date.day for dt in DayType.query.filter(
            DayType.date >= start_date,
            DayType.date < end_date,
            DayType.day_type == 'High Use'
        ).all()
    }

    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1

    return render_template('calendar.html',
                           year=year, month=month, month_name=month_name,
                           weeks=weeks, high_use_days=high_use_days,
                           today=today,
                           prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month)


@app.route('/calendar/full')
@app.route('/calendar/full/<int:year>')
def calendar_full(year=None):
    today = today_eastern()
    if year is None:
        year = today.year

    cal = cal_module.Calendar(firstweekday=6)

    months = []
    for m in range(1, 13):
        weeks = cal.monthdayscalendar(year, m)
        month_name = cal_module.month_name[m]

        start_date = date(year, m, 1)
        if m == 12:
            end_date = date(year + 1, 1, 1)
        else:
            end_date = date(year, m + 1, 1)

        high_use_days = {
            dt.date.day for dt in DayType.query.filter(
                DayType.date >= start_date,
                DayType.date < end_date,
                DayType.day_type == 'High Use'
            ).all()
        }

        months.append({
            'month': m,
            'month_name': month_name,
            'weeks': weeks,
            'high_use_days': high_use_days
        })

    return render_template('calendar_full.html',
                           year=year, months=months, today=today)

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

@app.route('/checkin/beacon-logout', methods=['POST'])
@csrf.exempt
def checkin_beacon_logout():
    session.pop('checkin_logged_in', None)
    return '', 204

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

@app.route('/admin/beacon-logout', methods=['POST'])
@csrf.exempt
def admin_beacon_logout():
    session.pop('admin_logged_in', None)
    return '', 204

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

# ---- Employee Splash Time ----

@app.route('/admin/employee-splash-time', methods=['GET', 'POST'])
@admin_required
def employee_splash_time():
    if request.method == 'GET':
        return render_template('admin/employee_splash_time.html')

    # --- process POST ---
    last_name = (request.form.get('last_name') or '').strip()
    first_name = (request.form.get('first_name') or '').strip()
    reservation_date_str = request.form.get('reservation_date', '')
    party_size_str = request.form.get('party_size', '')

    if not last_name or not first_name:
        flash('Last name and first name are required.', 'danger')
        return render_template('admin/employee_splash_time.html')

    try:
        res_date = datetime.strptime(reservation_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Please select a valid date.', 'danger')
        return render_template('admin/employee_splash_time.html')

    try:
        party_size = int(party_size_str)
        if party_size < 1 or party_size > 6:
            raise ValueError
    except (ValueError, TypeError):
        flash('Party size must be between 1 and 6.', 'danger')
        return render_template('admin/employee_splash_time.html')

    today = today_eastern()
    max_date = today + timedelta(days=EST_MAX_ADVANCE_DAYS)

    if res_date < today:
        flash('Cannot make a reservation in the past.', 'danger')
        return render_template('admin/employee_splash_time.html')
    if res_date > max_date:
        flash('Employee Splash Time reservations can only be made up to 6 months in advance.', 'danger')
        return render_template('admin/employee_splash_time.html')

    day_type, capacity = get_day_info(res_date)
    if day_type == 'High Use':
        flash('Employee Splash Time is not available on High Use days.', 'danger')
        return render_template('admin/employee_splash_time.html')

    used = get_capacity_used(res_date)
    if used + party_size > capacity:
        flash('Not enough capacity for that date and party size.', 'danger')
        return render_template('admin/employee_splash_time.html')

    code = generate_est_code()

    # Create a lightweight member record to link the reservation
    emp_member = Member(
        owner_number=code,
        last_name=last_name,
        first_name=first_name,
        enrollment_type=EST_MEMBERSHIP,
        membership=EST_MEMBERSHIP,
        active=True
    )
    db.session.add(emp_member)
    db.session.flush()  # get emp_member.id

    reservation = Reservation(
        confirmation_code=code,
        member_id=emp_member.id,
        reservation_date=res_date,
        party_size=party_size
    )
    db.session.add(reservation)
    db.session.commit()

    return render_template('admin/employee_splash_time.html',
                           confirmation=reservation)

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
        enroll_col = find_col([
            'enrollment_type', 'enrollmenttype', 'enrollment'
        ])
        email_col = find_col([
            'email', 'email_address', 'emailaddress', 'e_mail'
        ])

        if not owner_col:
            flash(f'Could not find owner number column. Headers found: {raw_headers}', 'danger')
            return redirect(url_for('admin_members'))
        if not first_col or not last_col:
            flash(f'Could not find name columns. Headers found: {raw_headers}', 'danger')
            return redirect(url_for('admin_members'))

        # Deactivate all existing members; members present in the CSV
        # will be reactivated (or created) below.  Reservations are
        # intentionally left untouched so upcoming bookings survive.
        # Employee members (created via Employee Splash Time) are excluded.
        Member.query.filter(Member.membership != EST_MEMBERSHIP).update({Member.active: False})

        count = 0
        skipped = 0
        no_tier = 0
        updated = 0

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

            enrollment_type = (row.get(enroll_col) or '').strip() if enroll_col else ''
            if not enrollment_type:
                enrollment_type = 'Individual'

            email = (row.get(email_col) or '').strip() if email_col else ''

            existing = Member.query.filter_by(owner_number=owner_number).first()
            if existing:
                existing.first_name = first_name
                existing.last_name = last_name
                existing.membership = membership
                existing.enrollment_type = enrollment_type
                existing.email = email if email else None
                existing.active = True
                updated += 1
            else:
                m = Member(
                    owner_number=owner_number,
                    last_name=last_name,
                    first_name=first_name,
                    membership=membership,
                    enrollment_type=enrollment_type,
                    email=email if email else None,
                    active=True
                )
                db.session.add(m)
            count += 1

        db.session.commit()

        new_count = count - updated
        msg = f'Loaded {count} members ({updated} updated, {new_count} new).'
        if skipped:
            msg += f' Skipped {skipped} rows (missing data).'
        if mem_col:
            msg += f' Membership column: "{mem_col}".'
        else:
            msg += ' ⚠️ No membership column found — all set to Silver.'
        if no_tier and mem_col:
            msg += f' {no_tier} rows had unrecognized tier (defaulted to Silver).'
        if email_col:
            msg += f' Email column: "{email_col}".'

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

    cal = cal_module.Calendar(firstweekday=6)
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

    last_year_count = DayType.query.filter(
        db.extract('year', DayType.date) == year - 1,
        db.extract('month', DayType.date) == month
    ).count()

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

@app.route('/admin/calendar/generate/<int:year>', methods=['POST'])
@admin_required
def admin_generate_calendar(year):
    try:
        full_path, web_path = generate_calendar_png(year)
        flash(f'Calendar generated for {year}.', 'success')
    except Exception as e:
        flash(f'Error generating calendar: {e}', 'danger')
    return redirect(url_for('admin_calendar'))

@app.route('/admin/calendar/download/<int:year>')
@admin_required
def admin_download_calendar(year):
    path = os.path.join('static', 'calendars', f'{year}_full.png')
    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name=f'HIC_Pool_Calendar_{year}.png')
    flash('Calendar not found. Generate it first.', 'danger')
    return redirect(url_for('admin_calendar'))

@app.route('/admin/calendar/copy-previous-year', methods=['POST'])
@admin_required
def admin_calendar_copy_previous_year():
    year = request.form.get('year', type=int)
    month = request.form.get('month', type=int)

    if not year or not month:
        flash('Invalid month/year.', 'danger')
        return redirect(url_for('admin_calendar'))

    source_year = year - 1

    cal = cal_module.Calendar(firstweekday=6)

    source_weeks = cal.monthdatescalendar(source_year, month)
    source_map = {}
    for wi, week in enumerate(source_weeks):
        for d in week:
            if d.month == month:
                dt = DayType.query.filter_by(date=d).first()
                if dt:
                    source_map[(wi, d.weekday())] = dt.day_type

    if not source_map:
        flash(f'No calendar data found for {date(source_year, month, 1).strftime("%B %Y")}.', 'warning')
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
    flash(f'Copied {applied} day types from {date(source_year, month, 1).strftime("%B %Y")} → '
          f'{date(year, month, 1).strftime("%B %Y")} (matched by week position & day of week).', 'success')
    return redirect(url_for('admin_calendar', year=year, month=month))

@app.route('/admin/calendar/copy-previous-month', methods=['POST'])
@admin_required
def admin_calendar_copy_previous_month():
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

    with db.engine.connect() as conn:
        migrations = [
            "ALTER TABLE member ADD COLUMN IF NOT EXISTS email VARCHAR(200)",
            "ALTER TABLE reservation ADD COLUMN IF NOT EXISTS arrived BOOLEAN DEFAULT FALSE",
            "ALTER TABLE member ADD COLUMN IF NOT EXISTS enrollment_type VARCHAR(20) NOT NULL DEFAULT 'Individual'",
            "ALTER TABLE reservation ALTER COLUMN confirmation_code TYPE VARCHAR(20)",
        ]
        for sql in migrations:
            try:
                conn.execute(db.text(sql))
            except Exception:
                pass
        conn.commit()


if __name__ == '__main__':
    app.run(debug=True)
