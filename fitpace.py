"""Rebuild a run's distance where the watch measured it wrong.

For a run whose pace is right up to some distance and wrong after it: a watch
that lost GPS and fell back on its accelerometer, or one that was spoofed and
had its distance run away. --keep learns how speed follows heart rate for this
runner on this day from the good part, and paces the rest by that relation.
--max-speed instead keeps every second of the recorded pace that is a pace at
all and replaces only the jumps. Either way the rest of the run is stretched
so it finishes on the route's length. --scale is for the other shape of
error: a watch that measured the whole run long, where no stretch of it is
wrong on its own and every record scales by the same ratio.

Only record.distance, the record speed fields and the lap and session
distance and speed totals change, in place. Positions are fitroute.py's job.
"""

import argparse
import math
import statistics
import struct
import sys
import xml.etree.ElementTree as ElementTree
from itertools import pairwise

from fitroute import measure, read_route, retime
from fitshift import RECORD, crc16, walk

SESSION = 18
LAP = 19

TIMESTAMP = 253
START_TIME = 2
TOTAL_ELAPSED_TIME = 7
TOTAL_TIMER_TIME = 8
TOTAL_DISTANCE = 9

HEART_RATE = 3
DISTANCE = 5
SPEED = 6
ENHANCED_SPEED = 73

# (message, field) for the speed totals; uint16 avg/max and uint32 enhanced.
AVG_SPEED = {SESSION: (14, 124), LAP: (13, 110)}
MAX_SPEED = {SESSION: (15, 125), LAP: (14, 111)}

# Heart rate trails effort by tens of seconds; smooth it over a minute.
SMOOTHING = 30

# The first minutes of a run raise heart rate without raising pace.
WARMUP = 120


def read_messages(data):
    """Return (messages, data_end).

    One entry per data message: its global number and, for each field we may
    read or write, (value, offset, size, endian).
    """
    messages = []

    def start(global_num):
        messages.append((global_num, {}))

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if developer or size not in (1, 2, 4):
            return
        code = {1: 'B', 2: 'H', 4: 'I'}[size]
        value = struct.unpack(endian + code, data[pos : pos + size])[0]
        messages[-1][1][number] = (value, pos, size, endian)

    end = walk(data, visit, start)
    return messages, end


def smooth(values, window):
    return [
        statistics.fmean(values[max(0, i - window) : i + window + 1])
        for i in range(len(values))
    ]


def model(times, heart, speeds, keep):
    """Fit speed = intercept + slope * heart rate on records [WARMUP, keep)."""
    first = next((i for i, t in enumerate(times) if t - times[0] >= WARMUP), 0)
    if keep - first < 2:
        raise ValueError('too little of the run before --keep to learn from')
    slope, intercept = statistics.linear_regression(
        heart[first:keep], speeds[first:keep]
    )
    if slope <= 0:
        raise ValueError('pace does not follow heart rate in the kept part')
    return slope, intercept


def pace(data, keep, length):
    """Return (bytes, stats) with distance after keep metres paced by heart rate.

    The heart-rate model sets the shape; the remaining distance to length
    sets the scale, so the last record lands exactly on length.
    """
    messages, end = read_messages(data)
    records = [
        fields
        for number, fields in messages
        if number == RECORD and {TIMESTAMP, HEART_RATE, DISTANCE} <= fields.keys()
    ]
    if not records:
        raise ValueError('no records with timestamp, heart rate and distance')

    times = [r[TIMESTAMP][0] for r in records]
    distances = [r[DISTANCE][0] / 100.0 for r in records]
    speeds = [
        r[ENHANCED_SPEED][0] / 1000.0
        if ENHANCED_SPEED in r
        else r.get(SPEED, (0,))[0] / 1000.0
        for r in records
    ]
    heart = smooth([r[HEART_RATE][0] for r in records], SMOOTHING)

    split = next((i for i, d in enumerate(distances) if d >= keep), None)
    if split is None or split == len(records) - 1:
        raise ValueError(f'the run never gets past {keep:.0f} m')
    remaining = length - distances[split]
    if remaining <= 0:
        raise ValueError(f'the route is only {length:.0f} m long')

    slope, intercept = model(times, heart, speeds, split)
    predicted = [max(0.0, intercept + slope * rate) for rate in heart[split + 1 :]]
    steps = [second - first for first, second in pairwise(times[split:])]
    modelled = sum(v * dt for v, dt in zip(predicted, steps, strict=True))
    ratio = remaining / modelled

    for i, (v, dt) in enumerate(zip(predicted, steps, strict=True), start=split + 1):
        speeds[i] = v * ratio
        distances[i] = distances[i - 1] + speeds[i] * dt
    distances[-1] = length

    out = rewrite(data, messages, end, times, distances, speeds, records, split)
    stats = {
        'records': len(records),
        'split': split,
        'kept_time': times[split] - times[0],
        'kept_distance': distances[split],
        'length': length,
        'modelled': modelled,
        'ratio': ratio,
        'slope': slope,
        'intercept': intercept,
        'pace': (times[-1] - times[split]) / remaining * 1000,
    }
    return out, stats


def unjam(data, limit, length):
    """Return (bytes, stats) with the distance rebuilt from the recorded pace.

    For a watch with only the jammed GPS to measure with: it integrates the
    jumps it is fed and its distance runs away. Every second whose pace a
    runner could have run is kept as recorded; the jumps take the median
    speed of those seconds, and everything after the first jump is stretched
    so the run finishes on the route's length.
    """
    messages, end = read_messages(data)
    records = [
        fields
        for number, fields in messages
        if number == RECORD and {TIMESTAMP, DISTANCE} <= fields.keys()
    ]
    if not records:
        raise ValueError('no records with a timestamp and distance')

    times = [r[TIMESTAMP][0] for r in records]
    recorded = [r[DISTANCE][0] / 100.0 for r in records]
    speeds = [
        r[ENHANCED_SPEED][0] / 1000.0
        if ENHANCED_SPEED in r
        else r.get(SPEED, (0,))[0] / 1000.0
        for r in records
    ]

    rebuilt, jumps, dropped = retime(times, recorded, limit)
    if not jumps:
        raise ValueError(f'nothing advances faster than {limit * 3.6:.1f} km/h')
    # The recorded distance stands up to the first jump.
    split = jumps[0] - 1
    remaining = length - rebuilt[split]
    if remaining <= 0:
        raise ValueError(f'the route is only {length:.0f} m long')
    ratio = remaining / (rebuilt[-1] - rebuilt[split])

    distances = [
        distance
        if index <= split
        else rebuilt[split] + (distance - rebuilt[split]) * ratio
        for index, distance in enumerate(rebuilt)
    ]
    distances[-1] = length
    for i in range(split + 1, len(records)):
        step = times[i] - times[i - 1]
        speeds[i] = (distances[i] - distances[i - 1]) / step if step > 0 else 0.0

    out = rewrite(data, messages, end, times, distances, speeds, records, split)
    stats = {
        'records': len(records),
        'split': split,
        'kept_time': times[split] - times[0],
        'kept_distance': distances[split],
        'length': length,
        'jumps': len(jumps),
        'dropped': dropped,
        'ratio': ratio,
        'rebuilt': rebuilt[-1] - rebuilt[split],
        'pace': (times[-1] - times[split]) / remaining * 1000,
    }
    return out, stats


def rescale(data, length):
    """Return (bytes, stats) with every distance and speed scaled onto length.

    For a watch that measured the whole run long: the shape of the pace is
    right everywhere and only its scale is wrong, so every record moves by
    the same ratio rather than one stretch of the run absorbing the error.
    """
    messages, end = read_messages(data)
    records = [
        fields
        for number, fields in messages
        if number == RECORD and {TIMESTAMP, DISTANCE} <= fields.keys()
    ]
    if not records:
        raise ValueError('no records with a timestamp and distance')

    times = [r[TIMESTAMP][0] for r in records]
    recorded = [r[DISTANCE][0] / 100.0 for r in records]
    if recorded[-1] <= 0:
        raise ValueError('the run records no distance to scale')
    ratio = length / recorded[-1]

    distances = [distance * ratio for distance in recorded]
    distances[-1] = length
    speeds = [
        (
            r[ENHANCED_SPEED][0] / 1000.0
            if ENHANCED_SPEED in r
            else r.get(SPEED, (0,))[0] / 1000.0
        )
        * ratio
        for r in records
    ]

    out = rewrite(data, messages, end, times, distances, speeds, records, -1)
    elapsed = times[-1] - times[0]
    stats = {
        'records': len(records),
        'split': -1,
        'recorded': recorded[-1],
        'length': length,
        'ratio': ratio,
        'elapsed': elapsed,
        'was': elapsed / recorded[-1] * 1000,
        'pace': elapsed / length * 1000,
    }
    return out, stats


def rewrite(data, messages, end, times, distances, speeds, records, split):
    """Return a copy of data with distance and speed after split replaced.

    The lap and session totals that are read from them move with them.
    """
    out = bytearray(data)
    for record, distance, speed in zip(
        records[split + 1 :], distances[split + 1 :], speeds[split + 1 :], strict=True
    ):
        put(out, record, DISTANCE, round(distance * 100))
        put(out, record, SPEED, round(speed * 1000))
        put(out, record, ENHANCED_SPEED, round(speed * 1000))

    for number, fields in messages:
        if number not in (SESSION, LAP) or START_TIME not in fields:
            continue
        # Not the message timestamp: a watch can write a lap at its start.
        low = fields[START_TIME][0]
        high = low + math.ceil(fields.get(TOTAL_ELAPSED_TIME, (0,))[0] / 1000.0)
        inside = [i for i, t in enumerate(times) if low <= t <= high]
        if not inside:
            continue
        before = inside[0] - 1
        total = distances[inside[-1]] - (distances[before] if before >= 0 else 0.0)
        put(out, fields, TOTAL_DISTANCE, round(total * 100))
        timer = fields.get(TOTAL_TIMER_TIME, (0,))[0] / 1000.0
        if timer > 0:
            for field in AVG_SPEED[number]:
                put(out, fields, field, round(total / timer * 1000))
        top = max(speeds[i] for i in inside)
        for field in MAX_SPEED[number]:
            put(out, fields, field, round(top * 1000))

    # The 14-byte header's own CRC covers bytes 0..11, which do not change.
    struct.pack_into('<H', out, end, crc16(bytes(out[:end])))
    return bytes(out)


def put(out, fields, number, value):
    """Overwrite a field in place, clamped to its size, if the message has it."""
    if number not in fields:
        return
    _, pos, size, endian = fields[number]
    code = {1: 'B', 2: 'H', 4: 'I'}[size]
    limit = 2 ** (8 * size) - 2  # all ones is the invalid sentinel
    struct.pack_into(endian + code, out, pos, max(0, min(value, limit)))


def minutes(seconds_per_km):
    mins, secs = divmod(round(seconds_per_km), 60)
    return f'{mins}:{secs:02d}/km'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='FIT file with the heart rate to model from')
    parser.add_argument('route', help='GPX file whose length the run covered')
    parser.add_argument('-o', '--out', help='output FIT file')
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument(
        '--keep',
        type=float,
        metavar='KM',
        help='the recorded pace is right up to this many km, '
        'and heart rate paces the rest',
    )
    how.add_argument(
        '--max-speed',
        type=float,
        metavar='KMH',
        help='the recorded pace is right wherever it is slower '
        'than this, and its median replaces the jumps',
    )
    how.add_argument(
        '--scale',
        action='store_true',
        help="the whole run covered the route's length and the recorded "
        'pace is the right shape, so scale every record onto it',
    )
    parser.add_argument(
        '--laps',
        type=int,
        default=1,
        metavar='N',
        help='the run went round a looped route N times',
    )
    parser.add_argument(
        '-n', '--dry-run', action='store_true', help='report the model without writing'
    )
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')
    if args.laps < 1:
        parser.error('--laps must be at least 1')
    if args.max_speed is not None and args.max_speed <= 0:
        parser.error('--max-speed must be positive')

    try:
        # A lap of a loop ends where the next one starts, so the route
        # repeated is the route the run actually covered.
        length = measure(read_route(args.route) * args.laps)[-1]
        data = open(args.base, 'rb').read()
        if args.scale:
            patched, stats = rescale(data, length)
        elif args.keep is not None:
            patched, stats = pace(data, args.keep * 1000, length)
        else:
            patched, stats = unjam(data, args.max_speed / 3.6, length)
    except (
        ValueError,
        IndexError,
        KeyError,
        struct.error,
        statistics.StatisticsError,
        ElementTree.ParseError,
    ) as err:
        print(f'cannot pace: {err}', file=sys.stderr)
        return 1

    laps = f', {args.laps} laps' if args.laps > 1 else ''
    print(f'route     {stats["length"] / 1000:.3f} km{laps}')
    if args.scale:
        print(
            f'recorded  {stats["recorded"] / 1000:.3f} km in '
            f'{stats["elapsed"] // 60}:{stats["elapsed"] % 60:02d}, '
            f'{minutes(stats["was"])}'
        )
        print(
            f'scaled    every record by {stats["ratio"]:.5f} to '
            f'{stats["length"] / 1000:.3f} km'
        )
    else:
        kept_pace = stats['kept_time'] / stats['kept_distance'] * 1000
        print(
            f'kept      {stats["kept_distance"] / 1000:.3f} km in '
            f'{stats["kept_time"] // 60}:{stats["kept_time"] % 60:02d}, '
            f'{minutes(kept_pace)}'
        )
        rest = (stats['length'] - stats['kept_distance']) / 1000
        if args.keep is not None:
            print(
                f'model     speed = {stats["intercept"]:.3f} + '
                f'{stats["slope"]:.4f} * hr m/s'
            )
            print(
                f'modelled  {stats["modelled"] / 1000:.3f} km by heart rate, '
                f'scaled {stats["ratio"]:.5f} to {rest:.3f} km'
            )
        else:
            print(
                f'dropped   {stats["dropped"] / 1000:.3f} km of inflated '
                f'distance from {stats["jumps"]} records'
            )
            print(
                f'rebuilt   {stats["rebuilt"] / 1000:.3f} km from the pace that '
                f'was left, scaled {stats["ratio"]:.5f} to {rest:.3f} km'
            )
    print(
        f'paced     {stats["records"] - stats["split"] - 1} records at '
        f'{minutes(stats["pace"])} on average'
    )

    if args.dry_run:
        return 0

    open(args.out, 'wb').write(patched)
    print(f'wrote     {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
