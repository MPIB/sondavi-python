#!/usr/bin/env python3
"""Optimise an interviewer's instructions with GEPA, through simulations on the platform.

    pip install "gepa[full]" sondavi
    export SONDAVI_TOKEN=sdv_…                  # a token with "Run simulations"
    export EXAMPLE_LLM_URL=https://api.openai.com/v1 EXAMPLE_LLM_KEY=… EXAMPLE_LLM_MODEL=gpt-4o-mini
    python gepa_interviewer.py https://survey.example.org 42 question:q7

GEPA (https://github.com/gepa-ai/gepa) evolves a text by reflecting on what happened with
it: it runs a candidate on a few examples, reads the feedback, and asks a model to propose a
better text. Here

- the text is the interviewer's instructions (`systemPrompt` of an interview question, or
  `system_prompt` of the whole-survey interview — the parameter is chosen for you);
- an example is a persona; running a candidate on it is one simulation on the platform,
  with the persona (your model) playing the respondent;
- the feedback is a judge's (your model's) verdict on the conversation, which is also
  attached to the simulation — so every candidate GEPA tried can be looked at, and the
  best one applied, under the survey's "Simulations" tab.

Cost: every simulation uses about `maxTurns + 1` of your organisation's simulated turns;
`--budget` is the number of simulations GEPA may run. Without EXAMPLE_LLM_URL the script
runs in a stub mode (see common.py).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # the package next door

import gepa  # noqa: E402
from gepa import EvaluationBatch, GEPAAdapter  # noqa: E402

from common import PERSONAS, judge, own_model, responder, rule_based_rewrite, transcript_text  # noqa: E402
from sondavi import ApiError, BudgetExhausted, connect  # noqa: E402

PROMPT_PARAMETER = {"interview_question": "systemPrompt", "survey_interview": "system_prompt"}


class SondaviAdapter(GEPAAdapter):
    """Runs a candidate as simulations on the platform and lets a judge score them."""

    def __init__(self, con, survey_id: int, component: str, parameter: str, llm, run: str, fixed: dict | None = None):
        self.con, self.survey_id, self.component = con, survey_id, component
        self.parameter, self.llm, self.run, self.fixed = parameter, llm, run, fixed or {}

    def evaluate(self, batch, candidate, capture_traces=False):
        outputs, scores, trajectories = [], [], []
        tag = hashlib.sha1(candidate[self.parameter].encode()).hexdigest()[:8]

        for persona in batch:
            try:
                sim = self.con.simulate(
                    self.survey_id, self.component, responder(persona, self.llm),
                    parameters={**self.fixed, self.parameter: candidate[self.parameter]},
                    label=f"{persona['name']} · {tag}", run=self.run,
                )
                transcript = sim.transcript
                score, feedback = judge(transcript, persona, self.llm)
                # Kept with the simulation: it is what the platform shows next to it.
                sim.attach(score=score, feedback=feedback, persona=persona["name"], candidate=tag)
            except BudgetExhausted:
                raise                                  # systemic: stop the whole run
            except ApiError as err:                    # one example failed: score it 0, go on
                transcript, score, feedback = [], 0.0, f"the simulation failed: {err}"

            outputs.append(transcript_text(transcript))
            scores.append(score)
            trajectories.append({"persona": persona, "transcript": transcript_text(transcript),
                                 "score": score, "feedback": feedback})

        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajectories if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        records = [{
            "Inputs": {"respondent": t["persona"]["description"]},
            "Generated Outputs": t["transcript"] or "(no conversation)",
            "Feedback": f"Score {t['score']:.2f}. {t['feedback']}",
        } for t in eval_batch.trajectories or []]
        return {name: records for name in components_to_update}


def stub_proposer(candidate, reflective_dataset, components_to_update):
    """Without a model of your own: add the rules the feedback asks for."""
    out = {}
    for name in components_to_update:
        feedback = [r["Feedback"] for r in reflective_dataset.get(name, [])]
        out[name] = rule_based_rewrite(candidate[name], feedback)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base_url")
    parser.add_argument("survey_id", type=int)
    parser.add_argument("component", help='e.g. "question:q7" or "survey-interview"')
    parser.add_argument("--budget", type=int, default=24, help="simulations GEPA may run (default 24)")
    parser.add_argument("--run", default=None, help="run name under which the simulations are kept")
    parser.add_argument("--max-turns", type=int, default=None, help="shorter interviews while optimising (interview question)")
    args = parser.parse_args()

    con = connect(args.base_url)
    llm = own_model()
    components = {c["id"]: c for c in con.components(args.survey_id)}
    if args.component not in components:
        sys.exit(f"No component {args.component!r}; the study has: {', '.join(components)}")
    component = components[args.component]
    parameter = PROMPT_PARAMETER[component["kind"]]
    current = next(p["default"] for p in component["parameters"] if p["name"] == parameter) or ""

    fixed = {"maxTurns": args.max_turns} if args.max_turns and component["kind"] == "interview_question" else {}
    run = args.run or f"gepa-{hashlib.sha1(current.encode()).hexdigest()[:6]}"
    print(f"Optimising {parameter} of {component['title']} — run {run!r}, "
          f"{'your model' if llm else 'STUB MODE (no EXAMPLE_LLM_URL)'}")

    adapter = SondaviAdapter(con, args.survey_id, args.component, parameter, llm, run, fixed)
    try:
        result = gepa.optimize(
            seed_candidate={parameter: current},
            trainset=PERSONAS[:3],
            valset=PERSONAS,
            adapter=adapter,
            reflection_lm=(lambda prompt: llm(prompt, temperature=0.7)) if llm else None,
            custom_candidate_proposer=None if llm else stub_proposer,
            reflection_minibatch_size=2,
            max_metric_calls=args.budget,
            display_progress_bar=False,
        )
    except BudgetExhausted as err:
        sys.exit(f"Stopped: {err} Everything run so far is kept under run {run!r}.")

    best = result.best_candidate[parameter]
    print(f"\nBest of {len(result.candidates)} candidates, score {result.val_aggregate_scores[result.best_idx]:.3f}:\n")
    print(best)

    # One more run with the winner, labelled, so it is one click away in the platform.
    sim = con.simulate(args.survey_id, args.component, responder(PERSONAS[0], llm),
                       parameters={**fixed, parameter: best}, label="GEPA best", run=run)
    sim.attach(score=result.val_aggregate_scores[result.best_idx], candidates=len(result.candidates))
    print(f"\nSimulation {sim.id} holds these settings — open the survey's Simulations tab to apply them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
