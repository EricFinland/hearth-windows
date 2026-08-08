#!/usr/bin/env python3
r"""Port the repository's docs/ into the Starlight content tree.

    python site/port_docs.py

The docs in docs/ are the source of truth and stay that way: someone reading
the repository on GitHub should not be sent to a website to find out how the
thing works. This script republishes them, it does not fork them. Run it again
after editing anything under docs/ and the site catches up.

What it does per file, and why each step is here:

  * Adds the Starlight frontmatter (title, description). Starlight renders the
    title itself, so the leading `# Heading` is removed as well; leaving it in
    renders the title twice.

  * Rewrites relative links. docs/updates.md links to `packaging-windows.md`
    because that resolves on GitHub. On the site the same page lives at
    /hearth-windows/reference/packaging/, and the raw link would 404. Every
    target that has a site page is rewritten to its route; every target that
    does not (LICENSE, NOTICE, the README) is rewritten to its GitHub blob URL
    rather than dropped, because those files are real and worth reaching.

  * Refuses to guess. A relative link whose target is not in either table
    stops the script instead of being emitted broken, so a doc that grows a
    new cross-reference is a build failure here and not a 404 a reader finds.
"""

import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "site", "src", "content", "docs")
BASE = "/hearth-windows"
BLOB = "https://github.com/EricFinland/hearth-windows/blob/main/"

#: source path -> (site path under content/docs, title, description)
PAGES = [
    ("docs/getting-started.md", "getting-started/install.md",
     "Install and first run",
     "From an empty folder to a working Hearth, in order, with no steps assumed."),
    ("docs/windows.md", "getting-started/guide.md",
     "The full guide",
     "What Hearth is, what it does today, the permission modes, containment, and what survives a restart."),
    ("docs/model-shop.md", "concepts/model-shop.md",
     "The model shop",
     "How Hearth decides which models actually fit your machine, including the KV cache."),
    ("docs/updates.md", "concepts/updates.md",
     "Signed updates",
     "The Ed25519 trust anchor, what it protects, and what it deliberately does not."),
    ("docs/agent-swarm.md", "concepts/agent-swarm.md",
     "The agent swarm",
     "Running several agents at once, and the measured verdict on whether it helps."),
    ("docs/mcp.md", "concepts/mcp.md",
     "MCP servers",
     "Connecting Model Context Protocol servers, and the gate every tool call passes through."),
    ("docs/limitations.md", "reference/limitations.md",
     "Limitations",
     "The honest page. Read this before trusting Hearth with anything that matters."),
    ("docs/security/windows-threat-model.md", "reference/threat-model.md",
     "Threat model",
     "What the boundaries are on Windows, what they are not, and which ones are weaker than on the NixOS system."),
    ("docs/privacy.md", "reference/privacy.md",
     "Privacy",
     "Every destination Hearth contacts, and why."),
    ("docs/packaging-windows.md", "reference/packaging.md",
     "Packaging and the installer",
     "How the installer is built, what it carries, and the measurements behind the Tauri port."),
    ("docs/licensing.md", "reference/licensing.md",
     "Licensing",
     "Apache-2.0, the vendored components, and how third-party notices are generated."),
    ("docs/code-signing-policy.md", "reference/code-signing.md",
     "Code signing policy",
     "What signing would prove, what it would not, and the route to a certificate."),
    ("CONTRIBUTING.md", "project/contributing.md",
     "Contributing",
     "Ground rules, the self-tests, and what to run before opening a pull request."),
    ("SECURITY.md", "project/security.md",
     "Security policy",
     "How to report a vulnerability privately, and what gets priority."),
]

#: A link target as written in a source file -> where it goes on the site.
#: Keyed by the target's repo-relative path, so the same table serves links
#: written from docs/ and from the repository root.
ROUTES = {}
for src, dest, _t, _d in PAGES:
    ROUTES[src] = "{}/{}/".format(BASE, dest[:-3])

#: Anything else the repository really contains (LICENSE, the README, a
#: workflow, a source file) is linked to GitHub rather than dropped: a reader
#: who wants the licence text or the CI definition should be able to reach it.
#: Membership is tested against the filesystem rather than a hand-kept list,
#: because a hand-kept list is one more thing to forget to update, and the
#: check that matters (does this target exist at all) is the one the disk can
#: answer. A target that is neither a page nor a real file is a dead link and
#: still stops the script.

LINK = re.compile(r"\[([^\]]*)\]\((?!https?:|#|mailto:)([^)]+)\)")


def resolve(target, src_path):
    """Map one relative link to a site route or a GitHub URL."""
    anchor = ""
    if "#" in target:
        target, anchor = target.split("#", 1)
        anchor = "#" + anchor
    if not target:
        return None  # a bare anchor, left alone
    # Resolve the link against the directory of the file that wrote it.
    joined = os.path.normpath(
        os.path.join(os.path.dirname(src_path), target)).replace("\\", "/")
    if joined in ROUTES:
        return ROUTES[joined] + anchor
    if os.path.exists(os.path.join(REPO, joined)):
        return BLOB + joined + anchor
    return False  # neither a page nor a real file: caller raises


def port(src, dest, title, description):
    full = os.path.join(REPO, src)
    text = io.open(full, encoding="utf-8").read()

    # Starlight renders the title from frontmatter; a leading H1 duplicates it.
    text = re.sub(r"\A#\s+[^\n]*\n+", "", text)

    unresolved = []

    def sub(m):
        label, target = m.group(1), m.group(2)
        r = resolve(target, src)
        if r is None:
            return m.group(0)
        if r is False:
            unresolved.append(target)
            return m.group(0)
        return "[{}]({})".format(label, r)

    text = LINK.sub(sub, text)
    if unresolved:
        raise SystemExit(
            "{}: link target(s) with no site page and no GitHub fallback: {}\n"
            "Add them to PAGES or ON_GITHUB in site/port_docs.py rather than "
            "letting the site ship a dead link.".format(src, ", ".join(sorted(set(unresolved)))))

    out = os.path.join(OUT, dest)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    head = "---\ntitle: {}\ndescription: {}\n---\n\n".format(
        title.replace('"', "'"), description.replace('"', "'"))
    io.open(out, "w", encoding="utf-8", newline="\n").write(head + text)
    return len(text)


def main():
    total = 0
    for src, dest, title, description in PAGES:
        n = port(src, dest, title, description)
        total += n
        print("  {:<44} -> {:<34} {:>7,} chars".format(src, dest, n))
    print("\n  {} pages, {:,} characters".format(len(PAGES), total))


if __name__ == "__main__":
    main()
