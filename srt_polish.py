"""srt_polish — editorial polish for коротульки subtitles (The Ukrainians Media).

Deterministic, dependency-free. The SINGLE source of truth for the коротульки
SRT rules; a vendored copy lives in tu-anton-config/workspace/scripts/ for the
Anton skill — keep both in sync (header carries a version tag).

Rules (from Dani + Olha Krysa, 2026-07-10):
  1. книга → книжка, у правильному відмінку (explicit word-form table, whole
     words only — «книгарня» stays)
  2. every «Книжковий клуб» mention → «Книжковий клуб від The Ukrainians Media»
     (idempotent)
  3. no sentence-final periods (…, ?!, abbreviations untouched)
  4. ≤2 lines per cue, ~40 chars each
  5. no hanging particles/conjunctions/prepositions at line end («не», «бо»,
     «і», «в»… — вони міняють сенс і висять; переносити далі). Cross-cue: a cue
     must not END on such a word either — it moves to the next cue.
  6. no single-word orphan as the last line of a cue
  7. ukrainian «» quotes

VERSION: 2026-07-10.2
"""
from __future__ import annotations

import re

# ── rule 1: книга → книжка, form → form ──────────────────────────────────────
# Explicit table — no stemming guesses. Plural genitive «книг» → «книжок» etc.
BOOK_FORMS = {
    "книга": "книжка", "книги": "книжки", "книзі": "книжці", "книгу": "книжку",
    "книгою": "книжкою", "книго": "книжко",
    "книг": "книжок", "книгам": "книжкам", "книгами": "книжками",
    "книгах": "книжках",
}
_BOOK_RE = re.compile(
    r"\b(" + "|".join(sorted(BOOK_FORMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _match_case(src: str, repl: str) -> str:
    if src.isupper():
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def fix_book_forms(text: str) -> str:
    return _BOOK_RE.sub(lambda m: _match_case(m.group(1), BOOK_FORMS[m.group(1).lower()]), text)


# ── rule 2: Книжковий клуб branding ──────────────────────────────────────────
_KK_RE = re.compile(
    r"(Книжков(?:ий|ого|ому|им)\s+клуб(?:у|і|ом)?)\b"
    r"(?!\s+від\s+The\s+Ukrainians\s+Media)",
    re.IGNORECASE,
)


def fix_bookclub_branding(text: str) -> str:
    return _KK_RE.sub(lambda m: f"{m.group(1)} від The Ukrainians Media", text)


# ── rule 3: strip sentence-final periods ─────────────────────────────────────
# A LONE period at end of the cue text goes away; «…», «?», «!», "?!" stay.
# Abbreviation guard: keep the period when it follows a single letter («т.»)
# or a known short abbreviation.
_ABBREV_TAILS = {"т", "і", "ін", "напр", "грн", "стор", "р", "рр", "св"}


def strip_final_period(text: str) -> str:
    t = text.rstrip()
    if not t.endswith(".") or t.endswith(("..", "?.", "!.")):
        return t.rstrip(".") if t.endswith(("?.", "!.")) else t
    last_word = t[:-1].rsplit(" ", 1)[-1].lower().rstrip(".")
    if last_word in _ABBREV_TAILS or len(last_word) == 1:
        return t
    return t[:-1].rstrip()


# ── rule 7: ukrainian quotes ─────────────────────────────────────────────────
def fix_quotes(text: str) -> str:
    out, opened = [], False
    for ch in text:
        if ch == '"':
            out.append("«" if not opened else "»")
            opened = not opened
        else:
            out.append(ch)
    return "".join(out)


# ── rules 4-6: layout ────────────────────────────────────────────────────────
# words that must never end a line: particles, conjunctions, prepositions,
# negation — «не» наприкінці рядка міняє сенс (Olha Krysa, 2026-07-10)
HANGING_WORDS = {
    "не", "ні", "ані", "бо", "і", "й", "та", "а", "але", "чи", "як", "що",
    "щоб", "коли", "де", "то", "же", "ж", "би", "б", "хоч", "аж", "у", "в",
    "з", "із", "зі", "на", "до", "від", "об", "по", "за", "під", "над", "при",
    "без", "про", "через", "для", "між", "крізь", "це", "той", "ця", "ці",
}

MAX_LINE = 40


def _is_hanging(word: str) -> bool:
    return word.lower().strip("«»„“…,.!?;:—-–()") in HANGING_WORDS


def layout_cue(text: str, max_line: int = MAX_LINE) -> str:
    """One cue text → 1-2 lines: ≤max_line chars, no hanging word at the end of
    line 1, no single-word orphan on line 2 (rebalance or merge)."""
    words = text.split()
    if not words:
        return ""
    one_line = " ".join(words)
    if len(one_line) <= max_line or len(words) == 1:
        return one_line

    # find the split closest to the middle that satisfies both line limits
    best = None
    target = len(one_line) / 2
    for i in range(1, len(words)):
        l1 = " ".join(words[:i])
        l2 = " ".join(words[i:])
        if len(l1) > max_line or len(l2) > max_line:
            continue
        score = abs(len(l1) - target)
        if _is_hanging(words[i - 1]):
            score += 100          # a hanging word at end of line 1 → last resort
        if len(words[i:]) == 1:
            score += 50           # single-word orphan on line 2 → strongly avoid
        if best is None or score < best[0]:
            best = (score, i)
    if best is None:  # nothing fits both limits — split at midpoint anyway
        i = max(1, len(words) // 2)
        return " ".join(words[:i]) + "\n" + " ".join(words[i:])
    return " ".join(words[:best[1]]) + "\n" + " ".join(words[best[1]:])


# ── SRT parsing / serialization ──────────────────────────────────────────────
_TS_LINE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")


def parse_srt(srt_text: str) -> list[dict]:
    """→ [{'index': str, 'time': str, 'text': str}] — tolerant of extra/missing
    blank lines. A digit-only line whose next non-blank line is a timestamp is
    a cue INDEX, not subtitle text (a subtitle legitimately can be «2026»)."""
    lines = [l.lstrip("﻿").rstrip() for l in srt_text.splitlines()]

    def next_nonblank(i: int) -> str:
        for j in range(i + 1, len(lines)):
            if lines[j].strip():
                return lines[j]
        return ""

    cues, cur = [], None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _TS_LINE.match(line):
            if cur:
                cues.append(cur)
            cur = {"index": str(len(cues) + 1), "time": stripped, "text": ""}
            continue
        if stripped.isdigit() and _TS_LINE.match(next_nonblank(i)):
            continue  # cue index — even mid-text of the previous cue
        if cur is None or not stripped:
            continue
        cur["text"] = (cur["text"] + " " + stripped).strip()
    if cur:
        cues.append(cur)
    return cues


def serialize_srt(cues: list[dict]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(c["time"])
        out.append(c["text"])
        out.append("")
    return "\n".join(out) + ("\n" if out else "")


# ── text-level polish (single cue, no layout) ────────────────────────────────
def polish_text(text: str) -> str:
    t = fix_quotes(text)
    t = fix_book_forms(t)
    t = fix_bookclub_branding(t)
    t = re.sub(r"\s+([,.!?…;:»])", r"\1", t)   # «слово ,» → «слово,» (STT artifacts)
    t = re.sub(r"«\s+", "«", t)
    t = strip_final_period(t)
    return re.sub(r"\s+", " ", t).strip()


# ── tiny-cue merge (word-level segments) ─────────────────────────────────────
def merge_tiny_segments(segments: list, min_words: int = 2) -> list:
    """A cue of a single word («нема» flashing for half a second) is as bad as
    an orphan line. Merge it into the previous segment (or the next, if it is
    the very first). Works on word-dict lists from create_srt_from_json."""
    def word_count(seg):
        return sum(1 for w in seg if re.search(r"[\wа-яіїєґА-ЯІЇЄҐ]", w["text"]))

    out = []
    for seg in segments:
        if out and word_count(seg) < min_words:
            out[-1] = out[-1] + seg
        else:
            out.append(seg)
    # first segment tiny → fold into the following one
    if len(out) >= 2 and word_count(out[0]) < min_words:
        out[1] = out[0] + out[1]
        out.pop(0)
    return out


# ── whole-file polish ────────────────────────────────────────────────────────
def polish_srt(srt_text: str, max_line: int = MAX_LINE) -> str:
    """Arbitrary SRT → коротульки rules. Cross-cue: a cue must not END on a
    hanging word — the word moves to the FRONT of the next cue (approximate
    timing; exact fix happens at generation time in Transponster)."""
    cues = parse_srt(srt_text)
    # cross-cue pass first (needs raw word streams)
    for i in range(len(cues) - 1):
        words = cues[i]["text"].split()
        moved = []
        while len(words) > 1 and _is_hanging(words[-1]):
            moved.insert(0, words.pop())
        if moved:
            cues[i]["text"] = " ".join(words)
            cues[i + 1]["text"] = (" ".join(moved) + " " + cues[i + 1]["text"]).strip()
    # per-cue text polish + layout
    polished = []
    for c in cues:
        text = polish_text(c["text"])
        if not text:
            continue  # cue emptied (e.g. was a lone particle) — drop it
        polished.append({"time": c["time"], "text": layout_cue(text, max_line)})
    return serialize_srt(polished)


# ── naming scheme: which files get this treatment ───────────────────────────
# Only files Dani (or anyone) explicitly marks as коротульки; extends the
# existing filename convention («субтитри»/«subtitles» → srt mode).
_KOR_RE = re.compile(r"(коротул|коротк|korotul|korotk|(?:^|[\s_\-\.])kor(?:$|[\s_\-\.]))",
                     re.IGNORECASE)


def is_korotulka_filename(filename: str) -> bool:
    return bool(_KOR_RE.search(filename or ""))
