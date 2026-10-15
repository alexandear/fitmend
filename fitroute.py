"""Graft a GPX route's shape onto a FIT file's records, matched by distance.

For a ride whose GPS is unusable but whose wheel sensor is fine, and where the
donor is a planned route rather than a recording. A route has no timestamps,
so `fitgraft.py` has nothing to match on; what both files do share is distance
travelled. Each record is placed at its own odometer reading along the route.

Only position_lat and position_long change, in place. Speed, distance, power,
heart rate, cadence, altitude, temperature, laps, session totals and every
private message stay exactly as recorded.
"""

import argparse
import bisect
import math
import struct
import sys
import xml.etree.ElementTree as ElementTree

from fitshift import RECORD, crc16, walk

EARTH_RADIUS = 6371000.0

# FIT stores latitude and longitude as semicircles: degrees * 2^31 / 180.
SEMICIRCLES = 2 ** 31 / 180.0

POSITION_LAT = 0
POSITION_LONG = 1
DISTANCE = 5
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


def haversine(first, second):
    lat1, lon1 = first
    lat2, lon2 = second
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
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


def measure(points):
    """Return cumulative distance along points, one entry per point."""
    cumulative = [0.0]
    for first, second in zip(points, points[1:]):
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
        fraction = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
        off = math.hypot(ax + fraction * dx, ay + fraction * dy)
        if off < best[0]:
            span = cumulative[index + 1] - cumulative[index]
            best = (off, cumulative[index] + fraction * span)
    return best


def anchor(records, points, cumulative):
    """Return [(ride metres, route metres), ...] for the trusted fixes.

    A fix is trusted when it lies on the route, which rejects both the
    jammer's sentinel and the coordinates it spoofs. Anchors only move
    forward, so GPS jitter at a standstill cannot fold the ride back.
    """
    anchors = []
    along = 0.0
    for record in records:
        if record['distance'] is None or record['fix'] is None:
            continue
        off, along_fix = project(points, cumulative, record['fix'],
                                 along - SEARCH_BACK, along + SEARCH_AHEAD)
        if off > ON_ROUTE:
            continue
        record['trusted'] = True
        along = along_fix
        if not anchors or (record['distance'] > anchors[-1][0]
                           and along > anchors[-1][1]):
            anchors.append((record['distance'], along))
    return anchors


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
        records.append({'distance': None, 'lat': None, 'lon': None,
                        'fix': None, 'trusted': False, 'endian': '<'})

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if global_num != RECORD or developer or not records:
            return
        current = records[-1]
        current['endian'] = endian
        if number == DISTANCE and size == 4 and base_type & 0x1F == UINT32:
            # record.distance is scaled by 100, so the stored unit is a cm.
            current['distance'] = struct.unpack(endian + 'I',
                                                data[pos:pos + 4])[0] / 100.0
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


def graft(data, points, scale=True, reverse=False, gaps_only=False):
    """Return a copy of data with record positions taken from the route.

    gaps_only keeps every fix that lies on the route and places the rest
    between them, so the wheel sensor's drift resets at each trusted fix
    instead of accumulating over the whole ride.
    """
    if reverse:
        points = points[::-1]
    cumulative = measure(points)
    route_length = cumulative[-1]

    records, end = read_records(data)
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
    anchors = anchor(records, points, cumulative) if gaps_only else []
    trusted = len(anchors)
    if scale:
        inside = [(ride, route) for ride, route in anchors
                  if 0 < ride < ride_length and 0 < route < route_length]
        anchors = [(0.0, 0.0)] + inside + [(ride_length, route_length)]
    elif not anchors:
        anchors = [(0.0, 0.0)]

    out = bytearray(data)
    stats = {'records': len(records), 'placed': 0, 'skipped': 0, 'kept': 0,
             'anchors': trusted, 'route_length': route_length,
             'ride_length': ride_length, 'ratio': ratio}

    for record in records:
        if record['distance'] is None or record['lat'] is None or record['lon'] is None:
            stats['skipped'] += 1
            continue
        if record['trusted']:
            stats['kept'] += 1
            continue
        lat, lon = locate(points, cumulative,
                          interpolate(anchors, record['distance']))
        endian = record['endian']
        struct.pack_into(endian + 'i', out, record['lat'],
                         round(lat * SEMICIRCLES))
        struct.pack_into(endian + 'i', out, record['lon'],
                         round(lon * SEMICIRCLES))
        stats['placed'] += 1

    # The 14-byte header's own CRC covers bytes 0..11, which do not change.
    struct.pack_into('<H', out, end, crc16(bytes(out[:end])))
    return bytes(out), stats


def rebuild(base_path, points, scale=True, reverse=False):
    """Return (bytes, stats) with a position written into every record.

    For a base recorded with the GPS off: its records carry no position field
    at all, so there are no bytes for graft() to patch and the file has to be
    decoded and encoded again. That costs the fidelity graft() protects --
    fit_tool re-encodes what it understands -- so it is opt-in.
    """
    # Imported here so the in-place path keeps working without fit_tool.
    from fit_tool.definition_message import DefinitionMessage
    from fit_tool.fit_file import FitFile
    from fit_tool.fit_file_builder import FitFileBuilder

    from fitgraft import set_position

    if reverse:
        points = points[::-1]
    cumulative = measure(points)
    route_length = cumulative[-1]

    base = FitFile.from_file(base_path)
    messages = [r.message for r in base.records
                if not isinstance(r.message, DefinitionMessage)]
    ridden = [m for m in messages
              if type(m).__name__ == 'RecordMessage' and m.distance is not None]
    if not ridden:
        raise ValueError('no record.distance field to match on')
    ride_length = max(m.distance for m in ridden)
    if ride_length <= 0:
        raise ValueError('the ride has no distance to match on')

    ratio = route_length / ride_length if scale else 1.0
    stats = {'records': 0, 'placed': 0, 'skipped': 0,
             'route_length': route_length, 'ride_length': ride_length,
             'ratio': ratio}

    builder = FitFileBuilder(auto_define=True)
    for message in messages:
        if type(message).__name__ == 'RecordMessage':
            stats['records'] += 1
            if message.distance is None:
                stats['skipped'] += 1
            else:
                set_position(message, *locate(points, cumulative,
                                              message.distance * ratio))
                stats['placed'] += 1
        builder.add(message)

    return builder.build().to_bytes(), stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='FIT file with the good sensor data')
    parser.add_argument('route', help='GPX file with the route to follow')
    parser.add_argument('-o', '--out', help='output FIT file')
    parser.add_argument('--no-scale', action='store_true',
                        help='match raw distances instead of stretching the '
                             'ride to the route length')
    parser.add_argument('--reverse', action='store_true',
                        help='the ride went round the route backwards')
    parser.add_argument('--rebuild', action='store_true',
                        help='re-encode the file to add positions, for a base '
                             'recorded with no position field at all '
                             '(needs fit-tool)')
    parser.add_argument('--gaps-only', action='store_true',
                        help='keep fixes that lie on the route and place only '
                             'the missing, invalid or spoofed ones between them')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='report the match without writing')
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')
    if args.gaps_only and args.rebuild:
        parser.error('--gaps-only needs fixes to keep; --rebuild is for a '
                     'base with none')

    try:
        points = read_route(args.route)
        if args.rebuild:
            patched, stats = rebuild(args.base, points, not args.no_scale,
                                     args.reverse)
        else:
            data = open(args.base, 'rb').read()
            patched, stats = graft(data, points, not args.no_scale,
                                   args.reverse, args.gaps_only)
    except (ValueError, IndexError, KeyError, struct.error, ImportError,
            ElementTree.ParseError) as err:
        print(f'cannot graft: {err}', file=sys.stderr)
        return 1

    print(f"route     {len(points)} points, {stats['route_length'] / 1000:.3f} km")
    print(f"ride      {stats['records']} records, {stats['ride_length'] / 1000:.3f} km")
    print(f"scale     {stats['ratio']:.5f}")
    if args.gaps_only:
        print(f"kept      {stats['kept']} fixes on the route, "
              f"{stats['anchors']} anchors")
    print(f"placed    {stats['placed']}")
    print(f"skipped   {stats['skipped']} records without a distance to place"
          if args.rebuild else
          f"skipped   {stats['skipped']} records written without a position field")

    if args.dry_run:
        return 0

    open(args.out, 'wb').write(patched)
    print(f"wrote     {args.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
