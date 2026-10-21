"""Sending notifications: mail and SMS to everyone at once, calls as a cascade."""
import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, messages
from .channels import ami, mail, sms, tts

log = logging.getLogger("notify")

CALL_ROUNDS = 2  # the call list is tried twice when nobody confirms
_cascades = {}  # rule id -> running cascade thread
_cascade_lock = threading.Lock()


# --- helpers --------------------------------------------------------------------

def with_org(rule):
    """Rule plus "org_name" — only filled when there is more than one organisation, so messages
    tell people in several organisations what they are about."""
    rule = dict(rule)
    many = (db.one("SELECT COUNT(*) AS n FROM organizations") or {"n": 0})["n"] > 1
    org = db.one("SELECT name FROM organizations WHERE id = ?", rule.get("org_id")) if many else None
    rule["org_name"] = org["name"] if org else ""
    return rule


def recipients(rule_id):
    return db.q(
        "SELECT c.*, rc.email AS want_email, rc.sms AS want_sms, rc.call AS want_call, rc.position "
        "FROM rule_contacts rc JOIN contacts c ON c.id = rc.contact_id WHERE rc.rule_id = ? "
        "ORDER BY rc.position, c.name", rule_id)


def load_items(alarm_ids):
    """[{check, alarm}] for the given alarm ids, in check order."""
    if not alarm_ids:
        return []
    marks = ",".join("?" * len(alarm_ids))
    rows = db.q(f"SELECT a.*, c.bucket, c.measurement, c.field, c.tags, c.label, c.lower, c.upper, c.last_value, "
                f"c.last_time, c.position AS check_position FROM alarms a JOIN checks c ON c.id = a.check_id "
                f"WHERE a.id IN ({marks}) ORDER BY c.position, a.id", *alarm_ids)
    items = []
    for r in rows:
        check = {k: r[k] for k in ("bucket", "measurement", "field", "label", "lower", "upper", "last_value", "last_time")}
        check["id"] = r["check_id"]
        check["tags"] = db.jload(r["tags"], {})
        items.append({"check": check, "alarm": r})
    return items


def in_quiet_hours(rule, now, tz):
    if not rule.get("quiet_enabled"):
        return False
    local = datetime.fromtimestamp(now, ZoneInfo(tz)).strftime("%H:%M")
    start, end = rule.get("quiet_start") or "22:00", rule.get("quiet_end") or "07:00"
    return (start <= local < end) if start <= end else (local >= start or local < end)


def quiet_end(rule, now, tz):
    """Epoch of the next end of the quiet hours."""
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now, zone)
    h, m = map(int, (rule.get("quiet_end") or "07:00").split(":"))
    end = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.timestamp()


# --- sending ------------------------------------------------------------------

def _send_mail(cfg, rule, contact, kind, items, alarm_ids):
    subject = messages.subject(kind, rule, items)
    text, body_html = messages.mail_bodies(kind, rule, items, cfg)
    try:
        mail.send(cfg, contact["email"], subject, text, body_html)
        db.log(rule["id"], "mail", kind, "ok", contact["email"], contact["id"], alarm_ids)
        return True
    except mail.MailError as e:
        db.log(rule["id"], "mail", kind, "error", str(e), contact["id"], alarm_ids)
        return False


def _send_sms(cfg, rule, contact, kind, items, alarm_ids):
    if contact["phone"].lower().startswith("test:"):
        db.log(rule["id"], "sms", kind, "overgeslagen", "testnummer: geen sms verstuurd", contact["id"], alarm_ids)
        return False
    try:
        sms.send(cfg, contact["phone"], messages.sms_text(kind, rule, items))
        db.log(rule["id"], "sms", kind, "ok", contact["phone"], contact["id"], alarm_ids)
        return True
    except sms.SmsError as e:
        db.log(rule["id"], "sms", kind, "error", str(e), contact["id"], alarm_ids)
        return False


def notify(cfg, rule, kind, alarm_ids, now=None):
    """Alarm or reminder: mail + SMS right away, calls as a cascade (deferred during quiet hours)."""
    now = now or time.time()
    rule = with_org(rule)
    items = load_items(alarm_ids)
    if not items:
        return
    people = recipients(rule["id"])
    for p in people:
        if p["want_email"] and p["email"]:
            _send_mail(cfg, rule, p, kind, items, alarm_ids)
        if p["want_sms"] and p["phone"]:
            _send_sms(cfg, rule, p, kind, items, alarm_ids)
    if any(p["want_call"] and p["phone"] for p in people):
        if in_quiet_hours(rule, now, cfg["timezone"]):
            due = quiet_end(rule, now, cfg["timezone"])
            db.run("INSERT INTO pending_calls (rule_id, due, kind, alarm_ids) VALUES (?,?,?,?)",
                   rule["id"], due, kind, ",".join(map(str, alarm_ids)))
            db.log(rule["id"], "call", kind, "uitgesteld",
                   f"stille uren: oproepen om {messages.fmt_time(due, cfg['timezone'])}", alarm_ids=alarm_ids)
        else:
            start_cascade(cfg, rule, kind, alarm_ids)
    if not people:
        db.log(rule["id"], "-", kind, "error", "Geen contactpersonen gekoppeld aan deze regel", alarm_ids=alarm_ids)


def notify_resolved(cfg, rule, alarm_ids):
    """'Resolved' mail to everyone who was notified; SMS too for those who got an SMS or a call."""
    rule = with_org(rule)
    items = load_items(alarm_ids)
    if not items:
        return
    patterns = [f"%,{i},%" for i in alarm_ids]
    where = " OR ".join("alarm_ids LIKE ?" for _ in patterns)
    sent = db.q(f"SELECT DISTINCT contact_id, channel FROM log WHERE rule_id = ? AND kind IN ('alarm','reminder') "
                f"AND contact_id IS NOT NULL AND ({where})", rule["id"], *patterns)
    by_contact = {}
    for s in sent:
        by_contact.setdefault(s["contact_id"], set()).add(s["channel"])
    for cid, channels in by_contact.items():
        contact = db.one("SELECT * FROM contacts WHERE id = ?", cid)
        if not contact:
            continue
        if contact["email"]:
            _send_mail(cfg, rule, contact, "resolved", items, alarm_ids)
        if channels & {"sms", "call"} and contact["phone"]:
            _send_sms(cfg, rule, contact, "resolved", items, alarm_ids)


# --- call cascade ----------------------------------------------------------------

def cascade_running(rule_id):
    t = _cascades.get(rule_id)
    return bool(t and t.is_alive())


def start_cascade(cfg, rule, kind, alarm_ids):
    with _cascade_lock:
        if cascade_running(rule["id"]):
            db.log(rule["id"], "call", kind, "overgeslagen", "er loopt al een belronde voor deze regel", alarm_ids=alarm_ids)
            return
        t = threading.Thread(target=_cascade, args=(cfg, rule, kind, list(alarm_ids)), daemon=True,
                             name=f"cascade-{rule['id']}")
        _cascades[rule["id"]] = t
        t.start()


def _still_relevant(rule_id, alarm_ids, started):
    """Stop calling once the alarms are resolved/ignored, confirmed, or the rule is paused."""
    rule = db.one("SELECT paused FROM rules WHERE id = ?", rule_id)
    if not rule or rule["paused"]:
        return False
    marks = ",".join("?" * len(alarm_ids))
    open_ = db.q(f"SELECT acked FROM alarms WHERE id IN ({marks}) AND resolved IS NULL AND ignored IS NULL", *alarm_ids)
    return bool(open_) and not any(a["acked"] and a["acked"] >= started for a in open_)


def _cascade(cfg, rule, kind, alarm_ids):
    started = time.time()
    rule = with_org(rule)  # also for calls deferred by quiet hours
    try:
        items = load_items(alarm_ids)
        callees = [p for p in recipients(rule["id"]) if p["want_call"] and p["phone"]]
        if not items or not callees:
            return
        sound = tts.synthesize(cfg, messages.speech_text(kind, rule, items), f"call-{rule['id']}-{int(started)}")
        ack_sound = ensure_ack_sound(cfg)
        for round_no in range(1, CALL_ROUNDS + 1):
            for p in callees:
                if not _still_relevant(rule["id"], alarm_ids, started):
                    return
                call_id = f"r{rule['id']}-{int(time.time() * 1000)}"
                try:
                    result = ami.call(cfg, p["phone"], call_id, sound, ack_sound,
                                      ring_seconds=rule.get("ring_seconds") or 30, caller_id=cfg.get("caller_id", ""))
                except ami.AmiError as e:
                    result = "failed"
                    db.log(rule["id"], "call", kind, "error", str(e), p["id"], alarm_ids)
                    continue
                db.log(rule["id"], "call", kind, result, f"{p['phone']} (ronde {round_no})", p["id"], alarm_ids)
                if result == "ack":
                    acknowledge(rule["id"], f"{p['name']} (telefoon)", alarm_ids)
                    return
            if round_no < CALL_ROUNDS:
                wait_until = time.time() + (rule.get("cascade_wait_min") or 5) * 60
                while time.time() < wait_until:
                    if not _still_relevant(rule["id"], alarm_ids, started):
                        return
                    time.sleep(5)
        db.log(rule["id"], "call", kind, "niemand", "niemand heeft bevestigd", alarm_ids=alarm_ids)
    except Exception as e:  # noqa: BLE001 - a cascade must never take the app down
        log.exception("cascade failed")
        db.log(rule["id"], "call", kind, "error", f"{type(e).__name__}: {e}", alarm_ids=alarm_ids)


def acknowledge(rule_id, by, alarm_ids=None):
    """Confirmation: stops the running cascade (reminders after 24 h continue while unresolved)."""
    ts = time.time()
    if alarm_ids:
        marks = ",".join("?" * len(alarm_ids))
        db.run(f"UPDATE alarms SET acked = ?, acked_by = ? WHERE id IN ({marks}) AND resolved IS NULL", ts, by, *alarm_ids)
    else:
        db.run("UPDATE alarms SET acked = ?, acked_by = ? WHERE rule_id = ? AND resolved IS NULL", ts, by, rule_id)
    db.log(rule_id, "ack", "ack", "ok", f"bevestigd door {by}", alarm_ids=alarm_ids or ())


_ack_lock = threading.Lock()


def ensure_ack_sound(cfg):
    import os
    name = f"ack-{cfg.get('tts_voice')}"
    with _ack_lock:
        if not os.path.exists(os.path.join(cfg["sounds_dir"], f"{name}.wav")):
            tts.synthesize(cfg, "Bedankt, het alarm is bevestigd. Tot ziens.", name)
    return f"alarm/{name}"


def process_pending_calls(cfg, now):
    """Calls deferred by quiet hours."""
    for p in db.q("SELECT * FROM pending_calls WHERE due <= ?", now):
        db.run("DELETE FROM pending_calls WHERE id = ?", p["id"])
        rule = db.one("SELECT * FROM rules WHERE id = ?", p["rule_id"])
        ids = [int(i) for i in p["alarm_ids"].split(",") if i]
        if rule and _still_relevant(rule["id"], ids, p["due"] - 1):
            start_cascade(cfg, rule, p["kind"], ids)


# --- tests from the UI ----------------------------------------------------------------

def test_contact(cfg, contact, channel):
    """Sends a test via one channel; returns a short result text (raises on error)."""
    rule = {"id": 0, "name": "Testregel", "message": "Dit is een test van Influx Alarm"}
    check = {"bucket": "bucket", "measurement": "°C", "field": "value", "tags": {"entity_id": "sensor.test"},
             "label": "Testsensor", "lower": 2, "upper": 7, "last_value": "8.5", "last_time": time.time()}
    alarm = {"kind": "high", "reason": "8,5 °C boven de bovengrens 7 °C", "started": time.time()}
    items = [{"check": check, "alarm": alarm}]
    if channel == "mail":
        text, body_html = messages.mail_bodies("test", rule, items, cfg)
        mail.send(cfg, contact["email"], messages.subject("test", rule, items), text, body_html)
        return f"Testmail verstuurd naar {contact['email']}"
    if channel == "sms":
        sms.send(cfg, contact["phone"], messages.sms_text("test", rule, items))
        return f"Test-SMS verstuurd naar {contact['phone']}"
    if channel == "call":
        sound = tts.synthesize(cfg, "Dit is een test van Influx Alarm. " + messages.speech_text("alarm", rule, items),
                               f"call-test-{int(time.time())}")
        result = ami.call(cfg, contact["phone"], f"test-{int(time.time() * 1000)}", sound, ensure_ack_sound(cfg),
                          ring_seconds=30, caller_id=cfg.get("caller_id", ""))
        return {"ack": "Opgenomen en 1 gedrukt: alles werkt",
                "noack": "Opgenomen, maar geen 1 gedrukt",
                "noanswer": "Niet opgenomen",
                "busy": "Bezet",
                "congestion": "Netwerk bezet / nummer niet bereikbaar",
                "failed": "Oproep mislukt"}.get(result, result)
    raise ValueError(f"Onbekend kanaal: {channel}")
