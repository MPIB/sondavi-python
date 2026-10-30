"""What the optimisation examples share: a language model of your own, personas that play
the respondent, and a judge that scores a conversation.

The platform's language model plays the INTERVIEWER — that is what is being optimised.
Everything here (the respondent, the judge, the proposals for better instructions) runs on
a model of your own: any OpenAI-compatible chat endpoint, configured by environment

    EXAMPLE_LLM_URL     e.g. https://api.openai.com/v1  (or a local server: http://localhost:11434/v1)
    EXAMPLE_LLM_KEY     its key, if it needs one
    EXAMPLE_LLM_MODEL   e.g. gpt-4o-mini

Without EXAMPLE_LLM_URL the examples run in a stub mode: a scripted persona, a rule-based
judge and rule-based proposals. That is not an optimisation worth the name — it is there so
the mechanics can be watched against your study before any model of yours is paid for.
(The platform's interviewer still runs, and uses your organisation's simulated turns.)
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

# ── a language model of your own ─────────────────────────────────────────────────────


class ChatModel:
    """An OpenAI-compatible chat endpoint, standard library only."""

    def __init__(self, url: str, model: str, key: str | None = None):
        self.url = url.rstrip("/") + "/chat/completions"
        self.model = model
        self.key = key

    def __call__(self, prompt: str | list[dict], temperature: float = 0.7) -> str:
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature}).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()


def own_model() -> ChatModel | None:
    """The model configured by environment, or None for the stub mode."""
    url = os.environ.get("EXAMPLE_LLM_URL")
    if not url:
        return None
    return ChatModel(url, os.environ.get("EXAMPLE_LLM_MODEL", "gpt-4o-mini"), os.environ.get("EXAMPLE_LLM_KEY"))


# ── personas: who plays the respondent ───────────────────────────────────────────────

# Varied on purpose: an interviewer that only works for the talkative is not a good one.
PERSONAS = [
    {"name": "talkative teacher", "description": "A 45-year-old secondary school teacher who likes to talk and drifts into anecdotes.",
     "facts": ["I teach maths to teenagers.", "Mornings are the hardest, the first lesson starts at eight.", "I go running after school to clear my head."]},
    {"name": "terse nurse", "description": "A 31-year-old night-shift nurse, tired, answers in a few words unless asked something specific.",
     "facts": ["Night shift.", "Busy ward.", "Sleeping during the day is hard."]},
    {"name": "careful retiree", "description": "A 72-year-old retired engineer who answers precisely and asks back when a question is vague.",
     "facts": ["I am retired since 2019.", "I garden every morning.", "I volunteer at the library on Thursdays."]},
    {"name": "hesitant student", "description": "A 19-year-old first-year student, unsure, often says 'I don't know' before giving an answer.",
     "facts": ["I don't know, lectures mostly.", "I work in a café on weekends.", "I miss my friends from home."]},
]


def pick(question: dict, n: int) -> dict:
    """An answer to a question with buttons: one of what it offers, in turn."""
    if question.get("type") == "boolean":
        return {"value": n % 2 == 0}
    offered = [c["value"] for c in (question.get("choices") or []) + (question.get("rate_values") or [])
               if not c.get("disabled")]
    if offered:
        value = offered[n % len(offered)]
        return {"value": [value] if question.get("type") == "checkbox" else value}
    if question.get("can_skip"):
        return {"skip": True}
    specials = [c["value"] for c in question.get("special_items") or [] if c["value"] != "other"]
    if specials:
        return {"value": [specials[0]] if question.get("type") == "checkbox" else specials[0]}
    return {"value": "other", "other": "something else"}


def responder(persona: dict, llm: ChatModel | None):
    """A function that plays `persona` for `Connection.simulate()`."""
    said: list[str] = []

    def answer(stimulus: dict):
        expects = stimulus.get("expects")
        if expects == "summary":
            return 0                       # take the first summary offered
        if expects == "choice":
            # A question answered with buttons takes one of them, nothing typed.
            return pick(stimulus.get("question") or {}, len(said))

        question = " ".join(stimulus.get("messages") or [])
        if llm is None:
            # The stub: the persona's facts, one per turn.
            reply = persona["facts"][len(said) % len(persona["facts"])]
        else:
            history = "\n".join(said[-6:])
            reply = llm(
                f"You are taking part in a research interview. You are: {persona['description']}\n"
                f"Facts about you: {' '.join(persona['facts'])}\n"
                f"What you have said so far:\n{history or '(nothing yet)'}\n\n"
                f"The interviewer says: {question}\n\n"
                "Answer as this person would, in one to three sentences. Do not mention that this is a role.",
                temperature=0.9,
            )
        said.append(reply)
        return reply

    return answer


# ── the judge: how good was the interviewer? ─────────────────────────────────────────

RUBRIC = """Score the INTERVIEWER (not the respondent) from 0 to 10:
- asks one question at a time, briefly;
- asks open, non-leading questions;
- follows up on what the respondent actually said instead of switching topics;
- adapts to the respondent (more specific with terse people, focused with talkative ones);
- is neutral: no praise, no judgement of the answers."""


def transcript_text(transcript: list[dict]) -> str:
    return "\n".join(f"{'Interviewer' if t['role'] == 'interviewer' else 'Respondent'}: {t['text']}" for t in transcript)


def judge(transcript: list[dict], persona: dict, llm: ChatModel | None) -> tuple[float, str]:
    """A score in [0, 1] and the reasons — the reasons are what a reflective optimiser
    learns from, so they must be specific."""
    if llm is not None:
        reply = llm(
            f"{RUBRIC}\n\nThe respondent was: {persona['description']}\n\nTranscript:\n{transcript_text(transcript)}\n\n"
            'Answer with JSON only: {"score": <0-10>, "feedback": "<what the interviewer should do differently, concretely>"}',
            temperature=0,
        )
        match = re.search(r"\{.*\}", reply, re.S)
        try:
            verdict = json.loads(match.group(0)) if match else {}
            return max(0.0, min(1.0, float(verdict.get("score", 0)) / 10)), str(verdict.get("feedback", ""))
        except (ValueError, TypeError):
            return 0.0, f"The judge's answer could not be read: {reply[:200]}"

    # The stub: a few rules of interviewing that can be checked without a model.
    turns = [t["text"] for t in transcript if t["role"] == "interviewer"][:-1] or [""]
    problems = []
    several = sum(t.count("?") > 1 for t in turns)
    if several:
        problems.append(f"asked several questions in one message {several} time(s) — ask one question at a time")
    long = sum(len(t) > 220 for t in turns)
    if long:
        problems.append(f"{long} message(s) longer than 220 characters — keep questions short")
    leading = sum(bool(re.search(r"\b(don't you|wouldn't you|isn't it|surely)\b", t, re.I)) for t in turns)
    if leading:
        problems.append(f"{leading} leading question(s) — ask openly")
    praising = sum(bool(re.search(r"\b(great|wonderful|excellent|amazing)\b", t, re.I)) for t in turns)
    if praising:
        problems.append("praised the answers — stay neutral")
    no_question = sum("?" not in t for t in turns)
    if no_question:
        problems.append(f"{no_question} message(s) without a question — always end with one question")

    score = max(0.0, 1.0 - 0.2 * len(problems) - 0.05 * (several + long + leading + no_question))
    return round(score, 3), "; ".join(problems) or "no rule broken"


# ── proposing better instructions without a model (stub mode) ──────────────────────

RULES = {
    "one question": "Ask exactly one question per message.",
    "short": "Keep every message under two sentences.",
    "openly": "Ask open questions; never suggest an answer.",
    "neutral": "Do not praise or judge answers; acknowledge briefly and neutrally.",
    "end with one": "Always end your message with a single question.",
}


def rule_based_rewrite(instructions: str, feedback: list[str]) -> str:
    """Add the rule each piece of feedback asks for — the stub's stand-in for reflection."""
    text = instructions.strip()
    for key, rule in RULES.items():
        if any(key in f for f in feedback) and rule not in text:
            text = (text + "\n" + rule).strip()
    return text
