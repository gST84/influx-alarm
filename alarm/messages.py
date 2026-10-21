"""Text for evaluation reasons, mails, SMS and spoken messages."""
import html
from datetime import datetime
from zoneinfo import ZoneInfo

UNIT_TAGS = {"unit", "units", "unit_of_measurement", "uom", "eenheid"}
NAME_TAGS = ["friendly_name", "name", "alias", "display_name", "sensor_name", "naam"]
ID_TAGS = ["entity_id", "sensor_id", "device_id", "sensor", "device", "id", "topic"]
# Home Assistant stores the unit as measurement name.
KNOWN_UNITS = {"W", "kW", "MW", "Wh", "kWh", "MWh", "V", "mV", "A", "mA", "Hz", "°C", "°F", "K", "%", "hPa", "mbar",
               "bar", "Pa", "lx", "ppm", "ppb", "µg/m³", "dB", "dBm", "m³", "L", "l", "mm", "m", "km/h", "m/s",
               "VA", "var", "s", "min", "h", "d", "€", "EUR"}
SPOKEN_UNITS = {"°C": "graden", "°F": "graden Fahrenheit", "%": "procent", "W": "watt", "kW": "kilowatt",
                "MW": "megawatt", "Wh": "wattuur", "kWh": "kilowattuur", "V": "volt", "mV": "millivolt",
                "A": "ampère", "mA": "milliampère", "Hz": "hertz", "hPa": "hectopascal", "mbar": "millibar",
                "bar": "bar", "lx": "lux", "ppm": "p p m", "m³": "kubieke meter", "L": "liter", "l": "liter",
                "km/h": "kilometer per uur", "m/s": "meter per seconde", "€": "euro", "EUR": "euro"}


def unit_of(check):
    for k, v in check["tags"].items():
        if k.lower() in UNIT_TAGS and v:
            return v
    return check["measurement"] if check["measurement"] in KNOWN_UNITS else ""


# Tag values that say nothing about which sensor it is.
PLACEHOLDERS = {"unknown", "none", "null", "n/a", "na", "-", "?", "onbekend"}


def _find(tags, names, suffixes=()):
    """First tag with one of the given names; else the first whose name ends in a suffix
    (e.g. "load_alias", "load_id")."""
    lower = {k.lower(): v for k, v in tags.items() if v and v.lower() not in PLACEHOLDERS}
    hit = next((lower[n] for n in names if n in lower), None)
    if hit is None:
        hit = next((v for k, v in sorted(lower.items()) if k.endswith(suffixes)), None) if suffixes else None
    return hit


def label_of(check):
    """Name shown to people: the user's label, else a name/id tag, else measurement · field."""
    if check.get("label"):
        return check["label"]
    name = _find(check["tags"], NAME_TAGS, ("alias", "_name", "naam"))
    ident = _find(check["tags"], ID_TAGS, ("_id",))
    if name and ident and name != ident:
        return f"{name} ({ident})"
    if name or ident:
        return name or ident
    # No usable name: the other tags tell series apart (unit left out, it is shown separately).
    extra = [f"{k}={v}" for k, v in sorted(check["tags"].items())
             if k.lower() not in UNIT_TAGS and v and v.lower() not in PLACEHOLDERS][:3]
    return f"{check['measurement']} · {check['field']}" + (f" ({', '.join(extra)})" if extra else "")


def fmt_num(v):
    if isinstance(v, bool):
        return "aan" if v else "uit"
    if isinstance(v, (int, float)):
        s = f"{v:.3f}".rstrip("0").rstrip(".")
        return s.replace(".", ",")
    return "" if v is None else str(v)


def fmt_value(v, unit=""):
    s = fmt_num(v)
    return f"{s} {unit}".strip() if unit and isinstance(v, (int, float)) and not isinstance(v, bool) else s


def fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 2 * 86400:
        h, m = divmod(round(seconds / 60), 60)
        return f"{h} u {m} min" if m else f"{h} u"
    return f"{round(seconds / 86400)} dagen"


def fmt_time(epoch, tz):
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%d/%m/%Y %H:%M:%S")


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def evaluate(check, last, extremes, now):
    """Status of one check: ok | pending | high | low | nodata, plus a reason in words.

    last:     (value, epoch) of the latest point, or None
    extremes: (value before the delay window, min in window, max in window) when delay > 0
    """
    unit = unit_of(check)
    nodata_s = (check.get("nodata_min") or 0) * 60
    if last is None:
        # The checker looks back 30 days for the last point.
        return ("nodata", "geen data in de laatste 30 dagen") if nodata_s else ("ok", "")
    value, t = last
    age = now - t
    if nodata_s and age > nodata_s:
        return "nodata", f"geen nieuwe data sinds {fmt_duration(age)} (max {fmt_duration(nodata_s)})"
    lower, upper = check.get("lower"), check.get("upper")
    if not is_number(value) or (lower is None and upper is None):
        return "ok", ""
    above = upper is not None and value > upper
    below = lower is not None and value < lower
    delay = (check.get("delay_min") or 0) * 60
    if delay <= 0 or extremes is None:
        if above:
            return "high", f"{fmt_value(value, unit)} boven de bovengrens {fmt_value(upper, unit)}"
        if below:
            return "low", f"{fmt_value(value, unit)} onder de ondergrens {fmt_value(lower, unit)}"
        return "ok", ""
    before, lo, hi = extremes
    if upper is not None and before is not None and before > upper and (lo is None or lo > upper):
        return "high", (f"{fmt_value(value, unit)} — al {fmt_duration(delay)} boven de bovengrens "
                        f"{fmt_value(upper, unit)}")
    if lower is not None and before is not None and before < lower and (hi is None or hi < lower):
        return "low", (f"{fmt_value(value, unit)} — al {fmt_duration(delay)} onder de ondergrens "
                       f"{fmt_value(lower, unit)}")
    if above or below:
        limit = upper if above else lower
        side = "boven de bovengrens" if above else "onder de ondergrens"
        return "pending", f"{fmt_value(value, unit)} {side} {fmt_value(limit, unit)}, nog geen {fmt_duration(delay)}"
    return "ok", ""


# --- notifications ------------------------------------------------------------------

TITLES = {"alarm": "ALARM", "reminder": "HERINNERING", "resolved": "OPGELOST", "test": "TEST"}


def rule_title(rule):
    """Rule name, prefixed with the organisation when there are several."""
    return f"{rule['org_name']} · {rule['name']}" if rule.get("org_name") else rule["name"]


def subject(kind, rule, items):
    n = len(items)
    if kind == "resolved":
        return f"[OPGELOST] {rule_title(rule)}: {n} waarde{'n' if n != 1 else ''} weer in orde"
    return f"[{TITLES[kind]}] {rule_title(rule)}: {n} waarde{'n' if n != 1 else ''} in alarm"


def mail_bodies(kind, rule, items, cfg):
    """(plain text, html). items: [{check, alarm}] with alarm fields kind/reason/value/started."""
    tz = cfg["timezone"]
    link = f"{cfg['public_url'].rstrip('/')}/#/rule/{rule['id']}"
    intro = {
        "alarm": "De volgende waarden staan in alarm.",
        "reminder": "Deze waarden staan nog steeds in alarm (herinnering).",
        "resolved": "Deze waarden zijn weer in orde.",
        "test": "Dit is een testbericht. Zo ziet een alarmmelding eruit.",
    }[kind]
    lines = [f"{TITLES[kind]} — {rule_title(rule)}", ""]
    if rule.get("message") and kind != "resolved":
        lines += [rule["message"], ""]
    lines += [intro, ""]
    rows_html = []
    for it in items:
        c, a = it["check"], it["alarm"]
        unit = unit_of(c)
        tags = ", ".join(f"{k}={v}" for k, v in sorted(c["tags"].items())) or "-"
        detail = [
            ("Waarde", fmt_value(_jval(c.get("last_value")), unit)),
            ("Reden", a.get("reason") or ""),
            ("Grenzen", f"onder {fmt_value(c.get('lower'), unit) or '-'} / boven {fmt_value(c.get('upper'), unit) or '-'}"),
            ("In alarm sinds", fmt_time(a.get("started"), tz)),
            ("Opgelost", fmt_time(a.get("resolved"), tz)) if kind == "resolved" else None,
            ("Laatste data", fmt_time(c.get("last_time"), tz)),
            ("Bron", f"{c['bucket']} / {c['measurement']} / {c['field']}"),
            ("Tags", tags),
        ]
        detail = [d for d in detail if d]
        lines.append(f"• {label_of(c)}")
        lines += [f"    {k}: {v}" for k, v in detail]
        lines.append("")
        rows_html.append(
            f"<h3 style='margin:16px 0 4px'>{html.escape(label_of(c))}</h3><table style='border-collapse:collapse'>"
            + "".join(f"<tr><td style='padding:2px 12px 2px 0;color:#666;vertical-align:top'>{html.escape(k)}</td>"
                      f"<td style='padding:2px 0'>{html.escape(str(v))}</td></tr>" for k, v in detail)
            + "</table>")
    lines += [f"Bekijk de regel: {link}"]
    color = {"alarm": "#d03b3b", "reminder": "#ec835a", "resolved": "#0ca30c", "test": "#2a78d6"}[kind]
    body_html = (
        f"<div style='font-family:system-ui,sans-serif;font-size:14px;color:#111'>"
        f"<div style='background:{color};color:#fff;padding:10px 14px;border-radius:6px;font-weight:600'>"
        f"{TITLES[kind]} — {html.escape(rule_title(rule))}</div>"
        + (f"<p style='font-size:15px'><b>{html.escape(rule['message'])}</b></p>" if rule.get("message") and kind != "resolved" else "")
        + f"<p>{intro}</p>{''.join(rows_html)}"
        f"<p style='margin-top:20px'><a href='{html.escape(link)}'>Bekijk de regel</a></p></div>")
    return "\n".join(lines), body_html


def sms_text(kind, rule, items, limit=320):
    head = f"{TITLES[kind]} {rule_title(rule)}"
    if rule.get("message") and kind != "resolved":
        head += f": {rule['message']}"
    parts = []
    for it in items:
        c, a = it["check"], it["alarm"]
        if kind == "resolved":
            parts.append(f"{label_of(c)} OK ({fmt_value(_jval(c.get('last_value')), unit_of(c))})")
        else:
            parts.append(f"{label_of(c)}: {a.get('reason')}")
    text = head
    for i, p in enumerate(parts):
        candidate = f"{text} | {p}"
        rest = len(parts) - i - 1
        if len(candidate) + (12 if rest else 0) > limit:
            return f"{text} | +{len(parts) - i} meer"
        text = candidate
    return text


def _spoken_value(v, unit):
    s = fmt_num(v)
    spoken_unit = SPOKEN_UNITS.get(unit, unit)
    return f"{s} {spoken_unit}".strip() if is_number(v) else s


def _spoken_name(check):
    return label_of(check).replace("_", " ").replace(".", " ")


def speech_text(kind, rule, items, max_items=3):
    where = f"{rule['org_name']}, {rule['name']}" if rule.get("org_name") else rule["name"]
    out = [f"{'Herinnering. ' if kind == 'reminder' else ''}Alarm voor {where}."]
    if rule.get("message"):
        out.append(rule["message"].rstrip(".") + ".")
    for it in items[:max_items]:
        c, a = it["check"], it["alarm"]
        unit = unit_of(c)
        name = _spoken_name(c)
        if a["kind"] == "nodata":
            out.append(f"{name}: geen nieuwe gegevens.")
        else:
            limit = c.get("upper") if a["kind"] == "high" else c.get("lower")
            side = "boven" if a["kind"] == "high" else "onder"
            out.append(f"{name} is {_spoken_value(_jval(c.get('last_value')), unit)}, {side} de grens van "
                       f"{_spoken_value(limit, unit)}.")
    if len(items) > max_items:
        out.append(f"En nog {len(items) - max_items} andere waarden.")
    out.append("Druk 1 om te bevestigen.")
    return " ".join(out)


def _jval(v):
    import json
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v
