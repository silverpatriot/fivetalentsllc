"""Phase 8 Task 3: word-level diffing between two sermon revisions.

Checked what's already available before adding anything: difflib is
already a dependency, already used in this codebase for citation
similarity (bible.py's verify_citation, via SequenceMatcher.ratio()) —
SequenceMatcher works on any sequence, not just characters, so tokenizing
into words and diffing THAT sequence reuses the existing dependency
rather than adding a new text-diff library.

Word-level, not line-level: difflib's classic line-oriented tools
(unified_diff, HtmlDiff) assume short-ish lines, the code-review use
case they were built for. A sermon manuscript is a handful of long
paragraphs — each one a single "line" in that sense — so a line-level
diff would only ever report "paragraph 3 changed" wholesale, exactly
the "two full texts side by side for the pastor to manually spot
differences" this task explicitly said not to build. Sentence-level was
also considered: word-level was chosen instead because it's the
standard granularity for this kind of prose diff (what Google Docs'
"compare documents" and `git diff --word-diff` both do), and because
sentence boundaries are already known to be fragile in this codebase
specifically (see bible.py's _extract_quoted_near docstring and the
2026-09-02 session history fixing three escalating sentence-boundary
bugs there) — reusing that same fragile boundary logic here wasn't
worth the risk for what a word-level diff already does well.
"""
import difflib
import re

# Splits on whitespace RUNS while keeping them as their own tokens (the
# capturing group in re.split keeps matched separators in the result) —
# "".join(tokenize(text)) == text exactly, so paragraph breaks ("\n\n")
# and all original spacing survive as ordinary tokens, no special-casing
# needed to reconstruct an "equal" span's original text losslessly.
_TOKEN_RE = re.compile(r"(\s+)")


def _tokenize(text: str) -> list[str]:
    return [tok for tok in _TOKEN_RE.split(text) if tok]


def diff_words(old_text: str, new_text: str) -> list[dict]:
    """Returns a list of {"op": "equal"|"delete"|"insert", "text": str}
    segments — reading them in order and concatenating "equal"+"insert"
    text reconstructs new_text exactly; "equal"+"delete" reconstructs
    old_text exactly. Adjacent opcodes of the same type are merged into
    one segment (word-token-level opcodes from SequenceMatcher are
    already fairly coarse, but this keeps consecutive matched punctuation/
    whitespace tokens from fragmenting a single visual span for no
    reason)."""
    old_tokens = _tokenize(old_text)
    new_tokens = _tokenize(new_text)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)

    segments: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append({"op": "equal", "text": "".join(old_tokens[i1:i2])})
        elif tag == "delete":
            segments.append({"op": "delete", "text": "".join(old_tokens[i1:i2])})
        elif tag == "insert":
            segments.append({"op": "insert", "text": "".join(new_tokens[j1:j2])})
        elif tag == "replace":
            segments.append({"op": "delete", "text": "".join(old_tokens[i1:i2])})
            segments.append({"op": "insert", "text": "".join(new_tokens[j1:j2])})

    # Merge adjacent same-op segments (a "replace" opcode already emits
    # two segments back-to-back that are never mergeable with each
    # other, but two consecutive "equal" opcodes from the matcher — rare,
    # but possible — would otherwise render as an unnecessary split).
    merged: list[dict] = []
    for seg in segments:
        if merged and merged[-1]["op"] == seg["op"]:
            merged[-1]["text"] += seg["text"]
        else:
            merged.append(seg)
    return merged
