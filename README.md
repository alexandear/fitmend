# fitmend

Small tools that repair one thing in a FIT file and leave the rest of the
activity alone.

## Requirements

Python 3 and nothing else for `fitshift.py` and for `fitroute.py`'s default
path. `fitgraft.py` and `fitroute.py --rebuild` need `fit-tool`; install it into
a virtualenv rather than system Python:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install fit-tool

## fitgraft.py — graft GPS positions from one FIT file onto another

Written for rides recorded under GPS jamming, where the Garmin's position
stream is unusable but the speed sensor, power meter and heart rate strap are
fine. Existing repair tools rebuild the track by interpolating between fixes
and overwrite the speed stream with the result. This one only touches
`position_lat` and `position_long`.

    python fitgraft.py BASE.fit DONOR.fit -o OUT.fit

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

    python fitroute.py BASE.fit ROUTE.gpx -o OUT.fit
    python fitroute.py BASE.fit ROUTE.gpx -n

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

    python fitroute.py BASE.fit ROUTE.gpx --gaps-only -o OUT.fit

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

Fixes are matched to the route by searching a window around the previous one,
so a route that passes the same street twice cannot pull a fix onto the wrong
pass.

After the last fix the match is distance alone again, with nothing to correct
it. The barometric altitude can cross-check it: if the recording's climbs sit
consistently ahead of or behind the route's `<ele>` profile, the placement is
off by that much. The tool does not apply that correction itself, since
on a flat stretch it has nothing to go on.

### When the base has no position field at all

A watch that recorded with the GPS off wrote no `position_lat`/`position_long`
in its records, so there are no bytes to patch and `placed` comes back 0. Those
records only get a position if the file is decoded and encoded again.

    python fitroute.py BASE.fit ROUTE.gpx --rebuild -o OUT.fit

`--rebuild` is opt-in because it gives up what the in-place path protects: the
output is what `fit_tool` understood of the input, not the input's own bytes.
Everything else about the match is the same.

### What it does not do

Positions are written in place, into the fields the records already have, so
`skipped` counts records the Garmin wrote with no position field at all —
nothing can be patched into them without re-encoding the file. In the example
that is 53 of 9137 in nine short runs, the longest eight records; a viewer
draws straight lines across them. The trade for that is fidelity: only
`position_lat` and `position_long` bytes change, and a re-encode's risk of
dropping the twenty-odd private Garmin message types in a modern FIT file
never arises.

Altitude is left alone. The barometric altimeter in the recording is better
than a route's elevation lookup, and `<ele>` is not what you came for.

## fitpace.py — pace the rest of a run from heart rate

For a run whose distance is right up to some point and wrong after it, such
as a watch that ran on its accelerometer and then got spoofed. Up to `--keep`
km nothing changes. From there, pace follows heart rate. No dependencies.

    python fitpace.py BASE.fit ROUTE.gpx --keep 6 -o PACED.fit
    python fitroute.py PACED.fit ROUTE.gpx --rebuild -o OUT.fit

    route     9.446 km
    kept      6.000 km in 29:55, 4:59/km
    model     speed = -0.212 + 0.0256 * hr m/s
    modelled  3.194 km by heart rate, scaled 1.07898 to 3.446 km
    paced     912 records at 4:25/km on average

The kept part supplies the model: a straight line from heart rate to speed,
fitted after the first two minutes, because heart rate lags pace while it
climbs. Heart rate is averaged over a minute for the same reason. The model
sets the shape of the rest of the run. The route sets its length, so the run
finishes on the route's finish. The ratio shows how far the model alone would
have missed.

Rewritten fields: `record.distance`, the record speed fields, and the lap and
session total distance and average and maximum speed. Positions are left to
`fitroute.py`, which places each record at its new distance.

## fitshift.py — move an activity in time

For a ride recorded with the watch on the wrong date, or one that needs to
land on the day it was actually ridden. No dependencies.

    python fitshift.py IN.fit -o OUT.fit --to '2026-09-30 07:16'
    python fitshift.py IN.fit -o OUT.fit --by 1d
    python fitshift.py IN.fit -n --by -2h30m

`--to` sets the start; `--by` shifts by a duration (`90`, `1d`, `2h30m`,
`-45m`, `+1d4h`). `-n`/`--dry-run` prints the old and new span without
writing.

`--to` is read as the activity's own local time — the wall clock the ride was
ridden on. That offset comes from the file itself: `activity.local_timestamp`
is the same instant as `activity.timestamp` rendered in the wearer's timezone,
so the difference between them is the offset. `--utc` reads `--to` as UTC
instead.

### Trust the file's offset only as far as you trust the watch

    python fitshift.py IN.fit -o OUT.fit --to '2026-09-30 07:16' --zone +3

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

Two further guards:

- A private message can reuse field `253` for something that is not a date.
  Those values are recognised by falling outside the activity's own timestamp
  window and, for reporting the span, by not coming from a `record` message.
- Every timestamp inside that window moves by the same amount, so the streams
  keep their relative order, and `local_timestamp` moves with the rest so the
  file's implied timezone offset does not change. Shifting UTC alone would
  leave viewers disagreeing about the local time.

The parser must consume exactly `data_size` bytes and land on the file CRC. If
it does not, a record was misread and the file is not written.

## License

MIT — see [LICENSE](LICENSE).
