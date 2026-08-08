---
title: About
description: What this project is, how it relates to the NixOS system of the same name, and where its documentation lives.
---

Hearth for Windows is a desktop application for running language models on
your own hardware and pointing an autonomous coding agent at your own files.
It installs per-user, carries its own interpreter and its own inference
engine, and does not require an account, a subscription, or a network
connection to do the part it exists to do.

## Two projects share this name

That is worth stating plainly, because it causes confusion otherwise.

[**hearth**](https://ericfinland.github.io/hearth/) is a security-first NixOS
system: a whole reproducible operating system configuration where local models
and agents run sandboxed by default, every run is audited, and system state is
legible from boot. It is a Linux thing, and it expects you to be comfortable
with Linux.

**Hearth for Windows**, this project, is a desktop application. It shares an
engine lineage with the NixOS system and a good deal of code, but it is
installed by double-clicking an installer and used by opening a window.

The documentation sites are deliberately the same shape and deliberately
different colours: orange for the NixOS system, blue for this one. If you are
ever unsure which set of docs you have open, the colour answers it faster than
the URL does.

Where the two differ in what they can promise, the Windows documentation says
so rather than borrowing the stronger claim. The
[threat model](/hearth-windows/reference/threat-model/) and
[limitations](/hearth-windows/reference/limitations/) pages both name
protections that exist on NixOS and do not exist here.

## Where the documentation lives

The pages on this site are published from the `docs/` directory of the
repository, by `site/port_docs.py`. The repository is the source of truth: a
person reading the code on GitHub should not have to visit a website to learn
how the thing works, and a person reading the website should not be reading
something that has quietly drifted from the code.

If a page here looks wrong, the file to fix is under
[`docs/`](https://github.com/EricFinland/hearth-windows/tree/main/docs), and
the "Edit page" link at the bottom of each page goes to the right place.

## Licence

Apache-2.0, including the patent grant in section 3. See
[Licensing](/hearth-windows/reference/licensing/) for how that interacts with
the vendored components, and
[THIRD-PARTY-NOTICES.md](https://github.com/EricFinland/hearth-windows/blob/main/THIRD-PARTY-NOTICES.md)
for the generated list of everything that ships inside the installer.

There is no separate CLA. Section 5 of the licence already covers a
contribution offered under its terms.

## Reporting problems

Bugs and feature requests go in
[GitHub issues](https://github.com/EricFinland/hearth-windows/issues).

Anything security-sensitive should go through
[the security policy](/hearth-windows/project/security/) instead of a public
issue. Reports about the release signing key or the update channel should say
so in the first line: those are the two places where a problem reaches every
install at once.

## Credit

Built by [Eric Catalano](https://github.com/EricFinland).

The parts that are not mine are listed, with their licences, in
[THIRD-PARTY-NOTICES.md](https://github.com/EricFinland/hearth-windows/blob/main/THIRD-PARTY-NOTICES.md).
That file is generated from what is actually on disk at build time and is
never written by hand, and CI fails the build when it goes stale.
