"""Tests for the library half of each tool.

Every test builds the FIT bytes it needs with encode(), so none of them
depends on a real activity or on fit-tool.
"""

import argparse
import os
import struct
import tempfile
import unittest

import fitpace
import fitroute
import fitshift

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


def encode(messages):
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
    header = struct.pack('<BBHI4s', 14, 0x20, 2158, len(body), b'.FIT')
    header += struct.pack('<H', fitshift.crc16(header[:12]))
    data = header + bytes(body)
    return data + struct.pack('<H', fitshift.crc16(data))


def semicircles(degrees):
    return round(degrees * fitroute.SEMICIRCLES)


def activity(records, extra=()):
    """A file_id, these records, and a lap, session and activity around them."""
    first, last = records[0][0], records[-1][0]
    messages = [(FILE_ID, [(0, 'uint8', 4), (4, 'uint32', first)])]
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


def route(path, points, elevations=None):
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
    with open(path, 'w') as out:
        out.write('\n'.join(body))
    return path


def north(count, metres, lat=50.0, lon=30.0):
    """count points going north from (lat, lon), metres apart."""
    step = metres / (fitroute.EARTH_RADIUS * 3.141592653589793 / 180.0)
    return [(lat + step * index, lon) for index in range(count)]


class Crc(unittest.TestCase):
    def test_zero_bytes_leave_the_crc_zero(self):
        self.assertEqual(fitshift.crc16(b''), 0)
        self.assertEqual(fitshift.crc16(b'\x00' * 8), 0)

    def test_a_single_bit_changes_it(self):
        self.assertNotEqual(fitshift.crc16(b'fitmend'), fitshift.crc16(b'fitmene'))

    def test_an_encoded_file_carries_its_own_crc(self):
        data = activity([(START, [(5, 'uint32', 0)])])
        end = len(data) - 2
        self.assertEqual(struct.unpack('<H', data[end:])[0], fitshift.crc16(data[:end]))


class Walk(unittest.TestCase):
    def setUp(self):
        self.data = activity([(START + i, [(5, 'uint32', i * 100)]) for i in range(3)])

    def test_it_ends_on_the_file_crc(self):
        end = fitshift.walk(self.data, lambda *args: None)
        self.assertEqual(end, len(self.data) - 2)

    def test_it_visits_every_field_of_every_message(self):
        seen = []
        fitshift.walk(
            self.data,
            lambda global_num, number, *rest: seen.append((global_num, number)),
        )
        self.assertEqual(seen.count((fitshift.RECORD, 253)), 3)
        self.assertEqual(seen.count((fitshift.RECORD, 5)), 3)
        self.assertIn((FILE_ID, 4), seen)

    def test_a_truncated_file_is_a_misread_not_a_guess(self):
        broken = bytearray(self.data)
        struct.pack_into('<I', broken, 4, len(self.data) - 20)
        with self.assertRaisesRegex(ValueError, 'misread records'):
            fitshift.walk(bytes(broken), lambda *args: None)

    def test_it_needs_the_signature(self):
        with self.assertRaisesRegex(ValueError, 'signature'):
            fitshift.walk(b'not a fit file at all', lambda *args: None)


class IsDateTime(unittest.TestCase):
    def test_field_253_is_a_date_in_any_message(self):
        self.assertTrue(fitshift.is_date_time(PRIVATE, 253, 0x86, 4, False))

    def test_an_allowlisted_field_is_a_date(self):
        self.assertTrue(fitshift.is_date_time(FILE_ID, 4, 0x86, 4, False))
        self.assertTrue(fitshift.is_date_time(ACTIVITY, 5, 0x86, 4, False))

    def test_another_uint32_is_not(self):
        # record.distance decodes as a plausible date and must not move.
        self.assertFalse(fitshift.is_date_time(fitshift.RECORD, 5, 0x86, 4, False))

    def test_a_developer_field_is_not(self):
        self.assertFalse(fitshift.is_date_time(fitshift.RECORD, 253, 0x86, 4, True))


class Survey(unittest.TestCase):
    def test_it_reads_the_span_and_the_local_offset(self):
        data = activity([(START + i, [(5, 'uint32', 0)]) for i in range(5)])
        first, last, window, offset = fitshift.survey(data)
        self.assertEqual((first, last), (START, START + 4))
        self.assertEqual(window, (START, START + 4))
        self.assertEqual(offset, ZONE)

    def test_the_span_comes_from_the_records_alone(self):
        # A private message reusing field 253 for a counter is not the end.
        data = activity(
            [(START + i, [(5, 'uint32', 0)]) for i in range(3)],
            extra=[(PRIVATE, [(253, 'uint32', START + 99999)])],
        )
        _, last, _, _ = fitshift.survey(data)
        self.assertEqual(last, START + 2)

    def test_the_invalid_sentinel_is_not_a_timestamp(self):
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

    def test_a_private_counter_does_not_stretch_the_window(self):
        data = activity(
            [(START + i, [(5, 'uint32', 0)]) for i in range(3)],
            extra=[(PRIVATE, [(253, 'uint32', 7)])],
        )
        _, _, window, _ = fitshift.survey(data)
        self.assertEqual(window, (START, START + 2))


class Shift(unittest.TestCase):
    def setUp(self):
        self.data = activity(
            [(START + i, [(5, 'uint32', 0)]) for i in range(3)],
            extra=[
                (PRIVATE, [(253, 'uint32', 7)]),
                (PRIVATE, [(253, 'uint32', fitshift.INVALID)]),
            ],
        )
        _, _, self.window, _ = fitshift.survey(self.data)

    def stamps(self, data):
        found = []

        def visit(global_num, number, base_type, endian, pos, size, developer):
            if fitshift.is_date_time(global_num, number, base_type, size, developer):
                found.append(
                    (global_num, number, struct.unpack_from(endian + 'I', data, pos)[0])
                )

        fitshift.walk(data, visit)
        return found

    def test_every_timestamp_in_the_window_moves_together(self):
        low, high = self.window
        out, count = fitshift.shift(self.data, 86400, (low - 86400, high + 86400))
        # file_id, three records, the lap and session start and timestamp,
        # and the activity's timestamp and local_timestamp.
        self.assertEqual(count, 10)
        moved = {(g, n): v for g, n, v in self.stamps(out)}
        self.assertEqual(moved[(fitshift.RECORD, 253)], START + 2 + 86400)
        self.assertEqual(moved[(ACTIVITY, 5)], START + 2 + ZONE + 86400)

    def test_a_value_outside_the_window_stays(self):
        low, high = self.window
        out, _ = fitshift.shift(self.data, 86400, (low - 86400, high + 86400))
        values = [v for g, n, v in self.stamps(out) if g == PRIVATE]
        self.assertIn(7, values)
        self.assertIn(fitshift.INVALID, values)

    def test_the_file_crc_is_recomputed(self):
        low, high = self.window
        out, _ = fitshift.shift(self.data, 86400, (low - 86400, high + 86400))
        end = fitshift.walk(out, lambda *args: None)
        self.assertEqual(struct.unpack('<H', out[end:])[0], fitshift.crc16(out[:end]))

    def test_the_implied_offset_survives_the_shift(self):
        low, high = self.window
        out, _ = fitshift.shift(self.data, -3600, (low - 86400, high + 86400))
        self.assertEqual(fitshift.survey(out)[3], ZONE)

    def test_zone_rewrites_the_offset_instead(self):
        low, high = self.window
        out, _ = fitshift.shift(self.data, 0, (low - 86400, high + 86400), -7200)
        self.assertEqual(fitshift.survey(out)[3], -7200)


class Parsing(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(fitshift.parse_duration('1d'), 86400)
        self.assertEqual(fitshift.parse_duration('2h30m'), 9000)
        self.assertEqual(fitshift.parse_duration('-45m'), -2700)
        self.assertEqual(fitshift.parse_duration('90'), 90)

    def test_a_duration_needs_a_number(self):
        for text in ('', 'tomorrow', '+'):
            with self.assertRaises(argparse.ArgumentTypeError):
                fitshift.parse_duration(text)

    def test_zones(self):
        self.assertEqual(fitshift.parse_zone('+3'), 10800)
        self.assertEqual(fitshift.parse_zone('-5'), -18000)
        self.assertEqual(fitshift.parse_zone('+05:30'), 19800)
        self.assertEqual(fitshift.parse_zone('0'), 0)

    def test_a_zone_is_not_free_text(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            fitshift.parse_zone('Europe/Kyiv')

    def test_seconds_zone_renders_the_offset(self):
        self.assertEqual(fitshift.seconds_zone(10800), '+3')
        self.assertEqual(fitshift.seconds_zone(19800), '+5:30')
        self.assertEqual(fitshift.seconds_zone(-18000), '-5')


class Geometry(unittest.TestCase):
    def test_a_degree_of_latitude(self):
        metres = fitroute.haversine((50.0, 30.0), (51.0, 30.0))
        self.assertAlmostEqual(metres, 111194.9, delta=1.0)

    def test_measure_runs_along_the_points(self):
        cumulative = fitroute.measure(north(5, 100))
        self.assertEqual(len(cumulative), 5)
        self.assertAlmostEqual(cumulative[-1], 400, delta=0.5)

    def test_locate_interpolates_between_points(self):
        points = north(5, 100)
        cumulative = fitroute.measure(points)
        lat, lon = fitroute.locate(points, cumulative, 150)
        self.assertAlmostEqual(lat, (points[1][0] + points[2][0]) / 2)
        self.assertEqual(lon, 30.0)

    def test_locate_pins_past_the_end(self):
        points = north(5, 100)
        cumulative = fitroute.measure(points)
        self.assertEqual(fitroute.locate(points, cumulative, 10000), points[-1])

    def test_project_finds_how_far_off_and_how_far_along(self):
        points = north(5, 100)
        cumulative = fitroute.measure(points)
        lat, lon = fitroute.locate(points, cumulative, 250)
        # A tenth of a degree of longitude is well off a north-south route.
        off, along = fitroute.project(
            points, cumulative, (lat, lon + 0.001), 0, cumulative[-1]
        )
        self.assertAlmostEqual(along, 250, delta=1.0)
        self.assertAlmostEqual(off, 71.6, delta=1.0)

    def test_project_only_looks_inside_its_window(self):
        # What a route passing the same street twice rests on: a fix 100 m
        # along cannot be matched there from a window 500 m along.
        points = north(9, 100)
        cumulative = fitroute.measure(points)
        fix = fitroute.locate(points, cumulative, 100)
        off, along = fitroute.project(points, cumulative, fix, 500, 800)
        self.assertGreater(along, 390)
        self.assertGreater(off, 290)


class Interpolate(unittest.TestCase):
    def test_between_anchors(self):
        anchors = [(0.0, 0.0), (100.0, 200.0)]
        self.assertAlmostEqual(fitroute.interpolate(anchors, 50), 100.0)

    def test_before_the_first_anchor_the_offset_carries_back(self):
        anchors = [(100.0, 150.0), (200.0, 250.0)]
        self.assertAlmostEqual(fitroute.interpolate(anchors, 0), 50.0)

    def test_past_the_last_anchor_the_offset_carries_on(self):
        anchors = [(0.0, 0.0), (100.0, 150.0)]
        self.assertAlmostEqual(fitroute.interpolate(anchors, 200), 250.0)


class Retime(unittest.TestCase):
    def test_a_jump_takes_the_median_pace_of_the_rest(self):
        times = list(range(10))
        distances = [0, 3, 6, 9, 109, 112, 115, 118, 121, 124]
        rebuilt, jumps, dropped = fitroute.retime(times, distances, 5.0)
        self.assertEqual(jumps, [4])
        self.assertAlmostEqual(dropped, 97.0)
        self.assertEqual(rebuilt, [0, 3, 6, 9, 12, 15, 18, 21, 24, 27])

    def test_a_plausible_pace_is_left_exactly_as_recorded(self):
        times = list(range(5))
        distances = [0.0, 2.5, 5.0, 7.5, 10.0]
        rebuilt, jumps, dropped = fitroute.retime(times, distances, 5.0)
        self.assertEqual(jumps, [])
        self.assertEqual(dropped, 0.0)
        self.assertEqual(rebuilt, distances)

    def test_distance_going_backwards_is_a_jump_too(self):
        rebuilt, jumps, _ = fitroute.retime([0, 1, 2, 3], [0, 3, -50, -47], 5.0)
        self.assertEqual(jumps, [2])
        self.assertEqual(rebuilt, [0, 3, 6, 9])

    def test_a_repeated_timestamp_adds_no_distance(self):
        rebuilt, jumps, _ = fitroute.retime([0, 1, 1, 2], [0, 3, 3, 6], 5.0)
        self.assertEqual(jumps, [])
        self.assertEqual(rebuilt, [0, 3, 3, 6])

    def test_nothing_plausible_is_nothing_to_rebuild_from(self):
        with self.assertRaisesRegex(ValueError, 'rebuild'):
            fitroute.retime([0, 1, 2], [0, 500, 1000], 5.0)


class ReadRoute(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name='route.gpx'):
        return os.path.join(self.dir.name, name)

    def test_it_reads_the_track_points(self):
        points = fitroute.read_route(route(self.path(), north(3, 100)))
        self.assertEqual(len(points), 3)
        self.assertAlmostEqual(points[0][0], 50.0)
        self.assertAlmostEqual(points[0][1], 30.0)

    def test_two_points_are_the_least_a_route_can_be(self):
        with self.assertRaisesRegex(ValueError, 'two track points'):
            fitroute.read_route(route(self.path(), north(1, 100)))

    def test_elevations_come_back_per_point(self):
        path = route(self.path(), north(3, 100), [10.0, 20.0, 30.0])
        self.assertEqual(fitroute.read_elevations(path), [10.0, 20.0, 30.0])

    def test_one_missing_ele_is_no_profile(self):
        path = self.path()
        with open(path, 'w') as out:
            out.write(
                '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                '<trkpt lat="50.0" lon="30.0"><ele>1.0</ele></trkpt>'
                '<trkpt lat="50.1" lon="30.0"/></trkseg></trk></gpx>'
            )
        self.assertIsNone(fitroute.read_elevations(path))


class Graft(unittest.TestCase):
    """Positions are written into the records, and only those bytes move."""

    def setUp(self):
        self.points = north(5, 100)
        self.cumulative = fitroute.measure(self.points)

    def ride(self, fixes):
        """Five records 100 m apart, each with the given (lat, lon) or None."""
        records = []
        for index, fix in enumerate(fixes):
            lat, lon = fix or (SENTINEL, SENTINEL)
            records.append(
                (
                    START + index * 60,
                    [
                        (5, 'uint32', index * 100 * 100),
                        (0, 'sint32', lat if fix is None else semicircles(lat)),
                        (1, 'sint32', lon if fix is None else semicircles(lon)),
                    ],
                )
            )
        return activity(records)

    def test_the_sentinel_is_replaced_along_the_route(self):
        data = self.ride([None] * 5)
        out, stats = fitroute.graft(data, self.points)
        self.assertEqual((stats['placed'], stats['skipped']), (5, 0))
        records, _ = fitroute.read_records(out)
        for index, record in enumerate(records):
            self.assertAlmostEqual(record['fix'][0], self.points[index][0], places=5)

    def test_distance_and_timestamps_are_untouched(self):
        data = self.ride([None] * 5)
        out, _ = fitroute.graft(data, self.points)
        before, _ = fitroute.read_records(data)
        after, _ = fitroute.read_records(out)
        self.assertEqual(
            [r['distance'] for r in before], [r['distance'] for r in after]
        )
        self.assertEqual([r['stamp'] for r in before], [r['stamp'] for r in after])
        self.assertEqual(len(data), len(out))

    def test_gaps_only_keeps_a_fix_that_lies_on_the_route(self):
        fixes = [None, self.points[1], None, None, None]
        _, stats = fitroute.graft(self.ride(fixes), self.points, gaps_only=True)
        self.assertEqual(stats['kept'], 1)
        self.assertEqual(stats['placed'], 4)
        self.assertEqual(stats['anchors'], 1)

    def test_gaps_only_replaces_a_fix_that_does_not(self):
        fixes = [None, (50.5, 30.5), None, None, None]
        _, stats = fitroute.graft(self.ride(fixes), self.points, gaps_only=True)
        self.assertEqual(stats['kept'], 0)
        self.assertEqual(stats['placed'], 5)

    def test_broken_only_keeps_a_real_fix_off_the_route(self):
        # 200 m east of the route: too far to be on it, too near to be spoofed.
        lat, lon = self.points[2]
        fixes = [None, None, (lat, lon + 0.0028), None, None]
        _, stats = fitroute.graft(self.ride(fixes), self.points, broken_only=True)
        self.assertEqual(stats['off_route'], 1)
        self.assertEqual(stats['placed'], 4)

    def test_the_scale_stretches_the_ride_onto_the_route(self):
        data = self.ride([None] * 5)
        _, stats = fitroute.graft(data, north(5, 200))
        self.assertAlmostEqual(stats['ratio'], 2.0, places=3)

    def test_no_scale_matches_raw_distance(self):
        data = self.ride([None] * 5)
        out, stats = fitroute.graft(data, north(9, 100), scale=False)
        self.assertEqual(stats['ratio'], 1.0)
        records, _ = fitroute.read_records(out)
        self.assertAlmostEqual(records[-1]['fix'][0], north(9, 100)[4][0], places=5)

    def test_max_speed_rebuilds_the_axis_the_match_runs_on(self):
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

    def test_the_summary_positions_come_from_the_records(self):
        out, _ = fitroute.graft(self.ride([None] * 5), self.points)
        found = {}

        def visit(global_num, number, base_type, endian, pos, size, developer):
            if global_num in (SESSION, LAP) and size == 4 and base_type == 0x85:
                found[(global_num, number)] = struct.unpack_from(
                    endian + 'i', out, pos
                )[0]

        fitshift.walk(out, visit)
        self.assertEqual(found[(SESSION, 3)], semicircles(self.points[0][0]))
        self.assertEqual(found[(SESSION, 38)], semicircles(self.points[-1][0]))
        # 27, 28 are the lap's north-east corner and 29, 30 its south-west.
        self.assertEqual(found[(LAP, 27)], semicircles(self.points[-1][0]))
        self.assertEqual(found[(LAP, 29)], semicircles(self.points[0][0]))
        self.assertEqual(found[(LAP, 30)], semicircles(30.0))

    def test_a_record_with_no_position_field_is_skipped(self):
        records = [
            (START + index, [(5, 'uint32', index * 100 * 100)]) for index in range(5)
        ]
        _, stats = fitroute.graft(activity(records), self.points)
        self.assertEqual((stats['placed'], stats['skipped']), (0, 5))

    def test_the_file_crc_is_recomputed(self):
        out, _ = fitroute.graft(self.ride([None] * 5), self.points)
        end = fitshift.walk(out, lambda *args: None)
        self.assertEqual(struct.unpack('<H', out[end:])[0], fitshift.crc16(out[:end]))


class Unjam(unittest.TestCase):
    """The distance stream itself, rebuilt from the pace that is a pace."""

    def ride(self, distances, speed=3.0):
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

    def test_the_jump_goes_and_the_run_lands_on_the_route(self):
        data = self.ride([0, 3, 6, 9, 109, 112, 115, 118, 121, 124])
        out, stats = fitpace.unjam(data, 5.0, 27.0)
        self.assertEqual(stats['jumps'], 1)
        self.assertAlmostEqual(stats['dropped'], 97.0)
        self.assertAlmostEqual(stats['ratio'], 1.0)
        records, _ = fitroute.read_records(out)
        self.assertEqual(
            [r['distance'] for r in records], [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
        )

    def test_the_recorded_distance_stands_before_the_first_jump(self):
        data = self.ride([0, 3, 6, 9, 109, 112, 115])
        out, stats = fitpace.unjam(data, 5.0, 100.0)
        self.assertEqual(stats['split'], 3)
        records, _ = fitroute.read_records(out)
        self.assertEqual([r['distance'] for r in records][:4], [0, 3, 6, 9])
        self.assertAlmostEqual(records[-1]['distance'], 100.0)

    def test_the_speed_fields_follow_the_new_distance(self):
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

    def test_the_session_total_follows_too(self):
        data = self.ride([0, 3, 6, 9, 109, 112, 115])
        out, _ = fitpace.unjam(data, 5.0, 100.0)
        messages, _ = fitpace.read_messages(out)
        totals = [
            fields[9][0] / 100.0
            for number, fields in messages
            if number in (SESSION, LAP)
        ]
        self.assertEqual(totals, [100.0, 100.0])

    def test_a_run_with_no_jump_is_not_this_tool_s_job(self):
        with self.assertRaisesRegex(ValueError, 'faster than'):
            fitpace.unjam(self.ride([0, 3, 6, 9]), 5.0, 27.0)

    def test_a_route_shorter_than_the_good_part_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'only'):
            fitpace.unjam(self.ride([0, 3, 6, 9, 109]), 5.0, 5.0)

    def test_the_file_crc_is_recomputed(self):
        out, _ = fitpace.unjam(self.ride([0, 3, 6, 9, 109, 112]), 5.0, 27.0)
        end = fitshift.walk(out, lambda *args: None)
        self.assertEqual(struct.unpack('<H', out[end:])[0], fitshift.crc16(out[:end]))


class Pace(unittest.TestCase):
    """The heart-rate model, on a run where speed follows heart rate exactly."""

    def ride(self, count=300):
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

    def test_the_paced_run_lands_on_the_route(self):
        out, stats = fitpace.pace(self.ride(), 500.0, 1200.0)
        self.assertGreater(stats['slope'], 0)
        records, _ = fitroute.read_records(out)
        self.assertAlmostEqual(records[-1]['distance'], 1200.0, places=1)

    def test_what_was_kept_is_left_alone(self):
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

    def test_a_pace_that_ignores_heart_rate_has_no_model(self):
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

    def test_a_run_that_never_reaches_keep(self):
        with self.assertRaisesRegex(ValueError, 'never gets past'):
            fitpace.pace(self.ride(), 100000.0, 200000.0)


class Put(unittest.TestCase):
    def test_a_field_the_message_lacks_is_left_alone(self):
        out = bytearray(b'\x00\x00')
        fitpace.put(out, {}, 9, 1234)
        self.assertEqual(bytes(out), b'\x00\x00')

    def test_a_value_is_clamped_below_the_invalid_sentinel(self):
        out = bytearray(b'\x00\x00')
        fitpace.put(out, {9: (0, 0, 2, '<')}, 9, 70000)
        self.assertEqual(struct.unpack('<H', out)[0], 0xFFFE)

    def test_a_negative_value_is_clamped_to_zero(self):
        out = bytearray(b'\xff\xff')
        fitpace.put(out, {9: (0, 0, 2, '<')}, 9, -5)
        self.assertEqual(struct.unpack('<H', out)[0], 0)


class Smooth(unittest.TestCase):
    def test_it_averages_each_point_with_its_window(self):
        self.assertEqual(fitpace.smooth([1, 1, 1, 1], 1), [1, 1, 1, 1])
        self.assertEqual(fitpace.smooth([0, 0, 3, 0, 0], 1), [0, 1, 1, 1, 0])

    def test_minutes_renders_a_pace(self):
        self.assertEqual(fitpace.minutes(299), '4:59/km')
        self.assertEqual(fitpace.minutes(60), '1:00/km')


if __name__ == '__main__':
    unittest.main()
