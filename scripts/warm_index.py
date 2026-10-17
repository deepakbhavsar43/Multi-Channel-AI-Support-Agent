"""Download the embedding model and build the vector index ahead of time.

Run at Docker build time so the first user message isn't slowed by a model
download, and so a blocked download fails the build instead of production.
"""
from app.config import get_settings
from app.rag.retriever import build_retriever

if __name__ == "__main__":
    r = build_retriever(get_settings())
    print(f"retriever={r.kind} chunks={len(r.chunks_by_id)}")
    hit = r.search("late cancellation fee", 1)[0]
    print(f"sanity: {hit.chunk.id} score={hit.score}")
