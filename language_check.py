"""language_check — spot ElevenLabs drifting into Russian mid-transcript.

Deterministic, dependency-free. When no language_code is sent, ElevenLabs
predicts the language itself, and on weak audio that prediction wanders between
Ukrainian and Russian — so a transcript comes back partly in the wrong one.

The signal is orthographic, not lexical: four Cyrillic letters exist only in
Russian (ы ъ э ё) and four only in Ukrainian (і ї є ґ). No word lists, no
stemming, no guessing at surzhyk.

Two things make this precise rather than merely loud:

  1. It classifies CHUNKS, not the whole file. A transcript that goes Russian
     for its last fifth still reads as overwhelmingly Ukrainian in aggregate,
     which is exactly the case a global ratio misses.
  2. It fires only on a MIX — Ukrainian chunks *and* Russian chunks in the same
     transcript. A recording that is Russian end to end is far more likely to be
     genuinely Russian-language audio than a misdetection, and nagging about it
     would be wrong.

Nothing here re-transcribes anything; the caller decides what to do with a hit.
"""
from __future__ import annotations

RUSSIAN_ONLY = frozenset("ыъэё")
UKRAINIAN_ONLY = frozenset("іїєґ")

# Words per chunk. Roughly a long paragraph — small enough to catch a drift
# that lasts a minute, large enough that a single quoted phrase can't swing it.
CHUNK_WORDS = 50
# Skip chunks too short to judge (trailing partials, музичні вставки).
MIN_CHUNK_LETTERS = 60
# Marker letters needed before a chunk counts as either language.
MIN_MARKERS = 2
# A transcript is reported only above both of these.
MIN_RUSSIAN_CHUNKS = 2
MIN_RUSSIAN_RATIO = 0.10


def classify_chunk(text: str) -> str | None:
    """'ru', 'ua', or None when the chunk carries too little evidence."""
    lowered = text.lower()
    if sum(1 for c in lowered if c.isalpha()) < MIN_CHUNK_LETTERS:
        return None

    russian = sum(1 for c in lowered if c in RUSSIAN_ONLY)
    ukrainian = sum(1 for c in lowered if c in UKRAINIAN_ONLY)

    if russian >= MIN_MARKERS and russian > ukrainian:
        return "ru"
    if ukrainian >= MIN_MARKERS and ukrainian > russian:
        return "ua"
    return None


def extract_words(transcription_result: dict) -> list[dict]:
    """Word entries with timings, or [] when the result has none."""
    # The result comes straight off the API, so treat every field as optional
    # rather than merely absent — a null here must read as "no data", not raise.
    return [
        w for w in (transcription_result.get("words") or [])
        if w.get("type") == "word" and (w.get("text") or "").strip()
    ]


def _chunk_from_words(words: list[dict]) -> list[tuple[str, float | None]]:
    """Group words into (text, start_time) chunks."""
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        group = words[i:i + CHUNK_WORDS]
        text = " ".join(w["text"].strip() for w in group)
        chunks.append((text, group[0].get("start")))
    return chunks


def _chunk_from_text(text: str) -> list[tuple[str, float | None]]:
    """Fallback when the result carries no word timings."""
    tokens = text.split()
    return [
        (" ".join(tokens[i:i + CHUNK_WORDS]), None)
        for i in range(0, len(tokens), CHUNK_WORDS)
    ]


def detect_russian_drift(transcription_result: dict) -> dict | None:
    """
    Report a Ukrainian transcript that went partly Russian, else None.

    Returns {'russian_chunks', 'ukrainian_chunks', 'ratio', 'first_start'}.
    'first_start' is the start time in seconds of the first Russian stretch, or
    None when the result had no word timings to attribute it to.
    """
    words = extract_words(transcription_result)
    if words:
        chunks = _chunk_from_words(words)
    else:
        chunks = _chunk_from_text(transcription_result.get("text") or "")

    russian = 0
    ukrainian = 0
    first_start = None
    for text, start in chunks:
        verdict = classify_chunk(text)
        if verdict == "ru":
            russian += 1
            if first_start is None:
                first_start = start
        elif verdict == "ua":
            ukrainian += 1

    classified = russian + ukrainian
    if not classified:
        return None

    # No Ukrainian anywhere means this is simply a Russian (or other) recording,
    # not a detection that slipped.
    if ukrainian == 0:
        return None

    ratio = russian / classified
    if russian < MIN_RUSSIAN_CHUNKS or ratio < MIN_RUSSIAN_RATIO:
        return None

    return {
        "russian_chunks": russian,
        "ukrainian_chunks": ukrainian,
        "ratio": ratio,
        "first_start": first_start,
    }


def format_offset(seconds: float | None) -> str | None:
    """Seconds → 'MM:SS' or 'H:MM:SS' for a human-readable pointer."""
    if seconds is None:
        return None
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes:02}:{secs:02}"
