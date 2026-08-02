# The agent swarm

A relay of narrow roles working one goal, on one shared budget, with one
writer.

This page is written to the standard the rest of Hearth's docs are: it says
what the feature does, what it costs, and what it does **not** buy. The
measurements are from this machine (RTX 5080, 16 GB VRAM, Windows 11) and are
reproducible with the scripts named at the bottom.

## Read this first: measured twice, and the two measurements disagree

The first comparison said the relay lost outright. Retuning the loop it lost to
then changed the relay as well, because every phase of the relay **is** one of
those loops, and a re-measurement on the one task this model can solve passed
2 of 2 where a single loop passed 0 of 2. Both samples are tiny. Read both, and
do not take either as settled:

- [the original comparison](#the-original-comparison-old-loop-defaults), swarm
  0/7, which is what shipped with the feature;
- [the loop retune that came out of it](#the-finding-that-outlived-the-swarm-the-loop-was-giving-up),
  which is the more useful half of this page;
- [the re-measurement](#does-the-retuned-loop-change-the-swarm-verdict), swarm 2/2 on
  `interval`, at two trials.

### The original comparison (old loop defaults)

Measured against the work loop on the same model, the same ceilings, the same
deterministic gate and the same tasks, **the swarm won nothing and cost about
twice as much.**

| arm | roman | csvparse | interval | total | mean tokens | mean wall clock |
| --- | --- | --- | --- | --- | --- | --- |
| single work loop (the default **at the time**) | 0/3 | 0/2 | 0/3 | **0/8** | 12,319 | 252 s |
| single work loop, stall detection off | - | - | **2/3** | **2/3** | 33,527 | 438 s |
| agent swarm | 0/3 | 0/1 | 0/3 | **0/7** | 28,817 | 480 s |

Model `qwen2.5-coder:latest` (7.6B Q4_K_M), `max_turns=18`,
`max_tokens=150000`, `max_seconds=900`, hidden test suites outside the
workspace so the model cannot read them.

> **The first row no longer describes the shipped loop.** Those stall settings
> were the finding, not the baseline: they have since been re-measured and
> replaced, and the loop now ships something much closer to the second row.
> See [the loop was giving up](#the-finding-that-outlived-the-swarm-the-loop-was-giving-up)
> below, which is the more useful half of this page.

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
turns the implementer gets roughly half. Letting an ordinary work loop simply
keep working spends all 18 turns implementing, and that is what actually passed
the tests.

So the summary at the time was: **the swarm identified the right problem and is
the wrong fix for it.** That held only as long as the loop it was measured
against gave up at turn 5. It does not any more, and the re-measurement below
does not agree with this paragraph.

### What would change this answer

The first item on this list is no longer hypothetical. It was written before
the loop was retuned, and retuning it is what
[the re-measurement](#does-the-retuned-loop-change-the-swarm-verdict) below acted on.

- **A more patient single loop, which changes the relay too.** Every phase of a
  relay is a work loop, so the same retune gives the implementer more turns
  before its stall becomes a handoff. This one happened, and it flipped the
  result on `interval`.
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

## The finding that outlived the swarm: the loop was giving up

One line in the comparison above is worth more than the verdict it was built to
reach: **8 of 8 default single runs stopped `stalled` at a mean of 5.5 turns out
of 18 allowed.** A loop that abandons a budget the user already granted, less
than a third of the way in, on tasks it can solve when allowed to continue, is
not being careful. It is wrong. The swarm was a way of working around that; the
direct fix is to stop doing it.

### The mechanism, which the thresholds alone do not show

The old defaults were `window=6, repeat_actions=4, repeat_errors=5,
oscillations=3, min_turns=3`, reasoned about rather than measured. The one that
did the damage was `repeat_errors`, and not for the reason its name suggests.

`run_workloop` feeds the completion check's own output into the turn's errors,
deliberately, so that the account can say "the same 3 tests failed each time".
But a run that has not passed its gate yet fails that gate **the same way every
turn** -- that is what "still working on it" looks like. So `repeat_errors=5`
was never "the model is stuck repeating itself". It was a hard cap of five
turns on any run whose tests do not go green almost immediately, and steady,
genuine, file-changing progress tripped it.

Replaying the recorded runs one detector at a time makes this concrete. Across
the recorded runs, with each detector enabled alone:

- `no_new_state` and `oscillation` **never fired**, at any threshold. Part of
  that is real (the model changed a file most turns) and part of it was a
  defect in the first harness, which wrote its journal inside the workspace
  being fingerprinted, so every turn looked like a change. The journal moved
  out and the runs recorded afterwards are clean; see the caveat under the
  table.
- `repeat_action` fired on 2 runs, at turns 10 and 13, and changed the overall
  outcome only at its most eager setting.
- `repeat_error` fired on **every** unfinished run, usually on exactly the turn
  its threshold names, because the gate's failure is present from turn one.
  Where it fired later, a turn's error text had briefly changed and broken the
  streak.

On a run with a completion check, one knob decides everything.

### The method: replay, which is exact rather than approximate

Tuning five thresholds by re-running a 7B once per candidate is slow and
statistically useless -- the model varies more between two identical runs than
between two neighbouring policies. It is also unnecessary, because of a
property of `hearth_workloop` worth stating plainly:

> A `StallPolicy` has exactly one effect on a run: it decides the turn on which
> the run stops. Nothing about it reaches the model. `ledger.verdict()` is
> consulted once per turn, after the turn is complete, and its only consequence
> is `_finish(STOP_STALLED, ...)`.

So the turn sequence a run produces is identical under every policy, up to the
turn the policy ends it. Record each run once with **every detector off**, keep
each turn's workspace digest, action fingerprint and error signatures, and
replay that recording through the real `ProgressLedger` for any candidate. The
turn a policy would have stopped on is not an estimate of the counterfactual,
it **is** the counterfactual, computed by the shipped detector code, with every
candidate scored on identical traces.

`scripts/loop_bench.py` is that harness: `trace` records, `replay` scores,
`import` reads traces back out of journals written by earlier runs, and `live`
runs a named setting for real. Only runs recorded with detection off can be
imported, and the importer refuses the others by name: a run stopped at turn 5
by its own policy cannot answer what turn 6 would have looked like, and
replaying a more patient policy against it would flatter the eager settings by
construction.

### The trade, measured in both directions

Nine recorded runs, replayed through every candidate. `interval` is the task
this model can actually solve, so it measures **giving up too early**. `rle`
was written as a second solvable task and the model never once solved it, so
it measures **grinding on a run that will never pass**. Wins are the
deterministic gate passing, not the model's opinion.

| policy | interval: wins / trials | interval: mean turns | rle: mean turns to stop | rle: mean tokens |
| --- | --- | --- | --- | --- |
| old default (`repeat_errors=5`) | 2/6 | 4.7 | 7.3 | 14,424 |
| **balanced** (new default) | **3/6** | 6.5 | 10.3 | 22,831 |
| give up early | 2/6 | 4.0 | 6.3 | 12,092 |
| keep trying | 3/6 | 10.8 | 16.3 | 41,090 |
| every detector off | 3/6 | 11.2 | 18.0 | 49,496 |

The retuned default wins everything the fully patient arm wins, and pays 46%
of what that arm spends on the runs that were never going to pass. The old
default bought a further 37% off that hopeless-run cost, and paid for it with
one of the three wins.

Sweeping the one knob that matters gives the actual curve, everything else
held at the default:

| `repeat_errors` | interval wins | rle turns | rle tokens |
| --- | --- | --- | --- |
| 3 | 2/6 | 4.3 | 7,018 |
| 4 | 2/6 | 6.3 | 12,092 |
| 5 *(old default)* | 2/6 | 7.3 | 14,424 |
| 6 | **3/6** | 8.3 | 17,010 |
| 7 | 3/6 | 9.3 | 19,833 |
| **8** *(new default)* | **3/6** | 10.3 | 22,831 |
| 9 | 3/6 | 11.3 | 26,097 |
| 12 | 3/6 | 13.0 | 30,440 |
| 16 | 3/6 | 14.3 | 33,139 |
| off | 3/6 | 15.0 | 34,508 |

The knee is at 6, and nothing above it buys another win on this data. Each
extra turn of patience costs about 2,500 tokens on a run that cannot pass. The
default is set at **8** rather than at the knee because the latest win in the
data arrived on turn 7: 6 would have kept it by luck, with no margin at all,
and two turns of headroom cost about 5,800 tokens on a hopeless run. That is
the whole trade, and it is why the number is 8 and not 5, 6 or off.

> **Caveat on these nine traces.** They were recorded with the harness writing
> its journal inside the workspace it was fingerprinting, so every turn read as
> a change and the `no_new_state` and `oscillation` detectors could not fire on
> them at any threshold. Everything above is driven by `repeat_errors`, which
> reads tool and gate output and is unaffected. The harness now keeps the
> journal outside the workspace and prints this warning itself when a trace
> carries the flag.

### The other thing the benchmark found

`oracle` was written as the worst case for a stall detector: a gate holding
values nothing in the workspace implies, reporting only how many are wrong.
It runs to its **full turn ceiling under every patience setting, including the
most eager**, and the reason has nothing to do with patience. Its failure
message is "predict is wrong at 3 of 6 indices", which contains no word in
`_ERROR_MARKERS`, so `extract_errors` returns nothing, no error signature is
ever recorded, and `repeat_error` has nothing to repeat.

Measured on a clean 18-turn recording: **zero** error signatures on all 18
turns, and `give_up_early`, `balanced`, `keep_trying` and the old default all
end the same way, at the turn ceiling, having spent 66,091 tokens.

**A completion check that fails quietly is invisible to stall detection**, and
in that case the ceilings are the only thing that will stop the run. That now
ships as one of `PROGRESS_BLIND_SPOTS`, rendered in every account and beside
the form.

### Confirmed live, not only in replay

Replay cannot catch a bug in the wiring, because it never uses the wiring. So
both settings were also run for real on `interval`, same model, same ceilings,
same hidden tests, journals outside the workspace this time. **Two trials per
arm**: enough to check that the setting reaches the run and moves the stopping
point where replay said it would, nowhere near enough to be a pass-rate claim.

| arm | trial | passed | stopped for | turn | tokens |
| --- | --- | --- | --- | --- | --- |
| old default | 1 | no | stalled: `repeat_error` | 5 | 20,014 |
| old default | 2 | no | stalled: `repeat_error` | 5 | 11,493 |
| balanced | 1 | no | stalled: `repeat_action`, `repeat_error` | 8 | 21,306 |
| balanced | 2 | no | stalled: `repeat_action` | 8 | 13,503 |

The old default stopped on turn 5 in both trials, on the completion check's own
repeated failure; the retuned default carried both runs to turn 8. Neither
trial reached the gate, so this table says nothing about pass rate and is not
offered as saying anything. What it does show is that the setting a caller
chooses is the setting the run is actually bounded by, in a live run, end to
end, and that the number it moved is the one replay said it would move. Each
run's journal header records the patience it ran under, so it is checkable
afterwards rather than taken on trust.

Wall clock is deliberately left out of this table: these runs shared the
machine with other work and their seconds are not comparable to the recorded
traces above.

### What a user sees

Five thresholds are meaningless without this page, so the app does not ask for
five numbers. It asks how long the run should keep trying, next to the
ceilings, before anything starts:

| setting | thresholds | it stops when |
| --- | --- | --- |
| Give up early | `window=4, repeat_actions=3, repeat_errors=4, oscillations=2, min_turns=2` | the first sign of repetition |
| **Balanced** (default) | `window=8, repeat_actions=4, repeat_errors=8, oscillations=3, min_turns=3` | the workspace is going in circles |
| Keep trying | `window=16, repeat_actions=8, repeat_errors=0, oscillations=0, min_turns=6` | the workspace is provably frozen |

The price of each is printed underneath it in the panel, not left in this file,
because the person who most needs it is the one about to leave a run going
unattended. `Keep trying` says outright that a run which cannot succeed will
spend nearly all of its turns, tokens and wall clock before stopping.

The five thresholds are still editable underneath. Changing one relabels the
run **custom** rather than leaving a preset name on screen that no longer
describes it, and that label is derived from the numbers on both sides:
`hearth_workloop.patience_of` on the server, the same comparison in the page.
The name reaches the run through `POST /session {"loop": {"patience": ...}}`
and the CLI's `--patience`; an unknown name is refused rather than quietly
defaulted, because a run that ignores the setting a user picked would show that
setting on screen and behave as something else.

The account says which setting judged the run, and when a run stops stalled
below the most patient setting it says how much budget was left:

```
patience   balanced  (min_turns=3, oscillations=3, repeat_actions=4,
                      repeat_errors=8, window=8)
...
why it stopped
  stopped making progress: the same error recurred in the last 8 turns
  [repeat_error] ...
  this judgement was made at the 'balanced' patience setting, with 10 of 18
  turns unspent. A more patient setting would have kept going.
```

### Patience cannot buy a single turn

Ceilings are the real bound and a stall policy has no vote on them. A preset
carries stall thresholds and nothing a `Ceilings` object reads, which is
asserted structurally; the most patient preset is run into the turn, wall
clock, token and unattended-write ceilings in `hearth_workloop._self_test`
case 13 and stops at exactly the value each ceiling names; and
`parse_loop_config` is checked to leave every ceiling untouched for every
preset. There is still no way to spell "unlimited" over HTTP.

Both properties were mutation-tested. Letting a preset carry `max_turns`,
letting `parse_loop_config` accept a patience name and then not use it, letting
it double `max_turns` for the most patient preset, and letting `run_workloop`
widen a ceiling for that preset each make a named test fail, at the layer that
should catch it.

### What this does not tell you

- **Six recorded runs on one solvable task.** The retuned default rescued one
  win out of six that the old default would have thrown away, on `interval`.
  That is one run, and the direction agrees with the earlier arm comparison
  (0/3 default versus 2/3 with detection off), but it is not a large sample.
- **One knob carried the tuning.** With a completion check configured,
  `repeat_errors` decided every outcome; `repeat_action` moved a result only at
  its most eager setting and the other two moved nothing. A run with **no**
  completion check has no gate output feeding its error stream, so its balance
  between the four detectors is different and nothing here measures it.
- **The thresholds are absolute turn counts, not fractions of the budget.**
  `repeat_errors=8` means eight turns whether the ceiling is 18 turns or 400.
  The numbers were chosen at an 18-turn ceiling.
- **One model, one machine, one shape of task.** All four tasks are a single
  small module with a hidden test suite.

## Does the retuned loop change the swarm verdict?

It has to be asked, because **every phase of the relay is a `hearth_workloop`
run** and inherits the new default. An implementer now works for 8 turns before
its stall, not 5.

Re-measured on `interval`, same ceilings, same hidden test, two trials:

| arm | trials | passed | turns | tokens | how it ended |
| --- | --- | --- | --- | --- | --- |
| agent swarm | 2 | **2/2** | 16, 17 | 28,797 / 48,270 | completion check passed |
| single loop, balanced | 2 | 0/2 | 8, 8 | 21,306 / 13,503 | stalled |
| single loop, old default | 2 | 0/2 | 5, 5 | 20,014 / 11,493 | stalled |

Both relay runs had the identical shape, and it is the shape this page claims
for the design:

```
planner      4 turns  (role cap)
implementer  8 turns  (stalled -- which in a relay is a handoff, not an ending)
reviewer     3 turns  (role cap)
implementer  1-2 turns  -> completion check passed
```

The implementer stalls at 8 with something half-built, the reviewer reads that
failure with a **clean context**, and the next implementer phase finishes it in
one or two turns. Under the old defaults the first implementer phase ended at
turn 5 with less to review, and the relay never got there.

**So the negative verdict above is no longer supported for this task**, and the
mechanism that changed it is legible rather than mysterious. What this does not
establish:

- **Two trials.** `interval` is roughly a coin flip for this model: across six
  recorded runs a single loop at the retuned default reaches the gate 3 times.
  2/2 against 0/2 is entirely consistent with two coins landing differently.
- **One task.** The other three tasks in the set were not re-run. The relay's
  cost is unchanged and still roughly double a single loop's on the runs that
  do not pass.
- **The full comparison was not re-run.** Settling this needs every task, both
  arms, and enough trials to separate 50% from 40%, which is many more than
  two. `python scripts/loop_bench.py swarm` and `... live` are how.

The honest position today: **start with a single work loop.** It is simpler and
cheaper per turn, and the relay's advantage over it is now plausible rather
than demonstrated, where before it was neither.

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

Measured over the runs in the original table: **2.34x the tokens and 1.91x the
wall clock of a single work loop at the stall settings of the day.** Two things
drive that. Each handoff re-establishes context a single conversation would
have carried for free, and the relay does not stop early: that loop stalled out
at a mean of 5.5 turns while every relay ran the full 18.

Spending more is not by itself an argument against it. Spending more and
passing fewer tests is, and that is what the first comparison found. The
re-measurement did not repeat it: there the relay spent 28,797 and 48,270
tokens and passed both times, against 21,306 and 13,503 for a single loop that
passed neither. The cost multiple is real in both; what it buys is what two
trials cannot tell you.

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

The stall-tuning harness is `scripts/loop_bench.py`, and it is kept rather than
thrown away, because the numbers above are only worth what re-running them is:

```
python scripts/loop_bench.py trace  --tasks interval,rle,roman,oracle --trials 3
python scripts/loop_bench.py replay traces.json            # the preset table
python scripts/loop_bench.py replay traces.json --sweep    # the trade curve
python scripts/loop_bench.py live   --tasks interval,rle --arms current,balanced
python scripts/loop_bench.py swarm  --tasks interval --trials 2
python scripts/loop_bench.py --self-test
```

- swap and prefill cost: `agent/hearth_llama.py` (`Server.swap_model`)
- self-test: `python agent/hearth_swarmloop.py --self-test`
- engine self-test: `python desktop/server/swarm_engine.py --self-test`
- untrusted-content check: `desktop/ui/xss-check.html`
