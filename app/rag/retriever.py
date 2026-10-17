"""Document ingestion and retrieval.

Docs are Markdown files split on `##` headings, so every chunk is one
self-contained policy/FAQ section with a stable id like
`03_appointment_policy#no-shows`. The agent must cite these ids.

Two backends:
  * dense — fastembed (local ONNX model, cosine similarity, no API key)
  * bm25  — pure-Python lexical fallback (also used by the offline tests)
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9₹]+")
_STOP = set(
    "a an the and or of to in on for is are was be do does i my me you your we our "
    "it this that with at by from can how what when where which who will would should "
    "have has please tell about any there their them us".split()
)


@dataclass
class Chunk:
    id: str
    doc: str
    title: str
    section: str
    text: str

    def render(self) -> str:
        return f"{self.title} › {self.section}\n{self.text}"


@dataclass
class Hit:
    chunk: Chunk
    score: float


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_chunks(docs_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(docs_dir).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        title_match = re.search(r"^# (.+)$", raw, re.M)
        title = title_match.group(1).strip() if title_match else path.stem
        parts = re.split(r"^## (.+)$", raw, flags=re.M)
        intro = parts[0].replace(title_match.group(0), "") if title_match else parts[0]
        if intro.strip():
            chunks.append(Chunk(f"{path.stem}#intro", path.stem, title, "Overview", intro.strip()))
        for heading, body in zip(parts[1::2], parts[2::2]):
            if body.strip():
                chunks.append(Chunk(f"{path.stem}#{_slug(heading)}", path.stem, title,
                                    heading.strip(), body.strip()))
    if not chunks:
        raise RuntimeError(f"No markdown docs found in {docs_dir}")
    return chunks


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if len(t) > 1 and t not in _STOP]


class BM25Index:
    kind = "bm25"

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [Counter(_tokens(c.render())) for c in chunks]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / len(self.lengths)
        df = Counter(term for d in self.docs for term in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int) -> list[Hit]:
        terms = _tokens(query)
        scored = []
        for chunk, doc, length in zip(self.chunks, self.docs, self.lengths):
            score = 0.0
            for t in terms:
                tf = doc.get(t, 0)
                if tf:
                    norm = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg_len))
                    score += self.idf.get(t, 0.0) * norm
            scored.append(Hit(chunk, round(score, 4)))
        scored.sort(key=lambda h: h.score, reverse=True)
        return [h for h in scored[:k] if h.score > 0]


class DenseIndex:
    kind = "dense"

    def __init__(self, chunks: list[Chunk], model_name: str, cache_path: Path | None = None):
        import numpy as np
        from fastembed import TextEmbedding

        self._np = np
        self.chunks = chunks
        self.model = TextEmbedding(model_name)
        vectors = self._load_cache(cache_path, model_name, chunks)
        if vectors is None:
            vectors = np.array(list(self.model.embed([c.render() for c in chunks])), dtype="float32")
            if cache_path:
                cache_path.write_text(json.dumps({
                    "model": model_name,
                    "chunks": [asdict(c) for c in chunks],
                    "vectors": vectors.round(6).tolist(),
                }))
        self.matrix = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def _load_cache(self, path, model_name, chunks):
        if not path or not Path(path).exists():
            return None
        try:
            data = json.loads(Path(path).read_text())
            if data["model"] == model_name and data["chunks"] == [asdict(c) for c in chunks]:
                return self._np.array(data["vectors"], dtype="float32")
        except (ValueError, KeyError):
            pass
        return None  # docs changed -> re-embed

    def search(self, query: str, k: int) -> list[Hit]:
        np = self._np
        q = np.array(next(iter(self.model.query_embed(query))), dtype="float32")
        q /= np.linalg.norm(q)
        sims = self.matrix @ q
        order = np.argsort(-sims)[:k]
        return [Hit(self.chunks[i], round(float(sims[i]), 4)) for i in order]


class Retriever:
    """Facade used by the agent. Knows its own relevance threshold."""

    def __init__(self, index, min_score: float):
        self.index = index
        self.min_score = min_score
        self.chunks_by_id = {c.id: c for c in index.chunks}

    @property
    def kind(self) -> str:
        return self.index.kind

    def search(self, query: str, k: int) -> list[Hit]:
        return self.index.search(query, k)


def build_retriever(settings) -> Retriever:
    chunks = load_chunks(settings.docs_dir)
    if settings.retriever == "dense":
        try:
            index = DenseIndex(chunks, settings.embedding_model, Path(settings.index_path))
            logger.info("retriever ready", extra={"kind": "dense", "chunks": len(chunks)})
            return Retriever(index, settings.min_score_dense)
        except Exception as exc:  # model download blocked, no onnxruntime, ...
            logger.warning("dense retriever unavailable, falling back to bm25",
                           extra={"error": repr(exc)})
    index = BM25Index(chunks)
    logger.info("retriever ready", extra={"kind": "bm25", "chunks": len(chunks)})
    return Retriever(index, settings.min_score_bm25)
