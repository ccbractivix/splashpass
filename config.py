import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme2025')
    TIMEZONE = 'US/Eastern'
    MAX_PARTY_SIZE = 6
    DEFAULT_CAPACITY = 128
    POOL_OPEN_HOUR = 7
    POOL_CLOSE_HOUR = 20

    database_url = os.environ.get('DATABASE_URL', '')
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = database_url or 'sqlite:///splashpass.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
