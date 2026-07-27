# The Model Shop

The shop is the feature meant to make local models feel free rather than
fiddly: instead of a catalog listing a parameter count and a download size
and leaving you to guess whether it'll actually run, it looks at your
actual hardware and gives you a verdict you can act on. This page documents
the honesty properties that verdict is built on. The logic described here
(`agent/hearth_hw.py`, `agent/hearth_shop.py`) is built, tested, and correct
today; the shop's actual on-screen interface is not built yet. See
[docs/windows.md](windows.md) for where the project stands overall.

## Fit verdicts, not vibes

Every model in the catalog gets a verdict, computed from your real detected
hardware against that model's real memory requirements, not a
one-size-fits-all label. Best to worst:

| Verdict | What it means |
| --- | --- |
| `great` | Fits fully in VRAM at the requested context, with roomy headroom (at least 20% of total VRAM free). Runs fast, nothing to worry about. |
| `good` | Fits fully in VRAM, but headroom is tight. Runs fine today; a bigger prompt or a second loaded model could push it over. |
| `reduced_context` | Doesn't fit at the requested context, but does at a shorter one. The verdict says exactly how short. |
| `cpu_spillover` | Doesn't fit VRAM at any useful context, but the weights and KV cache fit in system RAM. It will run, just slowly. No speed number is predicted - see below. |
| `wont_fit` | Doesn't fit VRAM or RAM. The shop's own rule: never recommend this model on this machine. |

When nothing in the catalog earns better than `wont_fit` on a given
machine, the shop's recommendation function returns nothing rather than
headline a model that can't actually run. An honest "nothing fits" beats a
confident wrong answer.

## Why KV-cache math instead of parameter count

This is the part most people, and most model catalogs, get wrong. Model
weights are not the whole memory story: context length is usually what
actually breaks the fit. Every token of context an attention model holds
onto costs a fixed number of bytes per token, spread across every layer and
every KV head, and that cost scales linearly with context length regardless
of how big the weights are. A 14B model at a short context might fit
comfortably in 16GB; the exact same model at a long context can need
roughly twice the memory of its weights alone once the KV cache is added
in.

So every catalog entry carries `kv_bytes_per_token`, computed from that
model's attention architecture (layer count, KV-head count, head dimension)
rather than from parameter count alone - the arithmetic itself is never a
guess. What isn't always known with the same confidence is the architecture
figures that arithmetic runs on, and each entry says so through its
`kv_confidence` label: `published_config` when the layer and head counts
come straight from the model family's published architecture, high
confidence; `recalled_estimate` when they're recalled from the model's
technical report rather than reverified against a live config file,
good-faith but not gospel; and `conservative_overestimate` when the exact
attention configuration isn't confidently known at all, in which case the
entry deliberately assumes the least favorable case (no grouped-query
attention) rather than inventing a number - safe to over-count KV cost,
never safe to under-count it. The confidence label is the honest part of
this system: it exists precisely because not every number behind the math
is equally certain, and it says so wherever that's true instead of
presenting every verdict with the same confidence.

The concrete payoff: on a 6GB RTX 2060, Ollama's own default context for
that card is 4096 tokens. Hearth's calculator instead selects 16384,
verified with the model actually loading at that context length, because
the KV-cache arithmetic says a 7B Q4 coding model still leaves about 0.80GB
of headroom there. The same model at 32768 tokens doesn't fit at all on
that card - the calculation is linear, so the ladder just stops climbing
once a rung fails to clear a real safety margin.

## Why there's no predicted tokens per second

Deliberately out of scope: predicting throughput from memory bandwidth and
parameter count. That kind of prediction breaks badly on mixture-of-experts
models, where the active parameters used per token are a small fraction of
the model's total weights - a naive formula built on total parameter count
would call an MoE model slow when it's actually fast, and would steer users
away from exactly the local coding models most capable of helping them.
Predicting a number you know breaks on an entire model family is worse than
not predicting one at all, so the shop doesn't. The plan instead is to
measure real throughput on the user's own machine and show that, once the
shop has an interface to show it in.

## The VRAM number behind a verdict might be a guess

Every verdict is graded against a VRAM reading, and that reading is not
always precise. On Windows, `nvidia-smi` gives an exact figure when it's
available. When it isn't, detection falls back to PowerShell or the
deprecated `wmic`, both of which read
`Win32_VideoController.AdapterRAM` - a signed 32-bit field that misreports
any card above roughly 4GB, sometimes reading low, sometimes reading a
wrapped or even negative value. Every reading that comes from that fallback
path is marked `approximate`, and the verdict logic checks that flag
explicitly: a result that would otherwise be `great` on an approximate
reading is downgraded to `good`, because a confident "great" built on a
guessed VRAM number isn't actually great. Anywhere the shop shows you a
verdict, it's obligated to also say when the number behind it is a guess.

## What's built, what's still just data

`hearth_hw.probe()` and `hearth_shop.catalog_with_verdicts()` are real,
tested, callable functions today: pure detection and pure arithmetic, no
network calls, no writes. What doesn't exist yet is anything to click:
there is no shop screen, no download button, no progress bar. See
[docs/windows.md](windows.md) for the full state of the project.
