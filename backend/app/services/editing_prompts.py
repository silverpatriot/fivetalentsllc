"""Prompt builders for Phase 6 iterative draft editing — the counterpart
to app/services/context_assembly.py's generation-time builders, but
deliberately separate: editing never re-assembles scripture/cadence/web
context from scratch, it only ever works from an ALREADY-generated
manuscript plus a pastor's instruction. See app/services/generation.py's
_run_edit for how these are used.

Two calls, two prompts:

- Locate (build_locate_messages): cheap/fast model, only runs when the
  pastor didn't select text themselves. Must return the exact verbatim
  span it's targeting, wrapped in sentinel markers, so the backend can
  find it in the real content via a plain string search — never trust a
  paraphrase as if it were a position. A span that can't be found (or
  isn't unique) is a clean, reported failure, not a silent bad edit —
  see _run_edit.

- Edit (build_edit_messages): stronger model, streamed. Given the full
  manuscript for context plus ONE exact target span, and asked to return
  ONLY the replacement for that span. The backend splices the result in
  — the model's own discipline about leaving the rest alone is not what
  this design depends on for that guarantee; the splice is.
"""

_LOCATE_SYSTEM_PROMPT = (
    "You are helping locate which exact part of an already-written sermon manuscript a pastor's "
    "edit instruction refers to. You will be given the full manuscript and the instruction. "
    "Identify the smallest contiguous passage (a sentence, a few sentences, or a paragraph) that "
    "the instruction is actually about — not the whole manuscript, and not more than the "
    "instruction needs. "
    "Respond with ONLY that passage, copied character-for-character EXACTLY as it appears in the "
    "manuscript — same words, same punctuation, same whitespace, no paraphrasing, no ellipsis, no "
    "correcting typos — wrapped exactly like this and nothing else:\n"
    "<<<TARGET>>>\n"
    "(the exact verbatim passage)\n"
    "<<<END_TARGET>>>"
)


def build_locate_messages(manuscript: str, instruction: str) -> list[dict[str, str]]:
    user = (
        f"## MANUSCRIPT\n{manuscript}\n\n"
        f"## INSTRUCTION\n{instruction}\n\n"
        "Return the exact verbatim target passage, wrapped in <<<TARGET>>>/<<<END_TARGET>>>, "
        "nothing else."
    )
    return [{"role": "system", "content": _LOCATE_SYSTEM_PROMPT}, {"role": "user", "content": user}]


_EDIT_SYSTEM_PROMPT = (
    "You are helping a pastor revise an already-written, already-approved sermon manuscript. You "
    "will be given the full manuscript for context, one exact passage from it marked as the "
    "TARGET, and an instruction for how to change ONLY that passage. Rewrite ONLY the TARGET "
    "passage according to the instruction — match the surrounding tone, voice, and flow so it "
    "reads as a seamless continuation of what comes immediately before and after it, but do not "
    "rewrite, summarize, quote, or repeat any other part of the manuscript. Whenever you cite "
    "scripture in your rewrite, cite the reference exactly (e.g. \"Romans 8:28\") so it can be "
    "checked against the source text — never quote a verse from memory without also giving its "
    "reference. Return ONLY the replacement text for the TARGET passage — no preamble, no "
    "headers, no explanation of what you changed, nothing else."
)


def build_edit_messages(manuscript: str, target_span: str, instruction: str) -> list[dict[str, str]]:
    user = (
        f"## FULL MANUSCRIPT (context only — do not repeat or rewrite any part of this except the "
        f"TARGET passage below)\n{manuscript}\n\n"
        f"## TARGET PASSAGE (rewrite only this)\n{target_span}\n\n"
        f"## INSTRUCTION\n{instruction}\n\n"
        "Return only the rewritten replacement for the TARGET passage."
    )
    return [{"role": "system", "content": _EDIT_SYSTEM_PROMPT}, {"role": "user", "content": user}]


def extract_target_span(raw_text: str) -> str | None:
    """Pulls the verbatim passage out of the locate model's
    <<<TARGET>>>/<<<END_TARGET>>> wrapper. None if the model didn't
    follow the format at all (treated as a locate failure by the
    caller, same as a span that doesn't match the manuscript)."""
    start_marker, end_marker = "<<<TARGET>>>", "<<<END_TARGET>>>"
    start = raw_text.find(start_marker)
    end = raw_text.find(end_marker)
    if start == -1 or end == -1 or end <= start:
        return None
    span = raw_text[start + len(start_marker) : end].strip("\n")
    return span or None
