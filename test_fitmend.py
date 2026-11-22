"""Tests for the library half of each tool.

Every test builds the FIT bytes it needs with encode(), so none of them
depends on a real activity or on fit-tool.
"""

import argparse
import datetime
import struct
import tempfile
import unittest
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import fithills
import fitpace
import fitroute
import fitshift
import fitstop

# (base type byte, size, struct code) per field type a test may write.
BASE = {
    'uint8': (0x02, 1, 'B'),
    'uint16': (0x84, 2, 'H'),
    'uint32': (0x86, 4, 'I'),
    'sint32': (0x85, 4, 'i'),
}

FILE_ID = 0
SESSION = 18
LAP = 19
ACTIVITY = 34
PRIVATE = 0xFF01

START = 1_000_000  # an arbitrary FIT date_time, seconds since 1989-12-31
ZONE = 3 * 3600

# 0x7FFFFFFF semicircles is 180.0 degrees: the position a jammed Garmin writes.
SENTINEL = 0x7FFFFFFF

# A field to encode: its number, a key of BASE, and its value.
type Field = tuple[int, str, int]
# A data message to encode: its global_num and fields.
type Message = tuple[int, list[Field]]
# A record to encode: its timestamp and the fields besides it.
type Record = tuple[int, list[Field]]


def raw(body: bytes | bytearray) -> bytes:
    """Return the bytes of a FIT file whose message stream is body."""
    header = struct.pack('<BBHI4s', 14, 0x20, 2158, len(body), b'.FIT')
    header += struct.pack('<H', fitshift.crc16(header[:12]))
    data = header + bytes(body)
    return data + struct.pack('<H', fitshift.crc16(data))


def encode(messages: Sequence[Message]) -> bytes:
    """Return the bytes of a FIT file holding these data messages.

    messages is [(global_num, [(field number, type, value), ...]), ...]. Each
    message gets its own definition, which a real file does not bother with
    but a parser has to accept.
    """
    body = bytearray()
    for global_num, fields in messages:
        body += bytes([0x40, 0, 0]) + struct.pack('<HB', global_num, len(fields))
        for number, kind, _ in fields:
            code, size, _ = BASE[kind]
            body += bytes([number, size, code])
        body.append(0x00)
        for _, kind, value in fields:
            _, _, fmt = BASE[kind]
            body += struct.pack('<' + fmt, value)
    return raw(body)


def semicircles(degrees: float) -> int:
    return round(degrees * fitroute.SEMICIRCLES)


def activity(records: Sequence[Record], extra: Sequence[Message] = ()) -> bytes:
    """A file_id, these records, and a lap, session and activity around them."""
    first, last = records[0][0], records[-1][0]
    messages: list[Message] = [(FILE_ID, [(0, 'uint8', 4), (4, 'uint32', first)])]
    for stamp, fields in records:
        messages.append((fitshift.RECORD, [(253, 'uint32', stamp), *fields]))
    messages += [
        (
            LAP,
            [
                (253, 'uint32', last),
                (2, 'uint32', first),
                (7, 'uint32', (last - first) * 1000),
                (8, 'uint32', (last - first) * 1000),
                (9, 'uint32', 0),
                (3, 'sint32', SENTINEL),
                (4, 'sint32', SENTINEL),
                (5, 'sint32', SENTINEL),
                (6, 'sint32', SENTINEL),
                (27, 'sint32', SENTINEL),
                (28, 'sint32', SENTINEL),
                (29, 'sint32', SENTINEL),
                (30, 'sint32', SENTINEL),
                (13, 'uint16', 0),
                (14, 'uint16', 0),
            ],
        ),
        (
            SESSION,
            [
                (253, 'uint32', last),
                (2, 'uint32', first),
                (7, 'uint32', (last - first) * 1000),
                (8, 'uint32', (last - first) * 1000),
                (9, 'uint32', 0),
                (3, 'sint32', SENTINEL),
                (4, 'sint32', SENTINEL),
                (38, 'sint32', SENTINEL),
                (39, 'sint32', SENTINEL),
                (29, 'sint32', SENTINEL),
                (30, 'sint32', SENTINEL),
                (31, 'sint32', SENTINEL),
                (32, 'sint32', SENTINEL),
                (14, 'uint16', 0),
                (15, 'uint16', 0),
            ],
        ),
        (ACTIVITY, [(253, 'uint32', last), (5, 'uint32', last + ZONE)]),
    ]
    return encode(messages + list(extra))


def route(
    path: Path,
    points: Sequence[fitroute.Point],
    elevations: Sequence[float] | None = None,
) -> Path:
    """Write a GPX track of these (lat, lon) points and return the path."""
    body = [
        '<?xml version="1.0"?>',
        '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>',
    ]
    for index, (lat, lon) in enumerate(points):
        body.append(f'<trkpt lat="{lat}" lon="{lon}">')
        if elevations is not None:
            body.append(f'<ele>{elevations[index]}</ele>')
        body.append('</trkpt>')
    body.append('</trkseg></trk></gpx>')
    path.write_text('\n'.join(body))
    return path


def sint32_fields(data: bytes, global_num: int) -> dict[int, int]:
    """Each sint32 field of the first message of this type, by number."""
    found: dict[int, int] = {}

    def visit(
        kind: int,
        number: int,
        base_type: int,
        endian: str,
        pos: int,
        size: int,
        developer: bool,
    ) -> None:
        if kind == global_num and size == 4 and base_type == BASE['sint32'][0]:
            found.setdefault(number, struct.unpack_from(endian + 'i', data, pos)[0])

    fitshift.walk(data, visit)
    return found


def north(
    count: int, metres: float, lat: float = 50.0, lon: float = 30.0
) -> list[fitroute.Point]:
    """count points going north from (lat, lon), metres apart."""
    step = metres / (fitroute.EARTH_RADIUS * 3.141592653589793 / 180.0)
    return [(lat + step * index, lon) for index in range(count)]


class Crc(unittest.TestCase):
    def test_zero_bytes_leave_the_crc_zero(self) -> None:
        self.assertEqual(fitshift.crc16(b''), 0)
        self.assertEqual(fitshift.crc16(b'\x00' * 8), 0)

    def test_a_single_bit_changes_it(self) -> None:
        self.assertNotEqual(fitshift.crc16(b'fitmend'), fitshift.crc16(b'fitmene'))

    def test_an_encoded_file_carries_its_own_crc(self) -> None:
        data = activity([(START, [(5, 'uint32', 0)])])
        end = len(data) - 2
        self.assertEqual(struct.unpack('<H', data[end:])[0], fitshift.crc16(data[:end]))


class Walk(unittest.TestCase):
    def setUp(self) -> None:
        self.data = activity([(START + i, [(5, 'uint32', i * 100)]) for i in range(3)])

    def test_it_ends_on_the_file_crc(self) -> None:
        end = fitshift.walk(self.data, lambda *args: None)
        self.assertEqual(end, len(self.data) - 2)

    def test_it_visits_every_field_of_every_message(self) -> None:
        seen = []
        fitshift.walk(
            self.data,
            lambda global_num, number, *rest: seen.append((global_num, number)),
        )
        self.assertEqual(seen.count((fitshift.RECORD, 253)), 3)
        self.assertEqual(seen.count((fitshift.RECORD, 5)), 3)
        self.assertIn((FILE_ID, 4), seen)

    def test_a_truncated_file_is_a_misread_not_a_guess(self) -> None:
        broken = bytearray(self.data)
        struct.pack_into('<I', broken, 4, len(self.data) - 20)
        with self.assertRaisesRegex(ValueError, 'misread records'):
            fitshift.walk(bytes(broken), lambda *args: None)

    def test_it_needs_the_signature(self) -> None:
        with self.assertRaisesRegex(ValueError, 'signature'):
            fitshift.walk(b'not a fit file at all', lambda *args: None)


class Headers(unittest.TestCase):
    """The two message headers encode() never writes."""

    def test_a_compressed_timestamp_header_carries_no_timestamp_field(self) -> None:
        # Definition for local type 0: record, distance alone.
        body = bytearray([0x40, 0, 0])
        body += struct.pack('<HB', fitshift.RECORD, 1)
        body += bytes([5, 4, BASE['uint32'][0]])
        # The data message: bit 7 set, local type 0 in bits 5-6, and a 5-bit
        # offset from the rolling reference timestamp in the rest.
        body += bytes([0x80 | 0x05])
        body += struct.pack('<I', 1234)
        data = raw(body)

        seen = []

        def visit(
            global_num: int,
            number: int,
            base_type: int,
            endian: str,
            pos: int,
            size: int,
            developer: bool,
        ) -> None:
            seen.append((global_num, number, struct.unpack_from('<I', data, pos)[0]))

        end = fitshift.walk(data, visit)
        self.assertEqual(seen, [(fitshift.RECORD, 5, 1234)])
        self.assertEqual(end, len(data) - 2)

    def test_a_record_under_one_is_patched_like_any_other(self) -> None:
        # The offset in the header is relative to the timestamp before it, so
        # a shift that moves every absolute timestamp by the same amount
        # leaves it correct and the fields under the header move with it.
        body = bytearray([0x40, 0, 0])
        body += struct.pack('<HB', fitshift.RECORD, 1)
        body += bytes([253, 4, BASE['uint32'][0]])
        body += bytes([0x00]) + struct.pack('<I', START)
        body += bytes([0x80 | 0x05]) + struct.pack('<I', START + 1)
        data = raw(body)

        out, count = fitshift.shift(data, 86400, (START - 10, START + 10))
        self.assertEqual(count, 2)

        moved = []

        def visit(
            global_num: int,
            number: int,
            base_type: int,
            endian: str,
            pos: int,
            size: int,
            developer: bool,
        ) -> None:
            moved.append(struct.unpack_from('<I', out, pos)[0])

        fitshift.walk(out, visit)
        self.assertEqual(moved, [START + 86400, START + 1 + 86400])

    def test_a_developer_field_is_walked_but_never_moved(self) -> None:
        body = bytearray([0x60, 0, 0])  # definition, with developer fields
        body += struct.pack('<HB', fitshift.RECORD, 1)
        body += bytes([253, 4, BASE['uint32'][0]])
        body += bytes([1])  # one developer field
        body += bytes([253, 4, 0])  # its number, its size, its data index
        body += bytes([0x00])
        body += struct.pack('<II', START, START)
        data = raw(body)

        seen = []

        def visit(
            global_num: int,
            number: int,
            base_type: int,
            endian: str,
            pos: int,
            size: int,
            developer: bool,
        ) -> None:
            seen.append(developer)

        self.assertEqual(fitshift.walk(data, visit), len(data) - 2)
        self.assertEqual(seen, [False, True])

        # Both fields are a uint32 holding the same date; only the real one
        # is a date_time.
        out, count = fitshift.shift(data, 86400, (START - 10, START + 10))
        self.assertEqual(count, 1)
        self.assertEqual(struct.unpack_from('<I', out, len(out) - 6)[0], START)


class IsDateTime(unittest.TestCase):
    def test_field_253_is_a_date_in_any_message(self) -> None:
        self.assertTrue(fitshift.is_date_time(PRIVATE, 253, 0x86, 4, False))

    def test_an_allowlisted_field_is_a_date(self) -> None:
        self.assertTrue(fitshift.is_date_time(FILE_ID, 4, 0x86, 4, False))
        self.assertTrue(fitshift.is_date_time(ACTIVITY, 5, 0x86, 4, False))

    def test_another_uint32_is_not(self) -> None:
        # record.distance decodes as a plausible date and must not move.
        self.assertFalse(fitshift.is_date_time(fitshift.RECORD, 5, 0x86, 4, False))

    def test_a_developer_field_is_not(self) -> None:
        self.assertFalse(fitshift.is_date_time(fitshift.RECORD, 253, 0x86, 4, True))


class Survey(unittest.TestCase):
    def test_it_reads_the_span_and_the_local_offset(self) -> None:
        data = activity([(START + i, [(5, 'uint32', 0)]) for i in range(5)])
        first, last, window, offset = fitshift.survey(data)
        self.assertEqual((first, last), (START, START + 4))
        self.assertEqual(window, (START, START + 4))
        self.assertEqual(offset, ZONE)

    def test_the_span_comes_from_the_records_alone(self) -> None:
        # A private message reusing field 253 for a counter is not the end.
        data = activity(
            [(START + i, [(5, 'uint32', 0)]) for i in range(3)],
            extra=[(PRIVATE, [(253, 'uint32', START + 99999)])],
        )
        _, last, _, _ = fitshift.survey(data)
        self.assertEqual(last, START + 2)

    def test_the_invalid_sentinel_is_not_a_timestamp(self) -> None:
        # With no records to go on, the summaries are the span; the sentinel
        # would otherwise be the end of the activity.
        data = encode(
            [
                (FILE_ID, [(0, 'uint8', 4), (4, 'uint32', START)]),
                (SESSION, [(253, 'uint32', START + 10), (2, 'uint32', START)]),
                (PRIVATE, [(253, 'uint32', fitshift.INVALID)]),
            ]
        )
        _, last, window, _ = fitshift.survey(data)
        self.assertEqual(last, START + 10)
        self.assertLess(window[1], fitshift.INVALID)

    def test_a_file_with_no_timestamp_at_all_is_refused(self) -> None:
        data = encode([(FILE_ID, [(0, 'uint8', 4), (4, 'uint32', START)])])
        with self.assertRaisesRegex(ValueError, 'no timestamps'):
            fitshift.survey(data)

    def test_a_private_counter_does_not_stretch_the_window(self) -> None:
        data = activity(
            [(START + i, [(5, 'uint32', 0)]) for i in range(3)],
            extra=[(PRIVATE, [(253, 'uint32', 7)])],
        )
        _, _, window, _ = fitshift.survey(data)
        self.assertEqual(window, (START, START + 2))


class Shift(unittest.TestCase):
    def setUp(self) -> None:
        self.data = activity(
            [(START + i, [(5, 'uint32', 0)]) for i in range(3)],
            extra=[
                (PRIVATE, [(253, 'uint32', 7)]),
                (PRIVATE, [(253, 'uint32', fitshift.INVALID)]),
            ],
        )
        _, _, self.window, _ = fitshift.survey(self.data)

    def stamps(self, data: bytes) -> list[tuple[int, int, int]]:
        found: list[tuple[int, int, int]] = []

        def visit(
            global_num: int,
            number: int,
            base_type: int,
            endian: str,
            pos: int,
            size: int,
            developer: bool,
        ) -> None:
            if fitshift.is_date_time(global_num, number, base_type, size, developer):
                found.append(
                    (global_num, number, struct.unpack_from(endian + 'I', data, pos)[0])
                )

        fitshift.walk(data, visit)
        return found

    def test_every_timestamp_in_the_window_moves_together(self) -> None:
        low, high = self.window
        out, count = fitshift.shift(self.data, 86400, (low - 86400, high + 86400))
        # file_id, three records, the lap and session start and timestamp,
        # and the activity's timestamp and local_timestamp.
        self.assertEqual(count, 10)
        moved = {(g, n): v for g, n, v in self.stamps(out)}
        self.assertEqual(moved[(fitshift.RECORD, 253)], START + 2 + 86400)
        self.assertEqual(moved[(ACTIVITY, 5)], START + 2 + ZONE + 86400)

    def test_a_value_outside_the_window_stays(self) -> None:
        low, high = self.window
        out, _ = fitshift.shift(self.data, 86400, (low - 86400, high + 86400))
        values = [v for g, n, v in self.stamps(out) if g == PRIVATE]
        self.assertIn(7, values)
        self.assertIn(fitshift.INVALID, values)

    def test_the_file_crc_is_recomputed(self) -> None:
        low, high = self.window
        out, _ = fitshift.shift(self.data, 86400, (low - 86400, high + 86400))
        end = fitshift.walk(out, lambda *args: None)
        self.assertEqual(struct.unpack('<H', out[end:])[0], fitshift.crc16(out[:end]))

    def test_the_implied_offset_survives_the_shift(self) -> None:
        low, high = self.window
        out, _ = fitshift.shift(self.data, -3600, (low - 86400, high + 86400))
        self.assertEqual(fitshift.survey(out)[3], ZONE)

    def test_zone_rewrites_the_offset_instead(self) -> None:
        low, high = self.window
        out, _ = fitshift.shift(self.data, 0, (low - 86400, high + 86400), -7200)
        self.assertEqual(fitshift.survey(out)[3], -7200)


class SetZone(unittest.TestCase):
    def test_it_reports_what_it_could_not_find(self) -> None:
        # A file without an activity message declares no offset to rewrite.
        data = activity([(START, [(5, 'uint32', 0)])])
        self.assertTrue(fitshift.set_zone(bytearray(data), ZONE))

        data = encode([(fitshift.RECORD, [(253, 'uint32', START)])])
        self.assertFalse(fitshift.set_zone(bytearray(data), ZONE))


class Parsing(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(fitshift.parse_duration('1d'), 86400)
        self.assertEqual(fitshift.parse_duration('2h30m'), 9000)
        self.assertEqual(fitshift.parse_duration('-45m'), -2700)
        self.assertEqual(fitshift.parse_duration('90'), 90)

    def test_a_duration_needs_a_number(self) -> None:
        for text in ('', 'tomorrow', '+'):
            with self.assertRaises(argparse.ArgumentTypeError):
                fitshift.parse_duration(text)

    def test_zones(self) -> None:
        self.assertEqual(fitshift.parse_zone('+3'), 10800)
        self.assertEqual(fitshift.parse_zone('-5'), -18000)
        self.assertEqual(fitshift.parse_zone('+05:30'), 19800)
        self.assertEqual(fitshift.parse_zone('0'), 0)

    def test_a_zone_is_not_free_text(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            fitshift.parse_zone('Europe/Kyiv')

    def test_date_times(self) -> None:
        self.assertEqual(
            fitshift.parse_datetime('2026-09-30 07:16'),
            datetime.datetime(2026, 9, 30, 7, 16),  # noqa: DTZ001
        )
        # The form a filename tends to carry.
        self.assertEqual(
            fitshift.parse_datetime('2026/09/30 07:16:05'),
            datetime.datetime(2026, 9, 30, 7, 16, 5),  # noqa: DTZ001
        )

    def test_a_date_time_is_not_free_text(self) -> None:
        for text in ('', 'yesterday', '30.09.2026'):
            with self.assertRaises(argparse.ArgumentTypeError):
                fitshift.parse_datetime(text)

    def test_seconds_zone_renders_the_offset(self) -> None:
        self.assertEqual(fitshift.seconds_zone(10800), '+3')
        self.assertEqual(fitshift.seconds_zone(19800), '+5:30')
        self.assertEqual(fitshift.seconds_zone(-18000), '-5')
        self.assertEqual(fitshift.seconds_zone(0), '+0')

    def test_render_shows_both_clocks(self) -> None:
        # date_time 0 is the FIT epoch itself.
        self.assertEqual(
            fitshift.render(0, ZONE),
            '1989-12-31 03:00:00 local / 1989-12-31 00:00:00 UTC',
        )


class Geometry(unittest.TestCase):
    def test_a_degree_of_latitude(self) -> None:
        metres = fitroute.haversine((50.0, 30.0), (51.0, 30.0))
        self.assertAlmostEqual(metres, 111194.9, delta=1.0)

    def test_measure_runs_along_the_points(self) -> None:
        cumulative = fitroute.measure(north(5, 100))
        self.assertEqual(len(cumulative), 5)
        self.assertAlmostEqual(cumulative[-1], 400, delta=0.5)

    def test_locate_interpolates_between_points(self) -> None:
        points = north(5, 100)
        cumulative = fitroute.measure(points)
        lat, lon = fitroute.locate(points, cumulative, 150)
        self.assertAlmostEqual(lat, (points[1][0] + points[2][0]) / 2)
        self.assertEqual(lon, 30.0)

    def test_locate_pins_past_the_end(self) -> None:
        points = north(5, 100)
        cumulative = fitroute.measure(points)
        self.assertEqual(fitroute.locate(points, cumulative, 10000), points[-1])

    def test_project_finds_how_far_off_and_how_far_along(self) -> None:
        points = north(5, 100)
        cumulative = fitroute.measure(points)
        lat, lon = fitroute.locate(points, cumulative, 250)
        # A tenth of a degree of longitude is well off a north-south route.
        off, along = fitroute.project(
            points, cumulative, (lat, lon + 0.001), 0, cumulative[-1]
        )
        self.assertAlmostEqual(along, 250, delta=1.0)
        self.assertAlmostEqual(off, 71.6, delta=1.0)

    def test_project_only_looks_inside_its_window(self) -> None:
        # What a route passing the same street twice rests on: a fix 100 m
        # along cannot be matched there from a window 500 m along.
        points = north(9, 100)
        cumulative = fitroute.measure(points)
        fix = fitroute.locate(points, cumulative, 100)
        off, along = fitroute.project(points, cumulative, fix, 500, 800)
        self.assertGreater(along, 390)
        self.assertGreater(off, 290)


class Height(unittest.TestCase):
    def test_it_interpolates_between_track_points(self) -> None:
        self.assertAlmostEqual(
            fitroute.height([0.0, 100.0, 200.0], [10.0, 20.0, 30.0], 150.0), 25.0
        )

    def test_past_either_end_the_nearest_point_stands(self) -> None:
        cumulative, elevations = [0.0, 100.0, 200.0], [10.0, 20.0, 30.0]
        self.assertAlmostEqual(fitroute.height(cumulative, elevations, 9999.0), 30.0)
        self.assertAlmostEqual(fitroute.height(cumulative, elevations, -50.0), 10.0)


class Interpolate(unittest.TestCase):
    def test_between_anchors(self) -> None:
        anchors = [(0.0, 0.0), (100.0, 200.0)]
        self.assertAlmostEqual(fitroute.interpolate(anchors, 50), 100.0)

    def test_before_the_first_anchor_the_offset_carries_back(self) -> None:
        anchors = [(100.0, 150.0), (200.0, 250.0)]
        self.assertAlmostEqual(fitroute.interpolate(anchors, 0), 50.0)

    def test_past_the_last_anchor_the_offset_carries_on(self) -> None:
        anchors = [(0.0, 0.0), (100.0, 150.0)]
        self.assertAlmostEqual(fitroute.interpolate(anchors, 200), 250.0)


class Retime(unittest.TestCase):
    def test_a_jump_takes_the_median_pace_of_the_rest(self) -> None:
        times = list(range(10))
        distances = [0, 3, 6, 9, 109, 112, 115, 118, 121, 124]
        rebuilt, jumps, dropped = fitroute.retime(times, distances, 5.0)
        self.assertEqual(jumps, [4])
        self.assertAlmostEqual(dropped, 97.0)
        self.assertEqual(rebuilt, [0, 3, 6, 9, 12, 15, 18, 21, 24, 27])

    def test_a_plausible_pace_is_left_exactly_as_recorded(self) -> None:
        times = list(range(5))
        distances = [0.0, 2.5, 5.0, 7.5, 10.0]
        rebuilt, jumps, dropped = fitroute.retime(times, distances, 5.0)
        self.assertEqual(jumps, [])
        self.assertEqual(dropped, 0.0)
        self.assertEqual(rebuilt, distances)

    def test_distance_going_backwards_is_a_jump_too(self) -> None:
        rebuilt, jumps, _ = fitroute.retime([0, 1, 2, 3], [0, 3, -50, -47], 5.0)
        self.assertEqual(jumps, [2])
        self.assertEqual(rebuilt, [0, 3, 6, 9])

    def test_a_repeated_timestamp_adds_no_distance(self) -> None:
        rebuilt, jumps, _ = fitroute.retime([0, 1, 1, 2], [0, 3, 3, 6], 5.0)
        self.assertEqual(jumps, [])
        self.assertEqual(rebuilt, [0, 3, 3, 6])

    def test_nothing_plausible_is_nothing_to_rebuild_from(self) -> None:
        with self.assertRaisesRegex(ValueError, 'rebuild'):
            fitroute.retime([0, 1, 2], [0, 500, 1000], 5.0)


class ReadRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name: str = 'route.gpx') -> Path:
        return Path(self.dir.name) / name

    def test_it_reads_the_track_points(self) -> None:
        points = fitroute.read_route(route(self.path(), north(3, 100)))
        self.assertEqual(len(points), 3)
        self.assertAlmostEqual(points[0][0], 50.0)
        self.assertAlmostEqual(points[0][1], 30.0)

    def test_two_points_are_the_least_a_route_can_be(self) -> None:
        with self.assertRaisesRegex(ValueError, 'two track points'):
            fitroute.read_route(route(self.path(), north(1, 100)))

    def test_elevations_come_back_per_point(self) -> None:
        path = route(self.path(), north(3, 100), [10.0, 20.0, 30.0])
        self.assertEqual(fitroute.read_elevations(path), [10.0, 20.0, 30.0])

    def test_one_missing_ele_is_no_profile(self) -> None:
        path = self.path()
        path.write_text(
            '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
            '<trkpt lat="50.0" lon="30.0"><ele>1.0</ele></trkpt>'
            '<trkpt lat="50.1" lon="30.0"/></trkseg></trk></gpx>'
        )
        self.assertIsNone(fitroute.read_elevations(path))


class Graft(unittest.TestCase):
    """Positions are written into the records, and only those bytes move."""

    def setUp(self) -> None:
        self.points = north(5, 100)
        self.cumulative = fitroute.measure(self.points)

    def ride(self, fixes: Sequence[fitroute.Point | None]) -> bytes:
        """Five records 100 m apart, each with the given (lat, lon) or None."""
        records = []
        for index, fix in enumerate(fixes):
            lat, lon = (
                (SENTINEL, SENTINEL)
                if fix is None
                else (semicircles(fix[0]), semicircles(fix[1]))
            )
            records.append(
                (
                    START + index * 60,
                    [
                        (5, 'uint32', index * 100 * 100),
                        (0, 'sint32', lat),
                        (1, 'sint32', lon),
                    ],
                )
            )
        return activity(records)

    def test_the_sentinel_is_replaced_along_the_route(self) -> None:
        data = self.ride([None] * 5)
        out, stats = fitroute.graft(data, self.points)
        self.assertEqual((stats['placed'], stats['skipped']), (5, 0))
        records, _ = fitroute.read_records(out)
        for index, record in enumerate(records):
            self.assertAlmostEqual(record['fix'][0], self.points[index][0], places=5)

    def test_distance_and_timestamps_are_untouched(self) -> None:
        data = self.ride([None] * 5)
        out, _ = fitroute.graft(data, self.points)
        before, _ = fitroute.read_records(data)
        after, _ = fitroute.read_records(out)
        self.assertEqual(
            [r['distance'] for r in before], [r['distance'] for r in after]
        )
        self.assertEqual([r['stamp'] for r in before], [r['stamp'] for r in after])
        self.assertEqual(len(data), len(out))

    def test_gaps_only_keeps_a_fix_that_lies_on_the_route(self) -> None:
        fixes = [None, self.points[1], None, None, None]
        _, stats = fitroute.graft(self.ride(fixes), self.points, gaps_only=True)
        self.assertEqual(stats['kept'], 1)
        self.assertEqual(stats['placed'], 4)
        self.assertEqual(stats['anchors'], 1)

    def test_gaps_only_replaces_a_fix_that_does_not(self) -> None:
        fixes = [None, (50.5, 30.5), None, None, None]
        _, stats = fitroute.graft(self.ride(fixes), self.points, gaps_only=True)
        self.assertEqual(stats['kept'], 0)
        self.assertEqual(stats['placed'], 5)

    def test_broken_only_keeps_a_real_fix_off_the_route(self) -> None:
        # 200 m east of the route: too far to be on it, too near to be spoofed.
        lat, lon = self.points[2]
        fixes = [None, None, (lat, lon + 0.0028), None, None]
        _, stats = fitroute.graft(self.ride(fixes), self.points, broken_only=True)
        self.assertEqual(stats['off_route'], 1)
        self.assertEqual(stats['placed'], 4)

    def test_the_scale_stretches_the_ride_onto_the_route(self) -> None:
        data = self.ride([None] * 5)
        _, stats = fitroute.graft(data, north(5, 200))
        self.assertAlmostEqual(stats['ratio'], 2.0, places=3)

    def test_no_scale_matches_raw_distance(self) -> None:
        data = self.ride([None] * 5)
        out, stats = fitroute.graft(data, north(9, 100), scale=False)
        self.assertEqual(stats['ratio'], 1.0)
        records, _ = fitroute.read_records(out)
        self.assertAlmostEqual(records[-1]['fix'][0], north(9, 100)[4][0], places=5)

    def test_max_speed_rebuilds_the_axis_the_match_runs_on(self) -> None:
        records = [
            (
                START + index,
                [
                    (5, 'uint32', distance * 100),
                    (0, 'sint32', SENTINEL),
                    (1, 'sint32', SENTINEL),
                ],
            )
            for index, distance in enumerate([0, 3, 6, 1009, 1012])
        ]
        data = activity(records)
        _, stats = fitroute.graft(data, self.points, limit=5.0)
        self.assertEqual(stats['replaced'], 1)
        self.assertAlmostEqual(stats['dropped'], 1000.0)
        self.assertAlmostEqual(stats['ride_length'], 12.0)

    def test_reverse_runs_the_route_backwards(self) -> None:
        data = self.ride([None] * 5)
        out, _ = fitroute.graft(data, self.points, reverse=True)
        records, _ = fitroute.read_records(out)
        self.assertAlmostEqual(records[0]['fix'][0], self.points[-1][0], places=5)
        self.assertAlmostEqual(records[-1]['fix'][0], self.points[0][0], places=5)

    def test_a_ride_with_no_distance_field_has_nothing_to_match_on(self) -> None:
        records = [(START + index, [(3, 'uint8', 140)]) for index in range(5)]
        with self.assertRaisesRegex(ValueError, 'no record.distance'):
            fitroute.graft(activity(records), self.points)

    def test_a_ride_that_never_moved_has_nothing_to_match_on(self) -> None:
        records = [(START + index, [(5, 'uint32', 0)]) for index in range(5)]
        with self.assertRaisesRegex(ValueError, 'no distance to match'):
            fitroute.graft(activity(records), self.points)

    def test_a_recording_without_altitude_has_no_climbs_to_anchor(self) -> None:
        # --hills rests on the recording's own altimeter; these records were
        # written without one, so the two ends of the scale are all there is.
        _, anchors, _, stats = fitroute.match(
            self.ride([None] * 5),
            self.points,
            elevations=[0.0, 10.0, 20.0, 30.0, 40.0],
        )
        self.assertEqual(stats['hills'], 0)
        self.assertEqual(len(anchors), 2)

    def test_the_summary_positions_come_from_the_records(self) -> None:
        out, _ = fitroute.graft(self.ride([None] * 5), self.points)
        session = sint32_fields(out, SESSION)
        lap = sint32_fields(out, LAP)
        self.assertEqual(session[3], semicircles(self.points[0][0]))
        self.assertEqual(session[38], semicircles(self.points[-1][0]))
        # 27, 28 are the lap's north-east corner and 29, 30 its south-west.
        self.assertEqual(lap[27], semicircles(self.points[-1][0]))
        self.assertEqual(lap[29], semicircles(self.points[0][0]))
        self.assertEqual(lap[30], semicircles(30.0))

    def test_a_summary_without_a_start_time_keeps_its_positions(self) -> None:
        # There is no span to gather the fixes of, so nothing is written back.
        records = [
            (
                fitshift.RECORD,
                [
                    (253, 'uint32', START + index),
                    (5, 'uint32', index * 100 * 100),
                    (0, 'sint32', SENTINEL),
                    (1, 'sint32', SENTINEL),
                ],
            )
            for index in range(5)
        ]
        data = encode(
            [*records, (SESSION, [(3, 'sint32', SENTINEL), (4, 'sint32', SENTINEL)])]
        )
        out, stats = fitroute.graft(data, self.points)
        self.assertEqual(stats['placed'], 5)
        self.assertEqual(sint32_fields(out, SESSION), {3: SENTINEL, 4: SENTINEL})

    def test_a_record_with_no_position_field_is_skipped(self) -> None:
        records = [
            (START + index, [(5, 'uint32', index * 100 * 100)]) for index in range(5)
        ]
        _, stats = fitroute.graft(activity(records), self.points)
        self.assertEqual((stats['placed'], stats['skipped']), (0, 5))

    def test_the_file_crc_is_recomputed(self) -> None:
        out, _ = fitroute.graft(self.ride([None] * 5), self.points)
        end = fitshift.walk(out, lambda *args: None)
        self.assertEqual(struct.unpack('<H', out[end:])[0], fitshift.crc16(out[:end]))


class Align(unittest.TestCase):
    """The anchors the climbs pin down between the trusted fixes.

    test_tracks.Hills runs this over a real altimeter; these cases are the
    profiles that must add nothing, which no real file happened to hold.
    """

    def setUp(self) -> None:
        self.points = north(50, 100)
        self.climb = [index * 2.0 for index in range(50)]
        self.flat = [100.0] * 50
        # The same shape as the climb, a twentieth of the relief.
        self.gentle = [index * 0.1 for index in range(50)]

    def ride(self, altitudes: Sequence[float]) -> bytes:
        """One record every 100 m, each carrying its altitude in metres."""
        records = [
            (
                START + index * 10,
                [
                    (5, 'uint32', index * 100 * 100),
                    (78, 'uint32', round((altitude + 500) * 5)),
                    (0, 'sint32', SENTINEL),
                    (1, 'sint32', SENTINEL),
                ],
            )
            for index, altitude in enumerate(altitudes)
        ]
        return activity(records)

    def test_a_climb_that_follows_the_route_anchors_the_placement(self) -> None:
        _, anchors, _, stats = fitroute.match(
            self.ride(self.climb), self.points, elevations=self.climb
        )
        self.assertGreater(stats['hills'], 0)
        # The ride measures the same metres the route does, so each anchor
        # lands where it was ridden, give or take the lag that was searched.
        for ride, route in anchors:
            self.assertAlmostEqual(ride, route, delta=fitroute.HILL_LAG)

    def test_a_window_without_the_relief_to_go_on_is_passed_over(self) -> None:
        # It follows the route's climb exactly, but a few metres over three
        # kilometres is within what an altimeter drifts by.
        _, anchors, _, stats = fitroute.match(
            self.ride(self.gentle), self.points, elevations=self.climb
        )
        self.assertEqual(stats['hills'], 0)
        self.assertEqual(len(anchors), 2)

    def test_a_flat_route_has_nothing_to_match_against(self) -> None:
        # Correlation is undefined against a constant, so no lag wins.
        _, anchors, _, stats = fitroute.match(
            self.ride(self.climb), self.points, elevations=self.flat
        )
        self.assertEqual(stats['hills'], 0)
        self.assertEqual(len(anchors), 2)


class Unjam(unittest.TestCase):
    """The distance stream itself, rebuilt from the pace that is a pace."""

    def ride(self, distances: Sequence[float], speed: float = 3.0) -> bytes:
        records = [
            (
                START + index,
                [
                    (5, 'uint32', round(distance * 100)),
                    (73, 'uint32', round(speed * 1000)),
                    (3, 'uint8', 140),
                ],
            )
            for index, distance in enumerate(distances)
        ]
        return activity(records)

    def test_the_jump_goes_and_the_run_lands_on_the_route(self) -> None:
        data = self.ride([0, 3, 6, 9, 109, 112, 115, 118, 121, 124])
        out, stats = fitpace.unjam(data, 5.0, 27.0)
        self.assertEqual(stats['jumps'], 1)
        self.assertAlmostEqual(stats['dropped'], 97.0)
        self.assertAlmostEqual(stats['ratio'], 1.0)
        records, _ = fitroute.read_records(out)
        self.assertEqual(
            [r['distance'] for r in records], [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
        )

    def test_the_recorded_distance_stands_before_the_first_jump(self) -> None:
        data = self.ride([0, 3, 6, 9, 109, 112, 115])
        out, stats = fitpace.unjam(data, 5.0, 100.0)
        self.assertEqual(stats['split'], 3)
        records, _ = fitroute.read_records(out)
        self.assertEqual([r['distance'] for r in records][:4], [0, 3, 6, 9])
        self.assertAlmostEqual(records[-1]['distance'], 100.0)

    def test_the_speed_fields_follow_the_new_distance(self) -> None:
        data = self.ride([0, 3, 6, 9, 109, 112, 115])
        out, _ = fitpace.unjam(data, 5.0, 100.0)
        messages, _ = fitpace.read_messages(out)
        speeds = [
            fields[73][0] / 1000.0
            for number, fields in messages
            if number == fitshift.RECORD
        ]
        self.assertEqual(speeds[:4], [3.0, 3.0, 3.0, 3.0])
        self.assertGreater(speeds[-1], 3.0)  # the rest was stretched

    def test_the_session_total_follows_too(self) -> None:
        data = self.ride([0, 3, 6, 9, 109, 112, 115])
        out, _ = fitpace.unjam(data, 5.0, 100.0)
        messages, _ = fitpace.read_messages(out)
        totals = [
            fields[9][0] / 100.0
            for number, fields in messages
            if number in (SESSION, LAP)
        ]
        self.assertEqual(totals, [100.0, 100.0])

    def test_a_run_with_no_distance_is_nothing_to_rebuild(self) -> None:
        records = [(START + index, [(3, 'uint8', 140)]) for index in range(5)]
        with self.assertRaisesRegex(ValueError, 'no records with a timestamp'):
            fitpace.unjam(activity(records), 5.0, 27.0)

    def test_a_lap_outside_the_records_keeps_its_totals(self) -> None:
        # A watch can leave a lap behind that no record falls inside; there
        # is nothing to total it from, so it is left as written.
        data = activity(
            [
                (START + i, [(5, 'uint32', d * 100), (3, 'uint8', 140)])
                for i, d in enumerate([0, 3, 6, 9, 109, 112])
            ],
            extra=[
                (
                    LAP,
                    [
                        (253, 'uint32', START + 7200),
                        (2, 'uint32', START + 3600),
                        (7, 'uint32', 1000),
                        (9, 'uint32', 4242),
                    ],
                )
            ],
        )
        out, _ = fitpace.unjam(data, 5.0, 27.0)
        messages, _ = fitpace.read_messages(out)
        totals = [fields[9][0] for number, fields in messages if number == LAP]
        self.assertEqual(totals[-1], 4242)

    def test_a_run_with_no_jump_is_not_this_tool_s_job(self) -> None:
        with self.assertRaisesRegex(ValueError, 'faster than'):
            fitpace.unjam(self.ride([0, 3, 6, 9]), 5.0, 27.0)

    def test_a_route_shorter_than_the_good_part_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, 'only'):
            fitpace.unjam(self.ride([0, 3, 6, 9, 109]), 5.0, 5.0)

    def test_the_file_crc_is_recomputed(self) -> None:
        out, _ = fitpace.unjam(self.ride([0, 3, 6, 9, 109, 112]), 5.0, 27.0)
        end = fitshift.walk(out, lambda *args: None)
        self.assertEqual(struct.unpack('<H', out[end:])[0], fitshift.crc16(out[:end]))


class Rescale(unittest.TestCase):
    """The whole distance stream scaled onto the route's length."""

    def ride(self, distances: Sequence[float], speed: float = 3.0) -> bytes:
        records = [
            (
                START + index,
                [
                    (5, 'uint32', round(distance * 100)),
                    (73, 'uint32', round(speed * 1000)),
                    (6, 'uint16', round(speed * 1000)),
                ],
            )
            for index, distance in enumerate(distances)
        ]
        return activity(records)

    def test_the_run_lands_on_the_route(self) -> None:
        out, stats = fitpace.rescale(self.ride([0, 3, 6, 9, 12]), 6.0)
        self.assertAlmostEqual(stats['ratio'], 0.5)
        records, _ = fitroute.read_records(out)
        self.assertEqual([r['distance'] for r in records], [0, 1.5, 3, 4.5, 6])

    def test_every_record_moves_by_the_same_ratio(self) -> None:
        out, _ = fitpace.rescale(self.ride([0, 10, 20, 100]), 50.0)
        records, _ = fitroute.read_records(out)
        self.assertEqual([r['distance'] for r in records], [0, 5, 10, 50])

    def test_speed_scales_with_distance(self) -> None:
        out, _ = fitpace.rescale(self.ride([0, 3, 6, 9, 12], speed=4.0), 6.0)
        messages, _ = fitpace.read_messages(out)
        speeds = [
            fields[73][0]
            for number, fields in messages
            if number == fitshift.RECORD and 73 in fields
        ]
        self.assertEqual(speeds, [2000] * 5)

    def test_the_plain_speed_field_scales_too(self) -> None:
        out, _ = fitpace.rescale(self.ride([0, 3, 6, 9, 12], speed=4.0), 6.0)
        messages, _ = fitpace.read_messages(out)
        speeds = [
            fields[6][0]
            for number, fields in messages
            if number == fitshift.RECORD and 6 in fields
        ]
        self.assertEqual(speeds, [2000] * 5)

    def test_the_totals_follow_the_records(self) -> None:
        out, _ = fitpace.rescale(self.ride([0, 3, 6, 9, 12]), 6.0)
        messages, _ = fitpace.read_messages(out)
        for number, fields in messages:
            if number in (fitpace.SESSION, fitpace.LAP):
                self.assertEqual(fields[9][0], 600)

    def test_timestamps_are_left_alone(self) -> None:
        data = self.ride([0, 3, 6, 9, 12])
        out, _ = fitpace.rescale(data, 6.0)
        before, _ = fitroute.read_records(data)
        after, _ = fitroute.read_records(out)
        self.assertEqual([r['stamp'] for r in before], [r['stamp'] for r in after])

    def test_a_run_with_no_distance_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            fitpace.rescale(self.ride([0, 0, 0]), 6.0)

    def test_a_run_with_no_records_is_refused(self) -> None:
        records = [(START + i, [(3, 'uint8', 140)]) for i in range(3)]
        with self.assertRaises(ValueError):
            fitpace.rescale(activity(records), 6.0)


class Grade(unittest.TestCase):
    """The recorded pace as effort, slowed by the route's climbs.

    The route runs 1000 m north: flat for 400 m, a 10% climb of 200 m, then
    flat again 20 m up. The run records a steady 3 m/s for 300 s, the
    accelerometer reading of a runner who eased off on the hill.
    """

    def setUp(self) -> None:
        self.points = north(21, 50)
        self.elevations = [0.0] * 8 + [0.0, 5.0, 10.0, 15.0, 20.0] + [20.0] * 8
        self.flat = [0.0] * 21

    def session(self, legs: Sequence[tuple[int, float]], speed: float = 3.0) -> bytes:
        """A FIT whose laps each cover their (seconds, metres) in turn."""
        messages = [(FILE_ID, [(0, 'uint8', 4), (4, 'uint32', START)])]
        laps = []
        stamp, travelled = START, 0.0
        for seconds, metres in legs:
            start = stamp
            for step in range(seconds):
                along = travelled + metres * step / seconds
                messages.append(
                    (
                        fitshift.RECORD,
                        [
                            (253, 'uint32', stamp),
                            (5, 'uint32', round(along * 100)),
                            (73, 'uint32', round(speed * 1000)),
                        ],
                    )
                )
                stamp += 1
            travelled += metres
            laps.append(
                (
                    LAP,
                    [
                        (253, 'uint32', stamp - 1),
                        (2, 'uint32', start),
                        (7, 'uint32', seconds * 1000),
                        (8, 'uint32', seconds * 1000),
                        (9, 'uint32', 0),
                        (13, 'uint16', 0),
                    ],
                )
            )
        return encode(messages + laps)

    def distances(self, data: bytes) -> list[float]:
        records, _ = fitroute.read_records(data)
        return [r['distance'] for r in records]

    def seconds_between(
        self, distances: Sequence[float], low: float, high: float
    ) -> int:
        return sum(low <= d < high for d in distances)

    def test_the_run_lands_on_the_route(self) -> None:
        out, stats = fitpace.grade(
            self.session([(300, 900)]), self.points, self.elevations
        )
        self.assertAlmostEqual(self.distances(out)[-1], 1000.0, places=1)
        self.assertAlmostEqual(stats['length'], 1000.0, places=1)

    def test_a_climb_is_run_slower_than_the_flat(self) -> None:
        out, _ = fitpace.grade(self.session([(300, 900)]), self.points, self.elevations)
        distances = self.distances(out)
        hill = self.seconds_between(distances, 450, 550)
        flat = self.seconds_between(distances, 100, 200)
        self.assertGreater(hill, flat * 1.3)

    def test_on_a_flat_route_it_is_the_rescale(self) -> None:
        data = self.session([(300, 900)])
        graded, _ = fitpace.grade(data, self.points, self.flat)
        scaled, _ = fitpace.rescale(data, fitroute.measure(self.points)[-1])
        for one, other in zip(
            self.distances(graded), self.distances(scaled), strict=True
        ):
            self.assertAlmostEqual(one, other, delta=0.05)

    def test_distance_never_goes_backwards(self) -> None:
        out, _ = fitpace.grade(self.session([(300, 900)]), self.points, self.elevations)
        distances = self.distances(out)
        self.assertEqual(distances, sorted(distances))

    def test_the_speed_fields_slow_on_the_climb(self) -> None:
        out, _ = fitpace.grade(self.session([(300, 900)]), self.points, self.elevations)
        records, _ = fitroute.read_records(out)
        messages, _ = fitpace.read_messages(out)
        speeds = [f[73][0] for n, f in messages if n == fitshift.RECORD]
        on_hill = [
            s
            for s, r in zip(speeds, records, strict=True)
            if 450 <= r['distance'] < 550
        ]
        on_flat = [
            s
            for s, r in zip(speeds, records, strict=True)
            if 100 <= r['distance'] < 200
        ]
        self.assertLess(max(on_hill), min(on_flat))

    def test_a_split_pins_the_lap_press_to_the_route(self) -> None:
        # the watch read the first lap 600 m long, and it was 400 m
        data = self.session([(150, 600), (150, 300)])
        out, stats = fitpace.grade(data, self.points, self.elevations, [400.0])
        self.assertAlmostEqual(self.distances(out)[150], 400.0, places=1)
        routes = [round(lap['route'], 3) for lap in stats['laps']]
        self.assertEqual(routes, [400.0, 600.0])

    def test_the_lap_totals_are_the_spans(self) -> None:
        data = self.session([(150, 600), (150, 300)])
        out, _ = fitpace.grade(data, self.points, self.elevations, [400.0])
        messages, _ = fitpace.read_messages(out)
        totals = [f[9][0] for n, f in messages if n == LAP]
        self.assertEqual(totals, [40000, 60000])

    def test_a_split_needs_a_lap_for_each_side(self) -> None:
        with self.assertRaises(ValueError):
            fitpace.grade(
                self.session([(300, 900)]), self.points, self.elevations, [400.0]
            )

    def test_a_split_past_the_end_of_the_route_is_refused(self) -> None:
        data = self.session([(150, 600), (150, 300)])
        with self.assertRaises(ValueError):
            fitpace.grade(data, self.points, self.elevations, [1200.0])

    def test_a_run_with_no_speed_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            fitpace.grade(
                self.session([(300, 900)], speed=0.0), self.points, self.elevations
            )

    def test_the_parse_still_ends_on_the_file_crc(self) -> None:
        out, _ = fitpace.grade(self.session([(300, 900)]), self.points, self.elevations)
        self.assertEqual(fitshift.walk(out, lambda *_: None), len(out) - 2)


class Cost(unittest.TestCase):
    def test_the_flat_costs_one(self) -> None:
        self.assertEqual(fitpace.cost(0.0), 1.0)

    def test_a_climb_costs_more(self) -> None:
        self.assertGreater(fitpace.cost(0.10), 1.4)

    def test_a_gentle_descent_costs_less(self) -> None:
        self.assertLess(fitpace.cost(-0.05), 1.0)

    def test_a_steep_descent_is_braked(self) -> None:
        self.assertGreater(fitpace.cost(-0.30), fitpace.cost(-0.10))

    def test_past_the_steepest_it_stops_climbing(self) -> None:
        self.assertEqual(fitpace.cost(0.5), fitpace.cost(0.3))


class Pace(unittest.TestCase):
    """The heart-rate model, on a run where speed follows heart rate exactly."""

    def ride(self, count: int = 300) -> bytes:
        records = []
        distance = 0.0
        for index in range(count):
            heart = 120 + index // 10
            speed = 0.02 * heart
            distance += speed
            records.append(
                (
                    START + index,
                    [
                        (5, 'uint32', round(distance * 100)),
                        (73, 'uint32', round(speed * 1000)),
                        (3, 'uint8', heart),
                    ],
                )
            )
        return activity(records)

    def test_the_paced_run_lands_on_the_route(self) -> None:
        out, stats = fitpace.pace(self.ride(), 500.0, 1200.0)
        self.assertGreater(stats['slope'], 0)
        records, _ = fitroute.read_records(out)
        self.assertAlmostEqual(records[-1]['distance'], 1200.0, places=1)

    def test_what_was_kept_is_left_alone(self) -> None:
        data = self.ride()
        out, stats = fitpace.pace(data, 500.0, 1200.0)
        before, _ = fitroute.read_records(data)
        after, _ = fitroute.read_records(out)
        split = stats['split']
        self.assertGreater(split, 0)
        self.assertEqual(
            [r['distance'] for r in before[: split + 1]],
            [r['distance'] for r in after[: split + 1]],
        )

    def test_a_pace_that_ignores_heart_rate_has_no_model(self) -> None:
        records = [
            (
                START + index,
                [
                    (5, 'uint32', index * 300),
                    (73, 'uint32', 3000),
                    (3, 'uint8', 120 + index % 40),
                ],
            )
            for index in range(300)
        ]
        with self.assertRaisesRegex(ValueError, 'heart rate'):
            fitpace.pace(activity(records), 500.0, 1200.0)

    def test_a_run_without_heart_rate_cannot_be_modelled(self) -> None:
        records = [
            (START + index, [(5, 'uint32', index * 300)]) for index in range(300)
        ]
        with self.assertRaisesRegex(ValueError, 'no records with timestamp'):
            fitpace.pace(activity(records), 500.0, 1200.0)

    def test_a_route_shorter_than_the_kept_part_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, 'only'):
            fitpace.pace(self.ride(), 500.0, 400.0)

    def test_a_keep_inside_the_warmup_leaves_too_little_to_learn_from(self) -> None:
        # The first two minutes raise heart rate without raising pace, so
        # they are dropped; a --keep inside them leaves no good part at all.
        with self.assertRaisesRegex(ValueError, 'too little'):
            fitpace.pace(self.ride(), 100.0, 1200.0)

    def test_a_run_that_never_reaches_keep(self) -> None:
        with self.assertRaisesRegex(ValueError, 'never gets past'):
            fitpace.pace(self.ride(), 100000.0, 200000.0)


class Stops(unittest.TestCase):
    """Which pauses invented distance, read off the times and the odometer."""

    def test_a_pause_followed_by_a_jump_is_a_stop(self) -> None:
        times = [0, 1, 2, 90, 91, 92, 93]
        distances = [0.0, 3.0, 6.0, 56.0, 116.0, 176.0, 179.0]
        # The window runs from the last record before the pause to the last
        # record of the jump, so it owns every metre the two invented.
        self.assertEqual(fitstop.stops(times, distances), [(2, 5)])

    def test_a_pause_the_watch_measured_across_is_left_alone(self) -> None:
        # No jump follows, so the watch kept measuring and the distance is real.
        times = [0, 1, 2, 90, 91, 92]
        distances = [0.0, 3.0, 6.0, 270.0, 273.0, 276.0]
        self.assertEqual(fitstop.stops(times, distances), [])

    def test_a_jump_without_a_pause_before_it_is_not_this_tool_s(self) -> None:
        times = list(range(6))
        distances = [0.0, 3.0, 6.0, 106.0, 109.0, 112.0]
        self.assertEqual(fitstop.stops(times, distances), [])

    def test_a_silence_shorter_than_the_gap_is_a_dropped_sample(self) -> None:
        times = [0, 1, 2, 5, 6, 7]
        distances = [0.0, 3.0, 6.0, 56.0, 116.0, 119.0]
        self.assertEqual(fitstop.stops(times, distances, gap=10), [])

    def test_a_pause_that_gained_nothing_is_left_alone(self) -> None:
        times = [0, 1, 2, 90, 91, 92]
        distances = [0.0, 3.0, 6.0, 6.0, 9.0, 12.0]
        self.assertEqual(fitstop.stops(times, distances), [])

    def test_every_stop_is_found(self) -> None:
        times = [0, 1, 90, 91, 92, 200, 201, 202]
        distances = [0.0, 3.0, 50.0, 110.0, 113.0, 160.0, 220.0, 223.0]
        self.assertEqual(fitstop.stops(times, distances), [(1, 3), (4, 6)])


class Destop(unittest.TestCase):
    """The distance stream with the standing-still metres taken out."""

    def run_with_a_stop(self, speed: float = 3.0) -> bytes:
        """Three seconds of running, an 80 s stop, a jump, three more."""
        times = [0, 1, 2, 82, 83, 84, 85, 86]
        distances = [0.0, 3.0, 6.0, 56.0, 116.0, 176.0, 179.0, 182.0]
        records = [
            (
                START + stamp,
                [
                    (5, 'uint32', round(distance * 100)),
                    (73, 'uint32', round(speed * 1000)),
                    (3, 'uint8', 80 if 2 < index < 6 else 140),
                ],
            )
            for index, (stamp, distance) in enumerate(
                zip(times, distances, strict=True)
            )
        ]
        return activity(records)

    def distances(self, data: bytes) -> list[float]:
        records, _ = fitroute.read_records(data)
        return [r['distance'] for r in records]

    def speeds(self, data: bytes) -> list[float]:
        messages, _ = fitpace.read_messages(data)
        return [
            fields[73][0] / 1000.0
            for number, fields in messages
            if number == fitshift.RECORD
        ]

    def test_the_invented_distance_goes_and_the_rest_stands(self) -> None:
        out, stats = fitstop.destop(self.run_with_a_stop())
        self.assertAlmostEqual(stats['dropped'], 170.0)
        self.assertAlmostEqual(stats['ratio'], 1.0)
        # 6 m at the stop, then the two real strides after the jump.
        self.assertEqual(self.distances(out), [0, 3, 6, 6, 6, 6, 9, 12])

    def test_the_speed_of_a_record_that_stood_still_is_zero(self) -> None:
        out, _ = fitstop.destop(self.run_with_a_stop())
        self.assertEqual(self.speeds(out), [3.0, 3.0, 3.0, 0.0, 0.0, 0.0, 3.0, 3.0])

    def test_the_heart_rate_that_proves_it_is_reported(self) -> None:
        _, stats = fitstop.destop(self.run_with_a_stop())
        self.assertEqual(len(stats['windows']), 1)
        window = stats['windows'][0]
        self.assertEqual(window['heart'], 80)
        self.assertEqual(window['at'], 2)
        self.assertEqual(window['span'], 82)

    def test_what_is_left_over_is_scaled_onto_the_route(self) -> None:
        out, stats = fitstop.destop(self.run_with_a_stop(), 24.0)
        self.assertAlmostEqual(stats['kept'], 12.0)
        self.assertAlmostEqual(stats['ratio'], 2.0)
        self.assertEqual(self.distances(out), [0, 6, 12, 12, 12, 12, 18, 24])

    def test_the_scale_reaches_the_speed_fields_too(self) -> None:
        out, _ = fitstop.destop(self.run_with_a_stop(), 24.0)
        self.assertEqual(self.speeds(out), [6.0, 6.0, 6.0, 0.0, 0.0, 0.0, 6.0, 6.0])

    def test_the_last_record_lands_exactly_on_the_route(self) -> None:
        out, _ = fitstop.destop(self.run_with_a_stop(), 1234.5)
        self.assertAlmostEqual(self.distances(out)[-1], 1234.5, places=1)

    def test_the_lap_and_session_totals_follow(self) -> None:
        out, _ = fitstop.destop(self.run_with_a_stop(), 24.0)
        messages, _ = fitpace.read_messages(out)
        for number, fields in messages:
            if number in (fitpace.SESSION, fitpace.LAP):
                self.assertAlmostEqual(fields[9][0] / 100.0, 24.0, places=1)

    def test_the_parse_still_ends_on_the_file_crc(self) -> None:
        out, _ = fitstop.destop(self.run_with_a_stop(), 24.0)
        end = fitshift.walk(out, lambda *args: None, lambda *args: None)
        self.assertEqual(end, len(out) - 2)
        self.assertEqual(struct.unpack('<H', out[end:])[0], fitshift.crc16(out[:end]))

    def test_a_run_without_a_stop_is_refused(self) -> None:
        records = [
            (START + index, [(5, 'uint32', index * 300), (73, 'uint32', 3000)])
            for index in range(10)
        ]
        with self.assertRaisesRegex(ValueError, 'no pause'):
            fitstop.destop(activity(records))

    def test_a_run_without_distance_is_refused(self) -> None:
        records = [(START + index, [(3, 'uint8', 140)]) for index in range(10)]
        with self.assertRaisesRegex(ValueError, 'no records with a timestamp'):
            fitstop.destop(activity(records))

    def test_a_run_that_was_all_stop_is_refused(self) -> None:
        # Nothing is left to scale onto the route, so the file is not written.
        times = [0, 82, 83, 84]
        distances = [0.0, 56.0, 116.0, 176.0]
        records = [
            (START + stamp, [(5, 'uint32', round(distance * 100))])
            for stamp, distance in zip(times, distances, strict=True)
        ]
        with self.assertRaisesRegex(ValueError, 'every metre'):
            fitstop.destop(activity(records), 1000.0)

    def test_a_route_with_no_length_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, 'no length'):
            fitstop.destop(self.run_with_a_stop(), 0.0)

    def test_a_jump_slower_than_the_limit_is_a_stride(self) -> None:
        with self.assertRaisesRegex(ValueError, 'no pause'):
            fitstop.destop(self.run_with_a_stop(), limit=100.0)

    def test_a_run_without_heart_rate_still_reports_its_stop(self) -> None:
        times = [0, 1, 2, 82, 83, 84]
        distances = [0.0, 3.0, 6.0, 56.0, 116.0, 119.0]
        records = [
            (START + stamp, [(5, 'uint32', round(distance * 100))])
            for stamp, distance in zip(times, distances, strict=True)
        ]
        _, stats = fitstop.destop(activity(records))
        self.assertIsNone(stats['windows'][0]['heart'])
        self.assertAlmostEqual(stats['dropped'], 110.0)


class Hills(unittest.TestCase):
    """A hill session rescaled onto a route that climbs the hill once.

    The route runs 800 m north and comes back, so its climb is the 400 m to
    the turnaround and the descent is the 400 m home: every span the splice
    produces is 400 m, and a lap that recorded anything else has to land on
    400 m exactly.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        line = north(9, 100)
        self.points = line + line[7::-1]
        # flat to the foot, then 10 m a point to the turnaround, then back
        self.elevations = (
            [0.0] * 5 + [10.0, 20.0, 30.0, 40.0, 30.0, 20.0, 10.0] + [0.0] * 5
        )
        self.cumulative = fitroute.measure(self.points)

    def path(self, name: str = 'hill.gpx') -> Path:
        return Path(self.dir.name) / name

    def hill(self, low: float = 400.0, high: float = 800.0) -> tuple[int, int, int]:
        return fithills.find_hill(
            self.points, self.cumulative, self.elevations, low, high
        )

    def session(
        self, legs: Sequence[tuple[int, float]], speed: float = 3.0, heart: int = 140
    ) -> bytes:
        """A FIT whose laps each cover their (seconds, metres) in turn."""
        messages = [(FILE_ID, [(0, 'uint8', 4), (4, 'uint32', START)])]
        laps = []
        stamp, travelled = START, 0.0
        for seconds, metres in legs:
            start = stamp
            for step in range(seconds):
                along = travelled + metres * step / (seconds - 1)
                messages.append(
                    (
                        fitshift.RECORD,
                        [
                            (253, 'uint32', stamp),
                            (5, 'uint32', round(along * 100)),
                            (73, 'uint32', round(speed * 1000)),
                            (6, 'uint16', round(speed * 1000)),
                            (3, 'uint8', heart),
                        ],
                    )
                )
                stamp += 1
            travelled += metres
            laps.append(
                (
                    LAP,
                    [
                        (253, 'uint32', stamp - 1),
                        (2, 'uint32', start),
                        (7, 'uint32', seconds * 1000),
                        (8, 'uint32', seconds * 1000),
                        (9, 'uint32', 0),
                        (13, 'uint16', 0),
                        (14, 'uint16', 0),
                    ],
                )
            )
        messages += laps
        messages.append(
            (
                SESSION,
                [
                    (253, 'uint32', stamp - 1),
                    (2, 'uint32', START),
                    (7, 'uint32', (stamp - 1 - START) * 1000),
                    (8, 'uint32', (stamp - 1 - START) * 1000),
                    (9, 'uint32', 0),
                    (14, 'uint16', 0),
                    (15, 'uint16', 0),
                ],
            )
        )
        messages.append((ACTIVITY, [(253, 'uint32', stamp - 1), (5, 'uint32', stamp)]))
        return encode(messages)

    def legs(
        self,
        count: int = 3,
        warm: float = 500.0,
        up: float = 300.0,
        down: float = 500.0,
        cool: float = 200.0,
    ) -> list[tuple[int, float]]:
        """A session of count repeats, every lap recording the wrong distance."""
        return [(60, warm)] + [(30, up), (30, down)] * count + [(60, cool)]

    def rescaled(
        self, count: int = 3, **kwargs: float
    ) -> tuple[tuple[bytes, dict[str, Any]], list[tuple[float, float]]]:
        foot, apex, back = self.hill()
        along = fitroute.measure(fithills.splice(self.points, foot, back, count))
        spans = fithills.spans(along, foot, apex, back, count)
        return fithills.relap(self.session(self.legs(count, **kwargs)), spans), spans

    # --- finding the hill on the route ---

    def test_nearest_snaps_to_the_closer_point(self) -> None:
        self.assertEqual(fithills.nearest(self.cumulative, 390.0), 4)
        self.assertEqual(fithills.nearest(self.cumulative, 460.0), 5)

    def test_nearest_clamps_to_the_ends(self) -> None:
        self.assertEqual(fithills.nearest(self.cumulative, -100.0), 0)
        self.assertEqual(fithills.nearest(self.cumulative, 99e3), 16)

    def test_the_hill_is_its_foot_its_top_and_the_way_back(self) -> None:
        self.assertEqual(self.hill(), (4, 8, 12))

    def test_the_top_is_the_highest_point_not_the_one_asked_for(self) -> None:
        # 700 m along is a point short of the turnaround; the summit still wins.
        self.assertEqual(self.hill(high=700.0), (4, 8, 12))

    def test_a_route_that_never_comes_back_is_refused(self) -> None:
        # a straight line climbs away from the foot and never returns to it
        line = north(12, 100)
        rise = [float(index) for index in range(12)]
        with self.assertRaisesRegex(ValueError, 'never comes back'):
            fithills.find_hill(line, fitroute.measure(line), rise, 400.0, 800.0)

    def test_a_climb_that_ends_before_it_starts_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, 'must start before'):
            self.hill(low=800.0, high=400.0)

    def test_a_flat_out_and_back_has_no_top(self) -> None:
        with self.assertRaisesRegex(ValueError, 'no top'):
            fithills.find_hill(self.points, self.cumulative, [0.0] * 17, 400.0, 800.0)

    # --- repeating the route ---

    def test_splice_repeats_the_block(self) -> None:
        self.assertEqual(
            fithills.splice([0, 1, 2, 3, 4], 1, 3, 3), [0, 1, 2, 1, 2, 1, 2, 3, 4]
        )

    def test_splice_once_is_the_route_unchanged(self) -> None:
        self.assertEqual(fithills.splice(self.points, 4, 12, 1), self.points)

    def test_seven_repeats_add_six_blocks(self) -> None:
        foot, _, back = self.hill()
        repeated = fithills.splice(self.points, foot, back, 7)
        self.assertEqual(len(repeated), len(self.points) + 6 * (back - foot))
        self.assertAlmostEqual(fitroute.measure(repeated)[-1], 1600.0 + 6 * 800.0)

    def test_the_route_keeps_its_start_and_finish(self) -> None:
        repeated = fithills.splice(self.points, 4, 12, 4)
        self.assertEqual(repeated[0], self.points[0])
        self.assertEqual(repeated[-1], self.points[-1])

    # --- the spans each lap covers ---

    def test_there_is_a_span_for_every_lap(self) -> None:
        _, spans = self.rescaled(count=7)
        self.assertEqual(len(spans), 2 * 7 + 2)

    def test_the_spans_tile_the_route_end_to_end(self) -> None:
        _, spans = self.rescaled(count=3)
        self.assertAlmostEqual(spans[0][0], 0.0)
        self.assertAlmostEqual(spans[-1][1], 3200.0)
        for (_, high), (low, _) in pairwise(spans):
            self.assertAlmostEqual(high, low)

    def test_every_leg_of_this_route_is_four_hundred_metres(self) -> None:
        _, spans = self.rescaled(count=3)
        self.assertEqual([round(high - low) for low, high in spans], [400] * 8)

    # --- rescaling the laps ---

    def test_each_lap_lands_on_its_span(self) -> None:
        (out, _), spans = self.rescaled(count=3)
        messages, _ = fitpace.read_messages(out)
        totals = [
            fields[9][0] / 100.0 for number, fields in messages if number == fitpace.LAP
        ]
        self.assertEqual(totals, [round(high - low, 2) for low, high in spans])

    def test_the_lap_totals_tile_the_session_total(self) -> None:
        (out, _), _ = self.rescaled(count=3)
        messages, _ = fitpace.read_messages(out)
        laps = [f[9][0] for n, f in messages if n == fitpace.LAP]
        session = next(f[9][0] for n, f in messages if n == fitpace.SESSION)
        self.assertEqual(sum(laps), session)

    def test_the_run_ends_on_the_route(self) -> None:
        (out, stats), _ = self.rescaled(count=3)
        records, _ = fitroute.read_records(out)
        self.assertAlmostEqual(records[-1]['distance'], 3200.0, places=1)
        self.assertAlmostEqual(stats['length'], 3200.0)

    def test_distance_never_goes_backwards(self) -> None:
        (out, _), _ = self.rescaled(count=3)
        records, _ = fitroute.read_records(out)
        along = [r['distance'] for r in records]
        self.assertEqual(along, sorted(along))

    def test_a_lap_that_measured_long_is_brought_in(self) -> None:
        # the descents recorded 500 m of the 400 m they covered
        (_, stats), _ = self.rescaled(count=3)
        for lap in [stats['laps'][index] for index in (2, 4, 6)]:
            self.assertAlmostEqual(lap['was'], 500.0, places=1)
            self.assertAlmostEqual(lap['route'], 400.0, places=1)

    def test_a_lap_that_measured_short_is_let_out(self) -> None:
        (_, stats), _ = self.rescaled(count=3)
        for index in (1, 3, 5):
            self.assertAlmostEqual(stats['laps'][index]['was'], 300.0, places=1)
            self.assertAlmostEqual(stats['laps'][index]['route'], 400.0, places=1)

    def test_speed_follows_its_own_lap(self) -> None:
        # the climbs shrink by 400/300, the descents by 400/500
        (out, _), _ = self.rescaled(count=1)
        messages, _ = fitpace.read_messages(out)
        speeds = [
            fields[73][0]
            for number, fields in messages
            if number == fitshift.RECORD and 73 in fields
        ]
        self.assertEqual(speeds[60], round(3000 * 400 / 300))
        self.assertEqual(speeds[90], round(3000 * 400 / 500))

    def test_heart_rate_is_left_alone(self) -> None:
        data = self.session(self.legs(3))
        foot, apex, back = self.hill()
        along = fitroute.measure(fithills.splice(self.points, foot, back, 3))
        out, _ = fithills.relap(data, fithills.spans(along, foot, apex, back, 3))
        for source in (data, out):
            messages, _ = fitpace.read_messages(source)
            hearts = [
                fields[3][0]
                for number, fields in messages
                if number == fitshift.RECORD and 3 in fields
            ]
            self.assertEqual(set(hearts), {140})

    def test_timestamps_are_left_alone(self) -> None:
        data = self.session(self.legs(3))
        foot, apex, back = self.hill()
        along = fitroute.measure(fithills.splice(self.points, foot, back, 3))
        out, _ = fithills.relap(data, fithills.spans(along, foot, apex, back, 3))
        before, _ = fitroute.read_records(data)
        after, _ = fitroute.read_records(out)
        self.assertEqual([r['stamp'] for r in before], [r['stamp'] for r in after])

    def test_the_parse_still_ends_on_the_file_crc(self) -> None:
        (out, _), _ = self.rescaled(count=3)
        self.assertEqual(fitshift.walk(out, lambda *a: None), len(out) - 2)

    # --- the refusals ---

    def test_too_few_laps_is_refused(self) -> None:
        foot, apex, back = self.hill()
        along = fitroute.measure(fithills.splice(self.points, foot, back, 7))
        spans = fithills.spans(along, foot, apex, back, 7)
        with self.assertRaisesRegex(ValueError, '7 repeats need 16 laps'):
            fithills.relap(self.session(self.legs(3)), spans)

    def test_a_file_with_no_records_is_refused(self) -> None:
        foot, apex, back = self.hill()
        along = fitroute.measure(fithills.splice(self.points, foot, back, 3))
        spans = fithills.spans(along, foot, apex, back, 3)
        with self.assertRaisesRegex(ValueError, 'no records'):
            fithills.relap(encode([(FILE_ID, [(0, 'uint8', 4)])]), spans)


class Put(unittest.TestCase):
    def test_a_field_the_message_lacks_is_left_alone(self) -> None:
        out = bytearray(b'\x00\x00')
        fitpace.put(out, {}, 9, 1234)
        self.assertEqual(bytes(out), b'\x00\x00')

    def test_a_value_is_clamped_below_the_invalid_sentinel(self) -> None:
        out = bytearray(b'\x00\x00')
        fitpace.put(out, {9: (0, 0, 2, '<')}, 9, 70000)
        self.assertEqual(struct.unpack('<H', out)[0], 0xFFFE)

    def test_a_negative_value_is_clamped_to_zero(self) -> None:
        out = bytearray(b'\xff\xff')
        fitpace.put(out, {9: (0, 0, 2, '<')}, 9, -5)
        self.assertEqual(struct.unpack('<H', out)[0], 0)


class Smooth(unittest.TestCase):
    def test_it_averages_each_point_with_its_window(self) -> None:
        self.assertEqual(fitpace.smooth([1, 1, 1, 1], 1), [1, 1, 1, 1])
        self.assertEqual(fitpace.smooth([0, 0, 3, 0, 0], 1), [0, 1, 1, 1, 0])

    def test_minutes_renders_a_pace(self) -> None:
        self.assertEqual(fitpace.minutes(299), '4:59/km')
        self.assertEqual(fitpace.minutes(60), '1:00/km')


if __name__ == '__main__':
    unittest.main()
