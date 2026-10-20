"""Cutting lesson content into chunks. FR-11.

No database here. chunk_text takes a string and returns a list of strings,
so these call it directly - which is the point of keeping it a pure
function rather than something that reaches for a session.

Small ceilings are used throughout so the examples stay readable. The real
one is 400, and nothing about the behaviour depends on the number.
"""

from smartcourse.services.chunking import chunk_text, estimate_tokens

LESSON = """When you look for a name in a phone book, you don't start at page one. You open it in the middle, see whether the name comes before or after, and throw half the book away. Then you do it again.

Binary search is that idea written down. The list must be sorted. You look at the middle item. If it's what you want, you're done.

That halving is why it's fast. A list of a million items takes about twenty steps, not a million. We write this as O(log n) time."""


# --- the ceiling is a ceiling ----------------------------------------------


def test_text_that_already_fits_is_not_cut() -> None:
    """A short lesson is already a good chunk. Nothing is gained by splitting
    it, and a target - rather than a ceiling - would have done."""
    result = chunk_text("One short line.", max_tokens=400, overlap_tokens=60)

    assert result == ["One short line."]


def test_a_whole_lesson_fits_under_the_real_ceiling() -> None:
    """A-06 puts a lesson at 500 words, about 650 tokens, so a real lesson
    splits into two or three. This one is shorter and should come back
    whole."""
    result = chunk_text(LESSON, max_tokens=400, overlap_tokens=60)

    assert len(result) == 1


def test_no_chunk_exceeds_the_ceiling() -> None:
    """The one promise the whole thing makes."""
    result = chunk_text(LESSON, max_tokens=60, overlap_tokens=0)

    assert result
    assert all(estimate_tokens(chunk) <= 60 for chunk in result)


# --- nothing in, nothing out -----------------------------------------------


def test_empty_sources_produce_no_chunks() -> None:
    """lessons.content is nullable - a lesson can exist with nothing in it
    yet, and publishing one must not fail or store an empty chunk. An empty
    chunk would also break ck_chunks_token_count_positive."""
    for empty in (None, "", "   ", "\n\n  \n"):
        assert chunk_text(empty, max_tokens=400, overlap_tokens=60) == []


# --- where it cuts ---------------------------------------------------------


def test_it_cuts_on_paragraph_breaks() -> None:
    """A paragraph is already one thought, because whoever wrote it made it
    one. Following that costs nothing and avoids chunks that start half way
    through a sentence."""
    text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph."

    result = chunk_text(text, max_tokens=4, overlap_tokens=0)

    assert result == [
        "First paragraph here.",
        "Second paragraph here.",
        "Third paragraph.",
    ]


def test_an_oversized_paragraph_falls_back_to_sentences() -> None:
    """One paragraph bigger than the ceiling cannot be kept whole, so the
    next natural unit down is used."""
    text = "One two three four. Five six seven eight. Nine ten eleven twelve."

    result = chunk_text(text, max_tokens=6, overlap_tokens=0)

    assert len(result) == 3
    assert result[0] == "One two three four."


def test_an_oversized_sentence_falls_back_to_words() -> None:
    """A sentence longer than the ceiling is unusual - a bulleted list with
    no blank lines, or a PDF that lost its formatting - but it must still
    terminate, and must not cut mid-word."""
    text = " ".join(f"word{i}" for i in range(40))

    result = chunk_text(text, max_tokens=10, overlap_tokens=0)

    assert len(result) > 1
    assert all(estimate_tokens(chunk) <= 10 for chunk in result)
    # Every word survives, in order, exactly once.
    assert " ".join(result) == text


def test_sentences_from_one_paragraph_rejoin_with_spaces() -> None:
    """The stored chunk has to be a faithful slice of the lesson.

    An earlier version joined every part with a blank line, so sentences
    split out of a single paragraph came back looking like separate
    paragraphs - and a citation would then show text the lesson does not
    contain.
    """
    text = "Alpha one two. Beta three four. Gamma five six."

    result = chunk_text(text, max_tokens=8, overlap_tokens=0)

    assert all("\n" not in chunk for chunk in result)
    assert result[0] == "Alpha one two. Beta three four."


def test_paragraph_breaks_survive_inside_a_chunk() -> None:
    """Two paragraphs packed into one chunk keep the blank line between
    them, for the same reason."""
    text = "First para.\n\nSecond para."

    result = chunk_text(text, max_tokens=400, overlap_tokens=0)

    assert result == ["First para.\n\nSecond para."]


# --- overlap ---------------------------------------------------------------


def test_overlap_carries_the_end_of_one_chunk_into_the_next() -> None:
    """An idea straddling a cut is incomplete in both pieces and whole in
    neither. The overlap means it survives in at least one."""
    text = "Alpha one. Bravo two. Charlie three. Delta four. Echo five."

    with_overlap = chunk_text(text, max_tokens=8, overlap_tokens=4)

    assert len(with_overlap) > 1
    # The second chunk begins with something the first one ended with.
    assert with_overlap[1].split(".")[0] in with_overlap[0]


def test_zero_overlap_turns_it_off() -> None:
    """Nothing is repeated, so the chunks joined back together are the
    original text."""
    text = "Alpha one. Bravo two. Charlie three. Delta four."

    result = chunk_text(text, max_tokens=6, overlap_tokens=0)

    assert " ".join(result) == text


def test_overlap_does_not_stop_it_terminating() -> None:
    """A chunk starts with the tail of the one before, so the tail counts
    against its budget. If the overlap were allowed to approach the ceiling
    there would be no room left for new content and it would loop."""
    text = " ".join(f"sentence number {i}." for i in range(60))

    result = chunk_text(text, max_tokens=20, overlap_tokens=15)

    assert len(result) < 60
    assert all(estimate_tokens(chunk) <= 20 for chunk in result)


# --- counting --------------------------------------------------------------


def test_token_estimate_is_never_zero() -> None:
    """chunks.token_count has a CHECK that it is positive, so any text at all
    has to cost something."""
    assert estimate_tokens("a") >= 1
    assert estimate_tokens("one two three") >= 3


# --- regressions, both found by fuzzing rather than by reading -------------


def test_the_overlap_never_pushes_a_chunk_over_the_ceiling() -> None:
    """The first version let it, badly.

    A chunk is opened with the tail of the one before, and the first part is
    then added without re-checking. A ceiling of 20 with an overlap of 15
    gave a 13-token tail plus a 13-token part: 26, with the limit set to 20.
    Every chunk after the first was over.

    The ceiling wins over the overlap when the two conflict. Losing the
    overlap at one boundary costs a little retrieval quality; exceeding the
    ceiling risks the embedding model silently truncating the text, which
    loses it entirely and without an error.
    """
    text = " ".join(
        f"alpha bravo charlie delta echo foxtrot golf hotel number {i}."
        for i in range(8)
    )

    result = chunk_text(text, max_tokens=20, overlap_tokens=15)

    assert len(result) > 1
    assert all(estimate_tokens(chunk) <= 20 for chunk in result)


def test_sizes_are_measured_on_the_joined_text_not_summed() -> None:
    """Python rounds half to even, so estimates do not add up.

    Two five-word pieces estimate at 6 + 6 = 12. The same ten words joined
    estimate at 13. Checking the sum while storing the join produced chunks
    exactly one token over - found 47 times in 400 random documents, and
    never by reading the code.
    """
    five = "alpha bravo charlie delta echo"
    assert estimate_tokens(five) * 2 < estimate_tokens(f"{five} {five}")

    text = ". ".join([five] * 12) + "."
    for ceiling in range(6, 30):
        result = chunk_text(text, max_tokens=ceiling, overlap_tokens=2)
        assert all(estimate_tokens(chunk) <= ceiling for chunk in result), ceiling


def test_no_text_is_lost() -> None:
    """With no overlap nothing is repeated, so the chunks put back together
    are the original words in the original order. A chunker that quietly
    dropped a paragraph would pass every other test here."""
    text = (
        "Alpha one two three. Bravo four five six.\n\n"
        "Charlie seven eight. Delta nine ten eleven twelve thirteen."
    )

    for ceiling in range(4, 40):
        result = chunk_text(text, max_tokens=ceiling, overlap_tokens=0)
        assert " ".join(result).split() == text.split(), ceiling
