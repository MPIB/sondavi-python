"""Talking to the platform. Deliberately on the standard library alone, so an
analysis environment needs nothing installed to read its own data."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _timezone
from typing import Any, Iterator

__all__ = ["connect", "Connection", "ApiError", "unnest", "markings"]

_USER_AGENT = "sondavi-python/0.3.0"

# The platform's own timestamps, which every study carries. They arrive as wall-clock
# strings without a zone ("2026-09-23 10:45:29"); left as text they sort lexically and
# compare against a datetime without complaining, which is the kind of mistake that
# reaches a paper.
_TIMESTAMPS = ("completed_at", "started_at")


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
        """Variable names, labels, types and value labels."""
        return self._request("GET", f"surveys/{survey_id}/codebook")["variables"]

    def responses(self, survey_id: int, since: str | None = None,
                  include_test: bool = False, page_size: int = 500,
                  max_rows: float = float("inf"), parse_dates: bool = True) -> list[dict]:
        """All responses, walking the pages for you.

        These are the same rows as the platform's JSON export, which means **partial
        responses are included** when the study saves them: someone who stopped halfway
        is a row whose `completed_at` is None. `len()` is therefore not the number of
        completed participations.

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
        rather than bare codes — that is what a CSV cannot carry.

        Pass `unnest=True` to spread matrices and dynamic panels into one column each,
        under the same names the platform's own export writes.
        """
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover - depends on the environment
            raise RuntimeError("frame() needs pandas: pip install 'sondavi[pandas]'") from None

        spread = kwargs.pop("unnest", False)

        rows = self.responses(survey_id, **kwargs)
        if spread:
            rows = unnest(rows)

        df = pd.DataFrame(rows)
        if df.empty:
            return df

        # No date handling here: `responses()` already yields real datetimes, which pandas
        # turns into a datetime64 column by itself. Doing it twice raised
        # "Already tz-aware, use tz_convert" — the kind of error that only appears once
        # both halves are right.

        for var in self.codebook(survey_id):
            name = var["name"]
            if name not in df.columns:
                continue
            labels = var.get("value_labels") or []
            if labels:
                mapping = {str(l["value"]): l["text"] for l in labels}
                df[name] = pd.Categorical(
                    df[name].astype(str).map(mapping),
                    categories=[l["text"] for l in labels],
                )
            elif var.get("type") == "numeric":
                df[name] = pd.to_numeric(df[name], errors="coerce")

        df.attrs["mpi_fingerprint"] = getattr(self, "last_fingerprint", None)
        return df


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


def unnest(rows: list[dict], columns: list[str] | None = None) -> list[dict]:
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
    """
    out = []

    for row in rows:
        flat: dict = {}
        for key, value in row.items():
            if columns is not None and key not in columns:
                flat[key] = value
                continue
            flat.update(_flatten_answer(value, key))
        out.append(flat)

    return out


def _flatten_answer(value: Any, prefix: str) -> dict:
    # An image marking is one answer with its own column rule, not a tree of fields: the
    # export writes one column per marking type, and so does this.
    if _is_image_marking(value):
        return _image_marking_columns(value, prefix)

    if not isinstance(value, (dict, list)) or not value:
        return {prefix: value}

    # A list of scalars is a single answer (checkbox, ranking, tagbox).
    if isinstance(value, list) and not isinstance(value[0], (dict, list)):
        return {prefix: value}

    pairs = value.items() if isinstance(value, dict) else enumerate(value)

    flat: dict = {}
    for key, child in pairs:
        if isinstance(key, str) and key.startswith("__"):
            continue
        flat.update(_flatten_answer(child, f"{prefix}.{key}"))

    return flat or {prefix: value}


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
