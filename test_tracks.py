"""Tests for the library half of each tool, run over real activities.

test_fitmend.py builds the FIT bytes of every case it covers, which keeps that
suite fast and independent of a sample file. What it cannot show is that the
tools read what a watch actually wrote: a real export is big-endian or not,
full of private Garmin message types, and missing every field its device never
recorded. Two of them live in testdata/, and these tests run over those bytes.

`testdata/Run_16.10.2026.fit`, with `16.10.2027.gpx` as its route.
    A jammed run, big-endian, 2708 records. Only 162 of them carry a position
    field at all, the 94 fixes among those were spoofed to another continent,
    and the watch integrated the jumps into 16.605 km of odometer for a run of
    9.446 km. Nineteen of its date fields hold the invalid sentinel.

`testdata/Lunch_Run.fit`, with `13.10.2026.gpx` as its route.
    A run recorded with the GPS off: 2517 records, not one with a position
    field, so only the re-encoding path can give it a position at all. It has
    the heart rate `fitpace.py --keep` models from, and climbs enough for
    `--hills` to find.
"""

import collections
import importlib.util
import os
import struct
import tempfile
import unittest
from itertools import pairwise

import fitpace
import fitroute
import fitshift

TESTDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata')

JAMMED = os.path.join(TESTDATA, 'Run_16.10.2026.fit')
JAMMED_ROUTE = os.path.join(TESTDATA, '16.10.2027.gpx')
BLIND = os.path.join(TESTDATA, 'Lunch_Run.fit')
BLIND_ROUTE = os.path.join(TESTDATA, '13.10.2026.gpx')

DAY = 86400

# Faster than this runner ran, slower than the jumps the jammer fed the watch.
LIMIT = 20 / 3.6

# fit-tool is the rebuild extra rather than a dependency.
FIT_TOOL = importlib.util.find_spec('fit_tool') is not None


def read(path):
    with open(path, 'rb') as source:
        return source.read()


def crc_is_right(data):
    return struct.unpack('<H', data[-2:])[0] == fitshift.crc16(data[:-2])


def message_counts(data):
    """How many data messages of each global number the file holds."""
    counted = collections.Counter()

    def start(global_num):
        counted[global_num] += 1

    fitshift.walk(data, lambda *args: None, start)
    return counted


def endians(data):
    """The byte orders the file's definition messages declared."""
    found = set()

    def visit(global_num, number, base_type, endian, pos, size, developer):
        found.add(endian)

    fitshift.walk(data, visit)
    return found


def field_bytes(data, keep):
    """The offsets of every field keep() accepts, plus the file CRC's own."""
    found = set()

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if keep(global_num, number, base_type, size, developer):
            found.update(range(pos, pos + size))

    end = fitshift.walk(data, visit)
    found.update(range(end, end + 2))
    return found


def rewritten(before, after):
    """The offsets whose byte the tool changed."""
    return {i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b}


def is_position(global_num, number, base_type, size, developer):
    """The counterpart of fitshift.is_date_time for what fitroute may write."""
    if developer or size != 4 or base_type & 0x1F != fitroute.SINT32:
        return False
    if global_num == fitshift.RECORD:
        return number in (fitroute.POSITION_LAT, fitroute.POSITION_LONG)
    return number in fitroute.SUMMARY_FIELDS.get(global_num, ())


def invalid_dates(data):
    found = []

    def visit(global_num, number, base_type, endian, pos, size, developer):
        if not fitshift.is_date_time(global_num, number, base_type, size, developer):
            return
        if struct.unpack(endian + 'I', data[pos : pos + 4])[0] == fitshift.INVALID:
            found.append((global_num, number))

    fitshift.walk(data, visit)
    return found


def furthest_off_route(data, points, cumulative):
    """How far from the route the furthest fix in data lies, in metres."""
    records, _ = fitroute.read_records(data)
    return max(
        fitroute.project(points, cumulative, record['fix'], 0, cumulative[-1])[0]
        for record in records
        if record['fix'] is not None
    )


def positions(data, global_num, number):
    """Every value of one sint32 position field, in degrees."""
    found = []

    def visit(kind, field, base_type, endian, pos, size, developer):
        if kind == global_num and field == number and size == 4:
            value = struct.unpack_from(endian + 'i', data, pos)[0]
            found.append(value / fitroute.SEMICIRCLES)

    fitshift.walk(data, visit)
    return found


def total_distance(data, global_num):
    """The first lap or session total_distance in the file, in centimetres."""
    found = []

    def visit(kind, number, base_type, endian, pos, size, developer):
        if kind == global_num and number == fitpace.TOTAL_DISTANCE and size == 4:
            found.append(struct.unpack_from(endian + 'I', data, pos)[0])

    fitshift.walk(data, visit)
    return found[0]


def sensors(data):
    """The streams every tool here promises to leave alone."""
    records, _ = fitroute.read_records(data)
    return [(r['stamp'], r['distance'], r['altitude']) for r in records]


class Parse(unittest.TestCase):
    def test_a_real_export_is_read_to_its_last_byte(self):
        for path in (JAMMED, BLIND):
            with self.subTest(file=os.path.basename(path)):
                data = read(path)
                self.assertEqual(fitshift.walk(data, lambda *args: None), len(data) - 2)
                self.assertTrue(crc_is_right(data))

    def test_a_file_can_be_written_either_way_round(self):
        self.assertEqual(endians(read(JAMMED)), {'>'})
        self.assertEqual(endians(read(BLIND)), {'<'})

    def test_a_private_message_type_is_walked_like_any_other(self):
        counted = message_counts(read(JAMMED))
        self.assertEqual(counted[fitshift.RECORD], 2708)
        # No profile documents 160, 216, 312 or 313: they are Garmin's own,
        # and they are what a decode and re-encode round trip drops.
        self.assertLessEqual({160, 216, 312, 313}, counted.keys())


class Shift(unittest.TestCase):
    def setUp(self):
        self.data = read(JAMMED)
        self.first, self.last, window, self.offset = fitshift.survey(self.data)
        # The margin main() allows, for a local_timestamp that falls on the
        # far side of the UTC day from the values it sits between.
        self.window = (window[0] - DAY, window[1] + DAY)

    def test_it_reads_the_span_and_the_wearers_offset(self):
        self.assertEqual(self.last - self.first, 2707)
        self.assertEqual(self.offset, 3 * 3600)

    def test_the_whole_activity_moves_together(self):
        out, moved = fitshift.shift(self.data, DAY, self.window)
        first, last, _, offset = fitshift.survey(out)
        self.assertEqual((first, last), (self.first + DAY, self.last + DAY))
        self.assertEqual(offset, self.offset)
        self.assertEqual(moved, 2774)

    def test_nothing_but_the_dates_is_touched(self):
        out, _ = fitshift.shift(self.data, DAY, self.window)
        self.assertEqual(len(out), len(self.data))
        self.assertLessEqual(
            rewritten(self.data, out), field_bytes(self.data, fitshift.is_date_time)
        )
        self.assertTrue(crc_is_right(out))

    def test_an_invalid_date_is_no_date_at_all(self):
        out, _ = fitshift.shift(self.data, DAY, self.window)
        self.assertEqual(len(invalid_dates(self.data)), 19)
        self.assertEqual(invalid_dates(out), invalid_dates(self.data))

    def test_a_zone_rewrites_the_offset_the_file_declares(self):
        out, _ = fitshift.shift(self.data, 0, self.window, zone=-5 * 3600)
        self.assertEqual(fitshift.survey(out)[3], -5 * 3600)


class Graft(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = read(JAMMED)
        cls.points = fitroute.read_route(JAMMED_ROUTE)
        cls.cumulative = fitroute.measure(cls.points)
        cls.out, cls.stats = fitroute.graft(
            cls.data, cls.points, broken_only=True, limit=LIMIT
        )

    def test_a_spoofed_fix_is_neither_kept_nor_trusted(self):
        # Every fix the watch recorded is a continent away, so not one of
        # them anchors the match and not one is kept as real.
        self.assertEqual(self.stats['anchors'], 0)
        self.assertEqual(self.stats['kept'], 0)
        self.assertEqual(self.stats['placed'], 162)
        self.assertEqual(self.stats['skipped'], 2546)

    def test_every_placed_fix_lands_on_the_route(self):
        self.assertLess(furthest_off_route(self.out, self.points, self.cumulative), 0.1)

    def test_nothing_but_the_positions_is_touched(self):
        self.assertEqual(len(self.out), len(self.data))
        self.assertLessEqual(
            rewritten(self.data, self.out), field_bytes(self.data, is_position)
        )
        self.assertTrue(crc_is_right(self.out))

    def test_the_sensors_are_left_as_recorded(self):
        self.assertEqual(sensors(self.out), sensors(self.data))

    def test_the_summaries_follow_the_records(self):
        records, _ = fitroute.read_records(self.out)
        fixes = [r['fix'] for r in records if r['fix'] is not None]
        session = fitroute.SESSION
        # Start, and the north-east and south-west corners of the box.
        self.assertAlmostEqual(positions(self.out, session, 3)[0], fixes[0][0])
        self.assertAlmostEqual(positions(self.out, session, 4)[0], fixes[0][1])
        self.assertAlmostEqual(
            positions(self.out, session, 29)[0], max(lat for lat, _ in fixes)
        )
        self.assertAlmostEqual(
            positions(self.out, session, 31)[0], min(lat for lat, _ in fixes)
        )

    def test_the_odometer_the_match_runs_on_is_rebuilt(self):
        *_, recorded = fitroute.match(self.data, self.points)
        # The watch measured the run with the GPS it was being fed, so its
        # total is most of a second run longer than the route.
        self.assertAlmostEqual(recorded['ride_length'], 16604.98, places=2)
        self.assertAlmostEqual(self.stats['dropped'], 7418.29, places=2)
        self.assertEqual(self.stats['replaced'], 92)
        # Rebuilt from the deltas that are a running pace, it agrees with the
        # route to within a few per cent.
        self.assertLess(abs(1 - self.stats['ratio']), 0.03)


class Hills(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = read(BLIND)
        cls.points = fitroute.read_route(BLIND_ROUTE)
        cls.elevations = fitroute.read_elevations(BLIND_ROUTE)

    def test_the_route_carries_an_elevation_for_every_point(self):
        self.assertEqual(len(self.elevations), len(self.points))

    def test_a_climb_anchors_the_middle_of_a_blind_run(self):
        *_, plain = fitroute.match(self.data, self.points)
        self.assertEqual(plain['hills'], 0)

        _, anchors, _, stats = fitroute.match(
            self.data, self.points, elevations=self.elevations
        )
        self.assertEqual(stats['hills'], 2)
        # Scaling gives the two ends; the climbs pin two points between them,
        # and every anchor moves forward on both axes.
        self.assertEqual(len(anchors), 4)
        for (ride1, route1), (ride2, route2) in pairwise(anchors):
            self.assertLess(ride1, ride2)
            self.assertLess(route1, route2)


class Pace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blind_length = fitroute.measure(fitroute.read_route(BLIND_ROUTE))[-1]
        cls.jammed_length = fitroute.measure(fitroute.read_route(JAMMED_ROUTE))[-1]

    def test_a_heart_rate_model_finishes_on_the_route(self):
        data = read(BLIND)
        out, stats = fitpace.pace(data, 3000.0, self.blind_length)

        self.assertEqual(len(out), len(data))
        self.assertTrue(crc_is_right(out))
        self.assertGreater(stats['slope'], 0)

        recorded = [distance for _, distance, _ in sensors(data)]
        paced = [distance for _, distance, _ in sensors(out)]
        # The watch had the first three km right; only what follows moves.
        self.assertEqual(paced[: stats['split'] + 1], recorded[: stats['split'] + 1])
        self.assertAlmostEqual(paced[-1], self.blind_length, places=1)
        self.assertEqual(paced, sorted(paced))

    def test_the_totals_follow_the_records(self):
        data = read(BLIND)
        out, _ = fitpace.pace(data, 3000.0, self.blind_length)
        self.assertEqual(
            total_distance(out, fitpace.SESSION), round(self.blind_length * 100)
        )
        self.assertGreater(
            total_distance(out, fitpace.LAP), total_distance(data, fitpace.LAP)
        )

    def test_the_jammed_odometer_is_replaced_by_the_pace_that_was_left(self):
        data = read(JAMMED)
        out, stats = fitpace.unjam(data, LIMIT, self.jammed_length)

        self.assertEqual(len(out), len(data))
        self.assertTrue(crc_is_right(out))
        self.assertEqual(stats['jumps'], 92)
        self.assertAlmostEqual(stats['dropped'], 7418.29, places=2)

        paced = [(stamp, distance) for stamp, distance, _ in sensors(out)]
        self.assertAlmostEqual(paced[-1][1], self.jammed_length, places=1)
        # Not one second of the rebuilt run is faster than a run.
        for (stamp1, first), (stamp2, second) in pairwise(paced):
            self.assertLessEqual(first, second)
            if stamp2 > stamp1:
                self.assertLessEqual(second - first, LIMIT * (stamp2 - stamp1))


@unittest.skipUnless(FIT_TOOL, 'fit-tool is the rebuild extra, not a dependency')
class Rebuild(unittest.TestCase):
    """The two paths that re-encode: fitroute --rebuild and fitgraft."""

    @classmethod
    def setUpClass(cls):
        import fitgraft

        cls.fitgraft = fitgraft
        cls.data = read(BLIND)
        cls.points = fitroute.read_route(BLIND_ROUTE)
        cls.cumulative = fitroute.measure(cls.points)
        cls.out, cls.stats = fitroute.rebuild(BLIND, cls.points)

        # A donor holding a different position at every one of the same
        # timestamps: the same run placed round the route backwards.
        donor, _ = fitroute.graft(cls.out, cls.points[::-1])

        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.rebuilt_path = os.path.join(directory.name, 'rebuilt.fit')
        cls.donor_path = os.path.join(directory.name, 'donor.fit')
        cls.out_path = os.path.join(directory.name, 'out.fit')
        for path, content in ((cls.rebuilt_path, cls.out), (cls.donor_path, donor)):
            with open(path, 'wb') as target:
                target.write(content)

    def fixes(self, path):
        records, _ = fitroute.read_records(read(path))
        return [r['fix'] for r in records]

    def test_a_record_written_without_a_position_gets_one(self):
        before, _ = fitroute.read_records(self.data)
        self.assertEqual([r for r in before if r['lat'] is not None], [])
        self.assertEqual(self.stats['placed'], 2517)
        self.assertEqual(self.stats['skipped'], 0)

        after, _ = fitroute.read_records(self.out)
        self.assertEqual(len(after), len(before))
        self.assertEqual([r for r in after if r['fix'] is None], [])
        self.assertLess(furthest_off_route(self.out, self.points, self.cumulative), 0.1)

    def test_the_re_encoded_file_still_parses(self):
        self.assertEqual(fitshift.walk(self.out, lambda *args: None), len(self.out) - 2)
        self.assertTrue(crc_is_right(self.out))

    def test_the_sensors_survive_the_re_encode(self):
        self.assertEqual(sensors(self.out), sensors(self.data))

    def test_a_donor_fills_a_base_that_has_no_position_field(self):
        stats = self.fitgraft.graft(BLIND, self.donor_path, self.out_path)
        self.assertEqual(
            stats,
            {
                'records': 2517,
                'replaced': 0,
                'filled': 2517,
                'kept': 0,
                'unmatched': 0,
            },
        )
        self.assertEqual(self.fixes(self.out_path), self.fixes(self.donor_path))
        self.assertEqual(sensors(read(self.out_path)), sensors(self.data))

    def test_gaps_only_keeps_the_fixes_the_base_already_has(self):
        stats = self.fitgraft.graft(
            self.rebuilt_path, self.donor_path, self.out_path, gaps_only=True
        )
        self.assertEqual(stats['kept'], 2517)
        self.assertEqual(stats['replaced'], 0)
        self.assertEqual(self.fixes(self.out_path), self.fixes(self.rebuilt_path))

    def test_without_it_every_fix_comes_from_the_donor(self):
        stats = self.fitgraft.graft(self.rebuilt_path, self.donor_path, self.out_path)
        self.assertEqual(stats['replaced'], 2517)
        self.assertEqual(self.fixes(self.out_path), self.fixes(self.donor_path))


if __name__ == '__main__':
    unittest.main()
