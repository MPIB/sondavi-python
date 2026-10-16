"""Talking to the platform. Deliberately on the standard library alone, so an
analysis environment needs nothing installed to read its own data."""

from __future__ import annotations

import json
import math
import numbers
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _timezone
from typing import Any, Iterator

__all__ = ["connect", "Connection", "ApiError", "unnest", "markings"]

_USER_AGENT = "sondavi-python/0.7.0"

# The platform's own timestamps, which every study carries. They arrive as wall-clock
# strings without a zone ("2026-09-23 10:45:29"); left as text they sort lexically and
# compare against a datetime without complaining, which is the kind of mistake that
# reaches a paper.
# `screened_out_at` only comes for a study that can screen people out.
_TIMESTAMPS = ("completed_at", "started_at", "screened_out_at")

# What became of each response, in this order (the field `response_status`). In `frame()` a
# Categorical with all three categories, so a count shows 0 for nobody screened out.
RESPONSE_STATUSES = ("complete", "partial", "screened_out")


class ApiError(RuntimeError):
    """The platform refused, and said why."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def connect(base_url: str, token: str | None = None) -> "Connection":
    """Open a connection.

    `token` defaults to the SONDAVI_TOKEN environment variable, and that is
    where it belongs.
    """
    token = token if token is not None else os.environ.get("SONDAVI_TOKEN", "")
    if not base_url:
        raise ValueError("base_url is empty.")
    if not token:
        raise ValueError(
            "No token. Export it as\n"
            "    SONDAVI_TOKEN=sdv_...\n"
            "Create one under your account on the platform."
        )
    return Connection(base_url.rstrip("/"), token)


@dataclass
class Connection:
    base_url: str
    token: str = field(repr=False)

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, query: dict | None = None,
                 body: dict | None = None, max_tries: int = 4) -> Any:
        url = f"{self.base_url}/api/v1/{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})

        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(max_tries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as err:
                payload = err.read().decode(errors="replace")
                try:
                    message = json.loads(payload).get("message", "")
                except ValueError:
                    message = ""

                # The platform limits per token and says how long to wait.
                # Retrying without reading Retry-After turns a small limit into a
                # long outage — and getting this right is half the reason to ship
                # a package rather than let everyone write their own call.
                if err.code == 429 and attempt < max_tries - 1:
                    time.sleep(float(err.headers.get("Retry-After", 1)))
                    continue

                raise ApiError(err.code, _explain(err.code, message)) from None

        raise ApiError(429, "Still rate limited after several attempts.")

    # ── what a researcher asks for ───────────────────────────────────────────

    def surveys(self) -> list[dict]:
        """The studies this token may read."""
        return self._request("GET", "surveys")["data"]

    def codebook(self, survey_id: int) -> list[dict]:
        """Variable names, labels, types and value labels, as the API sends them.

        A matrix row the researcher marked "Reverse-scored" carries
        `reverse_scored: {"min": ..., "max": ...}`, the smallest and largest number on its
        scale; both are None when the scale has no numbers (marked, but nothing to reverse).
        Its reversed copy is (min + max) − answer, which `unnest()` adds as `<name>_r`. The
        copy is not a variable here: the API lists what its rows hold.
        """
        return self._request("GET", f"surveys/{survey_id}/codebook")["variables"]

    def responses(self, survey_id: int, since: str | None = None,
                  include_test: bool = False, page_size: int = 500,
                  max_rows: float = float("inf"), parse_dates: bool = True) -> list[dict]:
        """All responses, walking the pages for you.

        These are the same rows as the platform's JSON export, which means **partial
        responses are included** when the study saves them: someone who stopped halfway
        is a row whose `completed_at` is None. So is someone the study screened out - their
        row has `screened_out_at` and `screen_out_reason` instead. `len()` is therefore not
        the number of completed participations. Every row says what became of it in
        `response_status` - `"complete"`, `"partial"` or `"screened_out"`:
        `[r for r in rows if r["response_status"] == "complete"]`.

        Afterwards, `fingerprint()` gives the line that belongs next to a published
        result; for a citable dataset use `snapshot()`.
        """
        return list(self.iter_responses(survey_id, since, include_test, page_size, max_rows,
                                        parse_dates))

    def iter_responses(self, survey_id: int, since: str | None = None,
                       include_test: bool = False, page_size: int = 500,
                       max_rows: float = float("inf"), parse_dates: bool = True) -> Iterator[dict]:
        """The same, one row at a time — for studies too large to hold at once."""
        query = {"per_page": page_size, "since": since}
        if include_test:
            query["include_test"] = 1

        seen = 0
        cursor = None
        while True:
            if cursor is not None:
                query["cursor"] = cursor
            body = self._request("GET", f"surveys/{survey_id}/responses", query)

            if seen == 0:
                self.last_fingerprint = body["meta"].get("fingerprint")
                self.last_timezone = body["meta"].get("timezone") or "UTC"

            for row in body["data"]:
                if seen >= max_rows:
                    return
                seen += 1
                yield _with_real_dates(row, self.last_timezone) if parse_dates else row

            cursor = body["meta"].get("next_cursor")
            if cursor is None or not body["data"]:
                return

    def fingerprint(self) -> str:
        """The line that belongs next to a published result.

        A live query is not a dataset: run the same script tomorrow and it may return
        different rows. Record this, and a later run that differs is recognisable as a
        different dataset rather than a silent correction. For something citable, use
        `snapshot()`.
        """
        fp = getattr(self, "last_fingerprint", None)
        if not fp:
            raise RuntimeError("Nothing fetched yet — call responses() first.")

        return (f"{fp.get('rows')} rows, fetched {fp.get('generated_at')}, "
                f"digest {str(fp.get('digest', ''))[:16]}")

    def waves(self, survey_ids: list[int], names: list[str] | None = None,
              by: str = "respondent_id", **kwargs) -> list[dict]:
        """Join the waves of a study series into one row per person.

        Each wave's fields are suffixed with its name, so `mood` becomes `mood_wave1`
        and `mood_wave2` instead of colliding.

        **Everyone is kept, including those who did not take part in every wave** —
        their later fields are simply absent. That is a full outer join rather than an
        inner one on purpose: attrition is usually what a longitudinal design is about,
        and quietly dropping the people who stopped answering would remove exactly the
        cases you want to describe.

        The match needs an identifier, so the token must carry the respondent ability
        and the studies must not be anonymous.
        """
        if len(survey_ids) < 2:
            raise ValueError("Give at least two studies to join.")

        labels = names or [f"wave{i + 1}" for i in range(len(survey_ids))]
        if len(labels) != len(survey_ids):
            raise ValueError("`names` must have one entry per study.")

        joined: dict[Any, dict] = {}
        for survey_id, label in zip(survey_ids, labels):
            rows = self.responses(survey_id, **kwargs)

            if rows and by not in rows[0]:
                raise ApiError(200, (
                    f"Study {survey_id} returned no `{by}`, so its responses cannot be "
                    f"matched to another wave. Either the token was created without the "
                    f"respondent identifier, or the study is anonymous — in which case the "
                    f"answers are not linkable by design."
                ))

            for row in rows:
                target = joined.setdefault(row[by], {by: row[by]})
                for key, value in row.items():
                    if key != by:
                        target[f"{key}_{label}"] = value

        return list(joined.values())

    def frame(self, survey_id: int, **kwargs):
        """The responses as a pandas DataFrame, with the codebook applied.

        Categorical questions become pandas Categoricals with their real labels
        rather than bare codes — that is what a CSV cannot carry. A value the codebook has
        no label for (an answer to an option deleted after people had chosen it, a code
        added without a label) becomes a category of its own, named by the value, with a
        warning naming the column — not NaN, which would read as "not answered".

        The codebook is kept in `df.attrs["sondavi_codebook"]`, so that `unnest(df)` gives
        the matrix cells it spreads out the same treatment. Pass `unnest=True` to do that
        here: one column per cell, under the names the platform's own export writes, and
        the reversed copy `<name>_r` after a reverse-scored item.
        """
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover - depends on the environment
            raise RuntimeError("frame() needs pandas: pip install 'sondavi[pandas]'") from None

        spread = kwargs.pop("unnest", False)

        df = pd.DataFrame(self.responses(survey_id, **kwargs))
        if df.empty:
            return df

        # No date handling here: `responses()` already yields real datetimes, which pandas
        # turns into a datetime64 column by itself. Doing it twice raised
        # "Already tz-aware, use tz_convert" — the kind of error that only appears once
        # both halves are right.

        df = _with_status_categories(df)
        codebook = self.codebook(survey_id)
        df = _apply_codebook(df, codebook)
        df.attrs["mpi_fingerprint"] = getattr(self, "last_fingerprint", None)
        df.attrs["sondavi_codebook"] = codebook

        # Unnested after the codebook rather than before, so the cells are labelled by the
        # same code path as `unnest(con.frame(42))` - one way to get a column, one result.
        return unnest(df) if spread else df


def _explain(status: int, message: str) -> str:
    if status == 401:
        return ("The platform rejected the token (401). It may be unknown, expired or revoked — "
                "the answer is deliberately the same for all three. Check your account page.")
    if status == 404:
        return ("Not found (404). Either it does not exist, or this token was not given access "
                "to it — the platform does not distinguish the two on purpose.")
    if status == 403:
        return f"Refused (403): {message}"
    return f"The platform answered {status}. {message}".strip()


# ── turning what the platform sends into what Python works with ──────────────────────

def _zone(name: str | None):
    """The platform's time zone, without adding a dependency.

    `zoneinfo` is standard library but needs the `tzdata` package on Windows, which has
    no system zone database — so a named zone falls back to naive datetimes rather than
    making the whole package depend on it. The platform itself runs on UTC, which needs
    neither.
    """
    if not name or name.upper() == "UTC":
        return _timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def _with_real_dates(row: dict, timezone_name: str | None) -> dict:
    tz = _zone(timezone_name)

    for column in _TIMESTAMPS:
        value = row.get(column)
        if not isinstance(value, str):
            continue
        try:
            moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        row[column] = moment.replace(tzinfo=tz) if tz is not None else moment

    return row


def _with_status_categories(df):
    """`response_status` as a Categorical with all three categories, in their order. A value
    this version does not know stays, as a category of its own, with a warning."""
    if "response_status" not in df.columns:
        return df
    import pandas as pd

    seen = [v for v in pd.unique(df["response_status"]) if isinstance(v, str)]
    unknown = [v for v in seen if v not in RESPONSE_STATUSES]
    if unknown:
        warnings.warn(f"`response_status` holds {_quoted(unknown)}, which this version of the package "
                      "does not know. It is kept as a category of its own; a newer version may know "
                      "what it means.", stacklevel=3)
    df["response_status"] = pd.Categorical(df["response_status"], categories=[*RESPONSE_STATUSES, *unknown])
    return df


def unnest(rows, columns: list[str] | None = None, codebook: list[dict] | None = None):
    """Spread nested answers into one field each.

    Matrices and dynamic panels arrive nested, because flattening them on arrival would
    invent a shape the platform did not give. This produces exactly the fields the
    platform's own export writes, so a script built on the export file and one built on
    the API agree on the variable names.

    A matrix cell becomes `question.row.column`, an entry of a dynamic panel
    `question.0.field` — counting from zero, as the export does. A multiple-choice answer
    is a list of values and stays one field: there the list IS the answer.

    An image marking answer becomes one field per marking type, as in the export:
    painted cells as row runs ("12:31-40 13:30" = row 12, columns 31 to 40, and row 13,
    column 30), pins as "x,y" pairs. For a heatmap use `markings()` instead, on the rows
    before unnesting.

    `rows` is either what `responses()` returns or a DataFrame from `frame()`.

    With a codebook, a matrix row marked "Reverse-scored" gets its reversed copy
    `<name>_r` right after it, as in the platform's export file: (smallest + largest
    scale value) − answer, computed from the answer as given; an answer off the scale
    ("dk", empty) gives None there. The name is `<name>_r2`, `_r3` … when another variable
    is already called `<name>_r` — the platform's rule. A DataFrame from `frame()` carries
    its codebook, so this happens by itself, and the new columns also become Categoricals
    and numbers like the questions beside them; `codebook=[]` leaves them as stored. For
    rows, pass `codebook=con.codebook(42)`; dicts keep the codes, so only the copies are
    added.
    """
    pd = sys.modules.get("pandas")
    if pd is not None and isinstance(rows, pd.DataFrame):
        return _unnest_frame(rows, columns, codebook)

    copies = _reversed_copies(codebook or [])
    out = []

    for row in rows:
        flat: dict = {}
        for key, value in row.items():
            if columns is not None and key not in columns:
                flat[key] = value
                continue
            for name, leaf in _flatten_answer(value, key).items():
                flat[name] = leaf
                if name in copies and copies[name]["name"] not in row:
                    flat[copies[name]["name"]] = _reversed(leaf, copies[name]["sum"])
        out.append(flat)

    return out


def _unnest_frame(df, columns: list[str] | None, codebook: list[dict] | None):
    import pandas as pd

    codebook = df.attrs.get("sondavi_codebook") if codebook is None else codebook
    nested = [c for c in df.columns
              if df[c].dtype == object and df[c].map(lambda v: isinstance(v, (dict, list))).any()]
    targets = nested if columns is None else [c for c in nested if c in columns]
    if not targets:
        return df

    parts = []
    for column in df.columns:
        if column not in targets:
            parts.append(df[[column]])
            continue
        # A missing answer contributes no fields — the rows path leaves the key out, and a
        # stray column of None named after the question would be a variable nobody asked.
        flats = [{} if _is_missing(v) else _flatten_answer(v, column) for v in df[column]]
        names = list(dict.fromkeys(k for flat in flats for k in flat))
        # From the dicts, so pandas infers the types exactly as it does for frame()'s own rows.
        spread = pd.DataFrame(flats, columns=names, index=df.index)
        # The codebook used to reach the cells only when it happened to be applied after
        # unnesting; this way a cell gets what a question of its own gets, reversed copy
        # from the raw answer first.
        if codebook:
            spread = _apply_codebook(spread, codebook)
        parts.append(spread)

    out = pd.concat(parts, axis=1)
    out.attrs = dict(df.attrs)
    return out


def _flatten_answer(value: Any, prefix: str) -> dict:
    # An image marking is one answer with its own column rule, not a tree of fields: the
    # export writes one column per marking type, and so does this.
    if _is_image_marking(value):
        return _image_marking_columns(value, prefix)
    # So is an AI interview: the export's columns, not one per message (see conversations()).
    if _is_interview_answer(value):
        return _interview_columns(value, prefix)

    if not isinstance(value, (dict, list)) or not value:
        return {prefix: value}

    # A list of scalars is a single answer (checkbox, ranking, tagbox).
    if isinstance(value, list) and not isinstance(value[0], (dict, list)):
        return {prefix: value}

    pairs = value.items() if isinstance(value, dict) else enumerate(value)

    # Every key, those starting with "__" included: this is the raw answer, and a field the
    # platform stored inside it is data, not noise. They used to be dropped here - and only
    # here, so the R package and this one disagreed about what an answer holds.
    flat: dict = {}
    for key, child in pairs:
        flat.update(_flatten_answer(child, f"{prefix}.{key}"))

    return flat or {prefix: value}


# ── Applying the codebook ────────────────────────────────────────────────────────────────

def _apply_codebook(df, codebook: list[dict]):
    """The point of shipping a codebook: real Categoricals, right types. On the top level in
    frame() and on the new columns in unnest(), so a matrix cell is treated like a question
    of its own."""
    import pandas as pd

    # The reversed copies first, from the answers as given: once a column is a Categorical its
    # values are labels, and "Agree" has no arithmetic.
    df = _add_reversed_copies(df, codebook)

    for var in codebook:
        name = var.get("name")
        if name not in df.columns:
            continue
        column = df[name]
        # Nested answers are left alone, as in the R package. A multiple-choice answer is a
        # list; matched against the labels as text it used to become an empty Categorical.
        if column.dtype == object and column.map(lambda v: isinstance(v, (dict, list))).any():
            continue
        labels = var.get("value_labels") or []
        if labels:
            df[name] = _labelled(column, labels, name)
        elif var.get("type") == "numeric":
            df[name] = _numbers(column, name)
    return df


def _labelled(column, labels: list[dict], name: str):
    """A Categorical that keeps every answer.

    Matching against the value labels alone turned a value without one — an answer to an
    option deleted after people had chosen it, a "don't know" coded 99 and added later
    without a label — into NaN, and NaN reads as "not answered". Such a value becomes a
    category of its own, named by the value, after the labelled ones; the analyst hears
    about it once per column.
    """
    import pandas as pd

    mapping: dict[str, str] = {}
    for label in labels:
        # Two options with the same value: the first names it, as the platform lets the first
        # question define a shared variable.
        mapping.setdefault(_value_key(label.get("value")), label.get("text"))

    keys = [_value_key(v) for v in column]
    # An empty string is no answer, not an unknown one.
    keys = [None if k == "" and "" not in mapping else k for k in keys]
    unknown = list(dict.fromkeys(k for k in keys if k is not None and k not in mapping))
    if unknown:
        warnings.warn(
            f"`{name}` holds {'a value' if len(unknown) == 1 else 'values'} the codebook has no "
            f"label for: {_quoted(unknown)}. "
            f"{'It is kept as a category of its own' if len(unknown) == 1 else 'They are kept as categories of their own'}, "
            f"named by the value, rather than turned into NaN — usually an option deleted after "
            f"people had chosen it, or a code added without a label.",
            stacklevel=4,
        )

    texts = [None if k is None else mapping.get(k, k) for k in keys]
    categories = list(dict.fromkeys(list(mapping.values()) + unknown))
    return pd.Categorical(texts, categories=categories)


def _numbers(column, name: str):
    """A numeric variable as numbers. What is no number ("dk" in a rating, stray text) cannot
    stay in a numeric column, but it must not vanish into NaN unannounced either."""
    import pandas as pd

    out = pd.to_numeric(column, errors="coerce")
    lost = list(dict.fromkeys(str(v) for v, n in zip(column, out)
                              if pd.isna(n) and not _is_missing(v) and str(v) != ""))
    if lost:
        warnings.warn(
            f"`{name}` is numeric in the codebook but holds answers that are no numbers: "
            f"{_quoted(lost)}. They are NaN here; responses() gives the answers as stored.",
            stacklevel=4,
        )
    return out


def _quoted(values: list, at_most: int = 10) -> str:
    shown = ", ".join(f'"{v}"' for v in values[:at_most])
    return shown + (f" and {len(values) - at_most} more" if len(values) > at_most else "")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    # pandas' own markers, without importing pandas for the comparison.
    return type(value).__name__ in ("NAType", "NaTType")


def _value_key(value: Any) -> str | None:
    """What an answer is compared with the value labels by, written the way the codebook
    writes values.

    Matched as `str()`, a yes/no answer was "True" against the codebook's "true", and a
    numeric code in a column with one missing answer was "4.0" (pandas makes such a
    column float) against "4" — either way every answer of the column became NaN.
    """
    if _is_missing(value):
        return None
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return "true" if value else "false"
    if isinstance(value, numbers.Real):
        number = float(value)
        if number.is_integer() and abs(number) < 1e15:
            return str(int(number))
        return repr(number)
    return str(value)


# ── Reverse-scored items ─────────────────────────────────────────────────────────────────
#
# The platform stores an answer as it was given and mirrors it only when the data are read
# (its SPECS §75): the reversed copy `<name>_r` = (smallest + largest scale value) − answer.
# Its data files carry that column; the API does not, and names the range instead
# (`reverse_scored: {min, max}` on the variable), so the copy is built here. Name, arithmetic
# and what counts as a number follow the platform, so `b5.worry_r` means the same thing in an
# exported file and in data read through the API.

# What PHP's is_numeric() accepts, which is what the platform mirrors: " 4" is a number,
# "0x1A", "inf" and "1_000" are not (float() would take the last two).
_NUMBER = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*$")


def _reversed_copies(codebook: list[dict]) -> dict[str, dict]:
    """source name → {"name", "sum", "label"} for each copy to make, in codebook order."""
    taken = {v.get("name") for v in codebook}
    copies: dict[str, dict] = {}
    for var in codebook:
        scale = var.get("reverse_scored") or {}
        if scale.get("min") is None or scale.get("max") is None:
            continue
        source = var["name"]
        # `<name>_r`, or `_r2`, `_r3` … when another variable already has that name.
        name, n = f"{source}_r", 2
        while name in taken:
            name, n = f"{source}_r{n}", n + 1
        taken.add(name)
        copies[source] = {"name": name, "sum": scale["min"] + scale["max"], "label": var.get("label")}
    return copies


def _reversed(value: Any, total: float) -> float | int | None:
    """(min + max) − answer for an answer that is a number; None for "dk", an empty answer,
    yes/no and anything else off the scale."""
    if isinstance(value, bool) or type(value).__name__ == "bool_" or _is_missing(value):
        return None
    if isinstance(value, numbers.Real):
        result = total - value
    elif isinstance(value, str) and _NUMBER.match(value):
        result = total - float(value)
    else:
        return None
    # 6.0 → 6, as the platform writes it: whole numbers stay whole.
    return int(result) if float(result).is_integer() else result


def _add_reversed_copies(df, codebook: list[dict]):
    """Each copy right after the column it mirrors, as the export file has it. A numeric
    column, whatever the source becomes afterwards: it is for computing a scale score with."""
    import pandas as pd

    for source, copy in _reversed_copies(codebook).items():
        if source not in df.columns or copy["name"] in df.columns:
            continue
        if df[source].map(lambda v: isinstance(v, (dict, list))).any():
            continue   # a nested answer has no single value to mirror
        values = [_reversed(v, copy["sum"]) for v in df[source]]
        df.insert(df.columns.get_loc(source) + 1, copy["name"],
                  pd.to_numeric(pd.Series(values, index=df.index, dtype=object), errors="coerce"))
    return df


# ── Scales ───────────────────────────────────────────────────────────────────────────────

def items(data, names: list[str], codebook: list[dict] | None = None, reverse: bool = True):
    """The items of a scale as numbers, reverse-scored ones reversed.

    A scale is computed from numbers, but `frame()` hands a choice item out as a
    Categorical with its labels — what a table or a plot needs. Its category codes are
    positions, not answers: on a 1–5 scale with "Don't know" as a sixth option, "Don't
    know" is 6. This takes the numbers from the codebook instead.

    An item the researcher marked "Reverse-scored" in the editor comes reversed —
    (smallest + largest scale value) − answer — under the name of its reversed copy
    (`b5.worry_r`), so you name the items as they are called in the questionnaire and do
    not need to remember which are worded the other way round. `reverse=False` gives
    every item as answered. An answer that is no number ("dk") is None (NaN in a frame).

    `data` is what `unnest()` returns: rows (then pass `codebook=con.codebook(42)`) or a
    DataFrame, which carries its codebook. Returns the same kind: rows of numbers, or a
    DataFrame of floats.
    """
    pd = sys.modules.get("pandas")
    frame = pd is not None and isinstance(data, pd.DataFrame)
    if codebook is None and frame:
        codebook = data.attrs.get("sondavi_codebook")
    if codebook is None:
        raise ValueError("items() reads the numbers from the codebook: pass codebook=con.codebook(42).")

    variables = {v.get("name"): v for v in codebook}
    copies = _reversed_copies(codebook)
    columns: dict[str, list] = {}
    for name in names:
        if frame:
            if name not in data.columns:
                raise KeyError(f"Not a column of the data: {name!r}")
            # Only a Categorical holds labels; in rows and in plain columns the codes are the answers.
            labelled = isinstance(data[name].dtype, pd.CategoricalDtype)
            raw = list(data[name])
        else:
            if name not in variables and not any(name in row for row in data):
                raise KeyError(f"Not a field of the rows: {name!r}")
            labelled = False
            raw = [row.get(name) for row in data]
        values = [_item_number(v, variables.get(name) if labelled else None) for v in raw]
        if reverse and name in copies:
            values = [None if v is None else _whole(copies[name]["sum"] - v) for v in values]
            name = copies[name]["name"]
        columns[name] = values

    if frame:
        return pd.DataFrame(columns, index=data.index, dtype=float)
    return [dict(zip(columns, values)) for values in zip(*columns.values())]


def scale(data, names: list[str], codebook: list[dict] | None = None, min_items: int | None = None):
    """The scale score per row: the mean of `items()`.

    None (NaN in a frame) for a row with fewer than `min_items` numbers — by default every
    item has to be answered, so a mean over fewer items never happens unnoticed. Lower it to
    allow gaps, e.g. `min_items=3` for three of four.
    """
    need = len(names) if min_items is None else min_items
    if need < 1:
        raise ValueError("min_items has to be at least 1.")
    table = items(data, names, codebook=codebook)

    def score(values):
        numbers_ = [v for v in values if not _is_missing(v)]
        return sum(numbers_) / len(numbers_) if len(numbers_) >= need else None

    pd = sys.modules.get("pandas")
    if pd is not None and isinstance(table, pd.DataFrame):
        return pd.Series([score(list(r)) for r in table.itertuples(index=False)], index=table.index, dtype=float)
    return [score(list(row.values())) for row in table]


def _item_number(value: Any, variable: dict | None) -> float | int | None:
    """The stored number of one answer. With a variable, `value` is a label and is turned
    back into its value first; a category the codebook has no label for is named by its
    value already. What is no number is None — the same rule as the reversed copies."""
    if _is_missing(value) or isinstance(value, bool) or type(value).__name__ == "bool_":
        return None
    if isinstance(value, numbers.Real):
        return _whole(float(value))
    text = str(value)
    for label in (variable or {}).get("value_labels") or []:
        if label.get("text") == text:
            text = _value_key(label.get("value")) or ""
            break
    return _whole(float(text)) if _NUMBER.match(text) else None


def _whole(number: float) -> float | int:
    # 6.0 → 6, as the platform writes it: whole numbers stay whole.
    return int(number) if float(number).is_integer() else number


# ── Image marking ────────────────────────────────────────────────────────────────────────
#
# The stored answer of an image marking question carries its own image and grid:
#   {"mode": "area", "image": {"src", "width", "height"}, "grid": {"cols", "rows"},
#    "cells": {<category>: [<cell index>, ...]}}
#   {"mode": "point", "image": {...}, "points": [{"x", "y", "category"}, ...]}
# A cell index is row * cols + col, counted from 0 at the top left. The conventions are the
# platform's (App\Support\ImageAnnotation) and must stay identical to it: a heatmap drawn
# from the API has to agree with one drawn from the export file.

_MARKING_COLUMNS = ("response_id", "respondent_id", "completed_at", "question", "category",
                    "row", "col", "x_norm", "y_norm", "x_px", "y_px", "image_src")


def _is_image_marking(value: Any) -> bool:
    return (isinstance(value, dict) and value.get("mode") in ("area", "point")
            and isinstance(value.get("image"), dict))


def _short_number(x: Any) -> str:
    # 0.5 rather than 0.5000 or 5e-01, like the export.
    return f"{round(float(x), 4):.4f}".rstrip("0").rstrip(".") or "0"


def _cell_runs(cells: list, cols: int) -> str:
    out: list[str] = []
    ids = sorted({int(c) for c in cells})
    start = prev = None
    for cell in ids + [None]:
        # A run continues only with the right-hand neighbour in the same row.
        if cell is not None and prev is not None and cell == prev + 1 and cell // cols == prev // cols:
            prev = cell
            continue
        if start is not None:
            row, a, b = start // cols, start % cols, prev % cols
            out.append(f"{row}:{a}" if a == b else f"{row}:{a}-{b}")
        start = prev = cell
    return " ".join(out)


def _image_marking_columns(value: dict, prefix: str) -> dict:
    if value.get("mode") == "point":
        by_category: dict[str, list[str]] = {}
        for p in value.get("points") or []:
            by_category.setdefault(str(p.get("category", "")), []).append(
                f"{_short_number(p.get('x', 0))},{_short_number(p.get('y', 0))}")
        return {f"{prefix}.{cat}": " ".join(pairs) for cat, pairs in by_category.items()}

    cols = max(1, int((value.get("grid") or {}).get("cols") or 1))
    return {f"{prefix}.{cat}": _cell_runs(ids or [], cols) for cat, ids in (value.get("cells") or {}).items()}


def markings(rows: list[dict], columns: list[str] | None = None) -> list[dict]:
    """One row per marked cell or pin, for a heatmap.

    Image marking questions arrive as the stored answer: the image it was given on, the
    grid, and the painted cells or the pins. This turns them into the long table a heatmap
    is drawn from — the same rows and fields as `markings.csv` in the platform's
    image-markings export, so a script can switch between the file and the API unchanged.

    Every row carries its position twice: `x_norm`/`y_norm` between 0 and 1 (left to right,
    top to bottom), and `x_px`/`y_px` in pixels of the original image — the mapping,
    already done. For a painted cell that is the cell's centre; `row` and `col` say which
    cell (counted from 0 at the top left) and are None for pins. A cell covers `x_norm`
    from `col / cols` to `(col + 1) / cols`. Nothing is aggregated.

        import pandas as pd
        m = pd.DataFrame(markings(con.responses(42)))
        m[m.question == "map"].groupby(["row", "col"]).size()   # people per cell

    `columns` limits it to some questions; default is every image marking question found.
    `respondent_id` is included when the rows carry it.
    """
    found: list[str] = []
    for row in rows:
        for key, value in row.items():
            if key not in found and _is_image_marking(value):
                found.append(key)
    targets = found if columns is None else [c for c in found if c in columns]
    with_respondent = any("respondent_id" in r for r in rows)

    out: list[dict] = []
    for question in targets:
        for row in rows:
            answer = row.get(question)
            if not _is_image_marking(answer):
                continue
            lead = {"response_id": row.get("response_id")}
            if with_respondent:
                lead["respondent_id"] = row.get("respondent_id")
            lead["completed_at"] = row.get("completed_at")
            lead["question"] = question
            for mark in _image_marking_rows(answer):
                out.append({**lead, **mark})

    return out


def _image_marking_rows(answer: dict) -> list[dict]:
    image = answer.get("image") or {}
    width = float(image.get("width") or 0)
    height = float(image.get("height") or 0)
    src = str(image.get("src") or "")

    def place(category, row, col, x, y):
        return {"category": category, "row": row, "col": col,
                "x_norm": round(x, 6), "y_norm": round(y, 6),
                "x_px": round(x * width, 2), "y_px": round(y * height, 2), "image_src": src}

    if answer.get("mode") == "point":
        return [place(str(p.get("category", "")), None, None, float(p.get("x", 0)), float(p.get("y", 0)))
                for p in answer.get("points") or []]

    grid = answer.get("grid") or {}
    cols = max(1, int(grid.get("cols") or 1))
    rows = max(1, int(grid.get("rows") or 1))
    marks = []
    for category, ids in (answer.get("cells") or {}).items():
        for cell in ids or []:
            r, c = divmod(int(cell), cols)
            # The centre of the cell — the platform's +0.5 convention.
            marks.append(place(category, r, c, (c + 0.5) / cols, (r + 0.5) / rows))
    return marks


# ── AI interview question ────────────────────────────────────────────────────────────────
#
# The stored answer of an interview question is the whole conversation:
#   {"mode": "free" | "state" | "flow", "status", "turns",
#    "messages": [{"role": "assistant" | "user", "text", "at", "input"?, "state"?}, ...],
#    "questionStatus"?, "statePath"?, "microNarrative"?, "voiceConsent"?, ...}
# unnest() writes the columns the platform's export file and codebook have
# (App\Support\LlmInterviewAnswer); the conversation itself is the long table of
# conversations().

_CONVERSATION_COLUMNS = ("response_id", "respondent_id", "completed_at", "question", "mode",
                         "message", "role", "answer", "text", "input", "at", "state")
_ROLES = {"assistant": "interviewer", "user": "respondent"}


def _is_interview_answer(value: Any) -> bool:
    return isinstance(value, dict) and all(k in value for k in ("mode", "status", "turns", "messages"))


def _interview_columns(value: dict, prefix: str) -> dict:
    out = {f"{prefix}.status": value.get("status"), f"{prefix}.turns": int(value.get("turns") or 0)}
    # Only an interview with a list of questions has these; elsewhere there is no column
    # rather than a 0 of nothing - the export's rule.
    if value.get("mode") == "state":
        statuses = [q.get("status") for q in value.get("questionStatus") or [] if isinstance(q, dict)]
        out[f"{prefix}.answered"] = statuses.count("answered")
        out[f"{prefix}.skipped"] = statuses.count("skipped_anticipated")
    # Only when the interview offers summaries; None when none was chosen.
    narrative = value.get("microNarrative")
    if isinstance(narrative, dict):
        chosen = narrative.get("chosen")
        out[f"{prefix}.summary"] = chosen if isinstance(chosen, str) and chosen.strip() else None
    return out


def conversations(rows, columns: list[str] | None = None) -> list[dict]:
    """One row per message of an AI interview question.

    An interview question arrives as its stored answer: the whole conversation, with
    how it ended and, where the interview offers one, the summary the respondent
    chose. `unnest()` turns that into the export's columns (`<question>.status`,
    `.turns`, `.answered`, `.skipped`, `.summary`); this turns the conversation itself
    into a long table, the shape for coding answers, counting words or measuring how
    long people took:

        import pandas as pd
        c = pd.DataFrame(conversations(con.responses(42)))
        said = c[c.role == "respondent"]
        said.groupby("response_id").text.apply(" ".join)       # everything a person said
        said.groupby("input").size()                            # typed or spoken

    Fields: `response_id`, `respondent_id` (when the rows carry it), `completed_at`,
    `question`, `mode` (free, state or flow), `message` (1, 2, ... in the order of the
    conversation), `role` ("interviewer" or "respondent"), `answer` (the respondent's
    1st, 2nd ... answer; None for the interviewer), `text`, `input` ("text" or "voice",
    for answers), `at` (when it was written, ISO 8601 in UTC) and `state` (the stage of
    a flow interview the interviewer was in; None otherwise).

    `rows` is what `responses()` returns or a DataFrame from `frame()`, before
    `unnest()`. `columns` limits it to some questions; default is every interview
    question found. People who never started the interview contribute no rows.
    """
    pd = sys.modules.get("pandas")
    if pd is not None and isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")

    found: list[str] = []
    for row in rows:
        for key, value in row.items():
            if key not in found and _is_interview_answer(value):
                found.append(key)
    targets = found if columns is None else [c for c in found if c in columns]
    with_respondent = any("respondent_id" in r for r in rows)

    out: list[dict] = []
    for question in targets:
        for row in rows:
            answer = row.get(question)
            if not _is_interview_answer(answer):
                continue
            lead = {"response_id": row.get("response_id")}
            if with_respondent:
                lead["respondent_id"] = row.get("respondent_id")
            lead["completed_at"] = row.get("completed_at")
            lead["question"] = question
            lead["mode"] = answer.get("mode")
            given = 0
            for number, message in enumerate(answer.get("messages") or [], start=1):
                if not isinstance(message, dict):
                    continue
                role = _ROLES.get(message.get("role"), message.get("role"))
                if role == "respondent":
                    given += 1
                out.append({**lead, "message": number, "role": role,
                            "answer": given if role == "respondent" else None,
                            "text": message.get("text"), "input": message.get("input"),
                            "at": message.get("at"), "state": message.get("state")})

    return out
