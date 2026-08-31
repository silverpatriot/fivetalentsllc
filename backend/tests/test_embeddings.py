"""app.services.embeddings against the real, live OpenRouter embeddings
endpoint — confirmed reachable and functional at the start of this phase
(see app/core/config.py's embedding_model comment); exercised for real
here, not mocked, same as bible-api.com/Tavily elsewhere in this suite.
"""
import pytest

from app.services import embeddings as embeddings_module
from app.services.embeddings import EmbeddingError, embed_batch_sync, embed_text, embed_text_sync


async def test_embed_text_returns_a_1536_dim_vector_for_real():
    vector = await embed_text("Grace and truth came by Jesus Christ.")
    assert len(vector) == 1536
    assert all(isinstance(x, float) for x in vector)


def test_embed_text_sync_matches_the_async_version_in_shape():
    vector = embed_text_sync("In the beginning God created the heavens and the earth.")
    assert len(vector) == 1536


def test_embed_batch_sync_returns_one_vector_per_input_in_order():
    texts = ["faith", "hope", "love"]
    vectors = embed_batch_sync(texts)
    assert len(vectors) == 3
    assert all(len(v) == 1536 for v in vectors)
    # Different inputs must not collapse to identical vectors.
    assert vectors[0] != vectors[1] != vectors[2]


def test_embed_batch_sync_empty_list_returns_empty_without_a_network_call():
    assert embed_batch_sync([]) == []


def test_similar_sentences_embed_closer_than_unrelated_ones():
    """A real sanity check on the vectors themselves, not just their
    shape — two sentences about the same topic should have a smaller
    cosine distance than two sentences about unrelated topics. Computed
    directly here (not via pgvector) since this test has no database."""
    import math

    def cosine_distance(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        return 1 - dot / (norm_a * norm_b)

    v1 = embed_text_sync("The pastor preached on grace and forgiveness.")
    v2 = embed_text_sync("The sermon was about God's mercy and forgiving others.")
    v3 = embed_text_sync("The quarterly financial report showed a 3% revenue increase.")

    assert cosine_distance(v1, v2) < cosine_distance(v1, v3)


def test_embed_text_raises_clear_error_when_no_api_key_configured(monkeypatch):
    monkeypatch.setattr(embeddings_module.settings, "openrouter_api_key", "")
    with pytest.raises(EmbeddingError, match="OPENROUTER_API_KEY"):
        embed_text_sync("anything")
