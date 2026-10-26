"""Embedding backend interfaces and lightweight projection vectors.

Note: HashEmbedding is a deterministic, pure-Python SimHash random-projection
vectorizer from agentmem (MIT License). It provides fast lexical similarity with
zero external dependencies. It is NOT equivalent to ML-trained dense semantic models.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import List, Protocol, Tuple, runtime_checkable


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Protocol for optional embedding backends."""
    dim: int
    name: str

    def embed(self, text: str) -> List[float]:
        """Compute an embedding vector for a single text."""
        ...

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Compute embedding vectors for a batch of texts."""
        ...


class HashEmbedding:
    """Random-Projection Hash Vectorizer (Pure Python stdlib, zero dependencies).

    Adapted from oxgeneral/agentmem (MIT License).
    Generates deterministic 128-dimensional vectors using token unigrams,
    character n-grams, and MD5 bitwise projection. Useful for fast lexical/token
    similarity when SQLite FTS5 is supplemented.
    """

    _WORD_WEIGHT = 3.0
    _NGRAM_WEIGHT = 1.0
    _MAX_TOKENS = 200

    def __init__(self, dim: int = 128, ngram_range: Tuple[int, int] = (2, 3)):
        self.dim = dim
        self.name = "hash"
        self._ngram_min, self._ngram_max = ngram_range
        self._k = min(4, dim // 2)

    def _tokenize(self, text: str) -> List[Tuple[str, float]]:
        cleaned = text.lower().strip()
        words = [w for w in re.split(r"[\s\W]+", cleaned, flags=re.UNICODE) if len(w) >= 2]
        tokens: List[Tuple[str, float]] = [(w, self._WORD_WEIGHT) for w in words]

        for word in words:
            padded = f"#{word}#"
            for n in range(self._ngram_min, self._ngram_max + 1):
                for i in range(len(padded) - n + 1):
                    tokens.append((padded[i : i + n], self._NGRAM_WEIGHT))
        return tokens[: self._MAX_TOKENS]

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = self._tokenize(text)
        if not tokens:
            return vec

        for token_str, weight in tokens:
            digest = hashlib.md5(token_str.encode("utf-8")).digest()
            words = struct.unpack("4i", digest)
            for w in words[: self._k]:
                idx = abs(w) % self.dim
                sign = 1.0 if w >= 0 else -1.0
                vec[idx] += sign * weight

        # L2-normalize
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


def get_embedding_backend(backend_type: str = "none") -> EmbeddingBackend | None:
    """Factory to retrieve an isolated embedding backend."""
    if backend_type == "hash":
        return HashEmbedding()
    elif backend_type == "model2vec":
        try:
            from model2vec import StaticModel  # type: ignore
            model = StaticModel.from_pretrained("minishlab/potion-base-8M")
            
            class Model2VecAdapter:
                dim = 256
                name = "model2vec"
                def embed(self, text: str) -> List[float]:
                    return model.encode(text).tolist()
                def embed_batch(self, texts: List[str]) -> List[List[float]]:
                    return model.encode(texts).tolist()
            return Model2VecAdapter()
        except Exception:
            return None
    return None
