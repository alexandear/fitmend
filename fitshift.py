"""Shift every timestamp in a FIT file by a constant offset.

The activity keeps its shape: the same records, the same field layout, the
same byte sizes. Only the date_time values change, in place, and the file CRC
is recomputed. Nothing is re-encoded, so a stream this tool does not know
about survives untouched.
"""

import argparse
import datetime
import re
import struct
import sys

# FIT date_time is uint32 seconds since this instant, UTC.
EPOCH = datetime.datetime(1989, 12, 31, tzinfo=datetime.timezone.utc)

UINT32 = 6

# Field 253 is `timestamp` in every message type. The rest are per-message:
# file_id.time_created, session.start_time, lap.start_time and
# activity.local_timestamp. Other uint32 fields are left alone, because a
# plain counter can decode as a plausible date and must not move.
TIMESTAMP = 253
DATE_TIME_FIELDS = {(0, 4), (18, 2), (19, 2), (34, 5)}

RECORD = 20
ACTIVITY = 34

CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]

# 90, 1d, 2h30m, -45m, +1d4h, 1h5m30s
DURATION = re.compile(r'^([+-]?)(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$')

# +3, -5, +05:30, 0
ZONE = re.compile(r'^([+-]?)(\d{1,2})(?::?(\d{2}))?$')


def crc16(data):
    crc = 0
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            table = CRC_TABLE[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ table ^ CRC_TABLE[nibble]
    return crc


def parse_duration(text):
    match = DURATION.match(text.strip())
    if not match or not any(match.group(n) for n in range(2, 6)):
        raise argparse.ArgumentTypeError(f'not a duration: {text}')
    sign, days, hours, minutes, seconds = match.groups()
    total = (int(days or 0) * 86400 + int(hours or 0) * 3600
             + int(minutes or 0) * 60 + int(seconds or 0))
    return -total if sign == '-' else total


def parse_zone(text):
    match = ZONE.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f'not a UTC offset: {text}')
    sign, hours, minutes = match.groups()
    total = int(hours) * 3600 + int(minutes or 0) * 60
    return -total if sign == '-' else total


def parse_datetime(text):
    try:
        return datetime.datetime.fromisoformat(text.strip().replace('/', '-'))
    except ValueError:
        raise argparse.ArgumentTypeError(f'not an ISO date-time: {text}')


def walk(data, visit, on_message=None):
    """Call visit() for every field of every data message; return the data end.

    on_message(global_num) is called once per data message, before its fields,
    for callers that need to know where one message ends and the next begins.

    The returned offset is where the two-byte file CRC starts. Reaching it
    exactly is the proof that no record was misread.
    """
    if len(data) < 14 or data[8:12] != b'.FIT':
        raise ValueError('missing the .FIT header signature')
    header_size = data[0]
    data_size = struct.unpack('<I', data[4:8])[0]
    pos = header_size
    end = header_size + data_size
    definitions = {}

    while pos < end:
        header = data[pos]
        pos += 1

        # A compressed-timestamp header carries a 5-bit offset from a rolling
        # reference timestamp. Shifting every absolute timestamp by the same
        # amount leaves those offsets correct, so there is nothing to patch.
        if header & 0x80:
            pos = read_fields(data, pos, definitions[(header >> 5) & 0x3],
                              visit, on_message)
            continue

        local = header & 0xF
        if not header & 0x40:
            pos = read_fields(data, pos, definitions[local], visit, on_message)
            continue

        pos += 1  # reserved
        endian = '>' if data[pos] else '<'
        pos += 1
        global_num = struct.unpack(endian + 'H', data[pos:pos + 2])[0]
        pos += 2
        count = data[pos]
        pos += 1

        fields = []
        for _ in range(count):
            fields.append((data[pos], data[pos + 1], data[pos + 2], False))
            pos += 3
        if header & 0x20:
            developer_count = data[pos]
            pos += 1
            for _ in range(developer_count):
                fields.append((data[pos], data[pos + 1], UINT32, True))
                pos += 3

        definitions[local] = (global_num, endian, fields)

    if pos != end:
        raise ValueError(f'misread records: ended at {pos}, expected {end}')
    return end


def read_fields(data, pos, definition, visit, on_message=None):
    global_num, endian, fields = definition
    if on_message:
        on_message(global_num)
    for number, size, base_type, developer in fields:
        visit(global_num, number, base_type, endian, pos, size, developer)
        pos += size
    return pos


def is_date_time(global_num, number, base_type, size, developer):
    if developer or size != 4 or base_type & 0x1F != UINT32:
        return False
    return number == TIMESTAMP or (global_num, number) in DATE_TIME_FIELDS


def survey(data):
    """Return the activity span, the shiftable window and the local offset."""
    stamps = []
    track = []
    local = {}

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if not is_date_time(global_num, number, base_type, size, developer):
            return
        value = struct.unpack(endian + 'I', data[pos:pos + 4])[0]
        if global_num == ACTIVITY and number in (5, TIMESTAMP):
            local[number] = value
        if number != TIMESTAMP:
            return
        stamps.append(value)
        if global_num == RECORD:
            track.append(value)

    walk(data, visit)
    if not stamps:
        raise ValueError('no timestamps found')

    # The span is read from the record stream alone. A private message can
    # reuse field 253 for a value that is not a date, and one of those must
    # not be mistaken for the end of the ride.
    span = track or stamps

    # activity.local_timestamp is the same instant as activity.timestamp
    # rendered in the wearer's timezone, so the difference is the offset that
    # a viewer will use to display the activity.
    offset = 0
    if 5 in local and TIMESTAMP in local:
        offset = local[5] - local[TIMESTAMP]
    return min(span), max(span), (min(stamps), max(stamps)), offset


def shift(data, seconds, window, zone=None):
    """Return a copy of data with every date_time inside window moved."""
    out = bytearray(data)
    low, high = window
    count = 0

    def visit(global_num, number, base_type, endian, pos, size, developer):
        nonlocal count
        if not is_date_time(global_num, number, base_type, size, developer):
            return
        value = struct.unpack(endian + 'I', data[pos:pos + 4])[0]
        # Private messages reuse field 253 for values that are not dates at
        # all. Anything outside the activity's own window is not ours to move.
        if not low <= value <= high:
            return
        struct.pack_into(endian + 'I', out, pos, value + seconds)
        count += 1

    end = walk(data, visit)
    if zone is not None:
        set_zone(out, zone)
    # The 14-byte header's own CRC covers bytes 0..11, which do not change.
    struct.pack_into('<H', out, end, crc16(bytes(out[:end])))
    return bytes(out), count


def set_zone(out, seconds):
    """Rewrite activity.local_timestamp so the file declares this UTC offset.

    A viewer that trusts the file reads the offset as local_timestamp minus
    timestamp, so the two must agree with where the ride actually happened.
    """
    found = {}

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if global_num != ACTIVITY or developer or size != 4:
            return
        if base_type & 0x1F != UINT32 or number not in (5, TIMESTAMP):
            return
        found[number] = (pos, endian)

    walk(bytes(out), visit)
    if TIMESTAMP not in found or 5 not in found:
        return False
    stamp_pos, endian = found[TIMESTAMP]
    stamp = struct.unpack(endian + 'I', bytes(out[stamp_pos:stamp_pos + 4]))[0]
    local_pos, endian = found[5]
    struct.pack_into(endian + 'I', out, local_pos, stamp + seconds)
    return True


def seconds_zone(seconds):
    sign = '-' if seconds < 0 else '+'
    hours, minutes = divmod(abs(seconds) // 60, 60)
    return f'{sign}{hours}' + (f':{minutes:02d}' if minutes else '')


def render(stamp, offset):
    utc = EPOCH + datetime.timedelta(seconds=stamp)
    local = utc + datetime.timedelta(seconds=offset)
    return f"{local:%Y-%m-%d %H:%M:%S} local / {utc:%Y-%m-%d %H:%M:%S} UTC"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='FIT file to read')
    parser.add_argument('-o', '--out', help='output FIT file')
    when = parser.add_mutually_exclusive_group(required=True)
    when.add_argument('--to', type=parse_datetime, metavar='DATETIME',
                      help='move the start to this date-time, e.g. "2026-09-30 07:16"')
    when.add_argument('--by', type=parse_duration, metavar='DURATION',
                      help='shift by this much, e.g. 1d, -2h30m, 28615')
    parser.add_argument('--utc', action='store_true',
                        help='read --to as UTC instead of the activity local time')
    parser.add_argument('--zone', type=parse_zone, metavar='OFFSET',
                        help='the UTC offset the ride was ridden in, e.g. +3; '
                             'reads --to as a time in that zone and records it '
                             'in the file')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='report the shift without writing')
    args = parser.parse_args()

    if not args.out and not args.dry_run:
        parser.error('-o/--out is required unless --dry-run')

    data = open(args.input, 'rb').read()
    try:
        start, last, window, offset = survey(data)
    except (ValueError, IndexError, KeyError, struct.error) as err:
        print(f'{args.input}: not a readable FIT file: {err}', file=sys.stderr)
        return 1
    if args.utc:
        offset = 0
    if args.zone is not None:
        offset = args.zone

    seconds = args.by
    if seconds is None:
        target = args.to.replace(tzinfo=datetime.timezone.utc)
        seconds = int((target - EPOCH).total_seconds()) - offset - start

    print(f"start  {render(start, offset)}")
    print(f"end    {render(last, offset)}")
    print(f"shift  {seconds}s")
    print(f"new    {render(start + seconds, offset)}")

    if args.dry_run:
        return 0

    # Every timestamp in the file moves together, so the streams keep their
    # relative order. The one-day margin covers a local_timestamp that falls
    # on the far side of the UTC day from the values it sits between.
    low, high = window
    patched, count = shift(data, seconds, (low - 86400, high + 86400), args.zone)
    open(args.out, 'wb').write(patched)
    print(f"moved  {count} timestamps")
    if args.zone is not None:
        print(f"zone   recorded as UTC{seconds_zone(args.zone)}")
    print(f"wrote  {args.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
