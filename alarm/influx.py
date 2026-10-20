"""InfluxDB 2 access over the HTTP API (Flux), based on influx_browser."""
import csv
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

RESERVED = {"", "result", "table", "_start", "_stop", "_time", "_value", "_field", "_measurement"}


class InfluxError(Exception):
    pass


def _request(cfg, path, params=None, body=None, timeout=60):
    url = cfg["influx_url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Token {cfg['influx_token']}", "Accept-Encoding": "identity"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/csv"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        try:
            msg = json.loads(msg).get("message", msg)
        except ValueError:
            pass
        raise InfluxError(f"InfluxDB: {msg}") from None
    except (urllib.error.URLError, OSError) as e:
        raise InfluxError(f"InfluxDB niet bereikbaar op {cfg['influx_url']}: {getattr(e, 'reason', e)}") from None


def _convert(value, dtype):
    if value == "":
        return None
    if dtype == "double":
        return float(value)
    if dtype in ("long", "unsignedLong"):
        return int(value)
    if dtype == "boolean":
        return value == "true"
    return value


def records(cfg, query):
    """Runs a Flux query and yields one dict per row (values typed via #datatype)."""
    resp = _request(cfg, "/api/v2/query", {"org": cfg["influx_org"]},
                    {"query": query, "type": "flux",
                     "dialect": {"header": True, "annotations": ["datatype"], "delimiter": ","}})
    with resp:
        reader = csv.reader(io.TextIOWrapper(resp, encoding="utf-8", newline=""))
        types, header, is_error = None, None, False
        for row in reader:
            if not row or row == [""]:
                types, header = None, None
                continue
            if row[0] == "#datatype":
                types, header = row, None
                continue
            if row[0].startswith("#"):
                continue
            if header is None:
                header = row
                is_error = header[1:3] == ["error", "reference"]
                continue
            if is_error:
                raise InfluxError(f"Flux-fout: {row[1]}")
            yield {h: _convert(v, t) for h, v, t in zip(header, row, types)} if types else dict(zip(header, row))


def fstr(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${") + '"'


def ftag(k):
    return f"r[{fstr(k)}]"


def parse_time(s):
    """RFC3339 (up to nanoseconds) → epoch seconds."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$", s.strip())
    if not m:
        raise ValueError(f"Ongeldige tijd: {s}")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(m.group(1) + (m.group(2) or "")[:7] + tz).timestamp()


def ftime(epoch):
    return 'time(v: "' + datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") + '")'


def tags_of(rec):
    return {k: str(v) for k, v in rec.items() if k not in RESERVED and v not in (None, "")}


def kind_of(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


# --- browsing ----------------------------------------------------------------

def buckets(cfg):
    out, offset = [], 0
    while True:
        params = {"limit": 100, "offset": offset}
        if cfg["influx_org"]:
            params["org"] = cfg["influx_org"]
        with _request(cfg, "/api/v2/buckets", params) as resp:
            page = json.load(resp).get("buckets", [])
        out += [{"name": b["name"], "system": b.get("type") == "system"} for b in page]
        if len(page) < 100:
            return sorted(out, key=lambda b: (b["system"], b["name"].lower()))
        offset += 100


def _range_args(bucket, start, stop):
    return f"bucket: {fstr(bucket)}, start: {ftime(start)}, stop: {ftime(stop)}"


def measurements(cfg, bucket, start, stop):
    query = f'import "influxdata/influxdb/schema"\nschema.measurements({_range_args(bucket, start, stop)})'
    return sorted({str(r["_value"]) for r in records(cfg, query)}, key=str.lower)


def schema(cfg, bucket, measurement, start, stop):
    tag_query = ('import "influxdata/influxdb/schema"\n'
                 f"schema.measurementTagKeys({_range_args(bucket, start, stop)}, measurement: {fstr(measurement)})")
    tags = sorted((str(r["_value"]) for r in records(cfg, tag_query) if r["_value"] not in RESERVED), key=str.lower)
    field_query = (f"from(bucket: {fstr(bucket)})\n  |> range(start: {ftime(start)}, stop: {ftime(stop)})\n"
                   f"  |> filter(fn: (r) => r._measurement == {fstr(measurement)})\n  |> last()\n"
                   '  |> keep(columns: ["_field", "_value"])')
    fields = {}
    for r in records(cfg, field_query):
        t = kind_of(r.get("_value"))
        if fields.get(r["_field"]) != "string":
            fields[r["_field"]] = t
    return {"fields": [{"name": k, "type": fields[k]} for k in sorted(fields, key=str.lower)], "tags": tags}


def tag_values(cfg, bucket, measurement, tag, start, stop):
    query = ('import "influxdata/influxdb/schema"\n'
             f"schema.measurementTagValues({_range_args(bucket, start, stop)}, measurement: {fstr(measurement)}, tag: {fstr(tag)})")
    return sorted({str(r["_value"]) for r in records(cfg, query)}, key=str.lower)


def selection_filter(selections):
    """[{measurement, fields: [names], tags: {key: [values]}}] → Flux filter expression."""
    parts = []
    for sel in selections:
        if not sel.get("fields"):
            continue
        cond = [f"r._measurement == {fstr(sel['measurement'])}",
                "(" + " or ".join(f"r._field == {fstr(f)}" for f in sel["fields"]) + ")"]
        for tag, values in (sel.get("tags") or {}).items():
            if values:
                cond.append("(" + " or ".join(f"{ftag(tag)} == {fstr(v)}" for v in values) + ")")
        parts.append("(" + " and ".join(cond) + ")")
    return " or ".join(parts)


def resolve_series(cfg, bucket, selections, start, stop):
    """The individual series a selection matches, each with its last value."""
    flt = selection_filter(selections)
    if not flt:
        return []
    query = (f"from(bucket: {fstr(bucket)})\n  |> range(start: {ftime(start)}, stop: {ftime(stop)})\n"
             f"  |> filter(fn: (r) => {flt})\n  |> last()")
    out = []
    for r in records(cfg, query):
        out.append({"bucket": bucket, "measurement": r["_measurement"], "field": r["_field"], "tags": tags_of(r),
                    "kind": kind_of(r.get("_value")), "value": r.get("_value"), "time": parse_time(r["_time"])})
    return sorted(out, key=lambda s: (s["measurement"], s["field"], sorted(s["tags"].items())))


# --- checking ------------------------------------------------------------------

def series_filter(check):
    """Exact filter for one checked series (all its tags)."""
    cond = [f"r._measurement == {fstr(check['measurement'])}", f"r._field == {fstr(check['field'])}"]
    cond += [f"{ftag(k)} == {fstr(v)}" for k, v in sorted(check["tags"].items())]
    return " and ".join(cond)


def matches(check, rec):
    return (rec.get("_measurement") == check["measurement"] and rec.get("_field") == check["field"]
            and all(str(rec.get(k)) == v for k, v in check["tags"].items()))


def last_points(cfg, bucket, checks, lookback_s, now):
    """{check id: (value, epoch)} with the latest point of each check within the lookback."""
    if not checks:
        return {}
    flt = " or ".join(f"({series_filter(c)})" for c in checks)
    query = (f"from(bucket: {fstr(bucket)})\n  |> range(start: {ftime(now - lookback_s)}, stop: {ftime(now + 60)})\n"
             f"  |> filter(fn: (r) => {flt})\n  |> last()")
    out = {}
    for r in records(cfg, query):
        t = parse_time(r["_time"])
        for c in checks:
            if matches(c, r) and (c["id"] not in out or out[c["id"]][1] < t):
                out[c["id"]] = (r.get("_value"), t)
    return out


def window_extremes(cfg, bucket, check, start, now, lookback_s):
    """(value just before `start`, min within [start, now], max within [start, now]).

    Home Assistant and similar writers only store a point when the value changes, so a value
    that stays out of bounds can have no points inside the window: the value before the window
    counts too.
    """
    base = (f"from(bucket: {fstr(bucket)})\n  |> range(start: {{start}}, stop: {{stop}})\n"
            f"  |> filter(fn: (r) => {series_filter(check)})\n")
    before = [r.get("_value") for r in records(cfg, base.format(start=ftime(now - lookback_s), stop=ftime(start)) + "  |> last()")]
    lo = [r.get("_value") for r in records(cfg, base.format(start=ftime(start), stop=ftime(now + 60)) + "  |> min()")]
    hi = [r.get("_value") for r in records(cfg, base.format(start=ftime(start), stop=ftime(now + 60)) + "  |> max()")]
    nums = lambda xs: [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    b, lo, hi = nums(before), nums(lo), nums(hi)
    return (b[-1] if b else None, min(lo) if lo else None, max(hi) if hi else None)
