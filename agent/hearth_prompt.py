#!/usr/bin/env python3
"""hearth prompt: versioned prompt templates with strict variable checks.

System prompts used to live as string literals scattered across the agent
modules. That had two costs that only showed up after the fact. Nobody
could say which prompt text a finished run actually used, because the text
was welded to whatever revision of the Python happened to be installed.
And nobody could adjust an instruction without editing code, which meant
prompt changes travelled through code review as if they were logic changes.

A prompt is configuration. This module treats it that way: templates live
as `.md` files in a directory, `Store` reads them, and every template has a
short content fingerprint so a run can record exactly which prompt text it
was given. Two runs that report the same fingerprint were driven by byte
identical instructions, whatever the code around them looked like.

The variable checking is deliberately strict in both directions, because
both directions are bugs:

  missing   the template asks for a variable the caller did not supply.
            Loose templating would leave the placeholder sitting in the
            prompt as literal text, or substitute an empty string, and the
            model would silently receive a broken or weakened instruction.
  unused    the caller supplied a variable the template never mentions.
            The caller believes it configured something. It did not. A
            renamed placeholder or a typo in a key looks exactly like this,
            and staying quiet means shipping a prompt that ignores an
            input someone went to the trouble of computing.

So `render` raises on either, and it validates the whole template before it
substitutes anything. Rendering is all or nothing: there is no code path
that returns half substituted text. A caller that gets a string back can
rely on it containing no unresolved placeholders.

What this module deliberately does NOT do:

  No expression language. One placeholder form, a doubled brace pair
  holding a bare variable name. No filters, no conditionals, no loops, no
  dotted attribute lookups, no arithmetic. A prompt store that can compute
  is a second program nobody is reviewing, and the moment a template can
  branch the fingerprint stops describing what the model was told.
  No third-party template engine. Standard library only, so the prompt
  path has no dependency that can change rendering behaviour under it.
  No inheritance, includes or partials. A template file is the whole
  prompt, which is what makes its hash meaningful.
  No caching and no file watching. `load` reads the file every call, so an
  edited template takes effect on the next run with no reload protocol.
  No network, no clock, no randomness. Rendering the same template with
  the same variables yields the same text forever.
  No defaults for variables. A default is a silent instruction, and the
  whole point here is that silence is the failure mode being removed.
  No value escaping or sanitising. Values go in verbatim; this module is
  not a security boundary between a caller and its own prompt.
"""

import argparse
import hashlib
import io
import os
import re
import sys
import tempfile

EXT = ".md"
OPEN = "{{"
CLOSE = "}}"
FINGERPRINT_LEN = 12
# A placeholder name is a plain Python style identifier. Nothing exotic is
# accepted, so a template cannot grow a lookup path by accident.
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A template name is a file stem. No dots, no separators, no leading dash,
# which keeps a caller supplied name from reaching outside the store
# directory or naming something that is not a template.
STORE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
DEFAULT_DIR_ENV = "HEARTH_PROMPT_DIR"


class PromptError(Exception):
    """Base for every error this module raises."""


class TemplateError(PromptError):
    """The template text itself is malformed."""


class VariableError(PromptError):
    """The supplied variables do not match what the template needs."""


class MissingVariableError(VariableError):
    """The template needs a variable the caller did not supply."""


class UnusedVariableError(VariableError):
    """The caller supplied a variable the template does not use."""


class UnknownTemplateError(PromptError):
    """No template by that name in the store."""


class EmptyTemplateError(PromptError):
    """The template file exists but holds no prompt text."""


def default_dir():
    """Where templates live when the caller names no directory: the
    HEARTH_PROMPT_DIR override, else a `prompts` directory beside the
    agent package. Resolved per call so a test can set the env var."""
    override = os.environ.get(DEFAULT_DIR_ENV, "")
    if override:
        return os.path.abspath(override)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "prompts")


def _parse(template):
    """Split a template into segments: ("text", s) and ("var", name).

    A single brace is ordinary text, so a template may contain JSON, a
    shell snippet or prose about braces without quoting anything. Only a
    doubled brace opens a placeholder, and once opened it must close: an
    unterminated one is a typo that would otherwise ship a placeholder to
    the model as literal text, so it raises rather than passing through.
    Inner whitespace is not significant, so the tight and the spaced
    spelling of a name are the same variable."""
    if not isinstance(template, str):
        raise TemplateError("template must be str, got {}".format(
            type(template).__name__))
    segments = []
    pos = 0
    while True:
        start = template.find(OPEN, pos)
        if start < 0:
            segments.append(("text", template[pos:]))
            return segments
        segments.append(("text", template[pos:start]))
        end = template.find(CLOSE, start + len(OPEN))
        if end < 0:
            raise TemplateError(
                "unterminated placeholder at offset {}: opened but never "
                "closed".format(start))
        name = template[start + len(OPEN):end].strip()
        if not name:
            raise TemplateError(
                "empty placeholder name at offset {}".format(start))
        if not NAME_RE.match(name):
            raise TemplateError(
                "illegal placeholder name {!r} at offset {}: names must be "
                "letters, digits and underscores, not starting with a "
                "digit".format(name, start))
        segments.append(("var", name))
        pos = end + len(CLOSE)


def variables(template):
    """The set of variable names this template needs. Raises TemplateError
    if the template is malformed, so a caller can validate a template on
    its own before it has any values to hand."""
    return set(name for kind, name in _parse(template) if kind == "var")


def render(template, variables=None):
    """Substitute `variables` into `template` and return the result.

    Raises MissingVariableError when the template needs a name the caller
    did not supply, and UnusedVariableError when the caller supplied a
    name the template does not use. See the module docstring for why both
    are errors rather than warnings. When both are true it reports the
    missing ones, since a lost instruction is the worse of the two.

    Validation happens over the whole template before any text is built,
    so a failed render produces no output at all: there is no partially
    substituted string to leak into a prompt or a log.

    A value of None raises, because None is what an unset variable looks
    like and accepting it would reintroduce the silent empty substitution
    this module exists to prevent. Any other value is rendered with str().
    """
    segments = _parse(template)
    needed = set(name for kind, name in segments if kind == "var")
    supplied = dict(variables or {})
    missing = sorted(needed - set(supplied))
    if missing:
        raise MissingVariableError(
            "template needs variable(s) not supplied: {}".format(
                ", ".join(missing)))
    unused = sorted(set(supplied) - needed)
    if unused:
        raise UnusedVariableError(
            "variable(s) supplied but not used by the template: {}".format(
                ", ".join(unused)))
    values = {}
    for name in sorted(needed):
        value = supplied[name]
        if value is None:
            raise MissingVariableError(
                "variable {!r} was supplied as None".format(name))
        values[name] = value if isinstance(value, str) else str(value)
    out = []
    for kind, item in segments:
        out.append(item if kind == "text" else values[item])
    return "".join(out)


def fingerprint(template):
    """A short content hash of the template text, for a run to record so
    the exact instructions it used stay identifiable later.

    Line endings are normalised first, so the same template checked out
    with CRLF and with LF fingerprints the same. Everything else counts:
    whitespace and wording changes all move the hash, which is the point.
    Truncated for readability in logs; this identifies text, it does not
    authenticate it."""
    if not isinstance(template, str):
        raise TemplateError("template must be str, got {}".format(
            type(template).__name__))
    norm = template.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LEN]


class Store:
    """A directory of `.md` prompt templates, addressed by file stem.

    Nothing is cached: `load` reads from disk every call, so editing a
    template file is the whole deployment story for a prompt change."""

    def __init__(self, directory=None):
        self.directory = os.path.abspath(directory or default_dir())

    def path(self, name):
        """The file a template name maps to. Raises UnknownTemplateError
        for a name that is not a bare stem, so a traversal attempt or a
        stray path separator fails here rather than opening a file the
        store does not own."""
        if not isinstance(name, str) or not STORE_NAME_RE.match(name):
            raise UnknownTemplateError(
                "illegal template name {!r}: expected a bare name of "
                "letters, digits, underscore or dash".format(name))
        return os.path.join(self.directory, name + EXT)

    def names(self):
        """Every template in the store, sorted. An absent or unreadable
        directory is an empty store, not an error: a deployment with no
        prompts yet should report that it has none."""
        try:
            entries = os.listdir(self.directory)
        except OSError:
            return []
        found = []
        for entry in entries:
            if not entry.endswith(EXT):
                continue
            stem = entry[:-len(EXT)]
            if STORE_NAME_RE.match(stem):
                found.append(stem)
        return sorted(found)

    def load(self, name):
        """The raw template text. Raises UnknownTemplateError when there
        is no such template and EmptyTemplateError when the file holds
        nothing but whitespace, because an empty prompt is a truncated
        file or a bad merge, never an instruction anyone meant to give."""
        target = self.path(name)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            raise UnknownTemplateError(
                "no template {!r} in {}".format(name, self.directory))
        if not text.strip():
            raise EmptyTemplateError(
                "template {!r} is empty: {}".format(name, target))
        return text

    def variables(self, name):
        """The set of variables the named template needs."""
        return variables(self.load(name))

    def fingerprint(self, name):
        """The content hash of the named template, for run records."""
        return fingerprint(self.load(name))

    def render(self, name, variables=None):
        """Load the named template and render it, same rules as render()."""
        return render(self.load(name), variables)


def _parse_var(pair):
    """Turn a `k=v` CLI argument into a (name, value) tuple. The value may
    contain `=`; only the first one separates."""
    if not isinstance(pair, str) or "=" not in pair:
        raise VariableError(
            "bad --var {!r}: expected name=value".format(pair))
    name, _, value = pair.partition("=")
    name = name.strip()
    if not NAME_RE.match(name):
        raise VariableError("bad --var name {!r}".format(name))
    return name, value


def _self_test():
    saved = os.environ.pop(DEFAULT_DIR_ENV, None)
    try:
        # parsing: the two spellings are one variable, and a lone brace is
        # ordinary text that needs no quoting
        tpl = "a {{name}} b {{ name }} c {{   other   }}"
        assert variables(tpl) == {"name", "other"}, variables(tpl)
        assert variables("no vars here") == set()
        literal = 'use {"a": 1} and { x } and a } alone'
        assert variables(literal) == set(), variables(literal)
        assert render(literal, {}) == literal

        # rendering substitutes every occurrence, whitespace and all
        got = render(tpl, {"name": "N", "other": "O"})
        assert got == "a N b N c O", got

        # a value is used verbatim, including braces, and is never rescanned
        got = render("x {{ v }} y", {"v": "{{ name }}"})
        assert got == "x {{ name }} y", got

        # missing: the template would have lost an instruction
        try:
            render(tpl, {"name": "N"})
            raise AssertionError("missing variable did not raise")
        except MissingVariableError as exc:
            assert "other" in str(exc), exc

        # unused: the caller thinks it configured something it did not
        try:
            render("{{ a }}", {"a": "1", "b": "2", "c": "3"})
            raise AssertionError("unused variable did not raise")
        except UnusedVariableError as exc:
            assert "b, c" in str(exc), exc

        # both wrong at once reports the missing one
        try:
            render("{{ a }}", {"zz": "1"})
            raise AssertionError("mismatch did not raise")
        except MissingVariableError:
            pass

        # None is an unset variable wearing a value's clothes
        try:
            render("{{ a }}", {"a": None})
            raise AssertionError("None value did not raise")
        except MissingVariableError:
            pass

        # a non-string value is rendered with str()
        assert render("{{ n }}", {"n": 7}) == "7"

        # all or nothing: a template whose later variable is missing
        # produces no text at all, not a prefix
        half = "KEEP {{ first }} MID {{ second }} TAIL"
        result = "sentinel"
        try:
            result = render(half, {"first": "1"})
            raise AssertionError("partial render did not raise")
        except MissingVariableError:
            pass
        assert result == "sentinel", result

        # malformed templates
        for bad in ("open {{ name",
                    "{{ }}",
                    "{{}}",
                    "{{ 9lives }}",
                    "{{ a-b }}",
                    "{{ a.b }}",
                    "{{ a b }}",
                    "{{ a+1 }}"):
            try:
                variables(bad)
                raise AssertionError("accepted bad template: " + bad)
            except TemplateError:
                pass
        for bad in (None, 7, b"bytes"):
            try:
                render(bad, {})
                raise AssertionError("non-str template did not raise")
            except TemplateError:
                pass

        # fingerprints: stable, short, sensitive to text, blind to CRLF
        text = "line one\nline two\n"
        fp = fingerprint(text)
        assert len(fp) == FINGERPRINT_LEN, fp
        assert fp == fingerprint(text), "not stable"
        assert fp == fingerprint("line one\r\nline two\r\n"), "CRLF differs"
        assert fp != fingerprint("line one\nline  two\n"), "whitespace blind"
        assert fp != fingerprint("line one\nline three\n"), "wording blind"
        assert fingerprint(text) == hashlib.sha256(
            text.encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            assert store.names() == [], store.names()
            assert Store(os.path.join(root, "gone")).names() == []

            def write(filename, body):
                target = os.path.join(root, filename)
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(body)

            write("review" + EXT, "Review {{ target }} for {{ goal }}.\n")
            write("summarise" + EXT, "Summarise this:\n\n{{ body }}\n")
            write("blank" + EXT, "   \n\n")
            write("fixed" + EXT, "No variables at all.\n")
            write("notes.txt", "not a template")  # wrong suffix, ignored

            assert store.names() == ["blank", "fixed", "review",
                                     "summarise"], store.names()
            assert store.variables("review") == {"target", "goal"}
            assert store.variables("fixed") == set()
            out = store.render("review", {"target": "x.py", "goal": "bugs"})
            assert out == "Review x.py for bugs.\n", out
            assert store.render("fixed", {}) == "No variables at all.\n"
            assert store.render("fixed") == "No variables at all.\n"

            # the store hash matches hashing the loaded text, and tracks an
            # edit to the file
            body = store.load("summarise")
            assert store.fingerprint("summarise") == fingerprint(body)
            before = store.fingerprint("review")
            write("review" + EXT,
                  "Review {{ target }} closely for {{ goal }}.\n")
            assert store.fingerprint("review") != before, "edit not seen"

            # unknown template
            try:
                store.load("absent")
                raise AssertionError("unknown template did not raise")
            except UnknownTemplateError:
                pass

            # an empty file is a truncated prompt, not an empty instruction
            try:
                store.load("blank")
                raise AssertionError("empty template did not raise")
            except EmptyTemplateError:
                pass

            # illegal names never reach the filesystem
            for bad in ("../secret", "a/b", "a\\b", "", "-x", ".hidden",
                        "a.b", "a b", None, 7):
                try:
                    store.path(bad)
                    raise AssertionError(
                        "accepted bad name: {!r}".format(bad))
                except UnknownTemplateError:
                    pass

            # CLI surface. stderr is captured as well as stdout, so the
            # deliberate failures below stay out of the test's own output.
            def run(args):
                buf = io.StringIO()
                err = io.StringIO()
                real_out, real_err = sys.stdout, sys.stderr
                sys.stdout, sys.stderr = buf, err
                try:
                    code = main(args)
                finally:
                    sys.stdout, sys.stderr = real_out, real_err
                return code, buf.getvalue()

            code, shown = run(["--dir", root, "--list"])
            assert code == 0, code
            assert shown.split() == ["blank", "fixed", "review",
                                     "summarise"], shown
            code, shown = run(["--dir", root, "--show", "fixed"])
            assert code == 0 and shown == "No variables at all.\n", shown
            code, shown = run(["--dir", root, "--render", "summarise",
                               "--var", "body=hello=world"])
            assert code == 0, code
            assert shown == "Summarise this:\n\nhello=world\n", shown
            code, shown = run(["--dir", root, "--fingerprint", "summarise"])
            assert code == 0, code
            assert shown.strip() == fingerprint(body), shown
            # the CLI reports a mismatch instead of printing a prompt
            code, shown = run(["--dir", root, "--render", "summarise"])
            assert code == 1 and shown == "", shown
            code, shown = run(["--dir", root, "--render", "summarise",
                               "--var", "body=x", "--var", "extra=y"])
            assert code == 1 and shown == "", shown
            code, shown = run(["--dir", root, "--render", "summarise",
                               "--var", "nonsense"])
            assert code == 1 and shown == "", shown
            code, shown = run(["--dir", root, "--show", "absent"])
            assert code == 1 and shown == "", shown
            code, shown = run(["--dir", root, "--show", "blank"])
            assert code == 1 and shown == "", shown

        # the default directory honours the env override
        os.environ[DEFAULT_DIR_ENV] = os.path.join("some", "where")
        assert Store().directory == os.path.abspath(
            os.path.join("some", "where")), Store().directory
        os.environ.pop(DEFAULT_DIR_ENV)
        assert Store().directory.endswith("prompts"), Store().directory
    finally:
        if saved is None:
            os.environ.pop(DEFAULT_DIR_ENV, None)
        else:
            os.environ[DEFAULT_DIR_ENV] = saved

    print("hearth-prompt self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-prompt",
        description="versioned prompt templates with strict variable checks")
    p.add_argument("--dir", help="template directory (default: the prompts "
                                 "directory beside the agent)")
    p.add_argument("--list", action="store_true",
                   help="print every template name in the store")
    p.add_argument("--show", metavar="NAME",
                   help="print a template's raw text")
    p.add_argument("--render", metavar="NAME",
                   help="render a template, needs one --var per variable")
    p.add_argument("--var", metavar="K=V", action="append", default=[],
                   help="a variable for --render, repeatable")
    p.add_argument("--fingerprint", metavar="NAME",
                   help="print a template's short content hash")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    store = Store(a.dir)
    try:
        if a.list:
            for name in store.names():
                print(name)
            return 0
        if a.show:
            sys.stdout.write(store.load(a.show))
            return 0
        if a.fingerprint:
            print(store.fingerprint(a.fingerprint))
            return 0
        if a.render:
            pairs = dict(_parse_var(item) for item in a.var)
            sys.stdout.write(store.render(a.render, pairs))
            return 0
    except PromptError as exc:
        sys.stderr.write("hearth-prompt: {}\n".format(exc))
        return 1
    p.error("nothing to do (use --self-test, --list, --show, --render or "
            "--fingerprint)")


if __name__ == "__main__":
    sys.exit(main())
