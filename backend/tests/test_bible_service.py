"""app.services.bible against the real bible-api.com — no API key, no
mocking: this is exactly the "call a free public API" case the Phase 3
spec asked to verify live rather than assume. Needs outbound internet;
skips itself if bible-api.com is unreachable rather than failing the
whole suite over a network blip.
"""
import httpx
import pytest

from app.services import bible

try:
    _REACHABLE = httpx.get("https://bible-api.com/john+3:16", timeout=5).status_code == 200
except httpx.HTTPError:
    _REACHABLE = False

pytestmark = pytest.mark.skipif(not _REACHABLE, reason="bible-api.com not reachable from this environment")


async def test_fetch_passage_returns_real_kjv_text():
    passage = await bible.fetch_passage("John 3:16", "kjv")
    assert passage is not None
    assert "only begotten Son" in passage.text  # KJV wording specifically, not WEB's "one and only Son"
    assert passage.translation == "kjv"


async def test_fetch_passage_range():
    passage = await bible.fetch_passage("Romans 8:28-30", "kjv")
    assert passage is not None
    assert "work together for good" in passage.text
    assert "predestinate" in passage.text


async def test_fetch_passage_invalid_reference_returns_none():
    passage = await bible.fetch_passage("Frobnitz 99:99", "kjv")
    assert passage is None


def test_extract_citations_finds_references_in_free_text():
    text = (
        'As Paul writes in Romans 8:28, "all things work together for good." '
        "This is also true in 1 Peter 5:7 and John 3:16-18."
    )
    refs = bible.extract_citations(text)
    assert "Romans 8:28" in refs
    assert "1 Peter 5:7" in refs
    assert "John 3:16-18" in refs


def test_extract_citations_does_not_swallow_a_preceding_word():
    """Regression: found live in the Phase 3 smoke test — a sentence-
    initial capitalized word right before a real reference ("And
    Philippians 4:19") was being absorbed into the book name, turning a
    real, resolvable verse into a mangled one that looks hallucinated."""
    text = "And Philippians 4:19 promises that God shall supply all your need."
    refs = bible.extract_citations(text)
    assert "Philippians 4:19" in refs
    assert "And Philippians 4:19" not in refs


def test_extract_citations_keeps_ordinal_prefixes_for_numbered_books():
    """Regression: the opposite direction of the bug above — confirmed
    live that "I Corinthians 13:4" was extracted as "Corinthians 13:4"
    (ordinal dropped), which then genuinely fails to resolve against
    either Bible source and reports a real, accurately-quoted verse as a
    hallucinated citation. Digit, Roman-numeral, and spelled-out ordinal
    forms must all be kept attached to the book name."""
    text = (
        "As I Corinthians 13:4 says, love is patient. First Corinthians 13:13 speaks of faith, "
        "hope, and love. See also 2 Timothy 3:16 and II Timothy 2:15."
    )
    refs = bible.extract_citations(text)
    assert "I Corinthians 13:4" in refs
    assert "First Corinthians 13:13" in refs
    assert "2 Timothy 3:16" in refs
    assert "II Timothy 2:15" in refs
    # None of these should have lost their ordinal prefix.
    assert "Corinthians 13:4" not in refs
    assert "Timothy 2:15" not in refs


async def test_verify_citation_resolves_ordinal_forms_of_numbered_books():
    """End-to-end: not just extraction — the ordinal forms must actually
    resolve through fetch_passage's api.bible/bible-api.com chain too.
    No quote attached — this is testing reference RESOLUTION, not the
    separate quote-similarity check (a truncated quote would legitimately
    score as a mismatch against the full multi-clause verse, which isn't
    what this test is about). status is not_quoted, not verified, for the
    same reason as test_verify_citation_reports_not_quoted_for_a_bare_
    reference below — this draft genuinely has no quote marks in it at
    all, so resolution succeeding is proven by source_text, not status."""
    draft = "As I Corinthians 13:4 reminds us, love is patient and kind."
    result = await bible.verify_citation("I Corinthians 13:4", draft)
    assert result["status"] == "not_quoted"
    assert result["source_text"] is not None
    assert "suffereth long" in result["source_text"]


async def test_verify_citation_flags_hallucinated_reference():
    """The required test case: a deliberately wrong/hallucinated reference
    must be flagged, not silently passed through."""
    draft = 'The prophet declares in Habakkuk 12:5, "the fake shall live by faith."'
    result = await bible.verify_citation("Habakkuk 12:5", draft)
    assert result["status"] == "invalid_reference"
    assert result["source_text"] is None


async def test_verify_citation_flags_misquoted_real_reference():
    """A real, resolvable reference, but the draft quotes something that
    isn't what that verse actually says."""
    draft = 'Jesus himself said in John 3:16, "whoever knocks shall find the door open to riches."'
    result = await bible.verify_citation("John 3:16", draft)
    assert result["status"] == "quote_mismatch"
    assert result["source_text"] is not None
    assert "everlasting life" in result["source_text"]


async def test_verify_citation_passes_accurate_quote():
    draft = (
        'As John 3:16 says, "For God so loved the world, that he gave his only begotten Son, '
        'that whosoever believeth in him should not perish, but have everlasting life."'
    )
    result = await bible.verify_citation("John 3:16", draft)
    assert result["status"] == "verified"


async def test_verify_citation_reports_not_quoted_for_a_bare_reference():
    """A bare parenthetical reference with nothing quoted next to it isn't
    itself suspicious, but it's also not the same claim as `verified` —
    nothing was checked, so it gets its own status (`not_quoted`) rather
    than reusing `verified` for a check that never happened. Renamed from
    this test's old status=="verified" assertion — a deliberate, confirmed
    behavior change, not a regression (see test_verify_citation_does_not_
    misattribute_a_distant_unrelated_quote below for why the distinction
    matters in practice)."""
    draft = "God's love for the world is the whole point of the gospel (John 3:16)."
    result = await bible.verify_citation("John 3:16", draft)
    assert result["status"] == "not_quoted"
    assert result["quoted_text"] is None
    assert result["source_text"] is not None  # the reference did resolve


async def test_verify_citation_does_not_misattribute_a_distant_unrelated_quote():
    """Regression: the exact bug found live in Phase 6 edit-testing. A
    reference mentioned only in indirect/paraphrased prose, in its own
    sentence, with an unrelated quotation elsewhere in the document close
    enough (well within the OLD fixed 400-character window) to have been
    wrongly matched to it. Must be reported as not_quoted, not matched
    against — and definitely not flagged as a mismatch — against text
    that has nothing to do with this reference."""
    draft = (
        'She said, "Do you see how much that hurt me?" That question sat with him for days.\n\n'
        "I think of the warning in Hebrews 12:15, where believers are told to watch that no one "
        "falls short of the grace of God."
    )
    result = await bible.verify_citation("Hebrews 12:15", draft)
    assert result["status"] == "not_quoted"
    assert result["quoted_text"] is None
    assert "hurt me" not in (result["detail"] or "")


async def test_verify_citation_boundary_survives_a_quote_ending_sentence_across_a_paragraph_break():
    """Regression: the exact real bug found live (2026-09-02, Phase 6
    edit-testing on a real sermon), a second escape of the same class
    the previous test already fixed. The FIRST fix (_sentence_span)
    still used a bare "punctuation then whitespace" boundary regex —
    which never matched a sentence ending mid-quote ('...out.”', a
    closing curly quote sitting between the period and the whitespace).
    That one missed boundary let the "same sentence" window silently
    walk backward across an entire paragraph break into a wholly
    unrelated quote from the PREVIOUS paragraph, on a real production
    sermon, for the exact reference a pastor was actively trying to fix
    — confirmed live: identical shape to this test's draft below, one
    quote-ending sentence, then a paragraph break, then the reference in
    its own unquoted sentence. Must resolve to not_quoted; must never
    see the unrelated prior-paragraph quote at all."""
    draft = (
        'It says, "There is a way out."\n\n'
        "Romans 8:28 gives us that way. It calls us to trust that God works all things "
        "together for good."
    )
    result = await bible.verify_citation("Romans 8:28", draft)
    assert result["status"] == "not_quoted"
    assert result["quoted_text"] is None
    assert "way out" not in (result["detail"] or "")
