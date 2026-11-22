"""Graft GPS positions from a donor FIT file onto a base FIT file.

Everything else in the base is preserved byte-for-byte in meaning: speed and
distance from the speed sensor, power, heart rate, cadence, altitude and all
lap and session summaries.
"""

import argparse
import sys
from collections.abc import Iterator
from pathlib import Path

from fit_tool.definition_message import DefinitionMessage
from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.record_message import (
    RecordMessage,
    RecordPositionLatField,
    RecordPositionLongField,
)

# GPS jamming makes a Garmin write the FIT "invalid" sentinel (0x7FFFFFFF
# semicircles == 180.0 degrees) instead of omitting the field.
SENTINEL = 180.0


def is_valid(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    if abs(lat) >= SENTINEL or abs(lon) >= SENTINEL:
        return False
    return -90.0 <= lat <= 90.0


def records(fit_file: FitFile) -> Iterator[RecordMessage]:
    for record in fit_file.records:
        message = record.message
        if isinstance(message, RecordMessage):
            yield message


def donor_positions(path: Path) -> dict[int | None, tuple[float, float]]:
    positions = {}
    for message in records(FitFile.from_bytes(path.read_bytes())):
        lat, lon = message.position_lat, message.position_long
        if lat is not None and lon is not None and is_valid(lat, lon):
            positions[message.timestamp] = (lat, lon)
    return positions


def set_position(message: RecordMessage, lat: float, lon: float) -> None:
    empty = False
    for field_class in (RecordPositionLatField, RecordPositionLongField):
        field = message.get_field(field_class.ID)
        if field is None:
            field = field_class()
            message.fields.append(field)
        # A field decoded without a value has a fixed length of zero, and the
        # encoder refuses to write past a fixed length.
        if field.size == 0:
            empty = True
        field.growable = True

    if empty:
        # DefinitionMessage.from_data_message returns the decoded definition
        # verbatim when one is attached, so a field that was absent from the
        # original record never reaches the encoder unless it is dropped first.
        message.definition_message = None  # ty: ignore[invalid-assignment]

    message.position_lat = lat
    message.position_long = lon


def graft(
    base_path: Path,
    donor_path: Path,
    out_path: Path,
    gaps_only: bool = False,
) -> dict[str, int]:
    positions = donor_positions(donor_path)
    base = FitFile.from_bytes(base_path.read_bytes())

    stats = {'records': 0, 'replaced': 0, 'filled': 0, 'kept': 0, 'unmatched': 0}
    builder = FitFileBuilder(auto_define=True)

    for record in base.records:
        message = record.message
        if isinstance(message, DefinitionMessage):
            continue
        if not isinstance(message, RecordMessage):
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

    out_path.write_bytes(builder.build().to_bytes())
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'base', type=Path, help='FIT file with the good speed/power/HR data'
    )
    parser.add_argument('donor', type=Path, help='FIT file with the good GPS track')
    parser.add_argument('-o', '--out', type=Path, required=True, help='output FIT file')
    parser.add_argument(
        '--gaps-only',
        action='store_true',
        help='keep existing fixes, fill only missing/invalid ones',
    )
    args = parser.parse_args()

    stats = graft(args.base, args.donor, args.out, args.gaps_only)
    print(f'records   {stats["records"]}')
    print(f'replaced  {stats["replaced"]}')
    print(f'filled    {stats["filled"]}')
    print(f'kept      {stats["kept"]}')
    print(f'unmatched {stats["unmatched"]}')
    print(f'wrote     {args.out}')
    return 1 if stats['unmatched'] else 0


if __name__ == '__main__':
    sys.exit(main())
