"""Jev: the semantic fallback, called only where code has no answer.

Everything that can be computed is computed: routes, damage, prompt answers
that match a rule, menu positions. TypeSafe's System One model (Jev) is asked
only in four narrow situations, each a small typed judgment:

1. `yes_no`      -- a yes/no prompt whose text no rule recognises.
2. `multichoice` -- a menu whose options no preference matches.
3. `battle_advice` -- a close call between moves in a battle that matters
                    (gym leader, rival, Elite Four), with code's damage numbers
                    already attached to each option.
4. `recover`     -- a milestone is stuck. Jev picks which of the concrete things
                    we could do next (an exit, a person, a sign...) most plausibly
                    unblocks the story goal. Code then does the walking.

Answers are cached by input, every call is budgeted and logged, and each hook
returns None on any failure so the caller falls back to its code default.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

log = logging.getLogger("pokeauto.jev")


class Brain:
    def __init__(self, model: str = "jev-latest", max_calls: int = 200):
        from typesafe_sdk import TypeSafeClient
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self.client = TypeSafeClient()
        self.model = model
        self.max_calls = max_calls
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._cache: dict[str, object] = {}

    # -- plumbing -----------------------------------------------------------------
    def _ask(self, state: dict, questions: dict):
        key = hashlib.sha1(json.dumps([state, {k: repr(v) for k, v in questions.items()}],
                                      sort_keys=True, default=str).encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        if self.calls >= self.max_calls:
            log.warning("JEV budget exhausted (%d calls); using code defaults", self.calls)
            return None
        try:
            resp = self.client.system_one(state=state, questions=questions, model=self.model,
                                          timeout=45.0)
        except Exception as exc:          # network, quota, validation: never fatal
            log.warning("JEV call failed: %s", exc)
            return None
        self.calls += 1
        usage = getattr(resp, "usage", None)
        if usage:
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self._cache[key] = resp.answers
        return resp.answers

    @staticmethod
    def _choice(instructions: str, criteria: dict):
        from typesafe_sdk import Choice
        return Choice(instructions=instructions, criteria=criteria)

    @staticmethod
    def _noul(instructions: str, criteria: dict | None = None):
        from typesafe_sdk import Noul
        return Noul(instructions=instructions, criteria=criteria) if criteria \
            else Noul(instructions=instructions)

    # -- 1. yes/no prompts ------------------------------------------------------------
    goal = ""          # the current milestone, set by the agent

    def yes_no(self, text: str) -> bool | None:
        answers = self._ask(
            {"game": "Pokemon Emerald", "my_current_goal": self.goal,
             "prompt_on_screen": text},
            {"yes": self._noul(
                "Answering YES to the prompt on screen is the right choice for a player "
                "trying to finish the game efficiently and progress toward the current "
                "goal.",
                {"true": "Yes moves the story forward, accepts help, or is harmless",
                 "false": "Yes wastes money or time, gives away something useful, starts "
                          "an unnecessary detour, or declines progress"})})
        if not answers:
            return None
        p = float(answers["yes"].noul)
        log.info("JEV yes/no %r -> p(yes)=%.2f", text[-60:], p)
        return p >= 0.5

    # -- 2. multichoice menus ------------------------------------------------------------
    def multichoice(self, text: str, labels: list[str]) -> int | None:
        opts = {f"option_{i}": l for i, l in enumerate(labels) if l.strip()}
        if len(opts) < 2:
            return None
        answers = self._ask(
            {"game": "Pokemon Emerald", "my_current_goal": self.goal, "prompt_on_screen": text},
            {"pick": self._choice("Which menu option should the player pick to make progress "
                                  "toward the current goal?", opts)})
        if not answers:
            return None
        choice = answers["pick"].choice
        log.info("JEV multichoice %r %s -> %s", text[-50:], labels, choice)
        return int(choice.split("_")[1])

    # -- 3. battle close calls --------------------------------------------------------------
    def battle_advice(self, desc: dict, options: list) -> int | None:
        opts = {f"option_{i}": f"{o.kind} {o.why}" for i, o in enumerate(options)}
        answers = self._ask(
            {"battle": desc,
             "note": "Damage estimates were computed by the game's own formula and are "
                     "accurate. Judge strategy: KO chances, what the opponent is likely to "
                     "do next, and keeping my Pokemon healthy for the rest of the fight."},
            {"best": self._choice("Which action is the best play this turn?", opts)})
        if not answers:
            return None
        pick = answers["best"]
        log.info("JEV battle %s -> %s (conf %.2f)", opts, pick.choice, float(pick.confidence))
        return int(pick.choice.split("_")[1])

    # -- 4. stuck recovery -----------------------------------------------------------------
    def recover(self, context: dict, options: dict[str, str]) -> str | None:
        if len(options) < 2:
            return next(iter(options), None)
        answers = self._ask(
            {"game": "Pokemon Emerald", **context,
             "how_to_read_the_options": "Each option is something I can physically do right "
                                        "now; code will walk me there. Pick the one most "
                                        "likely to unblock the goal."},
            {"next": self._choice(
                "The goal is not progressing. Which of these actions most plausibly "
                "unblocks it (talking to a specific person, going through a particular "
                "exit, reading a sign)?", options)})
        if not answers:
            return None
        pick = answers["next"].choice
        log.info("JEV recover -> %s: %s", pick, options.get(pick))
        return pick

    def note_stuck(self, agent, milestone, exc) -> None:
        """Called by Agent.recover when a milestone raised Stuck."""
        agent.jev_recover(milestone, exc)
