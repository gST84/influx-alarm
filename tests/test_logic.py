import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from alarm import checker, db, messages, notify  # noqa: E402

NOW = 1_800_000_000.0


def check(**kw):
    base = {"measurement": "°C", "field": "value", "tags": {"entity_id": "sensor.koelcel"}, "label": "",
            "lower": 2.0, "upper": 7.0, "delay_min": 0, "nodata_min": 60}
    base.update(kw)
    return base


class EvaluateTest(unittest.TestCase):
    def test_bounds_without_delay(self):
        self.assertEqual(messages.evaluate(check(), (8.5, NOW - 10), None, NOW)[0], "high")
        self.assertEqual(messages.evaluate(check(), (1.0, NOW - 10), None, NOW)[0], "low")
        self.assertEqual(messages.evaluate(check(), (5.0, NOW - 10), None, NOW), ("ok", ""))

    def test_reason_uses_unit_and_decimal_comma(self):
        self.assertEqual(messages.evaluate(check(), (8.5, NOW), None, NOW)[1], "8,5 °C boven de bovengrens 7 °C")

    def test_nodata(self):
        status, reason = messages.evaluate(check(), (5.0, NOW - 2 * 3600), None, NOW)
        self.assertEqual(status, "nodata")
        self.assertIn("2 u", reason)
        self.assertEqual(messages.evaluate(check(), None, None, NOW)[0], "nodata")
        self.assertEqual(messages.evaluate(check(nodata_min=0), (5.0, NOW - 99999), None, NOW)[0], "ok")

    def test_delay_needs_the_whole_window(self):
        c = check(delay_min=10)
        # out of bounds now, but within bounds just before the window: not yet an alarm
        self.assertEqual(messages.evaluate(c, (8.0, NOW), (6.0, 8.0, 8.0), NOW)[0], "pending")
        # above the limit before and during the whole window
        self.assertEqual(messages.evaluate(c, (8.0, NOW), (9.0, 7.5, 9.0), NOW)[0], "high")
        # a dip inside the window: no alarm
        self.assertEqual(messages.evaluate(c, (8.0, NOW), (9.0, 6.0, 9.0), NOW)[0], "pending")

    def test_delay_with_sparse_data(self):
        # Home Assistant only writes on change: no points inside the window, the value before it counts.
        self.assertEqual(messages.evaluate(check(delay_min=10), (8.0, NOW - 3000), (8.0, None, None), NOW)[0], "high")

    def test_text_values_only_check_for_missing_data(self):
        self.assertEqual(messages.evaluate(check(), ("open", NOW), None, NOW)[0], "ok")


class MessagesTest(unittest.TestCase):
    def test_labels_and_units(self):
        c = check(tags={"entity_id": "sensor.koelcel", "friendly_name": "Koelcel"})
        self.assertEqual(messages.label_of(c), "Koelcel (sensor.koelcel)")
        self.assertEqual(messages.unit_of(c), "°C")
        self.assertEqual(messages.unit_of(check(measurement="victron", tags={"unit": "V"})), "V")

    def test_sms_is_short(self):
        rule = {"id": 1, "name": "Koeling", "message": "Bel de technieker"}
        items = [{"check": check(label=f"Sensor {i}"), "alarm": {"kind": "high", "reason": "8 °C boven 7 °C"}}
                 for i in range(20)]
        text = messages.sms_text("alarm", rule, items)
        self.assertLessEqual(len(text), 320)
        self.assertIn("meer", text)

    def test_speech(self):
        rule = {"id": 1, "name": "Koeling", "message": ""}
        items = [{"check": check(last_value="8.5"), "alarm": {"kind": "high"}}]
        self.assertIn("8,5 graden, boven de grens van 7 graden", messages.speech_text("alarm", rule, items))


class QuietHoursTest(unittest.TestCase):
    def test_overnight_window(self):
        rule = {"quiet_enabled": 1, "quiet_start": "22:00", "quiet_end": "07:00"}
        tz = "Europe/Brussels"
        at = lambda h: time.mktime(time.strptime(f"2026-10-20 {h}", "%Y-%m-%d %H:%M"))
        with mock.patch.dict(os.environ, {"TZ": tz}):
            time.tzset()
            self.assertTrue(notify.in_quiet_hours(rule, at("23:30"), tz))
            self.assertTrue(notify.in_quiet_hours(rule, at("03:00"), tz))
            self.assertFalse(notify.in_quiet_hours(rule, at("12:00"), tz))
            self.assertEqual(time.strftime("%H:%M", time.localtime(notify.quiet_end(rule, at("23:30"), tz))), "07:00")
        self.assertFalse(notify.in_quiet_hours({**rule, "quiet_enabled": 0}, at("23:30"), tz))


class FlowTest(unittest.TestCase):
    """Alarm → reminder after 24 h → resolved, with InfluxDB and the channels faked."""

    def setUp(self):
        db.init()
        for t in ("rules", "checks", "alarms", "contacts", "rule_contacts", "log", "pending_calls"):
            db.run(f"DELETE FROM {t}")
        self.rid = db.run("INSERT INTO rules (name, message) VALUES ('Koeling', 'Kijk de koelcel na')")
        cid = db.run("INSERT INTO contacts (name, email, phone) VALUES ('Jan', 'jan@x.be', '+32470000000')")
        db.run("INSERT INTO rule_contacts (rule_id, contact_id, email, sms, call) VALUES (?,?,1,1,0)", self.rid, cid)
        db.run("INSERT INTO checks (rule_id, bucket, measurement, field, tags, lower, upper, nodata_min) "
               "VALUES (?, 'ha', '°C', 'value', '{\"entity_id\": \"sensor.koelcel\"}', 2, 7, 60)", self.rid)
        self.sent = []
        self.patches = [
            mock.patch("alarm.channels.mail.send", lambda cfg, to, subj, text, html=None: self.sent.append(("mail", subj))),
            mock.patch("alarm.channels.sms.send", lambda cfg, to, text: self.sent.append(("sms", text))),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def cycle(self, value, now):
        with mock.patch("alarm.influx.last_points", lambda cfg, b, checks, lb, n: {c["id"]: (value, now - 30) for c in checks}):
            checker.run_cycle(now=now)

    def test_alarm_reminder_resolved(self):
        self.cycle(5.0, NOW)
        self.assertEqual(self.sent, [])
        self.cycle(9.0, NOW + 60)
        self.assertEqual([k for k, _ in self.sent], ["mail", "sms"])
        self.assertIn("[ALARM] Koeling", self.sent[0][1])
        self.sent.clear()
        self.cycle(9.0, NOW + 3600)  # still in alarm: no new message within 24 h
        self.assertEqual(self.sent, [])
        self.cycle(9.0, NOW + 60 + 24 * 3600)
        self.assertIn("[HERINNERING]", self.sent[0][1])
        self.sent.clear()
        self.cycle(5.0, NOW + 25 * 3600)
        self.assertEqual([k for k, _ in self.sent], ["mail", "sms"])
        self.assertIn("[OPGELOST]", self.sent[0][1])

    def test_ignore_after_timeout(self):
        db.run("UPDATE rules SET ignore_after_hours = 48 WHERE id = ?", self.rid)
        self.cycle(9.0, NOW)
        self.sent.clear()
        self.cycle(9.0, NOW + 49 * 3600)  # past the timeout: ignored instead of a reminder
        self.assertEqual(self.sent, [])
        self.assertIsNotNone(db.one("SELECT ignored FROM alarms")["ignored"])

    def test_paused_rule_sends_nothing(self):
        db.run("UPDATE rules SET paused = 1 WHERE id = ?", self.rid)
        self.cycle(9.0, NOW)
        self.assertEqual(self.sent, [])
        self.assertEqual(db.one("SELECT status FROM checks")["status"], "paused")


if __name__ == "__main__":
    unittest.main()
