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


def read_records(data):
    """Return (records, data_end).

    One record per record message, holding its distance in metres and the
    offsets where its position fields live, or None when the record was
    written without them.
    """
    records = []

    def start(global_num):
        if global_num != RECORD:
            return
        records.append({'distance': None, 'lat': None, 'lon': None,
                        'endian': '<'})

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

    return records, walk(data, visit, start)


def graft(data, points, scale=True, reverse=False):
    """Return a copy of data with record positions taken from the route."""
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

    out = bytearray(data)
    stats = {'records': len(records), 'placed': 0, 'skipped': 0,
             'route_length': route_length, 'ride_length': ride_length,
             'ratio': ratio}

    for record in records:
        if record['distance'] is None or record['lat'] is None or record['lon'] is None:
            stats['skipped'] += 1
            continue
        lat, lon = locate(points, cumulative, record['distance'] * ratio)
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
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='report the match without writing')
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')

    try:
        points = read_route(args.route)
        if args.rebuild:
            patched, stats = rebuild(args.base, points, not args.no_scale,
                                     args.reverse)
        else:
            data = open(args.base, 'rb').read()
            patched, stats = graft(data, points, not args.no_scale,
                                   args.reverse)
    except (ValueError, IndexError, KeyError, struct.error, ImportError,
            ElementTree.ParseError) as err:
        print(f'cannot graft: {err}', file=sys.stderr)
        return 1

    print(f"route     {len(points)} points, {stats['route_length'] / 1000:.3f} km")
    print(f"ride      {stats['records']} records, {stats['ride_length'] / 1000:.3f} km")
    print(f"scale     {stats['ratio']:.5f}")
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
