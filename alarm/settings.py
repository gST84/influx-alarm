"""Settings stored in the database, with defaults from environment variables.

Asterisk/AMI settings come only from the environment: they are shared with the
Asterisk container through docker compose's .env file.
"""
import json
import os

from . import db

SECRET_KEYS = {"influx_token", "smtp_password", "sms_http_headers", "twilio_token"}

DEFAULTS = {
    # InfluxDB
    "influx_url": os.environ.get("INFLUX_URL", "http://localhost:8086"),
    "influx_token": os.environ.get("INFLUX_TOKEN", ""),
    "influx_org": os.environ.get("INFLUX_ORG", ""),
    # general
    "check_interval": 60,
    "public_url": os.environ.get("PUBLIC_URL", "http://localhost:8080"),
    "timezone": os.environ.get("TZ", "Europe/Brussels"),
    "tts_voice": "nl_BE-nathalie-medium",
    "caller_id": os.environ.get("CALLER_ID", ""),
    "public_status": True,  # open status page at /status, without login
    # mail
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",  # starttls | ssl | none
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    # sms
    "sms_provider": "none",  # none | http | twilio | asterisk
    "sms_http_method": "POST",
    "sms_http_url": "",
    "sms_http_headers": "",  # one "Name: value" per line (e.g. an Authorization header)
    "sms_http_body": '{"to": "{to}", "message": "{message}"}',
    "sms_http_content_type": "application/json",
    "twilio_sid": "",
    "twilio_token": "",
    "twilio_from": "",
    "sms_asterisk_mode": "dongle",  # dongle | sip
    "sms_dongle_device": "dongle0",
    "sms_sip_domain": "",
    "sms_sip_from": "",
}

ENV_ONLY = {
    "ami_host": os.environ.get("AMI_HOST", "asterisk"),
    "ami_port": int(os.environ.get("AMI_PORT", "5038")),
    "ami_user": os.environ.get("AMI_USER", "alarm"),
    "ami_password": os.environ.get("AMI_PASSWORD", ""),
    "sip_endpoint": "trunk",
    "sip_configured": bool(os.environ.get("SIP_HOST") and os.environ.get("SIP_USERNAME")),
    "sounds_dir": os.environ.get("SOUNDS_DIR", "/data/sounds"),
    "voices_dir": os.environ.get("VOICES_DIR", "/opt/voices"),
}


def get_all():
    cfg = dict(DEFAULTS)
    for row in db.q("SELECT key, value FROM settings"):
        if row["key"] in DEFAULTS:
            cfg[row["key"]] = json.loads(row["value"])
    cfg.update(ENV_ONLY)
    return cfg


def public():
    """Settings for the UI: secrets replaced by a marker telling whether they are set."""
    cfg = get_all()
    out = {k: v for k, v in cfg.items() if k != "ami_password"}
    for k in SECRET_KEYS:
        out[k] = "••••••" if cfg.get(k) else ""
    return out


def update(values):
    for k, v in values.items():
        if k not in DEFAULTS:
            continue
        if k in SECRET_KEYS and v == "••••••":
            continue  # unchanged secret
        if isinstance(DEFAULTS[k], bool):
            v = v in (True, 1, "1", "true", "on")
        elif isinstance(DEFAULTS[k], int):
            v = int(v)
        db.run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               k, json.dumps(v))
