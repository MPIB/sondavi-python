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

Nested answers — matrices, dynamic panels — stay nested, as the platform sends them. The R
package can spread them into one column each (`sondavi_unnest()`) and join the waves of a
study series; this one cannot yet.

## The token

Create one under your account, then export it:

```
SONDAVI_TOKEN=sdv_…
```

**Not in the script.** A token written into an analysis travels with it into version
control, onto shared drives and into supplementary material.

## A live query is not a dataset

Record what your result was computed from:

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
tests/run.sh
```

Replays **real answers captured from the platform** (`tests/fixtures/`), including the
one recorded after a response was erased. No network, no credentials.
