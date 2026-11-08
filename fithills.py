"""Rebuild a hill-repeat session against a route that climbs the hill once.

A route drawn for hill repeats goes up the hill and comes back down, once.
The session ran it seven times, so the route is neither the length nor the
shape of what the watch recorded, and stretching the run onto it whole puts
every repeat in the wrong place.

Which is more than bookkeeping, because a watch with no footpod measures a
steep repeat far worse than it measures the flat approach to it. The session
this was written for read its approach 2% short and its repeats 16% long,
and read the same 3.9 km of path 12% apart on the way out and the way home.
The lap marks are the one thing such a watch records exactly: the wearer
pressed lap at the foot of the climb and again at the top. So each lap is
rescaled onto the span of route its own leg covered, rather than the whole
run onto the whole route by one ratio -- which for that session would have
started the first climb 470 m before the hill and left the turnarounds half
a repeat out of phase.

The laps have to read warm-up, then a climb and a descent per repeat, then
cool-down, which is 2N+2 of them. --route writes the repeated route out,
because fitroute.py has to place the positions against the same one.

Only record.distance, the record speed fields and the lap and session
distance and speed totals change, in place. Positions are fitroute.py's job.
"""

import argparse
import bisect
import struct
import sys
import xml.etree.ElementTree as ElementTree
from itertools import pairwise

from fitpace import (
    AVG_SPEED,
    DISTANCE,
    ENHANCED_SPEED,
    LAP,
    SPEED,
    START_TIME,
    TIMESTAMP,
    TOTAL_DISTANCE,
    TOTAL_TIMER_TIME,
    minutes,
    put,
    read_messages,
    rewrite,
)
from fitroute import haversine, measure, read_elevations, read_route
from fitshift import RECORD, crc16

# A descent is back at the foot of the climb once it is this close to it.
# The same tolerance fitroute.py calls a fix on the route.
RETURN = 25.0


def nearest(cumulative, metres):
    """Return the index of the route point nearest metres along it."""
    index = bisect.bisect_left(cumulative, metres)
    if index <= 0:
        return 0
    if index >= len(cumulative):
        return len(cumulative) - 1
    before, after = cumulative[index - 1], cumulative[index]
    return index - 1 if metres - before <= after - metres else index


def returns(points, foot, ahead):
    """Return the first index past ahead where the route is back at the foot.

    The nearest point of that approach, not the first one inside RETURN, so a
    route whose descent misses its own ascent by a few metres still lands on
    the foot it left.
    """
    best = None
    for index in range(ahead + 1, len(points)):
        gap = haversine(points[index], points[foot])
        if gap > RETURN:
            if best is not None:
                break
            continue
        if best is None or gap < best[0]:
            best = (gap, index)
    if best is None:
        raise ValueError(
            f'the route never comes back within {RETURN:.0f} m of the foot'
        )
    return best[1]


def find_hill(points, cumulative, elevations, low, high):
    """Return (foot, apex, back): the climb's foot and top, and the descent's end.

    low and high are metres along the route: where the climb starts, and a
    point at or past its top. The top is the highest point the route reaches
    between the foot and its return to it, so high only has to be close.
    """
    foot, ahead = nearest(cumulative, low), nearest(cumulative, high)
    if not 0 <= foot < ahead < len(points) - 1:
        raise ValueError('the climb must start before it tops out, inside the route')
    back = returns(points, foot, ahead)
    apex = max(range(foot, back + 1), key=elevations.__getitem__)
    if not foot < apex < back:
        raise ValueError('the climb has no top between its foot and its descent')
    return foot, apex, back


def splice(values, foot, back, count):
    """Return values with the block values[foot:back] repeated count times."""
    return values[:foot] + values[foot:back] * count + values[back:]


def spans(cumulative, foot, apex, back, count):
    """Return each lap's (low, high) metres along the repeated route.

    One for the warm-up, a climb and a descent for every repeat, and one for
    the cool-down home. cumulative is the repeated route's, so a boundary is
    read off it by index and no two of them can drift apart.
    """
    block = back - foot
    edges = [0, foot]
    for repeat in range(count):
        edges.append(foot + repeat * block + apex - foot)
        edges.append(foot + (repeat + 1) * block)
    edges.append(len(cumulative) - 1)
    return [(cumulative[a], cumulative[b]) for a, b in pairwise(edges)]


def relap(data, spans):
    """Return (bytes, stats) with every lap's distance rescaled onto its span.

    Each lap keeps the shape of the pace it recorded and only its scale
    changes, so where the wearer sped up inside a leg they still do.
    """
    messages, end = read_messages(data)
    records = [
        fields
        for number, fields in messages
        if number == RECORD and {TIMESTAMP, DISTANCE} <= fields.keys()
    ]
    laps = [
        fields for number, fields in messages if number == LAP and START_TIME in fields
    ]
    if not records:
        raise ValueError('no records with a timestamp and distance')
    if len(laps) != len(spans):
        raise ValueError(
            f'{len(spans) // 2 - 1} repeats need {len(spans)} laps '
            f'(warm-up, a climb and a descent each, cool-down), '
            f'and the file has {len(laps)}'
        )

    times = [r[TIMESTAMP][0] for r in records]
    recorded = [r[DISTANCE][0] / 100.0 for r in records]
    edges = [bisect.bisect_left(times, lap[START_TIME][0]) for lap in laps]
    if edges[0] != 0 or any(b <= a for a, b in pairwise(edges)):
        raise ValueError('the laps do not start one after another over the records')
    edges.append(len(records))

    distances, factors = list(recorded), []
    for (low, high), (start, stop) in zip(spans, pairwise(edges), strict=True):
        covered = recorded[stop - 1] - recorded[start]
        factors.append((high - low) / covered if covered > 0 else 0.0)
        for index in range(start, stop):
            along = (
                (recorded[index] - recorded[start]) / covered if covered > 0 else 0.0
            )
            distances[index] = low + (high - low) * along

    speeds = []
    for index, record in enumerate(records):
        recorded_speed = (
            record[ENHANCED_SPEED][0] / 1000.0
            if ENHANCED_SPEED in record
            else record.get(SPEED, (0,))[0] / 1000.0
        )
        lap = bisect.bisect_right(edges, index) - 1
        speeds.append(recorded_speed * factors[lap])

    out = rewrite(data, messages, end, times, distances, speeds, records, -1)
    out = retotal(out, end, laps, spans)
    stats = {
        'records': len(records),
        'recorded': recorded[-1],
        'length': spans[-1][1],
        'laps': [
            {
                'route': high - low,
                'was': recorded[stop - 1] - recorded[start],
                'timer': lap.get(TOTAL_TIMER_TIME, (0,))[0] / 1000.0,
            }
            for (low, high), (start, stop), lap in zip(
                spans, pairwise(edges), laps, strict=True
            )
        ],
    }
    return out, stats


def retotal(data, end, laps, spans):
    """Return a copy of data with every lap total its span of route exactly.

    rewrite() reads a lap total back off the records inside the lap's own
    reported window, which can reach a sample or two past where the next lap
    starts and charge its first metres to both. That is a rounding error
    elsewhere, but here the lap marks are what the whole rescale rests on, so
    the totals are written from the spans instead and tile the session.
    """
    out = bytearray(data)
    for lap, (low, high) in zip(laps, spans, strict=True):
        total = high - low
        put(out, lap, TOTAL_DISTANCE, round(total * 100))
        timer = lap.get(TOTAL_TIMER_TIME, (0,))[0] / 1000.0
        if timer > 0:
            for field in AVG_SPEED[LAP]:
                put(out, lap, field, round(total / timer * 1000))
    struct.pack_into('<H', out, end, crc16(bytes(out[:end])))
    return bytes(out)


def write_route(path, points, elevations):
    """Write the repeated route out as a GPX track."""
    with open(path, 'w', encoding='utf-8') as out:
        out.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="fithills.py"'
            ' xmlns="http://www.topografix.com/GPX/1/1">\n'
            '  <trk><trkseg>\n'
        )
        out.writelines(
            f'    <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{ele:.1f}</ele></trkpt>\n'
            for (lat, lon), ele in zip(points, elevations, strict=True)
        )
        out.write('  </trkseg></trk>\n</gpx>\n')


def name(index, count):
    if index == 0:
        return 'warm-up'
    if index == 2 * count + 1:
        return 'cool-down'
    repeat, leg = divmod(index - 1, 2)
    return f'{"descent" if leg else "climb"} {repeat + 1}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='FIT file the session was recorded to')
    parser.add_argument('route', help='GPX file whose route climbs the hill once')
    parser.add_argument(
        '--from',
        dest='foot',
        type=float,
        required=True,
        metavar='KM',
        help='how far along the route the climb starts',
    )
    parser.add_argument(
        '--to',
        dest='top',
        type=float,
        required=True,
        metavar='KM',
        help='how far along the route the climb tops out, near enough',
    )
    parser.add_argument(
        '--repeats',
        type=int,
        required=True,
        metavar='N',
        help='how many times the climb was run',
    )
    parser.add_argument('-o', '--out', help='output FIT file')
    parser.add_argument(
        '--route-out', metavar='GPX', help='write the repeated route here'
    )
    parser.add_argument(
        '-n', '--dry-run', action='store_true', help='report the result without writing'
    )
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')
    if args.repeats < 1:
        parser.error('--repeats must be at least 1')
    if args.foot >= args.top:
        parser.error('--from must come before --to')

    try:
        points = read_route(args.route)
        elevations = read_elevations(args.route)
        if elevations is None:
            raise ValueError(f'{args.route}: needs <ele> on every track point')
        cumulative = measure(points)
        foot, apex, back = find_hill(
            points, cumulative, elevations, args.foot * 1000, args.top * 1000
        )
        repeated = splice(points, foot, back, args.repeats)
        heights = splice(elevations, foot, back, args.repeats)
        along = measure(repeated)
        data = open(args.base, 'rb').read()
        patched, stats = relap(data, spans(along, foot, apex, back, args.repeats))
    except (
        ValueError,
        IndexError,
        KeyError,
        struct.error,
        ElementTree.ParseError,
    ) as err:
        print(f'cannot repeat: {err}', file=sys.stderr)
        return 1

    up = cumulative[apex] - cumulative[foot]
    down = cumulative[back] - cumulative[apex]
    relief = elevations[apex] - elevations[foot]
    print(
        f'hill      {up / 1000:.3f} km up {relief:+.0f} m, {down / 1000:.3f} km '
        f'down, from {cumulative[foot] / 1000:.3f} km'
    )
    print(
        f'route     {len(repeated)} points, {stats["length"] / 1000:.3f} km '
        f'({cumulative[-1] / 1000:.3f} km with {args.repeats} repeats)'
    )
    print(f'recorded  {stats["records"]} records, {stats["recorded"] / 1000:.3f} km')
    for index, lap in enumerate(stats['laps']):
        was = lap['timer'] / lap['was'] * 1000 if lap['was'] else 0
        now = lap['timer'] / lap['route'] * 1000 if lap['route'] else 0
        print(
            f'lap {index + 1:>3}   {name(index, args.repeats):<11}'
            f'{lap["route"] / 1000:.3f} km  was {lap["was"] / 1000:.3f} km   '
            f'{minutes(now):>8}  was {minutes(was):>8}'
        )

    if args.dry_run:
        return 0
    with open(args.out, 'wb') as out:
        out.write(patched)
    print(f'wrote     {args.out}')
    if args.route_out:
        write_route(args.route_out, repeated, heights)
        print(f'wrote     {args.route_out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
