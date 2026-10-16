# sondavi 0.7.0

## Every row says what became of it

* Each response now carries `response_status`: `"complete"`, `"partial"` (stopped before the
  end) or `"screened_out"`. In `frame()` it is a Categorical with all three categories, in
  this order, so `value_counts()` shows a 0 where nobody was screened out.
* A status this version does not know is kept as a category of its own, with a warning.
* The client now names its own version when it talks to the platform (it said 0.4.0).

# sondavi 0.6.1

## Screened-out participants and bot checks

* A study can now screen people out (the platform's "Screen out participant" rule). Such a
  person is a row like an unfinished one - `completed_at` is None - and carries
  `screened_out_at` and `screen_out_reason` instead. `screened_out_at` arrives as a
  `datetime` like `completed_at`, also in `frame()`.
* A bot check asked as a question arrives as a field of its own, `"passed"` / `"failed"`
  (in `frame()` with its labels), and `bot_check_passed` is filled for every study that checks.

The tests now include a study with a screen-out and a bot check question, recorded from a
real instance.

# sondavi 0.6.0

## AI interviews

* `unnest()` writes an AI interview question the way the platform's export and codebook do:
  `<question>.status`, `.turns`, and where they apply `.answered`, `.skipped` and `.summary`.
  It used to spread the stored conversation into one column per message and field
  (`talk.messages.3.text` …), and the columns the codebook names for the question were missing.
* `conversations(rows)` is the conversation as a long table, one row per message: who
  wrote it, the respondent's answer number, the text, typed or spoken, and when.

The tests now include a study with AI interviews, recorded from a real run.

# sondavi 0.5.0

## Scale scores

* `scale(data, names)` is the scale score per row, `items(data, names)` the items as
  numbers (a DataFrame for `corr()` or a reliability analysis). Name the items as in the
  questionnaire: a reverse-scored one is taken from its reversed copy by itself. Works on a
  frame (which carries its codebook) and on rows (pass `codebook=con.codebook(42)`).
* They read the numbers from the codebook. In a frame the items are Categoricals, whose
  codes are positions: a "Don't know" offered as sixth option was a 6.
* By default every item has to be answered; `min_items` allows gaps on purpose.

The tests now also compare the reversed copies with the platform's own export file of the
same study, value by value.

# sondavi 0.4.0

## Reverse-scored items

On the platform you can mark a matrix row as "Reverse-scored": an item worded the
other way round within a scale. The answer is stored as given, and the platform's
export files add a reversed copy `<name>_r` = (smallest + largest scale value) −
answer.

* `unnest()` now adds the same field, right after the item it mirrors: a number,
  computed from the answer as given, and None for an answer off the scale such as
  "don't know". Name and arithmetic are the platform's, so `b5.worry_r` means the same
  in an exported file and in data read through the API. It needs the codebook: pass
  `codebook=con.codebook(42)` with rows; a DataFrame from `frame()` brings its own.
* The codebook's variables carry `reverse_scored: {"min", "max"}` for a marked row.

This needs a platform that sends `reverse_scored` in the codebook; with an older one
there is simply no copy.

## unnest() takes a DataFrame, and labels the cells

`unnest(con.frame(42))` spreads the nested columns of a frame in place and gives the
new columns the codebook's treatment, as `frame()` does for the questions at the top
level: a matrix cell with value labels becomes a Categorical, a numeric one a number.
`frame()` keeps its codebook in `df.attrs["sondavi_codebook"]` for this;
`codebook=[]` leaves the cells as stored. `frame(42, unnest=True)` gives the same
result.

## Answers without a label are kept (changes existing behaviour)

A value the codebook has no label for — an answer to an option deleted after people
had chosen it, a code added later without a label — used to become NaN in `frame()`.
NaN reads as "not answered", so the answer was lost without notice. It is now kept as
a category of its own, named by the value, after the labelled ones, and a warning
names the column and the values.

A numeric variable holding something that is no number still has NaN there, but now
with a warning saying what was dropped.

## Fixed

* In `frame()`, three kinds of column came out entirely NaN: yes/no questions (the
  codebook writes `true`/`false`, the answers were compared as `True`/`False`),
  numerically coded questions with at least one missing answer (pandas makes such a
  column float, and `4.0` did not match `4`), and multiple-choice questions (a list
  compared as text). The first two now read as their labels; a multiple-choice answer
  stays a list, as in the R package.
* `unnest()` dropped nested fields whose name starts with `__`. They are part of the
  stored answer and are now kept, as the R package always did.

## On the platform's side

The rows now also carry the platform's internal fields under their own names
(`__locale`, `__randomization_order`, …), and a question named like one of the row's
own fields (`source`, `locale`, `completed_at`, …) arrives as `<name>_answer`. The
package passes both through unchanged. **Server-side change — your platform needs
updating for it.**

The features match the R package 0.4.0.

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
