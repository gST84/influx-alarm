"""SQLite storage. Times are stored as epoch seconds (REAL)."""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/alarm.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT DEFAULT '', phone TEXT DEFAULT '', notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  message TEXT DEFAULT '',
  paused INTEGER DEFAULT 0,
  quiet_enabled INTEGER DEFAULT 0,
  quiet_start TEXT DEFAULT '22:00',
  quiet_end TEXT DEFAULT '07:00',
  ignore_after_hours REAL DEFAULT 0,
  ring_seconds INTEGER DEFAULT 30,
  cascade_wait_min REAL DEFAULT 5,
  last_notified REAL,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS rule_contacts (
  rule_id INTEGER NOT NULL, contact_id INTEGER NOT NULL,
  email INTEGER DEFAULT 0, sms INTEGER DEFAULT 0, call INTEGER DEFAULT 0, position INTEGER DEFAULT 0,
  PRIMARY KEY (rule_id, contact_id));
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY,
  rule_id INTEGER NOT NULL,
  bucket TEXT NOT NULL, measurement TEXT NOT NULL, field TEXT NOT NULL, tags TEXT DEFAULT '{}',
  label TEXT DEFAULT '', kind TEXT DEFAULT 'number',
  lower REAL, upper REAL, delay_min REAL DEFAULT 0, nodata_min REAL DEFAULT 60,
  paused INTEGER DEFAULT 0, position INTEGER DEFAULT 0,
  last_value TEXT, last_time REAL, checked_at REAL, status TEXT DEFAULT 'unknown', reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY, rule_id INTEGER NOT NULL, check_id INTEGER NOT NULL,
  kind TEXT NOT NULL, reason TEXT, value TEXT,
  started REAL NOT NULL, resolved REAL, ignored REAL, acked REAL, acked_by TEXT, notified INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS alarms_open ON alarms(rule_id, resolved);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, rule_id INTEGER, alarm_ids TEXT DEFAULT '',
  contact_id INTEGER, channel TEXT, kind TEXT, status TEXT, detail TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS log_rule ON log(rule_id, ts);
CREATE TABLE IF NOT EXISTS pending_calls (
  id INTEGER PRIMARY KEY, rule_id INTEGER NOT NULL, due REAL NOT NULL, kind TEXT NOT NULL, alarm_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organizations (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS user_orgs (user_id INTEGER NOT NULL, org_id INTEGER NOT NULL, PRIMARY KEY (user_id, org_id));
"""

DEFAULT_ORG = "Mijn organisatie"

_local = threading.local()
_write_lock = threading.RLock()


def conn():
    """One connection per thread."""
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init():
    with _write_lock:
        conn().executescript(SCHEMA)
        migrate()


def _columns(table):
    return {r[1] for r in conn().execute(f"PRAGMA table_info({table})")}


def migrate():
    """Adds organisations to databases from before they existed; existing data goes to one organisation."""
    with transaction():
        if "is_admin" not in _columns("users"):
            conn().execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
            conn().execute("UPDATE users SET is_admin = 1")  # users from before roles existed manage everything
        for table in ("contacts", "rules", "log"):
            if "org_id" not in _columns(table):
                conn().execute(f"ALTER TABLE {table} ADD COLUMN org_id INTEGER")
        conn().execute("CREATE INDEX IF NOT EXISTS contacts_org ON contacts(org_id)")
        conn().execute("CREATE INDEX IF NOT EXISTS rules_org ON rules(org_id)")
        conn().execute("CREATE INDEX IF NOT EXISTS log_org ON log(org_id, ts)")
        orphans = any(conn().execute(f"SELECT 1 FROM {t} WHERE org_id IS NULL LIMIT 1").fetchone()
                      for t in ("contacts", "rules"))
        if orphans:
            row = conn().execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
            org_id = row[0] if row else conn().execute(
                "INSERT INTO organizations (name, created) VALUES (?, ?)", (DEFAULT_ORG, time.time())).lastrowid
            for t in ("contacts", "rules"):
                conn().execute(f"UPDATE {t} SET org_id = ? WHERE org_id IS NULL", (org_id,))
            conn().execute("UPDATE log SET org_id = COALESCE((SELECT org_id FROM rules WHERE rules.id = log.rule_id), ?) "
                           "WHERE org_id IS NULL", (org_id,))


def q(sql, *args):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, *args):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql, *args):
    with _write_lock:
        return conn().execute(sql, args).lastrowid


class transaction:
    """Serialised write transaction: `with db.transaction(): db.run(...)`."""

    def __enter__(self):
        _write_lock.acquire()
        conn().execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, *_):
        try:
            conn().execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            _write_lock.release()


def now():
    return time.time()


def log(rule_id, channel, kind, status, detail="", contact_id=None, alarm_ids=()):
    ids = "," + ",".join(str(i) for i in alarm_ids) + "," if alarm_ids else ""
    run("INSERT INTO log (ts, rule_id, alarm_ids, contact_id, channel, kind, status, detail, org_id) "
        "VALUES (?,?,?,?,?,?,?,?,(SELECT org_id FROM rules WHERE id = ?))",
        now(), rule_id, ids, contact_id, channel, kind, status, detail[:2000], rule_id)


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default
