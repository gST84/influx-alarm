import http.cookiejar
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from alarm import db, web  # noqa: E402


class MigrationTest(unittest.TestCase):
    def test_existing_data_moves_to_one_organisation(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        # Schema from before organisations existed.
        old.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created REAL);
            CREATE TABLE contacts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT DEFAULT '', phone TEXT DEFAULT '', notes TEXT DEFAULT '');
            CREATE TABLE rules (id INTEGER PRIMARY KEY, name TEXT NOT NULL, message TEXT DEFAULT '', paused INTEGER DEFAULT 0,
              quiet_enabled INTEGER DEFAULT 0, quiet_start TEXT DEFAULT '22:00', quiet_end TEXT DEFAULT '07:00',
              ignore_after_hours REAL DEFAULT 0, ring_seconds INTEGER DEFAULT 30, cascade_wait_min REAL DEFAULT 5,
              last_notified REAL, created REAL, updated REAL);
            CREATE TABLE log (id INTEGER PRIMARY KEY, ts REAL NOT NULL, rule_id INTEGER, alarm_ids TEXT DEFAULT '',
              contact_id INTEGER, channel TEXT, kind TEXT, status TEXT, detail TEXT DEFAULT '');
            INSERT INTO users (username, pw_hash) VALUES ('admin', 'x');
            INSERT INTO contacts (name) VALUES ('Jan');
            INSERT INTO rules (name) VALUES ('Koeling');
            INSERT INTO log (ts, rule_id, channel, kind, status) VALUES (1, 1, 'mail', 'alarm', 'ok');
        """)
        old.close()
        with mock.patch.object(db, "DB_PATH", path), mock.patch.object(db, "_local", threading.local()):
            db.init()
            db.init()  # running twice must be harmless
            self.assertEqual([o["name"] for o in db.q("SELECT name FROM organizations")], [db.DEFAULT_ORG])
            org = db.one("SELECT id FROM organizations")["id"]
            self.assertEqual(db.one("SELECT org_id FROM contacts")["org_id"], org)
            self.assertEqual(db.one("SELECT org_id FROM rules")["org_id"], org)
            self.assertEqual(db.one("SELECT org_id FROM log")["org_id"], org)
            self.assertEqual(db.one("SELECT is_admin FROM users")["is_admin"], 1)


class AccessTest(unittest.TestCase):
    """Organisations are separated through the real HTTP API."""

    @classmethod
    def setUpClass(cls):
        db.init()
        for t in ("users", "sessions", "organizations", "user_orgs", "contacts", "rules", "rule_contacts", "checks",
                  "alarms", "log"):
            db.run(f"DELETE FROM {t}")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api/"
        cls.patch = mock.patch("alarm.checker.run_cycle", lambda **kw: None)  # no InfluxDB in tests
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.server.shutdown()

    def client(self):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def call(method, path, body=None, org=None):
            headers = {"Content-Type": "application/json"}
            if org:
                headers["X-Org"] = str(org)
            req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with opener.open(req) as resp:
                    return resp.status, json.load(resp)
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.load(e)
        return call

    def test_flow(self):
        admin = self.client()
        self.assertEqual(admin("POST", "setup", {"username": "admin", "password": "geheim123", "org": "Bedrijf A"})[0], 200)
        status, me = admin("GET", "me")
        self.assertTrue(me["is_admin"])
        org_a = me["orgs"][0]["id"]
        org_b = admin("POST", "orgs", {"name": "Bedrijf B"})[1]["id"]
        self.assertEqual(admin("POST", "orgs", {"name": "bedrijf b"})[0], 400)  # names are unique

        jan = admin("POST", "contacts", {"name": "Jan", "email": "jan@a.be"}, org=org_a)[1]
        admin("POST", "contacts", {"name": "Piet", "email": "piet@b.be"}, org=org_b)
        self.assertEqual([c["name"] for c in admin("GET", "contacts", org=org_a)[1]], ["Jan"])
        self.assertEqual([c["name"] for c in admin("GET", "contacts", org=org_b)[1]], ["Piet"])

        # A rule in B cannot use a contact of A.
        status, err = admin("POST", "rules", {"name": "R", "recipients": [{"contact_id": jan["id"], "email": 1}]}, org=org_b)
        self.assertEqual(status, 400)
        rule_a = admin("POST", "rules", {"name": "Koeling A", "recipients": [{"contact_id": jan["id"], "email": 1}]}, org=org_a)[1]
        self.assertEqual(admin("GET", f"rules/{rule_a['id']}", org=org_b)[0], 404)

        # A user linked to B only.
        self.assertEqual(admin("POST", "users", {"username": "bert", "password": "geheim123", "orgs": [org_b]})[0], 200)
        bert = self.client()
        self.assertEqual(bert("POST", "login", {"username": "bert", "password": "geheim123"})[0], 200)
        me = bert("GET", "me")[1]
        self.assertFalse(me["is_admin"])
        self.assertEqual([o["name"] for o in me["orgs"]], ["Bedrijf B"])
        self.assertEqual(bert("GET", "rules", org=org_a)[0], 403)
        self.assertEqual(bert("GET", f"rules/{rule_a['id']}", org=org_b)[0], 404)
        self.assertEqual(bert("DELETE", f"contacts/{jan['id']}", org=org_b)[0], 404)
        self.assertEqual(bert("GET", "settings")[0], 403)
        self.assertEqual(bert("GET", "users")[0], 403)
        self.assertEqual(bert("POST", "orgs", {"name": "C"})[0], 403)
        self.assertEqual(bert("GET", "contacts", org=org_b)[0], 200)

        # Own password: needs the current one.
        self.assertEqual(bert("PUT", "me/password", {"current": "fout", "password": "nieuw12345"})[0], 400)
        self.assertEqual(bert("PUT", "me/password", {"current": "geheim123", "password": "nieuw12345"})[0], 200)

        # An organisation with rules or contacts cannot be removed.
        self.assertEqual(admin("DELETE", f"orgs/{org_a}")[0], 400)
        empty = admin("POST", "orgs", {"name": "Leeg"})[1]["id"]
        self.assertEqual(admin("DELETE", f"orgs/{empty}")[0], 200)


if __name__ == "__main__":
    unittest.main()
