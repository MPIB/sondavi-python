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

`unnest()` also takes a frame. `unnest(con.frame(42))` gives the new columns the
codebook too — a matrix cell with value labels becomes a Categorical, like the questions
beside it — and a matrix row marked "Reverse-scored" in the editor gets its reversed copy
right after it, as in the platform's export file: `b5.worry_r` = (smallest + largest scale
value) − answer, a number, empty for an answer off the scale such as "don't know". With
rows, pass the codebook for the copies: `unnest(rows, codebook=con.codebook(42))`.

A value the codebook has no label for (an answer to an option deleted later) is kept as a
category of its own, named by the value, with a warning — not turned into NaN.

## Scale scores

Name the items of a scale as they are called in the questionnaire; the package takes their
numbers from the codebook and uses the reversed copy for every item marked
"Reverse-scored", so you need not remember which ones are worded the other way round:

```python
from sondavi import items, scale

names = ["b5.calm", "b5.worry", "b5.rested"]                  # b5.worry is reverse-scored
df["b5_score"] = scale(df, names)                             # NaN unless all three are answered
df["b5_score"] = scale(df, names, min_items=2)                # allow one gap, on purpose
items(df, names)                                              # b5.calm, b5.worry_r, b5.rested
scores = scale(flat, names, codebook=con.codebook(42))        # rows work too
```

In a frame the items are Categoricals, and their codes are positions, not answers: on a
1–5 scale with "Don't know" as a sixth option, `.cat.codes + 1` makes "Don't know" a 6.
"Don't know" and other answers that are no number count as missing here; by default every
item has to be answered, so a mean over fewer items never happens unnoticed.

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

## AI interviews

An AI interview question (an interview conducted by a language model inside the questionnaire)
arrives as the whole conversation. `unnest()` writes the fields the CSV export and the codebook
have — `<question>.status` (how it ended), `.turns` (answers given), for an interview with a
list of questions `.answered` and `.skipped` (already covered by an earlier answer), and
`.summary` when the interview offers summaries. The conversation itself, one row per message:

```python
from sondavi import conversations

c = pd.DataFrame(conversations(rows))        # before unnest()
said = c[c.role == "respondent"]
said.groupby("response_id").text.apply(" ".join)   # all a person said
said.groupby("input").size()                        # typed or spoken
```

Each row has `question`, `message` (its place in the conversation), `role` (interviewer or
respondent), `answer` (the respondent's 1st, 2nd … answer), `text`, `input` (`"text"` or
`"voice"`) and `at` (when it was written, ISO 8601 in UTC); a flow interview's messages also
name the stage (`state`). `conversations()` takes a frame from `frame()` as well.

A study run entirely as an interview (the whole questionnaire as a chat) stores the answers
as usual, plus per question whether and how often the interviewer followed up and the
exchange about it as text: `unnest()` gives `interview_probing.<question>.was_probed`,
`.follow_up_count`, `.initial_response` and `.transcript`. The complete conversation of such
an interview, with recordings, is in the platform's interview-transcript export.

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
