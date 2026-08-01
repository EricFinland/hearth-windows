#!/usr/bin/env python3
r"""hearth inference backend: one interface, two engines, an explicit choice.

Hearth's promise is that the user installs exactly one thing. Until now that
was false: every path that talked to a model assumed Ollama was installed,
running, and had a model pulled. Hearth now bundles llama.cpp's llama-server
and drives it itself (see hearth_llama), so the bundled engine is the
default and Ollama becomes an optional backend for people who already run
one.

Keeping both is not politeness. Two implementations are what keep this
abstraction honest: an interface written against a single engine is just
that engine's API with extra indirection, and the places where the two
genuinely disagree are exactly the places a single-engine interface would
have quietly hard-coded.

## Where the two genuinely disagree

  Lifecycle. llama-server is a process Hearth owns: Hearth chooses the
  model, spawns the binary, waits out the load, and kills it. Ollama is a
  daemon somebody else started, that Hearth can only send requests to. So
  "the engine is not usable" means a missing bundled binary in one case and
  an unreachable port in the other, and those need different diagnoses and
  different remedies. See LlamaBackend.diagnose versus OllamaBackend.diagnose.

  Residency. llama-server holds exactly one model, chosen at spawn time;
  changing model is a stop-and-start (hearth_llama.Server.swap_model).
  Ollama holds zero or more models, swaps them on demand, and unloads them
  on its own schedule. So "which model is loaded" is a fact Hearth already
  knows in one case and a question that has to be asked over HTTP in the
  other.

  Model identity. Ollama names a model with a registry tag,
  "qwen2.5-coder:latest". llama.cpp names one with a path to a GGUF file on
  disk. These are not the same kind of thing and one cannot be derived from
  the other, so this module does NOT flatten them into a single string.
  ModelRef carries the kind alongside the value, every backend declares
  which kind it accepts, and handing a backend the wrong kind raises
  ModelKindError with a message naming both kinds rather than being coerced
  into something that would fail later and further away.

## What the interface is, and where it came from

The method set below was derived from the call sites, not from the union of
what the two engines expose:

  chat()            hearth_loop.chat, and through it every agent loop.
  own_vram_bytes()  hearth_idle, which must subtract Hearth's OWN resident
                    model before calling the rest of the card contention.
  measure()         hearth_bench, which times a deterministic generation.
  diagnose()        hearth_setup, which tells a user what to fix.
  available_models() the model pickers in the desktop server.

Nothing here exposes "swap the model" or "start the server" as interface
surface, because no caller asks for those: LlamaBackend does its own
lifecycle management behind chat(), and OllamaBackend has no lifecycle to
manage.

## Selection

THE MODEL DECIDES THE BACKEND. Availability is the tiebreaker, not the
primary key. That ordering was inverted once and it cost a live regression:
selection preferred the bundled binary the moment find_server() succeeded,
so a machine that had both a vendored llama-server and a user who picked
"qwen2.5-coder:latest" got LlamaBackend handed an Ollama registry tag, which
it cannot resolve. Every turn died with a generic error before any tool call
happened. Both modules were individually correct and individually tested; the
defect lived entirely in the seam, where the model's kind was known and
simply never consulted.

get_backend() resolves in this order, and active() reports which rung fired
and why so a setup screen can explain itself:

  1. %HEARTH_BACKEND% set to "llama" or "ollama". An explicit override is
     obeyed even when that backend is not currently usable, because a user
     who typed it needs to be told THAT backend is broken, not silently
     handed the other one. When the model named alongside it belongs to the
     other namespace, the override still wins and the decision carries a
     model_error naming both the override and the model's kind -- an
     override that quietly did something else would be worse than an error.
  2. The model's own kind, when its shape says which namespace it is in
     (see model_kind_hint). A GGUF path implies llama.cpp; a registry tag
     implies Ollama. If that backend is not usable, the decision still names
     it and carries a model_error saying so, because "this model needs an
     engine you do not have" is the honest answer and the other engine
     genuinely cannot run it.
  3. The bundled llama-server, if hearth_llama.find_server() finds it.
  4. Ollama, if it answers on its base URL.
  5. llama, as the reported default when neither is usable, so the
     resulting diagnosis is about the engine Hearth actually ships rather
     than about a dependency Hearth no longer requires.

Rungs 3 to 5 only run for an AMBIGUOUS model -- a bare name with no path
separator, no .gguf suffix, and no tag colon, or no model at all. Such a
name resolves in the active backend's own namespace, exactly as it did
before this existed.

## Constraints

Standard library only, Python 3.12+. Never prints (the CLI at the bottom is
the exception, since printing is its job). Every network and subprocess call
is bounded by a timeout. Nothing here writes to the user's data directory.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hearth_llama  # noqa: E402
import hearth_paths  # noqa: E402

try:  # hearth_hf is where downloaded GGUFs live; absent in a trimmed checkout.
    import hearth_hf  # noqa: E402
except Exception:  # noqa: BLE001 - a missing model source must not break chat
    hearth_hf = None


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Environment override naming the backend to use. "llama", "ollama", or
#: "auto" (the same as unset). Anything else is reported as an invalid
#: override rather than silently ignored.
ENV_BACKEND = "HEARTH_BACKEND"

#: Environment override for the Ollama base URL, matching the name
#: hearth_idle, hearth_bench and hearth_doctor already read.
ENV_OLLAMA = "HEARTH_OLLAMA"

BACKEND_LLAMA = "llama"
BACKEND_OLLAMA = "ollama"
BACKENDS = (BACKEND_LLAMA, BACKEND_OLLAMA)

#: Model identity kinds. See the module docstring: these are deliberately
#: not interchangeable, and ModelRef keeps them apart.
KIND_GGUF = "gguf"
KIND_OLLAMA = "ollama"
KINDS = (KIND_GGUF, KIND_OLLAMA)

#: Which backend serves each model kind. The two are one-to-one by
#: construction (a backend declares exactly one model_kind), and this is the
#: mapping selection reads in the direction the backends themselves cannot:
#: from a model to the engine that can run it.
BACKEND_FOR_KIND = {KIND_GGUF: BACKEND_LLAMA, KIND_OLLAMA: BACKEND_OLLAMA}
KIND_FOR_BACKEND = {BACKEND_LLAMA: KIND_GGUF, BACKEND_OLLAMA: KIND_OLLAMA}

#: Why a particular backend was selected. Reported by active().
WHY_OVERRIDE = "override"
WHY_MODEL_KIND = "model_kind"
WHY_BUNDLED = "bundled"
WHY_OLLAMA_REACHABLE = "ollama_reachable"
WHY_DEFAULT = "default"

DEFAULT_OLLAMA_URL = os.environ.get(ENV_OLLAMA, "http://127.0.0.1:11434")

#: Bounded waits. Everything polled or user-facing is short; only model
#: loading and generation get a generous ceiling, because both are genuinely
#: slow rather than broken when they take a while.
REACHABLE_TIMEOUT = 2
TAGS_TIMEOUT = 5
PS_TIMEOUT = 2
DEFAULT_CHAT_TIMEOUT = 600
NVIDIA_SMI_TIMEOUT = 3

#: The prompt hearth_bench uses. Kept here so both backends measure the same
#: work, and re-exported rather than duplicated in hearth_bench.
DEFAULT_BENCH_PROMPT = "Write a three-line haiku about a slow compiler."
DEFAULT_BENCH_TOKENS = 64

#: size_vram / size at or above this counts as "fully resident". The same
#: figure and the same reasoning as hearth_bench.RESIDENCY_FULL_THRESHOLD:
#: not 1.0, because byte accounting has rounding slack and calling a model
#: "partially offloaded" over a rounding error would cry wolf.
RESIDENCY_FULL_THRESHOLD = 0.98

#: A leading "gguf:" or "ollama:" on a model string is a kind prefix. The
#: match is anchored and the alternatives are spelled out, so a Windows
#: drive letter ("C:\models\x.gguf") and an Ollama tag with a colon in it
#: ("qwen2.5-coder:latest") are both left alone.
_KIND_PREFIX_RE = re.compile(r"^(?P<kind>gguf|ollama):(?P<value>.*)$", re.DOTALL)


class BackendError(RuntimeError):
    """Anything this module refuses to paper over."""


class StopStream(Exception):
    """Raised BY an on_token callback to abandon a generation immediately.

    Deliberately not a BackendError and deliberately not caught by any
    `except BackendError` a caller may already have: this is control flow, a
    consumer saying "I am not listening any more", not an engine failure.

    It is the only mechanism either engine has for stopping a generation
    early. Neither llama-server nor Ollama exposes a cancel endpoint; what
    stops them is the client hanging up. Raising out of the token callback
    unwinds the response-body iterator, which closes the socket inside the
    `with resp:` block, which is what the server sees as a disconnect and
    what makes it stop producing tokens. That matters more with streaming
    than it did without it: before, a cancelled turn left a whole generation
    running to completion in the background, holding the GPU.

    Both backends catch it, stop reading, and return the tokens that had
    already arrived, with "stopped": True on the result. A caller therefore
    never has to choose between prompt cancellation and knowing what was
    generated before it.
    """


class ModelKindError(BackendError):
    """A ModelRef of one kind reached a backend that speaks the other.

    Its own class, not a bare BackendError, because it is the one failure
    that means "the caller mixed up two model namespaces" rather than
    "the engine is unhappy", and a caller may well want to catch exactly
    that and re-resolve the model.
    """


# --------------------------------------------------------------------------
# Model identity
# --------------------------------------------------------------------------

class ModelRef:
    """Which model, and in whose namespace.

    Ollama identifies a model by a registry tag it resolves itself
    ("qwen2.5-coder:latest"); llama.cpp identifies one by a path to a GGUF
    file. Neither can be computed from the other: a tag says nothing about
    where a file is, and a file path is not a name any registry knows. So
    this type keeps the kind and the value together and refuses to guess
    between them once constructed.

    Instances are immutable, hashable, and compare by (kind, value), so a
    ref is usable as a dict key in a residency or measurement cache.
    """

    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        if kind not in KINDS:
            raise ValueError("unknown model kind {!r}; expected one of {}".format(
                kind, ", ".join(KINDS)))
        if not value or not str(value).strip():
            raise ValueError("a {} model reference needs a non-empty value".format(kind))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", str(value).strip())

    def __setattr__(self, name, value):
        raise AttributeError("ModelRef is immutable")

    @classmethod
    def gguf(cls, path):
        """A llama.cpp model: a path to a GGUF file on this machine."""
        return cls(KIND_GGUF, path)

    @classmethod
    def ollama(cls, tag):
        """An Ollama model: a registry tag such as 'qwen2.5-coder:latest'."""
        return cls(KIND_OLLAMA, tag)

    @classmethod
    def parse(cls, text, default_kind=None):
        """A ModelRef from a string that crossed a CLI, JSON, or config boundary.

        An explicit "gguf:" or "ollama:" prefix always wins and is stripped.
        Without one, `default_kind` decides; when that is None too, the shape
        of the value decides: something ending in .gguf, or containing a path
        separator, is a file, and anything else is a registry tag. That
        inference is a convenience for hand-typed input only, and it is why
        as_text() always writes the prefix back out: a ref that has been
        round-tripped through as_text() never has to be guessed at again.

        Returns the argument unchanged if it is already a ModelRef.
        """
        if isinstance(text, cls):
            return text
        if text is None:
            raise ValueError("no model reference given")
        raw = str(text).strip()
        if not raw:
            raise ValueError("no model reference given")
        m = _KIND_PREFIX_RE.match(raw)
        if m:
            return cls(m.group("kind"), m.group("value"))
        if default_kind is not None:
            return cls(default_kind, raw)
        low = raw.lower()
        if low.endswith(".gguf") or "/" in raw or "\\" in raw:
            return cls(KIND_GGUF, raw)
        return cls(KIND_OLLAMA, raw)

    def as_text(self):
        """The round-trippable form: "<kind>:<value>". parse() reads this back
        without having to infer anything."""
        return "{}:{}".format(self.kind, self.value)

    @property
    def display(self):
        """A short name for a UI. For a GGUF that is the filename without its
        directory, since the full path is usually long and uninformative; for
        an Ollama tag it is the tag itself."""
        if self.kind == KIND_GGUF:
            return os.path.basename(self.value) or self.value
        return self.value

    def __eq__(self, other):
        return (isinstance(other, ModelRef) and other.kind == self.kind
                and other.value == self.value)

    def __hash__(self):
        return hash((self.kind, self.value))

    def __repr__(self):
        return "ModelRef({!r}, {!r})".format(self.kind, self.value)


def model_kind_hint(model):
    """Which namespace `model` names, or None when its shape does not say.

    The difference from ModelRef.parse's own inference is the None: parse()
    must always produce a ref, so it falls back to "registry tag" for
    anything it cannot place. This function refuses to guess, and that
    refusal is what the selection rules are built on. A caller can then
    treat "gguf", "ollama" and "no idea" as three different situations
    rather than two, which is exactly what separates "route this to the
    engine that can run it" from "resolve this in whichever engine is
    already active".

    The evidence, in order:

      An explicit "gguf:" or "ollama:" prefix. Always decisive.
      A .gguf suffix. Only llama.cpp loads those.
      A path separator. A registry tag can technically contain one
        ("hf.co/user/repo:Q4"), so this is the one rule that can be wrong;
        it matches ModelRef.parse's own long-standing inference, and a tag
        of that shape is served by writing the "ollama:" prefix, which the
        rule above honours. Being wrong here fails loudly with a named
        model rather than silently.
      A colon. "qwen2.5-coder:latest" -- a registry tag. Checked last so a
        Windows drive letter ("C:\\models\\x.gguf") has already been claimed
        by one of the two rules above.

    Anything else -- a bare "llama3.2", the router's "auto" sentinel, an
    empty value -- is ambiguous and returns None.
    """
    if isinstance(model, ModelRef):
        return model.kind
    raw = "" if model is None else str(model).strip()
    if not raw:
        return None
    m = _KIND_PREFIX_RE.match(raw)
    if m:
        return m.group("kind")
    if raw.lower().endswith(".gguf"):
        return KIND_GGUF
    if "/" in raw or "\\" in raw or os.sep in raw:
        return KIND_GGUF
    if ":" in raw:
        return KIND_OLLAMA
    return None


# --------------------------------------------------------------------------
# Chat result
# --------------------------------------------------------------------------

def wire_safe_tool_calls(tool_calls):
    """Tool calls in the one shape BOTH engines will accept back.

    A tool call is read twice, and both readers have to be satisfied by
    the one shape this returns. hearth_loop reads it to decide which tool
    to run; then hearth_loop appends the whole assistant message to
    `messages` and the SAME dict is posted straight back to an engine on
    the next turn (hearth_llama.chat forwards `messages` to llama-server
    untranslated). A field that only the first reader needs is optional. A
    field the ENGINE needs is not, and omitting one does not fail here --
    it fails one turn later, as a 500 on the follow-up request, with the
    tool already executed and its result thrown away.

    That is not hypothetical for the Ollama path either. A session's
    `messages` list outlives a single turn (desktop/server/engine.py keeps
    it across the whole conversation) while the model a turn runs on does
    not: hearth_router picks per turn, so a tool call Ollama produced can
    be replayed to llama.cpp two turns later. Both engines' output is
    normalised here so that whichever one reads it next, it parses.

    WHAT LLAMA-SERVER REQUIRES, measured against build 10105 by posting
    each variant and reading the status back, not taken from the OpenAI
    docs:

      type          REQUIRED, and must be exactly "function". Omitting it
                    is `500 Failed to parse messages: Missing tool call
                    type`; any other value is `Unsupported tool call
                    type`. This is the field that made Hearth Code
                    unusable on the bundled engine: Ollama ignores its
                    absence, so nothing showed until llama.cpp became the
                    default.
      function      REQUIRED. Absent is `Missing tool call function`.
      function.name REQUIRED as a KEY. Absent is `Missing tool call name`;
                    an empty string is accepted. This one is always
                    written below, so it is a constraint being held rather
                    than a bug being fixed.
      function.arguments
                    REQUIRED as a key -- absent is `500 ... key
                    'arguments' not found`. The value may be a JSON
                    object, a string that parses as JSON, or null. An
                    EMPTY string is `500 Failed to parse tool call
                    arguments as JSON`, which matters because
                    hearth_llama.consume_stream starts every slot's
                    arguments at "" and only appends what the deltas
                    carry: a call to one of hearth_tools' no-argument
                    tools can legitimately produce "" and would take the
                    turn down. It is normalised to {} here.
      id            OPTIONAL. Verified accepted both present and absent.

    And on the `tool` messages hearth_loop appends beside the assistant
    one: `content` is REQUIRED (400 `All non-assistant messages must
    contain 'content'`), while `tool_call_id` is optional and is not even
    checked against the ids on the assistant turn -- results pair
    positionally.

    Ollama's own tool calls arrive with `arguments` already a dict, no
    `type`, and an `index` inside the function object. Rebuilding rather
    than patching drops that `index`, which nothing reads and which is not
    part of the request schema either engine documents.

    Arguments that are non-empty and still will not parse are passed
    through as the raw string, so hearth_loop's own "could not parse
    arguments" notice stays reachable instead of being swallowed here.
    That string is what the model actually emitted; inventing {} for it
    would hide a real failure behind a tool call with no arguments.
    """
    shaped = []
    for call in tool_calls or []:
        fn = (call or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError):
                # "" is not a malformed call, it is a call with no
                # arguments; the engine rejects it either way.
                parsed = {} if not args.strip() else args
            args = parsed
        entry = {"type": "function",
                 "function": {"name": fn.get("name") or "", "arguments": args}}
        if call.get("id"):
            entry["id"] = call["id"]
        shaped.append(entry)
    return shaped


def _ollama_shaped_message(content, tool_calls):
    """An assistant message from content plus OpenAI-shaped tool calls.

    The llama.cpp path's entry point into wire_safe_tool_calls; see there
    for what each engine requires of a tool call and why it is enforced on
    the way out rather than trusted on the way in.
    """
    out = {"role": "assistant", "content": content or ""}
    shaped = wire_safe_tool_calls(tool_calls)
    if shaped:
        out["tool_calls"] = shaped
    return out


def wire_safe_assistant_message(message):
    """An engine's own assistant message, made safe to replay to either
    engine.

    For the Ollama paths, which get a whole message dict from the response
    body rather than the (content, tool_calls) pair the llama.cpp path
    hands over. Only "tool_calls" is touched, and only when there are
    some; every other key Ollama sent (content, thinking, images) is
    passed through untouched, and a message with no tool calls is returned
    as it came in. See wire_safe_tool_calls for what is being enforced.
    """
    if not isinstance(message, dict) or not message.get("tool_calls"):
        return message
    out = dict(message)
    out["tool_calls"] = wire_safe_tool_calls(message["tool_calls"])
    return out


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------
#
# One token stream, two wire formats. Callers see exactly one thing --
# on_token(text) called once per fragment of assistant text, in order, as it
# arrives -- and this is where the two engines' genuinely different framing
# is absorbed rather than leaked upward.
#
# The two formats, both transcribed from live captures rather than docs:
#
#   llama-server (hearth_llama.consume_stream, already written and not
#   duplicated here): OpenAI-shaped SSE. `data: {json}` frames, a terminating
#   `data: [DONE]`, the opening frame carrying "content": null rather than
#   "", a usage frame with an EMPTY choices list, usage present only when the
#   request asked for stream_options.include_usage, and tool-call arguments
#   arriving as string FRAGMENTS that have to be reassembled per index.
#
#   Ollama (consume_ollama_stream, below): newline-delimited JSON. No `data:`
#   prefix, no sentinel line, no separate usage frame. Measured against
#   Ollama 0.30.7 with llama3.2:3b, mistral:7b and qwen2.5-coder:latest:
#
#     {"model":"llama3.2:3b","message":{"role":"assistant","content":"Here"},"done":false}
#     {"model":"llama3.2:3b","message":{"role":"assistant","content":" we"},"done":false}
#     {"model":"llama3.2:3b","message":{"role":"assistant","content":""},"done":true,
#      "done_reason":"stop","prompt_eval_count":38,"eval_count":28,...}
#
# Four places they differ, and each is handled here rather than pushed onto
# a caller:
#
#   1. Usage rides on the FINAL frame (the one with "done": true), not on a
#      frame of its own, and needs no opt-in flag. llama.cpp needs
#      include_usage or reports none at all.
#   2. content is always a string, never null. The llama.cpp trap of a
#      TypeError on the very first frame does not exist here, so this does
#      not carry a defence against it that would never fire.
#   3. Tool calls arrive COMPLETE in a single frame, with `arguments`
#      already a parsed dict:
#        "tool_calls":[{"id":"call_2rxkg38p",
#                       "function":{"index":0,"name":"get_weather",
#                                   "arguments":{"city":"Paris"}}}]
#      There is nothing to reassemble, and the per-index accumulation
#      llama.cpp requires would be wrong here: appending fragments of two
#      separate complete calls that happen to share an index would fuse
#      them. So complete entries are appended, not merged.
#   4. `index` sits INSIDE the function object, not on the tool call, which
#      is why nothing here reads a top-level "index".
#
# The result dict has the same keys either way, so LlamaBackend.chat and
# OllamaBackend.chat below fold their two engines into one return shape
# without either of them special-casing the other.


def _ollama_stream_error(frame):
    """Ollama's mid-stream error text, or None when `frame` is not one.

    Ollama reports a failure that happens after the response has already
    begun as an ordinary NDJSON line carrying an "error" key, not as an HTTP
    status (the status was already 200 by then). Accepts both the string
    form Ollama actually sends and the nested-object form llama.cpp uses,
    since a caller reading this module should not have to know which engine
    produced a failure to be told about it.
    """
    err = frame.get("error")
    if isinstance(err, str) and err.strip():
        return err.strip()
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return None


def consume_ollama_stream(lines, on_token=None):
    """Fold Ollama's newline-delimited JSON chat frames into one result.

    A pure function over an iterable of already-decoded lines, exactly like
    hearth_llama.consume_stream is for SSE, so the frame handling is
    testable against a captured real transcript with no daemon, no socket,
    and no model. See the block comment above for the measured format.

    `on_token(text)` is called once per non-empty content fragment, in
    order, before anything else is done with that fragment -- a consumer
    hears a token at the moment it lands, not at the end of the frame loop.

    Returns {"content", "tool_calls", "finish_reason", "tokens_in",
    "tokens_out", "model", "complete", "stopped"}, where "tool_calls" is in
    Ollama's own shape (arguments as a dict) because that is the shape
    hearth_loop already reads, "complete" means a frame with "done": true
    was seen, and "stopped" means on_token raised StopStream and the rest of
    the generation was abandoned.

    Raises BackendError on a mid-stream error frame; a stream that simply
    ends early (the server hung up) is NOT an error here, it comes back with
    complete=False and whatever had arrived, because the caller can tell
    those apart and only one of them is worth a traceback.
    """
    content = []
    tool_calls = []
    finish_reason = None
    tokens_in = 0
    tokens_out = 0
    model = None
    complete = False
    stopped = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue

        problem = _ollama_stream_error(frame)
        if problem:
            raise BackendError("Ollama streamed an error: {}".format(problem))

        model = frame.get("model") or model
        message = frame.get("message")
        if isinstance(message, dict):
            piece = message.get("content")
            if piece:
                content.append(piece)
                if on_token is not None:
                    try:
                        on_token(piece)
                    except StopStream:
                        # The consumer has hung up. Stop reading right here:
                        # returning promptly is what closes the response body
                        # and makes Ollama stop generating, and the tokens
                        # already collected are still worth reporting.
                        stopped = True
                        break
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    tool_calls.append(call)

        if frame.get("done"):
            complete = True
            finish_reason = frame.get("done_reason") or finish_reason
            # Usage rides on this frame and nowhere else. int() rather than
            # `or 0` on a possibly-absent key, matching _usage_from_response
            # in hearth_loop: an undercounted prompt is exactly the defect
            # that module's own docstring records.
            tokens_in = int(frame.get("prompt_eval_count") or tokens_in)
            tokens_out = int(frame.get("eval_count") or tokens_out)
            break

    return {
        "content": "".join(content),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model": model,
        "complete": complete,
        "stopped": stopped,
    }


def ollama_message_from_stream(folded):
    """The Ollama-shaped assistant message a folded stream describes.

    The non-streaming path gets this dict straight from the response body;
    the streaming path has to rebuild it, and it must come out identical in
    shape so nothing downstream can tell which path produced it. In
    particular "tool_calls" is present only when there are some, because
    hearth_loop and engine.py both branch on truthiness there and an empty
    list would read the same as a missing key today but is a difference
    waiting to matter.

    The tool calls go through wire_safe_tool_calls on the way out for the
    same reason the llama.cpp path's do: this message is appended to a
    session's `messages` and may be posted back to EITHER engine on a
    later turn, and Ollama's own shape is missing the "type" llama-server
    refuses a request without.
    """
    out = {"role": "assistant", "content": folded.get("content") or ""}
    shaped = wire_safe_tool_calls(folded.get("tool_calls"))
    if shaped:
        out["tool_calls"] = shaped
    return out


def _iter_body_lines(resp):
    """Decoded lines from an HTTP response body, newline stripped.

    Iterating the response object rather than reading it whole is the entire
    point: http.client yields each line as the socket delivers it, so a
    token reaches on_token while the model is still generating the next one.
    """
    for raw in resp:
        yield raw.decode("utf-8", "replace").rstrip("\r\n")


# --------------------------------------------------------------------------
# Backend interface
# --------------------------------------------------------------------------

class Backend:
    """What every Hearth inference engine must be able to do.

    Subclasses set `name` and `model_kind` and implement the five methods
    below. This base class implements only check_ref(), because that check
    is identical for every backend and must not be re-derived (or quietly
    skipped) per implementation.
    """

    #: One of BACKENDS.
    name = None
    #: The single ModelRef kind this backend accepts. See check_ref.
    model_kind = None

    def check_ref(self, ref):
        """Return `ref` as a ModelRef, or raise ModelKindError if it names a
        model in the other backend's namespace.

        SHAPE IS EVIDENCE, not just an explicit prefix. This used to parse a
        bare string with this backend's own kind as the default, so
        LlamaBackend.check_ref("qwen2.5-coder:latest") happily produced
        ModelRef(gguf, "qwen2.5-coder:latest") and the failure surfaced three
        layers down as llama-server refusing to open a file by that name.
        model_kind_hint() is consulted first now: a string whose shape names
        a namespace is held to it, and only a genuinely ambiguous one (a bare
        "llama3.2", with no separator, suffix or colon) falls back to this
        backend's own kind, so a caller that has only ever seen one backend
        keeps working.
        """
        if isinstance(ref, ModelRef):
            parsed = ref
        else:
            parsed = ModelRef.parse(ref, default_kind=model_kind_hint(ref) or self.model_kind)
        if parsed.kind != self.model_kind:
            raise ModelKindError(
                "the {} backend takes a {} model reference, but {!r} is a {} "
                "reference; these name models in different namespaces and one "
                "cannot be converted into the other".format(
                    self.name, self.model_kind, parsed.value, parsed.kind))
        return parsed

    def chat(self, ref, messages, tools=None, timeout=DEFAULT_CHAT_TIMEOUT,
             options=None, on_token=None):
        """One chat turn. Returns {"message", "tokens_in", "tokens_out",
        "model", "backend", "stopped"}, where "message" is an Ollama-shaped
        assistant message dict.

        `on_token(text)` is called once per fragment of assistant text as it
        arrives off the wire, in order, on the calling thread. It is the one
        piece of this interface that must behave identically on both
        engines despite their wire formats having nothing in common; see the
        Streaming block comment above for what each of them actually sends.

        A callback that raises StopStream abandons the generation and hangs
        up on the engine. That is not an error: chat() returns normally,
        with the text that had already arrived and "stopped": True.
        """
        raise NotImplementedError

    def own_vram_bytes(self):
        """(bytes, known) for VRAM held by Hearth's OWN inference stack.

        `known` False means ownership could not be determined at all, which
        a caller must treat differently from a confident zero. See
        hearth_idle: reading Hearth's own resting model as contention is
        what made that module report a permanently busy machine once
        already.
        """
        raise NotImplementedError

    def measure(self, ref, prompt=None, num_predict=DEFAULT_BENCH_TOKENS, timeout=None):
        """Time one deterministic generation. Returns a dict with at least
        ok, error, tokens_generated, wall_seconds, tokens_per_second, and
        tokens_per_second_source. Never raises."""
        raise NotImplementedError

    def diagnose(self):
        """Why this backend is or is not usable, in this backend's own terms.
        Returns {"ok", "status", "message", "remedy", "detail"}. Never
        raises."""
        raise NotImplementedError

    def available_models(self):
        """Every model this backend could run right now, as ModelRefs.
        Returns [] when none are available or the engine cannot be asked;
        never raises."""
        raise NotImplementedError

    def close(self):
        """Release anything this backend owns. Safe to call more than once,
        and safe to call on a backend that never started anything."""


# --------------------------------------------------------------------------
# nvidia-smi: per-process VRAM, for "what do WE hold"
# --------------------------------------------------------------------------

def _compute_apps():
    """Every GPU compute process, as [{"pid", "name", "bytes"}, ...].

    From `nvidia-smi --query-compute-apps=pid,process_name,used_memory`.
    This is the only way to answer "how much VRAM does OUR engine hold" for
    llama.cpp: unlike Ollama, llama-server publishes no residency endpoint,
    but it is our own child, so its PID is a fact we already have.

    Measured live on an NVIDIA host, and the exact output is the reason
    process_name is queried rather than just pid:

        4054178, /nix/store/...-ollama-0.30.7/lib/ollama/llama-server, 4636

    OLLAMA'S OWN RUNNER IS ALSO CALLED llama-server. Ollama embeds
    llama.cpp and execs a binary of exactly that name, so matching compute
    processes by basename would count a completely unrelated Ollama
    workload as Hearth's own resident model and hand hearth_idle the
    opposite of the truth. The full path is what tells them apart, and it
    is why own_vram_bytes() compares resolved paths and never names.

    THE NAME IS NOT ALWAYS AVAILABLE. On the same host, with the Ollama
    service running sandboxed under systemd, the identical query returned

        4067927, [No data], 2534

    because nvidia-smi could not resolve a path for a process in another
    namespace. So the name is reported verbatim, including "[No data]", and
    callers must treat it as a hint that may be absent rather than as a
    reliable identifier. own_vram_bytes() is built around that: its
    authority is the PID, which is always present and always exact for a
    server this process started, and the path comparison is a best-effort
    secondary that simply does not match when the name is unavailable. That
    failure direction is the safe one -- an unidentifiable process is left
    counted as somebody else's, which widens what looks like contention
    rather than narrowing it.

    Returns None on any failure whatsoever (no nvidia-smi, a timeout, a
    nonzero exit), never raises. None means "could not determine", which is
    not the same as an empty list.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=NVIDIA_SMI_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        # A process path can itself contain a comma, so split from the
        # left for the pid and from the right for the memory, and treat
        # everything between as the name.
        head, _, rest = line.partition(",")
        name, _, tail = rest.rpartition(",")
        try:
            pid = int(head.strip())
            used = int(float(tail.strip()) * 1024 * 1024)
        except ValueError:
            continue
        out.append({"pid": pid, "name": name.strip(), "bytes": used})
    return out


def _same_file(a, b):
    """True when two paths name the same file on disk.

    os.path.samefile where it works, a normalised-case comparison of the
    real paths otherwise, because nvidia-smi reports a path that may not be
    openable by this user (another account's process) and samefile raises
    in that case rather than answering.
    """
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        pass
    try:
        return (os.path.normcase(os.path.realpath(a))
                == os.path.normcase(os.path.realpath(b)))
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------
# The bundled llama.cpp backend
# --------------------------------------------------------------------------

class LlamaBackend(Backend):
    """Hearth's own llama-server: one resident model, a process we own.

    The server is started lazily on the first chat() and kept for as long
    as the same model is asked for. A chat() naming a different GGUF stops
    the running server and starts a fresh one on the new file, because
    llama-server holds exactly one model and two of them rarely fit in
    consumer VRAM (see hearth_llama.Server.swap_model).

    Not thread-safe across a model change: a lock serialises the start and
    swap paths, so two threads asking for different models cannot race into
    two servers, but a swap that lands between another thread's check and
    its request will surface as that request failing rather than silently
    answering from the wrong model.
    """

    name = BACKEND_LLAMA
    model_kind = KIND_GGUF

    def __init__(self, server_path=None, ctx_size=None, n_gpu_layers=None):
        self.server_path = server_path
        self.ctx_size = ctx_size
        self.n_gpu_layers = n_gpu_layers
        self._server = None
        self._ref = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    def _ensure(self, ref):
        """The running Server for `ref`, starting or swapping as needed."""
        with self._lock:
            if self._server is not None and self._ref == ref:
                if self._server.proc is not None and self._server.proc.poll() is None:
                    return self._server
                # The process died between turns. Drop it and start clean
                # rather than handing back a Server whose chat() would
                # raise a less informative "not running".
                self._server = None
                self._ref = None
            if self._server is not None:
                self._server.stop()
                self._server = None
                self._ref = None
            self._server = hearth_llama.start(
                ref.value, server_path=self.server_path, ctx_size=self.ctx_size,
                n_gpu_layers=self.n_gpu_layers,
            )
            self._ref = ref
            return self._server

    @property
    def server(self):
        """The running hearth_llama.Server, or None when nothing is loaded.
        Exposed so hearth_idle can find our PID without starting anything."""
        return self._server

    def close(self):
        with self._lock:
            if self._server is not None:
                try:
                    self._server.stop()
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    pass
            self._server = None
            self._ref = None

    # -- interface ---------------------------------------------------------

    def chat(self, ref, messages, tools=None, timeout=DEFAULT_CHAT_TIMEOUT,
             options=None, on_token=None):
        """One turn against the bundled server, loading the model if needed.

        `options` is Ollama's request-level options dict, accepted so that
        one call signature serves both backends. Only num_predict is
        translated (to max_tokens); num_ctx is deliberately NOT, because on
        llama.cpp the context length is fixed at spawn time by -c and
        cannot be changed per request. Passing a per-request num_ctx to a
        server already loaded at another size would be a silent no-op, so
        it is dropped here rather than pretended about. Construct the
        backend with ctx_size= to set it.

        Streaming is not optional on this path and never was: hearth_llama's
        chat() only speaks SSE, so tokens are already arriving one at a time
        whether or not anybody asked. All `on_token` changes is whether they
        are handed onward as they land or only summed up at the end.

        StopStream out of `on_token` is caught HERE rather than being left
        to propagate, and the text collected up to that point is returned
        with "stopped": True. That needs a local accumulator, because the
        exception unwinds hearth_llama.consume_stream, which owns the only
        other copy of the partial content and cannot hand it back once it
        has been unwound. hearth_llama is not this iteration's to change,
        and it should not have to be: a control-flow signal that a consumer
        invented is this module's to absorb.
        """
        ref = self.check_ref(ref)
        server = self._ensure(ref)
        params = {}
        if options:
            num_predict = options.get("num_predict")
            if isinstance(num_predict, int) and num_predict > 0:
                params["max_tokens"] = num_predict
            for key in ("temperature", "top_p", "top_k", "seed"):
                if options.get(key) is not None:
                    params[key] = options[key]

        seen = []
        stopped = False

        def _sink(piece):
            seen.append(piece)
            if on_token is not None:
                on_token(piece)

        try:
            got = server.chat(messages, on_token=_sink, tools=tools,
                              timeout=timeout, **params)
        except hearth_llama.LlamaError as exc:
            raise BackendError(str(exc)) from exc
        except StopStream:
            stopped = True
            got = {"content": "".join(seen), "tool_calls": [],
                   "tokens_in": 0, "tokens_out": 0, "model": None}
        return {
            "message": _ollama_shaped_message(got.get("content"), got.get("tool_calls")),
            "tokens_in": int(got.get("tokens_in") or 0),
            "tokens_out": int(got.get("tokens_out") or 0),
            "model": got.get("model") or ref.display,
            "backend": self.name,
            "stopped": stopped,
        }

    def own_vram_bytes(self):
        """VRAM held by Hearth's own llama-server, via nvidia-smi's
        per-process query.

        Two things are counted as ours: the server this backend started
        (matched by PID, which is exact), and any other compute process
        running the SAME llama-server binary Hearth resolves (matched by
        resolved path). The second case covers a Hearth sidecar holding a
        model while some other Hearth process asks the idle question, which
        would otherwise read its own resting model as somebody else's
        contention. That is the exact defect that made hearth_idle report a
        permanently busy machine once already.

        The second match is on the resolved binary PATH, never on the
        process name, because Ollama's embedded runner is also called
        "llama-server" (verified live, see _compute_apps). Matching by name
        would count an unrelated Ollama workload as Hearth's own model and
        invert the answer.

        The path match is best effort, and deliberately so: nvidia-smi
        cannot always report a process path (it returned "[No data]" for a
        sandboxed service on the host this was measured on), and when it
        cannot, that process is left counted as somebody else's. Erring
        that way widens what looks like contention instead of narrowing it,
        which is the direction that costs a delayed job rather than a
        stuttering video call. The PID match has no such caveat: a server
        this backend started is matched exactly.

        The answers, and why the difference matters to hearth_idle:

          (0, True)     nvidia-smi answered and nothing of ours is on the
                        GPU. A confident zero, not a guess, so hearth_idle
                        can attribute all resident memory to other software.
          (bytes, True) that much is ours.
          (None, False) a server of ours is running but nvidia-smi could
                        not be asked, or did not list it. Ownership is
                        unknown, and hearth_idle must widen what counts as
                        possible contention rather than assume zero.

        A running server that nvidia-smi does not list is unknown rather
        than zero because a CPU-only llama.cpp build holds no VRAM at all
        while an unlisted CUDA process holds an unknown amount, and this
        cannot tell those two apart.
        """
        with self._lock:
            server = self._server
            pid = None
            if server is not None and server.proc is not None and server.proc.poll() is None:
                pid = server.proc.pid

        apps = _compute_apps()
        if apps is None:
            return (None, False) if pid is not None else (0, True)

        own_path = self.server_path
        if own_path is None:
            found = hearth_llama.find_server()
            own_path = found["path"] if found.get("found") else None

        total = 0
        matched_pid = False
        for app in apps:
            if pid is not None and app["pid"] == pid:
                total += app["bytes"]
                matched_pid = True
            elif own_path and _same_file(app["name"], own_path):
                total += app["bytes"]
        if pid is not None and not matched_pid:
            return None, False
        return total, True

    def measure(self, ref, prompt=None, num_predict=DEFAULT_BENCH_TOKENS, timeout=None):
        """Time one deterministic generation on the bundled server.

        tokens_per_second is always wall-clock here, and says so in
        tokens_per_second_source. llama-server does report its own
        `timings` block, but hearth_llama.consume_stream folds only the
        usage counts out of the stream and discards timings, so there is no
        server-reported rate to prefer and none is invented.

        The load is reported separately: when the model was not already
        resident, load_seconds is the time spent starting the server and
        waiting out the weight load, and it is excluded from the generation
        window so a cold first measurement is comparable with a warm one.
        """
        result = {
            "ok": False, "error": None, "backend": self.name,
            "model": None, "num_predict": num_predict,
            "tokens_generated": 0, "prompt_tokens": 0,
            "wall_seconds": None, "load_seconds": None,
            "tokens_per_second": None, "tokens_per_second_source": None,
            "residency": None,
        }
        try:
            ref = self.check_ref(ref)
        except (BackendError, ValueError) as exc:
            result["error"] = str(exc)
            return result
        result["model"] = ref.display
        prompt = DEFAULT_BENCH_PROMPT if prompt is None else prompt

        with self._lock:
            warm = (self._server is not None and self._ref == ref
                    and self._server.proc is not None
                    and self._server.proc.poll() is None)
        t_load = time.monotonic()
        try:
            self._ensure(ref)
        except (hearth_llama.LlamaError, OSError) as exc:
            result["error"] = str(exc)
            return result
        result["load_seconds"] = None if warm else time.monotonic() - t_load

        t0 = time.monotonic()
        try:
            got = self.chat(ref, [{"role": "user", "content": prompt}],
                            timeout=timeout or DEFAULT_CHAT_TIMEOUT,
                            options={"num_predict": num_predict, "temperature": 0,
                                     "seed": 42})
        except (BackendError, OSError) as exc:
            result["error"] = str(exc)
            return result
        wall = time.monotonic() - t0

        result["wall_seconds"] = wall
        result["tokens_generated"] = got["tokens_out"]
        result["prompt_tokens"] = got["tokens_in"]
        if got["tokens_out"] > 0 and wall > 0:
            result["tokens_per_second"] = got["tokens_out"] / wall
            result["tokens_per_second_source"] = "wall_clock"
        result["residency"] = self.residency(ref)
        result["ok"] = True
        return result

    def residency(self, ref):
        """How much of `ref` is really in VRAM right now, in the same shape
        hearth_bench.residency reports for Ollama.

        `loaded` is a fact rather than a question here: llama-server holds
        exactly one model and Hearth chose it, so if a server of ours is up
        on this ref, the model is loaded. What is NOT free is how much of
        it landed in VRAM: llama.cpp offloads a chosen number of layers and
        keeps the rest on the CPU, and it publishes no per-model residency
        endpoint. So size_vram_bytes comes from nvidia-smi's per-process
        query, and when that cannot be read, vram_fraction and
        fully_resident are None rather than being inferred from -ngl (which
        llama-server silently clamps to the model's real layer count, so
        the requested number is not evidence of the achieved one).
        """
        out = {"loaded": False, "error": None, "size_bytes": None,
               "size_vram_bytes": None, "vram_fraction": None,
               "fully_resident": None, "note": None}
        with self._lock:
            live = (self._server is not None and self._ref == ref
                    and self._server.proc is not None
                    and self._server.proc.poll() is None)
        if not live:
            out["error"] = "model not currently loaded"
            return out
        out["loaded"] = True
        try:
            out["size_bytes"] = os.path.getsize(hearth_paths.long_path(ref.value))
        except OSError:
            pass
        own, known = self.own_vram_bytes()
        if not known or own is None:
            out["error"] = "per-process VRAM could not be read"
            return out
        out["size_vram_bytes"] = own
        if out["size_bytes"]:
            fraction = own / out["size_bytes"]
            out["vram_fraction"] = fraction
            out["fully_resident"] = fraction >= RESIDENCY_FULL_THRESHOLD
            if not out["fully_resident"]:
                out["note"] = (
                    "only {:.0f}% of this model is resident in VRAM; the rest is "
                    "running on CPU and throughput will fall off sharply".format(
                        fraction * 100))
        return out

    def diagnose(self):
        """Whether the bundled binary is present and will actually run.

        The question for llama.cpp is NOT "is a daemon up on a port": there
        is no daemon until Hearth starts one. It is whether the binary
        Hearth ships is where it should be, whether it executes, and
        whether there is a model file to give it. So a missing binary is
        reported as a broken install rather than as something the user
        should go and start.
        """
        found = hearth_llama.find_server()
        if not found["found"]:
            return {
                "ok": False, "status": "engine_missing", "backend": self.name,
                "message": "Hearth's bundled inference engine ({}) was not found.".format(
                    hearth_llama.SERVER_BASENAME),
                "remedy": ("This Hearth install is incomplete. Reinstall Hearth, or set "
                           "{} to the full path of a llama-server binary.".format(
                               hearth_llama.ENV_SERVER)),
                "detail": found,
            }
        info = hearth_llama.probe_binary(found["path"])
        if not info["ok"]:
            return {
                "ok": False, "status": "engine_broken", "backend": self.name,
                "message": "Hearth's inference engine at {} did not run: {}".format(
                    found["path"], info.get("error") or "no version reported"),
                "remedy": ("Reinstall Hearth. If this machine is missing a system "
                           "library the engine needs, the error above names it."),
                "detail": info,
            }
        models = self.available_models()
        if not models:
            return {
                "ok": False, "status": "no_models", "backend": self.name,
                "message": "The inference engine is ready (build {}, {} backend), but no "
                           "model has been downloaded yet.".format(
                               info["build"], info["backend"]),
                "remedy": "Pick a model in Hearth and let it download.",
                "detail": {"engine": info, "store": _model_store_dir()},
            }
        message = "Inference engine ready: llama.cpp build {}, {} backend, {} model(s) available.".format(
            info["build"], info["backend"], len(models))
        if not info["gpu_offload"]:
            message += (" This build has no GPU support, so generation will run on the "
                        "CPU and be slow.")
        return {
            "ok": True, "status": "ready", "backend": self.name,
            "message": message, "remedy": None,
            "detail": {"engine": info, "models": [m.as_text() for m in models]},
        }

    def available_models(self):
        """Every complete GGUF under Hearth's model store, as ModelRefs.

        Partial downloads (hearth_hf's .part files) are skipped, and for a
        model split across parts only the first part is listed, since that
        is the file llama-server is given and it loads its siblings itself.
        Returns [] when hearth_hf is unavailable or the store does not
        exist yet; never raises.
        """
        root = _model_store_dir()
        if not root:
            return []
        out = []
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in sorted(filenames):
                    low = fn.lower()
                    if not low.endswith(".gguf"):
                        continue
                    split = hearth_hf._SPLIT_RE.match(fn) if hearth_hf is not None else None
                    if split is not None and split.group("index") != "00001":
                        continue
                    out.append(ModelRef.gguf(os.path.join(dirpath, fn)))
        except OSError:
            return []
        return out


def _model_store_dir():
    """Where downloaded GGUFs live, or None when hearth_hf is unavailable."""
    if hearth_hf is None:
        return None
    try:
        return hearth_hf.model_store_dir()
    except Exception:  # noqa: BLE001 - a path resolution failure is not fatal here
        return None


# --------------------------------------------------------------------------
# The optional Ollama backend
# --------------------------------------------------------------------------

def _http_json(url, timeout, body=None):
    """A bounded JSON request. Raises urllib/OS errors to the caller, which
    is what lets each caller below choose its own degradation."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class OllamaBackend(Backend):
    """A daemon somebody else runs, holding whichever models it likes.

    Hearth owns no part of this lifecycle: it cannot start Ollama, cannot
    choose when a model is unloaded, and cannot know a model is resident
    without asking. Every method here is therefore a question over HTTP
    with a bounded timeout, and every one of them degrades to "could not
    determine" rather than raising.
    """

    name = BACKEND_OLLAMA
    model_kind = KIND_OLLAMA

    def __init__(self, base_url=None):
        self.base_url = (base_url or DEFAULT_OLLAMA_URL).rstrip("/")

    def chat(self, ref, messages, tools=None, timeout=DEFAULT_CHAT_TIMEOUT,
             options=None, on_token=None):
        """One /api/chat turn, streamed when somebody is listening.

        `on_token` decides the wire mode, and that is a real decision rather
        than an oversight. With a listener, "stream": true is sent and each
        newline-delimited frame is folded as it lands (see
        consume_ollama_stream), so a token reaches the callback while the
        model is still producing the next one. Without one, "stream": false
        is sent and the body is read whole: streaming to nobody buys nothing
        and costs a frame parser plus one socket read per token, and the
        non-streaming response is Ollama's own most-tested path.

        Both modes return the same shape, including token counts: Ollama
        puts prompt_eval_count/eval_count on the terminal streamed frame
        just as it puts them on a non-streamed body, so nothing is given up
        by streaming. That is not true of llama.cpp, which needs
        stream_options.include_usage or reports no usage at all.

        A callback raising StopStream ends the read and hangs up, which is
        what makes Ollama stop generating; the result carries what had
        arrived and "stopped": True.
        """
        ref = self.check_ref(ref)
        stream = on_token is not None
        body = {"model": ref.value, "messages": messages, "tools": tools,
                "stream": stream}
        if options:
            body["options"] = options
        url = self.base_url + "/api/chat"
        try:
            if not stream:
                data = _http_json(url, timeout, body)
                message = wire_safe_assistant_message(data.get("message") or {})
                return {
                    "message": message,
                    "tokens_in": int(data.get("prompt_eval_count") or 0),
                    "tokens_out": int(data.get("eval_count") or 0),
                    "model": data.get("model") or ref.value,
                    "backend": self.name,
                    "stopped": False,
                }
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                folded = consume_ollama_stream(_iter_body_lines(resp), on_token=on_token)
        except urllib.error.HTTPError as exc:
            raise BackendError("Ollama rejected the request (HTTP {})".format(exc.code)) from exc
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            raise BackendError("could not reach Ollama at {}: {}".format(
                self.base_url, exc)) from exc
        return {
            "message": ollama_message_from_stream(folded),
            "tokens_in": int(folded["tokens_in"]),
            "tokens_out": int(folded["tokens_out"]),
            "model": folded["model"] or ref.value,
            "backend": self.name,
            "stopped": folded["stopped"],
        }

    def own_vram_bytes(self):
        """VRAM Ollama reports resident, summed over GET /api/ps.

        Ollama's own accounting, not a guess from process names: /api/ps
        reports size_vram per loaded model. (None, False) on any failure,
        which a caller must read as "ownership unknown", not as "Ollama
        holds nothing" -- an unreachable Ollama might still be mid-restart
        with a model loaded.
        """
        try:
            data = _http_json(self.base_url + "/api/ps", PS_TIMEOUT)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return None, False
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None, False
        total = 0
        for entry in data["models"]:
            if not isinstance(entry, dict):
                continue
            size_vram = entry.get("size_vram")
            if isinstance(size_vram, (int, float)) and size_vram > 0:
                total += size_vram
        return int(total), True

    def measure(self, ref, prompt=None, num_predict=DEFAULT_BENCH_TOKENS, timeout=None):
        """Time one deterministic generation via hearth_bench's Ollama path.

        Delegates to hearth_bench.measure_ollama rather than reimplementing
        it: that function reads Ollama's own load/prompt_eval/eval duration
        fields, attributes GPU energy to the generation sub-window, and
        attaches residency, none of which this module should own a second
        copy of. Imported lazily because hearth_bench imports this module.
        """
        try:
            ref = self.check_ref(ref)
        except (BackendError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "backend": self.name,
                    "model": None, "tokens_generated": 0,
                    "tokens_per_second": None, "tokens_per_second_source": None,
                    "wall_seconds": None}
        import hearth_bench  # noqa: PLC0415 - lazy, hearth_bench imports this module
        out = hearth_bench.measure_ollama(
            self.base_url, ref.value, prompt=prompt, num_predict=num_predict,
            timeout=timeout or hearth_bench.GENERATE_TIMEOUT)
        out["backend"] = self.name
        return out

    def reachable(self):
        """(ok, version_or_None, error_or_None) from GET /api/version."""
        try:
            data = _http_json(self.base_url + "/api/version", REACHABLE_TIMEOUT)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            return False, None, "{}: {}".format(type(exc).__name__, exc)
        version = data.get("version") if isinstance(data, dict) else None
        return True, version, None

    def diagnose(self):
        """Whether the daemon is up and has anything pulled.

        Deliberately narrower than hearth_setup.diagnose(), which also
        checks that Ollama is installed at all, that its version clears the
        floor, and that a pulled model fits this hardware. This is the
        backend-level question only: can a chat turn be attempted right
        now. hearth_setup composes this with the rest.
        """
        ok, version, error = self.reachable()
        if not ok:
            return {
                "ok": False, "status": "not_running", "backend": self.name,
                "message": "Ollama is not reachable at {} ({}).".format(self.base_url, error),
                "remedy": "Start Ollama, or switch Hearth to its own bundled engine.",
                "detail": {"base_url": self.base_url, "error": error},
            }
        models = self.available_models()
        if not models:
            return {
                "ok": False, "status": "no_models", "backend": self.name,
                "message": "Ollama is running at {}, but no models are pulled.".format(
                    self.base_url),
                "remedy": "Pull a model, for example: ollama pull qwen2.5-coder",
                "detail": {"base_url": self.base_url, "version": version},
            }
        return {
            "ok": True, "status": "ready", "backend": self.name,
            "message": "Ollama {} is running at {} with {} model(s) pulled.".format(
                version or "(unknown version)", self.base_url, len(models)),
            "remedy": None,
            "detail": {"base_url": self.base_url, "version": version,
                       "models": [m.as_text() for m in models]},
        }

    def available_models(self):
        """Every model GET /api/tags reports as pulled, as ModelRefs.
        Returns [] on any failure; never raises."""
        try:
            data = _http_json(self.base_url + "/api/tags", TAGS_TIMEOUT)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return []
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return []
        out = []
        for entry in data["models"]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if name:
                out.append(ModelRef.ollama(name))
        return out


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def _override(env=None):
    """(backend_name_or_None, error_or_None) from %HEARTH_BACKEND%.

    "auto" and an empty value both mean "no override". An unrecognised
    value returns (None, message): a typo must be reported, not silently
    treated as auto, because the whole point of the override is that the
    user is trying to force something.
    """
    env = os.environ if env is None else env
    raw = (env.get(ENV_BACKEND) or "").strip().lower()
    if not raw or raw == "auto":
        return None, None
    if raw in BACKENDS:
        return raw, None
    return None, "{} is set to {!r}, which is not one of: {}, auto".format(
        ENV_BACKEND, raw, ", ".join(BACKENDS))


def _kind_phrase(kind, value):
    """How to describe a model to a person, in that namespace's own terms."""
    if kind == KIND_GGUF:
        return "{!r} is a GGUF model file, which only Hearth's bundled engine can run".format(value)
    return "{!r} is an Ollama model tag, which only Ollama can resolve".format(value)


def select(env=None, ollama_url=None, find_server_fn=None, ollama_probe_fn=None,
           model=None):
    """Decide which backend to use, and record why. Pure decision, no I/O of
    its own beyond the two probes it is handed.

    Returns {"backend", "why", "reason", "override", "override_error",
    "llama_found", "ollama_reachable", "model", "model_kind", "model_ok",
    "model_error", "model_remedy"}. Never raises.

    `model` is the model this decision is for, as a ModelRef or a plain
    string. Passing one is what makes the answer correct rather than merely
    plausible: see the module docstring's Selection section for the
    regression that came of choosing an engine from availability alone.
    Omitting it (or passing an ambiguous bare name) falls back to the
    availability order, which is the right answer for "which engine is this
    install using in general".

    model_ok is False only when the chosen backend genuinely cannot serve
    the named model -- either an override points at the other namespace, or
    the engine that owns this model's namespace is not usable here. In that
    case model_error and model_remedy carry the whole explanation, naming
    the model, the backend and the reason, so no caller has to reconstruct
    it from the flags.

    `find_server_fn` and `ollama_probe_fn` exist so the self-test can drive
    every branch of this without a binary and without a daemon; both
    default to the real probes.
    """
    override, override_error = _override(env)
    find_server_fn = find_server_fn or hearth_llama.find_server
    url = ollama_url or DEFAULT_OLLAMA_URL
    if ollama_probe_fn is None:
        def ollama_probe_fn():
            return OllamaBackend(url).reachable()[0]

    kind = model_kind_hint(model)
    value = model.value if isinstance(model, ModelRef) else (
        None if model is None else str(model).strip())

    out = {"backend": None, "why": None, "reason": None, "override": override,
           "override_error": override_error, "llama_found": None,
           "ollama_reachable": None, "model": value, "model_kind": kind,
           "model_ok": True, "model_error": None, "model_remedy": None}

    def _probe_llama():
        try:
            found = find_server_fn()
            out["llama_found"] = bool(found and found.get("found"))
        except Exception:  # noqa: BLE001 - selection must never raise
            out["llama_found"] = False
        return out["llama_found"]

    def _probe_ollama():
        try:
            out["ollama_reachable"] = bool(ollama_probe_fn())
        except Exception:  # noqa: BLE001 - selection must never raise
            out["ollama_reachable"] = False
        return out["ollama_reachable"]

    # 1. An explicit override wins outright, and is never silently switched.
    #    A model from the other namespace does not change WHICH backend is
    #    reported -- it changes what the decision says about it.
    if override:
        out["backend"] = override
        out["why"] = WHY_OVERRIDE
        out["reason"] = "{} is set to {!r}".format(ENV_BACKEND, override)
        if kind is not None and BACKEND_FOR_KIND[kind] != override:
            out["model_ok"] = False
            out["model_error"] = (
                "{} is set to {!r}, but {}. An explicit backend override is "
                "obeyed rather than silently switched, so this model cannot "
                "be run as configured.".format(ENV_BACKEND, override,
                                               _kind_phrase(kind, value)))
            out["model_remedy"] = (
                "Unset {} to let Hearth choose the backend from the model, or "
                "name a {} model instead.".format(
                    ENV_BACKEND, KIND_FOR_BACKEND[override]))
        return out

    # 2. The model's own namespace, when its shape names one. This is the
    #    rung that was missing.
    if kind == KIND_GGUF:
        out["backend"] = BACKEND_LLAMA
        out["why"] = WHY_MODEL_KIND
        out["reason"] = "{} is a GGUF file, which Hearth's bundled engine runs".format(value)
        if not _probe_llama():
            out["model_ok"] = False
            out["model_error"] = (
                "{}, and that engine ({}) was not found on this machine.".format(
                    _kind_phrase(kind, value), hearth_llama.SERVER_BASENAME))
            out["model_remedy"] = (
                "Reinstall Hearth, or set {} to the full path of a llama-server "
                "binary.".format(hearth_llama.ENV_SERVER))
        return out

    if kind == KIND_OLLAMA:
        out["backend"] = BACKEND_OLLAMA
        out["why"] = WHY_MODEL_KIND
        out["reason"] = "{} is an Ollama model tag, which Ollama resolves".format(value)
        if not _probe_ollama():
            out["model_ok"] = False
            out["model_error"] = (
                "{}, and Ollama is not reachable at {}. Hearth's bundled engine "
                "runs GGUF files on disk and cannot resolve a registry tag, so "
                "it cannot run this model either.".format(
                    _kind_phrase(kind, value), url))
            out["model_remedy"] = (
                "Start Ollama, or pick one of the models Hearth has downloaded "
                "for its own engine.")
        return out

    # 3-5. An ambiguous name (or none at all) resolves in whichever engine is
    #      usable, exactly as it did before model kinds were consulted.
    if _probe_llama():
        out["backend"] = BACKEND_LLAMA
        out["why"] = WHY_BUNDLED
        out["reason"] = "Hearth's bundled inference engine is present"
        return out

    if _probe_ollama():
        out["backend"] = BACKEND_OLLAMA
        out["why"] = WHY_OLLAMA_REACHABLE
        out["reason"] = ("Hearth's bundled engine was not found, but Ollama is "
                         "running and can be used instead")
        return out

    out["backend"] = BACKEND_LLAMA
    out["why"] = WHY_DEFAULT
    out["reason"] = ("neither Hearth's bundled engine nor a running Ollama was "
                     "found; reporting the bundled engine, which is what Hearth "
                     "ships and what its diagnosis should be about")
    return out


def check_model(model, ollama_url=None, env=None):
    """Can `model` be served right now, and by which backend?

    The one call a caller makes when it holds a model name and wants to know
    whether proceeding is worth attempting -- a session being created, a
    turn about to start. Returns {"ok", "backend", "kind", "message",
    "remedy", "why"}, where message and remedy are None when ok is True.
    Never raises, and never starts an engine: this is select() plus a
    friendlier shape, so it costs at most one bounded probe.
    """
    decision = select(env=env, ollama_url=ollama_url, model=model)
    return {
        "ok": decision["model_ok"],
        "backend": decision["backend"],
        "kind": decision["model_kind"],
        "why": decision["why"],
        "message": decision["model_error"],
        "remedy": decision["model_remedy"],
    }


#: At most one instance per backend name, built on first use. A single
#: instance matters for the llama backend specifically: it owns a subprocess
#: holding several GB of VRAM, and a second instance would start a second
#: server that could not allocate.
#:
#: A dict rather than a single slot because selection is per-model now: one
#: process can legitimately serve a GGUF on the bundled engine and a registry
#: tag through Ollama. Each name still has exactly one instance, which is the
#: invariant that actually mattered.
_INSTANCES = {}
#: The instance get_backend() handed out most recently -- "the one answering
#: chat calls", which is what active() reports on.
_ACTIVE = None
_ACTIVE_LOCK = threading.RLock()


def build(name, ollama_url=None):
    """A fresh Backend of the named kind. Does not touch the process-wide
    instance and does not start anything: LlamaBackend spawns its server on
    the first chat(), not here."""
    if name == BACKEND_LLAMA:
        return LlamaBackend()
    if name == BACKEND_OLLAMA:
        return OllamaBackend(ollama_url)
    raise ValueError("unknown backend {!r}; expected one of {}".format(
        name, ", ".join(BACKENDS)))


def get_backend(ollama_url=None, force=None, model=None):
    """The backend that should serve `model`, selected per call and cached
    per backend name.

    PASS THE MODEL. Selection reads its kind (see select()), and a caller
    that omits it gets a backend chosen from availability alone -- which is
    correct for "what is this install using" and wrong for "run this
    model". hearth_loop.chat passes it; anything else holding a model name
    should too.

    `force` names a backend directly and bypasses the environment, the
    model, and the cache, returning a fresh instance the caller owns and
    should close(). Everything else shares one instance per name, because
    the llama backend owns a GPU-resident subprocess that must not be
    duplicated.
    """
    if force:
        return build(force, ollama_url)
    global _ACTIVE
    name = select(ollama_url=ollama_url, model=model)["backend"]
    with _ACTIVE_LOCK:
        instance = _INSTANCES.get(name)
        if instance is None:
            instance = _INSTANCES[name] = build(name, ollama_url)
        _ACTIVE = instance
        return instance


def reset():
    """Drop and close every cached backend, so the next get_backend()
    selects and builds again. For the self-test and for a settings change
    that switches engines."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        for instance in _INSTANCES.values():
            try:
                instance.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        _INSTANCES.clear()
        _ACTIVE = None


def active(ollama_url=None, model=None):
    """Which backend is in use and why, without committing to one.

    The answer a settings screen or a diagnosis needs: the selection
    decision (see select()) plus whether the process-wide backend has
    already been built, since building it is what freezes the choice for
    the rest of the run.

    When one has already been built, "backend" reports the built one rather
    than what select() would choose today, because that is the one actually
    answering chat calls. If the two disagree (the environment changed
    mid-run, or a model finished downloading), "stale" carries the backend
    select() would now pick, so a caller can offer a restart instead of
    reporting a choice that is not in force.

    `model` narrows the question to one model, which is the form a settings
    screen wants when the user has already picked one: the decision then
    carries model_ok/model_error for that specific choice.
    """
    decision = select(ollama_url=ollama_url, model=model)
    with _ACTIVE_LOCK:
        current = _ACTIVE.name if _ACTIVE is not None else None
    decision["instantiated"] = current is not None
    decision["stale"] = None
    if current is not None:
        if current != decision["backend"]:
            decision["stale"] = decision["backend"]
        decision["backend"] = current
    return decision


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-backend",
        description="Which inference engine Hearth will use, and whether it works.")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="also run the tests that need a real engine")
    p.add_argument("--which", action="store_true",
                   help="report the selected backend and why")
    p.add_argument("--diagnose", action="store_true")
    p.add_argument("--models", action="store_true")
    p.add_argument("--backend", choices=BACKENDS, default=None)
    p.add_argument("--model", default=None,
                   help="the model the answer is about; its kind is what "
                        "chooses the backend")
    p.add_argument("--ollama-url", default=None)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    if a.self_test:
        _self_test(live=a.live)
        print("hearth_backend self-test: ok")
        return 0

    if a.which:
        info = active(ollama_url=a.ollama_url, model=a.model)
        if a.json:
            print(json.dumps(info, indent=2, sort_keys=True))
        else:
            print("backend: {} ({})".format(info["backend"], info["why"]))
            print("reason:  {}".format(info["reason"]))
            if info["override_error"]:
                print("warning: {}".format(info["override_error"]))
            if not info["model_ok"]:
                print("problem: {}".format(info["model_error"]))
                print("  -> {}".format(info["model_remedy"]))
        return 0 if info["model_ok"] else 1

    backend = get_backend(ollama_url=a.ollama_url, force=a.backend, model=a.model)
    try:
        if a.models:
            refs = backend.available_models()
            if a.json:
                print(json.dumps([r.as_text() for r in refs], indent=2))
            else:
                for r in refs:
                    print(r.as_text())
            return 0
        info = backend.diagnose()
        if a.json:
            print(json.dumps(info, indent=2, sort_keys=True, default=str))
        else:
            print("[{}] {}".format("ok" if info["ok"] else info["status"], info["message"]))
            if info["remedy"]:
                print("  -> {}".format(info["remedy"]))
        return 0 if info["ok"] else 1
    finally:
        if a.backend:
            backend.close()


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _self_test(live=False):
    # -- ModelRef: the two namespaces stay apart ---------------------------
    # A native path, so display's basename split is exercised on whichever
    # platform is running this rather than only on Windows.
    _gpath = os.path.join("models", "sub", "qwen.gguf")
    g = ModelRef.gguf(_gpath)
    o = ModelRef.ollama("qwen2.5-coder:latest")
    assert g.kind == KIND_GGUF and o.kind == KIND_OLLAMA
    assert g != o
    assert g.display == "qwen.gguf", g.display
    assert o.display == "qwen2.5-coder:latest", o.display

    # Round-tripping through a string boundary never needs a guess.
    assert ModelRef.parse(g.as_text()) == g, g.as_text()
    assert ModelRef.parse(o.as_text()) == o, o.as_text()
    # An Ollama tag's own colon must not be read as a kind prefix.
    assert ModelRef.parse("qwen2.5-coder:latest") == o
    # A Windows drive letter must not be read as a kind prefix either.
    assert ModelRef.parse(r"C:\models\qwen.gguf") == ModelRef.gguf(r"C:\models\qwen.gguf")
    # Shape inference for hand-typed input.
    assert ModelRef.parse("/srv/models/a.gguf").kind == KIND_GGUF
    assert ModelRef.parse("llama3.2").kind == KIND_OLLAMA
    # An explicit default beats shape inference.
    assert ModelRef.parse("weird-name", default_kind=KIND_GGUF).kind == KIND_GGUF
    # Immutable and hashable.
    assert len({ModelRef.gguf("a.gguf"), ModelRef.gguf("a.gguf")}) == 1
    try:
        g.kind = KIND_OLLAMA
        raise AssertionError("ModelRef must be immutable")
    except AttributeError:
        pass
    for bad in (None, "", "   "):
        try:
            ModelRef.parse(bad)
            raise AssertionError("empty model ref must raise: {!r}".format(bad))
        except ValueError:
            pass
    try:
        ModelRef("registry", "x")
        raise AssertionError("unknown kind must raise")
    except ValueError:
        pass

    # -- model_kind_hint: three answers, not two ---------------------------
    assert model_kind_hint("qwen2.5-coder:latest") == KIND_OLLAMA
    assert model_kind_hint(r"C:\models\qwen.gguf") == KIND_GGUF
    assert model_kind_hint("/srv/models/a.gguf") == KIND_GGUF
    assert model_kind_hint("models/qwen") == KIND_GGUF
    assert model_kind_hint("q4.GGUF") == KIND_GGUF
    # An explicit prefix beats every shape rule, in both directions.
    assert model_kind_hint("ollama:hf.co/user/repo:Q4") == KIND_OLLAMA
    assert model_kind_hint("gguf:weird-name") == KIND_GGUF
    # A ModelRef answers for itself.
    assert model_kind_hint(ModelRef.ollama("llama3.2")) == KIND_OLLAMA
    # Ambiguous is its own answer, and this is the whole reason the function
    # exists: ModelRef.parse must guess here, selection must not.
    for ambiguous in ("llama3.2", "auto", "", "   ", None):
        assert model_kind_hint(ambiguous) is None, ambiguous
    assert ModelRef.parse("llama3.2").kind == KIND_OLLAMA, (
        "parse must still guess where hint refuses to")

    # -- check_ref: a wrong-namespace ref is refused, not coerced ----------
    lb = LlamaBackend()
    ob = OllamaBackend("http://127.0.0.1:11434")
    # A bare string is read in the backend's own namespace.
    assert lb.check_ref("model.gguf").kind == KIND_GGUF
    assert ob.check_ref("qwen2.5-coder:latest").kind == KIND_OLLAMA
    # A genuinely ambiguous bare name resolves in this backend's namespace,
    # so a caller that has only ever seen one backend keeps working.
    assert ob.check_ref("llama3.2").kind == KIND_OLLAMA
    assert lb.check_ref("llama3.2").kind == KIND_GGUF
    # A bare string whose SHAPE names the other namespace is refused, not
    # coerced. This is the check that was missing: LlamaBackend used to
    # accept "qwen2.5-coder:latest" as a GGUF path and only fail once
    # llama-server tried to open a file by that name.
    for backend, wrong_text, wrong_kind in (
            (lb, "qwen2.5-coder:latest", KIND_OLLAMA),
            (ob, "weird.gguf", KIND_GGUF),
            (ob, r"C:\models\qwen.gguf", KIND_GGUF)):
        try:
            backend.check_ref(wrong_text)
            raise AssertionError("{} must refuse {!r}".format(backend.name, wrong_text))
        except ModelKindError as exc:
            assert wrong_kind in str(exc) and repr(wrong_text) in str(exc), str(exc)
    # An explicitly tagged ref of the wrong kind raises, and the message
    # names both namespaces rather than just saying "bad model".
    for backend, wrong in ((lb, o), (ob, g)):
        try:
            backend.check_ref(wrong)
            raise AssertionError("{} must refuse a {} ref".format(backend.name, wrong.kind))
        except ModelKindError as exc:
            assert backend.model_kind in str(exc) and wrong.kind in str(exc), str(exc)
    # ModelKindError is a BackendError, so a caller catching the general
    # case still catches it.
    assert issubclass(ModelKindError, BackendError)

    # -- message shaping: llama.cpp output -> what hearth_loop reads -------
    msg = _ollama_shaped_message("hello", [])
    assert msg == {"role": "assistant", "content": "hello"}, msg
    msg = _ollama_shaped_message(None, [
        {"id": "call_1", "function": {"name": "read_file", "arguments": '{"path": "a.md"}'}}])
    assert msg["content"] == "", msg
    assert msg["tool_calls"][0]["function"]["name"] == "read_file", msg
    # The streamed arguments string is decoded, because hearth_loop prefers
    # a dict and only falls back to json.loads on a string.
    assert msg["tool_calls"][0]["function"]["arguments"] == {"path": "a.md"}, msg
    assert msg["tool_calls"][0]["id"] == "call_1", msg
    # Arguments that will not parse are passed through as the raw string,
    # so hearth_loop's own "could not parse arguments" notice stays
    # reachable instead of being swallowed here.
    bad = _ollama_shaped_message("", [{"function": {"name": "x", "arguments": "{oops"}}])
    assert bad["tool_calls"][0]["function"]["arguments"] == "{oops", bad
    # An already-dict arguments value survives untouched.
    d = _ollama_shaped_message("", [{"function": {"name": "x", "arguments": {"a": 1}}}])
    assert d["tool_calls"][0]["function"]["arguments"] == {"a": 1}, d

    # -- the shape llama-server will actually accept back ------------------
    #
    # Every assertion below is a 500 llama-server (build 10105) was
    # measured returning for the shape it rejects. This message is not
    # only read by hearth_loop -- it is posted BACK to the engine on the
    # next turn, so a missing field kills the turn after the tool has
    # already run. See wire_safe_tool_calls.
    #
    # "Missing tool call type". The whole reason Hearth Code could not
    # complete a tool turn on the bundled engine.
    for _call in _ollama_shaped_message("", [
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a"}'}},
            {"function": {"name": "now", "arguments": {}}}])["tool_calls"]:
        assert _call.get("type") == "function", (
            "llama-server answers `500 Missing tool call type` without this, "
            "one turn after the tool has already run", _call)
        # "Missing tool call function" / "Missing tool call name" /
        # "key 'arguments' not found": all three keys must be present.
        assert "function" in _call, _call
        assert "name" in _call.get("function", {}), _call
        assert "arguments" in _call.get("function", {}), _call
    # "Failed to parse tool call arguments as JSON" on an empty string.
    # consume_stream starts every slot's arguments at "" and only appends
    # what the deltas carry, so a call to one of hearth_tools' five
    # no-argument tools reaches here as "". A call with no arguments is
    # {}, not a parse failure.
    for _empty in ("", "   "):
        _noargs = _ollama_shaped_message("", [{"function": {"name": "now", "arguments": _empty}}])
        assert _noargs["tool_calls"][0]["function"]["arguments"] == {}, _noargs
    # An id is optional (verified accepted both ways), so a call the
    # engine gave no id for must not grow an empty one.
    assert "id" not in _ollama_shaped_message(
        "", [{"function": {"name": "now", "arguments": {}}}])["tool_calls"][0]

    # Ollama's own shape is normalised the same way, because a session's
    # `messages` outlives the turn: a tool call Ollama produced can be
    # posted to llama.cpp on a later turn of the same conversation.
    _oll = wire_safe_assistant_message({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call_x",
                        "function": {"index": 0, "name": "get_weather",
                                     "arguments": {"city": "Paris"}}}]})
    assert _oll["tool_calls"][0].get("type") == "function", (
        "Ollama omits the type llama-server requires, and a session's "
        "messages outlive the turn that produced them", _oll)
    assert _oll["tool_calls"][0]["function"]["arguments"] == {"city": "Paris"}, _oll
    assert _oll["tool_calls"][0]["id"] == "call_x", _oll
    # Everything that is not a tool call is left exactly as Ollama sent it,
    # and a message without tool calls is not copied at all.
    _plain = {"role": "assistant", "content": "hi", "thinking": "hmm"}
    assert wire_safe_assistant_message(_plain) is _plain
    _kept = wire_safe_assistant_message({
        "role": "assistant", "content": "hi", "thinking": "hmm",
        "tool_calls": [{"function": {"name": "now", "arguments": {}}}]})
    assert _kept["thinking"] == "hmm", _kept

    # -- Ollama's stream format, folded ------------------------------------
    #
    # Transcribed verbatim from a live capture against Ollama 0.30.7 through
    # a tunnel to a real GPU host, not from documentation. See the Streaming
    # block comment for the four ways this differs from llama.cpp's SSE.
    _cap = [
        '{"model":"llama3.2:3b","created_at":"2026-08-01T04:18:58.310102705Z",'
        '"message":{"role":"assistant","content":"Here"},"done":false}',
        '{"model":"llama3.2:3b","created_at":"2026-08-01T04:18:58.323879385Z",'
        '"message":{"role":"assistant","content":" we"},"done":false}',
        '{"model":"llama3.2:3b","created_at":"2026-08-01T04:18:58.336314326Z",'
        '"message":{"role":"assistant","content":" go"},"done":false}',
        '{"model":"llama3.2:3b","created_at":"2026-08-01T04:18:58.6557908Z",'
        '"message":{"role":"assistant","content":""},"done":true,"done_reason":"stop",'
        '"total_duration":13810303034,"load_duration":13411707438,'
        '"prompt_eval_count":38,"prompt_eval_duration":51058000,'
        '"eval_count":28,"eval_duration":345445000}',
    ]
    _heard = []
    _folded = consume_ollama_stream(iter(_cap), on_token=_heard.append)
    assert _folded["content"] == "Here we go", _folded
    # Each fragment was announced separately and in order. This is the whole
    # point: a caller must see three arrivals, not one string at the end.
    assert _heard == ["Here", " we", " go"], _heard
    # The empty content on the terminal frame is not announced as a token.
    assert "" not in _heard, _heard
    # Usage rides on the done frame, and BOTH halves are read -- an
    # undercounted prompt is the exact defect hearth_loop._usage_from_response
    # exists to record.
    assert (_folded["tokens_in"], _folded["tokens_out"]) == (38, 28), _folded
    assert _folded["complete"] is True and _folded["stopped"] is False, _folded
    assert _folded["finish_reason"] == "stop", _folded
    assert _folded["model"] == "llama3.2:3b", _folded
    # No tool calls means the key is absent from the message, not present
    # and empty: hearth_loop and engine.py both branch on truthiness there.
    assert "tool_calls" not in ollama_message_from_stream(_folded)

    # Tool calls arrive COMPLETE in one frame with arguments already a dict,
    # so nothing is reassembled. Also captured live (llama3.2:3b).
    _tc = [
        '{"model":"llama3.2:3b","message":{"role":"assistant","content":"",'
        '"tool_calls":[{"id":"call_2rxkg38p","function":{"index":0,'
        '"name":"get_weather","arguments":{"city":"Paris"}}}]},"done":false}',
        '{"model":"llama3.2:3b","message":{"role":"assistant","content":""},'
        '"done":true,"done_reason":"stop","prompt_eval_count":156,"eval_count":17}',
    ]
    _f2 = consume_ollama_stream(iter(_tc))
    assert len(_f2["tool_calls"]) == 1, _f2
    assert _f2["tool_calls"][0]["function"]["name"] == "get_weather", _f2
    assert _f2["tool_calls"][0]["function"]["arguments"] == {"city": "Paris"}, _f2
    _m2 = ollama_message_from_stream(_f2)
    assert _m2["tool_calls"][0]["function"]["arguments"] == {"city": "Paris"}, _m2
    # The streaming Ollama path goes through wire_safe_tool_calls too. Its
    # output is appended to a `messages` list that outlives this turn, and
    # hearth_router can put the next turn of the same conversation on the
    # llama.cpp engine, which rejects a tool call with no type.
    assert _m2["tool_calls"][0].get("type") == "function", _m2
    # Two complete calls sharing function.index must stay two calls. The
    # per-index MERGE that llama.cpp's fragmented arguments require would
    # fuse these into one; that is the trap this format inverts.
    _two = consume_ollama_stream(iter([
        '{"message":{"content":"","tool_calls":[{"function":{"index":0,"name":"a",'
        '"arguments":{"x":1}}}]},"done":false}',
        '{"message":{"content":"","tool_calls":[{"function":{"index":0,"name":"b",'
        '"arguments":{"x":2}}}]},"done":false}',
        '{"message":{"content":""},"done":true}',
    ]))
    assert [c["function"]["name"] for c in _two["tool_calls"]] == ["a", "b"], _two

    # StopStream out of the callback abandons the read at that fragment and
    # keeps what had already arrived. Nothing after it is consumed, which is
    # what makes cancellation stop the generation rather than drain it.
    _pulled = []

    def _lines_with_a_tail():
        for line in _cap:
            _pulled.append(line)
            yield line

    def _stop_after_two(piece):
        _heard2.append(piece)
        if len(_heard2) == 2:
            raise StopStream

    _heard2 = []
    _f3 = consume_ollama_stream(_lines_with_a_tail(), on_token=_stop_after_two)
    assert _f3["stopped"] is True and _f3["complete"] is False, _f3
    assert _f3["content"] == "Here we", _f3
    assert len(_pulled) == 2, ("the stream must not be drained past the stop", _pulled)

    # A mid-stream error frame is an error. Ollama sends it as a plain
    # "error" string on an ordinary line, after the status was already 200.
    try:
        consume_ollama_stream(iter(['{"error":"model requires more system memory"}']))
        raise AssertionError("a streamed error frame must raise")
    except BackendError as exc:
        assert "more system memory" in str(exc), str(exc)
    # A stream that just stops is NOT an error: the caller can tell
    # complete=False apart from a raise, and only one of them is a bug.
    _short = consume_ollama_stream(iter([_cap[0]]))
    assert _short["complete"] is False and _short["content"] == "Here", _short
    # Junk lines and blank lines are skipped rather than fatal.
    assert consume_ollama_stream(iter(["", "not json", _cap[3]]))["tokens_out"] == 28

    # StopStream is control flow, not a failure, so a caller that catches
    # BackendError around a chat call does not accidentally swallow it.
    assert not issubclass(StopStream, BackendError), \
        "StopStream must not be caught by `except BackendError`"

    # -- OllamaBackend.chat: the wire mode follows the listener ------------
    #
    # No daemon: urlopen and _http_json are both replaced, so this proves
    # the branch and the request body rather than the network.
    _seen_bodies = []

    class _FakeResp:
        def __init__(self, lines):
            self._lines = [ln.encode("utf-8") + b"\n" for ln in lines]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return iter(self._lines)

    _real_urlopen = urllib.request.urlopen
    _real_http_json = globals()["_http_json"]
    try:
        def _fake_urlopen(req, timeout=None):
            _seen_bodies.append(json.loads(req.data.decode("utf-8")))
            return _FakeResp(_cap)

        def _fake_http_json(url, timeout, body=None):
            _seen_bodies.append(body)
            return {"model": "llama3.2:3b", "message": {"role": "assistant",
                                                        "content": "whole thing"},
                    "prompt_eval_count": 5, "eval_count": 7}

        urllib.request.urlopen = _fake_urlopen
        globals()["_http_json"] = _fake_http_json

        _ob = OllamaBackend("http://127.0.0.1:11434")
        got = _ob.chat("llama3.2:3b", [{"role": "user", "content": "hi"}])
        assert _seen_bodies[-1]["stream"] is False, _seen_bodies[-1]
        assert got["message"]["content"] == "whole thing", got
        assert (got["tokens_in"], got["tokens_out"]) == (5, 7), got
        assert got["stopped"] is False, got

        _tokens = []
        got = _ob.chat("llama3.2:3b", [{"role": "user", "content": "hi"}],
                       on_token=_tokens.append)
        assert _seen_bodies[-1]["stream"] is True, _seen_bodies[-1]
        assert _tokens == ["Here", " we", " go"], _tokens
        assert got["message"] == {"role": "assistant", "content": "Here we go"}, got
        # Streaming gives up nothing: the counts are the same fields, just
        # carried on the terminal frame instead of the whole body.
        assert (got["tokens_in"], got["tokens_out"]) == (38, 28), got
        assert got["backend"] == BACKEND_OLLAMA, got
    finally:
        urllib.request.urlopen = _real_urlopen
        globals()["_http_json"] = _real_http_json

    # -- selection: every branch, no binary and no daemon needed -----------
    yes = lambda: {"found": True, "path": "/x/llama-server"}   # noqa: E731
    no = lambda: {"found": False, "path": None}                # noqa: E731

    # 1. Bundled engine present -> llama, regardless of Ollama.
    s = select(env={}, find_server_fn=yes, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_LLAMA and s["why"] == WHY_BUNDLED, s
    assert s["llama_found"] is True, s
    # Ollama is not even probed once the bundled engine is found.
    assert s["ollama_reachable"] is None, s

    # 2. No bundled engine, Ollama up -> ollama.
    s = select(env={}, find_server_fn=no, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_OLLAMA and s["why"] == WHY_OLLAMA_REACHABLE, s

    # 3. Neither -> llama, because that is what Hearth ships and what the
    #    resulting diagnosis should be about.
    s = select(env={}, find_server_fn=no, ollama_probe_fn=lambda: False)
    assert s["backend"] == BACKEND_LLAMA and s["why"] == WHY_DEFAULT, s

    # 4. Override wins over both probes, in both directions, and is obeyed
    #    even when that backend is not usable -- a user who forced a
    #    backend must be told THAT one is broken.
    s = select(env={ENV_BACKEND: "ollama"}, find_server_fn=yes, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_OLLAMA and s["why"] == WHY_OVERRIDE, s
    s = select(env={ENV_BACKEND: "llama"}, find_server_fn=no, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_LLAMA and s["why"] == WHY_OVERRIDE, s
    assert s["llama_found"] is None, ("an override must not probe", s)
    # Case and surrounding whitespace do not matter.
    s = select(env={ENV_BACKEND: "  OLLAMA  "}, find_server_fn=yes, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_OLLAMA and s["why"] == WHY_OVERRIDE, s
    # "auto" and empty both mean no override.
    for val in ("auto", "", "   "):
        s = select(env={ENV_BACKEND: val}, find_server_fn=yes, ollama_probe_fn=lambda: True)
        assert s["why"] == WHY_BUNDLED, (val, s)
    # A typo is reported, not silently treated as auto.
    s = select(env={ENV_BACKEND: "llamacpp"}, find_server_fn=yes, ollama_probe_fn=lambda: True)
    assert s["override_error"] and "llamacpp" in s["override_error"], s
    assert s["backend"] == BACKEND_LLAMA and s["why"] == WHY_BUNDLED, s
    # A probe that raises must not take selection down with it.
    def _boom():
        raise RuntimeError("probe exploded")
    s = select(env={}, find_server_fn=_boom, ollama_probe_fn=lambda: False)
    assert s["backend"] == BACKEND_LLAMA and s["llama_found"] is False, s
    s = select(env={}, find_server_fn=no, ollama_probe_fn=_boom)
    assert s["backend"] == BACKEND_LLAMA and s["ollama_reachable"] is False, s

    # -- selection: the model decides, availability only breaks ties -------
    #
    # THE REGRESSION THIS EXISTS FOR. A machine with the bundled binary
    # present and a user who picked an Ollama registry tag used to get
    # LlamaBackend, which cannot resolve a tag; every turn died with a
    # generic error before any tool call. Availability is a tiebreaker now.
    s = select(env={}, model="qwen2.5-coder:latest",
               find_server_fn=yes, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_OLLAMA, ("an Ollama tag must not be routed to "
                                            "the bundled engine just because it "
                                            "is installed", s)
    assert s["why"] == WHY_MODEL_KIND and s["model_ok"] is True, s
    # The bundled binary is not even probed: the model already answered.
    assert s["llama_found"] is None, s

    # The mirror image: a GGUF goes to the bundled engine even with Ollama up.
    s = select(env={}, model=r"C:\models\qwen.gguf",
               find_server_fn=yes, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_LLAMA and s["why"] == WHY_MODEL_KIND, s
    assert s["model_ok"] is True and s["ollama_reachable"] is None, s

    # An ambiguous bare name resolves in the active backend's namespace,
    # exactly as it did before any of this existed.
    for ambiguous in (None, "llama3.2", "auto"):
        s = select(env={}, model=ambiguous, find_server_fn=yes,
                   ollama_probe_fn=lambda: True)
        assert s["why"] == WHY_BUNDLED and s["model_ok"] is True, (ambiguous, s)
        s = select(env={}, model=ambiguous, find_server_fn=no,
                   ollama_probe_fn=lambda: True)
        assert s["why"] == WHY_OLLAMA_REACHABLE, (ambiguous, s)

    # An Ollama tag with no reachable Ollama still reports ollama -- that is
    # the engine that owns this model -- and says exactly why it cannot run,
    # naming the model, the URL, and why the other engine is not a fallback.
    s = select(env={}, model="qwen2.5-coder:latest", ollama_url="http://h:11434",
               find_server_fn=yes, ollama_probe_fn=lambda: False)
    assert s["backend"] == BACKEND_OLLAMA and s["model_ok"] is False, s
    for fragment in ("qwen2.5-coder:latest", "http://h:11434", "GGUF"):
        assert fragment in s["model_error"], (fragment, s["model_error"])
    assert s["model_remedy"], s

    # A GGUF with no bundled engine, the mirror image again.
    s = select(env={}, model="/srv/a.gguf", find_server_fn=no,
               ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_LLAMA and s["model_ok"] is False, s
    assert "/srv/a.gguf" in s["model_error"] and hearth_llama.SERVER_BASENAME in s["model_error"], s

    # An override still wins outright and is never silently switched, but a
    # model from the other namespace is now an honest, named error rather
    # than a generic failure three layers down.
    s = select(env={ENV_BACKEND: "llama"}, model="qwen2.5-coder:latest",
               find_server_fn=yes, ollama_probe_fn=lambda: True)
    assert s["backend"] == BACKEND_LLAMA and s["why"] == WHY_OVERRIDE, (
        "an explicit override must not be overridden by the model", s)
    assert s["model_ok"] is False, s
    assert ENV_BACKEND in s["model_error"] and "qwen2.5-coder:latest" in s["model_error"], s
    assert "llama" in s["model_error"], s
    # And the matching direction is simply fine.
    s = select(env={ENV_BACKEND: "ollama"}, model="qwen2.5-coder:latest",
               find_server_fn=no, ollama_probe_fn=lambda: False)
    assert s["backend"] == BACKEND_OLLAMA and s["model_ok"] is True, s

    # check_model is select() in the shape a caller with a model wants.
    _cm = check_model("llama3.2", ollama_url="http://127.0.0.1:1")
    assert set(_cm) == {"ok", "backend", "kind", "why", "message", "remedy"}, _cm
    assert _cm["ok"] is True and _cm["message"] is None, _cm

    # -- build() / get_backend() / reset() ---------------------------------
    assert isinstance(build(BACKEND_LLAMA), LlamaBackend)
    assert isinstance(build(BACKEND_OLLAMA), OllamaBackend)
    try:
        build("nope")
        raise AssertionError("unknown backend must raise")
    except ValueError:
        pass
    # force= bypasses the cache entirely and hands back a fresh instance.
    reset()
    forced = get_backend(force=BACKEND_OLLAMA)
    assert isinstance(forced, OllamaBackend)
    with _ACTIVE_LOCK:
        assert _ACTIVE is None, "force= must not populate the process-wide backend"
        assert not _INSTANCES, "force= must not populate the instance cache"
    # The process-wide backend is built once and reused.
    _old_env = os.environ.get(ENV_BACKEND)
    os.environ[ENV_BACKEND] = BACKEND_OLLAMA
    try:
        reset()
        first = get_backend()
        second = get_backend()
        assert first is second, "get_backend must reuse one instance"
        assert first.name == BACKEND_OLLAMA, first.name
        info = active()
        assert info["instantiated"] is True and info["backend"] == BACKEND_OLLAMA, info
        # Selection agrees with what was built, so nothing is stale.
        assert info["stale"] is None, info

        # Now make selection disagree with the instance already in force.
        # active() must keep reporting the BUILT one (that is the one
        # answering chat calls) and surface the newly-preferred one as
        # stale, so a caller can offer a restart instead of displaying a
        # choice that is not actually in effect.
        os.environ[ENV_BACKEND] = BACKEND_LLAMA
        info = active()
        assert info["backend"] == BACKEND_OLLAMA, ("active() must report the "
                                                   "instantiated backend", info)
        assert info["stale"] == BACKEND_LLAMA, info
        os.environ[ENV_BACKEND] = BACKEND_OLLAMA

        reset()
        assert active()["instantiated"] is False
        assert active()["stale"] is None

        # One instance PER NAME, not one instance overall: per-model
        # selection means a single process can legitimately need both, and
        # the invariant that actually matters is that the llama backend --
        # which owns a GPU-resident subprocess -- is never duplicated.
        os.environ.pop(ENV_BACKEND, None)
        reset()
        a1 = get_backend(model=r"C:\models\a.gguf")
        a2 = get_backend(model="/srv/b.gguf")
        b1 = get_backend(model="qwen2.5-coder:latest")
        b2 = get_backend(model="llama3.2:3b")
        assert a1 is a2 and a1.name == BACKEND_LLAMA, (a1, a2)
        assert b1 is b2 and b1.name == BACKEND_OLLAMA, (b1, b2)
        assert a1 is not b1
        with _ACTIVE_LOCK:
            assert set(_INSTANCES) == {BACKEND_LLAMA, BACKEND_OLLAMA}, _INSTANCES
            assert _ACTIVE is b2, "active must be the one handed out most recently"
        reset()
        with _ACTIVE_LOCK:
            assert not _INSTANCES and _ACTIVE is None
    finally:
        if _old_env is None:
            os.environ.pop(ENV_BACKEND, None)
        else:
            os.environ[ENV_BACKEND] = _old_env
        reset()

    # -- nvidia-smi parsing ------------------------------------------------
    _real_which = shutil.which
    _real_run = subprocess.run
    try:
        shutil.which = lambda name: None
        assert _compute_apps() is None, "no nvidia-smi must read as unknown"

        shutil.which = lambda name: "/usr/bin/nvidia-smi"

        class _P:
            def __init__(self, rc, out):
                self.returncode, self.stdout, self.stderr = rc, out, ""

        # The real shape, transcribed from a live NVIDIA host. Note the
        # process name: Ollama's own runner IS called llama-server.
        subprocess.run = lambda *a, **k: _P(0, (
            "4054178, /nix/store/x-ollama-0.30.7/lib/ollama/llama-server, 4636\n"
            "1234, /opt/hearth/llama/llama-server, 100\n"))
        apps = _compute_apps()
        assert len(apps) == 2, apps
        assert apps[0] == {"pid": 4054178,
                           "name": "/nix/store/x-ollama-0.30.7/lib/ollama/llama-server",
                           "bytes": 4636 * 1024 * 1024}, apps[0]
        assert apps[1]["bytes"] == 100 * 1024 * 1024, apps[1]
        # A path containing a comma still parses: the pid is split from the
        # left and the memory from the right.
        subprocess.run = lambda *a, **k: _P(0, "7, /opt/a,b/llama-server, 8\n")
        assert _compute_apps() == [{"pid": 7, "name": "/opt/a,b/llama-server",
                                    "bytes": 8 * 1024 * 1024}], _compute_apps()
        # No compute processes is an empty list, which is a real answer and
        # must not be confused with the None that means "could not ask".
        subprocess.run = lambda *a, **k: _P(0, "\n")
        assert _compute_apps() == [], "no compute apps is [], not None"
        # Junk lines are skipped, not fatal.
        subprocess.run = lambda *a, **k: _P(0, "not a row\n7, x, 8\n[N/A], y, 5\n")
        assert _compute_apps() == [{"pid": 7, "name": "x", "bytes": 8 * 1024 * 1024}]
        subprocess.run = lambda *a, **k: _P(1, "")
        assert _compute_apps() is None, "a nonzero exit must read as unknown"

        def _raise(*a, **k):
            raise OSError("boom")
        subprocess.run = _raise
        assert _compute_apps() is None, "an OSError must read as unknown"
    finally:
        shutil.which = _real_which
        subprocess.run = _real_run

    # -- LlamaBackend.own_vram_bytes: the three distinct answers -----------
    #
    # This is the distinction hearth_idle depends on. Getting it wrong is
    # what made that module read its own resting model as contention and
    # report a permanently busy machine.
    class _FakeProc:
        def __init__(self, pid, rc=None):
            self.pid, self._rc = pid, rc

        def poll(self):
            return self._rc

    class _FakeServer:
        def __init__(self, pid, rc=None):
            self.proc = _FakeProc(pid, rc)

        def stop(self):
            self.proc = None

    lb2 = LlamaBackend(server_path="/opt/hearth/llama/llama-server")
    _real_compute = globals()["_compute_apps"]
    _real_same = globals()["_same_file"]
    try:
        # Compare paths as plain strings here: the fixture paths below do
        # not exist on the machine running this test, and the question
        # being tested is the matching POLICY, not realpath's behaviour.
        globals()["_same_file"] = lambda a, b: bool(a) and bool(b) and a == b

        # Nothing of ours anywhere on the card: a CONFIDENT zero, not
        # "unknown". Without this, hearth_idle would fall back to the
        # unsubtracted total on an ordinary machine with no model loaded.
        globals()["_compute_apps"] = lambda: []
        assert lb2.own_vram_bytes() == (0, True), lb2.own_vram_bytes()

        # THE TRAP, verified live: Ollama's own runner is also called
        # llama-server. It is a DIFFERENT binary at a different path and it
        # is somebody else's workload, so it must NOT be counted as ours.
        # A name-based match would return 4 GiB here and tell hearth_idle
        # the card is free when it is not.
        ollama_row = {"pid": 4054178, "bytes": 4 * 1024 ** 3,
                      "name": "/nix/store/x-ollama-0.30.7/lib/ollama/llama-server"}
        globals()["_compute_apps"] = lambda: [ollama_row]
        assert lb2.own_vram_bytes() == (0, True), (
            "Ollama's embedded llama-server must not count as Hearth's own",
            lb2.own_vram_bytes())

        # Our own server, matched by PID: exact.
        lb2._server = _FakeServer(999)
        lb2._ref = ModelRef.gguf("x.gguf")
        globals()["_compute_apps"] = lambda: [
            ollama_row, {"pid": 999, "name": "/anything", "bytes": 5 * 1024 ** 3}]
        assert lb2.own_vram_bytes() == (5 * 1024 ** 3, True), lb2.own_vram_bytes()

        # Running, but nvidia-smi cannot be asked: unknown, not zero.
        globals()["_compute_apps"] = lambda: None
        assert lb2.own_vram_bytes() == (None, False)
        # Running, but our PID is not listed (a CPU-only build, or a GPU we
        # cannot see): unknown, because those cannot be told apart here.
        globals()["_compute_apps"] = lambda: [{"pid": 111, "name": "z", "bytes": 1}]
        assert lb2.own_vram_bytes() == (None, False)

        # A server that has exited is not ours any more.
        lb2._server = _FakeServer(999, rc=0)
        globals()["_compute_apps"] = lambda: [
            {"pid": 999, "name": "/anything", "bytes": 5 * 1024 ** 3}]
        assert lb2.own_vram_bytes() == (0, True), lb2.own_vram_bytes()
        # ...and with no server here at all, nvidia-smi unavailable is
        # still a confident zero: this process holds nothing.
        lb2._server = None
        globals()["_compute_apps"] = lambda: None
        assert lb2.own_vram_bytes() == (0, True)

        # ANOTHER Hearth process holding a model IS ours, matched by the
        # resolved binary path. This is the cross-process case: a sidecar
        # holds the model while some other Hearth process asks the idle
        # question, and reading that as contention is precisely the defect
        # that made hearth_idle report a permanently busy machine.
        globals()["_compute_apps"] = lambda: [
            ollama_row,
            {"pid": 555, "name": "/opt/hearth/llama/llama-server", "bytes": 3 * 1024 ** 3}]
        assert lb2.own_vram_bytes() == (3 * 1024 ** 3, True), lb2.own_vram_bytes()
    finally:
        globals()["_compute_apps"] = _real_compute
        globals()["_same_file"] = _real_same
        lb2._server = None
        lb2._ref = None

    # _same_file itself, against real files rather than the stub above.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:
        _a = os.path.join(_td, "a")
        _b = os.path.join(_td, "b")
        for _p in (_a, _b):
            with open(_p, "w", encoding="utf-8") as _fh:
                _fh.write("x")
        assert _same_file(_a, _a) is True
        assert _same_file(_a, _b) is False
        # A path that does not exist is not the same file as anything, and
        # is answered rather than raised.
        assert _same_file(_a, os.path.join(_td, "nope")) is False
        assert _same_file(None, _a) is False and _same_file(_a, "") is False

    # -- OllamaBackend.own_vram_bytes: Ollama's own accounting -------------
    _real_http = globals()["_http_json"]
    try:
        globals()["_http_json"] = lambda url, timeout, body=None: {
            "models": [{"size_vram": 4748056984}, {"size_vram": 100}]}
        assert ob.own_vram_bytes() == (4748056984 + 100, True), ob.own_vram_bytes()
        # Nothing loaded is a confident zero.
        globals()["_http_json"] = lambda url, timeout, body=None: {"models": []}
        assert ob.own_vram_bytes() == (0, True)
        # Unreachable is UNKNOWN, not zero: an Ollama mid-restart may still
        # hold a model.
        def _unreachable(url, timeout, body=None):
            raise urllib.error.URLError("refused")
        globals()["_http_json"] = _unreachable
        assert ob.own_vram_bytes() == (None, False)
        # A shape we do not recognise is unknown too.
        globals()["_http_json"] = lambda url, timeout, body=None: {"models": "nope"}
        assert ob.own_vram_bytes() == (None, False)
    finally:
        globals()["_http_json"] = _real_http

    # -- OllamaBackend.chat: the real body, stubbed transport --------------
    seen = {}

    def _fake_chat_http(url, timeout, body=None):
        seen["url"], seen["body"] = url, body
        return {"message": {"role": "assistant", "content": "hi",
                            "tool_calls": [{"function": {"name": "t", "arguments": {}}}]},
                "prompt_eval_count": 21, "eval_count": 9, "model": "m:latest"}

    globals()["_http_json"] = _fake_chat_http
    try:
        got = ob.chat("m:latest", [{"role": "user", "content": "x"}], tools=[],
                      options={"num_ctx": 8192})
        assert seen["url"].endswith("/api/chat"), seen["url"]
        assert seen["body"]["model"] == "m:latest", seen["body"]
        assert seen["body"]["stream"] is False, seen["body"]
        assert seen["body"]["options"] == {"num_ctx": 8192}, seen["body"]
        assert got["tokens_in"] == 21 and got["tokens_out"] == 9, got
        assert got["backend"] == BACKEND_OLLAMA, got
        # The Ollama message shape passes through untouched: it is already
        # what hearth_loop reads.
        assert got["message"]["tool_calls"][0]["function"]["name"] == "t", got

        # Passing on_token does NOT take this branch at all: it switches the
        # request to "stream": true and leaves _http_json unused, which is
        # why the fake above is never consulted for it. The streamed branch
        # is proven in full in the Streaming section earlier in this test.
        assert seen["body"]["stream"] is False, seen["body"]

        # An unreachable daemon is a BackendError, not a raw URLError.
        globals()["_http_json"] = _unreachable
        try:
            ob.chat("m", [])
            raise AssertionError("an unreachable Ollama must raise BackendError")
        except BackendError as exc:
            assert "could not reach Ollama" in str(exc), str(exc)
    finally:
        globals()["_http_json"] = _real_http

    # The streamed branch degrades the same way, through its own transport.
    _real_urlopen2 = urllib.request.urlopen
    try:
        def _refuse(req, timeout=None):
            raise urllib.error.URLError("refused")
        urllib.request.urlopen = _refuse
        try:
            ob.chat("m", [], on_token=lambda _t: None)
            raise AssertionError("an unreachable Ollama must raise BackendError "
                                 "on the streaming branch too")
        except BackendError as exc:
            assert "could not reach Ollama" in str(exc), str(exc)
    finally:
        urllib.request.urlopen = _real_urlopen2

    # -- OllamaBackend.available_models / diagnose -------------------------
    try:
        globals()["_http_json"] = lambda url, timeout, body=None: (
            {"version": "0.5.1"} if url.endswith("/api/version") else
            {"models": [{"name": "a:latest"}, {"model": "b:7b"}, {"junk": 1}]})
        refs = ob.available_models()
        assert [r.as_text() for r in refs] == ["ollama:a:latest", "ollama:b:7b"], refs
        assert all(r.kind == KIND_OLLAMA for r in refs)
        d = ob.diagnose()
        assert d["ok"] is True and d["status"] == "ready", d
        assert "0.5.1" in d["message"], d

        # Running but empty is a distinct, actionable status.
        globals()["_http_json"] = lambda url, timeout, body=None: (
            {"version": "0.5.1"} if url.endswith("/api/version") else {"models": []})
        d = ob.diagnose()
        assert d["ok"] is False and d["status"] == "no_models", d
        assert "pull" in d["remedy"], d

        globals()["_http_json"] = _unreachable
        assert ob.available_models() == []
        d = ob.diagnose()
        assert d["ok"] is False and d["status"] == "not_running", d
        assert d["remedy"], d
    finally:
        globals()["_http_json"] = _real_http

    # -- LlamaBackend.diagnose: a missing binary is a BROKEN INSTALL, ------
    # -- never "go and start the daemon" -----------------------------------
    _real_find = hearth_llama.find_server
    _real_probe = hearth_llama.probe_binary
    try:
        hearth_llama.find_server = lambda env=None: {
            "found": False, "path": None, "reason": "no llama-server executable found",
            "searched": ["/a", "/b"], "source": "missing"}
        d = lb.diagnose()
        assert d["ok"] is False and d["status"] == "engine_missing", d
        assert "incomplete" in d["remedy"] or "Reinstall" in d["remedy"], d
        # The whole point of the cutover: never tell a user to start a
        # daemon, because for llama.cpp there is no daemon to start. The
        # remedy is about the install, not about a service.
        low = (d["message"] + " " + d["remedy"]).lower()
        assert "ollama" not in low, d
        for daemon_word in ("not running", "ollama serve", "start it", "systemctl"):
            assert daemon_word not in low, (daemon_word, d)

        hearth_llama.find_server = lambda env=None: {
            "found": True, "path": "/x/llama-server", "reason": "bundled",
            "searched": [], "source": "bundled"}
        hearth_llama.probe_binary = lambda path=None, timeout=None: {
            "ok": False, "path": path, "build": None, "commit": None,
            "backend": "unknown", "gpu_offload": False, "devices": [],
            "error": "libcuda.so.1: cannot open shared object file"}
        d = lb.diagnose()
        assert d["ok"] is False and d["status"] == "engine_broken", d
        assert "libcuda" in d["message"], d

        # Present and runnable, but nothing downloaded.
        hearth_llama.probe_binary = lambda path=None, timeout=None: {
            "ok": True, "path": path, "build": 9608, "commit": "70b54e1",
            "backend": "cuda", "gpu_offload": True, "devices": [], "error": None}
        lb.available_models = lambda: []
        d = lb.diagnose()
        assert d["ok"] is False and d["status"] == "no_models", d
        assert "9608" in d["message"], d

        # Ready, GPU build.
        lb.available_models = lambda: [ModelRef.gguf("/m/a.gguf")]
        d = lb.diagnose()
        assert d["ok"] is True and d["status"] == "ready", d
        assert "9608" in d["message"] and "cuda" in d["message"], d
        assert "slow" not in d["message"], d

        # Ready, but a CPU-only build: say so before the user waits five
        # minutes for a generation.
        hearth_llama.probe_binary = lambda path=None, timeout=None: {
            "ok": True, "path": path, "build": 9608, "commit": "70b54e1",
            "backend": "cpu", "gpu_offload": False, "devices": [], "error": None}
        d = lb.diagnose()
        assert d["ok"] is True and "CPU" in d["message"] and "slow" in d["message"], d
    finally:
        hearth_llama.find_server = _real_find
        hearth_llama.probe_binary = _real_probe
        lb.__dict__.pop("available_models", None)

    # -- LlamaBackend.available_models: real filesystem, temp store --------
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "repo"), exist_ok=True)
        for fn in ("plain.gguf", "half.gguf.part", "notes.txt",
                   "big-00001-of-00003.gguf", "big-00002-of-00003.gguf",
                   "big-00003-of-00003.gguf"):
            with open(os.path.join(td, "repo", fn), "w", encoding="utf-8") as fh:
                fh.write("x")
        _real_store = globals()["_model_store_dir"]
        try:
            globals()["_model_store_dir"] = lambda: td
            names = sorted(r.display for r in lb.available_models())
            # A partial download is not a model; a .txt is not a model; and
            # a split model is listed once, by the part llama-server is
            # actually handed.
            assert names == ["big-00001-of-00003.gguf", "plain.gguf"], names
            assert all(r.kind == KIND_GGUF for r in lb.available_models())
            # A store that does not exist yet is [], not an exception.
            globals()["_model_store_dir"] = lambda: os.path.join(td, "nope")
            assert lb.available_models() == []
            globals()["_model_store_dir"] = lambda: None
            assert lb.available_models() == []
        finally:
            globals()["_model_store_dir"] = _real_store

    # -- LlamaBackend.chat: options translation, stubbed server ------------
    captured = {}

    class _ChatServer:
        proc = _FakeProc(4242)

        def chat(self, messages, on_token=None, tools=None, timeout=None, **params):
            captured["messages"] = messages
            captured["tools"] = tools
            captured["params"] = params
            if on_token:
                on_token("hi")
            return {"content": "hi", "tool_calls": [], "tokens_in": 7,
                    "tokens_out": 3, "model": "loaded-alias"}

    lb3 = LlamaBackend()
    lb3._server = _ChatServer()
    lb3._ref = ModelRef.gguf("/m/a.gguf")
    got = lb3.chat("/m/a.gguf", [{"role": "user", "content": "x"}], tools=["t"],
                   options={"num_predict": 64, "num_ctx": 8192, "temperature": 0})
    assert got["tokens_in"] == 7 and got["tokens_out"] == 3, got
    assert got["backend"] == BACKEND_LLAMA, got
    assert got["message"] == {"role": "assistant", "content": "hi"}, got
    assert captured["tools"] == ["t"], captured
    # num_predict becomes max_tokens...
    assert captured["params"]["max_tokens"] == 64, captured
    assert captured["params"]["temperature"] == 0, captured
    # ...but num_ctx is NOT forwarded, because llama.cpp fixes the context
    # at spawn time with -c and a per-request value would silently do
    # nothing. Dropping it is the honest behaviour, and this pins it.
    assert "num_ctx" not in captured["params"], captured
    # A hearth_llama failure surfaces as a BackendError, so callers need
    # only know this module's exception type.
    class _DeadServer:
        proc = _FakeProc(1)

        def chat(self, *a, **k):
            raise hearth_llama.LlamaError("llama-server died mid-request")

    lb3._server = _DeadServer()
    try:
        lb3.chat("/m/a.gguf", [])
        raise AssertionError("a LlamaError must surface as a BackendError")
    except BackendError as exc:
        assert "died mid-request" in str(exc), str(exc)
    # A wrong-namespace ref is refused before any server work happens.
    try:
        lb3.chat(ModelRef.ollama("qwen:latest"), [])
        raise AssertionError("llama backend must refuse an ollama ref")
    except ModelKindError:
        pass

    # -- measure(): a bad ref is an error dict, never an exception ---------
    m = lb.measure(ModelRef.ollama("qwen:latest"))
    assert m["ok"] is False and m["error"], m
    assert m["backend"] == BACKEND_LLAMA, m
    m = ob.measure(ModelRef.gguf("/m/a.gguf"))
    assert m["ok"] is False and m["error"], m

    # -- measure() on the llama path, stubbed server -----------------------
    lb4 = LlamaBackend()
    lb4._server = _ChatServer()
    lb4._ref = ModelRef.gguf("/m/a.gguf")
    # A deterministic clock, so the rate is an exact expected number rather
    # than whatever a stubbed generation happens to take. Without this the
    # stub returns inside one clock tick on Windows and wall_seconds is
    # 0.0, which is a real (and correctly handled) case but not the one
    # being pinned here.
    _real_monotonic = time.monotonic
    _ticks = iter([100.0, 102.0, 104.0, 106.0, 108.0, 110.0])
    try:
        time.monotonic = lambda: next(_ticks)
        m = lb4.measure("/m/a.gguf", num_predict=8)
    finally:
        time.monotonic = _real_monotonic
    assert m["ok"] is True, m
    assert m["tokens_generated"] == 3 and m["prompt_tokens"] == 7, m
    # A warm server reports no load time, rather than a misleading zero.
    assert m["load_seconds"] is None, m
    # Always wall clock on this path, and it says so: hearth_llama discards
    # llama-server's own timings block, so there is no server-reported rate
    # to prefer and none is invented.
    assert m["tokens_per_second_source"] == "wall_clock", m
    assert m["tokens_per_second"] == 3 / 2.0, m

    # A generation that completes inside one clock tick reports no rate at
    # all rather than dividing by zero or claiming an infinite one.
    lb5 = LlamaBackend()
    lb5._server = _ChatServer()
    lb5._ref = ModelRef.gguf("/m/a.gguf")
    try:
        time.monotonic = lambda: 500.0
        m0 = lb5.measure("/m/a.gguf", num_predict=8)
    finally:
        time.monotonic = _real_monotonic
    assert m0["ok"] is True and m0["wall_seconds"] == 0.0, m0
    assert m0["tokens_per_second"] is None, m0
    assert m0["tokens_per_second_source"] is None, m0

    # -- close() is idempotent and safe on a backend that never started ----
    LlamaBackend().close()
    lb4.close()
    lb4.close()
    assert lb4.server is None

    if live:
        _live_test()


def _live_test():
    """Exercises that need a real engine. Only run with --live."""
    decision = select()
    sys.stderr.write("[live] selected backend: {} ({})\n".format(
        decision["backend"], decision["reason"]))
    backend = build(decision["backend"])
    try:
        d = backend.diagnose()
        sys.stderr.write("[live] diagnose: {} {}\n".format(d["status"], d["message"]))
        models = backend.available_models()
        sys.stderr.write("[live] {} model(s) available\n".format(len(models)))
        if not models:
            sys.stderr.write("[live] no models; skipping the chat exercise\n")
            return
        ref = models[0]
        got = backend.chat(ref, [{"role": "user", "content": "Say hello in three words."}],
                           options={"num_predict": 24, "temperature": 0})
        assert got["message"]["content"], got
        assert got["tokens_out"] > 0, ("usage counts must be real", got)
        assert got["backend"] == backend.name, got
        sys.stderr.write("[live] chat ok: {!r} ({} in / {} out)\n".format(
            got["message"]["content"][:60], got["tokens_in"], got["tokens_out"]))
        own, known = backend.own_vram_bytes()
        sys.stderr.write("[live] own vram: {} bytes (known={})\n".format(own, known))
        m = backend.measure(ref, num_predict=32)
        assert m["ok"], m
        sys.stderr.write("[live] measure: {:.1f} tok/s ({})\n".format(
            m["tokens_per_second"] or 0.0, m["tokens_per_second_source"]))
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
