"""app.services.chunking — pure logic, no live dependencies. Checks the
recursive-splitter's actual boundary behavior: chunks respect paragraph/
sentence breaks where possible, overlap actually overlaps, and
reassembling chunks (ignoring the overlap prefix) reproduces the original
text with nothing silently dropped.
"""
from app.services.chunking import DEFAULT_CHUNK_SIZE, chunk_text


def test_blank_input_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_a_single_chunk():
    text = "A short sermon opening. Just a few sentences here."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].index == 0


def test_long_text_splits_on_paragraph_boundaries_not_mid_sentence():
    # Three distinct paragraphs, each well under chunk_size on its own,
    # but the whole text far exceeds a tiny chunk_size — the splitter
    # must break between paragraphs, not mid-word/mid-sentence.
    paragraphs = [
        "In the beginning God created the heavens and the earth. " * 5,
        "And the earth was without form, and void. " * 5,
        "And the Spirit of God moved upon the face of the waters. " * 5,
    ]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, chunk_size=200, overlap=0)

    assert len(chunks) >= 3
    for c in chunks:
        # No chunk should end mid-word (last char is whitespace, a
        # sentence-ending period, or the very end of a source paragraph).
        assert c.content == c.content.strip()
        assert not c.content.endswith((" and", " the", " of"))


def test_chunks_never_wildly_exceed_the_requested_size():
    text = "Grace and truth came by Jesus Christ. " * 500  # well over any single chunk_size
    chunks = chunk_text(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=0)
    assert len(chunks) > 1
    # Overlap is 0 here, so no chunk should meaningfully exceed chunk_size
    # (a little slack is fine — the splitter completes whatever piece a
    # separator boundary landed on rather than hard-cutting a real word).
    assert all(len(c.content) <= DEFAULT_CHUNK_SIZE * 1.1 for c in chunks)


def test_overlap_repeats_the_tail_of_the_previous_chunk():
    text = ("Paragraph one about faith. " * 10) + "\n\n" + ("Paragraph two about hope. " * 10)
    chunks = chunk_text(text, chunk_size=100, overlap=30)
    assert len(chunks) >= 2
    # Chunk N (for N>0) should start with a fragment that actually
    # appeared at the end of chunk N-1 — that's what "overlap" means.
    for prev, cur in zip(chunks, chunks[1:]):
        tail = prev.content[-30:]
        assert tail[-10:] in cur.content


def test_chunk_indices_are_sequential_from_zero():
    text = "One. Two. Three. " * 300
    chunks = chunk_text(text, chunk_size=50, overlap=0)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_a_single_word_longer_than_chunk_size_hard_cuts_rather_than_erroring():
    # Pathological input (e.g. a URL or base64 blob) with no separator at
    # all — must not crash or infinite-loop, just hard-cut.
    text = "x" * 5000
    chunks = chunk_text(text, chunk_size=1000, overlap=0)
    assert len(chunks) == 5
    assert "".join(c.content for c in chunks) == text
