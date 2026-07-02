# Hermes ArcadeDB — Evaluation & Agent Model

## What was built (02-03.07.2026)

### Architecture Achieved

```
                    Hermes Agent
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
        Graph         Vector        Full-Text
     (MENTIONS,      (LSM_VECTOR,    (LIKE,
      RELATES_TO,    vector.         Lucene
      HAS_MESSAGE)   neighbors)      planned)
            │            │            │
            └────────┬───┘            │
                     ▼                │
              vector.fuse()           │
              (RRF fusion)            │
                     │                │
                     ▼                ▼
              Ranked results    Keyword matches
```

### Key Metrics (55/60 tests, 92% pass rate)

| Layer | Status | Details |
|-------|--------|---------|
| Schema | Complete | 30 vertex types, 15 edge types, LSM_VECTOR + FULL_TEXT indexes |
| Session CRUD | Complete | 83 methods, identical to SQLite SessionDB API |
| Vector Search | Working | `vector.neighbors()` + `vector.fuse()` + LIKE fallback |
| Entity Extraction | Working | Regex-based, 8 entities extracted from test messages |
| Graph Edges | Working | MENTIONS (Message→Entity) + RELATES_TO (Entity↔Entity) |
| Embedder | Working | 4 providers: fastembed (1024d), openai (1536d), ollama (768d), compat |
| Transactions | Working | BEGIN/COMMIT/ROLLBACK with connection recovery |
| Migration | Working | SQLite → ArcadeDB with dry-run + verify |

### What was NOT built (and why)

1. **Full-text SEARCH_INDEX** — ArcadeDB 26.7.1-SNAPSHOT Lucene hangs through PG protocol
2. **Streaming API** — AIAgent lacks streaming API; simulated streaming implemented instead
3. **Delete cascade** — DELETE VERTEX hangs on edge cascade; soft-delete used
4. **MCP integration** — not in scope for this migration
5. **Graph algorithms** — PageRank, community detection not wired

### Alignment with Industry Standards (from web research)

| Industry Pattern | Hermes Implementation | Gap |
|-----------------|---------------------|-----|
| GraphRAG indexing (~$7/1500 docs) | Entity extraction via regex (free) | Lower quality; should use LLM-based extraction |
| Agentic GraphRAG (7-step pipeline) | 1-step regex extraction | No conflict resolution, no schema alignment |
| "Embeddings on graph vertices" | Message.embedding (1024d LSM_VECTOR) | Aligned |
| "Write back to graph during execution" | MENTIONS + RELATES_TO on append | Aligned |
| Hybrid: vector + structured filters | vector.fuse() + source/role filters | Aligned |
| Cross-model query (graph+vector+text) | Search combines LIKE + vector.neighbors | Aligned |

### What makes this valuable

1. **Single-database architecture**: No more SQLite + vector DB + search engine — everything in ArcadeDB
2. **Graph enrichment on write**: Every message automatically extracts entities and builds knowledge graph
3. **Hybrid retrieval**: Dense vector + LIKE text search with RRF fusion
4. **Multilingual embeddings**: ru↔zh 0.93 cosine similarity
5. **Zero-config**: Factory auto-detects ArcadeDB vs SQLite
6. **Crash-safe**: Transactions with connection pool recovery

### What the agent NOW can do (that it couldn't before)

1. Search messages by meaning, not just keywords — "kubernetes deployment" finds Russian/Chinese versions
2. Find related sessions via shared entities — multi-hop graph traversal
3. Group search results by session — no duplicate sessions in results
4. Store session summaries as SearchMatter vertices for fast browsing
5. Use any embedder (local ONNX, OpenAI API, Ollama, custom URL)
6. Survive connection failures — pool auto-healing + transaction retry

### Risk Assessment (from production GraphRAG lessons)

| Risk | Mitigation |
|------|-----------|
| Entity extraction quality (85% required) | Regex-based extraction is ~60% accurate; should be replaced with LLM-based |
| Cost scaling (entity extraction every message) | No API cost for regex; LLM extraction would add ~$0.001/message |
| Graph size unbounded growth | Soft-delete + vacuum cleanup (TD-18); manual pruning needed |
| Pool corruption (ArcadeDB SNAPSHOT) | Fixed in stable ArcadeDB release; workaround: connection discard + pool rebuild |
| Cold start (embedder model load) | 37s for e5-large first time; cached on subsequent restarts |

### Next Steps (prioritized by impact)

1. **Stable ArcadeDB version** — fixes all SNAPSHOT workarounds (pool, SEARCH_INDEX, DELETE VERTEX)
2. **LLM-based entity extraction** — replaces regex with AIAgent self-extraction (higher quality)
3. **SearchMatter auto-creation** — `end_session()` triggers summary + embedding
4. **Production API auth** — `/v1/chat/completions` needs API key validation
5. **Sparse vector index** — SPLADE/BM25-style retrieval for exact-term matching (ArcadeDB v26.5.1+)
