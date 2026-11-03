"""Drop the distance a watch credited while its wearer was standing still.

A watch that loses its fix and auto-pauses re-acquires when the wearer moves
off again, and charges the whole re-acquisition jump to the few seconds after
the pause: hundreds of metres in a handful of records, at a speed no runner
holds. Those metres were never covered, so they are removed rather than
re-paced. `fitpace.py --max-speed` is the wrong tool for it, since replacing
the jump with a plausible pace keeps ground nobody ran. What is left over
against the route's length afterwards is the odometer's own calibration
error, so the run is scaled onto it.

A pause is only read as a stop when a jump follows it. A watch that drops
samples while the wearer keeps running leaves a gap too, but goes on
measuring across it, and that distance is real.

Only record.distance, the record speed fields and the lap and session
distance and speed totals change, in place. Positions are fitroute.py's job.
"""

import argparse
import statistics
import struct
import sys
import xml.etree.ElementTree as ElementTree
from itertools import pairwise

from fitpace import (
    DISTANCE,
    ENHANCED_SPEED,
    HEART_RATE,
    SPEED,
    TIMESTAMP,
    minutes,
    read_messages,
    rewrite,
)
from fitroute import measure, read_route
from fitshift import RECORD

# A silence longer than this is the watch pausing, not a dropped sample.
STOP = 5

# No runner holds this, so the distance came from a re-acquired fix. The
# other tools take a speed in km/h on the command line and metres a second
# in the library, and so does this one.
JUMP = 25.0 / 3.6


def read_run(data):
    """Return (messages, end, records, times, distances, speeds, heart)."""
    messages, end = read_messages(data)
    records = [
        fields
        for number, fields in messages
        if number == RECORD and {TIMESTAMP, DISTANCE} <= fields.keys()
    ]
    if not records:
        raise ValueError('no records with a timestamp and distance')
    times = [r[TIMESTAMP][0] for r in records]
    distances = [r[DISTANCE][0] / 100.0 for r in records]
    speeds = [
        r[ENHANCED_SPEED][0] / 1000.0
        if ENHANCED_SPEED in r
        else r.get(SPEED, (0,))[0] / 1000.0
        for r in records
    ]
    heart = [r[HEART_RATE][0] if HEART_RATE in r else None for r in records]
    return messages, end, records, times, distances, speeds, heart


def stops(times, distances, gap=STOP, limit=JUMP):
    """Return the record windows whose distance was never covered.

    A window is (first, last): first is the last record before the pause and
    last the final record of the jump that follows it, so the ground to
    remove is what the odometer gained between the two.
    """
    windows = []
    paused = (i for i, (a, b) in enumerate(pairwise(times), start=1) if b - a > gap)
    for after in paused:
        last = after
        while last + 1 < len(times):
            step = times[last + 1] - times[last]
            if step <= 0 or (distances[last + 1] - distances[last]) / step <= limit:
                break
            last += 1
        # Without a jump the watch went on measuring, and that distance is real.
        if last > after and distances[last] > distances[after - 1]:
            windows.append((after - 1, last))
    return windows


def destop(data, length=None, gap=STOP, limit=JUMP):
    """Return (bytes, stats) with the standing-still distance taken out.

    length is the route's length in metres, which the run is scaled onto
    once the invented distance is gone; None leaves the rest as recorded.
    """
    messages, end, records, times, recorded, speeds, heart = read_run(data)
    windows = stops(times, recorded, gap, limit)
    if not windows:
        raise ValueError(f'no pause longer than {gap} s is followed by a jump')

    deltas = [recorded[0], *(b - a for a, b in pairwise(recorded))]
    stopped = set()
    report = []
    for first, last in windows:
        beats = [rate for rate in heart[first : last + 1] if rate]
        report.append(
            {
                'at': times[first] - times[0],
                'span': times[last] - times[first],
                'dropped': recorded[last] - recorded[first],
                'heart': min(beats) if beats else None,
            }
        )
        stopped.update(range(first + 1, last + 1))

    kept = []
    total = 0.0
    for index, delta in enumerate(deltas):
        total += 0.0 if index in stopped else delta
        kept.append(total)

    if kept[-1] <= 0:
        raise ValueError('every metre of the run was gained standing still')
    if length is None:
        length = kept[-1]
    if length <= 0:
        raise ValueError('the route has no length')
    ratio = length / kept[-1]
    distances = [d * ratio for d in kept]
    distances[-1] = length

    out = rewrite(
        data,
        messages,
        end,
        times,
        distances,
        [0.0 if i in stopped else s * ratio for i, s in enumerate(speeds)],
        records,
        -1,
    )
    stats = {
        'records': len(records),
        'windows': report,
        'dropped': sum(w['dropped'] for w in report),
        'recorded': recorded[-1],
        'kept': kept[-1],
        'length': length,
        'ratio': ratio,
        'elapsed': times[-1] - times[0],
    }
    return out, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='FIT file whose watch paused and jumped')
    parser.add_argument(
        'route',
        nargs='?',
        help='GPX file whose length the run covered; without it '
        'the rest of the run is left as recorded',
    )
    parser.add_argument('-o', '--out', help='output FIT file')
    parser.add_argument(
        '--gap',
        type=float,
        default=STOP,
        metavar='S',
        help=f'a silence longer than this is a pause (default {STOP})',
    )
    parser.add_argument(
        '--max-speed',
        type=float,
        default=JUMP * 3.6,
        metavar='KMH',
        help=f'a record faster than this after a pause is a jump, '
        f'not a stride (default {JUMP * 3.6:.0f})',
    )
    parser.add_argument(
        '--laps',
        type=int,
        default=1,
        metavar='N',
        help='the run went round a looped route N times',
    )
    parser.add_argument(
        '-n', '--dry-run', action='store_true', help='report the stops without writing'
    )
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')
    if args.laps < 1:
        parser.error('--laps must be at least 1')
    if args.gap <= 0:
        parser.error('--gap must be positive')
    if args.max_speed <= 0:
        parser.error('--max-speed must be positive')

    try:
        # A lap of a loop ends where the next one starts, so the route
        # repeated is the route the run actually covered.
        length = measure(read_route(args.route) * args.laps)[-1] if args.route else None
        data = open(args.base, 'rb').read()
        patched, stats = destop(data, length, args.gap, args.max_speed / 3.6)
    except (
        ValueError,
        IndexError,
        KeyError,
        struct.error,
        statistics.StatisticsError,
        ElementTree.ParseError,
    ) as err:
        print(f'cannot destop: {err}', file=sys.stderr)
        return 1

    laps = f', {args.laps} laps' if args.laps > 1 else ''
    print(f'ride      {stats["records"]} records, {stats["recorded"] / 1000:.3f} km')
    for window in stats['windows']:
        at = window['at']
        beats = f', heart rate {window["heart"]}' if window['heart'] else ''
        print(
            f'stop      at {at // 60}:{at % 60:02d}, {window["span"]:.0f} s, '
            f'dropped {window["dropped"]:.1f} m{beats}'
        )
    print(
        f'dropped   {stats["dropped"] / 1000:.3f} km never covered, '
        f'leaving {stats["kept"] / 1000:.3f} km'
    )
    if args.route:
        print(f'route     {stats["length"] / 1000:.3f} km{laps}')
        print(f'scale     {stats["ratio"]:.5f}')
    print(
        f'pace      {minutes(stats["elapsed"] / stats["recorded"] * 1000)} -> '
        f'{minutes(stats["elapsed"] / stats["length"] * 1000)} by elapsed time'
    )

    if args.dry_run:
        return 0

    open(args.out, 'wb').write(patched)
    print(f'wrote     {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
