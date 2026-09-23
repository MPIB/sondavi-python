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
from typing import Any, Iterator

__all__ = ["connect", "Connection", "ApiError"]

_USER_AGENT = "sondavi-python/0.1.0"


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
                  max_rows: float = float("inf")) -> list[dict]:
        """All responses, walking the pages for you.

        The dataset fingerprint of the first page is available afterwards as
        `connection.last_fingerprint`; for a citable dataset use `snapshot()`.
        """
        return list(self.iter_responses(survey_id, since, include_test, page_size, max_rows))

    def iter_responses(self, survey_id: int, since: str | None = None,
                       include_test: bool = False, page_size: int = 500,
                       max_rows: float = float("inf")) -> Iterator[dict]:
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
                yield row

            cursor = body["meta"].get("next_cursor")
            if cursor is None or not body["data"]:
                return

    def frame(self, survey_id: int, **kwargs):
        """The responses as a pandas DataFrame, with the codebook applied.

        Categorical questions become pandas Categoricals with their real labels
        rather than bare codes — that is what a CSV cannot carry.
        """
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover - depends on the environment
            raise RuntimeError("frame() needs pandas: pip install 'sondavi[pandas]'") from None

        rows = self.responses(survey_id, **kwargs)
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        # `completed_at` and `started_at` arrive as wall-clock strings without a
        # zone. Left as text they sort lexically and compare against a Timestamp
        # without complaining, which is the kind of mistake that reaches a paper.
        for column in ("completed_at", "started_at"):
            if column in df.columns:
                df[column] = pd.to_datetime(df[column], errors="coerce", utc=False)
                if getattr(self, "last_timezone", None):
                    df[column] = df[column].dt.tz_localize(self.last_timezone, nonexistent="shift_forward", ambiguous="NaT")

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
