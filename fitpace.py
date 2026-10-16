"""Rebuild a run's distance after a point from its heart rate.

For a run whose pace is right up to some distance and wrong after it: a watch
that lost GPS and fell back on its accelerometer, or one that was spoofed and
had its distance run away. The good part teaches how speed follows heart rate
for this runner on this day; the rest of the run is paced by that relation and
stretched so it finishes on the route's length.

Only record.distance, the record speed fields and the lap and session
distance and speed totals change, in place. Positions are fitroute.py's job.
"""

import argparse
import math
import statistics
import struct
import sys
import xml.etree.ElementTree as ElementTree

from fitroute import measure, read_route
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
        value = struct.unpack(endian + code, data[pos:pos + size])[0]
        messages[-1][1][number] = (value, pos, size, endian)

    end = walk(data, visit, start)
    return messages, end


def smooth(values, window):
    return [statistics.fmean(values[max(0, i - window):i + window + 1])
            for i in range(len(values))]


def model(times, heart, speeds, keep):
    """Fit speed = intercept + slope * heart rate on records [WARMUP, keep)."""
    first = next((i for i, t in enumerate(times) if t - times[0] >= WARMUP), 0)
    if keep - first < 2:
        raise ValueError('too little of the run before --keep to learn from')
    slope, intercept = statistics.linear_regression(heart[first:keep],
                                                    speeds[first:keep])
    if slope <= 0:
        raise ValueError('pace does not follow heart rate in the kept part')
    return slope, intercept


def pace(data, keep, length):
    """Return (bytes, stats) with distance after keep metres paced by heart rate.

    The heart-rate model sets the shape; the remaining distance to length
    sets the scale, so the last record lands exactly on length.
    """
    messages, end = read_messages(data)
    records = [fields for number, fields in messages if number == RECORD
               and {TIMESTAMP, HEART_RATE, DISTANCE} <= fields.keys()]
    if not records:
        raise ValueError('no records with timestamp, heart rate and distance')

    times = [r[TIMESTAMP][0] for r in records]
    distances = [r[DISTANCE][0] / 100.0 for r in records]
    speeds = [r[ENHANCED_SPEED][0] / 1000.0 if ENHANCED_SPEED in r
              else r.get(SPEED, (0,))[0] / 1000.0 for r in records]
    heart = smooth([r[HEART_RATE][0] for r in records], SMOOTHING)

    split = next((i for i, d in enumerate(distances) if d >= keep), None)
    if split is None or split == len(records) - 1:
        raise ValueError(f'the run never gets past {keep:.0f} m')
    remaining = length - distances[split]
    if remaining <= 0:
        raise ValueError(f'the route is only {length:.0f} m long')

    slope, intercept = model(times, heart, speeds, split)
    predicted = [max(0.0, intercept + slope * heart[i])
                 for i in range(split + 1, len(records))]
    steps = [times[i] - times[i - 1] for i in range(split + 1, len(records))]
    modelled = sum(v * dt for v, dt in zip(predicted, steps))
    ratio = remaining / modelled

    for offset, (v, dt) in enumerate(zip(predicted, steps)):
        i = split + 1 + offset
        speeds[i] = v * ratio
        distances[i] = distances[i - 1] + speeds[i] * dt
    distances[-1] = length

    out = bytearray(data)
    for record, distance, speed in zip(records[split + 1:],
                                       distances[split + 1:],
                                       speeds[split + 1:]):
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
    stats = {'records': len(records), 'split': split,
             'kept_time': times[split] - times[0],
             'kept_distance': distances[split], 'length': length,
             'modelled': modelled, 'ratio': ratio, 'slope': slope,
             'intercept': intercept,
             'pace': (times[-1] - times[split]) / remaining * 1000}
    return bytes(out), stats


def put(out, fields, number, value):
    """Overwrite a field in place, clamped to its size, if the message has it."""
    if number not in fields:
        return
    _, pos, size, endian = fields[number]
    code = {1: 'B', 2: 'H', 4: 'I'}[size]
    limit = 2 ** (8 * size) - 2  # all ones is the invalid sentinel
    struct.pack_into(endian + code, out, pos, max(0, min(value, limit)))


def minutes(seconds_per_km):
    return '%d:%02d/km' % divmod(round(seconds_per_km), 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='FIT file with the heart rate to model from')
    parser.add_argument('route', help='GPX file whose length the run covered')
    parser.add_argument('-o', '--out', help='output FIT file')
    parser.add_argument('--keep', type=float, required=True, metavar='KM',
                        help='the recorded pace is right up to this many km')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='report the model without writing')
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')

    try:
        length = measure(read_route(args.route))[-1]
        data = open(args.base, 'rb').read()
        patched, stats = pace(data, args.keep * 1000, length)
    except (ValueError, IndexError, KeyError, struct.error,
            statistics.StatisticsError, ElementTree.ParseError) as err:
        print(f'cannot pace: {err}', file=sys.stderr)
        return 1

    kept_pace = stats['kept_time'] / stats['kept_distance'] * 1000
    print(f"route     {stats['length'] / 1000:.3f} km")
    print(f"kept      {stats['kept_distance'] / 1000:.3f} km in "
          f"{stats['kept_time'] // 60}:{stats['kept_time'] % 60:02d}, "
          f"{minutes(kept_pace)}")
    print(f"model     speed = {stats['intercept']:.3f} + "
          f"{stats['slope']:.4f} * hr m/s")
    print(f"modelled  {stats['modelled'] / 1000:.3f} km by heart rate, "
          f"scaled {stats['ratio']:.5f} to "
          f"{(stats['length'] - stats['kept_distance']) / 1000:.3f} km")
    print(f"paced     {stats['records'] - stats['split'] - 1} records at "
          f"{minutes(stats['pace'])} on average")

    if args.dry_run:
        return 0

    open(args.out, 'wb').write(patched)
    print(f"wrote     {args.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
