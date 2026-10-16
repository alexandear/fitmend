# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Four standalone CLI scripts, run directly. There is no package, build step, test
suite or linter.

```sh
python fitgraft.py BASE.fit DONOR.fit -o OUT.fit [--gaps-only]
python fitroute.py BASE.fit ROUTE.gpx -o OUT.fit [--no-scale] [--reverse] [--rebuild] [--gaps-only] [-n]
python fitpace.py BASE.fit ROUTE.gpx --keep KM -o OUT.fit [-n]
python fitshift.py IN.fit -o OUT.fit (--to 'YYYY-MM-DD HH:MM' | --by 1d) [--utc] [--zone +3] [-n]
```

`fitshift.py`, `fitpace.py` and `fitroute.py`'s default path are stdlib-only. `fitgraft.py` and
`fitroute.py --rebuild` need `fit-tool`, which is not installed in the working
environment — create a virtualenv first and verify any change to those paths in it:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install fit-tool
```

Never `pip install` outside the virtualenv.

`-n`/`--dry-run` on `fitroute.py` and `fitshift.py` reports the result without
writing; use it against `tracks/*.fit` to check a change. `tracks/` holds real
sample activities (`Lunch_Ride.fit`, `Morning_Run.fit`, a `09.10.2026.gpx` route)
alongside already-produced outputs.

## Architecture

Each tool repairs exactly one thing in a FIT file and leaves everything else
alone. That constraint drives the design, so preserve it:

- **Two layers per tool.** A library half (`graft`, `shift`, `survey`, `rebuild`,
  `read_route`) takes and returns bytes or plain dicts; a `main()` does argparse,
  printing and file I/O. Keep new logic in the library half.
- **In-place byte patching is the default; re-encoding is opt-in.** `fitshift.py`
  and `fitroute.py` parse the FIT binary themselves and overwrite only the fields
  they own, then recompute the trailing file CRC. A decode/re-encode round-trip
  through `fit_tool` yields only what the library understood of the input and can
  drop the private Garmin message types a modern file is full of — which is why
  `fitroute.py --rebuild` is a flag rather than the path.

`fitshift.py` owns the shared FIT parser and both other tools build on it:

- `walk(data, visit, on_message)` iterates every field of every data message,
  handling definition messages, big/little endian, developer fields and
  compressed-timestamp headers. It returns the offset of the file CRC, and
  **raises unless the parse lands exactly there** — that equality is the proof no
  record was misread, and the file is not written otherwise.
- `crc16`, `RECORD`, and the base-type/field-number constants come from here too.
  `fitroute.py` imports `RECORD`, `crc16` and `walk`; `fitroute.rebuild` imports
  `fitgraft.set_position`.

Correctness traps the code already handles — don't regress them:

- **Not every `uint32` is a date.** `is_date_time` admits field 253 plus an
  explicit allowlist (`file_id.time_created`, `session.start_time`,
  `lap.start_time`, `activity.local_timestamp`). Counters decode as plausible
  dates; moving one corrupts data.
- **Private messages reuse field 253** for non-dates. `shift` only moves values
  inside the activity's own timestamp window, and `survey` reads the reported span
  from `record` messages alone.
- **Local time comes from the file**: `activity.local_timestamp - activity.timestamp`
  is the wearer's UTC offset. It shifts with everything else so the implied offset
  is unchanged; `--zone` instead rewrites it, for a head unit that kept a stale
  timezone.
- **GPS jamming writes the invalid sentinel** (`0x7FFFFFFF` semicircles = 180.0
  degrees) rather than omitting the field, so `fitgraft.is_valid` rejects it
  explicitly.
- **A field absent from the original record cannot simply be assigned.**
  `fitgraft.set_position` appends the field class, sets `growable`, and drops
  `message.definition_message` when the decoded field had size 0 — otherwise
  `fit_tool` reuses the decoded definition and never writes the new field.
- **Distance matching scales.** A wheel sensor calibrated a fraction off totals
  differently than the route measures, so `fitroute` stretches ride distance onto
  route length by default. When distance stops advancing the position stops with
  it, so a traffic light reads as a stationary rider rather than a gap.

Altitude is deliberately never touched: the barometric altimeter in a recording
beats a route's elevation lookup.

## Documentation

`README.md` is the design rationale, not just usage — it explains why each
trade-off was made. Keep it in step with behaviour changes.
