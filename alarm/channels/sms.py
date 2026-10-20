"""SMS through a configurable HTTP API, Twilio, or Asterisk."""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import ami


class SmsError(Exception):
    pass


def _http(url, data=None, headers=None, method="POST"):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise SmsError(f"SMS-provider antwoordde {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    except (urllib.error.URLError, OSError) as e:
        raise SmsError(f"SMS-provider niet bereikbaar: {getattr(e, 'reason', e)}") from None


def _fill(template, to, message, escape):
    digits = re.sub(r"[^\d+]", "", to)
    values = {"to": digits, "to_digits": digits.lstrip("+"), "message": message}
    return re.sub(r"\{(to|to_digits|message)\}", lambda m: escape(values[m.group(1)]), template)


def send(cfg, to, text):
    provider = cfg.get("sms_provider", "none")
    if not to:
        raise SmsError("Geen telefoonnummer")
    if to.lower().startswith("test:"):
        return  # simulated phones only exist for calls
    if provider == "none":
        raise SmsError("Geen SMS-provider ingesteld (Instellingen → SMS)")
    if provider == "asterisk":
        try:
            ami.send_sms(cfg, to, text)
        except ami.AmiError as e:
            raise SmsError(str(e)) from None
        return
    if provider == "twilio":
        sid = cfg.get("twilio_sid", "")
        auth = base64.b64encode(f"{sid}:{cfg.get('twilio_token', '')}".encode()).decode()
        data = urllib.parse.urlencode({"To": re.sub(r"[^\d+]", "", to), "From": cfg.get("twilio_from", ""), "Body": text})
        _http(f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json", data.encode(),
              {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
        return
    # Generic HTTP: placeholders {to}, {to_digits} and {message} in URL and body.
    url = _fill(cfg.get("sms_http_url", ""), to, text, urllib.parse.quote)
    if not url:
        raise SmsError("Geen URL voor de SMS-API ingesteld")
    content_type = cfg.get("sms_http_content_type") or "application/json"
    if "json" in content_type:
        escape = lambda s: json.dumps(s)[1:-1]
    elif "x-www-form-urlencoded" in content_type:
        escape = urllib.parse.quote
    else:
        escape = lambda s: s
    method = (cfg.get("sms_http_method") or "POST").upper()
    body = _fill(cfg.get("sms_http_body", ""), to, text, escape).encode() if method != "GET" else None
    headers = {"Content-Type": content_type} if body else {}
    for line in (cfg.get("sms_http_headers") or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    _http(url, body, headers, method)
