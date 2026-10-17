"""Print best-match scores for in-scope vs out-of-scope questions so you can
set MIN_SCORE_DENSE / MIN_SCORE_BM25. Pick a threshold between the lowest
in-scope score and the highest out-of-scope score.

    python -m scripts.calibrate
"""
from app.config import get_settings
from app.rag.retriever import build_retriever

IN_SCOPE = [
    "How much is teeth cleaning?", "What is the late cancellation fee?", "Are you open on Sunday?",
    "Do you accept insurance?", "Can I pay braces in EMI?", "Do you do implants?",
    "Is there parking?", "What should I avoid after whitening?", "Do you treat children?",
    "How long do braces take?", "Which languages do you speak?", "How do refunds work?",
]
OUT_OF_SCOPE = [
    "Who won the cricket world cup?", "What's the weather tomorrow?", "Write me a poem",
    "What's the capital of France?", "Can you recommend a good laptop?", "How do I cook biryani?",
]

if __name__ == "__main__":
    r = build_retriever(get_settings())
    print(f"retriever={r.kind}  current threshold={r.min_score}\n")
    for label, qs in (("IN", IN_SCOPE), ("OUT", OUT_OF_SCOPE)):
        for q in qs:
            hits = r.search(q, 1)
            top = hits[0] if hits else None
            print(f"{label:3} {top.score if top else 0:7.3f}  {q:45} -> {top.chunk.id if top else '-'}")
