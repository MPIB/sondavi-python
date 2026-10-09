#!/usr/bin/env python3
"""Checks the package against the captured real API answers.

Run via tests/run.sh, which starts clients/fixtures/fixture-server.py first.
Plain asserts, no test framework — an analysis machine has none installed either."""

import os
import sys
import time
import warnings

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
from sondavi import unnest, markings  # noqa: E402

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
ok("the studies arrive", len(surveys) == 2 and isinstance(SURVEY, int))
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
