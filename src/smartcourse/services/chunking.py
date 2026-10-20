"""Cutting lesson content into retrievable pieces. FR-11.

Pure text in, pure text out. No database, no models, no session - which is
why it can be tested by calling it with a string and reading the list that
comes back.

Why chunks exist at all: a chunk becomes one vector in Module 4, and a vector
is the *average* meaning of its text. One idea gives a sharp point; five give
a point that sits slightly near everything and close to nothing. The other
half is the prompt - a model can only read so much, so the assistant has to
be handed a few passages rather than a course.

So a chunk wants to be small enough to mean one thing and large enough to
make sense alone. Everything below is working out where that line sits.
"""

import re

# Roughly how many tokens a word costs in English prose.
#
# A token is a piece of a word - common words are one, longer ones split
# ("unbelievable" is three). Models count tokens, not words, which is why
# this exists at all.
#
# An estimate rather than a real tokenizer, deliberately. The right tokenizer
# is whichever the embedding model uses, and that is chosen in Module 4;
# installing one now would bet on a provider. See STATE.md.
TOKENS_PER_WORD = 1.3

# A blank line between paragraphs, however it was typed.
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")

# End of sentence: . ! or ? followed by whitespace. Crude - it splits
# "Dr. Smith" - and good enough, because sentence splitting is only the
# fallback for a paragraph that is already too big on its own.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def estimate_tokens(text: str) -> int:
    """About how many tokens this text costs.

    The one place counting happens, so Module 4 can swap in a real tokenizer
    by changing this function and nothing else.

    Rounded up and never zero: a chunk with no tokens would fail the
    ck_chunks_token_count_positive constraint, and any text at all costs
    something.
    """
    words = len(text.split())
    return max(1, round(words * TOKENS_PER_WORD))


def chunk_text(
    text: str | None, *, max_tokens: int, overlap_tokens: int
) -> list[str]:
    """Split text into pieces, none larger than max_tokens.

    A ceiling, not a target. Text that already fits comes back as a single
    chunk, untouched - short lessons are not padded or pointlessly cut.

    Three levels, each a fallback for the one before:

        paragraphs   the normal case. A paragraph is already one thought,
                     because whoever wrote it made it one
        sentences    when a single paragraph exceeds the ceiling
        words        when a single sentence does, which is rare and means
                     something unusual, but must not loop forever

    Cutting mid-sentence is avoided wherever possible: a chunk that begins
    half way through a thought embeds badly, because the embedding is then of
    a fragment.
    """
    if not text or not text.strip():
        return []

    pieces = _split_to_fit(text.strip(), max_tokens)
    return _assemble(pieces, max_tokens=max_tokens, overlap_tokens=overlap_tokens)


def _split_to_fit(text: str, max_tokens: int) -> list[tuple[str, str]]:
    """Break text into parts that each fit, keeping the largest natural unit.

    Each part carries the separator that should go *before* it when it is
    joined back to whatever precedes it: a blank line when it starts a new
    paragraph, a space when it continues one.

    Without that, sentences split out of a single paragraph get rejoined
    with blank lines and the stored chunk no longer matches the lesson - so
    a citation would show text the source does not contain.
    """
    parts: list[tuple[str, str]] = []

    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

            if estimate_tokens(paragraph) <= max_tokens:
                parts.append(("\n\n", paragraph))
                continue

        # Too big whole, so split it - but everything that comes out of one
        # paragraph belongs to it, and only the first piece starts a new one.
        pieces: list[str] = []
        for sentence in _SENTENCE_END.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if estimate_tokens(sentence) <= max_tokens:
                pieces.append(sentence)
            else:
                pieces.extend(_split_words(sentence, max_tokens))

        for index, piece in enumerate(pieces):
            parts.append(("\n\n" if index == 0 else " ", piece))

    return parts


def _split_words(sentence: str, max_tokens: int) -> list[str]:
    """Last resort: cut a single enormous sentence on word boundaries.

    Still never mid-word. A sentence this long is a bulleted list without
    blank lines, or text extracted from a PDF that lost its formatting.
    """
    words = sentence.split()
    per_chunk = max(1, int(max_tokens / TOKENS_PER_WORD))
    return [
        " ".join(words[i : i + per_chunk]) for i in range(0, len(words), per_chunk)
    ]


def _assemble(
    parts: list[tuple[str, str]], *, max_tokens: int, overlap_tokens: int
) -> list[str]:
    """Pack the parts into chunks, carrying an overlap across each cut.

    Greedy: keep adding parts while they fit, then start a new chunk
    beginning with the tail of the one just finished, so an idea that
    straddles the cut survives whole in at least one piece.

    Every size check measures the joined string that will actually be
    stored, never the sum of its parts' estimates. Those disagree, because
    Python rounds half to even: two five-word pieces estimate at 6 + 6 = 12,
    while the same ten words together estimate at 13. Summing produced
    chunks exactly one token over the ceiling, which a fuzz run across 400
    random documents found 47 times.
    """
    chunks: list[str] = []
    current = ""

    for separator, part in parts:
        candidate = f"{current}{separator}{part}" if current else part

        if current and estimate_tokens(candidate) > max_tokens:
            chunks.append(current)
            current = _start_next(
                current,
                separator=separator,
                part=part,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            )
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


def _start_next(
    previous: str,
    *,
    separator: str,
    part: str,
    max_tokens: int,
    overlap_tokens: int,
) -> str:
    """Open the next chunk with as much of the previous one's tail as fits.

    Whole sentences, taken from the end backwards, so the repeated text
    reads as something rather than starting mid-clause.

    Two limits, and both are checked against the real joined string:

        the overlap budget      how much repetition is worth paying for
        the ceiling             the promise the whole function makes

    The ceiling wins when they conflict, so a boundary can end up with no
    overlap at all. That is the right trade: losing the overlap once costs
    a little retrieval quality, while exceeding the ceiling risks the
    embedding model silently truncating the chunk - which loses the text
    entirely, without an error.
    """
    sentences = [s.strip() for s in _SENTENCE_END.split(previous) if s.strip()]
    kept: list[str] = []

    if overlap_tokens > 0:
        for sentence in reversed(sentences):
            candidate = [sentence, *kept]
            tail = " ".join(candidate)
            if estimate_tokens(tail) > overlap_tokens:
                break
            if estimate_tokens(f"{tail}{separator}{part}") > max_tokens:
                break
            kept = candidate

    tail = " ".join(kept)
    return f"{tail}{separator}{part}" if tail else part
