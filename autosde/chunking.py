"""Chunking strategies for knowledge bases.

Each strategy turns a document's text into `Chunk`s. All of them keep chunks under
`chunk_tokens`, record the heading path in force where each chunk starts (so every
chunk is embedded with its context), and share the token estimate in knowledge.py.
`STRATEGIES` also carries the descriptions the Knowledge page shows.
"""

import math
import re

from autosde.knowledge import Chunk, chunk as structure_chunk, tokens

STRATEGIES = {
    "structure": {
        "name": "Structure-aware",
        "summary": "Follows headings, paragraphs and code definitions.",
        "how": "Splits at Markdown headings (and at function or class definitions in code), keeps "
               "paragraphs and fenced code whole, then packs blocks up to the chunk size. Each chunk "
               "starts with the tail of the previous one.",
        "best": "Docs with headings: guides, READMEs, runbooks, specs, and source code.",
        "tradeoff": "Uneven chunk sizes; flat text without headings behaves like recursive.",
        "uses": ["chunk_tokens", "overlap"],
    },
    "recursive": {
        "name": "Recursive",
        "summary": "Splits on paragraphs, then lines, sentences, words.",
        "how": "Tries the largest separator first (blank lines), and only splits a piece further "
               "(lines, then sentences, then words) when it is still too big. Pieces are then merged "
               "back up to the chunk size with overlap. The common default in LangChain.",
        "best": "Mixed or unknown documents; a solid general-purpose choice.",
        "tradeoff": "Ignores headings, so a chunk can span two sections.",
        "uses": ["chunk_tokens", "overlap"],
    },
    "fixed": {
        "name": "Fixed-size",
        "summary": "Equal windows of tokens, sliding with overlap.",
        "how": "Cuts the text into windows of exactly the chunk size, stepping forward by the size "
               "minus the overlap, and moves each cut to the nearest space so words stay whole.",
        "best": "Uniform text such as logs, transcripts or exports; predictable cost and size.",
        "tradeoff": "Cuts through sentences and ideas; relies on overlap to keep context.",
        "uses": ["chunk_tokens", "overlap"],
    },
    "sentence": {
        "name": "Sentence",
        "summary": "Whole sentences packed to size.",
        "how": "Splits into sentences, then packs consecutive sentences until the next one would "
               "exceed the chunk size. Overlap carries whole trailing sentences forward.",
        "best": "Prose: articles, policies, FAQs, meeting notes.",
        "tradeoff": "Sentence detection is heuristic; code and tables split poorly.",
        "uses": ["chunk_tokens", "overlap"],
    },
    "semantic": {
        "name": "Semantic",
        "summary": "Breaks where the meaning shifts, using embeddings.",
        "how": "Embeds every sentence (with its neighbours for context), measures how different "
               "each sentence is from the next, and starts a new chunk where the difference is in "
               "the top share set by the breakpoint percentile and clearly above the document's "
               "usual drift. Chunks still stop at the chunk size.",
        "best": "Long documents that drift between topics without clear headings.",
        "tradeoff": "Embeds every sentence while chunking (slower, more embedding tokens) and "
                    "results vary with the model.",
        "uses": ["chunk_tokens", "semantic_percentile"],
    },
}

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[`#*-])|\n{2,}")
MAX_SEMANTIC_SENTENCES = 3000


def _headings(text):
    """(offset, heading path) at every Markdown heading, for labelling chunks."""
    marks, path = [(0, "")], []
    for match in re.finditer(r"^(#{1,6})\s+(.+)$", text, flags=re.M):
        level = len(match[1])
        while path and path[-1][0] >= level:
            path.pop()
        path.append((level, match[2].strip()))
        marks.append((match.start(), " > ".join(t for _, t in path)))
    return marks


def _label(pieces, text, title):
    """Chunks from (start offset, text) pairs, labelled with the heading path at the start."""
    marks = _headings(text)
    chunks = []
    for start, piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        heading = next((path for offset, path in reversed(marks) if offset <= start), "")
        chunks.append(Chunk(len(chunks), " > ".join(p for p in (title, heading) if p), piece))
    return chunks


def fixed(text, *, title, chunk_tokens, overlap, **_):
    size = chunk_tokens * 4
    step = max(1, int(size * (1 - overlap)))
    pieces, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):  # move the cut back to a space so words stay whole
            space = text.rfind(" ", start + size // 2, end)
            end = space if space > start else end
        pieces.append((start, text[start:end]))
        if end >= len(text):
            break
        next_start = start + step if end - start >= step else end
        while next_start < len(text) and next_start > 0 and not text[next_start - 1].isspace():
            next_start += 1  # start the next window on a word
        start = min(next_start, end)
    return _label(pieces, text, title)


def _split(text, offset, size, separators):
    """Recursive split into (offset, piece) under `size` characters."""
    if len(text) <= size:
        return [(offset, text)]
    if not separators:
        return [(offset + i, text[i:i + size]) for i in range(0, len(text), size)]
    sep, rest = separators[0], separators[1:]
    parts = re.split(f"({re.escape(sep)})", text) if sep else list(text)
    out, cursor = [], offset
    for part in parts:
        if part == sep or not part:
            cursor += len(part)
            continue
        out += _split(part, cursor, size, rest) if len(part) > size else [(cursor, part)]
        cursor += len(part)
    return out


def _merge(pieces, text, size, overlap_chars):
    """Merge adjacent pieces up to `size` characters, carrying overlap from the source."""
    merged, start, end = [], None, None
    for offset, piece in pieces:
        if start is None:
            start, end = offset, offset + len(piece)
        elif offset + len(piece) - start <= size:
            end = offset + len(piece)
        else:
            merged.append((start, text[start:end]))
            back = max(start, end - overlap_chars)
            back = text.find(" ", back, end) + 1 or back  # begin the overlap on a word
            start = back if offset + len(piece) - back <= size else offset
            end = offset + len(piece)
    if start is not None:
        merged.append((start, text[start:end]))
    return merged


def recursive(text, *, title, chunk_tokens, overlap, **_):
    size = chunk_tokens * 4
    pieces = _split(text, 0, size, ["\n\n", "\n", ". ", " ", ""])
    return _label(_merge(pieces, text, size, int(size * overlap)), text, title)


def _sentences(text):
    out, cursor = [], 0
    for match in _SENTENCE.finditer(text):
        out.append((cursor, text[cursor:match.start()]))
        cursor = match.end()
    out.append((cursor, text[cursor:]))
    return [(o, s) for o, s in out if s.strip()]


def _pack(sentences, text, chunk_tokens, overlap_tokens, breaks=frozenset()):
    """Pack sentences into chunks; `breaks` holds sentence indexes that must start a chunk."""
    pieces, group = [], []
    for index, (offset, sentence) in enumerate(sentences):
        candidate = group + [(offset, sentence)]
        span = text[candidate[0][0]:offset + len(sentence)]
        if group and (index in breaks or tokens(span) > chunk_tokens):
            pieces.append((group[0][0], text[group[0][0]:group[-1][0] + len(group[-1][1])]))
            carry = []  # overlap: whole trailing sentences within the budget
            for prior in reversed(group):
                if index in breaks or sum(tokens(s) for _, s in carry + [prior]) > overlap_tokens:
                    break
                carry.insert(0, prior)
            group = carry
        if tokens(sentence) > chunk_tokens:  # one giant "sentence": fall back to fixed windows
            for start in range(0, len(sentence), chunk_tokens * 4):
                pieces.append((offset + start, sentence[start:start + chunk_tokens * 4]))
            group = []
            continue
        group.append((offset, sentence))
    if group:
        pieces.append((group[0][0], text[group[0][0]:group[-1][0] + len(group[-1][1])]))
    return pieces


def sentence(text, *, title, chunk_tokens, overlap, **_):
    pieces = _pack(_sentences(text), text, chunk_tokens, int(chunk_tokens * overlap))
    return _label(pieces, text, title)


def _cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


def semantic(text, *, title, chunk_tokens, semantic_percentile, embed, embed_model, dimensions, **_):
    """Break where adjacent sentences are least alike (by embedding distance)."""
    from autosde.knowledge import KnowledgeError

    sentences = _sentences(text)
    if len(sentences) > MAX_SEMANTIC_SENTENCES:
        raise KnowledgeError(f"Semantic chunking handles up to {MAX_SEMANTIC_SENTENCES} sentences; "
                             "use sentence or structure-aware chunking for this document.")
    if len(sentences) < 3:
        return _label(_pack(sentences, text, chunk_tokens, 0), text, title)
    windows = [" ".join(s for _, s in sentences[max(0, i - 1):i + 2])[:2000]
               for i in range(len(sentences))]  # each sentence with its neighbours
    vectors = []
    for start in range(0, len(windows), 64):
        got, _ = embed(windows[start:start + 64], embed_model, dimensions)
        vectors += got
    distances = [1 - _cosine(a, b) for a, b in zip(vectors, vectors[1:])]
    ranked = sorted(distances)
    threshold = ranked[min(len(ranked) - 1, int(len(ranked) * semantic_percentile / 100))]
    mean = sum(distances) / len(distances)
    spread = math.sqrt(sum((d - mean) ** 2 for d in distances) / len(distances))
    # A break must be in the top percentile and stand out from the document's usual drift.
    candidates = [i for i, d in enumerate(distances) if d >= threshold and d > mean + spread]
    breaks = set()
    for i in candidates:  # neighbour windows smear one shift over adjacent gaps: keep the peak
        if i - 1 in candidates and distances[i - 1] >= distances[i]:
            continue
        if i + 1 in candidates and distances[i + 1] > distances[i]:
            continue
        breaks.add(i + 1)
    return _label(_pack(sentences, text, chunk_tokens, 0, breaks), text, title)


def structure(text, *, title, chunk_tokens, overlap, code=False, **_):
    return structure_chunk(text, title=title, chunk_tokens=chunk_tokens, overlap=overlap, code=code)


FUNCTIONS = {"structure": structure, "recursive": recursive, "fixed": fixed,
             "sentence": sentence, "semantic": semantic}


def split(text, *, title, settings, code=False, embed=None):
    """Chunks for `text` with the knowledge base's strategy and parameters."""
    strategy = settings.get("strategy", "structure")
    return FUNCTIONS[strategy](
        text.replace("\r\n", "\n"), title=title, chunk_tokens=settings["chunk_tokens"],
        overlap=settings["overlap"], semantic_percentile=settings.get("semantic_percentile", 90),
        code=code, embed=embed, embed_model=settings["embed_model"],
        dimensions=settings["dimensions"])


def stats(chunks):
    sizes = [c.tokens for c in chunks] or [0]
    return {"chunks": len(chunks), "min_tokens": min(sizes), "max_tokens": max(sizes),
            "avg_tokens": round(sum(sizes) / len(sizes)), "total_tokens": sum(sizes)}
