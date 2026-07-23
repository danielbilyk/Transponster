"""Tests for language_check — Russian drift detection."""
import pytest

from language_check import (
    CHUNK_WORDS,
    classify_chunk,
    detect_russian_drift,
    extract_words,
    format_offset,
)

UKRAINIAN = (
    "Ми починаємо нашу розмову про те як українські медіа працюють під час "
    "великої війни це історія про людей які щодня виходять на роботу попри "
    "все її неможливо розповісти в одному епізоді тому ми зробили цілу серію "
    "наші герої це журналісти редактори та фотографи які залишилися вдома"
)

RUSSIAN = (
    "Мы начинаем наш разговор о том как эти медиа работают сейчас это история "
    "про людей которые каждый день выходят на работу несмотря ни на что её "
    "невозможно рассказать в одном эпизоде поэтому мы сделали целую серию "
    "наши герои это журналисты редакторы и фотографы которые остались дома"
)

ENGLISH = (
    "We begin our conversation about how independent media keep working during "
    "the war this is a story about people who show up every single day no "
    "matter what it cannot be told in one episode so we made a whole series "
    "our heroes are reporters editors and photographers who stayed at home"
)


def words_from(text, start=0.0, step=0.5):
    """Build ElevenLabs-shaped word entries with timings."""
    return [
        {"type": "word", "text": token, "start": start + i * step, "end": start + (i + 1) * step}
        for i, token in enumerate(text.split())
    ]


def repeat_to_chunks(text, chunk_count):
    """A text long enough to fill exactly `chunk_count` chunks."""
    tokens = text.split()
    needed = CHUNK_WORDS * chunk_count
    return " ".join((tokens * (needed // len(tokens) + 1))[:needed])


def result_of(*texts):
    """A transcription result whose words run through `texts` in order."""
    words = []
    clock = 0.0
    for text in texts:
        words.extend(words_from(text, start=clock))
        clock += len(text.split()) * 0.5
    return {"words": words, "text": " ".join(texts)}


class TestClassifyChunk:
    def test_ukrainian(self):
        assert classify_chunk(UKRAINIAN) == "ua"

    def test_russian(self):
        assert classify_chunk(RUSSIAN) == "ru"

    def test_english_has_no_verdict(self):
        assert classify_chunk(ENGLISH) is None

    def test_too_short_to_judge(self):
        assert classify_chunk("Привіт, як справи?") is None

    def test_ukrainian_quoting_russian_stays_ukrainian(self):
        # A speaker quoting a Russian phrase must not flip the whole chunk.
        assert classify_chunk(UKRAINIAN + " він сказав мы этого не делали") == "ua"


class TestDetectRussianDrift:
    def test_clean_ukrainian_is_silent(self):
        assert detect_russian_drift(result_of(repeat_to_chunks(UKRAINIAN, 8))) is None

    def test_fully_russian_recording_is_silent(self):
        # Russian end to end is almost certainly genuinely Russian audio,
        # not a misdetection — warning here would be wrong.
        assert detect_russian_drift(result_of(repeat_to_chunks(RUSSIAN, 8))) is None

    def test_english_is_silent(self):
        assert detect_russian_drift(result_of(repeat_to_chunks(ENGLISH, 8))) is None

    def test_empty_result_is_silent(self):
        assert detect_russian_drift({"words": [], "text": ""}) is None

    @pytest.mark.parametrize("result", [
        {},
        {"words": None},
        {"text": None},
        {"words": None, "text": None},
        {"words": [{"type": "word"}]},
        {"words": [{"type": "word", "text": None}]},
    ])
    def test_malformed_result_is_silent_not_fatal(self, result):
        # The result comes off the API — a missing or null field must read as
        # "no data" rather than take down an otherwise fine transcription.
        assert detect_russian_drift(result) is None

    def test_drift_at_the_tail_is_caught(self):
        report = detect_russian_drift(result_of(
            repeat_to_chunks(UKRAINIAN, 6),
            repeat_to_chunks(RUSSIAN, 3),
        ))
        assert report is not None
        assert report["russian_chunks"] == 3
        assert report["ukrainian_chunks"] == 6
        assert report["ratio"] == pytest.approx(1 / 3)

    def test_drift_reports_where_it_starts(self):
        report = detect_russian_drift(result_of(
            repeat_to_chunks(UKRAINIAN, 6),
            repeat_to_chunks(RUSSIAN, 3),
        ))
        # 6 chunks × 50 words × 0.5s = 150s in.
        assert report["first_start"] == pytest.approx(150.0)

    def test_drift_in_the_middle_is_caught(self):
        report = detect_russian_drift(result_of(
            repeat_to_chunks(UKRAINIAN, 4),
            repeat_to_chunks(RUSSIAN, 2),
            repeat_to_chunks(UKRAINIAN, 4),
        ))
        assert report is not None
        assert report["russian_chunks"] == 2
        assert report["first_start"] == pytest.approx(100.0)

    def test_single_russian_chunk_is_below_threshold(self):
        # One stretch is more likely a quote or a name than a real drift.
        assert detect_russian_drift(result_of(
            repeat_to_chunks(UKRAINIAN, 10),
            repeat_to_chunks(RUSSIAN, 1),
        )) is None

    def test_negligible_share_is_below_threshold(self):
        # Two Russian chunks in a very long transcript stay under the ratio.
        assert detect_russian_drift(result_of(
            repeat_to_chunks(UKRAINIAN, 25),
            repeat_to_chunks(RUSSIAN, 2),
        )) is None

    def test_falls_back_to_plain_text_without_timings(self):
        report = detect_russian_drift({
            "text": repeat_to_chunks(UKRAINIAN, 6) + " " + repeat_to_chunks(RUSSIAN, 3)
        })
        assert report is not None
        assert report["russian_chunks"] == 3
        assert report["first_start"] is None


class TestExtractWords:
    def test_skips_non_words_and_blanks(self):
        result = {"words": [
            {"type": "word", "text": "привіт"},
            {"type": "spacing", "text": " "},
            {"type": "audio_event", "text": "(laughter)"},
            {"type": "word", "text": "   "},
        ]}
        assert [w["text"] for w in extract_words(result)] == ["привіт"]


class TestFormatOffset:
    @pytest.mark.parametrize("seconds,expected", [
        (0.0, "00:00"),
        (9.9, "00:09"),
        (150.0, "02:30"),
        (3599.0, "59:59"),
        (3600.0, "1:00:00"),
        (7325.0, "2:02:05"),
        (None, None),
    ])
    def test_formats(self, seconds, expected):
        assert format_offset(seconds) == expected
