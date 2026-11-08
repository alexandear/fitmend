# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Six standalone CLI scripts, run with `uv`. The project is a uv virtual
project (`package = false`): there is no package and no build step.

```sh
uv run --extra rebuild fitgraft.py BASE.fit DONOR.fit -o OUT.fit [--gaps-only]
uv run fitroute.py BASE.fit ROUTE.gpx -o OUT.fit [--no-scale] [--reverse] [--laps N] [--rebuild] [--gaps-only] [--broken-only] [--hills] [--max-speed KMH] [-n]
uv run fitpace.py BASE.fit ROUTE.gpx (--keep KM | --max-speed KMH | --scale) [--laps N] -o OUT.fit [-n]
uv run fitstop.py BASE.fit [ROUTE.gpx] -o OUT.fit [--gap S] [--max-speed KMH] [--laps N] [-n]
uv run fithills.py BASE.fit ROUTE.gpx --from KM --to KM --repeats N -o OUT.fit [--route-out GPX] [-n]
uv run fitshift.py IN.fit -o OUT.fit (--to 'YYYY-MM-DD HH:MM' | --by 1d) [--utc] [--zone +3] [-n]
```

`fitshift.py`, `fitpace.py`, `fitstop.py`, `fithills.py` and `fitroute.py`'s
default path are stdlib-only. `fitgraft.py` and `fitroute.py --rebuild` need
`fit-tool`, which is the
`rebuild` extra rather than a dependency — pass `--extra rebuild` to `uv run`
when verifying a change to those two paths, as the lines above do. `uv`
creates and updates `.venv` itself; never `pip install` into it, and add a
dependency with `uv add` so `uv.lock` stays in step.

```sh
uv run python -m unittest                  # the whole suite
uv run --extra rebuild python -m unittest  # with the re-encoding paths too
uv run python -m unittest -v test_fitmend.Retime
```

The suite is two modules.

`test_fitmend.py` covers the library half of `fitshift.py`, `fitroute.py`,
`fitpace.py`, `fitstop.py` and `fithills.py`. Every test builds the FIT bytes
it needs with its own `encode()` helper, so it needs neither `fit-tool` nor a
sample file.
Add a test here whenever the case can be written as bytes: it is the fast half
of the suite and it states on the page the case it covers.

`test_tracks.py` runs the same library halves over the four real files in
`testdata/`, which are committed so that the suite can show the tools read
what a watch actually wrote: a big-endian export full of private Garmin
messages whose GPS was jammed, and a run recorded with no position field at
all, each with the GPX route it covered. The module docstring says what each
file holds, and the tests assert invariants rather than golden bytes — what
moved and what did not, that placed fixes land on the route, that the parse
still ends on the file CRC — so a deliberate change does not rewrite them.
`test_tracks.Rebuild` and `test_tracks.Valid` are the classes that cover
`fitgraft.py` and `fitroute.py --rebuild`; both are skipped unless `fit-tool`
is installed. `test_tracks.Destop` is the other way round: neither committed
file has a stop to repair, and it holds them that way, since the run
`fitstop.py` was written for stays out of the repo.

`tracks/` is gitignored and holds the rest of the real activities, which stay
out of the repo.

`-n`/`--dry-run` on `fitroute.py`, `fitstop.py` and `fitshift.py` reports the
result without writing; use it against `tracks/*.fit` or `testdata/*.fit` to
check a change.
`tracks/` holds real sample activities (`Lunch_Ride.fit`, `Morning_Run.fit`, a
`09.10.2026.gpx` route) alongside already-produced outputs.

`ruff` is the formatter and the linter, configured in `pyproject.toml`. Run
both before calling a change done:

```sh
uv run ruff format .
uv run ruff check .
```

`.githooks/pre-commit` runs both, in `--check` mode, and then the suite; a
clone enables it with `git config core.hooksPath .githooks`. It never edits
the code, so a commit it refuses is fixed by running the two commands above.

Single quotes, 88 columns. The rule set is broad and listed in
`pyproject.toml` with a comment on each family; both it and the `ignore` list
are the interesting part, so read them before adding a rule or a `noqa`. The
four standing exemptions are the lazy `fit_tool` import inside `rebuild()`
(`PLC0415`), `ElementTree` on a GPX file the user named (`S314`), the
one-line `open(...).read()` in each `main()` (`SIM115`), and `ISC001`, which
the formatter owns. Whole families left out rather than fought: `ANN` and `D`
(no annotations, and function names in place of per-function docstrings),
`PT` (the suite is unittest), `PTH`, `PLR` (magic values are FIT field
numbers), `T20` (print is how a CLI reports), `TRY`/`EM`, `ICN`
(`ElementTree`, not `ET`). The `CRC_TABLE` grid in `fitshift.py` is fenced
with `# fmt: off`, since one hex value per line is harder to check against
the spec than four rows of four.

## Architecture

Each tool repairs exactly one thing in a FIT file and leaves everything else
alone. That constraint drives the design, so preserve it:

- **Two layers per tool.** A library half (`graft`, `shift`, `survey`, `rebuild`,
  `read_route`, `destop`, `relap`) takes and returns bytes or plain dicts; a
  `main()` does argparse, printing and file I/O. Keep new logic in the library
  half.
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
  `fitgraft.set_position`; `fitpace.py` imports `measure`, `read_route` and
  `retime` from `fitroute.py`; `fitstop.py` imports `measure` and `read_route`
  from `fitroute.py` and `read_messages`, `rewrite` and `minutes` from
  `fitpace.py`, so the lap and session totals move one way only. `fithills.py`
  imports the same two sets, plus `put` and `AVG_SPEED` to write the lap
  totals from its spans after `rewrite` has read them off the records.

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
- **Distance credited across a pause was never covered.** A watch that
  auto-pauses on a lost fix charges the whole re-acquisition jump to the few
  seconds after it, and heart rate falling to rest through those seconds is the
  proof. `fitstop.py` removes those metres rather than re-pacing them, which is
  the opposite of what `fitpace.py --max-speed` does to the same shape of jump
  — right for a jammed receiver, wrong for a stop. A gap counts as a stop only
  when a jump follows it: a watch that drops samples while the wearer runs
  keeps measuring, and that distance is real.
- **A watch with no wheel sensor measures distance with the jammed GPS.**
  It integrates the jumps it is fed, so the odometer the match runs on is
  inflated too. `fitroute.retime` rebuilds it from the deltas that are a
  plausible pace (`--max-speed`), for placement only; `fitpace.py
  --max-speed` is what writes that rebuild into the file.
- **One ratio is not always right for a whole run.** A watch with no footpod
  measures a steep hill repeat far worse than the flat approach to it, so a
  session can read 2% short in one lap and 16% long in the next, and read the
  same ground 12% apart out and back. `fithills.py` rescales each lap onto the
  span of route its own leg covered, because the lap marks are the one thing
  such a watch recorded exactly. Scaling the whole run by one ratio instead
  would start the first climb 470 m before the hill.
- **A repeated route is not the route.** A hill session's GPX climbs the hill
  once. `fithills.py --repeats N` repeats the block from the climb's foot to
  where the descent is back at it, which is what the run covered;
  `fitroute.py --laps N` repeats the whole route, which is a different shape.
  Its `--to` need only be near the top: the top is the highest point before
  the route returns to the foot, since a turnaround rarely sits on a round
  number of metres.
- **Distance matching scales.** A wheel sensor calibrated a fraction off totals
  differently than the route measures, so `fitroute` stretches ride distance onto
  route length by default. When distance stops advancing the position stops with
  it, so a traffic light reads as a stationary rider rather than a gap.

Altitude is deliberately never touched: the barometric altimeter in a recording
beats a route's elevation lookup.

## Documentation

`README.md` is the design rationale, not just usage — it explains why each
trade-off was made. Keep it in step with behaviour changes.
