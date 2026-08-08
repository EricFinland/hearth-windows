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
  ES modules with no framework and no build step. `desktop/tauri/build.rs`
  links it into the executable verbatim.
- **There is no Node, on either machine.** The shell was Electron until the
  Tauri port; it is now Rust, and `cargo tauri build` is the whole packaging
  layer. If you find yourself wanting `npm install`, that is a sign the change
  belongs somewhere else.
- **Every crate you add ships, and has to be licensed.** `desktop/tauri/` is
  the one place third-party code enters the application, and
  `scripts/third_party_notices.py --check` fails the build when a crate's
  licence has no text on disk. Adding a dependency there is a licensing
  decision as much as a technical one.
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
   compiles the Tauri shell, and produces the installer.
   `scripts/verify_binary.py` then reads the hardening back out of the built
   executable rather than trusting that the build asked for it. The same
   command is what CI runs on every push, on a GitHub-hosted `windows-latest`
   runner.

   It needs Rust 1.82 or newer, `cargo install tauri-cli --locked`, and a
   network connection the first time. See
   [docs/getting-started.md](docs/getting-started.md) if you have not built it
   before.

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
- `desktop/tauri/` ... the Rust shell: window, sidecar supervision, the
  loopback origin check, the updater, and the installer configuration.
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
