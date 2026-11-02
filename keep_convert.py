"""Parse saved Keep activity JSON into TCX and FIT files.

Read Keep activity JSON from data/<user>/raw. Write TCX and FIT to
data/<user>/tcx and data/<user>/fit. The user name defaults to default.
"""

import base64
import json
import math
import os
import sys
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from Crypto.Cipher import AES
from eviltransform import gcj2wgs
from garmin_fit_sdk import Encoder, Profile

AES_KEY = base64.b64decode("NTZmZTU5OzgyZzpkODczYw==")
AES_IV = base64.b64decode("MjM0Njg5MjQzMjkyMDMwMA==")
ABSOLUTE_MILLISECONDS = 100_000_000_000
ABSOLUTE_DECISECONDS = 3_600_000
HEART_RATE_MATCH_SECONDS = 10
TRACK_MIN_METERS = 1
EARTH_RADIUS_M = 6_371_000
SEMICIRCLES_PER_DEGREE = 2**31 / 180
DEFAULT_USER = "default"
TCX_NAMESPACE = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"

SPORTS = {
    "outdoorRunning": "outdoor_run",
    "indoorRunning": "indoor_run",
    "outdoorCycling": "ride",
}
TCX_SPORT = {"outdoor_run": "Running", "ride": "Biking"}
FIT_SPORT = {
    "outdoor_run": ("running", "generic"),
    "indoor_run": ("running", "treadmill"),
    "ride": ("cycling", "generic"),
}
KIND_LABELS = {"outdoor_run": "室外跑", "indoor_run": "室内跑", "ride": "骑行"}


class Skip(Exception):
    """This file is saved Keep data, but not one of the three sports."""


@dataclass
class Sample:
    time: datetime
    lat: float | None = None
    lon: float | None = None
    altitude: float | None = None
    heart_rate: int | None = None
    distance_m: float = 0
    speed: float | None = None


@dataclass
class Activity:
    keep_id: str
    kind: str
    start: datetime
    end: datetime
    moving_s: float
    elapsed_s: float
    distance_m: float
    calories: int | None
    avg_heart_rate: int | None
    max_heart_rate: int | None
    timezone_name: str
    has_track: bool
    points: list[Sample] = field(default_factory=list)
    heart_rates: list[tuple[datetime, int]] = field(default_factory=list)


def point_epoch_seconds(timestamp, start_ms):
    """Keep stores three units in one timestamp field."""
    timestamp = float(timestamp or 0)
    if timestamp >= ABSOLUTE_MILLISECONDS:
        return timestamp / 1000
    if timestamp > ABSOLUTE_DECISECONDS:
        return timestamp / 10
    return start_ms / 1000 + timestamp / 10


def epoch_datetime(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def keep_id(raw_id):
    parts = str(raw_id).split("_")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return parts[0]


def decode_blob(text, encrypted):
    payload = base64.b64decode(text)
    if encrypted:
        payload = AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(payload)
    return json.loads(zlib.decompress(payload, 16 + zlib.MAX_WBITS))


def positive_heart_rate(value):
    if value is None:
        return None
    try:
        heart_rate = int(float(value))
    except (TypeError, ValueError):
        return None
    if heart_rate <= 0 or heart_rate > 255:
        return None
    return heart_rate


def haversine_m(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def segment_meters(previous, current):
    coords = (previous.lat, previous.lon, current.lat, current.lon)
    if any(value is None for value in coords):
        return 0.0
    return haversine_m(*coords)


def match_heart_rates(points, heart_rates):
    if not heart_rates:
        return
    for point in points:
        when, bpm = min(heart_rates, key=lambda item: abs((item[0] - point.time).total_seconds()))
        if abs((when - point.time).total_seconds()) <= HEART_RATE_MATCH_SECONDS:
            point.heart_rate = bpm


def assign_track_distance(points, official_m):
    if len(points) < 2:
        return 0.0
    cumulative = [0.0]
    for previous, current in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + segment_meters(previous, current))
    measured = cumulative[-1]
    scale = official_m / measured if measured > TRACK_MIN_METERS and official_m else 1
    for point, distance in zip(points, cumulative):
        point.distance_m = distance * scale
    for previous, current in zip(points, points[1:]):
        delta = (current.time - previous.time).total_seconds()
        if delta > 0:
            current.speed = (current.distance_m - previous.distance_m) / delta
    return measured


def elevation_gain(points):
    gain = 0.0
    previous = None
    for point in points:
        if point.altitude is None:
            continue
        if previous is not None and point.altitude > previous:
            gain += point.altitude - previous
        previous = point.altitude
    return gain


def resolve_zone(timezone_name):
    try:
        return ZoneInfo(timezone_name or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def timezone_offset(start, timezone_name):
    return start.astimezone(resolve_zone(timezone_name)).utcoffset() or timedelta(0)


def activity_data(payload):
    data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    if not isinstance(data, dict):
        raise ValueError("不是 Keep 活动 JSON")
    return data


def activity_kind(data):
    kind = SPORTS.get(data.get("dataType") or "")
    if kind is None:
        raise Skip(data.get("dataType") or "unknown")
    if data.get("isDoubtful"):
        raise Skip("doubtful")
    return kind


def activity_clock(data):
    start_ms = float(data.get("startTime") or 0)
    if not start_ms:
        raise ValueError("没有 startTime")
    moving_s = float(data.get("duration") or 0)
    if moving_s <= 0:
        raise Skip("duration")
    end_ms = float(data.get("endTime") or 0)
    elapsed_s = (end_ms - start_ms) / 1000 if end_ms > start_ms else moving_s
    elapsed_s = max(elapsed_s, moving_s)
    start = epoch_datetime(start_ms / 1000)
    return start_ms, start, start + timedelta(seconds=elapsed_s), moving_s, elapsed_s


def parse_heart_rates(heart, start_ms):
    blob = heart.get("heartRates")
    if not blob:
        return []
    samples = []
    for item in decode_blob(blob, encrypted=False):
        bpm = positive_heart_rate(item.get("beatsPerMinute"))
        if bpm is None:
            continue
        when = epoch_datetime(point_epoch_seconds(item.get("timestamp") or 0, start_ms))
        samples.append((when, bpm))
    samples.sort(key=lambda item: item[0])
    return samples


def track_sample(item, start_ms):
    timestamp = item.get("timestamp", item.get("unixTimestamp"))
    if timestamp is None:
        return None
    lat = item.get("latitude")
    lon = item.get("longitude")
    if lat is not None and lon is not None:
        lat, lon = gcj2wgs(float(lat), float(lon))
    altitude = item.get("altitude")
    if not isinstance(altitude, (int, float)):
        altitude = None
    return Sample(
        time=epoch_datetime(point_epoch_seconds(timestamp, start_ms)),
        lat=lat,
        lon=lon,
        altitude=altitude,
    )


def parse_track(data, start_ms):
    blob = data.get("geoPoints")
    if not blob:
        return []
    points = []
    for item in decode_blob(blob, encrypted=True):
        point = track_sample(item, start_ms)
        if point is not None:
            points.append(point)
    points.sort(key=lambda point: point.time)
    return points


def summarize_heart_rate(heart, points, series):
    average = positive_heart_rate(heart.get("averageHeartRate"))
    point_rates = [point.heart_rate for point in points if point.heart_rate]
    series_rates = [bpm for _, bpm in series]
    if average is None and (point_rates or series_rates):
        values = point_rates or series_rates
        average = int(round(sum(values) / len(values)))
    maximum = positive_heart_rate(heart.get("maxHeartRate"))
    if series_rates or point_rates:
        maximum = max([maximum or 0, *series_rates, *point_rates]) or None
    return average, maximum


def parse_calories(value):
    if value is None or float(value) < 0:
        return None
    return int(round(float(value)))


def parse_payload(payload):
    data = activity_data(payload)
    kind = activity_kind(data)
    start_ms, start, end, moving_s, elapsed_s = activity_clock(data)
    heart = data.get("heartRate") or {}
    heart_rates = parse_heart_rates(heart, start_ms)
    points = parse_track(data, start_ms)
    match_heart_rates(points, heart_rates)
    measured = assign_track_distance(points, float(data.get("distance") or 0))
    average, maximum = summarize_heart_rate(heart, points, heart_rates)
    return Activity(
        keep_id=keep_id(data.get("id") or ""),
        kind=kind,
        start=start,
        end=end,
        moving_s=moving_s,
        elapsed_s=elapsed_s,
        distance_m=float(data.get("distance") or 0),
        calories=parse_calories(data.get("calorie")),
        avg_heart_rate=average,
        max_heart_rate=maximum,
        timezone_name=data.get("timezone") or "Asia/Shanghai",
        has_track=measured > TRACK_MIN_METERS,
        points=points,
        heart_rates=heart_rates,
    )


def iter_raw_files(raw_dir):
    return sorted(path for path in Path(raw_dir).glob("*.json") if path.is_file())


def load_activities(raw_dir):
    activities = []
    skipped = Counter()
    failed = []
    for path in iter_raw_files(raw_dir):
        try:
            activities.append(parse_payload(json.loads(path.read_text(encoding="utf-8"))))
        except Skip as exc:
            skipped[str(exc)] += 1
        except Exception as exc:  # noqa: BLE001 - one bad file should not stop the batch
            failed.append(f"{path.name}: {exc}")
    return activities, skipped, failed


def tcx_time(value):
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def add_text(parent, tag, text):
    ET.SubElement(parent, tag).text = text


def add_heart_rate(parent, tag, bpm):
    node = ET.SubElement(parent, tag)
    add_text(node, "Value", str(bpm))


def add_position(parent, point):
    if point.lat is None or point.lon is None:
        return
    position = ET.SubElement(parent, "Position")
    add_text(position, "LatitudeDegrees", repr(point.lat))
    add_text(position, "LongitudeDegrees", repr(point.lon))


def add_trackpoint(track, point):
    trackpoint = ET.SubElement(track, "Trackpoint")
    add_text(trackpoint, "Time", tcx_time(point.time))
    add_position(trackpoint, point)
    if point.altitude is not None:
        add_text(trackpoint, "AltitudeMeters", repr(point.altitude))
    add_text(trackpoint, "DistanceMeters", f"{point.distance_m:.3f}")
    if point.heart_rate:
        add_heart_rate(trackpoint, "HeartRateBpm", point.heart_rate)


def add_lap(parent, activity):
    lap = ET.SubElement(parent, "Lap", {"StartTime": tcx_time(activity.start)})
    add_text(lap, "TotalTimeSeconds", f"{activity.moving_s:.3f}")
    add_text(lap, "DistanceMeters", f"{activity.distance_m:.3f}")
    add_text(lap, "Calories", str(activity.calories or 0))
    if activity.avg_heart_rate:
        add_heart_rate(lap, "AverageHeartRateBpm", activity.avg_heart_rate)
    add_text(lap, "Intensity", "Active")
    add_text(lap, "TriggerMethod", "Manual")
    if activity.has_track:
        track = ET.SubElement(lap, "Track")
        for point in activity.points:
            add_trackpoint(track, point)


def activity_to_tcx(activity):
    root = ET.Element(
        "TrainingCenterDatabase",
        {"xmlns": TCX_NAMESPACE, "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"},
    )
    activities = ET.SubElement(root, "Activities")
    activity_el = ET.SubElement(activities, "Activity", {"Sport": TCX_SPORT[activity.kind]})
    add_text(activity_el, "Id", tcx_time(activity.start))
    add_lap(activity_el, activity)
    xml = minidom.parseString(ET.tostring(root))
    return xml.toprettyxml(indent="  ", encoding="UTF-8")


def heart_rate_records(activity):
    span = max((activity.end - activity.start).total_seconds(), 1)
    margin = timedelta(seconds=HEART_RATE_MATCH_SECONDS)
    records = []
    for when, bpm in activity.heart_rates:
        if when < activity.start - margin or when > activity.end + margin:
            continue
        fraction = min(1, max(0, (when - activity.start).total_seconds() / span))
        records.append(Sample(time=when, heart_rate=bpm, distance_m=activity.distance_m * fraction))
    if len(records) < 2:
        return [
            Sample(time=activity.start, distance_m=0),
            Sample(time=activity.end, distance_m=activity.distance_m),
        ]
    return records


def collapse_records(records):
    by_second = {}
    for record in records:
        second = record.time.astimezone(timezone.utc).replace(microsecond=0)
        by_second[second] = record
    ordered = [by_second[second] for second in sorted(by_second)]
    for previous, current in zip(ordered, ordered[1:]):
        delta = (current.time - previous.time).total_seconds()
        if delta > 0:
            current.speed = max(0, (current.distance_m - previous.distance_m) / delta)
    return ordered


def fit_records(activity):
    records = activity.points if activity.has_track else heart_rate_records(activity)
    return collapse_records(records)


def semicircles(degrees):
    return int(degrees * SEMICIRCLES_PER_DEGREE)


def record_fields(record):
    fields = {"timestamp": record.time, "distance": max(record.distance_m, 0)}
    if record.lat is not None and record.lon is not None:
        fields["position_lat"] = semicircles(record.lat)
        fields["position_long"] = semicircles(record.lon)
    if record.altitude is not None:
        fields["enhanced_altitude"] = record.altitude
    if record.heart_rate:
        fields["heart_rate"] = record.heart_rate
    if record.speed is not None:
        fields["enhanced_speed"] = record.speed
    return fields


def lap_fields(activity, end):
    fields = {
        "timestamp": end,
        "start_time": activity.start,
        "total_elapsed_time": activity.elapsed_s,
        "total_timer_time": activity.moving_s,
        "total_distance": activity.distance_m,
    }
    if activity.avg_heart_rate:
        fields["avg_heart_rate"] = activity.avg_heart_rate
    if activity.max_heart_rate:
        fields["max_heart_rate"] = activity.max_heart_rate
    if activity.calories is not None:
        fields["total_calories"] = activity.calories
    if activity.moving_s:
        fields["enhanced_avg_speed"] = activity.distance_m / activity.moving_s
    gain = elevation_gain(activity.points) if activity.has_track else 0
    if gain:
        fields["total_ascent"] = round(gain)
    if activity.has_track and activity.points[0].lat is not None:
        fields["start_position_lat"] = semicircles(activity.points[0].lat)
        fields["start_position_long"] = semicircles(activity.points[0].lon)
    return fields


def write_fit_message(encoder, name, **fields):
    encoder.write_mesg({"mesg_num": Profile["mesg_num"][name], **fields})


def activity_to_fit(activity):
    sport, sub_sport = FIT_SPORT[activity.kind]
    records = fit_records(activity)
    end = records[-1].time if records else activity.end
    offset = timezone_offset(activity.start, activity.timezone_name)
    encoder = Encoder()
    write_fit_message(
        encoder,
        "FILE_ID",
        type="activity",
        manufacturer="development",
        product=0,
        time_created=activity.start,
    )
    write_fit_message(
        encoder,
        "DEVICE_INFO",
        device_index="creator",
        manufacturer="development",
        product=0,
        product_name="Keep",
        timestamp=activity.start,
    )
    write_fit_message(encoder, "EVENT", timestamp=records[0].time, event="timer", event_type="start")
    for record in records:
        write_fit_message(encoder, "RECORD", **record_fields(record))
    write_fit_message(encoder, "EVENT", timestamp=end, event="timer", event_type="stop_all")
    summary = lap_fields(activity, end)
    write_fit_message(encoder, "LAP", message_index=0, **summary)
    write_fit_message(
        encoder,
        "SESSION",
        message_index=0,
        sport=sport,
        sub_sport=sub_sport,
        first_lap_index=0,
        num_laps=1,
        **summary,
    )
    write_fit_message(
        encoder,
        "ACTIVITY",
        timestamp=end,
        num_sessions=1,
        local_timestamp=end + offset,
        total_timer_time=activity.moving_s,
    )
    return encoder.close()


def write_bytes(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def export_activity(activity, kind):
    if kind == "tcx":
        return activity_to_tcx(activity)
    return activity_to_fit(activity)


def accepts_kind(kind, output_kind):
    if output_kind == "tcx":
        return kind in TCX_SPORT
    return kind in FIT_SPORT


def user_name(argv, script):
    if len(argv) > 1:
        raise SystemExit(f"用法: python {script} [用户名]，默认 {DEFAULT_USER}")
    name = argv[0] if argv else DEFAULT_USER
    if name in {".", ".."} or any(part in name for part in ("/", "\\")):
        raise SystemExit("用户名无效")
    return name


def convert_directory(user, kind):
    root = Path("data") / user
    raw_dir = root / "raw"
    if not raw_dir.is_dir() or not iter_raw_files(raw_dir):
        raise SystemExit(f"目录里没有 JSON：{raw_dir}")
    output = root / kind
    output.mkdir(parents=True, exist_ok=True)
    activities, skipped, failed = load_activities(raw_dir)
    written = Counter()
    suffix = f".{kind}"
    for activity in activities:
        if not accepts_kind(activity.kind, kind):
            continue
        if not activity.keep_id:
            failed.append("一条记录没有 id")
            continue
        write_bytes(output / f"{activity.keep_id}{suffix}", export_activity(activity, kind))
        written[activity.kind] += 1
    label = kind.upper()
    counts = "，".join(f"{KIND_LABELS[name]} {written[name]}" for name in KIND_LABELS)
    print(f"{label} 已写入 {output}：{counts}。")
    if skipped:
        detail = "，".join(f"{name} {count}" for name, count in sorted(skipped.items()))
        print(f"跳过：{detail}")
    for item in failed:
        print(f"失败：{item}")
    if failed:
        raise SystemExit(1)


def main_tcx(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    convert_directory(user_name(argv, "convert_tcx.py"), "tcx")


def main_fit(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    convert_directory(user_name(argv, "convert_fit.py"), "fit")
