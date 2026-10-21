"""HTTP server: static UI + JSON API with session login."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import checker, db, influx, messages, notify, settings

log = logging.getLogger("web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_DAYS = 30
COOKIE = "alarm_session"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# --- auth ------------------------------------------------------------------------

def hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw, stored):
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h)
    except ValueError:
        return False


def _validate_password(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        raise ApiError(400, "Wachtwoord moet minstens 8 tekens hebben")


# --- routes ------------------------------------------------------------------------

ROUTES = []


def route(method, pattern, public=False, admin=False, org=False):
    """public: no login needed; admin: only for administrators; org: needs a selected organisation."""
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, public, admin, org))
        return fn
    return deco


def _int(v, name):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"Ongeldige waarde voor {name}") from None


def _num_or_none(v, name):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise ApiError(400, f"Ongeldig getal voor {name}: {v}") from None


def _range(q):
    now = time.time()
    try:
        start = float(q.get("start") or now - 7 * 86400)
        stop = float(q.get("stop") or now)
    except ValueError:
        raise ApiError(400, "Ongeldige periode") from None
    return start, stop


# --- organisations & access ----------------------------------------------------------

def user_orgs(user):
    """Organisations a user may open: all for administrators, otherwise the linked ones."""
    if user["is_admin"]:
        return db.q("SELECT id, name FROM organizations ORDER BY name COLLATE NOCASE")
    return db.q("SELECT o.id, o.name FROM organizations o JOIN user_orgs uo ON uo.org_id = o.id "
                "WHERE uo.user_id = ? ORDER BY o.name COLLATE NOCASE", user["id"])


def _own_rule(org, rid):
    rule = db.one("SELECT * FROM rules WHERE id = ? AND org_id = ?", int(rid), org)
    if not rule:
        raise ApiError(404, "Regel niet gevonden in deze organisatie")
    return rule


def _own_contact(org, cid):
    contact = db.one("SELECT * FROM contacts WHERE id = ? AND org_id = ?", int(cid), org)
    if not contact:
        raise ApiError(404, "Contact niet gevonden in deze organisatie")
    return contact


@route("GET", "/api/me", public=True)
def api_me(req, user, **_):
    has_users = bool(db.one("SELECT 1 AS x FROM users LIMIT 1"))
    if not user:
        return {"user": None, "setup": not has_users}
    return {"user": user["username"], "id": user["id"], "is_admin": bool(user["is_admin"]), "orgs": user_orgs(user),
            "setup": False}


@route("POST", "/api/setup", public=True)
def api_setup(req, user, body, **_):
    if db.one("SELECT 1 AS x FROM users LIMIT 1"):
        raise ApiError(403, "Er bestaat al een gebruiker")
    name = (body.get("username") or "").strip()
    if not name:
        raise ApiError(400, "Gebruikersnaam is verplicht")
    _validate_password(body.get("password"))
    org_name = (body.get("org") or "").strip() or db.DEFAULT_ORG
    with db.transaction():
        uid = db.run("INSERT INTO users (username, pw_hash, created, is_admin) VALUES (?,?,?,1)",
                     name, hash_password(body["password"]), time.time())
        if not db.one("SELECT 1 AS x FROM organizations LIMIT 1"):
            db.run("INSERT INTO organizations (name, created) VALUES (?,?)", org_name, time.time())
    return req.start_session(uid, {"user": name})


@route("POST", "/api/login", public=True)
def api_login(req, user, body, **_):
    u = db.one("SELECT * FROM users WHERE username = ?", (body.get("username") or "").strip())
    if not u or not check_password(body.get("password") or "", u["pw_hash"]):
        time.sleep(1)
        raise ApiError(401, "Onjuiste gebruikersnaam of wachtwoord")
    return req.start_session(u["id"], {"user": u["username"]})


@route("POST", "/api/logout")
def api_logout(req, user, **_):
    db.run("DELETE FROM sessions WHERE token = ?", req.session_token)
    req.extra_headers.append(("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"))
    return {"ok": True}


@route("PUT", "/api/me/password")
def api_my_password(req, user, body, **_):
    u = db.one("SELECT pw_hash FROM users WHERE id = ?", user["id"])
    if not check_password(body.get("current") or "", u["pw_hash"]):
        raise ApiError(400, "Huidig wachtwoord klopt niet")
    _validate_password(body.get("password"))
    db.run("UPDATE users SET pw_hash = ? WHERE id = ?", hash_password(body["password"]), user["id"])
    return {"ok": True}


# --- organisations (administrators) ---------------------------------------------------

@route("GET", "/api/orgs", admin=True)
def api_orgs(req, user, **_):
    rows = db.q("SELECT * FROM organizations ORDER BY name COLLATE NOCASE")
    for o in rows:
        o["rules"] = db.one("SELECT COUNT(*) AS n FROM rules WHERE org_id = ?", o["id"])["n"]
        o["contacts"] = db.one("SELECT COUNT(*) AS n FROM contacts WHERE org_id = ?", o["id"])["n"]
        o["users"] = [r["username"] for r in db.q(
            "SELECT u.username FROM users u JOIN user_orgs uo ON uo.user_id = u.id WHERE uo.org_id = ? "
            "ORDER BY u.username", o["id"])]
    return rows


def _org_name(body, exclude=None):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Naam van de organisatie is verplicht")
    other = db.one("SELECT id FROM organizations WHERE name = ? COLLATE NOCASE", name)
    if other and other["id"] != exclude:
        raise ApiError(400, "Die organisatie bestaat al")
    return name


@route("POST", "/api/orgs", admin=True)
def api_org_add(req, user, body, **_):
    oid = db.run("INSERT INTO organizations (name, created) VALUES (?,?)", _org_name(body), time.time())
    return db.one("SELECT * FROM organizations WHERE id = ?", oid)


@route("PUT", r"/api/orgs/(?P<oid>\d+)", admin=True)
def api_org_save(req, user, body, oid, **_):
    db.run("UPDATE organizations SET name = ? WHERE id = ?", _org_name(body, int(oid)), int(oid))
    return db.one("SELECT * FROM organizations WHERE id = ?", int(oid))


@route("DELETE", r"/api/orgs/(?P<oid>\d+)", admin=True)
def api_org_delete(req, user, oid, **_):
    oid = int(oid)
    if db.one("SELECT 1 AS x FROM rules WHERE org_id = ? UNION SELECT 1 FROM contacts WHERE org_id = ?", oid, oid):
        raise ApiError(400, "Verwijder eerst de regels en contactpersonen van deze organisatie")
    with db.transaction():
        db.run("DELETE FROM user_orgs WHERE org_id = ?", oid)
        db.run("DELETE FROM log WHERE org_id = ?", oid)
        db.run("DELETE FROM organizations WHERE id = ?", oid)
    return {"ok": True}


# --- users (administrators) -------------------------------------------------------------

def _user_access(body):
    is_admin = 1 if body.get("is_admin") else 0
    orgs = [_int(o, "organisatie") for o in body.get("orgs") or []]
    known = {o["id"] for o in db.q("SELECT id FROM organizations")}
    if any(o not in known for o in orgs):
        raise ApiError(400, "Onbekende organisatie")
    return is_admin, orgs


def _set_user_orgs(uid, orgs):
    db.run("DELETE FROM user_orgs WHERE user_id = ?", uid)
    for o in orgs:
        db.run("INSERT INTO user_orgs (user_id, org_id) VALUES (?,?)", uid, o)


@route("GET", "/api/users", admin=True)
def api_users(req, user, **_):
    rows = db.q("SELECT id, username, created, is_admin FROM users ORDER BY username COLLATE NOCASE")
    for u in rows:
        u["orgs"] = [r["org_id"] for r in db.q("SELECT org_id FROM user_orgs WHERE user_id = ?", u["id"])]
    return rows


@route("POST", "/api/users", admin=True)
def api_user_add(req, user, body, **_):
    name = (body.get("username") or "").strip()
    if not name:
        raise ApiError(400, "Gebruikersnaam is verplicht")
    _validate_password(body.get("password"))
    if db.one("SELECT 1 AS x FROM users WHERE username = ?", name):
        raise ApiError(400, "Die gebruikersnaam bestaat al")
    is_admin, orgs = _user_access(body)
    with db.transaction():
        uid = db.run("INSERT INTO users (username, pw_hash, created, is_admin) VALUES (?,?,?,?)",
                     name, hash_password(body["password"]), time.time(), is_admin)
        _set_user_orgs(uid, orgs)
    return {"ok": True}


@route("PUT", r"/api/users/(?P<uid>\d+)", admin=True)
def api_user_save(req, user, body, uid, **_):
    uid = int(uid)
    is_admin, orgs = _user_access(body)
    if uid == user["id"] and not is_admin:
        raise ApiError(400, "Je kunt je eigen beheerdersrechten niet afnemen")
    with db.transaction():
        db.run("UPDATE users SET is_admin = ? WHERE id = ?", is_admin, uid)
        _set_user_orgs(uid, orgs)
    return {"ok": True}


@route("PUT", r"/api/users/(?P<uid>\d+)/password", admin=True)
def api_user_password(req, user, body, uid, **_):
    _validate_password(body.get("password"))
    db.run("UPDATE users SET pw_hash = ? WHERE id = ?", hash_password(body["password"]), int(uid))
    return {"ok": True}


@route("DELETE", r"/api/users/(?P<uid>\d+)", admin=True)
def api_user_delete(req, user, uid, **_):
    if int(uid) == user["id"]:
        raise ApiError(400, "Je kunt jezelf niet verwijderen")
    with db.transaction():
        db.run("DELETE FROM sessions WHERE user_id = ?", int(uid))
        db.run("DELETE FROM user_orgs WHERE user_id = ?", int(uid))
        db.run("DELETE FROM users WHERE id = ?", int(uid))
    return {"ok": True}


# --- settings ---------------------------------------------------------------------

@route("GET", "/api/settings", admin=True)
def api_settings(req, user, **_):
    return settings.public()


@route("PUT", "/api/settings", admin=True)
def api_settings_save(req, user, body, **_):
    settings.update(body)
    checker.wake()
    return settings.public()


@route("POST", "/api/settings/test", admin=True)
def api_settings_test(req, user, body, **_):
    cfg = settings.get_all()
    what = body.get("what")
    if what == "influx":
        names = [b["name"] for b in influx.buckets(cfg)]
        return {"message": f"Verbonden met InfluxDB: {len(names)} buckets"}
    if what == "asterisk":
        from .channels import ami
        with ami.Ami(cfg["ami_host"], cfg["ami_port"], cfg["ami_user"], cfg["ami_password"]) as a:
            res = a.request([("Action", "Command"), ("Command", "pjsip show registrations")])
        reg = "geen SIP-provider ingesteld (alleen testnummers)" if not cfg["sip_configured"] else (
            "geregistreerd bij provider" if "Registered" in json.dumps(res) else "NIET geregistreerd bij provider")
        return {"message": f"Asterisk bereikbaar; {reg}"}
    contact = {"id": 0, "name": "test", "email": body.get("to", ""), "phone": body.get("to", "")}
    return {"message": notify.test_contact(cfg, contact, what)}


@route("GET", "/api/public/status", public=True)
def api_public_status(req, user, **_):
    """Read-only status for the open /status page: no contacts, tags or settings."""
    if not settings.get_all().get("public_status"):
        raise ApiError(404, "De statuspagina staat uit")
    now = time.time()
    orgs = []
    for o in db.q("SELECT id, name FROM organizations ORDER BY name COLLATE NOCASE"):
        rules = []
        for r in db.q("SELECT * FROM rules WHERE org_id = ? ORDER BY name COLLATE NOCASE", o["id"]):
            _rule_summary(r)
            checks = {c["id"]: c for c in checker.load_checks(r["id"])}

            def item(a):
                c = checks.get(a["check_id"])
                return {"label": messages.label_of(c) if c else "verwijderde waarde", "reason": a["reason"],
                        "kind": a["kind"], "started": a["started"], "resolved": a["resolved"],
                        "acked": bool(a["acked"]), "ignored": bool(a["ignored"])}

            open_ = [item(a) for a in db.q("SELECT * FROM alarms WHERE rule_id = ? AND resolved IS NULL ORDER BY started",
                                           r["id"])]
            resolved = [item(a) for a in db.q(
                "SELECT * FROM alarms WHERE rule_id = ? AND resolved > ? AND reason NOT LIKE '%(gepauzeerd)%' "
                "AND reason NOT LIKE '%(controle verwijderd)%' ORDER BY resolved DESC LIMIT 10", r["id"], now - 86400)]
            rules.append({"name": r["name"], "state": r["state"], "checks": r["counts"]["checks"],
                          "checked_at": r["checked_at"], "alarms": [a for a in open_ if not a["ignored"]],
                          "ignored": [a for a in open_ if a["ignored"]], "resolved": resolved})
        order = {"alarm": 0, "error": 1, "pending": 2, "ok": 3, "empty": 4, "paused": 5}
        rules.sort(key=lambda r: (order[r["state"]], r["name"].lower()))
        orgs.append({"name": o["name"], "rules": rules})
    return {"now": now, "last_run": checker.state["last_run"],
            "interval": settings.get_all().get("check_interval") or 60, "orgs": orgs}


@route("GET", "/api/status")
def api_status(req, user, **_):
    return {"last_run": checker.state["last_run"], "last_error": checker.state["last_error"], "now": time.time()}


# --- InfluxDB browsing ---------------------------------------------------------------

@route("GET", "/api/influx/buckets")
def api_buckets(req, user, **_):
    return influx.buckets(settings.get_all())


@route("GET", "/api/influx/measurements")
def api_measurements(req, user, q, **_):
    start, stop = _range(q)
    return influx.measurements(settings.get_all(), q.get("bucket", ""), start, stop)


@route("GET", "/api/influx/schema")
def api_schema(req, user, q, **_):
    start, stop = _range(q)
    return influx.schema(settings.get_all(), q.get("bucket", ""), q.get("measurement", ""), start, stop)


@route("GET", "/api/influx/tagvalues")
def api_tagvalues(req, user, q, **_):
    start, stop = _range(q)
    return influx.tag_values(settings.get_all(), q.get("bucket", ""), q.get("measurement", ""), q.get("tag", ""), start, stop)


@route("POST", "/api/influx/series")
def api_series(req, user, body, **_):
    start, stop = _range(body)
    series = influx.resolve_series(settings.get_all(), body.get("bucket", ""), body.get("selections") or [], start, stop)
    for s in series:
        s["unit"] = messages.unit_of(s)
        s["label"] = messages.label_of({**s, "label": ""})
    return series


# --- contacts ------------------------------------------------------------------------

def _contact_fields(body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Naam is verplicht")
    email = (body.get("email") or "").strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ApiError(400, f"Ongeldig mailadres: {email}")
    phone = (body.get("phone") or "").strip()
    if phone and not phone.lower().startswith("test:") and not re.match(r"^\+?[\d\s./()-]{6,}$", phone):
        raise ApiError(400, f"Ongeldig telefoonnummer: {phone}")
    return name, email, phone, (body.get("notes") or "").strip()


@route("GET", "/api/contacts", org=True)
def api_contacts(req, user, org, **_):
    rows = db.q("SELECT * FROM contacts WHERE org_id = ? ORDER BY name COLLATE NOCASE", org)
    used = {r["contact_id"]: r["n"] for r in db.q("SELECT contact_id, COUNT(*) AS n FROM rule_contacts GROUP BY contact_id")}
    for r in rows:
        r["rules"] = used.get(r["id"], 0)
    return rows


@route("POST", "/api/contacts", org=True)
def api_contact_add(req, user, body, org, **_):
    cid = db.run("INSERT INTO contacts (name, email, phone, notes, org_id) VALUES (?,?,?,?,?)", *_contact_fields(body), org)
    return db.one("SELECT * FROM contacts WHERE id = ?", cid)


@route("PUT", r"/api/contacts/(?P<cid>\d+)", org=True)
def api_contact_save(req, user, body, cid, org, **_):
    _own_contact(org, cid)
    db.run("UPDATE contacts SET name = ?, email = ?, phone = ?, notes = ? WHERE id = ?", *_contact_fields(body), int(cid))
    return db.one("SELECT * FROM contacts WHERE id = ?", int(cid))


@route("DELETE", r"/api/contacts/(?P<cid>\d+)", org=True)
def api_contact_delete(req, user, cid, org, **_):
    _own_contact(org, cid)
    with db.transaction():
        db.run("DELETE FROM rule_contacts WHERE contact_id = ?", int(cid))
        db.run("DELETE FROM contacts WHERE id = ?", int(cid))
    return {"ok": True}


@route("POST", r"/api/contacts/(?P<cid>\d+)/test", org=True)
def api_contact_test(req, user, body, cid, org, **_):
    contact = _own_contact(org, cid)
    return {"message": notify.test_contact(settings.get_all(), contact, body.get("channel"))}


# --- rules ---------------------------------------------------------------------------

def _rule_summary(rule):
    checks = db.q("SELECT status, paused FROM checks WHERE rule_id = ?", rule["id"])
    alarms = db.q("SELECT kind, ignored, acked FROM alarms WHERE rule_id = ? AND resolved IS NULL", rule["id"])
    active = [a for a in alarms if not a["ignored"]]
    rule["counts"] = {
        "checks": len(checks),
        "paused": sum(1 for c in checks if c["paused"]),
        "alarm": len(active),
        "ignored": len(alarms) - len(active),
        "acked": sum(1 for a in active if a["acked"]),
        "pending": sum(1 for c in checks if c["status"] == "pending"),
        "error": sum(1 for c in checks if c["status"] == "error"),
    }
    rule["contacts"] = db.one("SELECT COUNT(*) AS n FROM rule_contacts WHERE rule_id = ?", rule["id"])["n"]
    if rule["paused"]:
        rule["state"] = "paused"
    elif active:
        rule["state"] = "alarm"
    elif rule["counts"]["error"]:
        rule["state"] = "error"
    elif rule["counts"]["pending"]:
        rule["state"] = "pending"
    elif not checks:
        rule["state"] = "empty"
    else:
        rule["state"] = "ok"
    rule["alarm_since"] = min((a["started"] for a in db.q(
        "SELECT started FROM alarms WHERE rule_id = ? AND resolved IS NULL AND ignored IS NULL", rule["id"])), default=None)
    rule["checked_at"] = (db.one("SELECT MAX(checked_at) AS t FROM checks WHERE rule_id = ?", rule["id"]) or {}).get("t")
    return rule


@route("GET", "/api/rules", org=True)
def api_rules(req, user, org, **_):
    rules = [_rule_summary(r) for r in db.q("SELECT * FROM rules WHERE org_id = ? ORDER BY name COLLATE NOCASE", org)]
    order = {"alarm": 0, "error": 1, "pending": 2, "ok": 3, "empty": 4, "paused": 5}
    return sorted(rules, key=lambda r: (order[r["state"]], r["name"].lower()))


def _rule_detail(rid):
    rule = db.one("SELECT * FROM rules WHERE id = ?", rid)
    if not rule:
        raise ApiError(404, "Regel niet gevonden")
    _rule_summary(rule)
    rule["recipients"] = db.q("SELECT contact_id, email, sms, call, position FROM rule_contacts WHERE rule_id = ? "
                              "ORDER BY position", rid)
    checks = checker.load_checks(rid)
    for c in checks:
        c["last_value"] = db.jload(c["last_value"])
        c["unit"] = messages.unit_of(c)
        c["auto_label"] = messages.label_of({**c, "label": ""})
    rule["checks"] = checks
    rule["alarms"] = db.q("SELECT * FROM alarms WHERE rule_id = ? AND (resolved IS NULL OR resolved > ?) ORDER BY started DESC",
                          rid, time.time() - 7 * 86400)
    rule["log"] = db.q("SELECT l.*, c.name AS contact FROM log l LEFT JOIN contacts c ON c.id = l.contact_id "
                       "WHERE l.rule_id = ? ORDER BY l.ts DESC LIMIT 100", rid)
    rule["cascade_running"] = notify.cascade_running(rid)
    rule["pending_calls"] = db.q("SELECT due FROM pending_calls WHERE rule_id = ?", rid)
    return rule


@route("GET", r"/api/rules/(?P<rid>\d+)", org=True)
def api_rule(req, user, rid, org, **_):
    _own_rule(org, rid)
    return _rule_detail(int(rid))


def _hhmm(v, name):
    v = (v or "").strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
        raise ApiError(400, f"Ongeldig tijdstip voor {name} (uu:mm)")
    return v


def _save_rule(org, rid, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Naam van de regel is verplicht")
    fields = (
        name,
        (body.get("message") or "").strip(),
        1 if body.get("paused") else 0,
        1 if body.get("quiet_enabled") else 0,
        _hhmm(body.get("quiet_start") or "22:00", "begin stille uren"),
        _hhmm(body.get("quiet_end") or "07:00", "einde stille uren"),
        _num_or_none(body.get("ignore_after_hours"), "negeren na") or 0,
        max(10, min(120, _int(body.get("ring_seconds") or 30, "beltijd"))),
        max(0, _num_or_none(body.get("cascade_wait_min"), "wachttijd") or 5),
    )
    checks = body.get("checks") or []
    own = {c["id"] for c in db.q("SELECT id FROM contacts WHERE org_id = ?", org)}
    for r in body.get("recipients") or []:
        if _int(r.get("contact_id"), "contact") not in own:
            raise ApiError(400, "Contactpersoon hoort niet bij deze organisatie")
    for c in checks:
        lower = _num_or_none(c.get("lower"), "ondergrens")
        upper = _num_or_none(c.get("upper"), "bovengrens")
        if lower is not None and upper is not None and lower >= upper:
            raise ApiError(400, f"Ondergrens moet kleiner zijn dan bovengrens ({c.get('label') or c.get('field')})")
    with db.transaction():
        now = time.time()
        if rid is None:
            rid = db.run("INSERT INTO rules (name, message, paused, quiet_enabled, quiet_start, quiet_end, ignore_after_hours, "
                         "ring_seconds, cascade_wait_min, created, updated, org_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         *fields, now, now, org)
        else:
            db.run("UPDATE rules SET name=?, message=?, paused=?, quiet_enabled=?, quiet_start=?, quiet_end=?, "
                   "ignore_after_hours=?, ring_seconds=?, cascade_wait_min=?, updated=? WHERE id=?", *fields, now, rid)
        db.run("DELETE FROM rule_contacts WHERE rule_id = ?", rid)
        for i, r in enumerate(body.get("recipients") or []):
            if r.get("email") or r.get("sms") or r.get("call"):
                db.run("INSERT INTO rule_contacts (rule_id, contact_id, email, sms, call, position) VALUES (?,?,?,?,?,?)",
                       rid, _int(r.get("contact_id"), "contact"), int(bool(r.get("email"))), int(bool(r.get("sms"))),
                       int(bool(r.get("call"))), i)
        keep = []
        for pos, c in enumerate(checks):
            vals = (c.get("label") or "", _num_or_none(c.get("lower"), "ondergrens"), _num_or_none(c.get("upper"), "bovengrens"),
                    max(0, _num_or_none(c.get("delay_min"), "vertraging") or 0),
                    max(0, _num_or_none(c.get("nodata_min"), "geen-data-tijd") or 0),
                    1 if c.get("paused") else 0, pos)
            if c.get("id"):
                db.run("UPDATE checks SET label=?, lower=?, upper=?, delay_min=?, nodata_min=?, paused=?, position=? "
                       "WHERE id=? AND rule_id=?", *vals, int(c["id"]), rid)
                keep.append(int(c["id"]))
            else:
                keep.append(db.run(
                    "INSERT INTO checks (rule_id, bucket, measurement, field, tags, kind, label, lower, upper, delay_min, "
                    "nodata_min, paused, position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rid, c["bucket"], c["measurement"], c["field"], json.dumps(c.get("tags") or {}, sort_keys=True),
                    c.get("kind") or "number", *vals))
        marks = ",".join("?" * len(keep)) or "NULL"
        for gone in db.q(f"SELECT id FROM checks WHERE rule_id = ? AND id NOT IN ({marks})", rid, *keep):
            db.run("DELETE FROM checks WHERE id = ?", gone["id"])
            db.run("UPDATE alarms SET resolved = ? WHERE check_id = ? AND resolved IS NULL", now, gone["id"])
    checker.run_cycle(rule_id=rid)
    return _rule_detail(rid)


@route("POST", "/api/rules", org=True)
def api_rule_add(req, user, body, org, **_):
    return _save_rule(org, None, body)


@route("PUT", r"/api/rules/(?P<rid>\d+)", org=True)
def api_rule_save(req, user, body, rid, org, **_):
    _own_rule(org, rid)
    return _save_rule(org, int(rid), body)


@route("DELETE", r"/api/rules/(?P<rid>\d+)", org=True)
def api_rule_delete(req, user, rid, org, **_):
    rid = _own_rule(org, rid)["id"]
    with db.transaction():
        for table in ("rule_contacts", "checks", "alarms", "pending_calls"):
            db.run(f"DELETE FROM {table} WHERE rule_id = ?", rid)
        db.run("DELETE FROM rules WHERE id = ?", rid)
    return {"ok": True}


@route("POST", r"/api/rules/(?P<rid>\d+)/pause", org=True)
def api_rule_pause(req, user, body, rid, org, **_):
    _own_rule(org, rid)
    db.run("UPDATE rules SET paused = ?, updated = ? WHERE id = ?", 1 if body.get("paused") else 0, time.time(), int(rid))
    db.log(int(rid), "-", "pause", "ok", f"{'gepauzeerd' if body.get('paused') else 'hervat'} door {user['username']}")
    checker.run_cycle(rule_id=int(rid))
    return _rule_detail(int(rid))


@route("POST", r"/api/checks/(?P<cid>\d+)/pause", org=True)
def api_check_pause(req, user, body, cid, org, **_):
    c = db.one("SELECT c.rule_id FROM checks c JOIN rules r ON r.id = c.rule_id WHERE c.id = ? AND r.org_id = ?", int(cid), org)
    if not c:
        raise ApiError(404, "Controle niet gevonden")
    db.run("UPDATE checks SET paused = ? WHERE id = ?", 1 if body.get("paused") else 0, int(cid))
    checker.run_cycle(rule_id=c["rule_id"])
    return _rule_detail(c["rule_id"])


@route("POST", r"/api/rules/(?P<rid>\d+)/ack", org=True)
def api_rule_ack(req, user, rid, org, **_):
    _own_rule(org, rid)
    notify.acknowledge(int(rid), user["username"])
    return _rule_detail(int(rid))


@route("POST", r"/api/rules/(?P<rid>\d+)/check", org=True)
def api_rule_check(req, user, rid, org, **_):
    _own_rule(org, rid)
    checker.run_cycle(rule_id=int(rid))
    return _rule_detail(int(rid))


@route("POST", r"/api/rules/(?P<rid>\d+)/test", org=True)
def api_rule_test(req, user, rid, body, org, **_):
    """Sends a test notification to everyone on the rule, with the rule's real values."""
    rule = notify.with_org(_own_rule(org, rid))
    cfg = settings.get_all()
    checks = checker.load_checks(int(rid))[:5]
    items = [{"check": c, "alarm": {"kind": "high", "reason": c["reason"] or "testmelding", "started": time.time()}}
             for c in checks] or []
    if not items:
        raise ApiError(400, "Voeg eerst waarden toe aan de regel")
    results = []
    for p in notify.recipients(int(rid)):
        if p["want_email"] and p["email"]:
            text, body_html = messages.mail_bodies("test", rule, items, cfg)
            try:
                from .channels import mail
                mail.send(cfg, p["email"], messages.subject("test", rule, items), text, body_html)
                results.append(f"{p['name']}: mail OK")
            except Exception as e:  # noqa: BLE001
                results.append(f"{p['name']}: mail mislukt ({e})")
        if p["want_sms"] and p["phone"]:
            try:
                from .channels import sms
                sms.send(cfg, p["phone"], messages.sms_text("test", rule, items))
                results.append(f"{p['name']}: sms OK")
            except Exception as e:  # noqa: BLE001
                results.append(f"{p['name']}: sms mislukt ({e})")
    db.log(int(rid), "-", "test", "ok", "; ".join(results) or "geen mail/sms-ontvangers")
    return {"message": "; ".join(results) or "Geen ontvangers met mail of sms aangevinkt (gebruik de testknoppen bij Contacten om te bellen)"}


@route("GET", "/api/log", org=True)
def api_log(req, user, q, org, **_):
    sql = ("SELECT l.*, c.name AS contact, r.name AS rule FROM log l LEFT JOIN contacts c ON c.id = l.contact_id "
           "LEFT JOIN rules r ON r.id = l.rule_id WHERE l.org_id = ?")
    return db.q(sql + " ORDER BY l.ts DESC LIMIT 500", org)


# --- HTTP plumbing -----------------------------------------------------------------------

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "InfluxAlarm/1.0"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _user(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        token = cookie[COOKIE].value if COOKIE in cookie else None
        self.session_token = token
        if not token:
            return None
        return db.one("SELECT u.id, u.username, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id "
                      "WHERE s.token = ? AND s.expires > ?", token, time.time())

    def start_session(self, user_id, payload):
        token = secrets.token_urlsafe(32)
        db.run("DELETE FROM sessions WHERE expires < ?", time.time())
        db.run("INSERT INTO sessions (token, user_id, expires) VALUES (?,?,?)", token, user_id, time.time() + SESSION_DAYS * 86400)
        self.extra_headers.append(("Set-Cookie", f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"))
        return payload

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for k, v in self.extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        self.extra_headers = []
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if "/api/" in path:
            path = path[path.index("/api/"):]  # tolerate a reverse-proxy prefix
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else self._send(404, {"error": "Niet gevonden"})
        try:
            body = {}
            if method in ("POST", "PUT"):
                # JSON only: together with SameSite cookies this blocks cross-site form posts.
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    raise ApiError(415, "JSON verwacht")
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            user = self._user()
            for m, pattern, fn, public, admin, needs_org in ROUTES:
                match = pattern.match(path)
                if m == method and match:
                    if not public and not user:
                        raise ApiError(401, "Niet aangemeld")
                    if admin and not user["is_admin"]:
                        raise ApiError(403, "Alleen voor beheerders")
                    org = None
                    if needs_org:
                        org = int(self.headers.get("X-Org") or 0)
                        if org not in {o["id"] for o in user_orgs(user)}:
                            raise ApiError(403, "Geen toegang tot deze organisatie")
                    return self._send(200, fn(self, user, body=body, q=q, org=org, **match.groupdict()))
            raise ApiError(404, "Niet gevonden")
        except ApiError as e:
            self._send(e.status, {"error": e.message})
        except (influx.InfluxError, ValueError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("API error")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _static(self, path):
        rel = path.rsplit("/", 1)[-1] or "index.html"
        if rel == "status":
            rel = "status.html"  # the open status page
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full) or os.path.dirname(os.path.realpath(full)) != os.path.realpath(STATIC_DIR):
            full = os.path.join(STATIC_DIR, "index.html")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def serve(port):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    log.info("Influx Alarm op http://0.0.0.0:%s", port)
    server.serve_forever()
