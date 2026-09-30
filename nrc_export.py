#!/usr/bin/env python3
"""Export Nike Run Club runs to GPX/TCX files that can be uploaded to Strava.

Runs with GPS data are written as GPX (Strava uses the GPX <name> as the
activity title). Runs without GPS (treadmill/indoor) are written as TCX so the
distance is kept.

Standard library only. See README.md for how to get an access token.
"""

import argparse
import bisect
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

LIST_URL = "https://api.nike.com/plus/v3/activities/before_id/v3/{before_id}"
DETAIL_URL = "https://api.nike.com/sport/v3/me/activity/{id}?metrics=ALL"
RUN_TYPES = {"run", "jogging"}


# --------------------------------------------------------------------------- #
# Nike API
# --------------------------------------------------------------------------- #

class AuthError(Exception):
    pass


def api_get(url, token, retries=3):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (nrc-export)",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(
                    "Nike API returned HTTP %d: the token is invalid or expired. "
                    "Grab a fresh one and re-run (already downloaded runs are cached)." % e.code)
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("Giving up on " + url)


def list_activity_ids(token):
    """Yield (id, type) for every activity, newest first."""
    before_id = "*"
    while before_id:
        url = LIST_URL.format(before_id=urllib.parse.quote(before_id, safe="*")) + "?" + urllib.parse.urlencode({
            "limit": 30,
            "types": "run,jogging",
            "include_deleted": "false",
        })
        page = api_get(url, token)
        activities = page.get("activities") or []
        for a in activities:
            yield a["id"], a.get("type")
        if not activities:
            break
        before_id = (page.get("paging") or {}).get("before_id")


def download(token, raw_dir):
    os.makedirs(raw_dir, exist_ok=True)
    ids = list(list_activity_ids(token))
    print("Found %d activities" % len(ids))
    for i, (aid, atype) in enumerate(ids, 1):
        path = os.path.join(raw_dir, aid + ".json")
        if os.path.exists(path):
            continue
        if atype and atype not in RUN_TYPES:
            continue
        print("  [%d/%d] downloading %s" % (i, len(ids), aid))
        data = api_get(DETAIL_URL.format(id=aid), token)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        time.sleep(0.3)


# --------------------------------------------------------------------------- #
# Conversion helpers
# --------------------------------------------------------------------------- #

def iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metric_values(activity, mtype):
    for m in activity.get("metrics") or []:
        if m.get("type") == mtype:
            return sorted(m.get("values") or [], key=lambda v: v["start_epoch_ms"])
    return []


def summary_value(activity, metric, summary="total"):
    for s in activity.get("summaries") or []:
        if s.get("metric") == metric and s.get("summary") == summary:
            return s.get("value")
    return None


class Series:
    """Timestamped values with nearest-sample lookup."""

    def __init__(self, points, max_gap_ms):
        self.points = sorted(points)
        self.times = [p[0] for p in self.points]
        self.max_gap_ms = max_gap_ms

    def at(self, t):
        if not self.points:
            return None
        i = bisect.bisect_left(self.times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.points):
                if best is None or abs(self.times[j] - t) < abs(self.times[best] - t):
                    best = j
        if abs(self.times[best] - t) > self.max_gap_ms:
            return None
        return self.points[best][1]


def cumulative_distance(activity):
    """NRC distance samples are per-interval km; return [(end_ms, meters)]."""
    total = 0.0
    out = []
    for v in metric_values(activity, "distance"):
        total += float(v["value"]) * 1000.0
        out.append((v.get("end_epoch_ms", v["start_epoch_ms"]), total))
    return out


def build_points(activity):
    """Return a list of dicts: t, lat, lon, ele, hr, dist."""
    lats = {v["start_epoch_ms"]: v["value"] for v in metric_values(activity, "latitude")}
    lons = {v["start_epoch_ms"]: v["value"] for v in metric_values(activity, "longitude")}
    ele = Series([(v["start_epoch_ms"], v["value"]) for v in metric_values(activity, "elevation")], 60000)
    hr = Series([(v["start_epoch_ms"], v["value"]) for v in metric_values(activity, "heart_rate")], 30000)
    dist_pts = cumulative_distance(activity)
    dist = Series(dist_pts, 120000)

    gps_times = sorted(set(lats) & set(lons))
    points = []
    if gps_times:
        for t in gps_times:
            points.append({"t": t, "lat": lats[t], "lon": lons[t],
                           "ele": ele.at(t), "hr": hr.at(t), "dist": dist.at(t)})
    elif dist_pts:
        start = activity["start_epoch_ms"]
        points.append({"t": start, "dist": 0.0, "hr": hr.at(start)})
        for t, d in dist_pts:
            points.append({"t": t, "dist": d, "hr": hr.at(t)})
    return points, bool(gps_times)


def activity_name(activity):
    tags = activity.get("tags") or {}
    name = tags.get("com.nike.name")
    if name:
        return name
    dt = datetime.fromtimestamp(activity["start_epoch_ms"] / 1000.0)
    return "Run " + dt.strftime("%Y-%m-%d %H:%M")


def slug(text):
    s = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-")
    return s[:50] or "run"


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #

def to_gpx(activity, points):
    name = activity_name(activity)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="nrc-export" xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        "  <metadata><time>%s</time></metadata>" % iso(activity["start_epoch_ms"]),
        "  <trk>",
        "    <name>%s</name>" % escape(name),
        "    <type>running</type>",
        "    <trkseg>",
    ]
    for p in points:
        out.append('      <trkpt lat="%.7f" lon="%.7f">' % (p["lat"], p["lon"]))
        if p.get("ele") is not None:
            out.append("        <ele>%.1f</ele>" % p["ele"])
        out.append("        <time>%s</time>" % iso(p["t"]))
        if p.get("hr") is not None:
            out.append("        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>%d</gpxtpx:hr>"
                       "</gpxtpx:TrackPointExtension></extensions>" % round(p["hr"]))
        out.append("      </trkpt>")
    out += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(out)


def to_tcx(activity, points):
    start = activity["start_epoch_ms"]
    duration_ms = activity.get("active_duration_ms") or (activity["end_epoch_ms"] - start)
    total_km = summary_value(activity, "distance")
    total_m = float(total_km) * 1000.0 if total_km is not None else (points[-1]["dist"] if points else 0.0)
    calories = summary_value(activity, "calories") or 0

    if not points:
        # Manual entry: synthesize start/end so Strava gets time + distance.
        points = [{"t": start, "dist": 0.0}, {"t": activity["end_epoch_ms"], "dist": total_m}]

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">',
        "  <Activities>",
        '    <Activity Sport="Running">',
        "      <Id>%s</Id>" % iso(start),
        "      <Lap StartTime=%s>" % quoteattr(iso(start)),
        "        <TotalTimeSeconds>%.1f</TotalTimeSeconds>" % (duration_ms / 1000.0),
        "        <DistanceMeters>%.1f</DistanceMeters>" % total_m,
        "        <Calories>%d</Calories>" % round(float(calories)),
        "        <Intensity>Active</Intensity>",
        "        <TriggerMethod>Manual</TriggerMethod>",
        "        <Track>",
    ]
    for p in points:
        out.append("          <Trackpoint>")
        out.append("            <Time>%s</Time>" % iso(p["t"]))
        if p.get("lat") is not None:
            out.append("            <Position><LatitudeDegrees>%.7f</LatitudeDegrees>"
                       "<LongitudeDegrees>%.7f</LongitudeDegrees></Position>" % (p["lat"], p["lon"]))
        if p.get("ele") is not None:
            out.append("            <AltitudeMeters>%.1f</AltitudeMeters>" % p["ele"])
        if p.get("dist") is not None:
            out.append("            <DistanceMeters>%.1f</DistanceMeters>" % p["dist"])
        if p.get("hr") is not None:
            out.append("            <HeartRateBpm><Value>%d</Value></HeartRateBpm>" % round(p["hr"]))
        out.append("          </Trackpoint>")
    out += [
        "        </Track>",
        "      </Lap>",
        "      <Notes>%s</Notes>" % escape(activity_name(activity)),
        "    </Activity>",
        "  </Activities>",
        "</TrainingCenterDatabase>",
        "",
    ]
    return "\n".join(out)


def convert(raw_dir, out_dir, fmt):
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(raw_dir) if f.endswith(".json"))
    written = skipped = 0
    for fname in files:
        with open(os.path.join(raw_dir, fname), encoding="utf-8") as f:
            activity = json.load(f)
        if activity.get("type") not in RUN_TYPES or activity.get("deleted"):
            skipped += 1
            continue
        points, has_gps = build_points(activity)
        use_gpx = fmt == "gpx" or (fmt == "auto" and has_gps)
        if use_gpx and not has_gps:
            print("  %s has no GPS data, writing TCX instead" % fname)
            use_gpx = False
        dt = datetime.fromtimestamp(activity["start_epoch_ms"] / 1000.0)
        base = "%s_%s" % (dt.strftime("%Y-%m-%d_%H%M"), slug(activity_name(activity)))
        ext = ".gpx" if use_gpx else ".tcx"
        content = to_gpx(activity, points) if use_gpx else to_tcx(activity, points)
        with open(os.path.join(out_dir, base + ext), "w", encoding="utf-8") as f:
            f.write(content)
        written += 1
    print("Wrote %d files to %s (%d non-run/deleted skipped)" % (written, out_dir, skipped))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Export Nike Run Club runs to GPX/TCX for Strava.")
    ap.add_argument("--token", help="Nike API bearer token (or set NRC_TOKEN; prompted otherwise)")
    ap.add_argument("--raw-dir", default="raw", help="where raw activity JSON is cached (default: raw)")
    ap.add_argument("--out", default="export", help="output directory (default: export)")
    ap.add_argument("--format", choices=["auto", "gpx", "tcx"], default="auto",
                    help="auto = GPX for GPS runs, TCX for indoor runs (default)")
    ap.add_argument("--offline", action="store_true",
                    help="skip downloading, only convert what is already in --raw-dir")
    args = ap.parse_args()

    if not args.offline:
        token = args.token or os.environ.get("NRC_TOKEN") or getpass.getpass("Nike access token: ")
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        try:
            download(token, args.raw_dir)
        except AuthError as e:
            print("Error: %s" % e, file=sys.stderr)
            if not os.path.isdir(args.raw_dir):
                return 1
            print("Converting the runs downloaded so far...", file=sys.stderr)

    if not os.path.isdir(args.raw_dir):
        print("No raw data found in %s" % args.raw_dir, file=sys.stderr)
        return 1
    convert(args.raw_dir, args.out, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
