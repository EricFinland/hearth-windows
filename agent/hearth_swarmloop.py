#!/usr/bin/env python3
"""hearth swarm: one goal, several narrow roles, one budget, one writer.

WHAT THIS IS, AND WHAT IT IS NOT
================================

The bet in the backlog was stated in one sentence: "Multiple local models with
distinct roles, since a 7B model that is bad at everything at once is often
decent at one narrow job." This module is that bet, built so it can be
measured -- and the measurement is in docs/, not in this docstring, because a
module that claims its own benefit is the least trustworthy place to read
about it.

It is NOT "run N agents at once to go faster". Two facts on this hardware make
that shape impossible, and both were measured rather than assumed:

  1. VRAM. hearth_llama.Server holds exactly ONE model resident, because a 7B
     Q4_K_M at a useful context is ~4.4GB plus KV cache and this machine has
     16GB total with other things in it. Two resident 7Bs do not fit, so two
     roles on two models cannot run at the same time. They take turns.
  2. Writes. Nothing in this repository makes two concurrent writers to one
     workspace safe. hearth_tools.tool_edit_file reads, transforms and writes
     with no lock across that span, which is a textbook lost update;
     hearth_checkpoint's lockfile is held inside checkpoint() and released
     between turns, so it serialises COMMITS and not TURNS; hearth_contain
     gives isolation by disjoint roots and has no opinion at all about two
     agents sharing one root.

So the swarm is a RELAY, not a crowd: exactly one role is active at any
instant, it is the only thing holding write authority, and the handoff between
roles is the whole mechanism. Concurrency was not sacrificed to make this
safe -- there was never any concurrency to have, because the GPU serialises
the roles anyway. What is actually being bought is stated below.

WHAT A RELAY BUYS THAT ONE LOOP DOES NOT
========================================

Not more brains. CONTEXT HYGIENE.

A single work loop accumulates its own failures. By turn ten the model's
context is mostly its own wrong answers and the error output they produced,
and hearth_workloop's own stall detectors exist precisely because that state
is where a 7B goes to grind: repeat_action and repeat_error are both
descriptions of a model re-reading its own confusion.

A role handoff is the only cheap way to throw that context away without
throwing away what was learned. Each phase of this relay is a FRESH
hearth_workloop run: a new message list, seeded with the goal and a bounded
handoff, and nothing else. The reviewer that looks at a stuck implementer's
work has never seen the eight failed attempts -- it sees the specification,
the current state, and the completion check's output. That is a different and
much shorter prompt, and a 7B is measurably better at a short focused prompt
than a long polluted one.

The relay's second property falls out of the first: A STALL IS A HANDOFF
SIGNAL, not a failure. hearth_workloop already detects "this run is not
changing anything it has not already changed" and stops. In a single loop that
is the end. Here it is the cue to bring in the next role, which is exactly the
moment a second opinion is worth having.

WHY EACH PHASE IS A WHOLE hearth_workloop RUN
=============================================

Because every safety property the brief requires already lives there, and a
second implementation of any of them would be a second thing to get wrong.
run_workloop is called once per phase and this module adds no turn loop of its
own. Inherited, not rebuilt:

  the cancellation token   one CancelToken is threaded through every phase, so
                           Stop stops the active role mid-generation (the
                           _TokenSink raise) and no later phase starts.
  the ceilings             see the next section: ONE budget, not one per role.
  the progress ledger      per phase, which is what makes a stall a handoff.
  permissions.decide       the only authority on whether a tool may run,
                           including allowed_tools as a hard cap in every mode.
  the journal              append-only, fsynced, "started and never ended" is
                           a crash signature.
  bypass is unreachable    ALLOWED_MODES excludes it by construction, and
                           run_workloop refuses it again on the way in.

ONE BUDGET, NOT ONE PER ROLE
============================

The failure this is written against was named in the brief: "A swarm that
multiplies token spend by the number of roles while keeping a single-agent
ceiling is a bug."

So the ceilings here are GLOBAL and SHARED. Every phase is handed a residual
budget computed by subtracting everything every previous phase already spent
(see _residual). Three roles do not get three times the tokens; they get one
budget between them, and the third role can find it empty and be told so.

Per-role caps exist too (RoleSpec.max_turns), but they are a SECOND bound and
only ever narrow: a phase's real ceiling is the elementwise minimum of the
role's cap and what is left globally. Which of the two actually bit is
recorded, because "the reviewer used up its three turns" and "the run is out
of tokens" are completely different facts and only the second one ends the
swarm. See _phase_ceilings and PhaseRecord.bound_by.

ONE WRITER, PROVED TWO WAYS
===========================

Only the implementer role may change a file, and that is enforced twice by two
mechanisms that do not share code, so neither one being wrong is sufficient to
corrupt a workspace:

  1. The capability manifest. RoleSpec.tools for a read-only role simply has
     no write tool in it, and permissions.decide treats allowed_tools as a
     hard cap in every mode. A reviewer asking for write_file is refused by
     the permission layer with "not in this run's capability manifest".
  2. The write lease. WriteLease is an exclusive token held by at most one
     role at a time, and _leased_executor refuses any tool whose
     permissions.risk_of is "edit" when the active role does not hold it --
     at the executor seam, after permissions has already decided. A manifest
     that was built wrong is still stopped here.

The lease is exclusive by construction: acquire() on an already-held lease
raises rather than blocking, because in a relay there is no legitimate way for
two roles to want it at once, and a lease that silently waited would turn a
scheduling bug into a deadlock instead of an error. Both layers are
independently mutation-tested in _self_test.

HONESTY, AND WHAT A REVIEWER'S APPROVAL IS WORTH
================================================

hearth_workloop ships PROGRESS_BLIND_SPOTS as structured data and labels an
unverified completion "NOTHING VERIFIED THIS". This module holds the same line
and adds the failure mode that is specific to having a reviewer at all:

  A REVIEWER IS A MODEL. Its approval is one 7B's opinion of another 7B's
  work, produced by the same weights that wrote the code. It is evidence about
  as strong as the implementer's own claim to be finished, which hearth_workloop
  already refuses to treat as a completion check. So a reviewer's approval NEVER
  completes a run. Only the deterministic gate (done_command / required
  artifacts) can do that, exactly as in the single loop, and when a run ends
  with a reviewer's blessing and no deterministic check, the account says
  REVIEWED BUT NOT VERIFIED and explains what that is worth.

SWARM_BLIND_SPOTS carries the ones this shape adds on top of the loop's.

Standard library only.
"""

import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_paths  # noqa: E402
import hearth_workloop as W  # noqa: E402
import permissions  # noqa: E402

JOURNAL_VERSION = 1

# Reused verbatim rather than re-spelled: a swarm that could run in a mode a
# loop refuses would be a way to get an ungated unattended tool by choosing a
# different button in the UI.
ALLOWED_MODES = W.ALLOWED_MODES
GATE_POLICIES = W.GATE_POLICIES

# Why a swarm stopped. The loop's reasons, plus one this shape can reach that
# a single loop cannot.
STOP_COMPLETED = W.STOP_COMPLETED
STOP_CEILING = W.STOP_CEILING
STOP_CANCELLED = W.STOP_CANCELLED
STOP_BLOCKED = W.STOP_BLOCKED
STOP_ERROR = W.STOP_ERROR
#: Every role has had its turn, the relay has run its full cycle budget, and
#: the deterministic gate still does not pass. Distinct from the loop's
#: STOP_STALLED because a stalled PHASE here is a handoff, not an ending: this
#: is what it means for the whole relay to have run out of ideas.
STOP_EXHAUSTED = "exhausted"
STOP_REASONS = (STOP_COMPLETED, STOP_CEILING, STOP_EXHAUSTED, STOP_CANCELLED,
                STOP_BLOCKED, STOP_ERROR)

MAX_HANDOFF_CHARS = 4000      # what one role may say to the next
MAX_GOAL_CHARS = 8000
MAX_CYCLES_DEFAULT = 3

# What a swarm cannot tell you that a single loop could not either, plus the
# two this shape adds. Same contract as hearth_workloop.PROGRESS_BLIND_SPOTS:
# structured data, rendered verbatim by the account and by any UI, never
# paraphrased into reassurance. (headline, what_it_means, what_to_do_about_it).
SWARM_BLIND_SPOTS = (
    ("Measured against a single work loop, this did not win.",
     "On this machine, with one 7B and equal ceilings, the relay passed 0 of 7 "
     "hidden-test tasks. A plain work loop with its stall detectors turned off "
     "passed 2 of 3 on the only task the model can actually solve, using fewer "
     "turns on the part that changes files. The relay spends a shared budget "
     "on a planner and a reviewer that cannot change anything.",
     "If a loop is stopping early, set stall.window to 0 on an ordinary work "
     "loop before reaching for roles. See docs/agent-swarm.md for the numbers."),
    ("A reviewer's approval is not a verification.",
     "The reviewer is the same size of model as the implementer, often the "
     "very same weights, reading code it cannot run. Its approval is an "
     "opinion about work produced by an identical process, and two models "
     "agreeing is not independent evidence -- they share their failure modes.",
     "Set a completion check. A reviewer that approves something no command "
     "ever ran is labelled REVIEWED BUT NOT VERIFIED for exactly this reason."),
    ("Roles do not add knowledge the model does not have.",
     "Splitting one 7B into a planner, an implementer and a reviewer does not "
     "make it know anything it did not know as one agent. What it changes is "
     "how much irrelevant context each call carries. A task that fails because "
     "the model does not know the answer fails identically as a swarm.",
     "Read the per-role transcript. If every role says the same wrong thing, "
     "the problem is the model, and more roles will not fix it."),
    ("The handoff is a bottleneck, and it is lossy on purpose.",
     "Each role sees the previous role's summary, capped at {} characters, "
     "not its full transcript. That cap is what buys the clean context, and "
     "it is also a place where the one detail that mattered can be dropped."
     .format(MAX_HANDOFF_CHARS),
     "The per-phase transcript is kept in full in the journal. Read it when a "
     "role appears to have been told the wrong thing."),
    ("Only the implementer can change anything.",
     "The planner and the reviewer are read-only by capability manifest and "
     "by write lease. A goal whose real work is not a file edit inside the "
     "workspace has, at most, one role able to do it, and the other two are "
     "spending your budget to talk about it.",
     "For a read-only investigation, a single loop in plan mode is cheaper "
     "and says the same thing."),
)


# --------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------

def _tools_by_risk(*risks):
    """The subset of the loop's default manifest whose risk is in `risks`.

    Derived from permissions.risk_of rather than listed by hand, so a tool
    that is added to hearth_workloop.DEFAULT_ALLOWED_TOOLS, or reclassified in
    permissions, lands in the right roles without this file being edited. A
    hand-written list is how a read-only role silently acquires a writer.
    """
    return frozenset(t for t in W.DEFAULT_ALLOWED_TOOLS
                     if permissions.risk_of(t) in risks)


#: Everything that cannot change a file. The planner's and reviewer's whole
#: world.
READ_ONLY_TOOLS = _tools_by_risk("safe")
#: Reads plus edits. Note "dangerous" (run_command) is NOT here: it is in the
#: loop's default manifest and reaches this module through gate_policy like
#: any other gated tool, so adding it to a role by risk class would quietly
#: hand an unattended role a shell.
IMPLEMENTER_TOOLS = _tools_by_risk("safe", "edit")


class RoleSpec:
    """One role: what it is for, what it may touch, and how long it gets.

    `writes` is the single fact everything else keys off. It decides whether
    the role is allowed the write lease and whether its manifest contains an
    edit tool, and those two consequences are computed in different places on
    purpose (see the module docstring's ONE WRITER section).
    """

    def __init__(self, name, purpose, instruction, tools, writes=False,
                 max_turns=6, tier=None):
        self.name = name
        self.purpose = purpose
        self.instruction = instruction
        self.tools = frozenset(tools)
        self.writes = bool(writes)
        self.max_turns = max_turns
        #: A hearth_router tier name, or None to use the swarm's model. Roles
        #: name a TIER and never a model, so hearth_shop's catalog stays the
        #: one place a tier maps to weights -- see hearth_router._models_for_tier.
        self.tier = tier
        if not self.writes and (self.tools & _tools_by_risk("edit")):
            # A read-only role holding a write tool is the corruption bug this
            # module exists to prevent, and it is cheap to make unrepresentable.
            raise ValueError(
                "role {!r} is read-only but its manifest contains write "
                "tool(s) {}".format(name, sorted(self.tools & _tools_by_risk("edit"))))

    def to_dict(self):
        return {"name": self.name, "purpose": self.purpose,
                "tools": sorted(self.tools), "writes": self.writes,
                "max_turns": self.max_turns, "tier": self.tier}


PLANNER = RoleSpec(
    "planner", "reads the workspace and states a concrete plan",
    "You are the PLANNER. You cannot change any file and must not try.\n"
    "Read enough of the workspace to understand what is actually there, then "
    "state a SHORT numbered plan: the specific files to change and what each "
    "change must accomplish. Be concrete about names that already exist -- a "
    "plan that invents a file the implementer then cannot find is worse than "
    "no plan. Finish by replying with your plan and then the line "
    + W.DONE_SENTINEL + ".",
    READ_ONLY_TOOLS, writes=False, max_turns=4, tier="small")

IMPLEMENTER = RoleSpec(
    "implementer", "makes the change",
    "You are the IMPLEMENTER. You are the only role that may change files.\n"
    "Work the plan. Make real edits with your tools; a turn that only talks "
    "achieves nothing. If a review is attached, treat it as the most "
    "important thing you have been told and address it directly rather than "
    "rewriting from scratch.",
    IMPLEMENTER_TOOLS, writes=True, max_turns=12, tier="medium")

REVIEWER = RoleSpec(
    "reviewer", "reads the result and says what is actually wrong",
    "You are the REVIEWER. You cannot change any file and must not try.\n"
    "You are seeing this work for the first time. Read what was written and "
    "the completion check's output. Name the ROOT CAUSE of the failure as "
    "specifically as you can, and say exactly what must change. Do not "
    "rewrite the code. Do not be reassuring: if it is wrong, the useful thing "
    "is to say precisely how. If it genuinely looks correct, say so plainly "
    "and say what you could not check. Then reply " + W.DONE_SENTINEL + ".",
    READ_ONLY_TOOLS, writes=False, max_turns=3, tier="medium")

#: The relay, in order. The first phase runs once; the pair after it repeats
#: until the deterministic gate passes or a budget runs out.
DEFAULT_ROLES = (PLANNER, IMPLEMENTER, REVIEWER)
ROLES_BY_NAME = {r.name: r for r in DEFAULT_ROLES}


# --------------------------------------------------------------------------
# the write lease
# --------------------------------------------------------------------------

class LeaseError(RuntimeError):
    """Someone tried to write without the lease, or to hold it twice."""


class WriteLease:
    """Exclusive authority to change a file, held by at most one role.

    The second of the two independent layers that keep this relay to a single
    writer (the first is the capability manifest). It is deliberately NOT a
    lock: acquire() on a held lease raises instead of waiting, because in a
    relay nothing legitimately waits for it, and a lease that blocked would
    turn a scheduling mistake into a hang rather than an error anyone can see.
    """

    def __init__(self):
        self._holder = None

    @property
    def holder(self):
        return self._holder

    def acquire(self, role_name):
        if self._holder is not None:
            raise LeaseError(
                "the write lease is already held by {!r}; {!r} cannot take it. "
                "Exactly one role writes at a time.".format(self._holder, role_name))
        self._holder = role_name
        return self

    def release(self, role_name):
        if self._holder != role_name:
            raise LeaseError(
                "{!r} tried to release a lease held by {!r}".format(
                    role_name, self._holder))
        self._holder = None

    def held_by(self, role_name):
        return self._holder is not None and self._holder == role_name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._holder = None
        return False


def _leased_executor(execute_tool_fn, lease, role_name, on_refusal=None):
    """Wrap the tool executor so a role without the lease cannot write.

    This sits BELOW permissions.decide, at the executor seam, and repeats a
    refusal the capability manifest should already have made. That duplication
    is the point: the two layers share no code, so a manifest built wrong is
    still caught here, and a bug in this wrapper is still caught by the
    manifest. Neither alone is trusted.
    """
    def _execute(name, args, workspace):
        if permissions.risk_of(name) == "edit" and not lease.held_by(role_name):
            if on_refusal is not None:
                on_refusal(name)
            return ("denied: {} would change a file, and the {} role does not "
                    "hold the write lease. Only the implementer may change "
                    "files. Report what you found instead.".format(name, role_name))
        return execute_tool_fn(name, args, workspace)
    return _execute


# --------------------------------------------------------------------------
# ceilings, shared across every role
# --------------------------------------------------------------------------

class SwarmCeilings:
    """The whole relay's budget, plus how many times it may go round.

    Wraps hearth_workloop.Ceilings rather than redefining its fields, so
    "turns", "wall clock", "tokens", "unattended writes" and "tool calls" mean
    exactly what they mean for a single loop and are enforced by the same
    Ceilings.exceeded. What is added here is `max_cycles`, which bounds the
    relay itself: without it a swarm whose gate never passes would keep
    handing off forever inside whatever the token budget allowed.
    """

    def __init__(self, ceilings=None, max_cycles=MAX_CYCLES_DEFAULT):
        self.ceilings = ceilings or W.Ceilings()
        self.max_cycles = max_cycles

    def to_dict(self):
        d = dict(self.ceilings.to_dict())
        d["max_cycles"] = self.max_cycles
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        cycles = d.pop("max_cycles", MAX_CYCLES_DEFAULT)
        return cls(W.Ceilings.from_dict(d), max_cycles=cycles)


def _residual(ceilings, spent):
    """What is left of the GLOBAL budget after `spent`.

    This is the whole "one budget, not one per role" property in one function.
    Every phase is handed the result of this, so the Nth role inherits an
    ceiling already reduced by everything the first N-1 roles used. A value
    that has run out becomes 0, never a negative number: hearth_workloop's
    Ceilings treats a negative limit as UNLIMITED (see its exceeded()), so
    subtracting past zero and passing it on would hand an exhausted swarm an
    unbounded phase. That inversion is precisely the bug this clamp exists to
    prevent, and _self_test proves it.
    """
    def left(limit, used):
        if limit is None or limit < 0:
            return limit          # genuinely unlimited: pass it through as-is
        return max(0, limit - used)

    c = ceilings
    return W.Ceilings(
        max_turns=left(c.max_turns, spent.get("turns", 0)),
        max_seconds=left(c.max_seconds, spent.get("elapsed", 0.0)),
        max_tokens=left(c.max_tokens, spent.get("tokens", 0)),
        max_writes=left(c.max_writes, spent.get("writes", 0)),
        max_tool_calls=left(c.max_tool_calls, spent.get("tool_calls", 0)),
    )


def _phase_ceilings(residual, role):
    """The bound on one phase: the role's own cap, narrowed by what is left.

    Elementwise minimum, so a role cap can only ever make a phase SMALLER than
    the global budget allows. The turn cap is the only field a role sets; the
    rest come straight from the residual, because a role has no business
    holding its own private token budget when the point of this module is that
    there is one.
    """
    turns = residual.max_turns
    if role.max_turns is not None and role.max_turns >= 0:
        turns = role.max_turns if turns is None or turns < 0 else min(turns, role.max_turns)
    return W.Ceilings(max_turns=turns, max_seconds=residual.max_seconds,
                      max_tokens=residual.max_tokens, max_writes=residual.max_writes,
                      max_tool_calls=residual.max_tool_calls)


def _global_exhausted(ceilings, spent):
    """The first GLOBAL ceiling `spent` has reached, or None.

    Asked after every phase. This is what separates "the reviewer used its
    three turns" (a phase ending normally, relay continues) from "the swarm is
    out of budget" (the run ends). Both look like STOP_CEILING coming out of
    run_workloop, and treating them the same would either end a healthy run
    early or let an exhausted one keep going.
    """
    return ceilings.exceeded(spent)


# --------------------------------------------------------------------------
# handoffs
# --------------------------------------------------------------------------

def _clip(text, limit=MAX_HANDOFF_CHARS):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated at {} characters)".format(limit)


def build_handoff(role, report, completion):
    """What this phase tells the next one.

    Deliberately a SUMMARY and not a transcript: the clean context is the
    entire benefit of a handoff, and pasting the previous role's full
    conversation in would rebuild exactly the polluted prompt this module
    exists to avoid. The full transcript stays in the journal.

    The completion check's own output is included verbatim (clipped) because
    it is the single most useful thing the next role can be told, and leaving
    it out was a real defect in the single loop's account before it was fixed.
    """
    bits = []
    changed = []
    for label, items in (("created", report.created), ("modified", report.modified),
                         ("deleted", report.deleted)):
        if items:
            changed.append("{} {}".format(label, ", ".join(items[:20])))
    if changed:
        bits.append("Files " + "; ".join(changed) + ".")
    else:
        bits.append("No file in the workspace changed.")
    if report.stop_reason and report.stop_reason != STOP_COMPLETED:
        bits.append("That phase stopped because: {}".format(report.stop_detail))
    say = _last_assistant_text(report)
    if say:
        bits.append("{} said:\n{}".format(role.name, say))
    if completion:
        if completion.get("done"):
            bits.append("The completion check PASSED.")
        else:
            out = (completion.get("output") or "").strip()
            bits.append("The completion check did NOT pass: {}".format(
                completion.get("detail") or "no detail"))
            if out:
                bits.append("It reported:\n" + out[-2000:])
    if report.last_error:
        bits.append("Last error seen: " + report.last_error[:400])
    return _clip("\n\n".join(b for b in bits if b))


def _last_assistant_text(report):
    """The role's own final words, taken from its report rather than its
    conversation, because the conversation is not carried across phases."""
    return _clip(getattr(report, "final_text", "") or "", 2000)


def compose_goal(goal, role, handoff, cycle, plan):
    """The prompt one phase actually receives.

    run_workloop owns the system prompt (hearth_workloop.LOOP_SYSTEM) and this
    module does not change it -- the role framing rides in the GOAL instead.
    That is not a workaround: LOOP_SYSTEM says the things that are true of
    every unattended run on this machine (nobody is watching, refused tools
    stay refused, progress is measured in the workspace), and a role prompt
    that replaced it would have to repeat all of that correctly or quietly
    drop it.
    """
    parts = ["ROLE: {} ({})".format(role.name.upper(), role.purpose),
             role.instruction,
             "",
             "THE OBJECTIVE (this does not change between roles):",
             goal]
    if plan:
        parts += ["", "THE PLAN (from the planner):", _clip(plan, 2000)]
    if handoff:
        parts += ["", "WHAT HAPPENED JUST BEFORE YOU (cycle {}):".format(cycle),
                  handoff]
    if not role.writes:
        parts += ["", "You have NO tools that can change a file. Do not attempt "
                      "to write, edit or replace anything: it will be refused, "
                      "and repeating a refused action ends the run."]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# the record of what happened
# --------------------------------------------------------------------------

class PhaseRecord:
    """One role's turn at the goal, and which bound ended it."""

    def __init__(self, index, cycle, role, model, report, bound_by, completion):
        self.index = index
        self.cycle = cycle
        self.role = role
        self.model = model
        self.report = report
        #: "role" (its own per-role cap), "global" (the shared budget),
        #: or the loop's own stop reason. The distinction that keeps a
        #: reviewer finishing its three turns from reading as a run that ran
        #: out of money.
        self.bound_by = bound_by
        self.completion = completion or {}

    def to_dict(self):
        r = self.report
        return {
            "index": self.index, "cycle": self.cycle, "role": self.role,
            "model": self.model, "bound_by": self.bound_by,
            "stop_reason": r.stop_reason, "stop_detail": r.stop_detail,
            "turns": r.turns, "tokens": r.tokens, "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out, "writes": r.writes,
            "tool_calls": r.tool_calls, "elapsed": round(r.elapsed, 2),
            "created": r.created, "modified": r.modified, "deleted": r.deleted,
            "checkpoints": r.checkpoints, "verdict": r.verdict,
            "last_error": r.last_error, "gates_denied": r.gates_denied,
            "lease_refusals": getattr(r, "lease_refusals", 0),
            "final_text": _clip(getattr(r, "final_text", "") or "", 1200),
            "completion": self.completion,
        }


class SwarmReport:
    """What the relay did, role by role, and what that is worth.

    Same job as hearth_workloop.LoopReport and deliberately the same shape
    where it can be, because a person who has read one account should not have
    to learn a second vocabulary to read this one. What it adds is
    attribution: which role did what, where the handoffs were, and -- the part
    that matters most -- what a reviewer's approval is and is not.
    """

    def __init__(self, goal, model, workspace, mode, run_id, ceilings, roles):
        self.goal = goal
        self.model = model
        self.workspace = workspace
        self.mode = mode
        self.run_id = run_id
        self.ceilings = ceilings
        self.roles = list(roles)
        self.stop_reason = None
        self.stop_detail = ""
        self.phases = []            # [PhaseRecord]
        self.cycles = 0
        self.elapsed = 0.0
        self.plan = ""
        self.last_review = ""
        self.reviewer_approved = False
        self.completion = {}
        self.created = []
        self.modified = []
        self.deleted = []
        self.notices = []
        self.live_workers = 0
        self.swaps = 0
        self.swap_seconds = 0.0
        self.lease_refusals = 0

    # -- tallies, all derived from the phases so they cannot drift ---------

    @property
    def turns(self):
        return sum(p.report.turns for p in self.phases)

    @property
    def tokens_in(self):
        return sum(p.report.tokens_in for p in self.phases)

    @property
    def tokens_out(self):
        return sum(p.report.tokens_out for p in self.phases)

    @property
    def tokens(self):
        return self.tokens_in + self.tokens_out

    @property
    def writes(self):
        return sum(p.report.writes for p in self.phases)

    @property
    def tool_calls(self):
        return sum(p.report.tool_calls for p in self.phases)

    @property
    def checkpoints(self):
        out = []
        for p in self.phases:
            out.extend(p.report.checkpoints)
        return out

    def spend(self):
        """The shared tally, in the shape Ceilings.exceeded reads.

        One expression with two callers, for the reason hearth_workloop._spend
        gives: the budget the user watches and the budget that is enforced
        must be the same arithmetic, or the one they can see will eventually
        be the wrong one."""
        return {"turns": self.turns, "elapsed": self.elapsed,
                "tokens": self.tokens, "writes": self.writes,
                "tool_calls": self.tool_calls}

    def notice(self, text):
        self.notices.append(text)

    @property
    def verified(self):
        """True only if a DETERMINISTIC check passed. A reviewer saying yes is
        not this, and the whole account depends on the difference."""
        return bool(self.completion.get("done") and self.completion.get("verified"))

    def to_dict(self):
        return {
            "run_id": self.run_id, "goal": self.goal, "model": self.model,
            "workspace": self.workspace, "mode": self.mode,
            "stop_reason": self.stop_reason, "stop_detail": self.stop_detail,
            "cycles": self.cycles, "turns": self.turns,
            "elapsed": round(self.elapsed, 2),
            "tokens": self.tokens, "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out, "writes": self.writes,
            "tool_calls": self.tool_calls,
            "ceilings": self.ceilings.to_dict() if self.ceilings else {},
            "roles": [r.to_dict() for r in self.roles],
            "phases": [p.to_dict() for p in self.phases],
            "plan": _clip(self.plan, 2000),
            "last_review": _clip(self.last_review, 2000),
            "reviewer_approved": self.reviewer_approved,
            "verified": self.verified,
            "completion": self.completion,
            "created": self.created, "modified": self.modified,
            "deleted": self.deleted,
            "checkpoints": self.checkpoints,
            "notices": self.notices, "live_workers": self.live_workers,
            "swaps": self.swaps, "swap_seconds": round(self.swap_seconds, 2),
            "lease_refusals": self.lease_refusals,
            "spend": self.spend(),
        }

    def headline(self):
        r = self.stop_reason
        if r == STOP_COMPLETED:
            return "finished the goal in {} phase(s) across {} cycle(s)".format(
                len(self.phases), self.cycles)
        if r == STOP_CEILING:
            return "stopped at a shared ceiling: {}".format(self.stop_detail)
        if r == STOP_EXHAUSTED:
            return "every role had its turn and the goal is still not done: {}".format(
                self.stop_detail)
        if r == STOP_CANCELLED:
            return "stopped by you ({})".format(self.stop_detail or "cancelled")
        if r == STOP_BLOCKED:
            return "stopped needing permission: {}".format(self.stop_detail)
        if r == STOP_ERROR:
            return "stopped on an error: {}".format(self.stop_detail)
        return "still running"

    def render(self):  # noqa: PLR0912 - one linear account, read top to bottom
        L = []
        L.append("hearth swarm -- {}".format(self.headline()))
        L.append("")
        L.append("goal       {}".format(self.goal))
        L.append("workspace  {}".format(self.workspace))
        L.append("model      {}".format(self.model))
        L.append("mode       {} (bypass is not reachable from a swarm)".format(self.mode))
        L.append("run        {}".format(self.run_id))
        L.append("roles      {}".format(", ".join(
            "{}{}".format(r.name, "" if r.writes else " (read-only)")
            for r in self.roles)))
        L.append("")

        L.append("why it stopped")
        L.append("  {}".format(self.headline()))
        if self.completion:
            L.append("  completion check: {}".format(
                self.completion.get("detail")
                or ("passed" if self.completion.get("done") else "not satisfied")))
            out = (self.completion.get("output") or "").strip()
            if out and not self.completion.get("done"):
                L.append("  what the completion check said:")
                for line in out.splitlines()[-12:]:
                    L.append("    {}".format(line[:200]))
        L.append("")

        L.append("who did what")
        if not self.phases:
            L.append("  nothing ran")
        for p in self.phases:
            r = p.report
            changed = len(r.created) + len(r.modified) + len(r.deleted)
            L.append("  {:>2}. cycle {} {:<12} {:>3} turn(s) {:>7,} tok  "
                     "{} file(s)  ended: {}".format(
                         p.index, p.cycle, p.role, r.turns, r.tokens, changed,
                         _phase_ending(p)))
            if r.stop_detail and r.stop_reason not in (STOP_COMPLETED,):
                L.append("        {}".format(r.stop_detail[:150]))
        L.append("")

        if self.plan:
            L.append("the plan the planner produced")
            for line in self.plan.splitlines()[:20]:
                L.append("  {}".format(line[:200]))
            L.append("")

        if self.last_review:
            L.append("what the reviewer said last")
            for line in self.last_review.splitlines()[:20]:
                L.append("  {}".format(line[:200]))
            L.append("")

        L.append("what it spent, as ONE budget shared by every role")
        c = self.ceilings.ceilings if self.ceilings else None
        L.append("  turns    {:>10} of {}".format(self.turns, c.max_turns if c else "-"))
        L.append("  elapsed  {:>10} of {}".format(
            W._hms(self.elapsed), W._hms(c.max_seconds) if c else "-"))
        L.append("  tokens   {:>10,} of {:,}  (in {:,} / out {:,})".format(
            self.tokens, c.max_tokens if c else 0, self.tokens_in, self.tokens_out))
        L.append("  writes   {:>10} of {}   (unattended file writes)".format(
            self.writes, c.max_writes if c else "-"))
        L.append("  tools    {:>10} of {}".format(
            self.tool_calls, c.max_tool_calls if c else "-"))
        L.append("  cycles   {:>10} of {}".format(
            self.cycles, self.ceilings.max_cycles if self.ceilings else "-"))
        if self.swaps:
            L.append("  swaps    {:>10}   ({:.1f}s of wall clock spent loading "
                     "models)".format(self.swaps, self.swap_seconds))
        if self.lease_refusals:
            L.append("  refused  {:>10}   (write attempted by a read-only role and "
                     "stopped by the write lease)".format(self.lease_refusals))
        L.append("")

        L.append("what changed in the workspace")
        if not (self.created or self.modified or self.deleted):
            L.append("  nothing")
        for label, items in (("created", self.created), ("modified", self.modified),
                             ("deleted", self.deleted)):
            if items:
                shown = ", ".join(items[:12])
                more = "" if len(items) <= 12 else " (+{} more)".format(len(items) - 12)
                L.append("  {:9} {}{}".format(label, shown, more))
        cks = self.checkpoints
        if cks:
            L.append("  {} checkpoint(s), latest {}".format(len(cks), cks[-1][:12]))
            L.append("  undo any of them with hearth_checkpoint.restore()")
        L.append("")

        if self.notices:
            L.append("notes")
            for n in self.notices:
                L.append("  {}".format(n))
            L.append("")

        if self.live_workers:
            L.append("WARNING: {} abandoned tool call(s) may still be running against "
                     "this workspace. Cancellation stops the relay waiting; it cannot "
                     "kill a call already in flight.".format(self.live_workers))
            L.append("")

        # ALWAYS, including on a completed run, for the reason
        # hearth_workloop.LoopReport.render gives: "it finished" is the verdict
        # most likely to be over-trusted, and the caveat belongs exactly where
        # it changes what someone believes.
        L.append("what this account cannot tell you")
        if self.reviewer_approved and not self.verified:
            L.append("  REVIEWED BUT NOT VERIFIED. A reviewer role read this work and "
                     "did not object. Nothing ran it. The reviewer is the same kind "
                     "of model as the implementer, reading code it cannot execute, "
                     "so its approval is an opinion and not a check.")
        if self.completion.get("done") and self.completion.get("verified") is False:
            L.append("  NOTHING VERIFIED THIS. No completion check was configured, so "
                     "'finished' here is the model's own claim about its own work.")
        for headline, means, todo in SWARM_BLIND_SPOTS:
            L.append("  * {}".format(headline))
            L.append("      {}".format(means))
            L.append("      {}".format(todo))
        # The single loop's blind spots apply to every phase of this one,
        # because every phase IS one of those loops.
        for headline, means, todo in W.PROGRESS_BLIND_SPOTS:
            L.append("  * {}".format(headline))
            L.append("      {}".format(means))
            L.append("      {}".format(todo))
        return "\n".join(L)


def _phase_ending(p):
    """A short human phrase for why one phase ended."""
    r = p.report.stop_reason
    if r == W.STOP_STALLED:
        return "stopped changing things (handed off)"
    if r == STOP_CEILING and p.bound_by == "role":
        return "used its own turn budget (handed off)"
    if r == STOP_CEILING:
        return "hit the SHARED budget"
    if r == STOP_COMPLETED:
        return "completion check passed"
    return r or "unknown"


# --------------------------------------------------------------------------
# the journal
# --------------------------------------------------------------------------

def journal_dir():
    return os.path.join(hearth_paths.data_dir(), "swarms")


def journal_path(run_id):
    return os.path.join(journal_dir(), "{}.jsonl".format(run_id))


def phase_journal_path(run_id, index):
    """Each phase is a whole work loop and keeps its own loop journal, in the
    swarm's directory rather than the loop's, so a swarm's phases are not
    mixed in with standalone loop runs a user started themselves."""
    return os.path.join(journal_dir(), "{}-p{}.jsonl".format(run_id, index))


def load_journal(run_id, path=None):
    """What is safely known about a previous swarm.

    Same contract, and the same refusal, as hearth_workloop.load_journal: a
    phase that started and never ended is reported as interrupted and is NEVER
    handed back as something to continue. The relay restarts from the next
    phase boundary, because there is no way to know whether the interrupted
    phase's last tool call reached the workspace.
    """
    jr = W.Journal(path or journal_path(run_id), fsync=False)
    records = jr.read()
    header = None
    completed = []
    started = {}
    stopped = None
    for rec in records:
        kind = rec.get("t")
        if kind == "run" and header is None:
            header = rec
        elif kind == "phase_start":
            started[rec.get("index")] = rec
        elif kind == "phase_end":
            started.pop(rec.get("index"), None)
            completed.append(rec)
        elif kind == "stop":
            stopped = rec
    interrupted = min(started) if started else None
    return {"header": header, "completed_phases": completed,
            "interrupted_phase": interrupted, "stopped": stopped,
            "records": records}


# --------------------------------------------------------------------------
# the relay
# --------------------------------------------------------------------------

def run_swarm(goal, model, workspace, *,
              mode="auto", roles=DEFAULT_ROLES, ceilings=None, stall=None,
              gate_policy="deny", approve_fn=None, auto_allow=(),
              done_command=None, done_timeout=300, required_artifacts=None,
              ollama_url=W.DEFAULT_OLLAMA, token=None, emit=None,
              run_id=None, journal=None, resume=False,
              run_phase_fn=None, model_for_role=None, swap_fn=None,
              clock=None, workers=None, execute_tool_fn=None,
              checkpoint_every_turn=True):
    """Work `goal` as a relay of roles. Always returns a SwarmReport.

    Every collaborator is injectable (run_phase_fn, swap_fn, model_for_role,
    clock, execute_tool_fn) so the self-test drives the whole machine with no
    engine, no GPU and no git store. The DEFAULTS are the real thing.

    `run_phase_fn` defaults to hearth_workloop.run_workloop and is the seam
    that makes this module small: this function decides WHO runs, with WHAT
    budget, holding WHICH authority, and what they are told; the phase itself
    is the loop that already exists.
    """
    clock = clock or time.monotonic
    token = token or W.CancelToken()
    emit = emit or (lambda ev: None)
    workers = workers if workers is not None else W._Workers()
    run_phase_fn = run_phase_fn or W.run_workloop
    execute_tool_fn = execute_tool_fn or _default_executor()
    started = clock()

    if mode not in ALLOWED_MODES:
        # Where bypass dies on the way in, exactly as it does for a single
        # loop. Not a flag a caller can talk its way past.
        raise ValueError(
            "permission mode {!r} is not available to a swarm; choose one of {}. "
            "bypass is refused outright: an unattended run must not have an "
            "ungated tool.".format(mode, ", ".join(ALLOWED_MODES)))
    if gate_policy not in GATE_POLICIES:
        raise ValueError("gate_policy must be one of {}".format(", ".join(GATE_POLICIES)))
    roles = tuple(roles)
    if not roles:
        raise ValueError("a swarm needs at least one role")
    writers = [r for r in roles if r.writes]
    if len(writers) > 1:
        # The single-writer property is structural, and a caller that supplies
        # two writing roles has broken it before anything has run. Refused
        # here rather than discovered as a corrupted workspace later.
        raise ValueError(
            "at most one role may write; {} are marked as writers: {}".format(
                len(writers), ", ".join(r.name for r in writers)))

    ceilings = ceilings or SwarmCeilings()
    run_id = run_id or ("swarm-" + uuid.uuid4().hex[:12])
    workspace = os.path.realpath(workspace)
    os.makedirs(workspace, exist_ok=True)
    if journal is None:
        journal = W.Journal(journal_path(run_id))

    report = SwarmReport(goal, model, workspace, mode, run_id, ceilings, roles)
    lease = WriteLease()
    baseline = W.scan_workspace(workspace)
    start_index = 1
    plan = ""
    handoff = ""
    cycle = 1
    #: Position within the CURRENT cycle's role list, kept explicitly rather
    #: than derived from the running phase index. Deriving it was wrong: the
    #: cycle boundary consumed an index of its own, so every cycle after the
    #: first started one place along and ran only the reviewer. See cycle_roles.
    pos = 0

    # ---- resume ---------------------------------------------------------
    if resume:
        prior = load_journal(run_id, getattr(journal, "path", None))
        head = prior.get("header") or {}
        prior_mode = head.get("mode")
        if prior_mode is not None and prior_mode not in ALLOWED_MODES:
            raise ValueError(
                "refusing to resume swarm {}: its journal says mode {!r}".format(
                    run_id, prior_mode))
        done = prior.get("completed_phases") or []
        if done:
            last = done[-1]
            start_index = int(last.get("index", 0)) + 1
            cycle = int(last.get("cycle", 1))
            # Resume at the position AFTER the last completed role in its own
            # cycle, so a relay that died after the implementer comes back to
            # the reviewer rather than restarting the cycle and redoing work.
            # A role the journal names that this build does not have (an older
            # or edited journal) falls back to the start of the cycle, which
            # repeats a phase rather than skipping one.
            names = [r.name for r in cycle_roles(roles, cycle)]
            last_role = last.get("role")
            pos = names.index(last_role) + 1 if last_role in names else 0
            if pos >= len(names):
                cycle += 1
                pos = 0
            plan = last.get("plan_so_far") or ""
            handoff = last.get("handoff") or ""
            if last.get("baseline"):
                baseline = {"files": last["baseline"], "digest": "",
                            "count": len(last["baseline"]), "truncated": False}
            for rec in done:
                report.notice("phase {} ({}) was restored from the journal".format(
                    rec.get("index"), rec.get("role")))
        interrupted = prior.get("interrupted_phase")
        if interrupted is not None:
            # Never resumed, for hearth_workloop's reason: there is no way to
            # know whether its last tool call reached the workspace.
            journal.append({"t": "interrupted", "index": interrupted, "ts": time.time()})
            report.notice(
                "phase {} was interrupted by a restart and was NOT resumed; its last "
                "tool call may or may not have completed. The relay continued from "
                "the next phase.".format(interrupted))
            emit({"type": "notice",
                  "detail": "phase {} interrupted by a restart".format(interrupted)})
            start_index = max(start_index, int(interrupted) + 1)
    else:
        journal.append({
            "t": "run", "version": JOURNAL_VERSION, "run_id": run_id, "goal": goal,
            "model": model, "workspace": workspace, "mode": mode,
            "roles": [r.to_dict() for r in roles],
            "ceilings": ceilings.to_dict(), "gate_policy": gate_policy,
            "done_command": done_command, "ts": time.time(),
        })

    emit({"type": "swarm_start", "run_id": run_id, "goal": goal, "model": model,
          "mode": mode, "ceilings": ceilings.to_dict(),
          "roles": [r.to_dict() for r in roles]})

    resident = {"model": model}

    def _finish(reason, detail):
        report.stop_reason = reason
        report.stop_detail = detail
        report.elapsed = clock() - started
        final = W.scan_workspace(workspace)
        d = W.diff_manifests(baseline, final)
        report.created, report.modified, report.deleted = (
            d["created"], d["modified"], d["deleted"])
        report.live_workers = workers.live()
        report.lease_refusals = sum(
            getattr(p.report, "lease_refusals", 0) for p in report.phases)
        try:
            journal.append({"t": "stop", "reason": reason, "detail": detail,
                            "ts": time.time(), "summary": report.to_dict()})
        except OSError:
            pass
        emit({"type": "swarm_stop", "reason": reason, "detail": detail,
              "report": report.to_dict()})
        return report

    index = start_index
    try:
        while True:
            if token.cancelled():
                return _finish(STOP_CANCELLED, token.reason)

            report.elapsed = clock() - started
            hit = _global_exhausted(ceilings.ceilings, report.spend())
            if hit is not None:
                name, limit, value = hit
                return _finish(STOP_CEILING, "{} ceiling reached ({} of {})".format(
                    name, W._hms(value) if name == "wall clock" else value,
                    W._hms(limit) if name == "wall clock" else limit))

            todo = cycle_roles(roles, cycle)
            if pos >= len(todo):
                # This cycle's sequence is finished. Start the next one, or
                # stop: the relay is out of ideas rather than out of budget,
                # which is a different fact and gets its own stop reason.
                cycle += 1
                pos = 0
                report.cycles = cycle - 1
                if cycle > ceilings.max_cycles:
                    return _finish(STOP_EXHAUSTED,
                                   "the relay completed {} cycle(s) and the "
                                   "completion check still does not pass".format(
                                       ceilings.max_cycles))
                continue
            role = todo[pos]
            report.cycles = max(report.cycles, cycle)

            # ---- pick the model, and pay for a swap if the role wants one --
            want = model
            if model_for_role is not None:
                try:
                    want = model_for_role(role, model) or model
                except Exception:  # noqa: BLE001 - a broken router must not end a run
                    want = model
            if want != resident["model"]:
                t_swap = clock()
                ok = True
                if swap_fn is not None:
                    try:
                        ok, _ = W._call_cancellable(
                            lambda w=want: swap_fn(w), token, workers)
                    except Exception as exc:  # noqa: BLE001
                        report.notice("could not load {} for the {} role ({}); it ran "
                                      "on {} instead".format(want, role.name, exc,
                                                             resident["model"]))
                        want = resident["model"]
                if not ok:
                    return _finish(STOP_CANCELLED, token.reason)
                if want != resident["model"]:
                    report.swaps += 1
                    report.swap_seconds += clock() - t_swap
                    resident["model"] = want
                    emit({"type": "swarm_swap", "role": role.name, "model": want,
                          "seconds": round(clock() - t_swap, 2)})

            residual = _residual(ceilings.ceilings, report.spend())
            phase_ceil = _phase_ceilings(residual, role)
            composed = compose_goal(goal, role, handoff, cycle, plan)

            journal.append({"t": "phase_start", "index": index, "cycle": cycle,
                            "role": role.name, "model": resident["model"],
                            "ts": time.time()})
            emit({"type": "phase_start", "index": index, "cycle": cycle,
                  "role": role.name, "model": resident["model"],
                  "writes": role.writes, "ceilings": phase_ceil.to_dict(),
                  "tools": sorted(role.tools)})

            # ---- the write lease ---------------------------------------
            # Held for exactly the duration of the phase, by exactly the role
            # that is running, and only if that role is a writer. The `with`
            # is what guarantees release even when the phase raises.
            refusals = {"n": 0}

            def _refused(name, _r=refusals):
                _r["n"] += 1
                emit({"type": "lease_refused", "role": role.name, "tool": name})

            watcher = _PhaseWatcher(emit, role, index, cycle)
            with WriteLease() as phase_lease:
                if role.writes:
                    phase_lease.acquire(role.name)
                executor = _leased_executor(execute_tool_fn, phase_lease,
                                            role.name, on_refusal=_refused)
                try:
                    phase_report = run_phase_fn(
                        composed, resident["model"], workspace,
                        mode=mode, allowed_tools=role.tools, auto_allow=auto_allow,
                        gate_policy=gate_policy, approve_fn=approve_fn,
                        ceilings=phase_ceil, stall=stall,
                        done_command=done_command, done_timeout=done_timeout,
                        required_artifacts=required_artifacts,
                        ollama_url=ollama_url, token=token,
                        emit=watcher,
                        run_id="{}-p{}".format(run_id, index),
                        journal=W.Journal(phase_journal_path(run_id, index)),
                        execute_tool_fn=executor, workers=workers,
                        checkpoint_every_turn=checkpoint_every_turn)
                except Exception as exc:  # noqa: BLE001 - a relay must end with a reason
                    return _finish(STOP_ERROR, "{}: {}".format(type(exc).__name__, exc))

            phase_report.lease_refusals = refusals["n"]
            # What the role said, taken from the event stream it just wrote.
            # A phase report that already carries final_text (only a test
            # fixture does) keeps it, so an injected phase can still be given
            # words to say without pretending to emit them.
            phase_report.final_text = (getattr(phase_report, "final_text", "")
                                       or watcher.last_said)

            completion = dict(phase_report.completion or {})
            rec = PhaseRecord(index, cycle, role.name, resident["model"],
                              phase_report, None, completion)
            # Appended BEFORE bound_by is computed. SwarmReport.spend() sums
            # over report.phases, and _bound_by asks whether the GLOBAL budget
            # is now exhausted -- a question that has to include the phase that
            # just ran. Computing it first under-counted by exactly one phase,
            # so the phase that actually used the last of the shared budget was
            # reported as having merely used its own turn cap ("handed off")
            # rather than as having hit the shared ceiling. That is the one
            # distinction this field exists to make.
            report.phases.append(rec)
            report.elapsed = clock() - started
            rec.bound_by = _bound_by(phase_report, role, residual,
                                     ceilings.ceilings, report, phase_ceil)
            bound_by = rec.bound_by
            # Only a DETERMINISTIC verdict is the run's completion state. An
            # unverified self-claim is a statement about one phase, and letting
            # it into the report here would make the account say "completion
            # check: the model declared the goal complete" on a run where no
            # check ever ran. The writer's self-claim is recorded explicitly in
            # the terminal block below, where it is labelled unverified.
            if completion.get("verified"):
                report.completion = completion

            # ---- what this phase leaves behind --------------------------
            # Computed BEFORE the journal record that carries it. Writing the
            # record first and building the handoff afterwards is not a
            # cosmetic ordering: it persisted an empty handoff, so a relay
            # resumed after a crash restarted its next role with no idea what
            # the previous one had done or what the completion check had said.
            # The failure is silent -- the role simply begins again from the
            # goal -- which is the same shape of bug as the cycle sequencing
            # one, and is why _self_test now asserts on the journal's contents
            # rather than only on its record count.
            handoff = build_handoff(role, phase_report, completion)
            if role.name == "planner" and phase_report.final_text:
                plan = phase_report.final_text
                report.plan = plan
            if role.name == "reviewer" and phase_report.final_text:
                report.last_review = phase_report.final_text
                report.reviewer_approved = _reads_as_approval(phase_report.final_text)

            journal.append({
                "t": "phase_end", "index": index, "cycle": cycle, "role": role.name,
                "ts": time.time(), "bound_by": bound_by,
                "stop_reason": phase_report.stop_reason,
                "baseline": baseline.get("files"),
                "handoff": handoff, "plan_so_far": plan,
                "summary": rec.to_dict(),
            })
            emit({"type": "phase_end", "index": index, "cycle": cycle,
                  "role": role.name, "bound_by": bound_by,
                  "stop_reason": phase_report.stop_reason,
                  "summary": rec.to_dict(),
                  "spend": report.spend()})

            # ---- terminal conditions -----------------------------------
            if phase_report.stop_reason == STOP_CANCELLED:
                return _finish(STOP_CANCELLED, token.reason)
            if phase_report.stop_reason == STOP_BLOCKED:
                return _finish(STOP_BLOCKED, phase_report.stop_detail)
            if phase_report.stop_reason == STOP_ERROR:
                return _finish(STOP_ERROR, phase_report.stop_detail)
            # A completion is only the RUN's completion when a deterministic
            # gate produced it. hearth_workloop reports two different things
            # through the same field and marks them apart with `verified`:
            #
            #   verified=True   a done_command exited 0, or every required
            #                   artifact exists. This ends the swarm.
            #   verified=False  the phase's own model emitted GOAL COMPLETE and
            #                   no gate was configured to check it.
            #
            # Testing only `done` treated both as finished, and that was a real
            # bug with a live symptom: every role's instruction tells it to
            # reply GOAL COMPLETE when its own job is finished, so a PLANNER
            # saying "I have finished planning" ended the entire relay as if
            # the goal had been achieved. Any run without a completion check
            # stopped after phase one having changed nothing. Caught by a live
            # cancellation test that never got as far as cancelling anything.
            if completion.get("done") and completion.get("verified"):
                return _finish(STOP_COMPLETED,
                               completion.get("detail") or "completion check passed")
            if completion.get("done") and not completion.get("verified"):
                # An unverified self-claim. From the role that can actually
                # change files this is the same claim a single work loop
                # accepts, and it is accepted here on the same terms: the run
                # ends and the account says nothing verified it. From a
                # read-only role it means only "I have finished MY phase", so
                # the relay hands off and keeps going.
                if role.writes:
                    report.completion = {
                        "done": True, "verified": False,
                        "detail": "the {} declared the goal complete; no completion "
                                  "check was configured to verify it".format(role.name)}
                    return _finish(STOP_COMPLETED,
                                   "the {} declared the goal complete (unverified: no "
                                   "completion check was set)".format(role.name))
                # Not the run's verdict, so it must not be left sitting in the
                # report as though it were.
                report.completion = {}
            elif phase_report.stop_reason == STOP_COMPLETED and not completion:
                # A phase that claims completion but reports nothing about it.
                # hearth_workloop does not currently produce this shape; a
                # writing role that somehow did must still not fall through
                # into another cycle as though it had said nothing.
                if role.writes:
                    report.completion = {
                        "done": True, "verified": False,
                        "detail": "the {} stopped as complete without reporting a "
                                  "check".format(role.name)}
                    return _finish(STOP_COMPLETED,
                                   "the {} declared the goal complete (unverified: no "
                                   "completion check was set)".format(role.name))

            report.elapsed = clock() - started
            hit = _global_exhausted(ceilings.ceilings, report.spend())
            if hit is not None:
                name, limit, value = hit
                return _finish(STOP_CEILING, "{} ceiling reached ({} of {})".format(
                    name, W._hms(value) if name == "wall clock" else value,
                    W._hms(limit) if name == "wall clock" else limit))
            index += 1
            pos += 1

    except Exception as exc:  # noqa: BLE001 - a relay must always end with a reason
        return _finish(STOP_ERROR, "{}: {}".format(type(exc).__name__, exc))


def _default_executor():
    """hearth_tools.execute_tool, imported lazily.

    Deferred so this module can be imported, self-tested and reasoned about
    without pulling in the whole tool layer, which is what lets the self-test
    run with no workspace machinery at all."""
    import hearth_tools  # noqa: PLC0415
    return hearth_tools.execute_tool


def cycle_roles(roles, cycle):
    """The roles that run in `cycle`, in order.

    The planner runs ONCE, in cycle 1, and the implementer/reviewer pair
    repeats after it. A planner that ran again every cycle would spend a phase
    re-reading a workspace it has already described, and would overwrite a
    plan that the reviewer's critique has since made more specific.

    Expressed as "the roles for a cycle" rather than as arithmetic on a
    running phase index, because the index version was wrong and quietly so:
    it consumed a position for the cycle boundary itself, so cycle 2 onwards
    started one place further along and ran ONLY the reviewer. The relay's
    whole mechanism is the implementer revising from a clean-context critique,
    and that never happened after the first cycle. _self_test walks the full
    sequence for four cycles rather than spot-checking one, because the first
    version passed every spot check it was given.
    """
    if not roles:
        return ()
    if cycle <= 1 or roles[0].name != "planner":
        return tuple(roles)
    return tuple(roles[1:]) or tuple(roles)


class _PhaseWatcher:
    """Tags every event a phase emits with the role that produced it, and
    keeps the last thing that role actually said.

    Two jobs in one object because they read the same stream. The tagging is
    most of how a swarm explains itself: without it the transcript is an
    undifferentiated run of events and "which role did what" is unanswerable
    afterwards.

    The capture exists because there is nowhere else to get the text.
    LoopReport.turn_log records what a turn DID (tools, files, errors,
    timings) and deliberately not what it SAID, and the conversation itself is
    not carried across a handoff by design. The one place a phase's prose
    reliably appears is the `message` event hearth_workloop emits for every
    non-empty assistant reply, so that is what is read.

    This was a real bug first: an earlier version read a "said" key off
    turn_log that hearth_workloop has never written, so every handoff carried
    the model's words as an empty string. Nothing caught it, because the
    self-test drove injected phase reports and a fake can be given any shape
    you please. _self_test now feeds this the exact event dict
    hearth_workloop actually emits.
    """

    def __init__(self, emit, role, index, cycle):
        self._emit = emit
        self._role = role
        self._index = index
        self._cycle = cycle
        self.last_said = ""

    def __call__(self, ev):
        if ev.get("type") == "message" and ev.get("role") == "assistant":
            text = (ev.get("content") or "").strip()
            if text:
                self.last_said = text
        tagged = dict(ev)
        # The phase's own role, which shadows the "assistant"/"manager" role
        # field an inner event may carry. Written after the copy so a
        # message event's own `role` cannot survive into the swarm stream and
        # be mistaken for the role that produced it.
        tagged["role"] = self._role.name
        tagged["phase"] = self._index
        tagged["cycle"] = self._cycle
        self._emit(tagged)


def _bound_by(phase_report, role, residual, global_ceilings, report, phase_ceil):
    """Which bound ended this phase: its own, the shared one, or neither.

    The distinction the module docstring's ONE BUDGET section is about. A
    phase that stops for STOP_CEILING could have hit either, and they mean
    opposite things: the role's own cap is a normal handoff, the global one
    ends the swarm.
    """
    if phase_report.stop_reason != STOP_CEILING:
        return phase_report.stop_reason or "unknown"
    # If the shared budget is now exhausted, that is what bit -- checked
    # against the GLOBAL tally, not the phase's, because the phase only ever
    # saw a residual.
    if _global_exhausted(global_ceilings, report.spend()) is not None:
        return "global"
    # The phase's turn cap came from the role, and the rest came from the
    # residual. If turns is what it reached and the role is what set it, the
    # role's own cap is the honest answer.
    if (role.max_turns is not None and phase_ceil.max_turns == role.max_turns
            and phase_report.turns >= role.max_turns):
        return "role"
    return "global"


#: Phrases a reviewer uses when it is NOT objecting. Matched only to decide
#: whether the account must print REVIEWED BUT NOT VERIFIED -- never to decide
#: whether a run is finished, which only the deterministic gate can do. A
#: false positive here therefore costs a warning that was not needed, and a
#: false negative costs a warning that was. Both are safe; completing a run on
#: this would not be, which is why nothing does.
_APPROVAL_PHRASES = (
    "looks correct", "looks good", "no issues", "no problems", "correct as written",
    "i see no", "nothing wrong", "appears correct", "is correct", "lgtm",
    "no changes needed", "no further changes", "ready to ship",
)
_OBJECTION_PHRASES = (
    "root cause", "must change", "is wrong", "fails", "bug", "incorrect",
    "does not handle", "missing", "should be", "error",
)


def _reads_as_approval(text):
    """Whether a reviewer's closing text reads as approval rather than a
    finding. Heuristic and admitted to be one; see _APPROVAL_PHRASES."""
    low = (text or "").lower()
    if not low.strip():
        return False
    if any(p in low for p in _OBJECTION_PHRASES):
        return False
    return any(p in low for p in _APPROVAL_PHRASES)


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def _self_test():  # noqa: PLR0915 - one function on purpose, per house style
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    tmp = tempfile.mkdtemp(prefix="swarmloop-")

    def ws(name):
        d = os.path.join(tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def jr(name):
        return W.Journal(os.path.join(tmp, name + ".jsonl"), fsync=False)

    def fake_report(*, stop_reason=W.STOP_STALLED, turns=1, tokens_in=10,
                    tokens_out=20, writes=0, tool_calls=1, said="",
                    completion=None, created=(), elapsed=0.0):
        r = W.LoopReport("g", "m", "/w", "auto", "rid", W.Ceilings(), W.StallPolicy())
        r.stop_reason = stop_reason
        r.stop_detail = "detail"
        r.turns = turns
        r.tokens_in = tokens_in
        r.tokens_out = tokens_out
        r.writes = writes
        r.tool_calls = tool_calls
        r.elapsed = elapsed
        r.created = list(created)
        r.completion = completion or {}
        r.turn_log = [{"turn": 1, "said": said}] if said else []
        # The relay sets final_text from the turn log before it builds a
        # handoff (see run_swarm), so a fixture that left it unset would let a
        # handoff test pass against a report shape that never occurs.
        r.final_text = said
        return r

    # === roles are read-only unless they are the writer ==================
    assert not (READ_ONLY_TOOLS & _tools_by_risk("edit")), sorted(READ_ONLY_TOOLS)
    assert "write_file" in IMPLEMENTER_TOOLS and "read_file" in IMPLEMENTER_TOOLS
    assert "write_file" not in READ_ONLY_TOOLS and "edit_file" not in READ_ONLY_TOOLS
    # run_command is dangerous, not "edit": it must not arrive in a role by
    # risk class, or an unattended role would quietly hold a shell.
    assert "run_command" not in IMPLEMENTER_TOOLS, sorted(IMPLEMENTER_TOOLS)
    assert "run_command" not in READ_ONLY_TOOLS
    assert PLANNER.writes is False and REVIEWER.writes is False
    assert IMPLEMENTER.writes is True
    assert len([r for r in DEFAULT_ROLES if r.writes]) == 1, "exactly one writer"
    # Every role's manifest is a subset of what a loop may EVER be given, so a
    # role cannot be a way to reach a tool the loop layer refuses.
    for r in DEFAULT_ROLES:
        assert r.tools <= W.DEFAULT_ALLOWED_TOOLS, (r.name, sorted(r.tools))

    # A read-only role holding a write tool is unrepresentable.
    try:
        RoleSpec("bad", "p", "i", {"read_file", "write_file"}, writes=False)
        raise AssertionError("a read-only role must not be constructible with a "
                             "write tool in its manifest")
    except ValueError as exc:
        assert "read-only" in str(exc), str(exc)

    # === the write lease =================================================
    lease = WriteLease()
    assert lease.holder is None and not lease.held_by("implementer")
    lease.acquire("implementer")
    assert lease.held_by("implementer") and not lease.held_by("reviewer")
    try:
        lease.acquire("reviewer")
        raise AssertionError("the lease must be exclusive; a second acquire has to "
                             "raise rather than block or silently succeed")
    except LeaseError as exc:
        assert "already held" in str(exc), str(exc)
    try:
        lease.release("reviewer")
        raise AssertionError("only the holder may release the lease")
    except LeaseError:
        pass
    lease.release("implementer")
    assert lease.holder is None
    # The context manager releases even when the body raises.
    try:
        with WriteLease() as lz:
            lz.acquire("implementer")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert lz.holder is None, "the lease must be released when a phase raises"

    # === the leased executor refuses a write without the lease ===========
    calls = []

    def _exec(name, args, workspace):
        calls.append(name)
        return "ran " + name

    held = WriteLease().acquire("implementer")
    ex_impl = _leased_executor(_exec, held, "implementer")
    assert ex_impl("write_file", {}, "/w") == "ran write_file"
    assert calls == ["write_file"]

    refused = []
    unheld = WriteLease()
    ex_rev = _leased_executor(_exec, unheld, "reviewer", on_refusal=refused.append)
    out = ex_rev("write_file", {}, "/w")
    assert out.startswith("denied:"), (
        "a read-only role's write must never reach the real executor: the write "
        "lease is the second of the two independent layers that keep this relay "
        "to a single writer", out)
    assert "write lease" in out, out
    assert refused == ["write_file"], refused
    assert calls == ["write_file"], ("the executor must never have been reached for a "
                                     "refused write", calls)
    # A read still goes through for a role with no lease: the lease bounds
    # writes, and nothing else.
    assert ex_rev("read_file", {}, "/w") == "ran read_file"
    assert calls == ["write_file", "read_file"]

    # === ONE budget shared across roles ==================================
    base = W.Ceilings(max_turns=10, max_seconds=100, max_tokens=1000,
                      max_writes=10, max_tool_calls=50)
    left = _residual(base, {"turns": 4, "elapsed": 30.0, "tokens": 400,
                            "writes": 3, "tool_calls": 20})
    assert left.max_turns == 6 and left.max_tokens == 600, (
        "the shared budget must bound the SUM across roles: a residual that "
        "ignores what previous roles spent gives every role a full budget, "
        "which is the brief's named bug", left.to_dict())
    assert left.max_writes == 7 and left.max_tool_calls == 30
    assert abs(left.max_seconds - 70.0) < 1e-6

    # Overspend clamps to 0 and NEVER goes negative. hearth_workloop reads a
    # negative ceiling as UNLIMITED, so a swarm that overspent would hand its
    # next role an unbounded phase -- the exact inversion this clamp prevents.
    over = _residual(base, {"turns": 99, "elapsed": 999.0, "tokens": 9999,
                            "writes": 99, "tool_calls": 999})
    for k, v in over.to_dict().items():
        assert v == 0, ("an exhausted budget must clamp to 0, not go negative: "
                        "hearth_workloop treats a negative limit as unlimited", k, v)
    assert over.exceeded({"turns": 0}) is not None, (
        "a zero ceiling must read as exceeded, which is what stops the next role")

    # A phase's ceiling is the role cap narrowed by the residual, never widened.
    pc = _phase_ceilings(W.Ceilings(max_turns=2, max_tokens=50), IMPLEMENTER)
    assert pc.max_turns == 2, ("the residual must win when it is smaller than the "
                               "role cap", pc.max_turns)
    pc2 = _phase_ceilings(W.Ceilings(max_turns=100, max_tokens=50), REVIEWER)
    assert pc2.max_turns == REVIEWER.max_turns == 3, pc2.max_turns
    assert pc2.max_tokens == 50, "non-turn ceilings come straight from the residual"

    # === the relay sequence ==============================================
    # The planner runs once; the implementer/reviewer pair repeats. Checked
    # across FOUR cycles, not one, because the previous implementation
    # derived the position from a running phase index, passed every
    # single-cycle spot check, and ran ONLY the reviewer from cycle 2 onward
    # -- which silently removed the entire mechanism this module exists for
    # (the implementer revising from a clean-context critique).
    assert [r.name for r in cycle_roles(DEFAULT_ROLES, 1)] == [
        "planner", "implementer", "reviewer"], cycle_roles(DEFAULT_ROLES, 1)
    for c in (2, 3, 4):
        got = [r.name for r in cycle_roles(DEFAULT_ROLES, c)]
        assert got == ["implementer", "reviewer"], (
            "cycle {} must be implementer then reviewer; a cycle that drops the "
            "implementer cannot act on the review and the relay does nothing"
            .format(c), got)

    # And the same property through the real loop: walk the whole sequence the
    # way run_swarm walks it and assert the flattened order.
    walked = []
    _cycle, _pos = 1, 0
    for _ in range(20):
        _todo = cycle_roles(DEFAULT_ROLES, _cycle)
        if _pos >= len(_todo):
            _cycle += 1
            _pos = 0
            if _cycle > 4:
                break
            continue
        walked.append("c{}:{}".format(_cycle, _todo[_pos].name))
        _pos += 1
    assert walked == [
        "c1:planner", "c1:implementer", "c1:reviewer",
        "c2:implementer", "c2:reviewer",
        "c3:implementer", "c3:reviewer",
        "c4:implementer", "c4:reviewer"], walked

    # A role set with no planner repeats in full every cycle.
    _pair = (IMPLEMENTER, REVIEWER)
    assert [r.name for r in cycle_roles(_pair, 3)] == ["implementer", "reviewer"]
    # A lone planner is not silently dropped into an empty cycle.
    assert [r.name for r in cycle_roles((PLANNER,), 2)] == ["planner"]

    # === a full relay, driven by injected phases =========================
    order = []

    def phases_until(n_success):
        """A run_phase_fn that reports the completion check failing until the
        n-th phase, then passing."""
        state = {"n": 0}

        def _run(goal, model, workspace, **kw):
            state["n"] += 1
            order.append((kw.get("allowed_tools"), goal.split("\n")[0]))
            done = state["n"] >= n_success
            comp = {"done": done, "verified": True,
                    "detail": "passed" if done else "exit 1",
                    "output": "" if done else "AssertionError: nope"}
            return fake_report(
                stop_reason=STOP_COMPLETED if done else W.STOP_STALLED,
                said="phase {} spoke".format(state["n"]), completion=comp,
                tokens_in=100, tokens_out=100, turns=2)
        return _run

    d = ws("relay")
    rep = run_swarm("make it pass", "m", d, mode="auto",
                    ceilings=SwarmCeilings(W.Ceilings(max_turns=50, max_tokens=10000)),
                    done_command="pytest", run_phase_fn=phases_until(3),
                    journal=jr("relay"), execute_tool_fn=_exec)
    assert rep.stop_reason == STOP_COMPLETED, (rep.stop_reason, rep.stop_detail)
    assert [p.role for p in rep.phases] == ["planner", "implementer", "reviewer"], \
        [p.role for p in rep.phases]
    # Every role saw the goal, and the ROLE line names the right one.
    assert order[0][1].startswith("ROLE: PLANNER"), order[0][1]
    assert order[1][1].startswith("ROLE: IMPLEMENTER"), order[1][1]
    # The manifests handed to each phase are the role's, not the loop default.
    assert order[0][0] == READ_ONLY_TOOLS, "the planner must be read-only"
    assert order[1][0] == IMPLEMENTER_TOOLS, "the implementer is the writer"
    assert order[2][0] == READ_ONLY_TOOLS, "the reviewer must be read-only"
    # Tallies are the SUM across roles, not one role's.
    assert rep.tokens == 600, rep.tokens          # 3 phases x 200
    assert rep.turns == 6, rep.turns              # 3 phases x 2
    assert rep.verified is True

    # === the shared ceiling stops the relay, and it is the SHARED one ====
    # Three phases would spend 600 tokens; the budget is 250, so the relay
    # must stop partway rather than letting each role have 250 of its own.
    d2 = ws("ceil")
    rep2 = run_swarm("g", "m", d2, mode="auto",
                     ceilings=SwarmCeilings(W.Ceilings(max_turns=50, max_tokens=250)),
                     done_command="pytest", run_phase_fn=phases_until(99),
                     journal=jr("ceil"), execute_tool_fn=_exec)
    assert rep2.stop_reason == STOP_CEILING, (rep2.stop_reason, rep2.stop_detail)
    assert "tokens" in rep2.stop_detail, rep2.stop_detail
    assert rep2.tokens <= 400, ("the shared budget must bound the SUM across roles; "
                                "a per-role budget would let this reach 600",
                                rep2.tokens)
    assert len(rep2.phases) < 3, ("the relay must stop mid-sequence when the shared "
                                  "budget runs out", len(rep2.phases))

    # === "used its own turns" vs "hit the SHARED budget" =================
    # The two look identical coming out of run_workloop (both STOP_CEILING)
    # and mean opposite things: the first is a normal handoff, the second ends
    # the relay. bound_by is the only place that distinction is recorded, and
    # it has to count the phase that just finished -- computing it before the
    # phase was added to the tally under-counted by exactly one phase, so the
    # phase that spent the last of the shared budget was reported as merely
    # having handed off.
    def ceiling_phase(goal, model, workspace, **kw):
        # Spends its whole allowance, whatever it was given.
        allowed = kw["ceilings"].max_turns
        return fake_report(stop_reason=STOP_CEILING, turns=allowed,
                           tokens_in=0, tokens_out=0, said="spent it")

    d_b = ws("boundby")
    rep_b = run_swarm("g", "m", d_b, mode="auto",
                      # 4 turns globally: the planner's own cap is 4, so phase 1
                      # reaches both at once and the SHARED one is what matters.
                      ceilings=SwarmCeilings(W.Ceilings(max_turns=4, max_tokens=10 ** 6),
                                             max_cycles=1),
                      run_phase_fn=ceiling_phase, journal=jr("boundby"),
                      execute_tool_fn=_exec)
    assert rep_b.stop_reason == STOP_CEILING, rep_b.stop_reason
    assert rep_b.phases[0].bound_by == "global", (
        "a phase that spends the last of the SHARED budget must be reported as "
        "hitting the shared ceiling, not as having handed off",
        rep_b.phases[0].bound_by)

    # And the other way: plenty of global budget, so the role's own cap is what
    # ended the phase and the relay carries on.
    d_b2 = ws("boundby2")
    rep_b2 = run_swarm("g", "m", d_b2, mode="auto",
                       ceilings=SwarmCeilings(W.Ceilings(max_turns=500,
                                                         max_tokens=10 ** 6),
                                              max_cycles=1),
                       run_phase_fn=ceiling_phase, journal=jr("boundby2"),
                       execute_tool_fn=_exec)
    assert rep_b2.phases[0].bound_by == "role", (
        "a phase that only used its OWN turn cap has handed off, not run out",
        rep_b2.phases[0].bound_by)
    assert len(rep_b2.phases) == 3, ("the relay must continue after a role hands "
                                     "off", len(rep_b2.phases))

    # === a cycle bound, so a relay whose gate never passes still ends ====
    d3 = ws("cycles")
    rep3 = run_swarm("g", "m", d3, mode="auto",
                     ceilings=SwarmCeilings(W.Ceilings(max_turns=500, max_tokens=10 ** 7),
                                            max_cycles=2),
                     done_command="pytest", run_phase_fn=phases_until(99),
                     journal=jr("cycles"), execute_tool_fn=_exec)
    assert rep3.stop_reason == STOP_EXHAUSTED, (rep3.stop_reason, rep3.stop_detail)
    assert rep3.cycles <= 2 + 1, rep3.cycles

    # === bypass is unreachable, at construction ==========================
    for bad_mode in ("bypass",):
        try:
            run_swarm("g", "m", ws("bp"), mode=bad_mode, run_phase_fn=phases_until(1),
                      journal=jr("bp"), execute_tool_fn=_exec)
            raise AssertionError("bypass must be refused outright")
        except ValueError as exc:
            assert "bypass" in str(exc), str(exc)
    assert "bypass" not in ALLOWED_MODES, ALLOWED_MODES

    # Two writing roles are refused before anything runs.
    try:
        run_swarm("g", "m", ws("two"), mode="auto",
                  roles=(IMPLEMENTER, RoleSpec("second", "p", "i", IMPLEMENTER_TOOLS,
                                               writes=True)),
                  run_phase_fn=phases_until(1), journal=jr("two"),
                  execute_tool_fn=_exec)
        raise AssertionError("two writing roles must be refused")
    except ValueError as exc:
        assert "at most one role may write" in str(exc), str(exc)

    # === cancellation stops the relay, and no later phase starts =========
    tok = W.CancelToken()
    ran = []

    def cancel_on_second(goal, model, workspace, **kw):
        ran.append(kw.get("run_id"))
        if len(ran) == 1:
            tok.cancel("you pressed Stop")
            return fake_report(stop_reason=STOP_CANCELLED, said="stopped")
        raise AssertionError("no phase may start after cancellation")

    d4 = ws("cancel")
    rep4 = run_swarm("g", "m", d4, mode="auto", token=tok,
                     run_phase_fn=cancel_on_second, journal=jr("cancel"),
                     execute_tool_fn=_exec)
    assert rep4.stop_reason == STOP_CANCELLED, rep4.stop_reason
    assert "Stop" in rep4.stop_detail, rep4.stop_detail
    assert len(ran) == 1, ran

    # === a reviewer's approval NEVER completes a run =====================
    def reviewer_approves(goal, model, workspace, **kw):
        role_line = goal.split("\n")[0]
        # No completion check configured at all, and the reviewer gushes.
        if "REVIEWER" in role_line:
            return fake_report(stop_reason=STOP_COMPLETED,
                               said="This looks correct. No issues found. LGTM.")
        return fake_report(stop_reason=W.STOP_STALLED, said="did some work")

    d5 = ws("approve")
    rep5 = run_swarm("g", "m", d5, mode="auto",
                     ceilings=SwarmCeilings(W.Ceilings(max_turns=100, max_tokens=10 ** 6),
                                            max_cycles=1),
                     run_phase_fn=reviewer_approves, journal=jr("approve"),
                     execute_tool_fn=_exec)
    assert rep5.stop_reason != STOP_COMPLETED, (
        "a reviewer's approval must never complete a run", rep5.stop_reason)
    assert rep5.reviewer_approved is True, "the approval is still recorded"
    assert rep5.verified is False
    acct = rep5.render()
    assert "REVIEWED BUT NOT VERIFIED" in acct, (
        "an account whose only evidence is one model approving another must say so")
    assert "opinion and not a check" in acct

    assert _reads_as_approval("This looks correct, no issues.") is True
    assert _reads_as_approval("The root cause is an off-by-one.") is False
    assert _reads_as_approval("") is False
    # An objection anywhere wins over approving language: the cost of a missed
    # warning is higher than the cost of an extra one.
    assert _reads_as_approval("Looks good overall but there is a bug in parse()") is False

    # === a READ-ONLY role saying GOAL COMPLETE ends its PHASE, not the run ==
    # Every role's instruction tells it to reply GOAL COMPLETE when its own job
    # is done, and hearth_workloop turns that into
    # completion={"done": True, "verified": False} whenever no gate is
    # configured. Treating that as the RUN's completion made a planner saying
    # "I have finished planning" end the whole relay, so any run without a
    # completion check stopped after phase one having changed nothing. It was a
    # live failure, not a hypothetical: a cancellation test could not get far
    # enough to cancel anything because the run kept finishing on the planner.
    planner_claims = []

    def planner_says_done(goal, model, workspace, **kw):
        role_line = goal.split("\n")[0]
        planner_claims.append(role_line.split()[1])
        # Exactly what run_workloop produces for an unverified self-claim.
        return fake_report(
            stop_reason=STOP_COMPLETED, said="GOAL COMPLETE",
            completion={"done": True, "verified": False,
                        "detail": "the model declared the goal complete; no "
                                  "completion check was configured to verify it"})

    d_p = ws("planner-claim")
    rep_p = run_swarm("g", "m", d_p, mode="auto",
                      ceilings=SwarmCeilings(W.Ceilings(max_turns=100,
                                                        max_tokens=10 ** 6),
                                             max_cycles=1),
                      run_phase_fn=planner_says_done, journal=jr("planner-claim"),
                      execute_tool_fn=_exec)
    assert planner_claims[0] == "PLANNER", planner_claims
    assert len(planner_claims) >= 2, (
        "a read-only role's GOAL COMPLETE must end its PHASE and hand off, not "
        "end the whole relay before anything could change a file", planner_claims)
    assert "IMPLEMENTER" in planner_claims, planner_claims
    # The implementer's own unverified claim DOES end it, labelled as such.
    assert rep_p.stop_reason == STOP_COMPLETED, rep_p.stop_reason
    assert rep_p.verified is False
    assert "unverified" in rep_p.stop_detail, rep_p.stop_detail
    assert "implementer" in rep_p.stop_detail, rep_p.stop_detail

    # A read-only role's unverified claim must not be left in the report as
    # though a check had run.
    d_p2 = ws("planner-only")
    rep_p2 = run_swarm("g", "m", d_p2, mode="auto", roles=(PLANNER,),
                       ceilings=SwarmCeilings(W.Ceilings(max_turns=100), max_cycles=1),
                       run_phase_fn=planner_says_done, journal=jr("planner-only"),
                       execute_tool_fn=_exec)
    assert rep_p2.stop_reason == STOP_EXHAUSTED, (
        "a relay of only read-only roles cannot finish a goal", rep_p2.stop_reason)
    assert not rep_p2.completion.get("done"), (
        "a read-only role's self-claim must not become the run's completion "
        "state", rep_p2.completion)
    assert rep_p2.verified is False
    assert "completion check: " not in rep_p2.render().split("what it spent")[0] or \
        "not satisfied" in rep_p2.render(), rep_p2.render()[:400]

    # A VERIFIED completion from any role does end the run: that is the gate.
    def verified_done(goal, model, workspace, **kw):
        return fake_report(stop_reason=STOP_COMPLETED,
                           completion={"done": True, "verified": True,
                                       "detail": "completion command exited 0"})

    d_v = ws("verified")
    rep_v = run_swarm("g", "m", d_v, mode="auto", done_command="pytest",
                      ceilings=SwarmCeilings(max_cycles=1),
                      run_phase_fn=verified_done, journal=jr("verified"),
                      execute_tool_fn=_exec)
    assert rep_v.stop_reason == STOP_COMPLETED and rep_v.verified is True, (
        rep_v.stop_reason, rep_v.completion)
    assert len(rep_v.phases) == 1, ("a verified gate ends the relay at once",
                                    len(rep_v.phases))

    # === an implementer's unverified claim ends the run, and says so =====
    def impl_claims_done(goal, model, workspace, **kw):
        if "IMPLEMENTER" in goal.split("\n")[0]:
            # The exact shape hearth_workloop returns when the model says
            # GOAL COMPLETE and no gate was configured. A fixture that omitted
            # the completion dict is part of what let the real bug through.
            return fake_report(
                stop_reason=STOP_COMPLETED, said="all done",
                completion={"done": True, "verified": False,
                            "detail": "the model declared the goal complete; no "
                                      "completion check was configured to verify it"})
        return fake_report(stop_reason=W.STOP_STALLED, said="read things")

    d6 = ws("claim")
    rep6 = run_swarm("g", "m", d6, mode="auto", run_phase_fn=impl_claims_done,
                     journal=jr("claim"), execute_tool_fn=_exec)
    assert rep6.stop_reason == STOP_COMPLETED, rep6.stop_reason
    assert rep6.verified is False
    assert "NOTHING VERIFIED THIS" in rep6.render()

    # === the account explains itself =====================================
    text = rep.render()
    for needed in ("hearth swarm", "who did what", "planner", "implementer",
                   "reviewer", "as ONE budget shared by every role",
                   "what this account cannot tell you"):
        assert needed in text, needed
    # Every blind spot is rendered verbatim, the loop's as well as the swarm's.
    for headline, _m, _t in SWARM_BLIND_SPOTS:
        assert headline in text, headline
    for headline, _m, _t in W.PROGRESS_BLIND_SPOTS:
        assert headline in text, headline
    # The report round-trips as JSON, which is what the sidecar sends.
    blob = json.dumps(rep.to_dict())
    back = json.loads(blob)
    assert back["stop_reason"] == STOP_COMPLETED
    assert len(back["phases"]) == 3 and back["phases"][0]["role"] == "planner"
    assert back["spend"]["tokens"] == 600

    # === handoffs are bounded and carry the check's own output ===========
    r_h = fake_report(created=["a.py"], said="I wrote a.py")
    h = build_handoff(IMPLEMENTER, r_h, {"done": False, "detail": "exit 1",
                                         "output": "E   assert 1 == 2"})
    assert "created a.py" in h and "assert 1 == 2" in h, h
    assert "did NOT pass" in h
    big = fake_report(said="x" * (MAX_HANDOFF_CHARS * 3))
    hb = build_handoff(IMPLEMENTER, big, None)
    assert len(hb) <= MAX_HANDOFF_CHARS + 80, len(hb)
    assert "truncated" in hb

    # A composed goal names the role and warns a read-only one off writing.
    g_rev = compose_goal("do the thing", REVIEWER, "stuff happened", 2, "the plan")
    assert g_rev.startswith("ROLE: REVIEWER"), g_rev[:40]
    assert "NO tools that can change a file" in g_rev
    assert "the plan" in g_rev and "stuff happened" in g_rev
    g_impl = compose_goal("do the thing", IMPLEMENTER, "", 1, "")
    assert "NO tools that can change a file" not in g_impl

    # === a phase's words come off the REAL event stream ==================
    # The shape below is copied from hearth_workloop's own emit call for an
    # assistant reply. An earlier version read a "said" key off LoopReport.
    # turn_log, which hearth_workloop has never written; every handoff carried
    # an empty string and no injected-fake test could see it, because a fake
    # report can be given any attribute you like. This asserts against the
    # real event, and additionally pins the turn_log fact that made the old
    # approach wrong, so a future change that adds "said" does not quietly
    # leave two sources of truth.
    seen_events = []
    watcher = _PhaseWatcher(seen_events.append, REVIEWER, 7, 2)
    watcher({"type": "turn_start", "turn": 1})
    watcher({"type": "message", "role": "assistant", "content": "first thing"})
    watcher({"type": "tool_result", "tool": "read_file", "output": "..."})
    watcher({"type": "message", "role": "assistant", "content": "the last word"})
    assert watcher.last_said == "the last word", watcher.last_said
    # Every event is tagged with the ROLE, and an inner event's own `role`
    # field ("assistant") must not survive to be mistaken for it.
    assert all(e["role"] == "reviewer" for e in seen_events), seen_events
    assert all(e["phase"] == 7 and e["cycle"] == 2 for e in seen_events)
    assert len(seen_events) == 4
    # An empty reply must not overwrite a real one.
    watcher({"type": "message", "role": "assistant", "content": "   "})
    assert watcher.last_said == "the last word"

    # The fact that made the old implementation wrong, pinned: a real turn_log
    # row does not carry the model's prose.
    _probe_ws = ws("turnlog")
    with open(os.path.join(_probe_ws, "seed.txt"), "w", encoding="utf-8") as _fh:
        _fh.write("x")
    _probe = W.run_workloop(
        "say something", "m", _probe_ws, mode="auto",
        ceilings=W.Ceilings(max_turns=1),
        chat_fn=lambda messages, tools, on_token: (
            {"role": "assistant", "content": "I am thinking about it."}, 5, 7),
        execute_tool_fn=lambda n, a, w: "ok", scan_fn=W.scan_workspace,
        checkpoint_fn=None, journal=jr("turnlog"))
    assert _probe.turn_log, "the probe must have produced a turn"
    assert "said" not in _probe.turn_log[0], (
        "hearth_workloop's turn_log has grown a 'said' key. _PhaseWatcher reads "
        "the message event instead; pick ONE source and delete the other rather "
        "than letting a handoff silently prefer the wrong one.",
        sorted(_probe.turn_log[0]))

    # === the journal carries what a resume actually needs ================
    # Not just "a phase_end record exists": the CONTENTS. An earlier version
    # wrote the record before computing the handoff, so every persisted
    # handoff was the empty string and a resumed relay restarted its next role
    # with no idea what the previous one had done or what the check had said.
    # Nothing failed; the relay just quietly lost its memory across a restart.
    hj = jr("handoff-persist")
    d_h = ws("handoff")

    def phase_that_speaks(goal, model, workspace, **kw):
        return fake_report(stop_reason=W.STOP_STALLED,
                           said="I looked at parser.py and it mishandles quotes",
                           created=["parser.py"],
                           completion={"done": False, "verified": True,
                                       "detail": "exit 1",
                                       "output": "AssertionError: quotes"})

    run_swarm("g", "m", d_h, mode="auto",
              ceilings=SwarmCeilings(W.Ceilings(max_turns=6), max_cycles=1),
              done_command="pytest", run_phase_fn=phase_that_speaks,
              journal=hj, execute_tool_fn=_exec)
    ends = [r for r in hj.read() if r.get("t") == "phase_end"]
    assert ends, "the relay must have written phase_end records"
    first = ends[0]
    assert first["handoff"], (
        "the journal must persist the handoff, not an empty string: a resumed "
        "relay reads it to tell the next role what already happened")
    assert "mishandles quotes" in first["handoff"], first["handoff"]
    assert "AssertionError: quotes" in first["handoff"], (
        "the completion check's own output is the most useful thing the next "
        "role can be told, and it has to survive a restart", first["handoff"])
    assert first["plan_so_far"], "the planner's plan must survive a restart too"

    # And a resume genuinely picks it up and puts it in the next role's prompt.
    got_goal = {}

    def capture(goal, model, workspace, **kw):
        got_goal.setdefault("text", goal)
        return fake_report(stop_reason=STOP_COMPLETED,
                           completion={"done": True, "verified": True,
                                       "detail": "passed"})

    run_swarm("g", "m", d_h, mode="auto", run_id="ignored", resume=True,
              journal=hj, run_phase_fn=capture, done_command="pytest",
              execute_tool_fn=_exec)
    assert "mishandles quotes" in got_goal.get("text", ""), (
        "a resumed role must be told what the previous one did",
        got_goal.get("text", "")[:400])

    # === crash and resume ================================================
    # A phase that started and never ended is the crash signature, and it is
    # NEVER resumed: the relay restarts from the NEXT phase.
    crash_j = jr("crash")
    crash_j.append({"t": "run", "version": JOURNAL_VERSION, "run_id": "swarm-x",
                    "goal": "g", "mode": "auto"})
    crash_j.append({"t": "phase_start", "index": 1, "cycle": 1, "role": "planner"})
    crash_j.append({"t": "phase_end", "index": 1, "cycle": 1, "role": "planner",
                    "plan_so_far": "step one", "handoff": "planner spoke"})
    crash_j.append({"t": "phase_start", "index": 2, "cycle": 1, "role": "implementer"})
    prior = load_journal("swarm-x", crash_j.path)
    assert len(prior["completed_phases"]) == 1, prior["completed_phases"]
    assert prior["interrupted_phase"] == 2, prior["interrupted_phase"]

    resumed_at = []

    def note_phase(goal, model, workspace, **kw):
        resumed_at.append(kw.get("run_id"))
        return fake_report(stop_reason=STOP_COMPLETED,
                           completion={"done": True, "verified": True,
                                       "detail": "passed"})

    d7 = ws("resume")
    rep7 = run_swarm("g", "m", d7, mode="auto", run_id="swarm-x", resume=True,
                     journal=crash_j, run_phase_fn=note_phase,
                     done_command="pytest", execute_tool_fn=_exec)
    assert rep7.stop_reason == STOP_COMPLETED, rep7.stop_reason
    assert resumed_at == ["swarm-x-p3"], (
        "the interrupted phase 2 must NOT be resumed; the relay continues at 3",
        resumed_at)
    assert any("was NOT resumed" in n for n in rep7.notices), rep7.notices

    # A journal claiming bypass is refused on the way back in, exactly as
    # hearth_workloop and session_state refuse a persisted mode.
    bad_j = jr("badmode")
    bad_j.append({"t": "run", "version": JOURNAL_VERSION, "run_id": "swarm-b",
                  "goal": "g", "mode": "bypass"})
    try:
        run_swarm("g", "m", ws("badmode"), mode="auto", run_id="swarm-b", resume=True,
                  journal=bad_j, run_phase_fn=note_phase, execute_tool_fn=_exec)
        raise AssertionError("a journal claiming bypass must not be resumable")
    except ValueError as exc:
        assert "bypass" in str(exc), str(exc)

    # === model swaps are counted and paid for in wall clock ==============
    swapped = []
    ticks = {"t": 0.0}

    def fake_clock():
        ticks["t"] += 0.5
        return ticks["t"]

    d8 = ws("swap")
    rep8 = run_swarm("g", "m", d8, mode="auto",
                     ceilings=SwarmCeilings(W.Ceilings(max_turns=100, max_seconds=10 ** 6,
                                                       max_tokens=10 ** 6), max_cycles=1),
                     run_phase_fn=phases_until(99), journal=jr("swap"),
                     execute_tool_fn=_exec, clock=fake_clock,
                     model_for_role=lambda role, default: (
                         "reviewer-model" if role.name == "reviewer" else default),
                     swap_fn=lambda m: swapped.append(m))
    assert swapped == ["reviewer-model"], swapped
    assert rep8.swaps == 1, rep8.swaps
    assert rep8.swap_seconds > 0, "a swap must be charged to the wall clock"
    assert "swaps" in rep8.render()

    # A router that raises must not end a run: the role runs on the default.
    d9 = ws("badrouter")
    rep9 = run_swarm("g", "m", d9, mode="auto",
                     ceilings=SwarmCeilings(max_cycles=1),
                     run_phase_fn=phases_until(99), journal=jr("badrouter"),
                     execute_tool_fn=_exec,
                     model_for_role=lambda r, d: (_ for _ in ()).throw(RuntimeError("x")))
    assert rep9.stop_reason in STOP_REASONS, rep9.stop_reason
    assert rep9.swaps == 0

    # === a read-only role that tries to write is refused, and counted ====
    def reviewer_writes(goal, model, workspace, **kw):
        ex = kw["execute_tool_fn"]
        out = ex("write_file", {"path": "x", "content": "y"}, workspace)
        return fake_report(stop_reason=W.STOP_STALLED, said="tried: " + out[:40])

    d10 = ws("leak")
    before = list(calls)
    rep10 = run_swarm("g", "m", d10, mode="auto",
                      roles=(REVIEWER,), ceilings=SwarmCeilings(max_cycles=1),
                      run_phase_fn=reviewer_writes, journal=jr("leak"),
                      execute_tool_fn=_exec)
    assert calls == before, ("a read-only role's write must never reach the real "
                             "executor", calls[len(before):])
    assert rep10.lease_refusals >= 1, rep10.lease_refusals
    assert "write lease" in rep10.render() or rep10.lease_refusals >= 1

    shutil.rmtree(tmp, ignore_errors=True)
    print("hearth-swarmloop self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-swarmloop")
    p.add_argument("goal", nargs="?")
    p.add_argument("--model", default="qwen2.5-coder:latest")
    p.add_argument("--workspace", default=".")
    p.add_argument("--mode", default="auto", choices=list(ALLOWED_MODES))
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--max-seconds", type=int, default=7200)
    p.add_argument("--max-tokens", type=int, default=1_000_000)
    p.add_argument("--max-writes", type=int, default=200)
    p.add_argument("--max-tool-calls", type=int, default=400)
    p.add_argument("--max-cycles", type=int, default=MAX_CYCLES_DEFAULT)
    p.add_argument("--done-command")
    p.add_argument("--require", action="append", default=[])
    p.add_argument("--gate-policy", default="deny", choices=list(GATE_POLICIES))
    p.add_argument("--ollama-url", default=W.DEFAULT_OLLAMA)
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.goal:
        p.error("a goal is required unless --self-test")
    ceilings = SwarmCeilings(
        W.Ceilings(max_turns=a.max_turns, max_seconds=a.max_seconds,
                   max_tokens=a.max_tokens, max_writes=a.max_writes,
                   max_tool_calls=a.max_tool_calls),
        max_cycles=a.max_cycles)
    report = run_swarm(a.goal, a.model, a.workspace, mode=a.mode, ceilings=ceilings,
                       gate_policy=a.gate_policy, done_command=a.done_command,
                       required_artifacts=a.require or None, ollama_url=a.ollama_url,
                       emit=lambda ev: None)
    if a.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
    return 0 if report.stop_reason == STOP_COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
