from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class Admin(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def get_id(self):
        return str(self.id)


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_number = db.Column(db.String(50), unique=True, nullable=False, index=True)
    last_name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    enrollment_type = db.Column(db.String(20), nullable=False)
    expiration_date = db.Column(db.String(50), nullable=True)
    membership = db.Column(db.String(100), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reservations = db.relationship('Reservation', backref='member', lazy=True)


class DayType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False, index=True)
    day_type = db.Column(db.String(20), nullable=False)
    capacity_override = db.Column(db.Integer, nullable=True)

    @staticmethod
    def get_type_for_date(target_date):
        entry = DayType.query.filter_by(date=target_date).first()
        if entry:
            return entry.day_type
        if target_date.weekday() < 5:
            return 'Weekday'
        return 'Weekend'

    @staticmethod
    def get_capacity_for_date(target_date):
        from flask import current_app
        entry = DayType.query.filter_by(date=target_date).first()
        if entry and entry.capacity_override:
            return entry.capacity_override
        return current_app.config['DEFAULT_CAPACITY']


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    confirmation_code = db.Column(db.String(8), unique=True, nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    reservation_date = db.Column(db.Date, nullable=False, index=True)
    party_size = db.Column(db.Integer, nullable=False)
    arrived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('member_id', 'reservation_date', name='one_per_member_per_day'),
    )
