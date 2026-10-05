"""Additive SQLite migrations. Original photos and all result versions are immutable."""
import base64
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('STUDIO_DATA', ROOT / 'data'))
FILES = DATA / 'photos'
MODELS = DATA / 'models'
WORK = DATA / 'work'
for folder in (DATA, FILES, MODELS, WORK):
    folder.mkdir(parents=True, exist_ok=True)
_initialized = False

@contextmanager
def db():
    connection = sqlite3.connect(DATA / 'studio.db', timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        with connection:
            yield connection
    finally:
        connection.close()

def initialize():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY, name TEXT,
            client TEXT DEFAULT '', price REAL DEFAULT 0, status TEXT DEFAULT 'new',
            original TEXT, result TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)''')
        columns = {r['name'] for r in c.execute('PRAGMA table_info(photos)')}
        if 'order_id' not in columns:
            c.execute('ALTER TABLE photos ADD COLUMN order_id TEXT')
        c.execute('CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY,value TEXT)')
        c.execute('''CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, name TEXT,
            client TEXT DEFAULT '', total REAL DEFAULT 0, deposit REAL DEFAULT 0,
            due TEXT DEFAULT '', notes TEXT DEFAULT '', status TEXT DEFAULT 'new',
            created TEXT DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,photo_id TEXT,
            image TEXT,label TEXT,job_id TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(photo_id) REFERENCES photos(id) ON DELETE CASCADE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,photo_id TEXT,
            kind TEXT DEFAULT 'process', engine TEXT, preset TEXT, options TEXT,
            source TEXT, status TEXT DEFAULT 'queued', progress INTEGER DEFAULT 0,
            stage TEXT DEFAULT 'Ожидает обработки', error TEXT DEFAULT '', device TEXT,
            priority INTEGER DEFAULT 0, cancel INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
            created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,priority,created)')
        for p in c.execute('SELECT * FROM photos WHERE result IS NOT NULL').fetchall():
            if not c.execute('SELECT 1 FROM versions WHERE photo_id=? LIMIT 1', (p['id'],)).fetchone():
                c.execute('INSERT INTO versions(id,photo_id,image,label) VALUES(?,?,?,?)',
                          (uuid.uuid4().hex, p['id'], p['result'], 'Результат из версии 1.0'))

def amount(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('Стоимость должна быть неотрицательным числом')
    return round(value, 2)

def image_path(url):
    if not isinstance(url, str) or not url.startswith('/photos/'):
        raise ValueError('Неизвестный файл')
    path = (FILES / url[8:]).resolve()
    if not path.is_relative_to(FILES.resolve()) or not path.is_file():
        raise ValueError('Файл не найден')
    return path

def save_image(value, name=None, max_bytes=25*1024*1024):
    if not isinstance(value, str) or not re.match(r'^data:image/(jpeg|png|webp);base64,', value):
        raise ValueError('Поддерживаются JPG, PNG и WebP')
    header, raw = value.split(',', 1)
    content = base64.b64decode(raw, validate=True)
    if len(content) > max_bytes:
        raise ValueError('Изображение превышает допустимый размер')
    ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[header[5:].split(';')[0]]
    valid = content.startswith(b'\xff\xd8\xff') if ext == 'jpg' else content.startswith(b'\x89PNG\r\n\x1a\n') if ext == 'png' else content[:4] == b'RIFF' and content[8:12] == b'WEBP'
    if not valid:
        raise ValueError('Некорректный файл изображения')
    name = name or uuid.uuid4().hex
    path = FILES / f'{name}.{ext}'
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(content)
    temporary.replace(path)
    return '/photos/' + path.name

def settings(public=False):
    with db() as c:
        row = c.execute('SELECT value FROM settings WHERE id=1').fetchone()
    result = json.loads(row['value']) if row else {}
    result = {'endpoint': '', 'name': 'AI-обработчик', 'devices': ['auto'],
              'color_model': 'ddcolor', 'tile': 256, **result}
    if public:
        result['has_key'] = bool(result.pop('key', ''))
    return result

def add_version(c, photo_id, image, label='Ручная коррекция', job_id=None, active=True):
    ident = uuid.uuid4().hex
    c.execute('INSERT INTO versions(id,photo_id,image,label,job_id) VALUES(?,?,?,?,?)',
              (ident, photo_id, image, str(label)[:150], job_id))
    if active:
        c.execute("UPDATE photos SET result=?,status='ready' WHERE id=?", (image, photo_id))
        c.execute("UPDATE orders SET status='working' WHERE status='delivered' AND id=(SELECT order_id FROM photos WHERE id=?)",(photo_id,))
    return ident

initialize()
