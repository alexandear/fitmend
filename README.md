# fitmend

Small tools that repair one thing in a FIT file and leave the rest of the
activity alone.

## Requirements

[uv](https://docs.astral.sh/uv/) and nothing else. It reads `pyproject.toml`,
creates `.venv` on the first `uv run` and keeps it in step with `uv.lock`;
there is no install step. Python 3.15 is the floor, and uv fetches it itself
if the machine has no Python that new.

`fitshift.py`, `fitpace.py` and `fitroute.py`'s default path are stdlib-only,
so plain `uv run` is enough:

    uv run fitshift.py IN.fit -n --by 1d

`fitgraft.py` and `fitroute.py --rebuild` are the two paths that re-encode and
need `fit-tool`. It is the `rebuild` extra rather than a dependency, so the
stdlib-only paths stay that way:

    uv run --extra rebuild fitgraft.py BASE.fit DONOR.fit -o OUT.fit

## fitgraft.py — graft GPS positions from one FIT file onto another

Written for rides recorded under GPS jamming, where the Garmin's position
stream is unusable but the speed sensor, power meter and heart rate strap are
fine. Existing repair tools rebuild the track by interpolating between fixes
and overwrite the speed stream with the result. This one only touches
`position_lat` and `position_long`.

    uv run --extra rebuild fitgraft.py BASE.fit DONOR.fit -o OUT.fit

`BASE` supplies everything: speed, distance, power, heart rate, cadence,
altitude, laps and session totals. `DONOR` supplies only coordinates, matched
by record timestamp.

`--gaps-only` keeps the base's existing fixes and fills in just the missing or
invalid ones. The default replaces every position, which is what you want when
jamming has left bogus coordinates rather than gaps.

## fitroute.py — graft a GPX route's shape, matched by distance

When the donor is a *planned route* rather than a recording. A Strava route
has no `<time>` on its track points, so `fitgraft.py` has nothing to match on.
What both files share is distance travelled: each record is placed at its own
odometer reading along the route. No dependencies.

    uv run fitroute.py BASE.fit ROUTE.gpx -o OUT.fit
    uv run fitroute.py BASE.fit ROUTE.gpx -n

`--reverse` if the ride went round the route the other way. `-n`/`--dry-run`
reports the match without writing.

    route     1125 points, 70.607 km
    ride      9137 records, 71.138 km
    scale     0.99253
    placed    9084
    skipped   53 records written without a position field

A wheel sensor calibrated a fraction off reads a different total than the route
measures — 0.75% in that example. By default the ride is stretched onto the
route so the finish lands on the finish rather than drifting past it by that
fraction; `--no-scale` matches raw distances and pins anything past the end to
the last point.

Stops cost nothing: when distance stops advancing the position stops with it,
so a traffic light reads as a stationary rider rather than a gap.

### When part of the ride's GPS is good

    uv run fitroute.py BASE.fit ROUTE.gpx --gaps-only -o OUT.fit

A ride that ran into jamming halfway through has real fixes up to that point,
and they beat any route. `--gaps-only` keeps every fix that lies within 25 m
of the route and places only the rest: dropouts, the jammer's `180.0` sentinel,
and spoofed coordinates, which land kilometres off the route and are rejected
by that distance.

    route     1222 points, 72.607 km
    ride      9820 records, 72.503 km
    scale     1.00144
    kept      5892 fixes on the route, 5756 anchors
    placed    3928
    skipped   0 records written without a position field

The kept fixes also anchor the match. A single scale over the whole ride
assumes the wheel sensor's error is spread evenly along it. In that example
the wheel was already 115 m behind the route by 48 km, so one ratio left
points in the middle up to 130 m off. With anchors, each gap is placed between
the trusted fixes on either side of it. Past the last fix, the default scaling
runs the ride on to finish on the route's finish; with `--no-scale`, the last
fix's offset carries on unchanged.

Fixes are matched to the route by searching a window around where the wheel
sensor says the rider is, counted on from the previous fix, so a route that
passes the same street twice cannot pull a fix onto the wrong pass. That
matters most on a loop after a long gap: a ride spoofed for 44 km and then
back on real GPS finishes next to the route's start, and a window left at the
last fix would trust those finishing fixes as the first kilometre.

### When the real GPS leaves the route

    uv run fitroute.py BASE.fit ROUTE.gpx --broken-only -o OUT.fit

`--gaps-only` decides by distance to the route, so it also replaces real
fixes that are more than 25 m off it: a detour, a road beside the one drawn,
the last metres to the door. `--broken-only` keeps those too and places only
the records that are actually broken: no fix, the `180.0` sentinel, or a
spoofed fix more than 2 km from anywhere on the route.

    kept      6352 fixes on the route, 6249 anchors
    kept      97 real fixes off the route
    placed    522

In that example `--gaps-only` would have placed 619 records. The other 97 were
a 60 m offset near the start, the rider leaving the route just before the
jamming began, and the finish. Only fixes on the route anchor the match; an
off-route fix has no position along the route. Where a real off-route fix
meets a placed one the track jumps back onto the route, because the path
between them was never recorded. A spoofer that puts its fixes within 2 km of
the route will pass as real; use `--gaps-only` for that.

### When the route is one lap of several

    uv run fitroute.py BASE.fit ROUTE.gpx --laps 2 -o OUT.fit

A route drawn as a loop is one lap, and a run of several laps covers that
route again each time round. `--laps N` repeats it N times before matching,
so the ride's distance maps onto the whole run instead of being squeezed into
one lap. The join is the loop's closing point next to its start, which is
where the runner was. A route that is not a loop would teleport back to the
start there, so repeating is opt-in.

### When the distance is jammed too

    uv run fitroute.py BASE.fit ROUTE.gpx --broken-only --max-speed 20 -o OUT.fit

A ride with a wheel sensor keeps its distance through jamming, which is what
makes distance worth matching on. A watch with nothing but the GPS integrates
the jumps it is fed: on one two-lap trail run, 210 seconds of spoofing added
8 km that was never run, and the odometer that should place the rest of the
run was useless.

    route     388 points, 10.236 km, 2 laps
    ride      3781 records, 9.737 km
    pace      8.052 km of inflated distance dropped from 135 records
    scale     1.05123
    kept      1266 fixes on the route, 1230 anchors
    placed    2515

`--max-speed` is the fastest the activity could plausibly have gone, in km/h.
A second that advances faster than that was not run, so it is replaced by the
median speed of the seconds that could have been — the pace the watch
recorded, used only where it is a pace and not a jump. The records after the
jamming then keep their place in the run instead of being pushed kilometres
along the route.

Only the axis the match runs on changes. `record.distance` in the output is
still what the watch recorded, because this tool writes positions and nothing
else. For the activity to report the distance it covered as well, run
`fitpace.py --max-speed` over it first; `fitroute.py` then needs no
`--max-speed` of its own, because the distance it matches on is already a pace.

### When the hills say where the ride was

    uv run fitroute.py BASE.fit ROUTE.gpx --gaps-only --hills -o OUT.fit

Between trusted fixes the match is distance alone, with nothing to correct
it. The barometric altitude can: if the recording's climbs sit consistently
ahead of or behind the route's `<ele>` profile, the placement is off by that
much. `--hills` compares the two over a 3 km window every kilometre, tries
every lag up to 300 m either way, and anchors the window's centre at the lag
whose shape matches best. Only shape is compared, so the altimeter's absolute
offset from the route's elevation model does not matter.

    kept      1133 fixes on the route, 1112 anchors
    hills     32 anchors from the climbs

In that example the wheel put every climb of the first 43 km 50–190 m ahead
of the route; with the anchors the residual is within a sample or two, and
the altitude mismatch drops from 4–6 m RMS to about 1 m. On the stretch with
real fixes the comparison already read zero, which is the check that the
method finds the placement's error and not its own.

A window is used only with at least 15 m of relief and a correlation of 0.95.
A flat stretch has nothing to go on, and a wrong anchor is worse than none,
so there the wheel sensor stands. Anchors that would make the ride run
backwards are dropped. It is opt-in because it needs `<ele>` on every track
point and a route that was actually followed: a detour shifts the climbs for
a reason no lag can model.

### When the base has no position field at all

A watch that recorded with the GPS off wrote no `position_lat`/`position_long`
in its records, so there are no bytes to patch and `placed` comes back 0. Those
records only get a position if the file is decoded and encoded again.

    uv run --extra rebuild fitroute.py BASE.fit ROUTE.gpx --rebuild -o OUT.fit

`--rebuild` is opt-in because it gives up what the in-place path protects: the
output is what `fit_tool` understood of the input, not the input's own bytes.
Everything else about the match is the same, including `--gaps-only` and
`--hills`, for a file whose GPS dropped out mid-ride and wrote stretches of
records without the field:

    uv run --extra rebuild fitroute.py BASE.fit ROUTE.gpx --gaps-only --hills --rebuild -o OUT.fit

The match is still made on the file's own bytes and paired with `fit_tool`'s
decode record by record; if the two disagree on the record count the file is
not written.

### Lap and session positions

Laps and the session keep their own start, end and bounding-box positions,
which still point wherever the spoofer said once the records have moved.
Viewers place the activity from them, and Strava picks the timezone it shows
the start time in from the location. Both paths recompute them from the
placed records, in place: each lap from the records between its start and
the next lap's, the session from all of them.

### What it does not do

Positions are written in place, into the fields the records already have, so
`skipped` counts records the Garmin wrote with no position field at all —
nothing can be patched into them without re-encoding the file. In the example
that is 53 of 9137 in nine short runs, the longest eight records; a viewer
draws straight lines across them. The trade for that is fidelity: only
position bytes change, and a re-encode's risk of dropping the twenty-odd
private Garmin message types in a modern FIT file never arises.

Altitude is left alone. The barometric altimeter in the recording is better
than a route's elevation lookup, and `<ele>` is not what you came for.

## fitpace.py — rebuild a run's distance

For a run whose distance is wrong: a watch that ran on its accelerometer and
then got spoofed, or one that measured with the jammed GPS itself. The route
sets the length, so the run finishes where it finished. Positions are left to
`fitroute.py`, which places each record at its new distance. No dependencies.

### From heart rate, where the pace is gone

Up to `--keep` km nothing changes. From there, pace follows heart rate.

    uv run fitpace.py BASE.fit ROUTE.gpx --keep 6 -o PACED.fit
    uv run --extra rebuild fitroute.py PACED.fit ROUTE.gpx --rebuild -o OUT.fit

    route     9.446 km
    kept      6.000 km in 29:55, 4:59/km
    model     speed = -0.212 + 0.0256 * hr m/s
    modelled  3.194 km by heart rate, scaled 1.07898 to 3.446 km
    paced     912 records at 4:25/km on average

The `--keep` part supplies the model: a straight line from heart rate to speed,
fitted after the first two minutes, because heart rate lags pace while it
climbs. Heart rate is averaged over a minute for the same reason. The model
sets the shape of the rest of the run. The route sets its length, so the run
finishes on the route's finish. The ratio shows how far the model alone would
have missed.

### From the recorded pace, where only the jumps are wrong

Jamming does not always ruin the pace throughout. `--max-speed` keeps every
second whose pace a runner could have run and replaces only the jumps with
the median of those seconds, then stretches what follows the first jump onto
the route's length.

    uv run fitpace.py BASE.fit ROUTE.gpx --max-speed 20 --laps 2 -o PACED.fit
    uv run --extra rebuild fitroute.py PACED.fit ROUTE.gpx --laps 2 --broken-only --rebuild -o OUT.fit

On one two-lap trail run the watch recorded a believable pace for all but
210 seconds of spoofing, which added 8 km it never ran:

    route     10.236 km, 2 laps
    kept      4.633 km in 33:56, 7:19/km
    dropped   8.052 km of inflated distance from 135 records
    rebuilt   5.104 km from the pace that was left, scaled 1.09772 to 5.603 km
    paced     2044 records at 6:20/km on average

`--laps N` measures the route as that many laps of it, for a loop covered more
than once. The ratio is what the rebuilt pace alone would have fallen short
by — 10% here, from a receiver still being jammed where it was not jumping
outright. It lands on the route's length because that is the length that was
run; nothing else in the recording knows how far the run went.

### What it rewrites

`record.distance`, the record speed fields, and the lap and session total
distance and average and maximum speed. Everything else, including heart rate,
cadence, altitude and timestamps, is left as recorded.

## fitshift.py — move an activity in time

For a ride recorded with the watch on the wrong date, or one that needs to
land on the day it was actually ridden. No dependencies.

    uv run fitshift.py IN.fit -o OUT.fit --to '2026-09-30 07:16'
    uv run fitshift.py IN.fit -o OUT.fit --by 1d
    uv run fitshift.py IN.fit -n --by -2h30m

`--to` sets the start; `--by` shifts by a duration (`90`, `1d`, `2h30m`,
`-45m`, `+1d4h`). `-n`/`--dry-run` prints the old and new span without
writing.

`--to` is read as the activity's own local time — the wall clock the ride was
ridden on. That offset comes from the file itself: `activity.local_timestamp`
is the same instant as `activity.timestamp` rendered in the wearer's timezone,
so the difference between them is the offset. `--utc` reads `--to` as UTC
instead.

### Trust the file's offset only as far as you trust the watch

    uv run fitshift.py IN.fit -o OUT.fit --to '2026-09-30 07:16' --zone +3

`--zone` reads `--to` as a time in that UTC offset and writes the offset back
into `activity.local_timestamp`, so the file agrees with where the ride
happened.

Use it whenever the recorded offset and the GPS disagree. A head unit that
kept a stale timezone writes a `local_timestamp` for somewhere the rider has
not been, and shifting to a local time then lands hours off. Strava resolves
that conflict by believing the GPS, not the file: the same ride uploaded with
its positions jammed was named from the file's offset, and again with a real
track through Kyiv was named from Kyiv's. Without `--zone`, `fitshift.py`
believes the file — which is why a ride can move by a whole timezone while
every clock in it still looks self-consistent.

    start  2026-09-29 23:19:05 local / 2026-09-30 04:19:05 UTC
    end    2026-09-30 02:27:44 local / 2026-09-30 07:27:44 UTC
    shift  28615s
    new    2026-09-30 07:16:00 local / 2026-09-30 12:16:00 UTC
    moved  20204 timestamps

### How it works

Unlike `fitgraft.py`, this one does not decode and re-encode the file. A
`date_time` is a `uint32` of seconds since `1989-12-31 00:00 UTC`, so a shift
is an add in place: same records, same field layout, same byte sizes. Only the
trailing file CRC is recomputed, over header and data. The 14-byte header's
own CRC covers bytes 0–11, which do not change.

What counts as a `date_time` is the whole problem. Field `253` is `timestamp`
in every message type; beyond that the tool knows `file_id.time_created`,
`session.start_time`, `lap.start_time` and `activity.local_timestamp`. Every
other `uint32` is left alone, because a counter or an accumulator can decode
as a perfectly plausible date — `event` field 3 in a Garmin file reads as
2015 — and moving one would corrupt real data.

Three further guards:

- `0xFFFFFFFF` is the FIT "invalid" value, not a date. A private message can
  fill field `253` with it, and counting it would stretch the window to the
  end of time and move the sentinel into a real-looking date in 2126.

- A private message can reuse field `253` for something that is not a date.
  Those values are recognised by falling outside the activity's own timestamp
  window and, for reporting the span, by not coming from a `record` message.
- Every timestamp inside that window moves by the same amount, so the streams
  keep their relative order, and `local_timestamp` moves with the rest so the
  file's implied timezone offset does not change. Shifting UTC alone would
  leave viewers disagreeing about the local time.

The parser must consume exactly `data_size` bytes and land on the file CRC. If
it does not, a record was misread and the file is not written.

## Tests

    uv run python -m unittest                  # the whole suite
    uv run --extra rebuild python -m unittest  # with the re-encoding paths too

`test_fitmend.py` builds the FIT bytes of every case it covers, so it needs no
sample activity and no `fit-tool`. It covers the library half of the three
stdlib-only tools: the parser and the shift, the route geometry and the match,
the distance rebuild, and the correctness traps each of them exists to avoid —
a private field 253 that is a counter rather than a date, the invalid
sentinels for a date and for a position, a record written with no position
field, and a route that passes the same point twice.

A file built by a test is a file written to be understood, which is the one
thing a file off a watch never is. So `test_tracks.py` runs the same library
halves over two real activities, committed in `testdata/` with the route each
one covered:

- `Run_16.10.2026.fit` with `16.10.2027.gpx`, a jammed run. Big-endian, full
  of private Garmin message types, 162 of its 2708 records carrying a position
  field at all, the 94 fixes among those spoofed to another continent, 19 of
  its date fields holding the invalid sentinel, and 16.605 km of odometer
  integrated out of the jumps for a run of 9.446 km.
- `Lunch_Run.fit` with `13.10.2026.gpx`, a run recorded with the GPS off. Not
  one of its 2517 records carries a position field, so only the re-encoding
  path can give it a position at all, and it is where `--rebuild` and
  `fitgraft.py` are tested — the class that needs `fit-tool`, and the only one
  that is skipped without it.

What those tests assert is invariants, not golden bytes: that the parse ends
on the file CRC, that the only bytes a tool rewrote belong to the fields it
owns, that every placed fix lands on the route, that the sensor streams read
back exactly as recorded, and that the rebuilt odometer agrees with the route
to within a few per cent. A deliberate change to an output does not rewrite
them; a regression fails them.

## Style

    uv run ruff format .
    uv run ruff check .

`ruff` formats and lints, configured in `pyproject.toml`: single quotes, 88
columns, and most of the rule families that do not argue with how the code is
written. The exclusions are listed there with a reason each, and four are
worth repeating, because each one is a decision rather than an oversight:

- `PLC0415`, imports not at top level. `fitroute.rebuild()` imports
  `fit_tool` inside the function. That is what keeps the default path
  stdlib-only; hoisting it would make every run depend on the library.
- `SIM115`, `open()` without a context manager. Each tool reads its input
  with a one-line `open(path, 'rb').read()` and writes its output the same
  way, in a `main()` that exits straight after. A `with` block there buys a
  level of indentation and nothing else.
- `S314`, `ElementTree` on untrusted XML. The GPX is a route file the user
  named on the command line, and `defusedxml` would be a dependency.
- `PLR2004`, magic values. The constants in the parser are FIT field and base
  type numbers; naming each one moves the spec away from the code that reads
  it.

The `CRC_TABLE` in `fitshift.py` is fenced with `# fmt: off` so the sixteen
values stay a grid, which is how they are checked against the FIT spec.

## License

MIT — see [LICENSE](LICENSE).
