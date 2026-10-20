"""Background loop: evaluates all rules, opens/closes alarms and triggers notifications."""
import json
import logging
import threading
import time

from . import db, influx, messages, notify, settings
from .channels import tts

log = logging.getLogger("checker")

LOOKBACK_S = 30 * 86400  # how far back to look for the last point of a series
REMIND_AFTER_S = 24 * 3600
ALARM_STATES = {"high", "low", "nodata"}

state = {"last_run": None, "last_error": None, "running": False}
_wake = threading.Event()
_cycle_lock = threading.Lock()


def load_checks(rule_id):
    rows = db.q("SELECT * FROM checks WHERE rule_id = ? ORDER BY position, id", rule_id)
    for r in rows:
        r["tags"] = db.jload(r["tags"], {})
    return rows


def open_alarms(rule_id):
    return db.q("SELECT * FROM alarms WHERE rule_id = ? AND resolved IS NULL ORDER BY id", rule_id)


def _close_silently(alarm_ids, now, why):
    for aid in alarm_ids:
        db.run("UPDATE alarms SET resolved = ?, reason = reason || ? WHERE id = ?", now, f" ({why})", aid)


def evaluate_rule(cfg, rule, now):
    """Updates the check states of one rule; returns {check_id: status}."""
    checks = load_checks(rule["id"])
    statuses = {}
    active = [c for c in checks if not c["paused"] and not rule["paused"]]
    for c in checks:
        if c not in active:
            db.run("UPDATE checks SET status = 'paused', reason = '', checked_at = ? WHERE id = ?", now, c["id"])
            statuses[c["id"]] = "paused"
    by_bucket = {}
    for c in active:
        by_bucket.setdefault(c["bucket"], []).append(c)
    for bucket, group in by_bucket.items():
        try:
            last = influx.last_points(cfg, bucket, group, LOOKBACK_S, now)
        except influx.InfluxError as e:
            for c in group:
                db.run("UPDATE checks SET status = 'error', reason = ?, checked_at = ? WHERE id = ?", str(e), now, c["id"])
                statuses[c["id"]] = "error"
            continue
        for c in group:
            point = last.get(c["id"])
            extremes = None
            try:
                if point and (c["delay_min"] or 0) > 0 and messages.is_number(point[0]) and \
                        (c["lower"] is not None or c["upper"] is not None):
                    extremes = influx.window_extremes(cfg, bucket, c, now - c["delay_min"] * 60, now, LOOKBACK_S)
                status, reason = messages.evaluate(c, point, extremes, now)
            except influx.InfluxError as e:
                status, reason = "error", str(e)
            value, t = point if point else (None, None)
            db.run("UPDATE checks SET status = ?, reason = ?, last_value = ?, last_time = ?, checked_at = ? WHERE id = ?",
                   status, reason, json.dumps(value), t, now, c["id"])
            c.update(status=status, reason=reason, last_value=json.dumps(value), last_time=t)
            statuses[c["id"]] = status
    return statuses


def process_rule(cfg, rule, now):
    statuses = evaluate_rule(cfg, rule, now)
    checks = {c["id"]: c for c in load_checks(rule["id"])}
    new_ids, resolved_ids = [], []

    for a in open_alarms(rule["id"]):
        c = checks.get(a["check_id"])
        st = statuses.get(a["check_id"])
        if c is None:
            _close_silently([a["id"]], now, "controle verwijderd")
        elif st == "paused":
            _close_silently([a["id"]], now, "gepauzeerd")
        elif st in ("ok", "pending"):
            db.run("UPDATE alarms SET resolved = ? WHERE id = ?", now, a["id"])
            if a["notified"] and not a["ignored"]:
                resolved_ids.append(a["id"])
        elif st in ALARM_STATES:
            db.run("UPDATE alarms SET kind = ?, reason = ?, value = ? WHERE id = ?", st, c["reason"], c["last_value"], a["id"])
        # "error" (InfluxDB unreachable): keep the alarm as it is

    open_by_check = {a["check_id"]: a for a in open_alarms(rule["id"])}
    for cid, st in statuses.items():
        if st in ALARM_STATES and cid not in open_by_check:
            c = checks[cid]
            new_ids.append(db.run("INSERT INTO alarms (rule_id, check_id, kind, reason, value, started) VALUES (?,?,?,?,?,?)",
                                  rule["id"], cid, st, c["reason"], c["last_value"], now))

    # Problems that are not solved after the timeout are ignored: no more reminders.
    if rule["ignore_after_hours"]:
        limit = now - rule["ignore_after_hours"] * 3600
        for a in db.q("SELECT id FROM alarms WHERE rule_id = ? AND resolved IS NULL AND ignored IS NULL AND started < ?",
                      rule["id"], limit):
            db.run("UPDATE alarms SET ignored = ? WHERE id = ?", now, a["id"])
            db.log(rule["id"], "-", "ignored", "ok", f"niet opgelost na {messages.fmt_duration(rule['ignore_after_hours'] * 3600)}: "
                   "wordt genegeerd", alarm_ids=[a["id"]])

    if resolved_ids and not rule["paused"]:
        notify.notify_resolved(cfg, rule, resolved_ids)

    if rule["paused"]:
        return
    active = db.q("SELECT id FROM alarms WHERE rule_id = ? AND resolved IS NULL AND ignored IS NULL ORDER BY id", rule["id"])
    active_ids = [a["id"] for a in active]
    kind = None
    if new_ids:
        kind = "alarm"  # new problems: send right away, together with the ones still open
    elif active_ids and (rule["last_notified"] is None or now - rule["last_notified"] >= REMIND_AFTER_S):
        kind = "reminder"  # at most once per 24 h while unresolved
    if kind:
        marks = ",".join("?" * len(active_ids))
        db.run(f"UPDATE alarms SET notified = 1 WHERE id IN ({marks})", *active_ids)
        db.run("UPDATE rules SET last_notified = ? WHERE id = ?", now, rule["id"])
        notify.notify(cfg, rule, kind, active_ids, now)


def run_cycle(now=None, rule_id=None):
    with _cycle_lock:
        cfg = settings.get_all()
        now = now or time.time()
        rules = db.q("SELECT * FROM rules" + (" WHERE id = ?" if rule_id else ""), *([rule_id] if rule_id else []))
        errors = []
        for rule in rules:
            try:
                process_rule(cfg, rule, now)
            except Exception as e:  # noqa: BLE001 - one broken rule must not stop the others
                log.exception("rule %s failed", rule["id"])
                errors.append(f"{rule['name']}: {e}")
        notify.process_pending_calls(cfg, now)
        state["last_run"] = now
        state["last_error"] = "; ".join(errors) or None


def wake():
    _wake.set()


def loop():
    while True:
        state["running"] = True
        try:
            run_cycle()
            tts.cleanup(settings.get_all())
        except Exception as e:  # noqa: BLE001
            log.exception("check cycle failed")
            state["last_error"] = str(e)
        interval = max(15, int(settings.get_all().get("check_interval") or 60))
        _wake.wait(interval)
        _wake.clear()


def start():
    threading.Thread(target=loop, daemon=True, name="checker").start()
