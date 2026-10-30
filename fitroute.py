"""Graft a GPX route's shape onto a FIT file's records, matched by distance.

For a ride whose GPS is unusable but whose wheel sensor is fine, and where the
donor is a planned route rather than a recording. A route has no timestamps,
so `fitgraft.py` has nothing to match on; what both files do share is distance
travelled. Each record is placed at its own odometer reading along the route.

Only positions change, in place: the records', and the lap and session start,
end and bounding-box positions derived from them. Speed, distance, power,
heart rate, cadence, altitude, temperature, lap and session totals and every
private message stay exactly as recorded.
"""

import argparse
import bisect
import math
import statistics
import struct
import sys
import xml.etree.ElementTree as ElementTree
from itertools import pairwise

from fitshift import RECORD, TIMESTAMP, crc16, walk

EARTH_RADIUS = 6371000.0

# FIT stores latitude and longitude as semicircles: degrees * 2^31 / 180.
SEMICIRCLES = 2**31 / 180.0

POSITION_LAT = 0
POSITION_LONG = 1
ALTITUDE = 2
DISTANCE = 5
ENHANCED_ALTITUDE = 78
UINT16 = 4
SINT32 = 5
UINT32 = 6

# GPS jamming writes 0x7FFFFFFF semicircles rather than omitting the field.
INVALID = 0x7FFFFFFF

# A fix further than this from the route is spoofed, not ridden.
ON_ROUTE = 25.0

# How far behind and ahead of the last trusted fix to look for the next one,
# so a route that passes the same street twice cannot capture it.
SEARCH_BACK = 300.0
SEARCH_AHEAD = 1500.0

# A fix nearer than this to anywhere on the route is real even when it is off
# the route: a detour or a parallel road. Spoofed fixes land far further away.
SPOOFED = 2000.0

# Climbs are compared over a window of this much ride, one window per step,
# trying every lag up to HILL_LAG either way. A window is trusted only with
# enough relief to go on and a near-perfect match of shape.
HILL_WINDOW = 3000.0
HILL_STEP = 1000.0
HILL_LAG = 300
HILL_SAMPLE = 10
HILL_RELIEF = 15.0
HILL_MATCH = 0.95

SESSION = 18
LAP = 19
START_TIME = 2

# Where each summary keeps its positions: start lat/long, end lat/long, and
# the north-east and south-west corners of its bounding box.
SUMMARY_FIELDS = {
    SESSION: (3, 4, 38, 39, 29, 30, 31, 32),
    LAP: (3, 4, 5, 6, 27, 28, 29, 30),
}


def haversine(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(a))


def read_route(path):
    """Return the route's track points as [(lat, lon), ...]."""
    points = []
    for element in ElementTree.parse(path).iter():
        # Strip the GPX namespace: {http://www.topografix.com/GPX/1/1}trkpt.
        if element.tag.rpartition('}')[2] != 'trkpt':
            continue
        points.append((float(element.get('lat')), float(element.get('lon'))))
    if len(points) < 2:
        raise ValueError(f'{path}: need at least two track points')
    return points


def read_elevations(path):
    """Return the route's <ele> per track point, or None if any is missing."""
    elevations = []
    for element in ElementTree.parse(path).iter():
        if element.tag.rpartition('}')[2] != 'trkpt':
            continue
        ele = next(
            (child.text for child in element if child.tag.rpartition('}')[2] == 'ele'),
            None,
        )
        if ele is None:
            return None
        elevations.append(float(ele))
    return elevations


def measure(points):
    """Return cumulative distance along points, one entry per point."""
    cumulative = [0.0]
    for first, second in pairwise(points):
        cumulative.append(cumulative[-1] + haversine(first, second))
    return cumulative


def locate(points, cumulative, target):
    """Interpolate the position that lies target metres along the route."""
    index = bisect.bisect_right(cumulative, target) - 1
    index = max(0, min(index, len(points) - 2))
    span = cumulative[index + 1] - cumulative[index]
    fraction = 0.0 if span <= 0 else (target - cumulative[index]) / span
    fraction = max(0.0, min(1.0, fraction))
    lat1, lon1 = points[index]
    lat2, lon2 = points[index + 1]
    return lat1 + (lat2 - lat1) * fraction, lon1 + (lon2 - lon1) * fraction


def project(points, cumulative, fix, low, high):
    """Return (metres off route, metres along it) for the nearest point.

    Only route segments that overlap [low, high] metres along are considered.
    """
    best = (math.inf, None)
    first = max(0, bisect.bisect_right(cumulative, low) - 1)
    last = min(len(points) - 1, bisect.bisect_right(cumulative, high))
    # An equirectangular plane around the fix is exact enough at this scale.
    lat0, lon0 = fix
    x_scale = math.radians(EARTH_RADIUS) * math.cos(math.radians(lat0))
    y_scale = math.radians(EARTH_RADIUS)
    for index in range(first, last):
        (lat1, lon1), (lat2, lon2) = points[index], points[index + 1]
        ax, ay = (lon1 - lon0) * x_scale, (lat1 - lat0) * y_scale
        dx, dy = (lon2 - lon1) * x_scale, (lat2 - lat1) * y_scale
        length = dx * dx + dy * dy
        fraction = (
            0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
        )
        off = math.hypot(ax + fraction * dx, ay + fraction * dy)
        if off < best[0]:
            span = cumulative[index + 1] - cumulative[index]
            best = (off, cumulative[index] + fraction * span)
    return best


def anchor(records, points, cumulative, ratio=1.0):
    """Return [(ride metres, route metres), ...] for the trusted fixes.

    A fix is trusted when it lies on the route, which rejects both the
    jammer's sentinel and the coordinates it spoofs. Anchors only move
    forward, so GPS jitter at a standstill cannot fold the ride back.
    """
    anchors = []
    along = ridden = 0.0
    for record in records:
        if record['distance'] is None or record['fix'] is None:
            continue
        # Dead-reckon from the last trusted fix, so that after a long gap the
        # search is still where the rider is: on a loop the finish passes the
        # start, and a window left at the start would trust it there.
        expected = along + (record['distance'] - ridden) * ratio
        off, along_fix = project(
            points,
            cumulative,
            record['fix'],
            expected - SEARCH_BACK,
            expected + SEARCH_AHEAD,
        )
        if off > ON_ROUTE:
            continue
        record['trusted'] = True
        along, ridden = along_fix, record['distance']
        if not anchors or (
            record['distance'] > anchors[-1][0] and along > anchors[-1][1]
        ):
            anchors.append((record['distance'], along))
    return anchors


def keep_real(records, points, cumulative):
    """Mark the real fixes that lie off the route kept; return how many.

    They are not anchors: an off-route fix has no place along the route.
    """
    kept = 0
    for record in records:
        if record['trusted'] or record['distance'] is None or record['fix'] is None:
            continue
        off, _ = project(points, cumulative, record['fix'], 0, cumulative[-1])
        if off > SPOOFED:
            continue
        record['trusted'] = True
        kept += 1
    return kept


def retime(times, distances, limit):
    """Return (distances rebuilt from the deltas that are a pace, jumps, dropped).

    Jamming inflates distance as well as moving the fix: a watch with no
    wheel sensor or footpod integrates the jumps it is fed, and a minute of
    them adds kilometres that were never covered. A delta implying more than
    limit metres a second was not covered, so it takes the median speed of
    the deltas that were, held over the same seconds. jumps are the indices
    whose delta was replaced, dropped the inflation they carried.
    """
    deltas = [
        (second - first, there - here)
        for (first, second), (here, there) in zip(
            pairwise(times), pairwise(distances), strict=True
        )
    ]
    speeds = [
        metres / seconds
        for seconds, metres in deltas
        if seconds > 0 and 0 <= metres <= limit * seconds
    ]
    if not speeds:
        raise ValueError(f'no delta under {limit:.2f} m/s to rebuild the distance from')
    median = statistics.median(speeds)

    rebuilt = [distances[0]]
    jumps = []
    dropped = 0.0
    for index, (seconds, metres) in enumerate(deltas, start=1):
        step = metres
        if seconds <= 0:
            step = 0.0
        elif not 0 <= metres <= limit * seconds:
            dropped += metres - median * seconds
            step = median * seconds
            jumps.append(index)
        rebuilt.append(rebuilt[-1] + step)
    return rebuilt, jumps, dropped


def interpolate(anchors, distance):
    """Map a ride distance to a route distance through the anchors.

    Past either end the nearest anchor's offset carries on unchanged.
    """
    rides = [ride for ride, _ in anchors]
    index = bisect.bisect_right(rides, distance) - 1
    if index < 0:
        return distance + anchors[0][1] - anchors[0][0]
    if index == len(anchors) - 1:
        return distance + anchors[-1][1] - anchors[-1][0]
    (ride1, route1), (ride2, route2) = anchors[index], anchors[index + 1]
    return route1 + (route2 - route1) * (distance - ride1) / (ride2 - ride1)


def height(cumulative, elevations, target):
    """Interpolate the route's elevation target metres along it."""
    index = bisect.bisect_right(cumulative, target) - 1
    index = max(0, min(index, len(cumulative) - 2))
    span = cumulative[index + 1] - cumulative[index]
    fraction = 0.0 if span <= 0 else (target - cumulative[index]) / span
    fraction = max(0.0, min(1.0, fraction))
    return elevations[index] + (elevations[index + 1] - elevations[index]) * fraction


def align(records, anchors, cumulative, elevations):
    """Return anchors plus those the climbs pin down between them.

    Between trusted fixes the placement rests on the wheel sensor alone. The
    barometric altitude says where the ride really was: where its climbs sit
    a consistent lag off the route's <ele>, the placement is off by that lag.
    Only the shape is compared, so the altimeter's absolute offset is moot,
    and a flat window has nothing to go on and adds no anchor.
    """
    profile = [
        (r['distance'], r['altitude'])
        for r in records
        if r['distance'] is not None and r['altitude'] is not None
    ]
    if not profile:
        return anchors
    distances = [distance for distance, _ in profile]
    route_length = cumulative[-1]

    def altitude(distance):
        index = max(0, bisect.bisect_right(distances, distance) - 1)
        return profile[index][1]

    added = []
    for (ride1, _), (ride2, _) in pairwise(anchors):
        centre = ride1 + HILL_STEP
        while centre < ride2 - HILL_STEP:
            low = max(0.0, centre - HILL_WINDOW / 2)
            samples = [
                low + step * HILL_SAMPLE
                for step in range(
                    int(min(HILL_WINDOW, distances[-1] - low) // HILL_SAMPLE)
                )
            ]
            ride = [altitude(distance) for distance in samples]
            centre += HILL_STEP
            if len(samples) < 2 or max(ride) - min(ride) < HILL_RELIEF:
                continue
            placed = [interpolate(anchors, distance) for distance in samples]
            best = (-1.0, 0)
            for lag in range(-HILL_LAG, HILL_LAG + 1, HILL_SAMPLE):
                route = [
                    height(
                        cumulative, elevations, min(max(at + lag, 0.0), route_length)
                    )
                    for at in placed
                ]
                try:
                    best = max(best, (statistics.correlation(ride, route), lag))
                except statistics.StatisticsError:
                    continue
            match, lag = best
            if match >= HILL_MATCH:
                middle = centre - HILL_STEP
                added.append((middle, interpolate(anchors, middle) + lag))

    # Keep only anchors that move forward on both axes, so the ride never
    # folds back on itself.
    merged = []
    for ride, route in sorted(anchors + added):
        if not merged or (ride > merged[-1][0] and route > merged[-1][1]):
            merged.append((ride, route))
    return merged


def read_records(data):
    """Return (records, data_end).

    One record per record message, holding its distance in metres, its fix in
    degrees (None when invalid) and the offsets where its position fields
    live, or None when the record was written without them.
    """
    records = []

    def start(global_num):
        if global_num != RECORD:
            return
        records.append(
            {
                'stamp': None,
                'distance': None,
                'altitude': None,
                'lat': None,
                'lon': None,
                'fix': None,
                'trusted': False,
                'endian': '<',
            }
        )

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if global_num != RECORD or developer or not records:
            return
        current = records[-1]
        current['endian'] = endian
        if number == TIMESTAMP and size == 4 and base_type & 0x1F == UINT32:
            current['stamp'] = struct.unpack(endian + 'I', data[pos : pos + 4])[0]
        elif number == DISTANCE and size == 4 and base_type & 0x1F == UINT32:
            # record.distance is scaled by 100, so the stored unit is a cm.
            current['distance'] = (
                struct.unpack(endian + 'I', data[pos : pos + 4])[0] / 100.0
            )
        elif number == ENHANCED_ALTITUDE and size == 4 and base_type & 0x1F == UINT32:
            raw = struct.unpack(endian + 'I', data[pos : pos + 4])[0]
            if raw != 0xFFFFFFFF:
                # Altitude is scaled by 5 and offset by 500 m.
                current['altitude'] = raw / 5 - 500
        elif (
            number == ALTITUDE
            and size == 2
            and base_type & 0x1F == UINT16
            and current['altitude'] is None
        ):
            raw = struct.unpack(endian + 'H', data[pos : pos + 2])[0]
            if raw != 0xFFFF:
                current['altitude'] = raw / 5 - 500
        elif number == POSITION_LAT and size == 4 and base_type & 0x1F == SINT32:
            current['lat'] = pos
        elif number == POSITION_LONG and size == 4 and base_type & 0x1F == SINT32:
            current['lon'] = pos

    end = walk(data, visit, start)
    for record in records:
        if record['lat'] is None or record['lon'] is None:
            continue
        endian = record['endian']
        lat = struct.unpack_from(endian + 'i', data, record['lat'])[0]
        lon = struct.unpack_from(endian + 'i', data, record['lon'])[0]
        if INVALID not in (abs(lat), abs(lon)):
            record['fix'] = (lat / SEMICIRCLES, lon / SEMICIRCLES)
    return records, end


def summarize(data):
    """Return data with lap and session positions taken from the records.

    A summary keeps the start, end and bounding box its device saw, so after
    the records move, those still point at the jammer's sentinel or wherever
    the spoofer said. Viewers place the activity, and pick its timezone, from
    them. Fields the summary was written without stay absent.
    """
    fixes = []
    summaries = []

    def start(global_num):
        if global_num in SUMMARY_FIELDS:
            summaries.append({'kind': global_num, 'start': None, 'fields': {}})

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if developer or size != 4:
            return
        if global_num == RECORD:
            if number == TIMESTAMP:
                fixes.append(
                    [struct.unpack_from(endian + 'I', data, pos)[0], None, None]
                )
            elif number in (POSITION_LAT, POSITION_LONG) and fixes:
                value = struct.unpack_from(endian + 'i', data, pos)[0]
                if abs(value) != INVALID:
                    fixes[-1][1 + number] = value
            return
        if global_num not in SUMMARY_FIELDS:
            return
        current = summaries[-1]
        if number == START_TIME and base_type & 0x1F == UINT32:
            current['start'] = struct.unpack_from(endian + 'I', data, pos)[0]
        elif number in SUMMARY_FIELDS[global_num] and base_type & 0x1F == SINT32:
            current['fields'][number] = (pos, endian)

    end = walk(data, visit, start)
    fixes = [
        (stamp, lat, lon)
        for stamp, lat, lon in fixes
        if lat is not None and lon is not None
    ]
    laps = sorted(
        s['start'] for s in summaries if s['kind'] == LAP and s['start'] is not None
    )

    out = bytearray(data)
    for summary in summaries:
        low = summary['start']
        if low is None:
            continue
        # A lap runs until the next one starts; the session spans them all.
        later = [lap for lap in laps if lap > low]
        high = later[0] if summary['kind'] == LAP and later else math.inf
        inside = [(lat, lon) for stamp, lat, lon in fixes if low <= stamp < high]
        if not inside:
            continue
        lats = [lat for lat, _ in inside]
        lons = [lon for _, lon in inside]
        values = (*inside[0], *inside[-1], max(lats), max(lons), min(lats), min(lons))
        for number, value in zip(SUMMARY_FIELDS[summary['kind']], values, strict=True):
            if number in summary['fields']:
                pos, endian = summary['fields'][number]
                struct.pack_into(endian + 'i', out, pos, value)

    # The 14-byte header's own CRC covers bytes 0..11, which do not change.
    struct.pack_into('<H', out, end, crc16(bytes(out[:end])))
    return bytes(out)


def match(
    data,
    points,
    scale=True,
    gaps_only=False,
    elevations=None,
    broken_only=False,
    limit=None,
):
    """Return (records, anchors, cumulative, stats) for placing the ride.

    gaps_only marks every fix that lies on the route as trusted and anchors
    the match on it, so the wheel sensor's drift resets at each trusted fix
    instead of accumulating over the whole ride. broken_only does the same
    and also keeps real fixes off the route, placing only missing, invalid
    and spoofed ones. elevations adds anchors where the recording's climbs
    line up with the route's. limit is the fastest plausible speed in metres
    a second, above which the recorded distance is rebuilt from the rest.
    """
    cumulative = measure(points)
    route_length = cumulative[-1]

    records, _ = read_records(data)
    replaced, dropped = 0, 0.0
    if limit:
        # Only the axis the placement runs on changes; the distance recorded
        # in the file is left alone, which is fitpace.py's to rewrite.
        paced = [
            r for r in records if r['distance'] is not None and r['stamp'] is not None
        ]
        rebuilt, jumps, dropped = retime(
            [r['stamp'] for r in paced], [r['distance'] for r in paced], limit
        )
        for record, distance in zip(paced, rebuilt, strict=True):
            record['distance'] = distance
        replaced = len(jumps)
    ridden = [r for r in records if r['distance'] is not None]
    if not ridden:
        raise ValueError('no record.distance field to match on')
    ride_length = max(r['distance'] for r in ridden)
    if ride_length <= 0:
        raise ValueError('the ride has no distance to match on')

    # A wheel sensor calibrated a fraction off reads a different total than
    # the route measures. Scaling makes the finish land on the route's finish
    # instead of drifting past it by that fraction.
    ratio = route_length / ride_length if scale else 1.0

    # Without trusted fixes this is plain scaling: start on start, finish on
    # finish, or with no scale the raw distance.
    anchors = (
        anchor(records, points, cumulative, ratio) if gaps_only or broken_only else []
    )
    trusted = len(anchors)
    if scale:
        inside = [
            (ride, route)
            for ride, route in anchors
            if 0 < ride < ride_length and 0 < route < route_length
        ]
        anchors = [(0.0, 0.0), *inside, (ride_length, route_length)]
    elif not anchors:
        anchors = [(0.0, 0.0)]

    before = len(anchors)
    if elevations is not None:
        anchors = align(records, anchors, cumulative, elevations)

    off_route = keep_real(records, points, cumulative) if broken_only else 0

    stats = {
        'records': len(records),
        'placed': 0,
        'skipped': 0,
        'kept': 0,
        'anchors': trusted,
        'hills': len(anchors) - before,
        'off_route': off_route,
        'replaced': replaced,
        'dropped': dropped,
        'route_length': route_length,
        'ride_length': ride_length,
        'ratio': ratio,
    }
    return records, anchors, cumulative, stats


def graft(
    data,
    points,
    scale=True,
    reverse=False,
    gaps_only=False,
    elevations=None,
    broken_only=False,
    limit=None,
):
    """Return a copy of data with record positions taken from the route."""
    if reverse:
        points = points[::-1]
        elevations = elevations and elevations[::-1]
    records, anchors, cumulative, stats = match(
        data, points, scale, gaps_only, elevations, broken_only, limit
    )

    out = bytearray(data)
    for record in records:
        if record['distance'] is None or record['lat'] is None or record['lon'] is None:
            stats['skipped'] += 1
            continue
        if record['trusted']:
            stats['kept'] += 1
            continue
        lat, lon = locate(points, cumulative, interpolate(anchors, record['distance']))
        endian = record['endian']
        struct.pack_into(endian + 'i', out, record['lat'], round(lat * SEMICIRCLES))
        struct.pack_into(endian + 'i', out, record['lon'], round(lon * SEMICIRCLES))
        stats['placed'] += 1

    return summarize(bytes(out)), stats


def rebuild(
    base_path,
    points,
    scale=True,
    reverse=False,
    gaps_only=False,
    elevations=None,
    broken_only=False,
    limit=None,
):
    """Return (bytes, stats) with a position written into every record.

    For a base recorded with the GPS off, wholly or in part: those records
    carry no position field at all, so there are no bytes for graft() to patch
    and the file has to be decoded and encoded again. That costs the fidelity
    graft() protects -- fit_tool re-encodes what it understands -- so it is
    opt-in.
    """
    # Imported here so the in-place path keeps working without fit_tool.
    from fit_tool.definition_message import DefinitionMessage
    from fit_tool.fit_file import FitFile
    from fit_tool.fit_file_builder import FitFileBuilder

    from fitgraft import set_position

    if reverse:
        points = points[::-1]
        elevations = elevations and elevations[::-1]
    with open(base_path, 'rb') as base_file:
        data = base_file.read()
    records, anchors, cumulative, stats = match(
        data, points, scale, gaps_only, elevations, broken_only, limit
    )

    base = FitFile.from_file(base_path)
    messages = [
        r.message for r in base.records if not isinstance(r.message, DefinitionMessage)
    ]
    decoded = [m for m in messages if type(m).__name__ == 'RecordMessage']
    # The match was made on the file's own bytes; pair it with fit_tool's
    # decode record by record.
    if len(decoded) != len(records):
        raise ValueError(
            f'fit_tool decoded {len(decoded)} records, the file has {len(records)}'
        )

    for message, record in zip(decoded, records, strict=True):
        if record['distance'] is None:
            stats['skipped'] += 1
        elif record['trusted']:
            stats['kept'] += 1
        else:
            set_position(
                message,
                *locate(points, cumulative, interpolate(anchors, record['distance'])),
            )
            stats['placed'] += 1

    builder = FitFileBuilder(auto_define=True)
    for message in messages:
        builder.add(message)
    return summarize(builder.build().to_bytes()), stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='FIT file with the good sensor data')
    parser.add_argument('route', help='GPX file with the route to follow')
    parser.add_argument('-o', '--out', help='output FIT file')
    parser.add_argument(
        '--no-scale',
        action='store_true',
        help='match raw distances instead of stretching the ride to the route length',
    )
    parser.add_argument(
        '--reverse', action='store_true', help='the ride went round the route backwards'
    )
    parser.add_argument(
        '--laps',
        type=int,
        default=1,
        metavar='N',
        help='the ride went round a looped route N times',
    )
    parser.add_argument(
        '--max-speed',
        type=float,
        metavar='KMH',
        help='rebuild the distance the match runs on wherever '
        'it advances faster than this, for a watch whose '
        'distance comes from the jammed GPS itself',
    )
    parser.add_argument(
        '--rebuild',
        action='store_true',
        help='re-encode the file to add positions, for a base '
        'recorded with no position field at all '
        '(needs fit-tool)',
    )
    parser.add_argument(
        '--gaps-only',
        action='store_true',
        help='keep fixes that lie on the route and place only '
        'the missing, invalid or spoofed ones between them',
    )
    parser.add_argument(
        '--broken-only',
        action='store_true',
        help='like --gaps-only, but also keep real fixes off '
        'the route and place only missing, invalid or '
        'spoofed ones',
    )
    parser.add_argument(
        '--hills',
        action='store_true',
        help="anchor the placement where the recording's "
        "climbs line up with the route's <ele>",
    )
    parser.add_argument(
        '-n', '--dry-run', action='store_true', help='report the match without writing'
    )
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')
    if args.laps < 1:
        parser.error('--laps must be at least 1')
    if args.max_speed is not None and args.max_speed <= 0:
        parser.error('--max-speed must be positive')

    limit = args.max_speed and args.max_speed / 3.6
    try:
        points = read_route(args.route)
        elevations = None
        if args.hills:
            elevations = read_elevations(args.route)
            if elevations is None:
                raise ValueError(
                    f'{args.route}: --hills needs <ele> on every track point'
                )
        # A lap of a loop ends where the next one starts, so the route
        # repeated is the route the ride actually covered.
        points = points * args.laps
        elevations = elevations and elevations * args.laps
        if args.rebuild:
            patched, stats = rebuild(
                args.base,
                points,
                not args.no_scale,
                args.reverse,
                args.gaps_only,
                elevations,
                args.broken_only,
                limit,
            )
        else:
            data = open(args.base, 'rb').read()
            patched, stats = graft(
                data,
                points,
                not args.no_scale,
                args.reverse,
                args.gaps_only,
                elevations,
                args.broken_only,
                limit,
            )
    except (
        ValueError,
        IndexError,
        KeyError,
        struct.error,
        ImportError,
        ElementTree.ParseError,
    ) as err:
        print(f'cannot graft: {err}', file=sys.stderr)
        return 1

    laps = f', {args.laps} laps' if args.laps > 1 else ''
    print(
        f'route     {len(points)} points, {stats["route_length"] / 1000:.3f} km{laps}'
    )
    print(f'ride      {stats["records"]} records, {stats["ride_length"] / 1000:.3f} km')
    if limit:
        print(
            f'pace      {stats["dropped"] / 1000:.3f} km of inflated '
            f'distance dropped from {stats["replaced"]} records'
        )
    print(f'scale     {stats["ratio"]:.5f}')
    if args.gaps_only or args.broken_only:
        print(
            f'kept      {stats["kept"] - stats["off_route"]} fixes on the route, '
            f'{stats["anchors"]} anchors'
        )
    if args.broken_only:
        print(f'kept      {stats["off_route"]} real fixes off the route')
    if args.hills:
        print(f'hills     {stats["hills"]} anchors from the climbs')
    print(f'placed    {stats["placed"]}')
    print(
        f'skipped   {stats["skipped"]} records without a distance to place'
        if args.rebuild
        else f'skipped   {stats["skipped"]} records written without a position field'
    )

    if args.dry_run:
        return 0

    open(args.out, 'wb').write(patched)
    print(f'wrote     {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
