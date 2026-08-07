# What this changes

<!-- A short description of the change and why it matters. -->

## Checklist

- [ ] Every `agent/*.py` and `desktop/server/*.py` module still passes `--self-test` (and a changed one gained an assertion)
- [ ] `python scripts/build_windows.py` still produces an installer, if packaging, vendoring or the shell changed
- [ ] `python scripts/third_party_notices.py --check` is green, if what is vendored changed
- [ ] No new third-party dependencies under `agent/` or `desktop/server/` (standard library only)
- [ ] No secrets, tokens, or private keys in the diff
- [ ] No em dashes in committed files
