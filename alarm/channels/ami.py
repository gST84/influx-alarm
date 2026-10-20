"""Minimal Asterisk Manager Interface (AMI) client for alarm calls and SMS."""
import itertools
import re
import socket
import time

_ids = itertools.count(1)

# OriginateResponse "Reason" codes (Asterisk's AST_CONTROL_* values).
_REASONS = {3: "noanswer", 5: "busy", 8: "congestion"}


class AmiError(Exception):
    pass


class Ami:
    def __init__(self, host, port, user, secret, timeout=10):
        self.host, self.port, self.user, self.secret, self.timeout = host, int(port), user, secret, timeout
        self.sock = None
        self.buf = b""

    def __enter__(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise AmiError(f"Asterisk (AMI) niet bereikbaar op {self.host}:{self.port}: {e}") from e
        self._readline()  # banner "Asterisk Call Manager/x.y"
        res = self.request([("Action", "Login"), ("Username", self.user), ("Secret", self.secret), ("Events", "on")])
        if res.get("Response") != "Success":
            raise AmiError(f"AMI-login mislukt: {res.get('Message', res)}")
        return self

    def __exit__(self, *exc):
        try:
            self.send([("Action", "Logoff")])
        except OSError:
            pass
        self.sock.close()

    def _readline(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AmiError("Verbinding met Asterisk verbroken")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def send(self, fields):
        """Sends an action given as a list of (key, value) pairs (keys may repeat). Returns its ActionID."""
        action_id = next((v for k, v in fields if k == "ActionID"), None)
        if action_id is None:
            action_id = f"alarm-{next(_ids)}"
            fields = [*fields, ("ActionID", action_id)]
        data = "".join(f"{k}: {_clean(v)}\r\n" for k, v in fields) + "\r\n"
        self.sock.sendall(data.encode())
        return action_id

    def read(self):
        """Reads one message (response or event) as a dict."""
        msg = {}
        while True:
            line = self._readline()
            if not line:
                if msg:
                    return msg
                continue
            key, _, value = line.partition(":")
            msg[key.strip()] = value.strip()

    def request(self, fields):
        action_id = self.send(fields)
        while True:
            msg = self.read()
            if msg.get("ActionID") == action_id and "Response" in msg:
                return msg


def _clean(value):
    # Header values must stay on one line.
    return re.sub(r"[\r\n]+", " ", str(value))


def channel_for(number, endpoint="trunk"):
    """Dial string: "test:1001" uses the simulated phones in Asterisk's [alarm-test] context."""
    number = number.strip()
    if number.lower().startswith("test:"):
        return f"Local/{number[5:].strip()}@alarm-test"
    digits = re.sub(r"[^\d+*#]", "", number)
    if not digits:
        raise AmiError(f"Ongeldig telefoonnummer: {number}")
    return f"PJSIP/{digits}@{endpoint}"


def call(cfg, number, call_id, sound, ack_sound, ring_seconds=30, caller_id="", max_seconds=180):
    """Calls a number, plays `sound` and waits for "1".

    Returns "ack", "noack" (answered, no 1 pressed), "noanswer", "busy", "congestion" or "failed".
    """
    with Ami(cfg["ami_host"], cfg["ami_port"], cfg["ami_user"], cfg["ami_password"]) as ami:
        action_id = ami.send([
            ("Action", "Originate"),
            ("Channel", channel_for(number, cfg.get("sip_endpoint") or "trunk")),
            ("Context", "alarm-call"),
            ("Exten", "s"),
            ("Priority", "1"),
            ("Timeout", str(int(ring_seconds) * 1000)),
            ("CallerID", caller_id or "Alarm"),
            ("Async", "true"),
            ("Variable", f"ALARM_CALL_ID={call_id}"),
            ("Variable", f"ALARM_SOUND={sound}"),
            ("Variable", f"ALARM_ACK_SOUND={ack_sound}"),
        ])
        deadline = time.monotonic() + ring_seconds + max_seconds
        ami.sock.settimeout(5)
        while time.monotonic() < deadline:
            try:
                msg = ami.read()
            except socket.timeout:
                continue
            if msg.get("ActionID") == action_id and msg.get("Response") == "Error":
                raise AmiError(f"Oproep starten mislukt: {msg.get('Message')}")
            if msg.get("Event") == "OriginateResponse" and msg.get("ActionID") == action_id:
                if msg.get("Response") != "Success":
                    return _REASONS.get(int(msg.get("Reason") or 0), "failed")
            if msg.get("Event") == "UserEvent" and msg.get("UserEvent") == "AlarmCall" and msg.get("AlarmCallId") == call_id:
                return "ack" if msg.get("Result") == "ack" else "noack"
        return "failed"


def send_sms(cfg, number, text):
    """SMS through Asterisk: a GSM dongle (chan_dongle) or SIP MESSAGE over the trunk."""
    mode = cfg.get("sms_asterisk_mode", "dongle")
    digits = re.sub(r"[^\d+]", "", number)
    with Ami(cfg["ami_host"], cfg["ami_port"], cfg["ami_user"], cfg["ami_password"]) as ami:
        if mode == "dongle":
            fields = [("Action", "DongleSendSMS"), ("Device", cfg.get("sms_dongle_device") or "dongle0"),
                      ("Number", digits), ("Message", text)]
        else:
            domain = cfg.get("sms_sip_domain") or "localhost"
            fields = [("Action", "MessageSend"), ("Destination", f"pjsip:{cfg.get('sip_endpoint') or 'trunk'}/sip:{digits}@{domain}"),
                      ("From", cfg.get("sms_sip_from") or ""), ("Body", text)]
        res = ami.request(fields)
        if res.get("Response") != "Success":
            raise AmiError(f"SMS via Asterisk mislukt: {res.get('Message', res)}")
