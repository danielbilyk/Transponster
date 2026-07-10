import unittest

from srt_polish import (
    fix_book_forms, fix_bookclub_branding, strip_final_period, fix_quotes,
    layout_cue, polish_srt, polish_text, is_korotulka_filename, parse_srt,
    segment_words, split_sentences_in_cue,
)


class BookFormsTest(unittest.TestCase):
    def test_all_declensions(self):
        cases = {
            "я читаю книгу": "я читаю книжку",
            "багато книг": "багато книжок",
            "у цій книзі": "у цій книжці",
            "з книгою в руках": "з книжкою в руках",
            "Книга року": "Книжка року",
            "книгам і книгами": "книжкам і книжками",
            "у книгах": "у книжках",
        }
        for src, want in cases.items():
            self.assertEqual(fix_book_forms(src), want, src)

    def test_derivatives_untouched(self):
        for text in ["книгарня на розі", "Книголюб", "бібліотека книгозбірні"]:
            self.assertEqual(fix_book_forms(text), text)

    def test_existing_knyzhka_untouched(self):
        self.assertEqual(fix_book_forms("книжка вже правильна"), "книжка вже правильна")


class BookclubBrandingTest(unittest.TestCase):
    def test_appends_brand(self):
        self.assertEqual(
            fix_bookclub_branding("приходьте на Книжковий клуб завтра"),
            "приходьте на Книжковий клуб від The Ukrainians Media завтра")

    def test_declined_forms(self):
        self.assertEqual(
            fix_bookclub_branding("учасники Книжкового клубу знають"),
            "учасники Книжкового клубу від The Ukrainians Media знають")

    def test_idempotent(self):
        once = fix_bookclub_branding("Книжковий клуб — це любов")
        self.assertEqual(fix_bookclub_branding(once), once)

    def test_idempotent_declined(self):
        # regex must not backtrack «клубі»→«клуб» and re-insert the brand
        for form in ["на Книжковому клубі", "до Книжкового клубу",
                     "з Книжковим клубом", "Книжковий клуб"]:
            once = fix_bookclub_branding(form)
            self.assertEqual(fix_bookclub_branding(once), once, form)


class FinalPeriodTest(unittest.TestCase):
    def test_lone_period_removed(self):
        self.assertEqual(strip_final_period("Ми не ставимо крапок."), "Ми не ставимо крапок")

    def test_ellipsis_and_marks_kept(self):
        self.assertEqual(strip_final_period("І тоді..."), "І тоді...")
        self.assertEqual(strip_final_period("Справді?"), "Справді?")
        self.assertEqual(strip_final_period("Так!"), "Так!")

    def test_abbreviation_kept(self):
        self.assertEqual(strip_final_period("та інші т."), "та інші т.")


class QuotesTest(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(fix_quotes('вона сказала "так" і пішла'),
                         "вона сказала «так» і пішла")


class LayoutTest(unittest.TestCase):
    def test_short_stays_one_line(self):
        self.assertEqual(layout_cue("коротка фраза"), "коротка фраза")

    def test_long_splits_two_lines_within_limit(self):
        out = layout_cue("це доволі довге речення яке точно не влазить в один рядок субтитрів")
        lines = out.split("\n")
        self.assertEqual(len(lines), 2)
        for l in lines:
            self.assertLessEqual(len(l), 40)

    def test_no_hanging_ne_at_line_end(self):
        # Olha's exact case shape: «…не / практикуєш» — «не» must go DOWN
        out = layout_cue("Ти з ним не стикаєшся не практикуєш бо часу нема зовсім")
        first = out.split("\n")[0]
        self.assertFalse(first.rstrip().endswith(" не"), out)

    def test_no_single_word_orphan(self):
        out = layout_cue("довше речення яке могло б лишити самотнє слово внизу")
        lines = out.split("\n")
        if len(lines) == 2:
            self.assertGreaterEqual(len(lines[1].split()), 2, out)


class CrossCueTest(unittest.TestCase):
    SRT = """1
00:00:01,000 --> 00:00:03,000
Ти з ним не стикаєшся, не практикуєш, бо

2
00:00:03,000 --> 00:00:05,000
часу на це просто немає.
"""

    def test_hanging_word_moves_to_next_cue(self):
        out = polish_srt(self.SRT)
        cues = parse_srt(out)
        self.assertFalse(cues[0]["text"].rstrip(",").endswith("бо"), cues[0]["text"])
        self.assertTrue(cues[1]["text"].startswith("бо"), cues[1]["text"])

    def test_final_periods_removed_everywhere(self):
        out = polish_srt(self.SRT)
        for cue in parse_srt(out):
            self.assertFalse(cue["text"].endswith("."), cue["text"])

    def test_idempotent(self):
        once = polish_srt(self.SRT)
        self.assertEqual(polish_srt(once), once)


class PolishTextIntegrationTest(unittest.TestCase):
    def test_all_rules_together(self):
        got = polish_text('Обговорюємо книгу на "Книжковому клубі".')
        self.assertEqual(got, "Обговорюємо книжку на «Книжковому клубі від The Ukrainians Media»")


class SegmentWordsTest(unittest.TestCase):
    @staticmethod
    def mk(sentence):
        words, ts = [], 0.0
        for w in sentence.split():
            words.append({"text": w, "start": ts, "end": ts + 0.28})
            ts += 0.3
        return words

    def test_sentence_tail_pulled_in(self):
        # «Озрени Кебо.» must NOT spill into the next cue
        ws = self.mk("Третя книжка — це «Сараєво для початківців» Озрени Кебо. "
                     "Це документально-художня історія про життя в Сараєво.")
        segs = segment_words(ws)
        texts = [" ".join(w["text"] for w in s) for s in segs]
        self.assertTrue(texts[0].endswith("Кебо."), texts)
        self.assertTrue(texts[1].startswith("Це"), texts)

    def test_never_two_sentences_in_one_cue(self):
        ws = self.mk("Перше речення тут. Друге речення тут. Третє довше речення тут є.")
        for s in segment_words(ws):
            inner = [w["text"] for w in s[:-1]]
            self.assertFalse(any(x.endswith(".") for x in inner), inner)

    def test_mid_sentence_cut_prefers_comma(self):
        ws = self.mk("Я всім, хто приходить в журналістику або кому цікаво, "
                     "як працює журналістика, я раджу прочитати цю книгу.")
        segs = segment_words(ws)
        texts = [" ".join(w["text"] for w in s) for s in segs]
        for txt in texts:
            self.assertFalse(txt.rstrip(",.").split()[-1] == "або", texts)

    def test_abo_is_hanging(self):
        from srt_polish import _is_hanging
        for w in ["або", "тобто", "якщо", "після"]:
            self.assertTrue(_is_hanging(w), w)


class SentenceSplitTest(unittest.TestCase):
    def test_split_on_internal_period(self):
        parts = split_sentences_in_cue("Озрени Кебо. Це документально-художня історія")
        self.assertEqual(parts, ["Озрени Кебо.", "Це документально-художня історія"])

    def test_no_split_without_capital(self):
        parts = split_sentences_in_cue("озрени кебо. це історія")
        self.assertEqual(len(parts), 1)

    def test_abbrev_not_split(self):
        parts = split_sentences_in_cue("та ін. Наступне речення")
        self.assertEqual(len(parts), 1)

    def test_polish_srt_splits_cue_with_interpolated_time(self):
        srt = ("1\n00:01:47,720 --> 00:01:51,420\n"
               "Озрени Кебо. Це документально-художня історія\n")
        out = polish_srt(srt)
        cues = parse_srt(out)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "Озрени Кебо")
        self.assertTrue(cues[1]["text"].startswith("Це"))
        self.assertTrue(out.startswith("1\n00:01:47,720 --> "))
        self.assertIn("--> 00:01:51,420", out)

    def test_no_newlines_in_cue_text(self):
        srt = ("1\n00:00:01,000 --> 00:00:04,000\n"
               "довше речення яке раніше розбивалось на два рядки а тепер ні\n")
        cues = parse_srt(polish_srt(srt))
        for c in cues:
            self.assertNotIn("\n", c["text"])


class NamingSchemeTest(unittest.TestCase):
    def test_korotulka_markers(self):
        for name in ["коротулька_чорноморець.mp4", "Чорноморець короткі.mov",
                     "korotulka-ep12.mp4", "ep12_kor.mp4", "kor ep12.wav",
                     "КОРОТУЛЬКИ фінал.mp4"]:
            self.assertTrue(is_korotulka_filename(name), name)

    def test_regular_files_not_marked(self):
        for name in ["інтерв'ю_грицак.mp4", "коридор запису.wav", "record.mp4",
                     "хор виступ.mp3", "субтитри_епізод.mp4", ""]:
            self.assertFalse(is_korotulka_filename(name), name)


if __name__ == "__main__":
    unittest.main()
