---
title: Code signing policy
description: What signing would prove, what it would not, and the route to a certificate.
---

Hearth's installer is not signed today. This page says what that means for
anyone who runs it, how signing will work when it is switched on, and what has
to be true before that can happen.

## What "unsigned" costs a user right now

A downloaded, unsigned installer on Windows produces a full-screen Microsoft
Defender SmartScreen warning. Its only visible button says "Don't run"; the
option to continue is behind "More info". Windows shows the publisher as
"Unknown publisher", and there is no way for a user to tell a real Hearth
installer from a forged one by inspection.

That is the whole reason no build is published. A project that tells people to
click past a security warning has taught them to click past security warnings.

## What signing will look like

Signing is planned through **SignPath Foundation**, which issues free
code-signing certificates to open-source projects.

The most important consequence, and the one worth stating before anyone is
surprised by it: **the certificate is issued to SignPath Foundation, not to
Hearth or to its author.** When a signed installer runs, the publisher Windows
and SmartScreen display is:

> **SignPath Foundation**

That is correct and expected. It is not a sign that the download was tampered
with or that Hearth is impersonating someone. SignPath Foundation is the
certificate holder; Hearth is the project whose artifacts it signs. Anyone
verifying a Hearth installer should expect that name.

### Which artifacts are signed

When signing is switched on, the signed artifact is the Windows installer:

    Hearth-Setup-<version>.exe

built by the `installer` job in [.github/workflows/build.yml](https://github.com/EricFinland/hearth-windows/blob/main/.github/workflows/build.yml)
on a GitHub-hosted `windows-latest` runner, from the source in this repository
at the commit named in the workflow run.

Nothing else is signed. In particular, no artifact built anywhere other than
that workflow is signed, ever. An installer that claims to be Hearth and was
not produced by a public run of that workflow is not a Hearth release, whatever
it is signed with.

### How the pipeline is arranged

SignPath Foundation's programme requires that the artifact it signs is
demonstrably the output of public source built by a public CI service. Hearth's
build is arranged to meet that:

- Every job runs on a GitHub-hosted runner. There is no self-hosted runner in
  this repository, and there is no path by which a locally built binary can
  enter the release flow.
- The build script, `scripts/build_windows.py`, runs from a clean checkout and
  fetches everything else against checksummed manifests in `vendor/`. Nothing
  it consumes comes from the build machine.
- The artifact is published by `actions/upload-artifact` from the job that
  built it, so what SignPath collects is the job's own output.

### What is deliberately not in the workflow yet

There is no signing step in `build.yml`. That is not an oversight. The step
that submits the artifact to SignPath needs an organisation id, a project slug,
a signing-policy slug and an API token that only exist once the project has
been accepted into the programme, and a workflow that references secrets which
do not exist is a workflow that fails on every run. It will be added as part of
enrolment, as a job that:

1. depends on `installer`,
2. downloads the `hearth-windows-installer` artifact,
3. submits it to SignPath with `SignPath/github-action-submit-signing-request`,
4. re-uploads the signed result under a distinct artifact name.

## Update signing, which is separate and already in place

Hearth does not rely on code signing to protect updates, and never did.
`agent/hearth_update.py` accepts a release manifest only when it carries a
valid Ed25519 signature from a key listed in `release/trust.json`, which is
committed here and ships inside every install. A compromised download host,
or a host presenting a perfectly valid TLS certificate, cannot make Hearth
install anything.

The two protect different things and neither replaces the other:

| | protects against | in force |
| --- | --- | --- |
| Authenticode signature | a forged installer downloaded by a person | not yet |
| Ed25519 release signature | a forged update fetched by an install | already |

[updates.md](/hearth-windows/concepts/updates/) documents the update path in full, including the fact
that the shipped feed URL is a reserved name that can never resolve, so no
install can currently fetch an update from anywhere at all.

## Verifying a release, once there is one

When releases begin, each will publish the SHA-256 of the installer alongside
it, and the `installer` job already prints that digest into its own public log.
Comparing a download against the digest in the workflow run that produced it is
a check anyone can do without trusting this page.

## Reporting a problem

A signing or update-channel problem reaches every install at once. Report it
privately through GitHub Security Advisories as described in
[SECURITY.md](/hearth-windows/project/security/), and say in the first line that it concerns
signing.

---

*The requirements described here are Hearth's understanding of SignPath
Foundation's open-source programme and should be re-read against
signpath.org before enrolment, since the terms are theirs to change.*
