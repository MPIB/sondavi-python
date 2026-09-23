#!/usr/bin/env python3
"""Checks the package against the captured real API answers.

Run via tests/run.sh, which starts clients/fixtures/fixture-server.py first.
Plain asserts, no test framework — an analysis machine has none installed either."""

import os
import sys
import time

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

print("\nResponses")
rows = con.responses(SURVEY)
ok("every page is walked", len(rows) == 3)
ok("no row twice", len({r["response_id"] for r in rows}) == 3)
ok("the granted identifier is there", "respondent_id" in rows[0])
ok("the ungranted one is not", "ip_address" not in rows[0])
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
except RuntimeError as err:
    print("  (skipped:", err, ")")

print(f"\n{checks - failures}/{checks} checks passed")
sys.exit(1 if failures else 0)
