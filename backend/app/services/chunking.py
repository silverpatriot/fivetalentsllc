"""Recursive, boundary-aware text chunking for the RAG pipeline (Phase 4
Task 1). Checked current practice before building this rather than
inventing a strategy from scratch: recursive character splitting — try
paragraph breaks first, fall back to sentence/line breaks, then spaces,
then hard-cut only as a last resort — at roughly 512 tokens with ~10%
overlap is the benchmarked 2026 default (one recent evaluation had it
outperforming embedding-based "semantic" chunking, 69% vs 54% end-to-end
retrieval accuracy, for zero extra model calls). Implemented directly
rather than pulling in a library (e.g. LangChain's RecursiveCharacterTextSplitter)
for one self-contained algorithm.

Sized in characters, not tokens: token-counting needs a tokenizer specific
to the embedding model, which is one more dependency and one more thing to
keep in sync with EMBEDDING_MODEL. ~4 characters/token for English prose is
a standard enough approximation that 512 tokens ~= 2000 characters, 10%
overlap ~= 200 characters, well within the accuracy difference between
chunking strategies actually being measured in characters vs tokens.
"""
import dataclasses

# ~512 tokens / ~10% overlap, in characters — see module docstring.
DEFAULT_CHUNK_SIZE = 2000
DEFAULT_CHUNK_OVERLAP = 200

# Tried in this order — paragraph breaks first, progressively finer
# fallbacks only if a piece still doesn't fit within chunk_size.
_SEPARATORS = ["\n\n", "\n", ". ", " "]


@dataclasses.dataclass
class Chunk:
    index: int
    content: str


def _split_on(text: str, separator: str) -> list[str]:
    if not text:
        return []
    parts = text.split(separator)
    # Keep the separator on every piece but the last, so re-joining pieces
    # back together (when a piece still needs recursing into) reproduces
    # the original text exactly rather than silently dropping whitespace.
    return [p + separator for p in parts[:-1]] + [parts[-1]]


def _recursive_split(text: str, chunk_size: int, separators: list[str]) -> list[str]:
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    if not separators:
        # Last resort: no separator left made pieces small enough — hard
        # cut. Only reachable for pathological input (one word longer
        # than chunk_size, e.g. a URL or a base64 blob), not normal prose.
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    separator, rest = separators[0], separators[1:]
    pieces = _split_on(text, separator)

    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if len(buffer) + len(piece) <= chunk_size:
            buffer += piece
            continue
        if buffer.strip():
            chunks.append(buffer)
        if len(piece) > chunk_size:
            # This single piece is still too big at this separator level
            # — recurse into it with the next, finer separator.
            chunks.extend(_recursive_split(piece, chunk_size, rest))
            buffer = ""
        else:
            buffer = piece
    if buffer.strip():
        chunks.append(buffer)
    return chunks


def chunk_text(
    text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP
) -> list[Chunk]:
    """Split `text` into overlapping, boundary-aware chunks. Returns []
    for blank/whitespace-only input — callers (app/services/ingestion.py)
    treat that as "nothing to ingest," not an error."""
    text = text.strip()
    if not text:
        return []

    pieces = [p.strip() for p in _recursive_split(text, chunk_size, _SEPARATORS) if p.strip()]

    if overlap <= 0 or len(pieces) <= 1:
        return [Chunk(index=i, content=p) for i, p in enumerate(pieces)]

    # Overlap: prepend the tail of the previous chunk to each subsequent
    # one, so a sentence/idea split across a chunk boundary still appears
    # whole in at least one chunk.
    overlapped = [pieces[0]]
    for prev, current in zip(pieces, pieces[1:]):
        tail = prev[-overlap:]
        overlapped.append(f"{tail} {current}")
    return [Chunk(index=i, content=p) for i, p in enumerate(overlapped)]
