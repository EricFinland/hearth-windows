#!/usr/bin/env python3
"""hearth similarity: near-duplicate detection without an embedding model.

Two stores in this agent fill up with text the agent wrote itself:
hearth_memory.py (self-learning, the lessons the loop decides are worth
keeping) and hearth_knowledge.py (retrieval, what gets pulled back into
the prompt). Both share one failure. An agent that learns the same lesson
forty times writes forty copies of it, then retrieves the same thing forty
times, and the context window it is filling holds about four thousand
tokens. The copies do not merely waste room: they outvote everything else
in retrieval, so the agent's own repetition becomes the only thing it can
still see.

Exact-match deduplication does not touch this. The forty copies are
reworded. "always run the tests before pushing" and "run the tests before
you push" hash differently and compare unequal, so a set(), a UNIQUE
column or a checksum lets every one of them through.

What this module deliberately does NOT do: call an embedding model. That
would mean a third-party dependency, a model download, weights competing
for the VRAM the agent is already using, latency on a path that runs on
every single write, and a network round trip inside a store that has to
work offline. Shingled Jaccard similarity is a dozen lines of standard
library and catches rewordings of one sentence, which is the actual
failure being seen. It does not catch paraphrase that reuses no wording
("ship only green builds" against "always run the tests before pushing"),
and it is not meant to. The cost of a miss is one duplicate row; the cost
of a wrong fold is a deleted memory, so a blunt, legible, conservative
measure is the right trade here.

Also deliberately absent: no persistence, no I/O, no store awareness. The
functions are pure, take and return plain built-ins, and leave the policy
question (fold on write, or sweep the table later) to the caller.

Character k-grams, not word k-grams
-----------------------------------
Shingles here are character k-grams of the normalised text, k=4 by
default. Word k-grams were the other option and are worse for this job:
one substituted word destroys k separate word-grams, so "run the test
suite before pushing" against "run the test suite before you push" loses
most of its word 4-grams and scores like an unrelated sentence.
Character 4-grams degrade smoothly instead. A substitution damages only
the grams straddling it, shared stems still match ("push" inside
"pushing", "test" inside "tests"), and a typo costs a few grams rather
than a whole phrase. The price is that character grams give unrelated
English prose a small non-zero floor, because every sentence contains
" the" and "ing "; measured on this module's own sentences that floor
sits near 0.1, well below any threshold worth using.

Thresholds
----------
The default is 0.8, a deliberately high bar: it catches light rewordings
(a swapped modal, a changed tense, a reordered clause) and leaves heavier
paraphrase alone. Around 0.5 it starts catching real paraphrase and also
starts folding together lessons that merely share a subject. A
self-learning store that can cheaply relearn a lesson should stay high.

Standard library only.
"""

import argparse
import json
import sys

DEFAULT_K = 4
DEFAULT_THRESHOLD = 0.8


def normalise(text):
    """Lowercase, drop punctuation, collapse whitespace runs to one space.

    Case, punctuation and spacing are exactly the differences that carry
    no meaning for "is this the same lesson", so they go before shingling.
    str.isalnum and str.casefold are unicode aware, so accented letters
    and non-Latin scripts survive while quotes, dashes, emoji and control
    characters are all treated as plain separators. The single remaining
    space is kept rather than stripped, so word boundaries still count as
    evidence inside a k-gram."""
    out = []
    pending_space = False
    for ch in text:
        if ch.isalnum():
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(ch.casefold())
        else:
            pending_space = True
    return "".join(out)


def shingles(text, k=DEFAULT_K):
    """Set of character k-grams of the normalised text.

    Empty, whitespace-only or punctuation-only text gives the empty set.
    Text shorter than k gives exactly one shingle, the whole normalised
    string, so a short entry ("retry 3x") still compares exactly instead
    of vanishing into an empty set that matches everything."""
    if k < 1:
        raise ValueError("k must be >= 1")
    norm = normalise(text)
    if not norm:
        return set()
    if len(norm) <= k:
        return {norm}
    return set(norm[i:i + k] for i in range(len(norm) - k + 1))


def jaccard(a, b):
    """|a & b| / |a | b| for two shingle sets, from 0.0 to 1.0.

    Two empty sets are identical, so they score 1.0 rather than raising on
    0/0. That is what dedupe wants: forty blank entries are forty
    duplicates. An empty set against a non-empty one scores 0.0."""
    a = set(a)
    b = set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / float(len(a | b))


def similarity(text_a, text_b, k=DEFAULT_K):
    """Jaccard similarity of two texts' character k-gram sets.

    Identical texts (and texts that differ only in case, punctuation or
    spacing) score exactly 1.0. Texts sharing no k-gram at all score
    exactly 0.0."""
    return jaccard(shingles(text_a, k), shingles(text_b, k))


def near_duplicates(items, threshold=DEFAULT_THRESHOLD, k=DEFAULT_K):
    """Every pair (i, j, score) with i < j and score at or above threshold.

    Returned in ascending (i, j) order, so the result is stable for a
    stable input. Quadratic in the number of items on purpose: these
    stores hold hundreds of rows, not millions, and an exact pass is far
    easier to trust than minhash buckets. Shingle sets are built once per
    item rather than once per comparison, which is where the real cost
    would otherwise be."""
    sets = [shingles(t, k) for t in items]
    pairs = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            score = jaccard(sets[i], sets[j])
            if score >= threshold:
                pairs.append((i, j, score))
    return pairs


def dedupe(items, threshold=DEFAULT_THRESHOLD, k=DEFAULT_K):
    """Fold near-duplicates together. Returns (kept, folded).

    kept    the surviving texts, in the order their first occurrence
            appeared in items.
    folded  {dropped index: index of the entry it was folded into}, both
            indices into the original items. An entry that survived is
            never a key, so an empty dict means nothing was folded.

    Two rules decide what survives a fold, and they can disagree:

      position  the FIRST occurrence holds the slot. Order is the one
                thing a store can rely on: row ids, timestamps and
                anything already citing the older entry stay valid, and
                re-running dedupe on an unchanged list gives an unchanged
                answer.
      text      the LONGEST variant supplies the text. Among rewordings
                of one lesson the longest usually carries the qualifier
                the short one dropped ("retry, but not on a 4xx"), and in
                a four-thousand-token window a lost qualifier is the
                expensive mistake. Length is measured on the normalised
                text, so extra padding or punctuation cannot win a fold,
                and ties keep the earlier text.

    So kept[n] may be a later item's text sitting in an earlier item's
    place, which is exactly why folded maps indices and not strings.

    Clustering is greedy against anchors rather than a transitive
    closure: an item joins the first anchor it matches, and two anchors
    that both match it are not thereby merged with each other. Chained
    merges are how a measure this blunt swallows a whole store, one 0.8
    hop at a time. Comparison always uses the anchor's own shingles, not
    the longest variant's, so the cluster keeps one fixed identity no
    matter what order the rewordings arrive in."""
    anchor_sets = []
    anchor_index = []
    texts = []
    sizes = []
    folded = {}
    for idx, item in enumerate(items):
        sh = shingles(item, k)
        size = len(normalise(item))
        hit = None
        for pos in range(len(anchor_sets)):
            if jaccard(sh, anchor_sets[pos]) >= threshold:
                hit = pos
                break
        if hit is None:
            anchor_sets.append(sh)
            anchor_index.append(idx)
            texts.append(item)
            sizes.append(size)
        else:
            folded[idx] = anchor_index[hit]
            if size > sizes[hit]:
                texts[hit] = item
                sizes[hit] = size
    return texts, folded


def _self_test():
    # normalisation: case, punctuation and whitespace runs all go away
    assert normalise("  Hello,   WORLD!!  ") == "hello world"
    assert normalise("") == ""
    assert normalise("!!! ... ???") == ""
    assert normalise("a\t\nb") == "a b"
    assert normalise("Run tests.") == normalise("run    tests")

    # shingles: empty, shorter than k, and the expected gram count
    assert shingles("") == set()
    assert shingles("   ,.  ") == set()
    assert shingles("ab", k=4) == {"ab"}
    assert shingles("abcd", k=4) == {"abcd"}
    assert shingles("abcde", k=4) == {"abcd", "bcde"}
    assert len(shingles("x" * 20, k=4)) == 1  # all grams identical
    try:
        shingles("abc", k=0)
        raise AssertionError("k=0 should raise")
    except ValueError:
        pass

    # jaccard edges
    assert jaccard(set(), set()) == 1.0
    assert jaccard(set(), {"abcd"}) == 0.0
    assert jaccard({"abcd"}, {"abcd"}) == 1.0
    assert jaccard({"ab", "cd"}, {"cd", "ef"}) == 1.0 / 3.0

    # identical is exactly 1.0, disjoint is exactly 0.0
    line = "always run the test suite before pushing to main"
    assert similarity(line, line) == 1.0
    noisy = "  ALWAYS, run the test suite; before pushing to main!  "
    assert similarity(line, noisy) == 1.0
    assert similarity("aaaa aaaa", "bbbb bbbb") == 0.0
    assert similarity("", "") == 1.0
    assert similarity("", line) == 0.0
    assert similarity("ab", "cd") == 0.0  # both shorter than k
    assert similarity("ab", "ab") == 1.0

    # unicode survives normalisation and compares sensibly
    assert similarity("Café protocol", "café protocol") == 1.0
    assert similarity("日本語のテスト",
                      "日本語のテスト") == 1.0
    assert similarity("日本語", "テスト") == 0.0
    # an accent is a real difference, so it costs grams without wiping
    # the match out entirely
    accented = similarity("naive retry", "naïve retry")
    assert 0.3 < accented < 1.0, accented

    # The whole point. Two genuine rewordings of one lesson (a swapped
    # verb phrase) clear the default threshold; two unrelated lessons of
    # the same length, both about this agent's own plumbing, do not come
    # anywhere near it. reword_short is the shorter of the pair.
    reword_short = ("when a tool call fails with a timeout, retry it once "
                    "and then stop rather than looping forever")
    reword_long = ("when a tool call fails with a timeout, retry it once "
                   "and then give up rather than looping forever")
    unrelated = ("the ledger is append only, so correct a bad row by "
                 "writing a new row instead of editing the old one")
    assert len(reword_long) > len(reword_short)
    assert abs(len(unrelated) - len(reword_long)) < 5  # similar length
    rs = similarity(reword_short, reword_long)
    us = similarity(reword_long, unrelated)
    assert rs > DEFAULT_THRESHOLD, rs
    assert us < DEFAULT_THRESHOLD, us
    assert us < 0.25, us  # unrelated prose sits near the k-gram floor
    assert rs > us

    # a reordered clause is also caught, which is what word k-grams would
    # have lost
    assert similarity("always run the test suite before pushing to main",
                      "before pushing to main, always run the test suite"
                      ) > DEFAULT_THRESHOLD

    # near_duplicates: the reworded pair, and nothing else
    items = [reword_short, unrelated, reword_long]
    pairs = near_duplicates(items)
    assert [(i, j) for i, j, _ in pairs] == [(0, 2)], pairs
    assert pairs[0][2] == rs, pairs
    assert near_duplicates([]) == []
    assert near_duplicates([reword_long]) == []  # a list of one: no pairs
    assert near_duplicates(items, threshold=1.01) == []

    # dedupe: a list of one item, an empty list, nothing to fold
    assert dedupe([reword_long]) == ([reword_long], {})
    assert dedupe([]) == ([], {})
    kept, folded = dedupe([reword_long, unrelated])
    assert kept == [reword_long, unrelated], kept
    assert folded == {}, folded

    # dedupe: the FIRST occurrence holds the slot, the LONGEST variant
    # supplies the text, and the mapping says what went where. The longer
    # wording arrives second here, so position and text disagree and both
    # rules are visible at once.
    store = [reword_short, unrelated, reword_long, reword_short]
    kept, folded = dedupe(store)
    assert kept == [reword_long, unrelated], kept
    assert folded == {2: 0, 3: 0}, folded
    assert len(kept) + len(folded) == len(store)
    # every fold points backwards, at a survivor
    for dropped, anchor in folded.items():
        assert anchor < dropped, (dropped, anchor)
        assert anchor not in folded, (dropped, anchor)

    # blank entries are duplicates of each other, not of real text
    kept, folded = dedupe(["", "   ", "!!", reword_long])
    assert kept == ["", reword_long], kept
    assert folded == {1: 0, 2: 0}, folded

    # stability: same input, same output, order preserved, run twice
    stable = [reword_short, unrelated, reword_long, "", "retry 3x",
              unrelated, reword_short]
    first = dedupe(stable)
    second = dedupe(stable)
    assert first == second, (first, second)
    kept, folded = first
    anchors = [i for i in range(len(stable)) if i not in folded]
    assert anchors == sorted(anchors), anchors
    assert len(kept) == len(anchors), (kept, anchors)
    assert anchors == [0, 1, 3, 4], anchors
    assert kept == [reword_long, unrelated, "", "retry 3x"], kept
    assert folded == {2: 0, 5: 1, 6: 0}, folded

    # threshold is honoured at both extremes
    assert dedupe(stable, threshold=1.01)[1] == {}
    kept_all, folded_all = dedupe(["a b c", "x y z"], threshold=0.0)
    assert kept_all == ["a b c"], kept_all
    assert folded_all == {1: 0}, folded_all

    print("hearth-similarity self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-similarity",
        description="near-duplicate detection over a JSON list of strings")
    p.add_argument("path", nargs="?",
                   help="file holding a JSON list of strings "
                        "(omitted or '-' reads stdin)")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help="fold at or above this score (default {})".format(
                       DEFAULT_THRESHOLD))
    p.add_argument("--k", type=int, default=DEFAULT_K,
                   help="character k-gram size (default {})".format(DEFAULT_K))
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()

    if a.path and a.path != "-":
        try:
            with open(a.path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            sys.stderr.write("cannot read {}: {}\n".format(a.path, exc))
            return 2
    else:
        raw = sys.stdin.read()
    try:
        items = json.loads(raw)
    except ValueError as exc:
        sys.stderr.write("not valid JSON: {}\n".format(exc))
        return 2
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        sys.stderr.write("expected a JSON list of strings\n")
        return 2

    kept, folded = dedupe(items, threshold=a.threshold, k=a.k)
    groups = {}
    for dropped, anchor in folded.items():
        groups.setdefault(anchor, []).append(dropped)
    head = "{} item(s), {} kept, {} duplicate group(s) at threshold {} (k={})"
    print(head.format(len(items), len(kept), len(groups), a.threshold, a.k))
    for anchor in sorted(groups):
        print("")
        print("[{}] {}".format(anchor, items[anchor]))
        for dropped in sorted(groups[anchor]):
            print("    {:.3f} [{}] {}".format(
                similarity(items[anchor], items[dropped], k=a.k),
                dropped, items[dropped]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
