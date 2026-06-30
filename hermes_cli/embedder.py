"""Pluggable embedder providers for semantic search.

Each embedder produces dense (1024d) and optionally sparse vectors.
The factory picks a provider by name from the ``auxiliary.embedding``
config block.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    dense: List[float]
    sparse: Optional[Dict[int, float]] = None


class EmbedderProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def initialize(self) -> None:
        ...

    @abstractmethod
    def embed(self, texts: List[str]) -> List[EmbeddingResult]:
        ...

    def embed_query(self, text: str) -> EmbeddingResult:
        return self.embed([text])[0]

    def shutdown(self) -> None:
        ...


try:
    from fastembed import TextEmbedding

    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False


class FastembedProvider(EmbedderProvider):
    MODEL_NAME = "intfloat/multilingual-e5-large"
    DIM = 1024

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        cache_dir: Optional[str] = None,
    ) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: Optional[TextEmbedding] = None

    @property
    def name(self) -> str:
        return "fastembed"

    def is_available(self) -> bool:
        return _HAS_FASTEMBED

    def initialize(self) -> None:
        if not _HAS_FASTEMBED:
            raise RuntimeError("fastembed is not installed")
        self._model = TextEmbedding(
            model_name=self._model_name,
            cache_dir=self._cache_dir,
            providers=None,
        )

    def embed(self, texts: List[str], query: bool = False) -> List[EmbeddingResult]:
        if self._model is None:
            raise RuntimeError("FastembedProvider not initialized")
        prefix = "query: " if query else "passage: "
        prefixed = [f"{prefix}{t}" for t in texts]
        results: List[EmbeddingResult] = []
        for dense_vec in self._model.embed(prefixed):
            vec = dense_vec.tolist()
            results.append(EmbeddingResult(dense=vec))
        return results

    def embed_query(self, text: str) -> EmbeddingResult:
        return self.embed([text], query=True)[0]

    def shutdown(self) -> None:
        self._model = None


def create_embedder(config: Optional[Dict[str, Any]] = None) -> EmbedderProvider:
    config = config or {}
    provider_name = config.get("provider", "fastembed").lower()

    if provider_name == "fastembed":
        return FastembedProvider(
            model_name=config.get("model", FastembedProvider.MODEL_NAME),
            cache_dir=config.get("cache_dir"),
        )

    raise ValueError(f"Unknown embedder provider: {provider_name}")
