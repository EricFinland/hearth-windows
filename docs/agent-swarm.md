# The agent swarm

A relay of narrow roles working one goal, on one shared budget, with one
writer.

This page is written to the standard the rest of Hearth's docs are: it says
what the feature does, what it costs, and what it does **not** buy. The
measurements are from this machine (RTX 5080, 16 GB VRAM, Windows 11) and are
reproducible with the scripts named at the bottom.

## Read this first: it did not beat a single loop

Measured against the work loop on the same model, the same ceilings, the same
deterministic gate and the same tasks, **the swarm won nothing and cost about
twice as much.**

| arm | roman | csvparse | interval | total | mean tokens | mean wall clock |
| --- | --- | --- | --- | --- | --- | --- |
| single work loop (default) | 0/3 | 0/2 | 0/3 | **0/8** | 12,319 | 252 s |
| single work loop, stall detection off | - | - | **2/3** | **2/3** | 33,527 | 438 s |
| agent swarm | 0/3 | 0/1 | 0/3 | **0/7** | 28,817 | 480 s |

Model `qwen2.5-coder:latest` (7.6B Q4_K_M), `max_turns=18`,
`max_tokens=150000`, `max_seconds=900`, hidden test suites outside the
workspace so the model cannot read them.

The decisive column is **interval**, the one task in the set that this model
can actually solve. There, a single work loop with its stall detectors turned
off passed 2 of 3. The swarm passed 0 of 3, spending 28,817 tokens per run
against the winner's 33,527 and failing where it succeeded.

### Why, mechanically

The data says the default loop's failure mode is **giving up**, not confusion:
8 of 8 default single runs stopped for `stalled`, at a mean of 5.5 turns out of
18 allowed. The swarm correctly diagnoses that, because a stall in a relay is a
handoff rather than an ending, and it does keep working: 7 of 7 swarm runs ran
to the full 18-turn ceiling.

But its remedy is worse than the obvious one. The relay spends a shared budget
on a planner and a reviewer that **cannot change a file**, so of 18 shared
turns the implementer gets roughly half. Setting `stall.window = 0` on an
ordinary work loop spends all 18 turns implementing, and that is what actually
passed the tests.

So the honest summary is: **the swarm identified the right problem and is the
wrong fix for it.** If your loop is stalling early, turn the stall detectors
down before reaching for roles.

### What would change this answer

- **A bigger turn budget.** Every swarm run hit the ceiling rather than
  stalling, so it was still working when it was stopped. The comparison bounds
  both arms identically, which is the fair test at a fixed budget; it does not
  show what a relay does with three times the budget.
- **Genuinely different models per role.** Every measurement here uses one
  model for all three roles. `model_for_role` supports per-role models and the
  swap costs about 3.3 s (below), but whether a different reviewer model helps
  is untested.
- **Tasks with real decomposition.** All three tasks are one small module.
  A relay's planner has nothing to decompose, which is close to the worst case
  for this shape.

### Limits of this evidence

Three tasks, three trials each, one model, one machine. The
stall-detection-off control arm was only run on `interval`; the run that would
have covered the other two tasks was killed partway. Two of the three tasks sit
at the floor (nothing passed them in any arm), so they bound the cost
comparison but say nothing about pass rate.

## What it is

Three roles take turns at one goal:

| role | may change files | turns per phase | job |
| --- | --- | --- | --- |
| planner | no | 4 | reads the workspace, states a concrete plan |
| implementer | **yes** | 12 | does the work |
| reviewer | no | 3 | reads the result and names the root cause of the failure |

The planner runs once. The implementer and reviewer then alternate until the
completion check passes, a shared ceiling is reached, or the relay runs out of
cycles.

It is reachable the same way the work loop is: pick "Agent swarm" in **What a
turn does**, set the bounds, and send a goal. `POST /session {"engine":
"swarm"}`, `GET /swarm`, `GET /swarm/events`. Stop is the existing
`POST /cancel`; there is no second kill switch.

## What it is not

**It is not parallelism.** Two facts on this hardware make concurrent agents
impossible, and both were measured rather than assumed:

1. `hearth_llama.Server` holds exactly one model resident. A 7B Q4_K_M is
   4.7 GB of weights plus KV cache, and this machine has 16 GB with other
   things already in it. Two resident 7B models do not fit, so two roles on
   two models cannot run at the same time.
2. Nothing in this repository makes two concurrent writers to one workspace
   safe. `hearth_tools.tool_edit_file` reads, transforms and writes with no
   lock across that span, which is a textbook lost update.
   `hearth_checkpoint`'s lockfile is held inside `checkpoint()` and released
   between turns, so it serialises commits and not turns.

So the swarm is a relay: exactly one role is active at any instant, and it is
the only thing holding write authority.

**It is not more intelligence.** Splitting one 7B into three roles does not
make it know anything it did not know as one agent.

## What it actually buys: context hygiene

A single work loop accumulates its own failures. By turn ten the model's
context is mostly its own wrong answers and the errors they produced, which is
exactly the state `hearth_workloop`'s stall detectors exist to catch.

A role handoff throws that context away without throwing away what was
learned. Each phase is a **fresh** `hearth_workloop` run: a new message list
seeded with the goal and a bounded handoff (4000 characters), and nothing
else. The reviewer that looks at a stuck implementer's work has never seen the
eight failed attempts. It sees the specification, the current state, and the
completion check's output.

The second property falls out of the first: **a stall is a handoff signal**,
not an ending. When a role stops changing anything, that is the cue to bring
in the next one.

## The costs, measured

### Model swap

`Server.swap_model` is a full process restart, because only one model is
resident. On this machine:

| | seconds |
| --- | --- |
| cold start, Qwen2.5-7B-Q4_K_M | 2.80 |
| swap 7B to 0.5B | 2.03 |
| swap 0.5B to 7B | 2.59 |
| swap 7B to 7B (warm page cache) | 2.77 |

A swap also discards the KV cache, so the next turn re-prefills from scratch.
Prefill on this GPU is fast:

| context | cold prefill | warm (cache hit) |
| --- | --- | --- |
| 2,030 tokens | 0.20 s | 0.05 s |
| 4,022 tokens | 0.30 s | 0.05 s |
| 8,030 tokens | 0.61 s | 0.05 s |
| 12,038 tokens | 0.72 s | 0.05 s |

**A swap therefore costs about 2.6 s of restart plus about 0.7 s of lost
prefill at a realistic context: roughly 3.3 s.** For comparison, generating a
500-token reply at the measured 158 tok/s takes about 3.2 s. So one swap costs
about one model reply.

That is affordable at **role-handoff** granularity and ruinous at per-turn
granularity, which is why roles are batched into phases with their own turn
budgets rather than alternating every turn. By default every role uses the
same model and there are **zero** swaps; per-role models are supported
(`model_for_role`) and the account reports how many swaps happened and what
they cost in wall clock.

### Tokens and wall clock

Measured over the runs in the table at the top: **2.34x the tokens and 1.91x
the wall clock of a default single work loop.** Two things drive that. Each
handoff re-establishes context a single conversation would have carried for
free, and the relay does not stop early: the default loop stalls out at a mean
of 5.5 turns while every relay ran the full 18.

Spending more is not by itself an argument against it. Spending more and
passing fewer tests is, and that is what happened.

## One budget, not one per role

The failure this is designed against: a swarm that multiplies token spend by
the number of roles while keeping a single-agent ceiling.

Ceilings here are **global and shared**. Every phase is handed a residual
budget computed by subtracting everything every previous phase already spent.
Three roles do not get three times the tokens; they get one budget between
them, and the third role can find it empty and be told so.

Per-role turn caps exist as a **second** bound that can only narrow: a phase's
real ceiling is the elementwise minimum of the role's cap and what is left
globally. Which of the two actually bit is recorded, because "the reviewer
used up its three turns" (a normal handoff) and "the relay is out of tokens"
(the run ends) are different facts.

An exhausted budget clamps to zero and never goes negative, because
`hearth_workloop` reads a negative limit as *unlimited* and an overspent relay
would otherwise hand its next role an unbounded phase.

## One writer, proved two ways

Only the implementer may change a file. This is enforced twice, by two
mechanisms that share no code:

1. **The capability manifest.** A read-only role's manifest contains no write
   tool, and `permissions.decide` treats `allowed_tools` as a hard cap in
   every mode. A `RoleSpec` that is read-only and holds a write tool cannot be
   constructed at all.
2. **The write lease.** An exclusive token held by at most one role at a time.
   The tool executor refuses any `edit`-risk tool when the active role does
   not hold it, *after* the permission layer has already decided. A manifest
   built wrong is still stopped here.

The lease raises rather than blocks on a double acquire: in a relay nothing
legitimately waits for it, so a lease that waited would turn a scheduling bug
into a hang instead of an error.

`parse_swarm_config` refuses a `roles` field outright. A caller that could
describe roles could describe two writers, and the single-writer property
would stop being structural. A request may only *narrow* the tool manifest,
which is applied to every role by intersection and can therefore never give a
read-only role a writer.

## Honesty: what a reviewer's approval is worth

**A reviewer is a model.** Its approval is one 7B's opinion of another 7B's
work, produced by the same weights that wrote the code, reading code it cannot
run. Two models agreeing is not independent evidence: they share their failure
modes.

So **a reviewer's approval never completes a run.** Only the deterministic
gate (`done_command` or required artifacts) can do that, exactly as in the
single loop. When a relay ends with a reviewer's blessing and no deterministic
check, the account says:

> REVIEWED BUT NOT VERIFIED. A reviewer role read this work and did not
> object. Nothing ran it.

and when no check was configured at all it still says `NOTHING VERIFIED THIS`,
the same words the work loop uses.

`reviewer_approved` and `verified` are separate fields all the way to the
screen and are never merged.

The four things the relay structurally cannot tell you ship as structured data
(`SWARM_BLIND_SPOTS`), are rendered verbatim by the account and by the UI, and
are never paraphrased into reassurance. The work loop's own five blind spots
apply to every phase and are rendered too, because every phase *is* one of
those loops.

## Crash recovery

The journal is append-only JSONL, fsynced at every phase boundary. A
`phase_start` with no matching `phase_end` is the signature of a process that
died mid-phase.

Such a phase is **never resumed**. There is no way to know whether its last
tool call reached the workspace, so the relay records that it was interrupted
and continues from the next phase. The persisted handoff and plan are carried
across, so the resumed role knows what the previous one did.

A journal is a file in a directory the agent's own `write_file` can reach, so
`inspect_journal` believes nothing in it about authority: a journal claiming
`bypass`, two writing roles, or tools outside the default manifest makes the
run non-resumable **with a stated reason** rather than being quietly
corrected.

## Modes

`auto` or `plan`. `bypass` is unreachable by construction in
`hearth_swarmloop.ALLOWED_MODES`, in `SWARM_MODES`, in
`session_state.RESTORABLE_MODES`, in `POST /session`, and again in
`inspect_journal` on the way back in. `edit` is refused because it gates every
write and nobody is awake to approve them. In `plan` every role is read-only,
which makes the relay an investigation rather than a change; the UI says so.

## Reproducing the measurements

- swap and prefill cost: `agent/hearth_llama.py` (`Server.swap_model`)
- self-test: `python agent/hearth_swarmloop.py --self-test`
- engine self-test: `python desktop/server/swarm_engine.py --self-test`
- untrusted-content check: `desktop/ui/xss-check.html`
