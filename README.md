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

`completed_at` and `started_at` (and `screened_out_at`, in a study that screens people out)
arrive as real timestamps in the platform's time zone, and
`frame()` applies the codebook — a categorical question becomes a pandas `Categorical` with
its real labels rather than bare codes.

These are the same rows as the platform's JSON export, which means **partial responses are
included** when the study saves them: someone who stopped halfway is a row whose
`completed_at` is empty. So is someone the study screened out; their row carries
`screened_out_at` and `screen_out_reason` instead. `len(rows)` is therefore not the number
of completed participations. Every row says what became of it in `response_status` —
`"complete"`, `"partial"` or `"screened_out"`:

```python
done = [r for r in rows if r["response_status"] == "complete"]
```

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

A scale score the questionnaire computes itself (Σ in the editor, with subscales if the matrix
rows have them) is stored with each response as it was computed then. To work it out again — for
responses recorded before you added or changed it — name it; the codebook supplies its rows,
mean or sum, and its minimum. `method="sum"` adds instead of averaging when you name the items
yourself:

```python
df["sdq_emo_now"] = scale(df, score="sdq_emo")                # the emotional subscale, summed
scale(flat, ["sdq.e1", "sdq.e2"], codebook=cb, method="sum")   # the same by hand
```

`score=` needs a platform that describes its scale scores in the codebook (version 0.106 or later).

## Questions with several answers

A multiple-choice answer, a tag box or a ranking arrives as the list of options chosen and stays
one field. `unnest()` adds one field per option right after it, as the export files have them —
1 chosen, 0 answered without it, None (NA in a frame) not answered; for a ranking the option's
place. `options(rows, codebook=cb)` does the same without unnesting anything else.

```python
flat = unnest(con.frame(42))
flat["devices.phone"].value_counts()
```

## Uploaded files

A "File Upload" answer in the responses is the file's name, type, size and a reference; the
files themselves come with `files()`:

```python
files = con.files(42)                  # unpacked into sondavi-files-42/
drawings = [f for f in files if f["question"] == "drawing"]
drawings[0]["local_path"]
```

A folder per question, the files named by response (`drawing/1234-1.png`), never by the name
a participant gave them — so the drawings of one task can be rated without seeing whose they
are. Join to the responses over `response_id`.

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

## Verifying a certified dataset

A certified dataset is a CSV file the platform issued together with a `.certificate.json`.
The certificate is signed by the platform, not by whoever shares the file, so a reviewer, a
journal or a co-author can check that every certified cell is what participants entered —
without an account or a token:

```python
import sondavi

result = sondavi.verify("study.csv", "study.certificate.json")
print(result)
#> Valid: every certified cell matches.
#>   Survey:          Wellbeing at work (version 3 of the questionnaire, privacy: pseudonymized)
#>   Organisation:    …
#>   Removed columns: respondent_id
#>   …
if not result:                       # true only when valid
    print(result.status, result.problems)
```

Pass the **files**, not a DataFrame: reading a file into pandas has already converted the
cells — "007" became 7, "NA" became missing — and the certificate is over the text as it
stands in the file. The file is read strictly, exactly as the platform reads it: UTF-8, quotes
as in RFC 4180, and every row exactly as wide as the header. A file that two programs could read
differently — a stray quote, a lone carriage return, a NUL, a row with more fields than the
header — is `unverifiable`.

Columns the platform computes when it writes the file — attention checks, quality scores, the
reversed `_r` copies — are in the file but not certified: they follow the questionnaire as it was
that day. The summary lists them as computed, and it names the values the participant's browser
reported rather than the platform measured (start time and duration, for instance).

`result.status` is `valid`, `modified` (the certificate is genuine, but cells differ:
`result.modified_cells` names each by data row, counted from 1 below the header, and column),
`unverifiable` (the file does not fit the certificate, e.g. a certified column is missing;
`result.problems` says what), `invalid_signature` (the certificate itself was altered),
`unknown_key`, `untrusted_issuer` or `malformed`. `removed_columns`, `removed_rows` and
`added_columns` say what was left out and what is in the file without being certified —
appended columns are allowed, and so is a removed column put back, but neither counts as
certified. When cells differ in several rows *and* several columns, `suspect` is
True: the certificate holds one sum per row and one per column, so the listed cells are the
candidates, not each of them changed.

**Whom to trust is your decision.** Only `https://sondavi.io` counts by default; a
certificate from another installation of the platform reports `untrusted_issuer` until you
name it, `verify(..., trust=["https://surveys.example.org"])` — otherwise anyone with a key
and an address of their own could issue "genuine" certificates.

**The keys.** The package carries the public keys of `https://sondavi.io` that were published
when its version was made, and a certificate signed with one of them is checked without a
connection. Any other key — a newer one, or another installation's — is fetched from
`<issuer>/.well-known/sondavi-certificate-keys.json`. To check offline in every case, save
that file once and pass `keys="sondavi-keys.json"`; keys you pass are then the only ones used.
A key counts only under its own id, `ed25519:` and the first 16 hex digits of its SHA-256.

A certificate can also cover **a file you already had** — your own CSV, Excel or JSON export,
which the platform compared with the stored responses, or with a file it certified earlier,
before signing it. Check it the same way, whatever its format:

```python
result = sondavi.verify("cleaned.xlsx", "cleaned.certificate.json")
print(result)
#> Valid: this is the file that was certified, unchanged to the last byte.
#>   …
#>   Read as:             Excel, sheet "Data"
#>   Rows matched:        by response ID
#>   In the file:         412 of 430 responses of this survey
#>   Left out:            18 responses: 15 incomplete, 3 screened out
#>   Own columns:         my_rating (not checked)
#>   Written differently: q_num (12 cells) - the same value in another notation
result.basis                         # everything the comparison found, as a dict
```

Such a certificate is over the file's bytes, not over single cells: the file is valid exactly
when it is the file that was certified, and otherwise reported as modified without naming
cells.

**To share fewer columns or rows** — a pseudonym, or people who did not agree to their answers
being shared — or the data in another format, certify the changed file on the platform that
issued the certificate ("Certify a file you have"). The platform compares it with the certified
file, so this works even after the responses were deleted there; the new certificate says which
columns and responses are left out, and its summary names the certificate it was derived from.
Deleting columns or rows by hand, or **opening the file in a spreadsheet program and saving it
again** — which rewrites numbers and dates, "007" becomes "7", "-0" becomes "0" — makes the
certificate fail. Keep the file as it came, and work on a copy.

All of this runs on the standard library, the Ed25519 signature check included.

## Tests

```
python tests/run.py
```

Replays **real answers captured from the platform** (`tests/fixtures/`), including the
one recorded after a response was erased. No network, no credentials.

The checks of certified datasets need no fixture server; `tests/run.py` runs them too, and
they run on their own with `python tests/test_certificates.py` (or
`python -m unittest tests/test_certificates.py`). They read certificates the platform itself
issued (`tests/fixtures/certificates/`).
