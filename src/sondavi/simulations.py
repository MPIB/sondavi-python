"""Simulations: play a study's AI interviews from a script.

The platform runs the interviewer exactly as it runs live; your script plays the
respondent. Each simulation runs with a parameter set — the study's own settings, changed
where you say — and keeps the data you attach to it (a score, the persona, notes). Use it
to try settings by hand, or to optimise them automatically (see the examples).

    con = connect("https://survey.example.org")
    [c["id"] for c in con.components(42)]          # 'survey-interview', 'question:q7', …

    sim = con.simulate(42, "question:q7",
                       responder=lambda s: "I teach maths.",
                       parameters={"systemPrompt": "Ask like a journalist."})
    sim.attach(score=0.8)

Needs a token with the permission "Run simulations". Every start and every response —
except choosing a summary — is a simulated turn; your organisation allows a number of
them per day, and `BudgetExhausted` says when they are used up.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import ApiError, BudgetExhausted, Connection

__all__ = ["Simulation", "BudgetExhausted"]


@dataclass
class Simulation:
    """One run of a component, as the platform last reported it."""

    id: str
    survey_id: int
    component: str
    status: str
    stimulus: dict | None
    parameters: dict
    attachments: dict
    raw: dict[str, Any] = field(repr=False)
    _con: Connection | None = field(default=None, repr=False, compare=False)

    @classmethod
    def _from(cls, body: dict, con: Connection) -> "Simulation":
        return cls(
            id=body["id"], survey_id=body["survey_id"], component=body["component"],
            status=body["status"], stimulus=body.get("stimulus"),
            parameters=dict(body.get("parameters") or {}),
            attachments=dict(body.get("attachments") or {}), raw=body, _con=con,
        )

    # ── what it says ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def expects(self) -> str | None:
        """What the current stimulus waits for: 'text', 'choice', 'summary' — or None."""
        return (self.stimulus or {}).get("expects")

    @property
    def transcript(self) -> list[dict]:
        """The conversation so far: [{'role': 'interviewer'|'respondent', 'text': …}]."""
        if "result" not in self.raw:
            self.refresh()
        return list((self.raw.get("result") or {}).get("transcript") or [])

    @property
    def outcome(self) -> Any:
        """What a real participation would have produced (see the API reference)."""
        if "result" not in self.raw:
            self.refresh()
        return (self.raw.get("result") or {}).get("outcome")

    @property
    def effective_parameters(self) -> dict:
        """Every parameter it ran with — the study's values where you changed nothing."""
        if "effective_parameters" not in self.raw:
            self.refresh()
        return dict(self.raw.get("effective_parameters") or {})

    # ── what it does ─────────────────────────────────────────────────────────────

    def respond(self, text: str | None = None, *, value: Any = None, choice: Any = ...,
                skip: bool = False, other: str | None = None, comment: str | None = None,
                retries: int = 2) -> "Simulation":
        """Answer the current stimulus — with `text`; where a question is answered with
        buttons (`expects == "choice"`) with a `value` (and the text of "Other" in `other`,
        a `comment` where the question has a comment area) or `skip=True` where it may be left
        out; with summaries a `choice` (the index, or None to skip).

        A model call that fails records nothing on the platform, so the same response is
        sent again, up to `retries` times.
        """
        if sum(x is not None and x is not ... for x in (text, value)) + (choice is not ...) + bool(skip) != 1:
            raise ValueError("Give exactly one of text, value, skip or choice.")
        if (other is not None or comment is not None) and value is None:
            raise ValueError("`other` and `comment` go with a value.")
        if text is not None:
            body = {"text": text}
        elif value is not None:
            body = {"value": value, **({"other": other} if other is not None else {}),
                    **({"comment": comment} if comment is not None else {})}
        elif skip:
            body = {"skip": True}
        else:
            body = {"choice": choice}

        for attempt in range(retries + 1):
            try:
                data = self._con._request("POST", f"simulations/{self.id}/responses", body=body)
                break
            except ApiError as err:
                if attempt < retries and err.reason in ("model_failed", "busy"):
                    time.sleep(1 + attempt)
                    continue
                raise
        self._update(data["data"])
        return self

    def attach(self, data: dict | None = None, **values: Any) -> "Simulation":
        """Keep your own data with the simulation: keys are added or replaced, a key set
        to None is removed. Also after it has ended."""
        merged = dict(data or {}, **values)
        self._update(self._con._request("PATCH", f"simulations/{self.id}", body={"attachments": merged})["data"])
        return self

    def set(self, *, label: Any = ..., run: Any = ...) -> "Simulation":
        """Change its label or run."""
        body = {k: v for k, v in (("label", label), ("run", run)) if v is not ...}
        self._update(self._con._request("PATCH", f"simulations/{self.id}", body=body)["data"])
        return self

    def refresh(self) -> "Simulation":
        self._update(self._con._request("GET", f"simulations/{self.id}")["data"])
        return self

    def delete(self) -> None:
        self._con._request("DELETE", f"simulations/{self.id}")
        self.status = "deleted"

    def _update(self, body: dict) -> None:
        fresh = Simulation._from(body, self._con)
        self.__dict__.update({k: v for k, v in fresh.__dict__.items() if k != "_con"})


# ── on the connection ──────────────────────────────────────────────────────────────

def _components(self: Connection, survey_id: int) -> list[dict]:
    """The study's simulatable components, each with its parameters — `default` is the
    value the study has now — and the inputs it takes."""
    return self._request("GET", f"surveys/{survey_id}/components")["data"]


def _start_simulation(self: Connection, survey_id: int, component: str,
                      parameters: dict | None = None, inputs: dict | None = None,
                      label: str | None = None, run: str | None = None) -> Simulation:
    """Start a simulation; its `stimulus` is the interviewer's opening."""
    body: dict[str, Any] = {"component": component}
    if parameters:
        body["parameters"] = parameters
    if inputs:
        body["inputs"] = inputs
    if label is not None:
        body["label"] = label
    if run is not None:
        body["run"] = run
    return Simulation._from(self._request("POST", f"surveys/{survey_id}/simulations", body=body)["data"], self)


def _simulate(self: Connection, survey_id: int, component: str,
              responder: Callable[[dict], Any], parameters: dict | None = None,
              inputs: dict | None = None, label: str | None = None, run: str | None = None,
              max_steps: int = 100) -> Simulation:
    """Play a simulation to its end.

    `responder` gets each stimulus and returns the response: a string (an answer, or with
    a summary a wish for new ones), or a dict of `Simulation.respond()`'s arguments —
    {"value": …, "other": …}, {"skip": True}, {"choice": 0} … With a summary, an int is
    taken as the choice; where a question is answered with buttons, anything but a
    string as its value.
    """
    sim = _start_simulation(self, survey_id, component, parameters, inputs, label, run)

    for _ in range(max_steps):
        if not sim.active:
            return sim
        answer = responder(sim.stimulus)
        if isinstance(answer, dict):
            unknown = set(answer) - {"text", "value", "choice", "skip", "other", "comment"}
            if unknown:
                raise ValueError(f"A responder's dict holds the arguments of respond(), not {sorted(unknown)}.")
            sim.respond(**answer)
        elif sim.expects == "summary" and (answer is None or (isinstance(answer, int) and not isinstance(answer, bool))):
            sim.respond(choice=answer)
        elif sim.expects == "choice" and not isinstance(answer, str):
            sim.respond(value=answer)
        else:
            sim.respond(str(answer))

    raise RuntimeError(f"Simulation {sim.id} still running after {max_steps} steps.")


def _simulation(self: Connection, simulation_id: str) -> Simulation:
    return Simulation._from(self._request("GET", f"simulations/{simulation_id}")["data"], self)


def _simulations(self: Connection, survey: int | None = None, run: str | None = None,
                 component: str | None = None, status: str | None = None,
                 limit: float = float("inf")) -> list[Simulation]:
    """The simulations of the studies this token covers, newest first, without their
    steps (`refresh()` fetches those)."""
    query = {"survey": survey, "run": run, "component": component, "status": status, "per_page": 500}
    out: list[Simulation] = []
    while True:
        body = self._request("GET", "simulations", query)
        for item in body["data"]:
            if len(out) >= limit:
                return out
            out.append(Simulation._from(item, self))
        cursor = body["meta"].get("next_cursor")
        if cursor is None:
            return out
        query["cursor"] = cursor


Connection.components = _components
Connection.start_simulation = _start_simulation
Connection.simulate = _simulate
Connection.simulation = _simulation
Connection.simulations = _simulations
