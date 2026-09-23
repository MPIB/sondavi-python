# sondavi (Python)

Read your own study's data from the Sondavi survey platform, instead of exporting a file first.

```python
from sondavi import connect
import sondavi.snapshots          # adds snapshot() / snapshots() / snapshot_responses()

con = connect("https://survey.example.org")   # token from SONDAVI_TOKEN

con.surveys()
rows = con.responses(42)                      # every page, as dicts
df   = con.frame(42)                          # pandas, codebook applied
```

Standard library only; `pandas` is optional and used by `frame()` alone.

`completed_at` and `started_at` arrive as real timestamps in the platform's time zone, and
`frame()` applies the codebook — a categorical question becomes a pandas `Categorical` with
its real labels rather than bare codes.

These are the same rows as the platform's JSON export, which means **partial responses are
included** when the study saves them: someone who stopped halfway is a row whose
`completed_at` is empty. `len(rows)` is therefore not the number of completed
participations.

Nested answers — matrices, dynamic panels — stay nested, as the platform sends them.
`unnest()` spreads them into one field each, under exactly the names the platform's own
export writes:

```python
from sondavi import unnest

flat = unnest(rows)
flat[0].keys()
#> … "ratings.speed.score", "contacts.0.who"          counting from zero, as the export does

df = con.frame(42, unnest=True)                      # or straight into pandas
```

The codebook is applied by `frame()`, not by `responses()`: a dict has no notion of a
factor, and replacing a code with its label would throw the code away. `codebook(42)`
gives you the mapping if you want it yourself.

## Image marking

An image marking question (participants paint areas or set pins on a map or picture) arrives
as the stored answer — the image, the grid, and the cells or pins. For a heatmap:

```python
import pandas as pd
from sondavi import markings

m = pd.DataFrame(markings(rows))
m[(m.question == "map") & (m.category == "green")].groupby(["row", "col"]).size()
```

One row per painted cell or pin, with its position between 0 and 1 (`x_norm`, `y_norm`) and in
pixels of the original image (`x_px`, `y_px`) — the same table as the platform's
image-markings export. `unnest()` writes these questions the way the CSV export does: one field
per marking type, cells as row runs (`"2:3-5 3:4"`), pins as `"x,y"` pairs.

## Waves of a study series

```python
d = con.waves([42, 43], names=["baseline", "followup"])
returned = sum("mood_followup" in row for row in d)
```

Fields are suffixed per wave, and **everyone seen in any wave is kept** — a person who
did not take the follow-up is simply missing those fields. Attrition is usually what a
longitudinal design is about, so an inner join would drop exactly the cases you want to
describe.

## The token

Create one under your account, then export it:

```
SONDAVI_TOKEN=sdv_…
```

**Not in the script.** A token written into an analysis travels with it into version
control, onto shared drives and into supplementary material.

## A live query is not a dataset

Run the same script tomorrow and it may return different rows. Either record the
fingerprint next to your result:

```python
con.fingerprint()
#> 318 rows, fetched 2026-09-23T09:32:29+00:00, digest 332f3d2a1fbfd880
```

or record what your result was computed from:

```python
snap = con.snapshot(42, label="Paper, figure 2")
print(snap.citation())
#> snapshot 8869174a-… (survey 42, 318 responses, recorded 2026-09-23T09:32:29+00:00, digest f93b7e05e30d0786)

rows, intact = con.snapshot_responses(snap.id)
if not intact:
    print(f"{snap.missing_rows} responses have been deleted since.")
```

A snapshot records *which* responses belonged to the set, never a copy of them — so
retention and the right to erasure keep working. If rows have gone since, you are told,
rather than handed a smaller set as if nothing had happened.

## Tests

```
python tests/run.py
```

Replays **real answers captured from the platform** (`tests/fixtures/`), including the
one recorded after a response was erased. No network, no credentials.
