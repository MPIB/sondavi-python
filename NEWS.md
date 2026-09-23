# sondavi 0.3.0

## Image marking questions

The platform has a new question type: participants paint areas or set pins on an
image. Its answer arrives as the stored object — the image, the grid and the cells or
pins.

* `markings()` turns these into one row per painted cell or pin, with the position
  relative to the image and in pixels of the original — the same table as
  `markings.csv` in the platform's image-markings export. This is what a heatmap is
  drawn from.
* `unnest()` writes them the way the CSV export does: one field per marking type,
  cells as row runs (`"2:3-5 3:4"`), pins as `"x,y"` pairs. Before, it spread the
  stored object into `map.image.src`, `map.grid.cols`, … — names the codebook does not
  know.

Same as the R package 0.3.0.

# sondavi 0.2.1

Test infrastructure only; the package itself is unchanged.

The run now picks a free port instead of a fixed one, and checks that the answer on it
comes from the fixture server rather than merely that something is listening — a foreign
service passed the old check, and the tests then ran against it and hung. When the server
does not come up, its output is printed instead of a bare timeout.

# sondavi 0.2.0

Functional parity with the R package.

## Timestamps are timestamps (changes existing behaviour)

`completed_at` and `started_at` now arrive from `responses()` as `datetime` objects in
the platform's own time zone, which the API states in its response. They used to be
strings, and `frame()` was the only place they became dates.

This was worth a breaking change because the old behaviour failed **silently**: strings
sort lexically and compare against a `datetime` without complaining. Pass
`parse_dates=False` if you need the raw strings, e.g. to `json.dumps()` the rows.

The time zone uses `zoneinfo` only when the platform runs on something other than UTC,
and falls back to naive datetimes if the zone database is unavailable — on Windows that
would otherwise mean depending on `tzdata`, and this package depends on nothing.

## New

* `unnest()` spreads matrices and dynamic panels into one field each, using exactly the
  names the platform's own export writes — so an analysis built on an exported file and
  one built on the API agree on variable names. Also as `frame(42, unnest=True)`.
* `Connection.waves()` joins the waves of a study series on the respondent identifier.
  Everyone seen in any wave is kept, so the people who stopped answering stay in the
  table instead of being dropped by an inner join.
* `Connection.fingerprint()` returns the line that belongs next to a published result.
* `snapshot_responses()` now warns when responses have been deleted since the snapshot
  was recorded, and separately when their contents have changed. A replay with fewer
  abilities than the snapshot was recorded with stays quiet — the digests cannot be
  compared there, and a warning that fires when nothing is wrong teaches you to ignore
  the one case it exists for.

## Fixed

* The Windows test run died with a `UnicodeEncodeError`: the console is cp1252 and the
  tick in the output is not in it. The run no longer goes through a shell script either,
  so it is the same on every system.
* The package carried the operator's own institute as the name of the platform. It is
  called Sondavi wherever it runs.

# sondavi 0.1.0

First release: `connect()`, `surveys()`, `codebook()`, `responses()`,
`iter_responses()`, `frame()`, and snapshots for citable datasets.
