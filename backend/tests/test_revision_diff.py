"""Phase 8 Task 3: word-level diffing. Pure unit tests, no infra needed —
see app/services/revision_diff.py's module docstring for why word-level
(not line-level, not sentence-level) was chosen."""
from app.services.revision_diff import diff_words


def test_identical_text_is_a_single_equal_segment():
    text = "The Lord is my shepherd; I shall not want."
    diff = diff_words(text, text)
    assert diff == [{"op": "equal", "text": text}]


def test_a_single_changed_word_is_isolated_not_the_whole_sentence():
    diff = diff_words("The quick brown fox.", "The slow brown fox.")
    ops = [(seg["op"], seg["text"]) for seg in diff]
    assert ("delete", "quick") in ops
    assert ("insert", "slow") in ops
    # Everything else stayed "equal" — not swallowed into one big replace.
    equal_text = "".join(seg["text"] for seg in diff if seg["op"] == "equal")
    assert equal_text == "The " + " brown fox."


def test_reconstructs_both_original_texts_exactly():
    old = "Point one: contentment is learned.\n\nPoint two: it takes practice."
    new = "Point one: contentment is chosen daily.\n\nPoint two: it takes real practice."
    diff = diff_words(old, new)
    assert "".join(seg["text"] for seg in diff if seg["op"] in ("equal", "delete")) == old
    assert "".join(seg["text"] for seg in diff if seg["op"] in ("equal", "insert")) == new


def test_paragraph_breaks_survive_as_ordinary_equal_text():
    old = "First paragraph.\n\nSecond paragraph."
    new = "First paragraph, revised.\n\nSecond paragraph."
    diff = diff_words(old, new)
    equal_segments = [seg["text"] for seg in diff if seg["op"] == "equal"]
    assert any("\n\n" in seg for seg in equal_segments)


def test_a_pure_insertion_has_no_delete_segments():
    diff = diff_words("Point one.", "Point one. Point two.")
    assert not any(seg["op"] == "delete" for seg in diff)
    assert any(seg["op"] == "insert" and "Point two." in seg["text"] for seg in diff)


def test_a_pure_deletion_has_no_insert_segments():
    diff = diff_words("Point one. Point two.", "Point one.")
    assert not any(seg["op"] == "insert" for seg in diff)
    assert any(seg["op"] == "delete" and "Point two." in seg["text"] for seg in diff)


def test_empty_strings_do_not_error():
    assert diff_words("", "") == []
    assert diff_words("", "New text.") == [{"op": "insert", "text": "New text."}]
    assert diff_words("Old text.", "") == [{"op": "delete", "text": "Old text."}]
