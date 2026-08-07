# Contributing to Hearth

Thanks for your interest. Hearth is a Windows desktop application for running
local models and an autonomous coding agent on your own hardware. Contributions
that make it more honest about what your machine can do, harder to get wrong,
or easier to install are especially welcome.

## Ground rules

- **Keep the engine dependency-light.** Everything under `agent/` and
  `desktop/server/` is Python 3 standard library only. No third-party Python
  packages in those paths, ever: they ship inside the installer next to a
  vendored interpreter, and every dependency added there is another thing to
  vendor, licence, and keep in sync.
- **Keep the interface dependency-free.** `desktop/ui/` is plain HTML, CSS and
  ES modules with no framework and no build step. The packaging script copies
  it verbatim.
- **Node is the packaging layer, not the application.** `npm install` under
  `desktop/shell/` exists to run electron-builder. Adding a runtime dependency
  there changes what ships.
- **Match the surrounding style.** Clear names, comments that explain *why*, no
  needless cleverness.
- **No em dashes in committed files**, including code, documentation and commit
  messages. Use periods, commas, parentheses, or rewrite the sentence.

## Before you open a pull request

1. **Run the self-tests.** Every module under `agent/` and `desktop/server/`
   carries its own checks behind `--self-test`. No test runner, no network, no
   model:

   ```sh
   for m in agent/*.py desktop/server/*.py; do python "$m" --self-test; done
   ```

   All of them must exit zero. Anything you change should keep its self-test
   green and, ideally, gain a new assertion for the behaviour you added.

2. **Build the installer if you touched packaging, vendoring or the shell.**

   ```sh
   python scripts/build_windows.py
   ```

   This fetches and checksums the pinned llama.cpp and CPython, stages the
   payload, proves the staged payload runs under the staged interpreter,
   verifies the Electron fuses, and produces the installer. It is also what CI
   runs on every push, on a GitHub-hosted `windows-latest` runner.

3. **Regenerate the third-party notices if you changed what is vendored.**

   ```sh
   python scripts/third_party_notices.py          # regenerate
   python scripts/third_party_notices.py --check   # fail if stale
   ```

   The notices are generated from what is actually on disk, never written by
   hand. `--check` is a CI gate.

4. **Keep commits clean and atomic.** One logical change per commit, a clear
   message in the imperative mood (`feat: add X`, `fix: Y`). Explain in the
   body why the change is right, not just what it does.

## Project layout

- `agent/` ... the engine: the agent loop, the permission gate, workspace
  containment, contained subprocesses, hardware detection, the model shop,
  checkpoint and undo, the injection and secret scanners, the signed updater.
- `desktop/server/` ... the sidecar, a localhost HTTP layer over the engine.
- `desktop/shell/` ... the Electron shell, its fuses, and packaging config.
- `desktop/ui/` ... the interface.
- `scripts/` ... vendoring, packaging, notices, release manifests, benchmarks.
- `vendor/` ... licence texts and the pinned manifests. Binaries are fetched
  and checksummed at build time and are never committed.
- `release/` ... `trust.json`, the public update trust anchor.
- `docs/` ... reference documentation.

## Reporting bugs and ideas

Open an issue for bugs and feature requests. For anything security-sensitive,
follow [SECURITY.md](SECURITY.md) instead of filing a public issue.

## Licensing

Hearth is [Apache-2.0](LICENSE). By contributing, you agree that your
contributions are licensed under those terms, including the patent grant in
section 3. There is no separate CLA to sign; section 5 of the licence already
says that a contribution you send in is offered under the licence itself.
