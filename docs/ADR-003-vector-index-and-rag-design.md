# ADR-003 — Vector index and RAG design

**Status:** Accepted · **Date:** Sprint 3 · **Decider:** Nikhil Vijayapuri

---

## Context

M3 adds per-tenant retrieval: documents are chunked, every chunk is embedded, and a
question is answered from its nearest chunks. Five choices were made in EAP-28..32 that
are expensive to reverse. They are recorded here so they are not re-litigated by accident.

## Decisions

### 1. Where vectors live — a column on `document_chunks`, not a separate vector store

| Option | Isolation | Consistency | Ops |
|---|---|---|---|
| `vector` column on `document_chunks` (pgvector) | **Same RLS as every other table** | Text and vector in one transaction | One database |
| Dedicated store (Pinecone, Qdrant, Weaviate) | Re-implemented per store | Two writes that must stay in sync | Second service, second bill |

Chosen: pgvector. ADR-002's guarantee — forget the tenant filter, get zero rows — extends
to similarity search for free, and EAP-35 proves that it did. Ingest writes text and vector
in one transaction, so no "chunk exists, vector missing" state can exist. Accepted cost:
Postgres ANN is slower than a dedicated engine past tens of millions of vectors. No tenant
is anywhere near that.

### 2. Dimension — 768, fixed

The Gemini embedding model emits a longer vector; we request `output_dimensionality=768`
and L2-normalise the result, because truncation leaves a non-unit vector and that skews
cosine. 768 is a quarter of the full-width storage and scan cost; the recall cost of the
narrower width is reported as small and is unmeasured on this corpus — accepted for now.
The number is baked into `vector(768)` on the column and into every stored row. Vectors of
different widths, or from different models, are never comparable, so changing either is a
migration plus a full re-embed (§5).

### 3. Distance — cosine (`<=>`), ordered on the bare operator

Stored vectors are unit length, so cosine, L2 and inner product rank identically. Cosine is
chosen because `1 - distance` reads as a similarity on a fixed scale, which makes the floor
(`DEFAULT_MIN_SIMILARITY = 0.35`) a number a human can reason about. The query orders by
the bare operator — `ORDER BY embedding <=> :q` — never by an expression wrapping it,
because only the bare operator can be served by a vector index later.

### 4. Index — none yet; HNSW when measurement says so

| Option | Build | Recall | Fit today |
|---|---|---|---|
| No index (sequential scan) | — | Exact | **Yes.** RLS plus the `(tenant_id, document_id)` index already shrink the scan to one tenant's rows |
| IVFFlat | Trains centroids on the rows present at build time | Permanently poor if built on an empty or small table | No |
| HNSW | Incremental, no training step | High, tunable with `ef_search` | Later |

Trigger to add HNSW, whichever comes first: p95 retrieval latency above 200 ms, or 100k
chunk rows. Measured, not guessed — the numbers that caused it get recorded here. When
added: `USING hnsw (embedding vector_cosine_ops)` — the ops class must match `<=>`.

### 5. What a re-embed costs

Changing the model or the width means a new `vector(N)` column (not an in-place alter),
every chunk re-embedded in bounded batches — 100k chunks at 100 per call is 1,000 API
calls — any index rebuilt, then a cut-over, because old and new vectors must never meet in
one query. Nothing in the schema records which model produced a row. **Follow-up:** add an
`embedding_model` column to `document_chunks` before a second model ever exists.

## Consequences

- Isolation for vectors is inherited, not built: one mechanism, one proof (EAP-35).
- Retrieval is exact today. HNSW makes it approximate; the isolation test must still pass,
  and a restrictive RLS filter under an ANN index can return fewer than `k` rows —
  pgvector's iterative scan (`hnsw.iterative_scan`) exists for exactly this. Evaluate then.
- Everything that ever compares vectors — retrieval, a future reranker's input, a future
  dedup — must share one model, width and normalisation. The `Embedder` boundary (EAP-28)
  is the single place that guarantees it.
- A zero vector has no direction; cosine against it is NaN and the row never matches
  anything. `GeminiEmbedder` rejects it as a bug, not data.