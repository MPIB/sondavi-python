#!/usr/bin/env python3
"""A plain evolutionary search over an interviewer's settings — no dependency but sondavi.

    export SONDAVI_TOKEN=sdv_…                  # a token with "Run simulations"
    export EXAMPLE_LLM_URL=… EXAMPLE_LLM_MODEL=…  # optional, see common.py
    python evolve_interviewer.py https://survey.example.org 42 question:q7

A (μ + λ) evolution strategy: each generation, every candidate setting is played against a
few personas (one simulation each), scored by the judge, and the best μ survive; λ children
are made from them by mutation. A candidate is several parameters at once — the instructions
(text), the temperature and the number of follow-ups — to show that a simulation's
parameters are not only prompts.

Mutating the text: with a model of your own, it rewrites the instructions from the judge's
feedback; in the stub mode, the rules the feedback asks for are added.

Cost: generations × population × personas simulations, each about `maxTurns + 1` turns.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import PERSONAS, judge, own_model, responder, rule_based_rewrite  # noqa: E402
from sondavi import BudgetExhausted, connect  # noqa: E402

# Which parameters make up a candidate, per kind of component (the names the platform uses).
GENES = {
    "interview_question": {"text": "systemPrompt", "temperature": "temperature", "count": "maxTurns"},
    "survey_interview": {"text": "system_prompt", "temperature": "temperature", "count": "max_follow_ups"},
}


def mutate(candidate: dict, feedback: list[str], genes: dict, limits: dict, llm, rng: random.Random) -> dict:
    child = dict(candidate)
    if llm is not None:
        child[genes["text"]] = llm(
            "Here are instructions for a research interviewer, and what a reviewer said about "
            f"interviews run with them.\n\nInstructions:\n{candidate[genes['text']]}\n\n"
            "Feedback:\n- " + "\n- ".join(feedback[:6]) +
            "\n\nWrite improved instructions (at most 120 words). Answer with the instructions only.",
            temperature=0.9,
        )
    else:
        child[genes["text"]] = rule_based_rewrite(candidate[genes["text"]], feedback)

    t_min, t_max = limits["temperature"]
    current_t = candidate.get(genes["temperature"])
    child[genes["temperature"]] = round(min(t_max, max(t_min, (current_t if current_t is not None else 0.7) + rng.gauss(0, 0.2))), 2)
    c_min, c_max = limits["count"]
    child[genes["count"]] = int(min(c_max, max(c_min, candidate[genes["count"]] + rng.choice([-1, 0, 1]))))
    return child


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base_url")
    parser.add_argument("survey_id", type=int)
    parser.add_argument("component")
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--mu", type=int, default=2, help="survivors per generation")
    parser.add_argument("--lam", type=int, default=2, help="children per generation")
    parser.add_argument("--personas", type=int, default=2, help="personas each candidate is played against")
    parser.add_argument("--run", default="evolve")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    con = connect(args.base_url)
    llm = own_model()
    components = {c["id"]: c for c in con.components(args.survey_id)}
    component = components.get(args.component) or sys.exit(f"No component {args.component!r}; the study has: {', '.join(components)}")
    genes = GENES[component["kind"]]
    params = {p["name"]: p for p in component["parameters"]}
    limits = {
        "temperature": (params[genes["temperature"]].get("min", 0), params[genes["temperature"]].get("max", 2)),
        # Short interviews while searching; the platform's own limits otherwise.
        "count": (max(1, params[genes["count"]].get("min", 0)), min(4, params[genes["count"]].get("max", 4))),
    }

    seed = {name: params[name]["default"] for name in genes.values()}
    seed[genes["count"]] = min(seed[genes["count"]], limits["count"][1])
    population = [(seed, None, [])]
    print(f"Evolving {', '.join(genes.values())} of {component['title']} — "
          f"{'your model' if llm else 'STUB MODE (no EXAMPLE_LLM_URL)'}")

    try:
        for generation in range(args.generations):
            scored = []
            for candidate, score, feedback in population:
                if score is None:
                    results = []
                    for persona in rng.sample(PERSONAS, args.personas):
                        sim = con.simulate(args.survey_id, args.component, responder(persona, llm),
                                           parameters=candidate, label=f"g{generation} {persona['name']}", run=args.run)
                        s, f = judge(sim.transcript, persona, llm)
                        sim.attach(score=s, feedback=f, persona=persona["name"], generation=generation)
                        results.append((s, f))
                    score = sum(s for s, _ in results) / len(results)
                    feedback = [f for _, f in results]
                scored.append((candidate, score, feedback))

            scored.sort(key=lambda c: c[1], reverse=True)
            survivors = scored[:args.mu]
            print(f"generation {generation}: best {survivors[0][1]:.3f} "
                  f"(temperature {survivors[0][0].get(genes['temperature'])}, {genes['count']} {survivors[0][0][genes['count']]})")

            children = [(mutate(*rng.choice(survivors)[::2], genes, limits, llm, rng), None, []) for _ in range(args.lam)]
            population = survivors + children
    except BudgetExhausted as err:
        print(f"Stopped: {err}")

    best = max((c for c in population if c[1] is not None), key=lambda c: c[1])
    print(f"\nBest settings (score {best[1]:.3f}):")
    for name, value in best[0].items():
        print(f"  {name}: {value}")
    print(f"\nTheir simulations are under run {args.run!r} in the survey's Simulations tab, where they can be applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
