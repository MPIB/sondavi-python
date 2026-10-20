#!/usr/bin/env python3
"""Certified datasets: the package against certificates the platform itself issued.

Offline - no fixture server, no network. tests/fixtures/certificates/ are copies of the
platform's own fixture set: a certificate issued by the platform (with ip_address left out),
the platform's redaction of it, a file with one changed cell, a file with a row longer than its
header, a certificate of the whole file, a smaller file certified from that certificate, the
test issuer's keys, how every verifier reads a CSV (csv-cases.json) and the results every
verifier must produce. Matching them is the point: a file must read the same
here, in R and on the platform's /verify page.

    python tests/test_certificates.py            (tests/run.py runs it too)
    python -m unittest tests/test_certificates.py
"""

import base64
import copy
import hashlib
import re
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

# Windows' console is cp1252 by default; a column name with an umlaut in a failure message
# would otherwise turn a failing test into a UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import sondavi  # noqa: E402
from sondavi import Verification, verify  # noqa: E402
from sondavi import _ed25519  # noqa: E402
from sondavi import certificates  # noqa: E402
from sondavi.certificates import _NotWellFormed, _read_csv, _redact, _write_csv  # noqa: E402

FIXTURES = HERE / "fixtures" / "certificates"
# Inside the platform's repository the originals sit four levels up; in the published
# package they do not (and a checkout may not even be that deep), so the comparison is skipped.
PLATFORM_FIXTURES = (HERE.parents[3] / "tests" / "fixtures" / "certificates") if len(HERE.parents) > 3 else None
FIXTURE_FILES = ["dataset.csv", "dataset.certificate.json", "redacted.csv", "redacted.certificate.json",
                 "tampered.csv", "file.certificate.json", "derived.csv", "derived.certificate.json",
                 "too_long.csv", "csv-cases.json", "keys.json", "expected.json"]

KEYS = FIXTURES / "keys.json"
ISSUER = "https://certificates.example"
TRUST = (ISSUER,)
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
CSV_CASES = json.loads((FIXTURES / "csv-cases.json").read_text(encoding="utf-8"))["cases"]
NOT_WELL_FORMED = ("unverifiable", ["The data file is not a well-formed CSV file."])


def certificate(name: str = "dataset") -> dict:
    return json.loads((FIXTURES / f"{name}.certificate.json").read_text(encoding="utf-8"))


def check(data, cert="dataset", **kwargs) -> Verification:
    kwargs.setdefault("trust", TRUST)
    kwargs.setdefault("keys", KEYS)
    if cert in ("dataset", "redacted", "file", "derived"):
        cert = FIXTURES / f"{cert}.certificate.json"
    return verify(data, cert, **kwargs)


def no_network(*_args, **_kwargs):
    raise AssertionError("the check must not go to the network here")


# The platform's test issuer signs with the seed 0x01 * 32 (its CertificateFixtures). Signing
# lives here, in the tests, and nowhere in the package: it lets a test build a certificate
# whose signed manifest is broken in a particular way. RFC 8032, 5.1.6.
TEST_SEED = b"\x01" * 32


def _encode(point) -> bytes:
    x, y, z, _ = point
    inverse = pow(z, _ed25519._P - 2, _ed25519._P)
    x, y = x * inverse % _ed25519._P, y * inverse % _ed25519._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def sign(message: bytes, seed: bytes = TEST_SEED) -> bytes:
    digest = hashlib.sha512(seed).digest()
    a = int.from_bytes(digest[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    public = _encode(_ed25519._multiply(a, _ed25519._BASE))
    r = int.from_bytes(hashlib.sha512(digest[32:] + message).digest(), "little") % _ed25519._L
    big_r = _encode(_ed25519._multiply(r, _ed25519._BASE))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % _ed25519._L
    return big_r + ((r + k * a) % _ed25519._L).to_bytes(32, "little")


def resigned(change, name: str = "dataset") -> dict:
    """A fixture certificate with `change(manifest)` applied to its manifest, signed again."""
    cert = certificate(name)
    manifest = json.loads(cert["manifest"])
    change(manifest)
    cert["manifest"] = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    cert["signature"] = base64.b64encode(sign(cert["manifest"].encode("utf-8"))).decode()
    return cert


class WorkDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sondavi-cert-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rewritten(self, change, source: str = "dataset.csv", name: str = "data.csv") -> Path:
        """The fixture file, changed by `change(header, rows)` and written out again."""
        header, rows = _read_csv(FIXTURES / source)
        header, rows = change(header, [list(r) for r in rows])
        path = self.dir / name
        _write_csv(path, header, rows)
        return path


class TheFixtureSet(unittest.TestCase):
    """What the platform issued reads exactly as the platform says it does."""

    def test_the_issued_file_is_valid_and_names_the_column_left_out_at_issue(self):
        result = check(FIXTURES / "dataset.csv")
        for key, value in EXPECTED["dataset"].items():
            self.assertEqual(getattr(result, key), value, key)
        self.assertTrue(result.valid)
        self.assertTrue(result)
        self.assertEqual(result.modified_cells, [])
        self.assertEqual(result.problems, [])
        self.assertEqual(result.certificate, "00000000-0000-4000-8000-000000000086")
        self.assertEqual(result.issuer, {"name": "Sondavi (test)", "url": ISSUER})
        self.assertEqual(result.organization, "Test Organisation")
        self.assertEqual(result.survey["title"], "TEST Certified")
        self.assertEqual(result.issued_at, "2026-10-20T08:00:00Z")

        text = str(result)
        self.assertIn("every certified cell matches", text)
        for part in ("TEST Certified", "Test Organisation", "Sondavi (test)", ISSUER,
                     "2026-10-20T08:00:00Z", "Missing from file: 1 - left out when the certificate was issued: ip_address",
                     "Not certified:     none"):
            self.assertIn(part, text)

    def test_the_redacted_file_is_valid_and_says_what_is_missing(self):
        result = check(FIXTURES / "redacted.csv", "redacted")
        for key, value in EXPECTED["redacted"].items():
            self.assertEqual(getattr(result, key), value, key)
        self.assertIn("respondent_id, ip_address", str(result))
        self.assertRegex(str(result), r"Removed rows: +1")

    def test_one_changed_cell_is_named_by_row_and_column(self):
        result = check(FIXTURES / "tampered.csv")
        for key, value in EXPECTED["tampered"].items():
            self.assertEqual(getattr(result, key), value, key)
        self.assertFalse(result)
        self.assertFalse(result.suspect)
        self.assertIn("row 3, column q_num", str(result))
        self.assertNotIn("suspect", str(result))

    def test_every_tricky_cell_reads_as_the_platform_wrote_it(self):
        header, rows = _read_csv(FIXTURES / "dataset.csv")
        self.assertEqual(header, ["response_id", "response_status", "respondent_id", "q_text", "q_num",
                                  "q_multi", "Spalte mit Ümlaut", "comma,name"])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][3], 'He said "hi", then left')
        self.assertEqual(rows[0][4], "007")
        self.assertEqual(rows[0][6], "Grüße 🌍")
        self.assertEqual(rows[1][3], "line1\nline2")
        self.assertEqual(rows[1][6], "NA")
        self.assertEqual(rows[1][7], '=HYPERLINK("http://x","y")')
        self.assertEqual(rows[2][3], "CRLF\r\ninside")
        self.assertEqual(rows[2][5], 'a\\"b')
        self.assertEqual(rows[2][6], " leading and trailing ")
        self.assertEqual(rows[2][7], "")
        self.assertEqual(rows[3][6], "\t tab")
        self.assertEqual(rows[3][7], '"')

    @unittest.skipUnless(PLATFORM_FIXTURES and PLATFORM_FIXTURES.is_dir(), "only inside the platform's repository")
    def test_the_copies_are_the_platforms_fixtures(self):
        for name in FIXTURE_FILES:
            self.assertEqual((FIXTURES / name).read_bytes(), (PLATFORM_FIXTURES / name).read_bytes(),
                             f"{name} differs from the platform's - copy it over again")


class WhatTheCheckCatches(WorkDir):
    def test_a_manifest_edited_after_signing(self):
        cert = certificate()
        cert["manifest"] = cert["manifest"].replace("TEST Certified", "TEST Forged")
        result = check(FIXTURES / "dataset.csv", cert)
        self.assertEqual(result.status, "invalid_signature")
        self.assertIn("Invalid signature", str(result))

    def test_a_signature_with_one_bit_flipped(self):
        cert = certificate()
        raw = bytearray(base64.b64decode(cert["signature"]))
        raw[10] ^= 0x04
        cert["signature"] = base64.b64encode(bytes(raw)).decode()
        self.assertEqual(check(FIXTURES / "dataset.csv", cert).status, "invalid_signature")
        cert["signature"] = "not base64!"
        self.assertEqual(check(FIXTURES / "dataset.csv", cert).status, "invalid_signature")

    def test_another_issuer_is_not_trusted_and_nothing_is_fetched(self):
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            result = verify(FIXTURES / "dataset.csv", FIXTURES / "dataset.certificate.json")
        self.assertEqual(result.status, "untrusted_issuer")
        self.assertIn(ISSUER, str(result))
        self.assertIn("trust=", str(result))
        self.assertIsNotNone(result.manifest, "the result names who issued it")

    def test_the_trusted_address_may_differ_by_a_trailing_slash_and_be_one_string(self):
        self.assertTrue(check(FIXTURES / "dataset.csv", trust=ISSUER + "/"))
        self.assertTrue(check(FIXTURES / "dataset.csv", trust=["https://sondavi.io", ISSUER]))

    def test_a_key_the_issuer_does_not_publish(self):
        result = check(FIXTURES / "dataset.csv", keys={"issuer": {"url": ISSUER}, "keys": []})
        self.assertEqual(result.status, "unknown_key")
        self.assertIn("ed25519:34750f98bd59fcfc", str(result))

    def test_something_that_is_no_certificate(self):
        result = check(FIXTURES / "dataset.csv", {"type": "something-else"})
        self.assertEqual(result.status, "malformed")
        self.assertEqual(result.problems, ["This is not a certificate of a supported version."])
        self.assertIsNone(result.manifest)

        not_json = self.dir / "x.certificate.json"
        not_json.write_text("type,version\n", encoding="utf-8")
        self.assertEqual(check(FIXTURES / "dataset.csv", not_json).status, "malformed")

        cert = certificate()
        cert["version"] = 2
        self.assertEqual(check(FIXTURES / "dataset.csv", cert).status, "malformed")
        cert = certificate()
        cert["manifest"] = "{not json"
        result = check(FIXTURES / "dataset.csv", cert)
        self.assertEqual((result.status, result.problems),
                         ("malformed", ["The certificate does not contain a readable manifest."]))

    def test_a_column_deleted_by_hand_cannot_be_verified(self):
        data = self.rewritten(lambda h, rows: ([c for i, c in enumerate(h) if i != 2],
                                               [[c for i, c in enumerate(r) if i != 2] for r in rows]))
        result = check(data)
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.problems, ['The certified column "respondent_id" is missing from the file. '
                                           'To share fewer columns, certify the changed file again where '
                                           'the certificate was issued.'])
        self.assertIn("Cannot be verified", str(result))

    def test_a_row_deleted_by_hand_cannot_be_verified(self):
        result = check(self.rewritten(lambda h, rows: (h, rows[1:])))
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.problems, ["The file has 3 data rows, the certificate accounts for 4. "
                                           "To share fewer rows, certify the changed file again where "
                                           "the certificate was issued."])

    def test_duplicate_column_names_cannot_be_verified(self):
        result = check(self.rewritten(lambda h, rows: (h + ["q_num"], [r + ["1"] for r in rows])))
        self.assertEqual((result.status, result.problems),
                         ("unverifiable", ["The data file has duplicate column names."]))

    def test_a_row_longer_than_the_header_cannot_be_verified(self):
        """Values no column names: R and pandas read such a file shifted, so no verifier passes over them."""
        result = check(FIXTURES / "too_long.csv")
        for key, value in EXPECTED["too_long"].items():
            self.assertEqual(getattr(result, key), value, key)
        self.assertEqual(str(result), "Cannot be verified: Data row 2 has 9 fields, the header 8.")

    def test_a_row_shorter_than_the_header_cannot_be_verified(self):
        result = check(self.rewritten(lambda h, rows: (h, rows[:2] + [rows[2][:-1]] + rows[3:])))
        self.assertEqual((result.status, result.problems),
                         ("unverifiable", ["Data row 3 has 7 fields, the header 8."]))

    def test_the_width_is_checked_after_the_names_and_before_the_record(self):
        twice = self.rewritten(lambda h, rows: (h + ["q_num"], [rows[0] + ["1"]] + rows[1:]))
        self.assertEqual(check(twice).problems, ["The data file has duplicate column names."])
        cert = certificate()
        cert["proof"]["columns"][3]["name"] = "q_other"
        self.assertEqual(check(FIXTURES / "too_long.csv", cert).problems, ["Data row 2 has 9 fields, the header 8."])

    def test_a_record_that_does_not_fit_is_never_valid(self):
        """A record that does not fit its manifest or the file is never valid, whichever way it is bent.

        Each case holds the record where one check of the verifier stands; without that check it
        would be read wrongly or crash. The platform and the browser run the same cases, and all
        verifiers must give the same status and say the same thing.
        """
        def manifest_no_json(cert):
            cert["manifest"] = "not json"

        def without_record(cert):
            del cert["proof"]

        def column_renamed(cert):
            self.assertEqual(cert["proof"]["columns"][3]["name"], "q_text")
            cert["proof"]["columns"][3]["name"] = "q_other"

        def leaf_too_few(cert):
            removed = [c for c in cert["proof"]["columns"] if c["name"] == "ip_address"]
            self.assertTrue(removed[0]["removed"])
            removed[0]["leaves"].pop()

        def salts_too_few(cert):
            self.assertEqual(cert["proof"]["columns"][0]["name"], "response_id")
            cert["proof"]["columns"][0]["salts"].pop()

        column_twice = self.rewritten(lambda h, rows: (h + ["q_num"], [r + ["1"] for r in rows]))
        cases = [
            ("a manifest that is no JSON", FIXTURES / "dataset.csv", "dataset", manifest_no_json,
             "malformed", "readable manifest"),
            ("a cell certificate without its record", FIXTURES / "dataset.csv", "dataset", without_record,
             "malformed", "supported version"),
            ("a column of the record renamed", FIXTURES / "dataset.csv", "dataset", column_renamed,
             "unverifiable", "does not match its manifest"),
            ("a removed column with a leaf too few", FIXTURES / "dataset.csv", "dataset", leaf_too_few,
             "unverifiable", 'removed column "ip_address" is incomplete'),
            ("a salt list a row too short", FIXTURES / "redacted.csv", "redacted", salts_too_few,
             "unverifiable", 'column "response_id" is incomplete'),
            ("a column twice in the file", column_twice, "dataset", None,
             "unverifiable", "duplicate column names"),
        ]
        for name, data, fixture, change, status, problem in cases:
            with self.subTest(name):
                cert = certificate(fixture)
                if change is not None:
                    change(cert)
                result = check(data, cert)
                self.assertEqual(result.status, status)
                self.assertIn(problem, result.problems[0] if result.problems else "")
                self.assertFalse(result.valid)

    def test_swapped_rows_are_caught(self):
        result = check(self.rewritten(lambda h, rows: (h, [rows[1], rows[0], rows[2], rows[3]])))
        self.assertEqual(result.status, "modified")

    def test_reordered_and_appended_columns_still_verify(self):
        def change(header, rows):
            order = list(reversed(range(len(header))))
            return ([header[i] for i in order] + ["my_score"], [[r[i] for i in order] + ["42"] for r in rows])

        result = check(self.rewritten(change))
        self.assertEqual(result.status, "valid")
        self.assertEqual(result.added_columns, ["my_score"])
        self.assertIn("Not certified:     1 - added after the certificate was issued: my_score", str(result))

    def test_changes_in_several_rows_and_columns_are_suspects(self):
        def change(header, rows):
            rows[0][3] = "changed"
            rows[3][4] = "2"
            return header, rows

        result = check(self.rewritten(change))
        self.assertEqual(result.status, "modified")
        self.assertTrue(result.suspect, "two rows x two columns: four candidates for two changes")
        self.assertEqual(result.modified_cells, [
            {"row": 1, "column": "q_num"}, {"row": 1, "column": "q_text"},
            {"row": 4, "column": "q_num"}, {"row": 4, "column": "q_text"},
        ])
        self.assertIn("suspects", str(result))

    def test_cells_are_ordered_by_row_then_column_name_in_code_point_order(self):
        """As the platform sorts them (strcmp): a capital before a small letter, "S" before "c"."""
        def change(header, rows):
            rows[0][6] = rows[0][7] = rows[3][6] = rows[3][7] = "changed"
            return header, rows

        result = check(self.rewritten(change))
        self.assertEqual(result.modified_cells, [
            {"row": 1, "column": "Spalte mit Ümlaut"}, {"row": 1, "column": "comma,name"},
            {"row": 4, "column": "Spalte mit Ümlaut"}, {"row": 4, "column": "comma,name"},
        ])

    def test_saving_from_a_spreadsheet_program_counts_as_a_change(self):
        def change(header, rows):
            rows[0][4] = "7"          # "007" as a spreadsheet program writes it back
            return header, rows

        result = check(self.rewritten(change))
        self.assertEqual(result.modified_cells, [{"row": 1, "column": "q_num"}])

    def test_the_same_cells_with_other_quoting_and_line_endings_still_verify(self):
        header, rows = _read_csv(FIXTURES / "dataset.csv")
        quoted = "\r\n".join(",".join('"' + v.replace('"', '""') + '"' for v in row) for row in [header, *rows])
        path = self.dir / "quoted.csv"
        path.write_bytes((quoted + "\r\n").encode("utf-8"))      # every cell quoted, CRLF, no BOM
        self.assertEqual(check(path).status, "valid")

    def test_a_file_that_is_not_utf8_cannot_be_verified(self):
        raw = (FIXTURES / "dataset.csv").read_bytes().replace("Grüße".encode("utf-8"), "Grüße".encode("latin-1"))
        path = self.dir / "latin1.csv"
        path.write_bytes(raw)
        result = check(path)
        self.assertEqual((result.status, result.problems), NOT_WELL_FORMED)

    def test_an_altered_record_of_removed_data_is_caught(self):
        cert = certificate()
        ip = next(c for c in cert["proof"]["columns"] if c["name"] == "ip_address")
        ip["leaves"][0] = "0" * 64
        result = check(FIXTURES / "dataset.csv", cert)
        self.assertEqual(result.status, "modified")
        self.assertEqual(result.modified_cells, [], "no cell of the file is wrong")
        self.assertEqual(result.problems, ["The certificate's record of removed data was altered."])

    def test_a_changed_value_cannot_hide_behind_a_leaf(self):
        """A present row whose salt was swapped for its leaf would leave its value unchecked."""
        cert = certificate("redacted")
        q = next(c for c in cert["proof"]["columns"] if c["name"] == "q_text")
        q["salts"][0] = "-" + "ab" * 32
        result = check(FIXTURES / "redacted.csv", cert)
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.problems,
                         ['The record of the column "q_text" is inconsistent with the rows in the file.'])

    def test_an_invented_row_cannot_hide_behind_a_row_number_used_twice(self):
        """Without the check the invented row would share row 0's salts and never be compared."""
        data = self.rewritten(lambda h, rows: (h, [["99", "complete", "invented", "1", "", "x", "y"]] + rows),
                              source="redacted.csv")
        invalid = ("unverifiable", ["The certificate's row list is invalid."])
        cert = certificate("redacted")
        cert["proof"]["rows"] = [0, 0, 2, 3]
        result = check(data, cert)
        self.assertEqual((result.status, result.problems), invalid)
        self.assertEqual(result.modified_cells, [])

        for rows in ([True, 2, 3], [False, 2, 3], [0.0, 2, 3], ["0", 2, 3], [-1, 2, 3], [4, 2, 3]):
            cert = certificate("redacted")
            cert["proof"]["rows"] = rows       # a yes/no is no row number, nor is 0.0 or "0"
            result = check(FIXTURES / "redacted.csv", cert)
            self.assertEqual((result.status, result.problems), invalid, repr(rows))

    def test_a_removed_column_put_back_into_the_file_is_added_not_certified(self):
        invented = iter(["6.6.6.6", "6.6.6.7", "6.6.6.8", "6.6.6.9"])
        result = check(self.rewritten(lambda h, rows: (h + ["ip_address"], [r + [next(invented)] for r in rows])))
        self.assertEqual(result.status, "valid", "the certified cells still match")
        self.assertEqual(result.removed_columns, ["ip_address"])
        self.assertEqual(result.added_columns, ["ip_address"])
        self.assertIn("Not certified:     1 - left out when the certificate was issued, so it cannot check them: ip_address",
                      str(result))

    def test_the_test_signer_signs_as_the_platform_does(self):
        """Ed25519 is deterministic: the fixture manifest signed again gives the fixture signature."""
        cert = certificate()
        self.assertEqual(base64.b64encode(sign(cert["manifest"].encode("utf-8"))).decode(), cert["signature"])
        self.assertTrue(check(FIXTURES / "dataset.csv", resigned(lambda m: None)))

    def test_a_signed_manifest_the_comparison_cannot_use(self):
        unreadable = ("unverifiable", ["The certificate's record could not be read."])
        for label, change in [
            ("no columns", lambda m: m.pop("columns")),
            ("a column without its sum", lambda m: m["columns"][3].pop("root")),
            ("no row count", lambda m: m.pop("rows")),
            ("columns that are no list", lambda m: m.update(columns="q_text")),
        ]:
            result = check(FIXTURES / "dataset.csv", resigned(change))
            self.assertEqual((result.status, result.problems), unreadable, label)

    def test_a_record_that_cannot_be_read(self):
        unreadable = ("unverifiable", ["The certificate's record could not be read."])
        for salt in ("zz" * 16, "ab" * 15, "ab " * 16, None, ["ab" * 16], {"a": 1}):   # no hex; 15 bytes; spaces; …
            cert = certificate("redacted")
            cert["proof"]["columns"][0]["salts"][0] = salt
            result = check(FIXTURES / "redacted.csv", cert)
            self.assertEqual((result.status, result.problems), unreadable, repr(salt))
        for seed in ("ab" * 16, "not hex at all", 12):                 # a seed is 32 bytes of hex
            cert = certificate()
            cert["proof"]["columns"][0]["seed"] = seed
            result = check(FIXTURES / "dataset.csv", cert)
            self.assertEqual((result.status, result.problems), unreadable, repr(seed))
        cert = certificate()
        ip = next(c for c in cert["proof"]["columns"] if c["name"] == "ip_address")
        ip["leaves"][2] = ip["leaves"][2][:-2]                           # a leaf one byte short
        result = check(FIXTURES / "dataset.csv", cert)
        self.assertEqual((result.status, result.problems), unreadable)

    def test_a_missing_data_file_is_an_error_not_a_status(self):
        with self.assertRaises(FileNotFoundError):
            check(self.dir / "nope.csv")


class TheStrictReader(WorkDir):
    """A certified CSV reads exactly as on the platform, in the browser and in R (SPECS §86.10).

    A file that another program could read differently - a stray quote, a lone carriage return,
    a NUL, bytes that are no UTF-8 - could show values nobody certified, so it is not read at all.
    """

    def test_every_shared_case(self):
        self.assertGreaterEqual(len(CSV_CASES), 24)
        for case in CSV_CASES:
            with self.subTest(case["name"]):
                path = self.dir / "case.csv"
                path.write_bytes(base64.b64decode(case["base64"]))
                try:
                    got = list(_read_csv(path))
                except _NotWellFormed:
                    got = "invalid"
                self.assertEqual(got, case["result"])

    def test_a_control_character_is_refused_in_a_name_not_in_a_value(self):
        path = self.dir / "case.csv"
        for name in ("a\x1f", "a\x7f", "\x01a", "a\tb"):
            path.write_bytes(f"{name},b\n1,2\n".encode())
            with self.assertRaises(_NotWellFormed, msg=repr(name)):
                _read_csv(path)
        path.write_bytes('a\u0080,b\n"x\x01\x1f\x7fy",2\n'.encode())
        self.assertEqual(_read_csv(path), (["a\u0080", "b"], [["x\x01\x1f\x7fy", "2"]]))

    def test_a_file_no_two_programs_read_alike_cannot_be_verified(self):
        original = (FIXTURES / "dataset.csv").read_bytes()
        for name, before, after in [
            ("a NUL in a cell", b"P-001", b"P-\x00001"),
            ("a carriage return alone", b"P-001,", b"P-001\r,"),
            ("a space before a quote", b',"He said', b', "He said'),
            ("a quote inside an unquoted field", b"P-002", b'P-"002'),
            ("an unterminated quote", b'""""\n', b'"""\n'),
            ("bytes that are no UTF-8", "Grüße".encode(), "Grüße".encode("latin-1")),
        ]:
            with self.subTest(name):
                self.assertEqual(original.count(before), 1)
                path = self.dir / "data.csv"
                path.write_bytes(original.replace(before, after))
                result = check(path)
                self.assertEqual((result.status, result.problems), NOT_WELL_FORMED)
                self.assertEqual(str(result), "Cannot be verified: The data file is not a well-formed CSV file.")

    def test_the_file_is_read_only_once_the_certificate_holds(self):
        """As on the platform: who issued the certificate, and whether it was altered, come first."""
        path = self.dir / "data.csv"
        path.write_bytes(b"a,b\r1,2\n")
        self.assertEqual(check(path, trust=()).status, "untrusted_issuer")
        cert = certificate()
        cert["signature"] = base64.b64encode(bytes(64)).decode()
        self.assertEqual(check(path, cert).status, "invalid_signature")
        self.assertEqual(check(path).problems, NOT_WELL_FORMED[1])


class WhatTheSummaryNames(WorkDir):
    """Columns in the file that are not certified, and values the platform did not measure.

    Worded as on the platform's /verify page.
    """

    COMPUTED = "worked out by the platform when the file was made, never certified"
    BROWSER = "certified as stored, but sent by the participant's browser rather than measured by the platform"

    def test_computed_and_browser_reported_columns_of_an_exported_file(self):
        def change(manifest):
            manifest["basis"] = {"kind": "export", "responses_total": 4, "columns_derived": ["quality"],
                                 "browser_reported": ["response_status", "q_num", "ip_address"], "offline_synced": 2}

        data = self.rewritten(lambda h, rows: (h + ["quality", "my_note"], [r + ["0.9", "n"] for r in rows]))
        result = check(data, resigned(change))
        self.assertEqual((result.status, result.added_columns), ("valid", ["quality", "my_note"]),
                         "a computed column is not certified, so it is among the added ones")
        self.assertEqual(result.column_groups()["computed"], ["quality"])
        lines = str(result).splitlines()
        for line in (f"  {'Certified:':<22}8 columns x 4 rows, every value as participants entered it",
                     f"  {'Not certified:':<22}2 - {self.COMPUTED}: quality; added after the certificate was issued: "
                     f"my_note",
                     f"  {'Missing from file:':<22}1 - left out when the certificate was issued: ip_address",
                     # ip_address was reported by the browser, but it is not certified in this file
                     f"  {'Sent by the browser:':<22}response_status, q_num - {self.BROWSER}",
                     f"  {'From offline devices:':<22}2 responses collected offline; "
                     f"the time they were completed is the device's"):
            self.assertIn(line, lines)

        for offline, shown in ((1, "1 response collected offline; the time they were completed is the device's"),
                               (0, None), (True, None), ("2", None)):
            basis = {"kind": "export", "offline_synced": offline}
            text = str(check(data, resigned(lambda m, b=basis: m.update(basis=b))))
            if shown:
                self.assertIn(f"  {'From offline devices:':<22}{shown}", text.splitlines())
            else:
                self.assertNotIn("offline", text, repr(offline))

        text = str(check(data, resigned(lambda m: m.update(basis={"kind": "export"}))))
        self.assertIn(f"  {'Not certified:':<19}2 - added after the certificate was issued: quality, my_note",
                      text.splitlines(), "named as computed by the certificate, or not at all")
        self.assertNotIn("worked out", text)
        self.assertNotIn("browser", text)

    def test_a_column_left_out_at_issue_and_back_in_a_copy_is_named_once(self):
        """The case of 20.10.2026: a fuller copy checked against a certificate that left a column out."""
        data = self.rewritten(lambda h, rows: (h + ["ip_address"], [r + ["10.0.0.1"] for r in rows]))
        result = check(data)
        self.assertEqual((result.status, result.removed_columns, result.added_columns),
                         ("valid", ["ip_address"], ["ip_address"]))
        groups = result.column_groups()
        self.assertEqual((groups["left_out"], groups["missing_left_out"]), (["ip_address"], []))
        text = str(result)
        self.assertEqual(text.count("ip_address"), 1, text)
        self.assertIn(f"  {'Not certified:':<19}1 - left out when the certificate was issued, so it cannot check them: "
                      f"ip_address", text.splitlines())
        self.assertIn(f"  {'Missing from file:':<19}none", text.splitlines())

    def test_how_a_certified_file_was_read_and_what_it_holds_besides(self):
        def change(fmt, **more):
            def apply(manifest):
                manifest["basis"]["file"]["format"] = fmt
                manifest["basis"].update(more)
            return apply

        for fmt, words in (({"type": "csv", "delimiter": ";", "encoding": "Windows-1252"},
                            "CSV, values separated by semicolons, Windows-1252"),
                           ({"type": "csv", "delimiter": "tab", "encoding": "UTF-8"},
                            "CSV, values separated by tabs, UTF-8"),
                           ({"type": "json"}, "JSON, one object per response")):
            text = str(check(FIXTURES / "dataset.csv", resigned(change(fmt), "file")))
            self.assertIn(f"  {'Read as:':<21}{words}", text.splitlines())

        result = check(FIXTURES / "dataset.csv", resigned(change(
            {"type": "csv", "delimiter": ",", "encoding": "UTF-8"}, columns_own=["my_score", "a_r"],
            columns_derived=["a_r"], browser_reported=["started_at", "q_num"], offline_synced=1), "file"))
        self.assertEqual((result.status, result.added_columns), ("valid", ["my_score", "a_r"]))
        lines = str(result).splitlines()
        for line in (f"  {'Not certified:':<22}2 - {self.COMPUTED}: a_r; not part of the survey's data, added by the "
                     f"researcher: my_score",
                     f"  {'Sent by the browser:':<22}q_num - {self.BROWSER}",
                     f"  {'From offline devices:':<22}1 response collected offline; "
                     f"the time they were completed is the device's",
                     f"  {'Read as:':<22}CSV, values separated by commas, UTF-8",
                     f"  {'Rows matched:':<22}by response ID"):
            self.assertIn(line, lines)


class CertifiedFile(WorkDir):
    """A file the researcher had, certified as a whole: its bytes are what was signed."""

    def test_the_certified_file_is_valid_and_says_what_the_comparison_found(self):
        result = check(FIXTURES / "dataset.csv", "file")
        for key, value in EXPECTED["file"].items():
            self.assertEqual(getattr(result, key), value, key)
        self.assertEqual((result.modified_cells, result.problems, result.suspect), ([], [], False))
        self.assertEqual(result.basis["responses_in_file"], 4)
        text = str(result)
        for part in ("this is the file that was certified", "4 of 6 responses of this survey",
                     "2 responses: 2 incomplete", "by group: B 2", "1 - in the survey's data, not in this file: ip_address",
                     "q_num (1 cell) - the same value in another notation", "\n  Row order:           unchanged",
                     "\n  File:                dataset.csv\n",
                     "\n  Read as:             CSV, values separated by commas, UTF-8\n",
                     "\n  Rows matched:        by response ID\n", "TEST Certified", "Sondavi (test)",
                     "own CSV file, after every value was compared with the stored responses\n"):
            self.assertIn(part, text)
        self.assertNotIn("A 0", text, "a group with nothing left out is not listed")
        self.assertNotIn("Derived from", text)

    def test_a_file_certified_from_an_earlier_certificate(self):
        result = check(FIXTURES / "derived.csv", "derived")
        for key, value in EXPECTED["derived"].items():
            self.assertEqual(getattr(result, key), value, key)
        self.assertEqual((result.modified_cells, result.problems, result.suspect), ([], [], False))
        derived_from = result.basis["derived_from"]
        self.assertEqual({key: derived_from.get(key) for key in EXPECTED["derived_from"]}, EXPECTED["derived_from"])
        text = str(result)
        self.assertIn("\n  How:                 Certified from the researcher's own CSV file, after every value "
                      "was compared with a file certified earlier\n", text)
        self.assertIn("\n  Derived from:        certificate 00000000 of 20 October 2026 (dataset.csv)\n", text)
        self.assertNotIn("the stored responses", text)
        for part in ("3 of 4 responses of this survey", "Left out:            1 response: 1 incomplete\n",
                     "2 - in the survey's data, not in this file: q_multi, ip_address"):
            self.assertIn(part, text)
        self.assertEqual(check(FIXTURES / "dataset.csv", "derived").status, "modified",
                         "the file it was derived from is another file")

    def test_responses_of_unknown_status_and_of_a_group_no_file_names(self):
        def change(by_group, group_unknown, derived_from):
            def apply(manifest):
                basis = manifest["basis"]
                basis["file"]["format"] = {"type": "json"}
                basis["missing"] = {"count": 3, "by_status": {"partial": 1, "unknown": 2}, "by_group": by_group,
                                    "group_unknown": group_unknown}
                basis["derived_from"] = derived_from
            return apply

        earlier = {"certificate": "abcdef01-2345", "issued_at": "2026-10-20T23:30:00-02:00", "kind": "file"}
        result = check(FIXTURES / "derived.csv", resigned(change({"A": 1}, 2, earlier), "derived"))
        self.assertEqual((result.status, result.removed_rows), ("valid", 3))
        text = str(result)
        for part in ("3 responses: 1 incomplete, 2 of unknown status; by group: A 1, and 2 of a group no file names\n",
                     "own JSON file, after every value was compared with a file certified earlier\n",
                     "Derived from:        certificate abcdef01 of 21 October 2026\n"):
            self.assertIn(part, text)

        text = str(check(FIXTURES / "derived.csv", resigned(change({}, 2, {"issued_at": "soon"}), "derived")))
        self.assertIn("2 of unknown status; by group: 2 of a group no file names\n", text)
        self.assertIn("Derived from:        certificate  of soon\n", text)
        text = str(check(FIXTURES / "derived.csv", resigned(change({"A": 1}, True, "nope"), "derived")))
        self.assertIn("by group: A 1\n", text, "a count is a number, not a yes")
        self.assertIn("compared with the stored responses", text, "only a record says what it was derived from")
        self.assertNotIn("Derived from", text)

    def test_any_other_file_is_modified_and_no_cell_is_named(self):
        result = check(FIXTURES / "tampered.csv", "file")
        for key, value in EXPECTED["file_tampered"].items():
            self.assertEqual(getattr(result, key), value, key)
        sentence = ("This is not the file that was certified: its fingerprint differs. "
                    "Any change counts, even saving it again.")
        self.assertEqual((result.problems, result.modified_cells), ([sentence], []))
        self.assertEqual(str(result), sentence)

        resaved = self.dir / "resaved.csv"           # the same cells, written again: another file
        header, rows = _read_csv(FIXTURES / "dataset.csv")
        _write_csv(resaved, header, rows)
        self.assertEqual(check(resaved, "file").status, "modified")

    def test_the_file_is_hashed_never_read_as_a_table(self):
        for name in ("study.xlsx", "study.json", "study"):
            copy_of = self.dir / name
            shutil.copy(FIXTURES / "dataset.csv", copy_of)
            self.assertEqual(check(copy_of, "file").status, "valid", name)
        not_a_table = self.dir / "noise.xlsx"
        not_a_table.write_bytes(b"PK\x03\x04\x00\xff" * 100)
        self.assertEqual(check(not_a_table, "file").status, "modified")

    def test_redact_refuses_a_certified_file(self):
        with self.assertRaisesRegex(ValueError, "certify the changed file again"):
            _redact(FIXTURES / "dataset.csv", FIXTURES / "file.certificate.json", drop_columns=["q_num"],
                    out_data=self.dir / "r.csv", trust=TRUST, keys=KEYS)
        self.assertEqual(list(self.dir.iterdir()), [], "nothing written")

    def test_a_record_of_cells_beside_it_does_not_change_the_kind(self):
        cert = certificate("file")
        cert["proof"] = certificate()["proof"]        # not signed, so it can be anything
        self.assertEqual(check(FIXTURES / "dataset.csv", cert).status, "valid")
        self.assertEqual(check(FIXTURES / "tampered.csv", cert).status, "modified")

    def test_only_a_certified_file_may_come_without_a_record(self):
        cert = certificate()
        cert["proof"] = None
        result = check(FIXTURES / "dataset.csv", cert)
        self.assertEqual((result.status, result.problems),
                         ("malformed", ["This is not a certificate of a supported version."]))

    def test_what_the_comparison_found_is_signed(self):
        cert = certificate("file")
        cert["manifest"] = cert["manifest"].replace('"columns_own":[]', '"columns_own":["my_score"]')
        self.assertEqual(check(FIXTURES / "dataset.csv", cert).status, "invalid_signature")
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            result = verify(FIXTURES / "dataset.csv", FIXTURES / "file.certificate.json")
        self.assertEqual(result.status, "untrusted_issuer")

    def test_an_excel_file_with_own_columns_and_screen_outs(self):
        def change(manifest):
            basis = manifest["basis"]
            basis["file"]["format"] = {"type": "xlsx", "sheet": "Data"}
            basis["missing"] = {"count": 3, "by_status": {"partial": 1, "screened_out": 2}, "by_group": {}}
            basis["columns_own"] = ["my_score"]
            basis["written_differently"] = {}
            basis["row_order"] = "changed"
            basis["matched_by"] = "content"

        result = check(FIXTURES / "dataset.csv", resigned(change, "file"))
        self.assertEqual((result.status, result.removed_rows, result.added_columns), ("valid", 3, ["my_score"]))
        text = str(result)
        for part in ('\n  Read as:             Excel, sheet "Data"\n', "3 responses: 1 incomplete, 2 screened out",
                     "1 - not part of the survey's data, added by the researcher: my_score", "Written differently: none", "\n  Row order:           changed",
                     "\n  Rows matched:        by their content (the file has no response ID)\n"):
            self.assertIn(part, text)
        self.assertNotIn("by group", text)


class Keys(unittest.TestCase):
    def test_keys_as_a_path_a_string_or_a_dict(self):
        document = json.loads(KEYS.read_text(encoding="utf-8"))
        for keys in (KEYS, str(KEYS), document):
            self.assertTrue(check(FIXTURES / "dataset.csv", keys=keys), repr(type(keys)))

    def test_by_default_they_come_from_the_issuer(self):
        asked = []

        def answer(request, timeout=None):
            asked.append(request.full_url)
            return io.BytesIO(KEYS.read_bytes())

        with mock.patch("urllib.request.urlopen", side_effect=answer):
            self.assertTrue(check(FIXTURES / "dataset.csv", keys=None))
            self.assertTrue(check(FIXTURES / "dataset.csv", keys="https://mirror.example/keys.json"))
        self.assertEqual(asked, [ISSUER + "/.well-known/sondavi-certificate-keys.json",
                                 "https://mirror.example/keys.json"])

    def test_an_issuer_out_of_reach_says_how_to_check_offline(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaisesRegex(RuntimeError, "keys="):
                check(FIXTURES / "dataset.csv", keys=None)

    def test_a_key_counts_only_under_its_own_id(self):
        document = json.loads(KEYS.read_text(encoding="utf-8"))
        real = document["keys"][0]
        raw = base64.b64decode(real["public_key"])
        other = base64.b64encode(hashlib.sha256(b"another key").digest()).decode()
        for label, entry in [
            ("another key under this id", dict(real, public_key=other)),
            ("this key under another id", dict(real, id="ed25519:0000000000000000")),
            ("a key of 31 bytes under its own id",
             dict(real, public_key=base64.b64encode(raw[:31]).decode(),
                  id="ed25519:" + hashlib.sha256(raw[:31]).hexdigest()[:16])),
        ]:
            result = check(FIXTURES / "dataset.csv", keys=dict(document, keys=[entry]))
            self.assertEqual(result.status, "unknown_key", label)
        # Ignored entries do not spoil the document for the key that is right.
        self.assertTrue(check(FIXTURES / "dataset.csv",
                              keys=dict(document, keys=[dict(real, id="ed25519:0000000000000000"), real])))

    def test_a_document_without_keys_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not a keys document"):
            check(FIXTURES / "dataset.csv", keys={"issuer": {}})


class KeysThatComeWithThePackage(unittest.TestCase):
    """The keys of https://sondavi.io ship inside the package; the issuer is asked only for others.

    The shipped document is swapped for one of the fixture issuer here, so the rules can be
    seen at work on the fixture certificate.
    """

    def setUp(self):
        self.fixture_keys = json.loads(KEYS.read_text(encoding="utf-8"))

    def built_in(self, url=ISSUER, keys=None):
        document = {"issuer": {"name": "Test", "url": url},
                    "keys": self.fixture_keys["keys"] if keys is None else keys,
                    "saved_from": None, "saved_at": None}
        return mock.patch.object(certificates, "_builtin_keys_document", return_value=document)

    def fetch(self, **kwargs):
        return mock.patch.object(certificates, "_fetch_json", **kwargs)

    def test_a_built_in_key_needs_no_connection(self):
        offline = RuntimeError("no connection")
        with self.built_in(), self.fetch(side_effect=offline) as fetched:
            self.assertTrue(check(FIXTURES / "dataset.csv", keys=None))
            self.assertTrue(check(FIXTURES / "redacted.csv", "redacted", keys=None))
        fetched.assert_not_called()
        with self.built_in(url=ISSUER + "/"), self.fetch(side_effect=offline):
            self.assertTrue(check(FIXTURES / "dataset.csv", keys=None), "a trailing slash is the same issuer")

    def test_a_key_not_built_in_is_fetched_from_the_issuer(self):
        with self.built_in(keys=[]), self.fetch(return_value=self.fixture_keys) as fetched:
            self.assertTrue(check(FIXTURES / "dataset.csv", keys=None))
        fetched.assert_called_once_with(ISSUER + "/.well-known/sondavi-certificate-keys.json")

    def test_keys_built_in_for_another_issuer_are_not_used(self):
        with self.built_in(url="https://sondavi.io"), self.fetch(return_value={"keys": []}) as fetched:
            result = check(FIXTURES / "dataset.csv", keys=None)
        self.assertEqual(result.status, "unknown_key")
        fetched.assert_called_once()

    def test_keys_passed_by_hand_are_the_only_ones(self):
        with self.built_in() as built_in, self.fetch(side_effect=no_network) as fetched:
            result = check(FIXTURES / "dataset.csv", keys={"issuer": {"url": ISSUER}, "keys": []})
        self.assertEqual(result.status, "unknown_key")
        built_in.assert_not_called()
        fetched.assert_not_called()

    def test_an_untrusted_issuer_looks_nothing_up(self):
        with self.built_in() as built_in, self.fetch(side_effect=no_network) as fetched:
            result = verify(FIXTURES / "dataset.csv", FIXTURES / "dataset.certificate.json")
        self.assertEqual(result.status, "untrusted_issuer")
        built_in.assert_not_called()
        fetched.assert_not_called()

    def test_a_newer_key_without_connection_says_what_it_needs(self):
        with self.built_in(keys=[]), self.fetch(side_effect=RuntimeError("Could not fetch.")):
            with self.assertRaisesRegex(RuntimeError, "newer key needs a connection .* keys="):
                check(FIXTURES / "dataset.csv", keys=None)

    def test_built_in_keys_go_through_the_same_filter(self):
        real = self.fixture_keys["keys"][0]
        raw = base64.b64decode(real["public_key"])
        other = base64.b64encode(hashlib.sha256(b"another key").digest()).decode()
        for label, entry in [
            ("another key under this id", dict(real, public_key=other)),
            ("a key of 31 bytes under the certificate's id", dict(real, public_key=base64.b64encode(raw[:31]).decode())),
        ]:
            with self.built_in(keys=[entry]), self.fetch(return_value={"keys": []}) as fetched:
                result = check(FIXTURES / "dataset.csv", keys=None)
            self.assertEqual(result.status, "unknown_key", label)
            fetched.assert_called_once()

    def test_a_missing_or_broken_file_counts_as_no_keys(self):
        folder = Path(tempfile.mkdtemp(prefix="sondavi-keys-"))
        try:
            for content in (None, "not json", "[1, 2]", '{"issuer": {"url": "%s"}, "keys": "x"}' % ISSUER):
                if content is not None:
                    (folder / "certificate-keys.json").write_text(content, encoding="utf-8")
                with mock.patch("importlib.resources.files", return_value=folder), \
                        self.fetch(return_value=self.fixture_keys) as fetched:
                    self.assertTrue(check(FIXTURES / "dataset.csv", keys=None), repr(content))
                fetched.assert_called_once()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_shipped_file(self):
        """Read the way the package reads it: a keys document of https://sondavi.io, every key usable."""
        document = certificates._builtin_keys_document()
        self.assertIsInstance(document, dict, "the shipped file is there and is JSON")
        self.assertEqual(document["issuer"]["url"], "https://sondavi.io")
        self.assertIsInstance(document["keys"], list)
        self.assertEqual(len(certificates._keys_in(document)), len(document["keys"]),
                         "every shipped key passes the filter")
        for field in ("saved_from", "saved_at"):
            self.assertIn(field, document)

    @unittest.skipUnless(PLATFORM_FIXTURES and (HERE.parents[2] / "certificate-keys.json").is_file(),
                         "only inside the platform's repository")
    def test_the_shipped_file_is_the_platforms_copy(self):
        shipped = HERE.parent / "src" / "sondavi" / "certificate-keys.json"
        self.assertEqual(shipped.read_bytes(), (HERE.parents[2] / "certificate-keys.json").read_bytes(),
                         "certificate-keys.json differs from clients/certificate-keys.json")


class Redacting(WorkDir):
    """Leaving cells out of a record: the platform does it, the package keeps the reference."""

    def test_redacting_is_not_offered_by_the_package(self):
        self.assertNotIn("redact", sondavi.__all__)
        self.assertFalse(hasattr(sondavi, "redact"))
        self.assertNotIn("redact", certificates.__all__)

    def test_our_redaction_is_the_platforms_redaction(self):
        out_data, out_cert = _redact(FIXTURES / "dataset.csv", FIXTURES / "dataset.certificate.json",
                                     out_data=self.dir / "r.csv", trust=TRUST, keys=KEYS,
                                     **EXPECTED["redaction"])
        self.assertEqual(out_cert, str(self.dir / "r.certificate.json"))
        mine = json.loads(Path(out_cert).read_text(encoding="utf-8"))
        platform = certificate("redacted")
        self.assertEqual(mine["proof"], platform["proof"])
        self.assertEqual(mine["manifest"], platform["manifest"], "the signed text, character for character")
        self.assertEqual(mine["signature"], platform["signature"])
        self.assertEqual(list(mine), ["type", "version", "manifest", "signature", "proof"])

        self.assertEqual(_read_csv(out_data), _read_csv(FIXTURES / "redacted.csv"), "the same cells")
        self.assertTrue(Path(out_data).read_bytes().startswith(b"\xef\xbb\xbf"), "with a BOM")

        result = check(out_data, mine)
        self.assertEqual(result.status, "valid")
        self.assertEqual((result.removed_columns, result.removed_rows), (["respondent_id", "ip_address"], 1))

    def test_redacting_again_from_a_redacted_file(self):
        out_data, out_cert = _redact(FIXTURES / "redacted.csv", FIXTURES / "redacted.certificate.json",
                                     drop_columns="q_multi", drop_rows=1, out_data=self.dir / "rr.csv",
                                     trust=TRUST, keys=KEYS)
        result = check(out_data, out_cert)
        self.assertEqual(result.status, "valid", result.problems)
        self.assertEqual(result.removed_columns, ["respondent_id", "q_multi", "ip_address"])
        self.assertEqual(result.removed_rows, 2)

    def test_a_removed_cell_keeps_neither_its_seed_nor_its_salt(self):
        out_data, out_cert = _redact(FIXTURES / "dataset.csv", FIXTURES / "dataset.certificate.json",
                                     drop_rows=[2], out_data=self.dir / "r.csv", trust=TRUST, keys=KEYS)
        cert = json.loads(Path(out_cert).read_text(encoding="utf-8"))
        for column in cert["proof"]["columns"]:
            if column.get("removed"):
                continue
            self.assertNotIn("seed", column, "with a row gone, a seed would give away its salts")
            self.assertTrue(column["salts"][1].startswith("-"), "the removed row is only a leaf")
            self.assertEqual(len(column["salts"][1]), 65)
        text = Path(out_data).read_text(encoding="utf-8-sig")
        self.assertNotIn("P-002", text)
        self.assertNotIn("line1", text)
        self.assertEqual(check(out_data, cert).status, "valid")

    def test_a_file_that_does_not_verify_is_refused(self):
        with self.assertRaisesRegex(ValueError, r"\(modified\)"):
            _redact(FIXTURES / "tampered.csv", FIXTURES / "dataset.certificate.json",
                    drop_columns=["respondent_id"], out_data=self.dir / "r.csv", trust=TRUST, keys=KEYS)
        with self.assertRaisesRegex(ValueError, r"\(untrusted_issuer\)"):
            _redact(FIXTURES / "dataset.csv", FIXTURES / "dataset.certificate.json",
                    out_data=self.dir / "r.csv", keys=KEYS)
        self.assertEqual(list(self.dir.iterdir()), [], "nothing written")

    def test_unknown_columns_and_rows_are_refused(self):
        args = (FIXTURES / "dataset.csv", FIXTURES / "dataset.certificate.json")
        kwargs = {"out_data": self.dir / "r.csv", "trust": TRUST, "keys": KEYS}
        with self.assertRaisesRegex(ValueError, 'No column "nope" in the file.'):
            _redact(*args, drop_columns=["nope"], **kwargs)
        for row in (0, 5, True, "2"):
            with self.assertRaisesRegex(ValueError, "No data row"):
                _redact(*args, drop_rows=[row], **kwargs)

    def test_the_input_is_never_overwritten_and_names_follow_the_input(self):
        data = self.dir / "study.csv"
        cert = self.dir / "study.certificate.json"
        shutil.copy(FIXTURES / "dataset.csv", data)
        shutil.copy(FIXTURES / "dataset.certificate.json", cert)
        with self.assertRaisesRegex(ValueError, "write the result elsewhere"):
            _redact(data, cert, drop_rows=[1], out_data=data, trust=TRUST, keys=KEYS)
        with self.assertRaisesRegex(ValueError, "write the result elsewhere"):
            _redact(data, cert, drop_rows=[1], out_data=self.dir / "x.csv", out_certificate=cert,
                    trust=TRUST, keys=KEYS)
        self.assertEqual(data.read_bytes(), (FIXTURES / "dataset.csv").read_bytes())

        out_data, out_cert = _redact(data, cert, drop_rows=[1], trust=TRUST, keys=KEYS)
        self.assertEqual((out_data, out_cert), (str(self.dir / "study-redacted.csv"),
                                                str(self.dir / "study-redacted.certificate.json")))
        self.assertTrue(check(out_data, out_cert))

    def test_awkward_cells_survive_being_written(self):
        cells = ["a\rb", "a\r\nb", "a\nb", 'say "hi"', "a,b", "", " x ", "\t", "=1+2", "﻿not a BOM", '"']
        path = self.dir / "awkward.csv"
        _write_csv(path, ["cell"] + [f"c{i}" for i in range(len(cells))], [["row"] + cells])
        header, rows = _read_csv(path)
        self.assertEqual(rows, [["row"] + cells])

        # A NUL and bytes that are no UTF-8 make a file no verifier reads: none can be certified.
        for cell in ("x\0y", "Gr\udcfc\udcdfe"):
            _write_csv(path, ["cell"], [[cell]])
            with self.assertRaises(_NotWellFormed, msg=repr(cell)):
                _read_csv(path)

        one = self.dir / "one.csv"
        _write_csv(one, ["only"], [["a"], [""], ["b"], [""]])
        self.assertEqual(_read_csv(one), (["only"], [["a"], [""], ["b"], [""]]))
        self.assertTrue(one.read_bytes().endswith(b'\na\n""\nb\n""\n'),
                        "an empty cell alone is quoted: most readers skip an empty line")

    def test_a_long_cell_is_read(self):
        """An interview transcript easily runs past 128 KiB, where the csv module stops by default."""
        path = self.dir / "long.csv"
        _write_csv(path, ["id", "text"], [["1", "x" * 300_000 + '"\r\n' + "y" * 300_000]])
        self.assertEqual(_read_csv(path)[1][0][1], "x" * 300_000 + '"\r\n' + "y" * 300_000)


class Ed25519(unittest.TestCase):
    """The signature check on the standard library alone - the one the certificates use."""

    # RFC 8032, section 7.1, tests 1-3: public key, message, signature.
    VECTORS = [
        ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ]
    L = 2**252 + 27742317777372353535851937790883648493

    def fixture(self):
        keys = json.loads(KEYS.read_text(encoding="utf-8"))
        cert = certificate()
        return base64.b64decode(keys["keys"][0]["public_key"]), cert["manifest"].encode("utf-8"), \
            base64.b64decode(cert["signature"])

    def test_the_rfc_8032_test_vectors(self):
        for public, message, signature in self.VECTORS:
            self.assertTrue(_ed25519.verify(bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)))

    def test_the_platforms_signature_and_every_flipped_bit(self):
        public, message, signature = self.fixture()
        self.assertTrue(_ed25519.verify(public, message, signature))
        self.assertEqual("ed25519:" + hashlib.sha256(public).hexdigest()[:16],
                         json.loads(message)["key_id"], "the key id is derived from the key")
        for bit in range(len(signature) * 8):
            flipped = bytearray(signature)
            flipped[bit // 8] ^= 1 << (bit % 8)
            self.assertFalse(_ed25519.verify(public, message, bytes(flipped)), f"bit {bit}")
        self.assertFalse(_ed25519.verify(public, message + b" ", signature))
        self.assertFalse(_ed25519.verify(bytes.fromhex(self.VECTORS[0][0]), message, signature))

    def test_what_the_platforms_library_refuses_is_refused(self):
        public, message, signature = (bytes.fromhex(v) for v in self.VECTORS[0])
        s = int.from_bytes(signature[32:], "little")
        self.assertFalse(_ed25519.verify(public, message, signature[:32] + (s + self.L).to_bytes(32, "little")),
                         "S must be below the group order")
        neutral = (1).to_bytes(32, "little")
        self.assertFalse(_ed25519.verify(neutral, message, neutral + bytes(32)),
                         "a public key of small order would let anyone sign")
        self.assertFalse(_ed25519.verify(public[:31], message, signature))
        self.assertFalse(_ed25519.verify(public, message, signature[:63]))
        self.assertFalse(_ed25519.verify(b"\xff" * 32, message, signature), "no point at all")

    def test_against_an_independent_implementation_when_one_is_installed(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        except ImportError:
            self.skipTest("the cryptography package is not installed")
        for i in range(25):
            key = Ed25519PrivateKey.generate()
            public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            message = os.urandom(i * 7)
            signature = key.sign(message)
            self.assertTrue(_ed25519.verify(public, message, signature))
            forged = bytearray(signature)
            forged[i % 64] ^= 0x80
            self.assertFalse(_ed25519.verify(public, message, bytes(forged)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
