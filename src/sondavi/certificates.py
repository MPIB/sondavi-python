"""Certified datasets: checking one, and leaving columns or rows out of it.

A certified dataset is a CSV file plus `<name>.certificate.json`, both issued by the platform.
The certificate is signed by the platform, not by the researcher who shares the file - which is
the point: a reviewer, a journal or a co-author can check that every cell is what participants
entered without having to trust whoever hands the file over.

    from sondavi import verify

    result = verify("study.csv", "study.certificate.json")
    print(result)                 # what was checked, and what is missing or was added
    if not result:
        ...                       # result.status says why

This is format version 1, checked exactly as the platform's own /verify page checks it: the same
statuses, in the same order, with the same messages.

How it works, in short: every cell has a salted hash (its "leaf"); the certificate's signed
manifest holds one sum over the leaves of every column and one over the leaves of every row. A
changed cell changes one column sum and one row sum, and the crossing names it. Salts make sure
that the hash of a cell left out says nothing about its value, even for a column with only two
possible answers.

There is a second kind: a certified *file*. The platform compared a file the researcher already
had - CSV, Excel or JSON - with the stored responses and signed the SHA-256 of its bytes together
with what the comparison found. Checking it means hashing the file, nothing else; the file is
never read as a table, so its format does not matter.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib.resources
import json
import numbers
import os
import re
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Union

from . import _ed25519
from .client import _USER_AGENT

__all__ = ["verify", "Verification", "DEFAULT_TRUST"]

PathLike = Union[str, "os.PathLike[str]"]

# Only the platform itself, unless you say otherwise. Anyone can run an installation with a
# key and an address of their own; if every issuer were accepted, anyone could issue
# "genuine" certificates. Trusting another installation is a decision, so it is explicit.
DEFAULT_TRUST = ("https://sondavi.io",)

_TYPE = "sondavi-certificate"
_MANIFEST_TYPE = "sondavi-certificate-manifest"
_VERSION = 1
_SALT_BYTES = 16
_SEED_BYTES = 32
_LEAF_BYTES = 32
_KEYS_PATH = "/.well-known/sondavi-certificate-keys.json"
# The keys of https://sondavi.io as published when this version of the package was made: a
# saved copy of its keys document, shipped inside the package.
_BUILTIN_KEYS = "certificate-keys.json"

_HEX = re.compile(r"[0-9a-fA-F]+")

# The messages are the platform's, word for word: a file must not read one way on the
# website and another way here.
_NOT_A_CERTIFICATE = "This is not a certificate of a supported version."
_NO_MANIFEST = "The certificate does not contain a readable manifest."
_NOT_WELL_FORMED = "The data file is not a well-formed CSV file."
_UNREADABLE_RECORD = "The certificate's record could not be read."
_NOT_THE_FILE = ("This is not the file that was certified: its fingerprint differs. "
                 "Any change counts, even saving it again.")

# How the platform words a response's status where a person reads it.
_STATUS_WORDS = {"complete": "complete", "partial": "incomplete", "screened_out": "screened out",
                 "unknown": "of unknown status"}
_FORMAT_NAMES = {"csv": "CSV", "xlsx": "Excel", "json": "JSON"}
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_DELIMITER_NAMES = {",": "commas", ";": "semicolons", "tab": "tabs"}


# ── the format ───────────────────────────────────────────────────────────────


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def _text(value: str) -> bytes:
    """The bytes of a cell or column name as the platform hashed them.

    A data file that is not UTF-8 is refused before anything is hashed; `surrogateescape` only
    keeps a name the certificate spells with an escaped lone surrogate from stopping the check
    with an encoding error - it then fails as not matching, the way the platform sees it.
    """
    return value.encode("utf-8", "surrogateescape")


def _name(column: str) -> bytes:
    """A column name with its length in front, as it enters every leaf and the column sum."""
    raw = _text(column)
    return _u32(len(raw)) + raw


def _leaf(salt: bytes, name: bytes, value: str) -> bytes:
    """SHA-256(salt, u32 length, column, u32 length, value); `name` comes from `_name()`."""
    raw = _text(value)
    return hashlib.sha256(salt + name + _u32(len(raw)) + raw).digest()


def _salt(seed: bytes, row: int) -> bytes:
    """The salt of an original row in a column with a seed: HMAC-SHA-256(seed, u32(row)), 16 bytes."""
    return hmac.digest(seed, _u32(row), "sha256")[:_SALT_BYTES]


def _row_root(row: int, leaves: list[bytes]) -> bytes:
    return hashlib.sha256(b"sondavi-row" + _u32(row) + b"".join(leaves)).digest()


def _column_hash(name: bytes) -> Any:
    return hashlib.sha256(b"sondavi-col" + name)


def _key_id(public_key: bytes) -> str:
    """`ed25519:` and the first 16 hex digits of SHA-256 over the raw public key."""
    return "ed25519:" + hashlib.sha256(public_key).hexdigest()[:16]


def _hex(value: Any, length: int) -> bytes:
    """Hex of exactly `length` bytes, or the record is unreadable.

    Strictly, as the platform reads it: `bytes.fromhex` alone would let spaces through, and a
    salt or leaf of the wrong length is no salt or leaf of this format.
    """
    if not isinstance(value, str) or len(value) != 2 * length or not _HEX.fullmatch(value):
        raise ValueError("not a hex string of the right length")
    return bytes.fromhex(value)


def _base64(value: str) -> bytes | None:
    """The signature, read as leniently as the platform reads it: whitespace and missing padding are fine."""
    compact = re.sub(r"\s+", "", value)
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None


# ── the CSV file ─────────────────────────────────────────────────────────────


# A control character in a header name makes the file unreadable: U+0000 to U+001F and U+007F.
_CONTROL = re.compile("[\x00-\x1f\x7f]")


class _NotWellFormed(ValueError):
    """The data file is not CSV the way every verifier reads it."""


def _parse_csv(text: str, delimiter: str = ",") -> tuple[list[str], list[list[str]]]:
    """Header and data rows of a CSV text, read strictly - exactly as the platform reads it.

    RFC 4180 with nothing tolerated that another program could read differently, because a
    file R or pandas reads one way and the verifier another could show values that were never
    certified. One leading byte order mark is removed (a second one is part of the first
    name). NUL anywhere, a carriage return outside quotes that does not end a line, a quote
    inside an unquoted field, text after a closing quote, an unterminated quote and a control
    character in a header name make the file unreadable: `_NotWellFormed`.

    A cell is exactly the text between the delimiters: nothing is trimmed or converted, "007"
    stays "007", and a line break inside quotes stays what it was (CRLF remains CRLF). A
    delimiter right before the end of the file adds one empty field; the last line needs no
    line break. Empty lines do not count, before the header too - except in a file whose header
    has one column, where an empty line is one empty cell. ``""`` is not an empty line.

    The rules are shared with the platform, the browser and R: tests/fixtures/certificates/
    csv-cases.json. Written out rather than left to the csv module, which reads most of these
    files leniently in its own way.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\0" in text:
        raise _NotWellFormed(_NOT_WELL_FORMED)
    unquoted = re.compile('[^"\r\n' + re.escape(delimiter) + "]*")
    n = len(text)
    records: list[tuple[list[str], bool]] = []
    record: list[str] = []
    blank = True        # the record so far is one empty, unquoted field
    i = 0
    while i < n or record:
        quoted = i < n and text[i] == '"'
        if quoted:
            parts = []
            i += 1
            while True:
                close = text.find('"', i)
                if close < 0:
                    raise _NotWellFormed(_NOT_WELL_FORMED)        # an unterminated quote
                parts.append(text[i:close])
                if text.startswith('"', close + 1):                 # "" inside: one quote
                    parts.append('"')
                    i = close + 2
                    continue
                i = close + 1
                break
            value = "".join(parts)
        else:
            match = unquoted.match(text, i)     # up to a quote too: see the end of the loop
            value = match.group()
            i = match.end()
        record.append(value)
        blank = blank and not quoted and value == "" and len(record) == 1

        if i >= n:
            records.append((record, blank))
            break
        char = text[i]
        if char == delimiter:
            blank = False
            i += 1
            if i >= n:
                record.append("")           # a delimiter right before the end: one more, empty field
                records.append((record, False))
                break
            continue
        if char == "\r":
            if not text.startswith("\n", i + 1):
                raise _NotWellFormed(_NOT_WELL_FORMED)            # a carriage return alone
            i += 2
        elif char == "\n":
            i += 1
        else:
            # A quote inside an unquoted field, or text after a closing quote.
            raise _NotWellFormed(_NOT_WELL_FORMED)
        records.append((record, blank))
        record = []
        blank = True

    header: list[str] | None = None
    rows: list[list[str]] = []
    for cells, is_blank in records:
        if header is None:
            if not is_blank:
                header = cells
            continue
        if is_blank and len(header) != 1:
            continue
        rows.append(cells)
    if any(_CONTROL.search(name) for name in header or []):
        raise _NotWellFormed(_NOT_WELL_FORMED)
    return header or [], rows


def _read_csv(path: PathLike) -> tuple[list[str], list[list[str]]]:
    """Header and data rows of a certified CSV file: UTF-8, read strictly (see `_parse_csv`).

    Bytes that are not UTF-8 make the file unreadable, as on the platform.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _NotWellFormed(_NOT_WELL_FORMED) from None
    return _parse_csv(text)


def _csv_cell(value: str) -> str:
    """A cell in quotes when it holds a comma, a quote or a line break, as csv.QUOTE_MINIMAL does.

    Written out rather than left to csv.writer: before Python 3.11 that leaves a lone carriage
    return unquoted when the line ends in "\\n", and the cell then breaks into a row of its own.
    """
    if any(c in value for c in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _write_csv(path: PathLike, header: list[str], rows: Iterable[list[str]]) -> None:
    """UTF-8 with a BOM (so a spreadsheet program shows umlauts right), comma, quotes only where needed."""
    with open(path, "w", encoding="utf-8-sig", errors="surrogateescape", newline="") as handle:
        for row in [header, *rows]:
            # A row of one empty cell would be an empty line, which readers skip.
            handle.write('""\n' if row == [""] else ",".join(_csv_cell(value) for value in row) + "\n")


def _cell(row: list[str], at: int) -> str:
    """A cell of a row; past the row's end, empty. A checked file has no such rows - every row is
    as wide as the header - so this only keeps a broken record from reading past a row."""
    return row[at] if at < len(row) else ""


# ── the result ───────────────────────────────────────────────────────────────


@dataclass
class Verification:
    """What checking a certified dataset found.

    `status` is one of, in the order they are checked:

    * ``"malformed"`` - not a certificate this package can read;
    * ``"untrusted_issuer"`` - issued by an installation that is not in `trust`; nothing
      else was checked;
    * ``"unknown_key"`` - the issuer does not publish the key it was signed with;
    * ``"invalid_signature"`` - the certificate itself was altered;
    * ``"unverifiable"`` - the file does not fit the certificate (a certified column or a row
      missing, removed by hand instead of certifying the changed file again; a row wider or
      narrower than the header) or is not CSV as every verifier reads it; `problems` says what;
    * ``"modified"`` - the certificate is genuine, but cells differ: `modified_cells`;
    * ``"valid"`` - every certified cell is what it was when the certificate was issued.

    `modified_cells` are ``{"row": ..., "column": ...}``, the row counted as the file's data
    rows from 1 (the header not counted). When cells differ in more than one row AND more
    than one column, `suspect` is True: the certificate holds one sum per row and one per
    column, not one per cell, so the listed cells are every crossing of a changed row and a
    changed column - each a candidate, not each changed.

    `added_columns` are the file's columns that are not certified: appended ones, a column
    the certificate lists as left out that is back in the file, and the columns the platform
    computed when it wrote the file. `column_groups()` sorts the columns as the platform's
    /verify page does - certified, in the file but not certified (and why), missing - and the
    printed summary follows it.

    For a certified *file* (`basis` is not None) there are no cells to name: the file is either
    the one that was certified, byte for byte, or "modified". `removed_columns`, `removed_rows`
    and `added_columns` then repeat what the platform's comparison found - columns of the survey
    not in the file, responses not in it, and the file's own columns, which were not checked.

    A Verification is true exactly when the file is valid, so ``if verify(...):`` works.
    """

    status: str
    manifest: dict | None = field(default=None, repr=False)
    removed_columns: list[str] = field(default_factory=list)
    removed_rows: int = 0
    added_columns: list[str] = field(default_factory=list)
    modified_cells: list[dict] = field(default_factory=list)
    suspect: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def __bool__(self) -> bool:
        return self.valid

    def _get(self, key: str) -> Any:
        return self.manifest.get(key) if isinstance(self.manifest, dict) else None

    @property
    def certificate(self) -> str | None:
        """The certificate's id."""
        return self._get("id")

    @property
    def issuer(self) -> dict | None:
        """``{"name": ..., "url": ...}`` of the installation that issued it."""
        return self._get("issuer")

    @property
    def organization(self) -> str | None:
        return self._get("organization")

    @property
    def survey(self) -> dict | None:
        """id, title, version of the questionnaire, SHA-256 of its definition, privacy level."""
        return self._get("survey")

    @property
    def issued_at(self) -> str | None:
        """When it was issued: ISO 8601 in UTC, as the manifest has it."""
        return self._get("issued_at")

    @property
    def basis(self) -> dict | None:
        """For a certified file, what the platform's comparison found; None for a certificate of cells.

        Among it: `file` (name, sha256, bytes, format), `responses_total`, `responses_in_file`,
        `missing` (count, by_status, by_group, group_unknown), `columns_checked`,
        `columns_not_in_file`, `columns_own`, `columns_derived` (computed, not compared),
        `browser_reported`, `offline_synced`, `written_differently` (column: cells),
        `compared_as`, `row_order`, `matched_by` and, for a file certified from an earlier
        certificate, `derived_from`.
        """
        return _whole_file_basis(self.manifest)

    def _issuer_url(self) -> str:
        issuer = self.issuer
        return str(issuer.get("url", "")) if isinstance(issuer, dict) else ""

    def __str__(self) -> str:
        problem = self.problems[0] if self.problems else ""
        if self.status == "malformed":
            return f"Not a certificate: {problem}"
        if self.status == "untrusted_issuer":
            url = self._issuer_url() or "an unnamed issuer"
            # "names", not "was issued by": the signature is checked only for a trusted issuer.
            return (f"Untrusted issuer: the certificate names {url} as its issuer, which is not among "
                    f"the trusted issuers, so nothing was checked. If you trust that installation, "
                    f"pass trust=[\"{url}\"].")
        if self.status == "unknown_key":
            return (f"Unknown key: the certificate says it was signed with the key {self._get('key_id')}, "
                    f"which is not among the keys of {self._issuer_url()}.")
        if self.status == "invalid_signature":
            return ("Invalid signature: the certificate has been altered since it was issued, "
                    "or it was not issued by the installation it names.")
        if self.status == "unverifiable":
            return f"Cannot be verified: {problem}"
        if self.status == "modified":
            return problem if self.basis is not None else self._modified()
        if self.status == "valid":
            return self._valid_file() if self.basis is not None else self._valid()
        return f"{self.status}: {problem}".rstrip(": ")

    @staticmethod
    def _lines(head: str, pairs: list[tuple[str, Any]]) -> str:
        """The headline, then one aligned line per label - as wide as the longest label shown."""
        width = max((len(label) for label, _ in pairs), default=0) + 2
        return "\n".join([head] + [f"  {label + ':':<{width}}{value}" for label, value in pairs])

    def column_groups(self) -> dict[str, list[str]]:
        """The file's columns as the platform's /verify page sorts them, each in one place.

        ``certified``; in the file but not certified: ``left_out`` (left out when the certificate
        was issued, back in this copy), ``computed`` (worked out by the platform when the file was
        made) and ``added``; missing from the file: ``missing_left_out`` (left out when the
        certificate was issued) and ``missing_not_in_file`` (a certified file: columns of the
        survey's data it does not hold); ``browser`` - certified columns the participant's browser
        sent. The same rule as resources/js/certificates/columns.js on the platform.
        """
        basis = self._get("basis")
        basis = basis if isinstance(basis, dict) else {}
        computed = _texts(basis.get("columns_derived"))
        removed, added = list(self.removed_columns), list(self.added_columns)
        if basis.get("kind") == "file":
            certified = _texts(basis.get("columns_checked"))
            groups = {"certified": certified, "left_out": [], "computed": computed,
                      "added": [n for n in added if n not in computed],
                      "missing_left_out": [], "missing_not_in_file": removed}
        else:
            columns = self._get("columns")
            names = [str(c.get("name")) for c in columns if isinstance(c, dict)] if isinstance(columns, list) else []
            certified = [n for n in names if n not in removed]
            groups = {"certified": certified,
                      "left_out": [n for n in added if n in removed and n not in computed],
                      "computed": [n for n in added if n in computed],
                      "added": [n for n in added if n not in removed and n not in computed],
                      "missing_left_out": [n for n in removed if n not in added and n not in computed],
                      "missing_not_in_file": []}
        groups["browser"] = [n for n in _texts(basis.get("browser_reported")) if n in certified]
        return groups

    def _columns(self, line: Any, whole_file: bool) -> None:
        """Certified, in the file but not certified, missing from the file - worded as on /verify."""
        g = self.column_groups()
        added = "not part of the survey's data, added by the researcher" if whole_file \
            else "added after the certificate was issued"
        line("Not certified", _grouped([
            (g["left_out"], "left out when the certificate was issued, so it cannot check them"),
            (g["computed"], "worked out by the platform when the file was made, never certified"),
            (g["added"], added)]))
        line("Missing from file", _grouped([
            (g["missing_left_out"], "left out when the certificate was issued"),
            (g["missing_not_in_file"], "in the survey's data, not in this file")]))
        if g["browser"]:
            line("Sent by the browser", ", ".join(g["browser"]) + " - certified as stored, but sent by the "
                 "participant's browser rather than measured by the platform")
        basis = self._get("basis")
        offline = basis.get("offline_synced") if isinstance(basis, dict) else None
        if isinstance(offline, int) and not isinstance(offline, bool) and offline > 0:
            line("From offline devices", f"{offline} {'response' if offline == 1 else 'responses'} collected "
                 f"offline; the time they were completed is the device's")

    def _origin(self, line: Any) -> None:
        """Survey, organisation, collection period, issuer: the same for both kinds."""
        survey = self.survey if isinstance(self.survey, dict) else {}
        details = [f"version {survey['version']} of the questionnaire"] if survey.get("version") is not None else []
        if survey.get("privacy"):
            details.append(f"privacy: {survey['privacy']}")
        issuer = self.issuer if isinstance(self.issuer, dict) else {}
        responses = self._get("responses")
        responses = responses if isinstance(responses, dict) else {}
        line("Survey", str(survey.get("title", "")) + (f" ({', '.join(details)})" if details else ""))
        line("Organisation", self.organization)
        if responses.get("first") or responses.get("last"):
            line("Responses", f"{responses.get('first')} to {responses.get('last')}")
        line("Issuer", f"{issuer.get('name')}, {issuer.get('url')}")
        line("Issued", f"{self.issued_at}, certificate {self.certificate}")

    def _valid(self) -> str:
        columns = self._get("columns")
        rows = self._get("rows")
        pairs: list[tuple[str, Any]] = []

        def line(label: str, value: Any) -> None:
            pairs.append((label, value))

        self._origin(line)
        if isinstance(rows, int) and isinstance(columns, list):
            line("Certified", f"{len(self.column_groups()['certified'])} columns x {rows - self.removed_rows} rows, "
                              f"every value as participants entered it")
        self._columns(line, whole_file=False)
        line("Removed rows", self.removed_rows)
        return self._lines("Valid: every certified cell matches.", pairs)

    def _valid_file(self) -> str:
        basis = self.basis or {}
        file = basis.get("file") if isinstance(basis.get("file"), dict) else {}
        missing = basis.get("missing") if isinstance(basis.get("missing"), dict) else {}
        pairs: list[tuple[str, Any]] = []

        def line(label: str, value: Any) -> None:
            pairs.append((label, value))

        self._origin(line)
        fmt = file.get("format") if isinstance(file.get("format"), dict) else {}
        kind = _FORMAT_NAMES.get(fmt.get("type"))
        derived = basis.get("derived_from") if isinstance(basis.get("derived_from"), dict) else None
        # The platform's own words: a file is compared either with the stored responses or, when
        # those are gone, with a file certified earlier.
        line("How", f"Certified from the researcher's own {kind + ' ' if kind else ''}file, after every "
                    f"value was compared with {'a file certified earlier' if derived else 'the stored responses'}")
        if derived:
            earlier = f"certificate {str(derived.get('certificate') or '')[:8]} of {_day(derived.get('issued_at'))}"
            line("Derived from", earlier + (f" ({derived['file']})" if derived.get("file") else ""))
        line("File", file.get("name"))
        # How the platform read the file and matched its rows: a reviewer's program should read it alike.
        line("Read as", _read_as(file.get("format")))
        line("Rows matched", "by response ID" if basis.get("matched_by") == "response_id"
             else "by their content (the file has no response ID)")
        line("In the file", f"{basis.get('responses_in_file')} of {basis.get('responses_total')} "
                            f"responses of this survey")
        line("Left out", _left_out_words(self.removed_rows, missing))
        line("Certified", f"{len(self.column_groups()['certified'])} columns, every value compared with what "
                          f"participants entered")
        self._columns(line, whole_file=True)
        differently = basis.get("written_differently")
        differently = differently if isinstance(differently, dict) else {}
        line("Written differently",
             (", ".join(f"{name} ({n} {'cell' if n == 1 else 'cells'})" for name, n in differently.items())
              + " - the same value in another notation") if differently else "none")
        line("Row order", basis.get("row_order"))
        return self._lines("Valid: this is the file that was certified, unchanged to the last byte.", pairs)

    def _modified(self) -> str:
        cells = self.modified_cells
        if not cells:
            return "Modified: " + (" ".join(self.problems) or "the certificate's record was altered.")
        if self.suspect:
            rows = len({c["row"] for c in cells})
            columns = len({c["column"] for c in cells})
            lines = [f"Modified: the certificate is genuine, but cells differ in {rows} rows and "
                     f"{columns} columns of the file. The certificate holds one sum per row and one "
                     f"per column, not one per cell, so these {len(cells)} cells are suspects - "
                     f"each lies in a changed row and a changed column, not each has changed:"]
        else:
            lines = [f"Modified: the certificate is genuine, but {len(cells)} "
                     f"{'cell differs' if len(cells) == 1 else 'cells differ'} from what was certified:"]
        lines += [f"  row {c['row']}, column {c['column']}" for c in cells]
        lines += self.problems
        lines.append("(Rows are the file's data rows, counted from 1 below the header.)")
        return "\n".join(lines)


def _read_as(fmt: Any) -> str:
    """How the platform read the file, worded as on its /verify page."""
    if not isinstance(fmt, dict) or not fmt:
        return "unknown"
    if fmt.get("type") == "xlsx":
        return "Excel" + (f', sheet "{fmt["sheet"]}"' if fmt.get("sheet") is not None else "")
    if fmt.get("type") == "json":
        return "JSON, one object per response"
    words = ["CSV"]
    delimiter = fmt.get("delimiter")
    if delimiter is not None:
        named = _DELIMITER_NAMES.get(delimiter) if isinstance(delimiter, str) else None
        words.append("values separated by " + (named or str(delimiter)))
    if fmt.get("encoding") is not None:
        words.append(str(fmt["encoding"]))
    return ", ".join(words)


def _grouped(groups: list[tuple[list[str], str]]) -> str:
    """'none', or the count and each non-empty group as "why: a, b", joined by semicolons."""
    shown = [(names, why) for names, why in groups if names]
    if not shown:
        return "none"
    count = sum(len(names) for names, _ in shown)
    return f"{count} - " + "; ".join(f"{why}: {', '.join(names)}" for names, why in shown)


def _texts(value: Any) -> list[str]:
    """A list of names from the manifest, as text; anything else is no list."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _left_out_words(count: int, missing: dict) -> str:
    """How many responses of the survey are not in the file, by status and by group."""
    if not count:
        return "none"
    text = f"{count} {'response' if count == 1 else 'responses'}"
    by_status = missing.get("by_status") if isinstance(missing.get("by_status"), dict) else {}
    statuses = [f"{n} {_STATUS_WORDS.get(status, status)}" for status, n in by_status.items() if n]
    if statuses:
        text += ": " + ", ".join(statuses)
    by_group = missing.get("by_group") if isinstance(missing.get("by_group"), dict) else {}
    groups = [f"{group} {n}" for group, n in by_group.items() if n]
    # Responses whose group no file names: left out of a file certified from an earlier one.
    unknown = missing.get("group_unknown")
    unknown = unknown if isinstance(unknown, int) and not isinstance(unknown, bool) else 0
    if groups:
        text += "; by group: " + ", ".join(groups)
        if unknown > 0:
            text += f", and {unknown} of a group no file names"
    elif unknown > 0:
        text += f"; by group: {unknown} of a group no file names"
    return text


def _day(iso: Any) -> str:
    """The day of an ISO 8601 time as "20 October 2026", or the text as it came.

    In UTC, as the platform's /verify page shows it: a time given with an offset late in the
    evening falls on the next day there. Python 3.9 reads no "Z" and no fraction of a second
    other than 3 or 6 digits; such a time still gives its day, as written."""
    if not isinstance(iso, str):
        return str(iso)
    try:
        moment = datetime.fromisoformat(re.sub(r"[zZ]$", "+00:00", iso.strip()))
    except ValueError:
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})(?!\d)", iso.strip())
        try:
            moment = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None
        except ValueError:
            moment = None
    if moment is None:
        return iso
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}"


def _result(status: str, manifest: dict | None = None, problems: list[str] | None = None,
            **details: Any) -> Verification:
    return Verification(status=status, manifest=manifest, problems=problems or [], **details)


# ── verifying ────────────────────────────────────────────────────────────────


def verify(data: PathLike, certificate: PathLike | dict, trust: Iterable[str] | str = DEFAULT_TRUST,
           keys: PathLike | dict | None = None) -> Verification:
    """Check a certified dataset: is every certified cell what participants entered?

    `data` is the CSV file and `certificate` its `.certificate.json`, both as paths. The
    file, not a DataFrame: reading it into pandas has already converted the cells - "007"
    became 7, "NA" became missing, "-0" became 0 - and the certificate is over the text as
    it stands in the file. (`certificate` may also be the parsed JSON.) The file is read
    strictly, as the platform reads it: UTF-8, quotes as in RFC 4180, every row exactly as
    wide as the header. A file two programs could read differently - a stray quote, a lone
    carriage return, a NUL, a row with more fields than the header - is "unverifiable".

    A certified *file* - one the researcher had and the platform compared with its responses -
    may be CSV, Excel or JSON: it is hashed, not read, and is valid only when it is the very
    file that was certified. `result.basis` then says what the comparison found.

    `trust` are the issuers whose certificates count, as URLs. By default only the platform
    itself, https://sondavi.io; a certificate of another installation reports
    "untrusted_issuer" until you name it here.

    `keys` are the issuer's public keys. By default (None) the keys of https://sondavi.io
    that come with this package are tried first: a certificate signed with one of them is
    checked without a connection. Any other key - a newer one, or another installation's -
    is fetched from the issuer (`<issuer>/.well-known/sondavi-certificate-keys.json`), and
    only once the issuer is found among `trust`, so checking an untrusted certificate
    contacts no one. To check offline in every case, pass that document yourself: as a
    path, a URL, or the parsed dict. Keys you pass are the only ones used.

    Columns and rows the platform left out when it issued the file are allowed and named in
    the result, as are columns appended to the file - those are not certified. To share fewer
    columns or rows, certify the changed file again where the certificate was issued; deleting
    them by hand makes the file unverifiable. Opening the file in a spreadsheet program and
    saving it again rewrites numbers and dates ("007" becomes "7"), and the check reports
    that as modified.
    """
    return _verify(data, certificate, trust, keys)[0]


def _verify(data: PathLike, certificate: PathLike | dict, trust: Iterable[str] | str,
            keys: PathLike | dict | None) -> tuple[Verification, Any, list[str] | None, list[list[str]] | None]:
    """The check, handing back the certificate and the file it read.

    `_redact()` works on exactly what was checked: reading the files a second time would
    redact whatever they hold by then.
    """
    if not os.path.isfile(data):
        raise FileNotFoundError(f"No data file at {os.fspath(data)}.")
    try:
        cert = _load_json(certificate)
    except ValueError:      # not JSON (UnicodeDecodeError and JSONDecodeError are ValueErrors)
        cert = None

    if (not isinstance(cert, dict) or cert.get("type") != _TYPE
            or type(cert.get("version")) is not int or cert.get("version") != _VERSION
            or not isinstance(cert.get("manifest"), str) or not isinstance(cert.get("signature"), str)):
        return _result("malformed", problems=[_NOT_A_CERTIFICATE]), cert, None, None
    try:
        manifest = json.loads(cert["manifest"])
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict) or manifest.get("type") != _MANIFEST_TYPE:
        return _result("malformed", problems=[_NO_MANIFEST]), cert, None, None
    # A certified file has no cell record - its bytes are what was signed - so only a
    # certificate of cells must carry one.
    whole_file = _whole_file_basis(manifest) is not None
    if not whole_file and not isinstance(cert.get("proof"), (dict, list)):
        return _result("malformed", problems=[_NOT_A_CERTIFICATE]), cert, None, None

    issuer = manifest.get("issuer")
    issuer_url = issuer.get("url") if isinstance(issuer, dict) else None
    issuer_url = issuer_url.rstrip("/") if isinstance(issuer_url, str) else ""
    if issuer_url not in [url.rstrip("/") for url in _trust_list(trust)]:
        return _result("untrusted_issuer", manifest), cert, None, None

    key = _key_for(issuer_url, manifest.get("key_id"), keys)
    if key is None:
        return _result("unknown_key", manifest), cert, None, None
    signature = _base64(cert["signature"])
    # surrogatepass: a manifest that is no valid UTF-8 cannot be what was signed, and is
    # then rejected by the signature rather than by an encoding error.
    if (signature is None or len(signature) != 64
            or not _ed25519.verify(key, cert["manifest"].encode("utf-8", "surrogatepass"), signature)):
        return _result("invalid_signature", manifest), cert, None, None

    if whole_file:
        return _compare_whole_file(manifest, data), cert, None, None
    try:
        header, rows = _read_csv(data)
    except _NotWellFormed:
        return _result("unverifiable", manifest, [_NOT_WELL_FORMED]), cert, None, None
    proof = cert["proof"] if isinstance(cert["proof"], dict) else {}
    try:
        return _compare(manifest, proof, header, rows), cert, header, rows
    except Exception:       # noqa: BLE001 - anything the record does not allow, as the platform has it
        return _result("unverifiable", manifest, [_UNREADABLE_RECORD]), cert, header, rows


def _whole_file_basis(manifest: Any) -> dict | None:
    """The record of a certified file, or None for a certificate of cells."""
    basis = manifest.get("basis") if isinstance(manifest, dict) else None
    return basis if isinstance(basis, dict) and basis.get("kind") == "file" else None


def _file_sha256(path: PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _compare_whole_file(manifest: dict, data: PathLike) -> Verification:
    """The file is the certified one when its SHA-256 is the signed one; what it holds, the manifest says.

    The file is hashed, never parsed: whether it is CSV, Excel or JSON does not matter, and
    neither does what a reader would make of it.
    """
    basis = manifest["basis"]
    file = basis.get("file")
    if _file_sha256(data) != (file.get("sha256") if isinstance(file, dict) else None):
        return _result("modified", manifest, [_NOT_THE_FILE])
    missing = basis.get("missing")
    count = missing.get("count") if isinstance(missing, dict) else 0
    try:
        removed_rows = int(count or 0)
    except (TypeError, ValueError):
        removed_rows = 0
    return _result("valid", manifest, removed_columns=_names(basis.get("columns_not_in_file")),
                   removed_rows=removed_rows, added_columns=_names(basis.get("columns_own")))


def _names(value: Any) -> list[str]:
    return list(value) if isinstance(value, list) else []


def _compare(manifest: dict, proof: dict, header: list[str], rows: list[list[str]]) -> Verification:
    """Recompute every leaf, every column sum and every row sum, and compare with the manifest.

    The checks of the record come first and in the platform's order, so that a broken file
    gets the same explanation everywhere.
    """
    n = int(manifest["rows"])
    if not isinstance(manifest["columns"], list):
        raise TypeError("the manifest's columns are no list")
    columns = [c["name"] for c in manifest["columns"] if isinstance(c, dict) and "name" in c]
    proof_columns = proof.get("columns")
    proof_columns = [] if proof_columns is None else proof_columns
    proof_rows = proof.get("rows")
    proof_rows = [] if proof_rows is None else proof_rows
    if not isinstance(proof_columns, list) or not isinstance(proof_rows, list):
        raise TypeError("the record is not a list")

    def unverifiable(problem: str) -> Verification:
        return _result("unverifiable", manifest, [problem])

    if len(set(header)) != len(header):
        return unverifiable("The data file has duplicate column names.")
    # A row longer than the header has values no column names: R and pandas read such a file
    # shifted, and a verifier that passed over the surplus would call it valid.
    for k, row in enumerate(rows):
        if len(row) != len(header):
            return unverifiable(f"Data row {k + 1} has {len(row)} fields, the header {len(header)}.")
    if (len(proof_columns) != len(columns)
            or [e["name"] for e in proof_columns if isinstance(e, dict) and "name" in e] != columns):
        return unverifiable("The certificate's record does not match its manifest.")
    if len(proof_rows) != len(rows):
        return unverifiable(f"The file has {len(rows)} data rows, the certificate accounts for "
                            f"{len(proof_rows)}. To share fewer rows, certify the changed file again "
                            f"where the certificate was issued.")
    file_row_of: dict[int, int] = {}
    for k, original in enumerate(proof_rows):
        if type(original) is not int or original < 0 or original >= n or original in file_row_of:
            return unverifiable("The certificate's row list is invalid.")
        file_row_of[original] = k
    all_rows_present = len(file_row_of) == n
    position = {name: at for at, name in enumerate(header)}

    # Where each column's leaves come from: the record (a removed column), a seed, or one salt
    # - or, for a removed row, one leaf - per original row.
    removed_columns: list[str] = []
    sources: list[tuple] = []
    for entry in proof_columns:
        name = entry["name"]
        if entry.get("removed"):
            removed_columns.append(name)
            leaves = entry.get("leaves")
            leaves = [] if leaves is None else leaves
            if not isinstance(leaves, list) or len(leaves) != n:
                return unverifiable(f'The record of the removed column "{name}" is incomplete.')
            sources.append(("leaves", [_hex(leaf, _LEAF_BYTES) for leaf in leaves]))
            continue
        if name not in position:
            return unverifiable(f'The certified column "{name}" is missing from the file. '
                                f'To share fewer columns, certify the changed file again where the '
                                f'certificate was issued.')
        at = position[name]
        if entry.get("seed") is not None:
            if not all_rows_present:
                return unverifiable("The certificate's record is inconsistent with the removed rows.")
            sources.append(("seed", _hex(entry["seed"], _SEED_BYTES), at))
            continue
        salts = entry.get("salts")
        if not isinstance(salts, list) or len(salts) != n:
            return unverifiable(f'The record of the column "{name}" is incomplete.')
        decoded = []
        for i, item in enumerate(salts):
            is_leaf = str(item).startswith("-")
            # A present row with only a leaf would leave its value unchecked; a removed row
            # with a salt has no value to check.
            if (i in file_row_of) == is_leaf:
                return unverifiable(f'The record of the column "{name}" is inconsistent with the rows in the file.')
            decoded.append((True, _hex(item[1:], _LEAF_BYTES)) if is_leaf else (False, _hex(item, _SALT_BYTES)))
        sources.append(("salts", decoded, at))

    # Row by row, feeding each column's sum as we go: nothing but the sums stays in memory,
    # however large the file.
    names = [_name(entry["name"]) for entry in proof_columns]
    column_sums = [_column_hash(name) for name in names]
    row_roots = manifest.get("row_roots")
    bad_rows = []
    for i in range(n):
        leaves = []
        for c, source in enumerate(sources):
            if source[0] == "leaves":
                leaf = source[1][i]
            elif source[0] == "seed":
                leaf = _leaf(_salt(source[1], i), names[c], _cell(rows[file_row_of[i]], source[2]))
            else:
                is_leaf, raw = source[1][i]
                leaf = raw if is_leaf else _leaf(raw, names[c], _cell(rows[file_row_of[i]], source[2]))
            column_sums[c].update(leaf)
            leaves.append(leaf)
        expected = row_roots[i] if isinstance(row_roots, list) and i < len(row_roots) else None
        if _row_root(i, leaves).hex() != expected:
            bad_rows.append(i)
    bad_columns = [c for c, column_sum in enumerate(column_sums)
                   if column_sum.hexdigest() != manifest["columns"][c]["root"]]      # missing: unreadable

    # A column the certificate lists as left out, back in the file, is not certified: its
    # values are new, so it counts as added - otherwise invented values would pass unnoticed
    # under the name of a column that was removed.
    certified = {entry["name"] for entry in proof_columns if not entry.get("removed")}
    details = {
        "removed_columns": removed_columns,
        "removed_rows": n - len(file_row_of),
        "added_columns": [name for name in header if name not in certified],
    }
    if not bad_columns and not bad_rows:
        return _result("valid", manifest, **details)

    cells = []
    problem = None
    for i in bad_rows:
        for c in bad_columns:
            if i in file_row_of and not proof_columns[c].get("removed"):
                cells.append({"row": file_row_of[i] + 1, "column": proof_columns[c]["name"]})
            else:
                problem = "The certificate's record of removed data was altered."
    if not bad_columns or not bad_rows:
        problem = "The certificate's record was altered."
    cells.sort(key=lambda cell: (cell["row"], cell["column"]))
    return _result("modified", manifest, [problem] if problem else [], **details,
                   modified_cells=cells, suspect=len(bad_rows) > 1 and len(bad_columns) > 1)


def _trust_list(trust: Iterable[str] | str) -> list[str]:
    # A single URL is one issuer, not an iterable of characters.
    return [trust] if isinstance(trust, str) else [str(url) for url in trust]


def _load_json(source: PathLike | dict) -> Any:
    if isinstance(source, dict):
        return source
    with open(source, encoding="utf-8-sig") as handle:
        return json.load(handle)


def _key_for(issuer_url: str, key_id: Any, keys: PathLike | dict | None) -> bytes | None:
    """The public key the certificate names, or None when there is none.

    Keys the caller passed are the only ones used. Otherwise the keys that come with the
    package count first - only for their own issuer - and the issuer is asked only for a key
    they do not hold, so a certificate signed with a key known when this version was made
    needs no connection.
    """
    if not isinstance(key_id, str):
        return None
    if keys is not None:
        return _keys_in(_given_keys_document(keys)).get(key_id)
    built_in = _builtin_keys(issuer_url)
    if key_id in built_in:
        return built_in[key_id]
    try:
        fetched = _keys_in(_fetch_json(issuer_url + _KEYS_PATH))
    except RuntimeError as err:
        if not _builtin_issuer_is(issuer_url):
            raise
        raise RuntimeError(
            f"{err} This package holds the keys {issuer_url} had published when this version "
            f"was made; a certificate signed with a newer key needs a connection to {issuer_url} "
            f"or its keys passed with keys=."
        ) from err
    return {**fetched, **built_in}.get(key_id)


def _given_keys_document(keys: PathLike | dict) -> Any:
    if isinstance(keys, dict):
        return keys
    location = os.fspath(keys)
    if re.match(r"https?://", location, re.IGNORECASE):
        return _fetch_json(location)
    with open(location, encoding="utf-8-sig") as handle:
        return json.load(handle)


def _keys_in(document: Any, strict: bool = True) -> dict[str, bytes]:
    """The usable keys of a keys document, by id.

    A key counts only under its own id - "ed25519:" and the start of its SHA-256 - and only
    with 32 bytes. Anything else in the document is ignored, so a certificate naming such an
    entry reports an unknown key. The same rule holds for the keys that come with the package.
    """
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        if not strict:
            return {}
        raise ValueError("That is not a keys document: it has no list of keys. Expected the "
                         "issuer's /.well-known/sondavi-certificate-keys.json.")
    found = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("public_key"), str):
            continue
        raw = _base64(entry["public_key"])
        if raw is not None and len(raw) == 32 and entry.get("id") == _key_id(raw):
            found[entry["id"]] = raw
    return found


def _builtin_keys_document() -> Any:
    """The keys document of https://sondavi.io that comes with the package, or None.

    A file that is missing or cannot be read counts as no keys: verification then asks the
    issuer, as it would for any key the package does not hold. It must never be the reason a
    check fails.
    """
    try:
        text = importlib.resources.files(__package__ or "sondavi").joinpath(_BUILTIN_KEYS).read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        return None


def _builtin_issuer_is(issuer_url: str) -> bool:
    document = _builtin_keys_document()
    issuer = document.get("issuer") if isinstance(document, dict) else None
    url = issuer.get("url") if isinstance(issuer, dict) else None
    return isinstance(url, str) and url.rstrip("/") == issuer_url


def _builtin_keys(issuer_url: str) -> dict[str, bytes]:
    """The keys that come with the package, when they are the certificate's issuer's."""
    if not _builtin_issuer_is(issuer_url):
        return {}
    return _keys_in(_builtin_keys_document(), strict=False)


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as err:
        reason = getattr(err, "reason", None) or err
        raise RuntimeError(
            f"Could not fetch the issuer's keys from {url} ({reason}). To check offline, pass "
            f"them with keys= - the keys document as a file, or as a dict."
        ) from err


# ── redacting ────────────────────────────────────────────────────────────────


def _redact(data: PathLike, certificate: PathLike | dict, drop_columns: Iterable[str] | str = (),
            drop_rows: Iterable[int] | int = (), out_data: PathLike | None = None,
            out_certificate: PathLike | None = None, trust: Iterable[str] | str = DEFAULT_TRUST,
            keys: PathLike | dict | None = None) -> tuple[str, str]:
    """Leave columns and/or rows out of a certified dataset, keeping it verifiable.

    Not part of the package's interface: to share fewer columns or rows, certify the changed
    file again on the platform that issued the certificate. This stays as the reference for
    the documented format - how a record of cells is rewritten when cells are left out - and
    is checked against the platform's own redaction.

    Writes a new CSV without `drop_columns` (names) and `drop_rows` (the file's data rows,
    counted from 1 below the header), and a certificate that fits it. The signature stays
    the platform's - nothing here can sign - and checking the new pair shows what was left
    out: the names of the columns, the number of rows. Their values cannot be recovered
    from the new certificate, not even by guessing among a few possible answers: it keeps
    only salted hashes of removed cells, never their salts.

    The file must verify as valid first (`trust` and `keys` as for `verify()`); anything else
    is refused. `out_data` defaults to `<name>-redacted.csv` beside the original, and
    `out_certificate` to the same name ending in `.certificate.json`. Returns both paths.
    """
    data_path = os.fspath(data)
    check, cert, header, rows = _verify(data_path, certificate, trust, keys)
    if check.basis is not None:
        raise ValueError("This certificate covers a whole file, not single cells, so it holds no record "
                         "that could leave columns or rows out. Leave them out of your own file and "
                         "certify the changed file again on the platform.")
    if not check.valid:
        raise ValueError(f"{data_path} does not verify against its certificate ({check.status}), "
                         f"so it cannot be redacted - only a valid certified file can.\n{check}")
    assert header is not None and rows is not None

    drop_columns = [drop_columns] if isinstance(drop_columns, str) else list(drop_columns)
    drop_rows = [drop_rows] if isinstance(drop_rows, numbers.Integral) else list(drop_rows)
    position = {name: at for at, name in enumerate(header)}
    for name in drop_columns:
        if name not in position:
            raise ValueError(f'No column "{name}" in the file.')
    proof = cert["proof"]
    dropped_originals = set()
    for r in drop_rows:
        # numbers.Integral, so that row numbers taken from numpy or pandas work too.
        if not isinstance(r, numbers.Integral) or isinstance(r, bool) or not 1 <= r <= len(rows):
            raise ValueError(f"No data row {r} in the file.")
        dropped_originals.add(proof["rows"][int(r) - 1])

    if out_data is None:
        out_data = re.sub(r"(\.csv)?$", "-redacted.csv", data_path, count=1, flags=re.IGNORECASE)
    out_data = os.fspath(out_data)
    if out_certificate is None:
        out_certificate = re.sub(r"(\.csv)?$", "", out_data, count=1, flags=re.IGNORECASE) + ".certificate.json"
    out_certificate = os.fspath(out_certificate)
    inputs = [data_path] + ([] if isinstance(certificate, dict) else [os.fspath(certificate)])
    for target in (out_data, out_certificate):
        if any(os.path.exists(target) and os.path.samefile(target, source) for source in inputs):
            raise ValueError(f"{target} is one of the files being redacted; write the result elsewhere.")

    new_certificate = dict(cert)
    new_certificate["proof"] = _redact_proof(json.loads(cert["manifest"]), proof, header, rows,
                                             set(drop_columns), dropped_originals)
    dropped = set(drop_columns)
    kept_columns = [at for at, name in enumerate(header) if name not in dropped]
    kept_rows = [row for k, row in enumerate(rows) if proof["rows"][k] not in dropped_originals]
    _write_csv(out_data, [header[at] for at in kept_columns],
               ([_cell(row, at) for at in kept_columns] for row in kept_rows))
    # The manifest goes back as the very string that was signed. Parsing it and writing it
    # out again could change a single character - and with it the signature's message.
    with open(out_certificate, "w", encoding="utf-8") as handle:
        json.dump(new_certificate, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    return out_data, out_certificate


def _redact_proof(manifest: dict, proof: dict, header: list[str], rows: list[list[str]],
                  drop_columns: set[str], dropped_originals: set[int]) -> dict:
    """The record of the redacted file.

    A removed cell keeps only its leaf, never its salt. And a column whose rows are no longer
    all there gives up its seed for one salt per row: from the seed, anyone could recompute
    the salt of a removed row and try the few values a column like "gender" can take until
    the leaf matches.
    """
    n = int(manifest["rows"])
    position = {name: at for at, name in enumerate(header)}
    file_row_of = {original: k for k, original in enumerate(proof["rows"])}
    rows_will_be_complete = len(file_row_of) - len(dropped_originals) == n

    columns = []
    for entry in proof["columns"]:
        name = entry["name"]
        if entry.get("removed"):
            columns.append(entry)
            continue
        at = position[name]
        prefix = _name(name)
        seed = _hex(entry["seed"], _SEED_BYTES) if entry.get("seed") is not None else None

        def salt(i: int) -> bytes:
            return _salt(seed, i) if seed is not None else _hex(entry["salts"][i], _SALT_BYTES)

        def leaf_hex(i: int) -> str:
            return _leaf(salt(i), prefix, _cell(rows[file_row_of[i]], at)).hex()

        if name in drop_columns:
            columns.append({"name": name, "removed": True, "leaves": [
                leaf_hex(i) if i in file_row_of else entry["salts"][i][1:] for i in range(n)
            ]})
            continue
        if seed is not None and rows_will_be_complete:
            columns.append(entry)
            continue
        salts = []
        for i in range(n):
            if i not in file_row_of:
                salts.append(entry["salts"][i])         # removed before: already a leaf
            elif i in dropped_originals:
                salts.append("-" + leaf_hex(i))
            else:
                salts.append(salt(i).hex())
        columns.append({"name": name, "salts": salts})

    return {
        "rows": [original for original in proof["rows"] if original not in dropped_originals],
        "columns": columns,
    }
