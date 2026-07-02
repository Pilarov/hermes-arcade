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


# ---------------------------------------------------------------------------
# OpenAI embedder
# ---------------------------------------------------------------------------

try:
    from openai import OpenAI as OpenAIClient
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False


class OpenAIEmbedder(EmbedderProvider):
    """Embedder using OpenAI-compatible API (text-embedding-3-small/large)."""

    MODEL_NAME = "text-embedding-3-small"
    DIM = 1536

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        api_key: str = "",
        base_url: str = "",
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._client: Optional[Any] = None
        self._dim = self.DIM

    @property
    def name(self) -> str:
        return "openai"

    def is_available(self) -> bool:
        return _HAS_OPENAI

    def initialize(self) -> None:
        if not _HAS_OPENAI:
            raise RuntimeError("openai package is not installed")
        kwargs = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = OpenAIClient(**kwargs)
        # Probe dimensions
        if "large" in self._model_name:
            self._dim = 3072
        elif "3-small" in self._model_name:
            self._dim = 1536
        elif "ada" in self._model_name:
            self._dim = 1536
        else:
            self._dim = 1536

    def embed(self, texts: List[str], query: bool = False) -> List[EmbeddingResult]:
        if self._client is None:
            raise RuntimeError("OpenAIEmbedder not initialized")
        resp = self._client.embeddings.create(
            model=self._model_name,
            input=texts,
        )
        return [EmbeddingResult(dense=d.embedding) for d in resp.data]

    def embed_query(self, text: str) -> EmbeddingResult:
        return self.embed([text])[0]

    def shutdown(self) -> None:
        self._client = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_embedder(config: Optional[Dict[str, Any]] = None) -> EmbedderProvider:
    """Factory: pick embedder by config auxiliary.embedding.provider.

    Supported: fastembed (local ONNX), openai (API).
    """
    config = config or {}
    provider_name = config.get("provider", "fastembed").lower()

    if provider_name == "fastembed":
        return FastembedProvider(
            model_name=config.get("model", FastembedProvider.MODEL_NAME),
            cache_dir=config.get("cache_dir"),
        )

    if provider_name == "openai":
        import os
        api_key = config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        base_url = config.get("base_url") or os.environ.get("OPENAI_BASE_URL", "")
        return OpenAIEmbedder(
            model_name=config.get("model", OpenAIEmbedder.MODEL_NAME),
            api_key=api_key,
            base_url=base_url,
        )

    raise ValueError(f"Unknown embedder provider: {provider_name}")
