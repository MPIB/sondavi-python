#!/usr/bin/env python3
"""Checks the package against the captured real API answers.

Run via tests/run.sh, which starts clients/fixtures/fixture-server.py first.
Plain asserts, no test framework — an analysis machine has none installed either."""

import json
import os
import sys
import time
import warnings
from datetime import datetime

# Windows' console is cp1252 by default, and the tick below is not in it: the run died with
# a UnicodeEncodeError on exactly one of the three CI systems, which hides the cause well.
# Printing ASCII would also work, but the output is read by people.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sondavi import ApiError, connect  # noqa: E402
import sondavi.snapshots  # noqa: E402,F401  (attaches the snapshot methods)
from sondavi import unnest, markings, conversations, items, scale  # noqa: E402
from sondavi.client import _with_status_categories  # noqa: E402

# The port comes from tests/run.py, which picks a free one: a fixed port is a bet on the
# machine, and CI runners carry their own listening services.
PORT = os.environ.get("SONDAVI_TEST_PORT", "8765")
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "sdv_" + "T" * 48
ERASED = "00000000-0000-4000-8000-000000000001"
NARROWED = "00000000-0000-4000-8000-000000000002"

checks = failures = 0


def ok(label, condition):
    global checks, failures
    checks += 1
    print(("  ✓ " if condition else "  ✗ ") + label)
    if not condition:
        failures += 1


def fails_with(label, fragment, fn):
    global checks, failures
    checks += 1
    try:
        fn()
        message, raised = "", False
    except Exception as err:            # noqa: BLE001 - the message is the assertion
        message, raised = str(err), True
    good = raised and fragment in message
    print(("  ✓ " if good else "  ✗ ") + label)
    if not good:
        failures += 1
        print("   got:", message or "(no error)")


print("\nConnecting")
fails_with("an empty token says where it belongs", "SONDAVI_TOKEN",
           lambda: connect(BASE, token=""))
ok("a trailing slash is harmless", connect(BASE + "/", TOKEN).base_url == BASE)

con = connect(BASE, TOKEN)

print("\nRefusals speak plainly")
fails_with("a bad token names the three indistinguishable cases", "expired or revoked",
           lambda: connect(BASE, "sdv_wrong").surveys())
fails_with("something out of scope does not guess", "not given access",
           lambda: con.responses(999))

print("\nListing and codebook")
surveys = con.surveys()
# Read from the recording, not written down: every re-recording creates a fresh study.
SURVEY = surveys[0]["id"]
ok("the studies arrive", len(surveys) == 4 and isinstance(SURVEY, int))
ok("with the privacy level", surveys[0]["privacy"] == "identified")

cb = con.codebook(SURVEY)
# Named, not counted: adding a question to the recorded study should not fail a test
# about the codebook arriving at all.
names = [v["name"] for v in cb]
ok("the codebook lists every variable",
   all(n in names for n in ("age", "mood", "why")) and "ratings.speed.score" in names)
mood = next(v for v in cb if v["name"] == "mood")
ok("and carries the value labels", len(mood["value_labels"]) == 3)
# The API names the range a marked row is mirrored in; the copy itself is the client's to make.
worry = next(v for v in cb if v["name"] == "b5.worry")
calm = next(v for v in cb if v["name"] == "b5.calm")
ok("a matrix row marked reverse-scored names its range", worry.get("reverse_scored") == {"min": 1, "max": 5})
ok("an unmarked row has no such field", "reverse_scored" not in calm)
ok("the reversed copy is not a variable of the API", "b5.worry_r" not in names)

print("\nResponses")
rows = con.responses(SURVEY)
ok("every page is walked", len(rows) == 3)
ok("no row twice", len({r["response_id"] for r in rows}) == 3)
ok("the granted identifier is there", "respondent_id" in rows[0])
ok("the ungranted one is not", "ip_address" not in rows[0])
ok("the platform's internal fields come under their own names",
   all(r["__locale"] == "default" for r in rows))
ok("max_rows stops early", len(con.responses(SURVEY, max_rows=1)) == 1)
ok("the fingerprint is kept", "digest" in (con.last_fingerprint or {}))

print("\nIterating, for studies too large to hold at once")
it = con.iter_responses(SURVEY, page_size=2)
ok("the iterator yields row by row", next(it)["response_id"] == rows[0]["response_id"])
ok("and can be exhausted", len(list(it)) == 2)

print("\nLimits")
start = time.monotonic()
con._request("GET", "surveys", {"flaky": 1})
ok("a 429 is waited out rather than hammered", time.monotonic() - start >= 1)

print("\nCitable datasets")
snap = con.snapshot(SURVEY, label="Paper, figure 2")
ok("a snapshot is cited by a uuid", len(snap.id) == 36)
ok("it records the size of the set", snap.recorded_rows == 3)
ok("and is complete when nothing changed", snap.complete)
ok("the citation line names the digest", "digest" in snap.citation())

replayed, intact = con.snapshot_responses(snap.id)
ok("replaying returns the recorded rows", len(replayed) == 3)
ok("and reports the set as intact", intact is True)

# The captured answer from AFTER one response was deleted — an erasure request
# (SPECS §34) or retention (§23) deletes them for real.
gone, still_intact = con.snapshot_responses(ERASED)
ok("a deleted response shrinks the replay", len(gone) == 2)
ok("and the set is no longer reported as intact", still_intact is False)

# A token reading fewer columns than the snapshot was recorded with. `matches_recorded`
# is null in that case and `bool(None)` would report a deletion that never happened —
# which is what the live test caught.
narrowed, narrowed_intact = con.snapshot_responses(NARROWED)
ok("a narrowed replay still returns every recorded row", len(narrowed) == 3)
ok("and is not mistaken for a deletion", narrowed_intact is True)

ok("recorded snapshots can be listed", len(con.snapshots()) >= 1)

print("\nThe fingerprint")
line = con.fingerprint()
ok("a line for the paper", "rows, fetched" in line and "digest" in line)

print("\nNested answers")
flat = unnest(rows)[0]
# The names must be the platform's own, or a script written against the export file and one
# written against the API disagree about what a variable is called.
ok("a matrix cell becomes question.row.column", "ratings.speed.score" in flat)
ok("with the recorded value", flat["ratings.speed.score"] == 4)
ok("a dynamic panel entry counts from zero, as the export does", "contacts.0.who" in flat)
ok("the nested field itself is gone", "ratings" not in flat)
ok("a multiple-choice answer stays one field", isinstance(rows[0].get("why"), (str, type(None))))
ok("naming one field leaves the others nested",
   isinstance(unnest(rows, columns=["ratings"])[0].get("contacts"), list))
cb_names = {v["name"] for v in cb}
produced = {k for k in flat if k.startswith(("ratings.", "contacts.", "map.", "visits."))}
ok("every flattened name appears in the codebook", produced <= cb_names)
ok("the internal fields pass through untouched", flat["__locale"] == "default")
# Raw is raw: the R package always kept these, this one dropped them.
kept = unnest([{"q": {"row": {"col": 1}, "__note": "x"}}])[0]
ok("a nested field starting with __ is kept", kept.get("q.__note") == "x" and kept.get("q.row.col") == 1)

print("\nReverse-scored items")
ok("without a codebook there is no reversed copy", "b5.worry_r" not in flat)
flat_r = unnest(rows, codebook=cb)
keys = list(flat_r[0])
ok("with one, the copy follows its item, as in the export file",
   keys[keys.index("b5.worry") + 1] == "b5.worry_r")
ok("min + max - answer: 2 on a 1-5 scale becomes 4", flat_r[0]["b5.worry_r"] == 4)
ok("an answer off the scale (dk) has no reversed value", flat_r[1]["b5.worry_r"] is None)
ok("the answer itself is untouched", flat_r[1]["b5.worry"] == "dk")
ok("an unmarked row gets no copy", "b5.calm_r" not in flat_r[0])
ok("a replayed snapshot is mirrored the same way", unnest(replayed, codebook=cb)[2]["b5.worry_r"] == 1)
ok("a reverse-scored row of a multi-select matrix gets its copy as well",
   flat_r[0]["ratings.clarity.score"] == 5 and flat_r[0]["ratings.clarity.score_r"] == 1)

print("\nThe reversed copies are the platform's own")
# The platform's "JSON (flat)" export of the same study, recorded next to the API answers
# (clients/fixtures-aufzeichnen.sh). The package builds `_r` itself from the codebook; this
# checks that it builds the same fields with the same values, rather than asserting it.
exported = json.loads((Path(__file__).parent / "fixtures" / "export_flat.json").read_text(encoding="utf-8"))
export_copies = {k for k in exported[0] if k.endswith("_r")}
# The snapshot replay holds all three recorded rows with their real ids.
built = {row["response_id"]: row for row in unnest(replayed, codebook=cb)}
ok("the export file has reversed copies to compare with", len(export_copies) >= 2)
ok("the package builds exactly the export's reversed fields",
   export_copies == {k for row in built.values() for k in row if k.endswith("_r")})
ok('with the export\'s values, row by row ("dk" empty in both)', all(
    (None if row[k] in ("", None) else float(row[k])) == built[row["response_id"]].get(k)
    for row in exported for k in export_copies))

print("\nScale scores")
scale_items = ["b5.calm", "b5.worry"]
table = items(flat_r, scale_items, codebook=cb)
ok("the items come as numbers, a reverse-scored one under its copy's name",
   list(table[0]) == ["b5.calm", "b5.worry_r"] and table[0] == {"b5.calm": 4, "b5.worry_r": 4})
ok('"dk" is no number', "b5.worry_r" in table[1] and table[1]["b5.worry_r"] is None)
ok("an answer without a label keeps its value (6 for a deleted option)", table[1].get("b5.calm") == 6)
ok("reverse=False gives the item as answered",
   items(flat_r, scale_items, codebook=cb, reverse=False)[0] == {"b5.calm": 4, "b5.worry": 2})
ok("the score is the mean of the items", scale(flat_r, scale_items, codebook=cb)[0] == 4)
ok("by default every item has to be answered", scale(flat_r, scale_items, codebook=cb)[1] is None)
ok("min_items allows gaps on purpose", scale(flat_r, scale_items, codebook=cb, min_items=1)[1] == 6)
fails_with("rows need the codebook passed", "codebook=con.codebook", lambda: items(flat_r, scale_items))
fails_with("an unknown field is named", "b5.nope", lambda: items(flat_r, ["b5.nope"], codebook=cb))

print("\nScale scores: the platform's rule, case by case")
# The cases of the platform's own scale score (its SPECS §75.6), made there into
# tests/fixtures/scale_cases.json: codebook and stored answers as the API gives them, and the score
# the questionnaire computes.
scale_cases = json.loads((Path(__file__).parent / "fixtures" / "scale_cases.json").read_text(encoding="utf-8"))["cases"]
for case in scale_cases:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        got = scale([case["data"]], case["items"], codebook=case["codebook"], min_items=case["min_items"])[0]
    want = case["expected"]
    ok(case["name"], (want is None and got is None) or (got is not None and want is not None and abs(got - want) < 1e-9))
noscale = next(c for c in scale_cases if "without a number scale" in c["name"])
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    items([noscale["data"]], noscale["items"], codebook=noscale["codebook"])
ok("a reverse-scored item with nothing to mirror against says so", any("m.r2" in str(w.message) for w in caught))

# The platform's rules, on answers the recording does not have.
synthetic_cb = [
    {"name": "q.a", "label": "Item A", "type": "categorical", "reverse_scored": {"min": 1, "max": 7},
     "value_labels": [{"value": str(i), "text": f"Level {i}"} for i in range(1, 8)]},
    {"name": "q.a_r", "label": "Another variable", "type": "numeric", "value_labels": []},
]
syn = unnest([{"q": {"a": 3, "a_r": 9}}, {"q": {"a": "7"}}, {"q": {"a": "0x1A"}}, {"q": {"a": "inf"}}],
             codebook=synthetic_cb)
ok("a taken name makes the copy <name>_r2", list(syn[0]) == ["q.a", "q.a_r2", "q.a_r"])
ok("and the variable that had the name keeps its value", syn[0]["q.a_r"] == 9)
ok("a number given as text is still a number", syn[1]["q.a_r2"] == 1)
# PHP's is_numeric, as on the platform; float() would read "inf".
ok("hexadecimal and inf are not", syn[2]["q.a_r2"] is None and syn[3]["q.a_r2"] is None)

print("\nImage marking")
# The API sends the stored answer (image, grid, cells or pins); unnest has to write it the way
# the export does — one field per marking type — not as a tree of image.src, grid.cols, …
flat_all = unnest(rows)
ok("an area answer becomes one field per marking type", {"map.green", "map.red"} <= set(flat))
ok("none of the stored structure leaks out as fields",
   not any(k.startswith(("map.mode", "map.image", "map.grid", "map.cells", "visits.points")) for k in flat))
ok("painted cells are written as row runs, like the export", flat["map.green"] == "2:3-5 3:4")
ok("a single cell has no dash", flat["map.red"] == "6:4")
ok("a type nobody used in this answer is absent", "map.red" not in flat_all[1])
ok("pins are x,y pairs in the order set", flat["visits.visit"] == "0.25,0.5 0.7,0.1234")
ok("someone who set no pins has no pin field", "visits.visit" not in flat_all[1])

m = markings(rows)
ok("the long table has one row per cell and per pin", len(m) == 4 + 1 + 1 + 2)
ok("with the fields of the export's markings.csv", list(m[0]) == [
    "response_id", "respondent_id", "completed_at", "question", "category",
    "row", "col", "x_norm", "y_norm", "x_px", "y_px", "image_src"])
cell = next(r for r in m if r["question"] == "map" and r["category"] == "green")
ok("a cell is named by row and column, counted from 0", (cell["row"], cell["col"]) == (2, 3))
# The centre of cell (2, 3) on a 16 x 11 grid over 1600 x 1100 px.
ok("placed at its centre, relative to the image",
   abs(cell["x_norm"] - 3.5 / 16) < 1e-6 and abs(cell["y_norm"] - 2.5 / 11) < 1e-6)
ok("and in pixels of the original image", (cell["x_px"], cell["y_px"]) == (350, 250))
pin = [r for r in m if r["question"] == "visits"][1]
ok("a pin has no cell", pin["row"] is None and pin["col"] is None)
ok("but both coordinates", pin["x_norm"] == 0.7 and pin["y_px"] == round(0.1234 * 1100, 2))
ok("each row says which image it was drawn on", all(r["image_src"] == "/storage/test/TEST-map.png" for r in m))
ok("and whose answer it is", all(r["respondent_id"] == "PNL-1" for r in m if r["question"] == "visits"))
ok("one question can be asked for", {r["question"] for r in markings(rows, columns=["visits"])} == {"visits"})
ok("a study without image marking gives an empty table",
   markings([{k: v for k, v in r.items() if k not in ("map", "visits")} for r in rows]) == [])

print("\nAI interviews")
# An interview question arrives as the whole conversation (mode, status, turns, messages, ...).
# unnest writes the export's columns - the ones the codebook lists - not a field per message;
# conversations() is the conversation as a long table. The study is a real recording.
INTERVIEW = surveys[2]["id"]
irows = con.responses(INTERVIEW)
iflat = unnest(irows)
ok("an interview question becomes the export's fields",
   {"talk.status", "talk.turns", "talk.answered", "talk.skipped", "talk.summary", "chat.status", "chat.turns"} <= set(iflat[0]))
ok("and nothing of the stored conversation leaks out as fields",
   not any(k.startswith(("talk.mode", "talk.messages", "talk.questionStatus", "talk.microNarrative", "chat.messages"))
           for r in iflat for k in r))
icodebook = con.codebook(INTERVIEW)
ok("every field the codebook names for it is there",
   {v["name"] for v in icodebook if v["name"].startswith(("talk.", "chat."))} <= {k for r in iflat for k in r})
ok("a free interview has no counts of questions", not any("chat.answered" in r or "chat.skipped" in r for r in iflat))
ok("how it ended, the turns, the question counts", (iflat[0]["talk.status"], iflat[0]["talk.turns"], iflat[0]["talk.answered"],
   iflat[0]["talk.skipped"]) == ("completed", 4, 2, 0) and (iflat[1]["talk.status"], iflat[1]["talk.turns"], iflat[1]["talk.answered"]) == ("aborted", 1, 1))
ok("the chosen summary is there, None where none was chosen",
   iflat[0]["talk.summary"].startswith("I write software") and iflat[1]["talk.summary"] is None)
ok("the whole-survey interview's probing stays readable",
   iflat[2]["interview_probing.job.was_probed"] is True and iflat[2]["interview_probing.job.transcript"].startswith("I: What is your job"))

talk = conversations(irows)
ok("the conversations: one row per message", len(talk) == 9 + 7 + 2)
ok("with their fields", list(talk[0]) == ["response_id", "respondent_id", "completed_at", "question", "mode",
                                          "message", "role", "answer", "text", "input", "at", "state"])
one = [r for r in talk if r["question"] == "talk" and r["respondent_id"] == "PNL-1"]
ok("in the order of the conversation, interviewer first", [r["message"] for r in one] == list(range(1, 10)) and one[0]["role"] == "interviewer")
ok("the respondent's answers are counted", [r["answer"] for r in one if r["role"] == "respondent"] == [1, 2, 3, 4]
   and all(r["answer"] is None for r in one if r["role"] == "interviewer"))
ok("an answer says whether it was typed or spoken",
   [r["input"] for r in talk if r["question"] == "chat" and r["role"] == "respondent"] == ["text", "voice", "text"])
ok("each message has its time", all(r["at"] and r["at"].endswith("Z") for r in talk))
ok("and its words", one[1]["text"].startswith("TEST I write software"))
ok("an interview that ended early keeps what was said", sum(r["respondent_id"] == "PNL-2" for r in talk) == 2)
ok("one question can be asked for", {r["question"] for r in conversations(irows, columns=["chat"])} == {"chat"})
# What the recording does not have: a flow interview, a summary in a free one, a blank choice.
flow = {"mode": "flow", "status": "active", "turns": 1, "statePath": [{"state": "intro", "via": "start", "turn": 0}], "messages": [
    {"role": "assistant", "text": "Hi", "at": "2026-10-15T20:00:00.000000Z", "state": "intro"},
    {"role": "user", "text": "Hello", "at": "2026-10-15T20:00:05.500000Z", "input": "voice"}]}
free = {"mode": "free", "status": "completed", "turns": 2, "messages": [],
        "microNarrative": {"status": "chosen", "options": ["  "], "chosenIndex": 0, "chosen": "  "}}
own = unnest([{"response_id": 1, "q": flow}, {"response_id": 2, "q": free}])
ok("a flow interview has no counts of questions and no summary",
   own[0] == {"response_id": 1, "q.status": "active", "q.turns": 1})
ok("a free interview offering summaries has the summary field; a blank choice is none",
   own[1] == {"response_id": 2, "q.status": "completed", "q.turns": 2, "q.summary": None})
covered = {"mode": "state", "status": "completed", "turns": 1, "messages": [], "questionStatus": [
    {"id": "a", "status": "answered"}, {"id": "b", "status": "skipped_anticipated"}, {"id": "c", "status": "pending"}]}
cf = unnest([{"response_id": 3, "q": covered}])[0]
ok("a question already covered by an earlier answer counts as skipped, an open one as neither",
   (cf["q.answered"], cf["q.skipped"]) == (1, 1))
oc = conversations([{"response_id": 1, "q": flow}, {"response_id": 2, "q": free}])
ok("a flow interview's messages say in which stage the interviewer was", [r["state"] for r in oc] == ["intro", None])
ok("an interview without messages contributes no rows", {r["response_id"] for r in oc} == {1})
ok("rows without respondent_id give none", "respondent_id" not in oc[0])
ok("a study without interview questions gives an empty table", conversations(rows) == [])

print("\nScreened out, and a bot check as a question")
# The fourth study screens people under 18 out and asks a bot check on its second page
# (SPECS §79, §80): PNL-2 was screened out, PNL-3 failed the check, which only flags.
SCREENING = surveys[3]["id"]
srows = con.responses(SCREENING)
ok("everyone is a row, the screened-out person too", len(srows) == 3)
ok("the screened-out row has no completed_at", [r["completed_at"] is None for r in srows] == [False, True, False])
ok("its time of screen-out is a timestamp, like completed_at",
   isinstance(srows[1]["screened_out_at"], datetime) and srows[0]["screened_out_at"] is None)
ok("and its reason is there", [r["screen_out_reason"] for r in srows] == [None, "under 18", None])
ok("every row says what became of it", [r["response_status"] for r in srows] == ["complete", "screened_out", "complete"])
ok("the bot check question reads as passed / failed", [r.get("human") for r in srows] == ["passed", None, "failed"])
ok("the overall bot check result is a yes/no field", [r["bot_check_passed"] for r in srows] == [True, None, False])
try:
    import pandas as pd
    sframe = con.frame(SCREENING)
    ok("in a data frame the bot check question carries its labels",
       [None if pd.isna(v) else v for v in sframe["human"]] == ["Passed", None, "Failed"])
    ok("and the time of screen-out is a datetime column", str(sframe["screened_out_at"].dtype).startswith("datetime64"))
    ok("the status is a Categorical with all three categories, in their order",
       str(sframe["response_status"].dtype) == "category"
       and list(sframe["response_status"].cat.categories) == ["complete", "partial", "screened_out"]
       and list(sframe["response_status"]) == ["complete", "screened_out", "complete"])
    ok("so a count names the empty category too", sframe["response_status"].value_counts(sort=False).tolist() == [2, 0, 1]
       if str(sframe["response_status"].dtype) == "category" else False)
    with warnings.catch_warnings(record=True) as said:
        warnings.simplefilter("always")
        odd = _with_status_categories(pd.DataFrame({"response_status": ["complete", "quota_full"]}))
    ok("a status this version does not know stays, as a category of its own, with a warning",
       list(odd["response_status"]) == ["complete", "quota_full"] and any("quota_full" in str(w.message) for w in said))
except (ImportError, RuntimeError) as err:
    print("  (skipped:", err, ")")

print("\nJoining waves")
waves = con.waves([s["id"] for s in surveys[:2]], names=["w1", "w2"])
ok("one row per person seen in any wave", len(waves) == 3)
ok("the fields carry their wave", all("mood_w1" in w for w in waves))
ok("the join key is not suffixed", all("respondent_id" in w for w in waves))
# The point of the full outer join: attrition is usually the thing being studied, so the
# person who stopped answering must not quietly disappear.
dropped = [w for w in waves if "mood_w2" not in w]
ok("someone who skipped the second wave is kept", len(dropped) == 1)
ok("and is recognisable by their missing wave", dropped[0]["respondent_id"] == "PNL-3")
fails_with("one study is not a series", "at least two",
           lambda: con.waves([surveys[0]["id"]]))

print("\nPandas")
try:
    df = con.frame(SURVEY)
    ok("the frame has one row per response", len(df) == 3)
    ok("a categorical question becomes a Categorical", str(df["mood"].dtype) == "category")
    ok("with the real labels", list(df["mood"].cat.categories) == ["Bad", "Neutral", "Good"])
    ok("and the first answer reads as its label", df["mood"].iloc[0] == "Good")
    # As text these sort lexically and compare against a Timestamp without complaining.
    ok("completed_at is a timestamp, not text",
       str(df["completed_at"].dtype).startswith("datetime64"))
    ok("carrying the platform's time zone", df["completed_at"].dt.tz is not None)
    ok("so a duration can simply be computed",
       (df["completed_at"] - df["started_at"]).dt.total_seconds().gt(0).all())
    ok("the codebook is kept with the frame", [v["name"] for v in df.attrs["sondavi_codebook"]] == names)
    ok("the internal fields are columns of their own", (df["__locale"] == "default").all())

    import pandas as pd
    from sondavi.client import _apply_codebook  # noqa: E402 - the top-level rules, synthetic cases

    print("\nMatrix cells in a frame get the codebook too")
    # The recording holds an answer whose option was deleted afterwards; what unnest says about
    # it is checked below.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fu = unnest(df)
    ok("unnest takes the frame", "b5.worry" in fu.columns and "b5" not in fu.columns)
    ok("and leaves the frame it was given alone", "b5" in df.columns)
    ok("a matrix cell with value labels becomes a Categorical", str(fu["b5.worry"].dtype) == "category")
    ok("reading as its label", fu["b5.worry"].iloc[0] == "A little")
    ok("an off-scale option keeps its own label", fu["b5.worry"].iloc[1] == "Don't know")
    columns = list(fu.columns)
    ok("the reversed copy follows its item", columns[columns.index("b5.worry") + 1] == "b5.worry_r")
    # Computed from the answer as given, before the cell became a Categorical.
    ok("as a number: 2 on a 1-5 scale becomes 4",
       str(fu["b5.worry_r"].dtype).startswith(("float", "int")) and fu["b5.worry_r"].iloc[0] == 4)
    ok("dk has no reversed value", pd.isna(fu["b5.worry_r"].iloc[1]))
    ok("an unmarked row gets no copy", "b5.calm_r" not in fu.columns)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ok("frame(unnest=True) gives the same frame", con.frame(SURVEY, unnest=True).equals(fu))
    dfi = con.frame(INTERVIEW)
    fi = unnest(dfi)
    ok("an interview's ending is a Categorical with the codebook's labels",
       str(fi["talk.status"].dtype) == "category" and fi["talk.status"].iloc[1] == "Ended early after technical problems")
    said = lambda table: [(r["question"], r["message"], r["role"], r["answer"], r["text"], r["input"]) for r in table]
    ok("conversations() takes the frame as well", said(conversations(dfi)) == said(talk))
    print("\nScale scores from a frame")
    # The category codes are positions: "Don't know", the sixth option, is code 5 — 6 counted
    # from one. items() reads the numbers from the codebook instead.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ft = items(fu, scale_items)
    ok("the items come as numbers", list(ft.columns) == ["b5.calm", "b5.worry_r"]
       and all(str(t).startswith("float") for t in ft.dtypes))
    ok('"Don\'t know" is no number, not 6',
       fu["b5.worry"].cat.codes.iloc[1] + 1 == 6 and pd.isna(ft["b5.worry_r"].iloc[1]))
    ok("the same numbers as from the rows", ft.iloc[0].to_dict() == {"b5.calm": 4.0, "b5.worry_r": 4.0})
    # Row 999 is the fixture server's replay of the first one (its second page); filtered out.
    ok("a frame carries its codebook, also when filtered",
       list(scale(fu[fu["response_id"] != 999], scale_items, min_items=1)) == [4.0, 6.0])
    ok("the score is a Series on the frame's index", scale(fu, scale_items).index.equals(fu.index))
    raw_cells = unnest(df, codebook=[])
    ok("codebook=[] leaves the cells as stored",
       list(raw_cells["b5.worry"]) == [2, "dk", 2] and "b5.worry_r" not in raw_cells.columns)

    print("\nAnswers without a value label")
    # Mapped against the labels alone they became NaN, which reads as "not answered".
    calm_warnings = [str(w.message) for w in caught if "b5.calm" in str(w.message)]
    ok("are kept, named by their value", fu["b5.calm"].iloc[1] == "6")
    ok("as a category after the labelled ones", list(fu["b5.calm"].cat.categories) == [
        "Not at all", "A little", "Somewhat", "Very", "Completely", "Don't know", "6"])
    ok("the analyst is warned once for the column, with the value",
       len(calm_warnings) == 1 and '"6"' in calm_warnings[0])
    ok("a column whose answers all have labels is not mentioned",
       not any("b5.worry" in str(w.message) for w in caught))

    # Two cases where every answer of a column used to become NaN: a yes/no answer compared
    # as "True" with the codebook's "true", and a numeric code in a column pandas made float
    # because one answer is missing ("4.0" against "4"). And a multiple-choice answer, which
    # matched as text became an empty Categorical.
    with warnings.catch_warnings(record=True) as caught_top:
        warnings.simplefilter("always")
        top = _apply_codebook(pd.DataFrame({
            "consent": [True, False, None], "code": [4, None, 2], "multi": [["a"], ["a", "b"], None],
            "q.a": [2, None, 5], "q.a_r": ["9", "dk", None],
        }), [
            {"name": "consent", "value_labels": [{"value": "true", "text": "Yes"}, {"value": "false", "text": "No"}]},
            {"name": "code", "value_labels": [{"value": "2", "text": "Two"}, {"value": "4", "text": "Four"}]},
            {"name": "multi", "value_labels": [{"value": "a", "text": "A"}, {"value": "b", "text": "B"}]},
            *synthetic_cb,
        ])
    ok("a yes/no answer reads as its label", list(top["consent"].iloc[:2]) == ["Yes", "No"])
    ok("a numeric code in a column with a missing answer finds its label",
       list(top["code"].iloc[[0, 2]]) == ["Four", "Two"])
    ok("a multiple-choice answer stays a list", top["multi"].iloc[1] == ["a", "b"])
    columns = list(top.columns)
    ok("a reverse-scored column at the top level gets its copy as well",
       columns[columns.index("q.a") + 1] == "q.a_r2" and list(top["q.a_r2"].iloc[[0, 2]]) == [6, 3])
    ok("a numeric variable says which answers are no numbers",
       any("`q.a_r`" in str(w.message) and '"dk"' in str(w.message) for w in caught_top))
    ok("and nothing else is warned about", len(caught_top) == 1)
except RuntimeError as err:
    print("  (skipped:", err, ")")

print(f"\n{checks - failures}/{checks} checks passed")
sys.exit(1 if failures else 0)
