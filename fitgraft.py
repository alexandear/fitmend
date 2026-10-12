"""Graft GPS positions from a donor FIT file onto a base FIT file.

Everything else in the base is preserved byte-for-byte in meaning: speed and
distance from the speed sensor, power, heart rate, cadence, altitude and all
lap and session summaries.
"""

import argparse
import sys

from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.definition_message import DefinitionMessage
from fit_tool.profile.messages.record_message import (
    RecordPositionLatField,
    RecordPositionLongField,
)

# GPS jamming makes a Garmin write the FIT "invalid" sentinel (0x7FFFFFFF
# semicircles == 180.0 degrees) instead of omitting the field.
SENTINEL = 180.0


def is_valid(lat, lon):
    if lat is None or lon is None:
        return False
    if abs(lat) >= SENTINEL or abs(lon) >= SENTINEL:
        return False
    return -90.0 <= lat <= 90.0


def records(fit_file):
    for record in fit_file.records:
        message = record.message
        if type(message).__name__ == 'RecordMessage':
            yield message


def donor_positions(path):
    positions = {}
    for message in records(FitFile.from_file(path)):
        if is_valid(message.position_lat, message.position_long):
            positions[message.timestamp] = (message.position_lat, message.position_long)
    return positions


def set_position(message, lat, lon):
    empty = False
    for field_class in (RecordPositionLatField, RecordPositionLongField):
        if not any(getattr(f, 'ID', None) == field_class.ID for f in message.fields):
            message.fields.append(field_class())
        field = message.get_field(field_class.ID)
        # A field decoded without a value has a fixed length of zero, and the
        # encoder refuses to write past a fixed length.
        if field.size == 0:
            empty = True
        field.growable = True

    if empty:
        # DefinitionMessage.from_data_message returns the decoded definition
        # verbatim when one is attached, so a field that was absent from the
        # original record never reaches the encoder unless it is dropped first.
        message.definition_message = None

    message.position_lat = lat
    message.position_long = lon


def graft(base_path, donor_path, out_path, gaps_only=False):
    positions = donor_positions(donor_path)
    base = FitFile.from_file(base_path)

    stats = {'records': 0, 'replaced': 0, 'filled': 0, 'kept': 0, 'unmatched': 0}
    builder = FitFileBuilder(auto_define=True)

    for record in base.records:
        message = record.message
        if isinstance(message, DefinitionMessage):
            continue
        if type(message).__name__ != 'RecordMessage':
            builder.add(message)
            continue

        stats['records'] += 1
        had_fix = is_valid(message.position_lat, message.position_long)
        if had_fix and gaps_only:
            stats['kept'] += 1
            builder.add(message)
            continue

        donor = positions.get(message.timestamp)
        if donor is None:
            stats['unmatched'] += 1
            builder.add(message)
            continue

        set_position(message, *donor)
        stats['replaced' if had_fix else 'filled'] += 1
        builder.add(message)

    builder.build().to_file(out_path)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='FIT file with the good speed/power/HR data')
    parser.add_argument('donor', help='FIT file with the good GPS track')
    parser.add_argument('-o', '--out', required=True, help='output FIT file')
    parser.add_argument('--gaps-only', action='store_true',
                        help='keep existing fixes, fill only missing/invalid ones')
    args = parser.parse_args()

    stats = graft(args.base, args.donor, args.out, args.gaps_only)
    print(f"records   {stats['records']}")
    print(f"replaced  {stats['replaced']}")
    print(f"filled    {stats['filled']}")
    print(f"kept      {stats['kept']}")
    print(f"unmatched {stats['unmatched']}")
    print(f"wrote     {args.out}")
    return 1 if stats['unmatched'] else 0


if __name__ == '__main__':
    sys.exit(main())
