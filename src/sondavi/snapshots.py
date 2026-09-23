"""Citable datasets.

A live query is not a dataset: run the analysis again next month and it may
return more rows, or fewer. A snapshot records the exact set a result was
computed from — the membership, never a copy of the answers, so that retention
and the right to erasure keep working."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import Connection

__all__ = ["Snapshot"]


@dataclass
class Snapshot:
    id: str
    survey_id: int
    label: str | None
    recorded_at: str
    digest: str
    recorded_rows: int
    present_rows: int
    missing_rows: int
    complete: bool
    raw: dict[str, Any]

    @classmethod
    def _from(cls, body: dict) -> "Snapshot":
        return cls(
            id=body["id"], survey_id=body["survey_id"], label=body.get("label"),
            recorded_at=body["recorded_at"], digest=body["digest"],
            recorded_rows=body["recorded_rows"], present_rows=body["present_rows"],
            missing_rows=body["missing_rows"], complete=body["complete"], raw=body,
        )

    def citation(self) -> str:
        """The line that belongs in the paper."""
        return (f"snapshot {self.id} (survey {self.survey_id}, "
                f"{self.recorded_rows} responses, recorded {self.recorded_at}, "
                f"digest {self.digest[:16]})")


def _snapshot(self: Connection, survey_id: int, label: str | None = None,
              since: str | None = None, include_test: bool = False) -> Snapshot:
    body = self._request("POST", f"surveys/{survey_id}/snapshots",
                         body={"label": label, "since": since,
                               "include_test": bool(include_test)})
    return Snapshot._from(body["data"])


def _snapshots(self: Connection) -> list[Snapshot]:
    return [Snapshot._from(s) for s in self._request("GET", "snapshots")["data"]]


def _snapshot_responses(self: Connection, snapshot_id: str) -> tuple[list[dict], bool]:
    """The rows of a recorded set, and whether every recorded response is still there.

    `False` means the dataset behind a published result has changed — through
    retention or an erasure request. That is information, not an error.

    Deliberately NOT `meta["matches_recorded"]`: the digest covers the RENDERED
    rows, so a token that reads fewer columns than the snapshot was recorded with
    produces a different one by design. That field is `None` in exactly that case,
    and `bool(None)` would report a deletion that never happened.
    """
    body = self._request("GET", f"snapshots/{snapshot_id}/responses")
    return body["data"], bool(body["meta"]["snapshot"]["complete"])


Connection.snapshot = _snapshot
Connection.snapshots = _snapshots
Connection.snapshot_responses = _snapshot_responses
