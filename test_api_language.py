"""API-side contract for forced-language transcription and drift reporting.

Anton's async pipeline (tu-anton-config) consumes these: `language_code` on
POST /api/transcribe forwards to ElevenLabs, and a completed job's result
carries `language_drift` with a ready-to-post note when auto-detect wandered
into Russian. The 🇺🇦 chat reaction and this API path must stay semantically
identical: forced run = fresh transcription, never a translation.
"""
import unittest

from pydantic import ValidationError

from api_transcribe import TranscribeRequest, artifact_base, language_drift_payload


def drifted_result() -> dict:
    # 3 Russian chunks vs 5 Ukrainian ones (>= MIN_RUSSIAN_CHUNKS, ratio 0.375)
    ru = "Это было очень интересно и мы обсуждали важные вещи весь эфир подряд. " * 12
    ua = "Це була дуже цікава розмова і ми обговорювали важливі речі цілу годину. " * 12
    words = []
    t = 0.0
    for chunk_text, start in ((ua, 0.0), (ru, 300.0), (ua, 600.0), (ru, 900.0),
                              (ua, 1200.0), (ru, 1500.0), (ua, 1800.0), (ua, 2100.0)):
        t = start
        for w in chunk_text.split():
            words.append({"text": w, "start": t, "end": t + 0.3, "type": "word"})
            t += 0.35
    return {"words": words}


class TranscribeRequestSchemaTest(unittest.TestCase):
    def base(self, **over):
        payload = {"file_url": "https://example.com/a.mp3", "filename": "a.mp3"}
        payload.update(over)
        return TranscribeRequest(**payload)

    def test_language_code_defaults_to_none(self):
        self.assertIsNone(self.base().language_code)

    def test_ukrainian_code_accepted(self):
        self.assertEqual(self.base(language_code="ukr").language_code, "ukr")

    def test_garbage_codes_rejected(self):
        for bad in ("ukrainian", "UA", "uk-UA", "", "u", "укр"):
            with self.assertRaises(ValidationError, msg=bad):
                self.base(language_code=bad)


class ArtifactBaseTest(unittest.TestCase):
    def test_forced_run_gets_language_suffix(self):
        # a rerun reusing ep17.txt would dedup against the OLD files in the
        # thread and silently re-deliver the auto-detected transcript
        self.assertEqual(artifact_base("ep17.mp3", "ukr"), "ep17-ukr")

    def test_default_run_keeps_plain_stem(self):
        self.assertEqual(artifact_base("ep17.mp3", None), "ep17")

    def test_mirrors_chat_reaction_suffix(self):
        # the 🇺🇦 reaction path emits {name}-ukr.* — API must match
        self.assertEqual(artifact_base("розмова з Ясею.mov", "ukr"), "розмова з Ясею-ukr")


class LanguageDriftPayloadTest(unittest.TestCase):
    def test_forced_run_never_nags(self):
        self.assertIsNone(language_drift_payload(drifted_result(), "ukr"))

    def test_clean_result_is_none(self):
        clean = {"text": "Це чиста українська розмова про важливі речі. " * 30}
        self.assertIsNone(language_drift_payload(clean, None))

    def test_drifted_result_carries_note_and_counts(self):
        payload = language_drift_payload(drifted_result(), None, "ep17.mp3")
        self.assertIsNotNone(payload)
        self.assertIn("`ep17.mp3`", payload["note"])  # which file, not "цей файл"
        self.assertGreaterEqual(payload["russian_chunks"], 2)
        self.assertGreater(payload["ukrainian_chunks"], 0)
        self.assertIn("заїхала російська", payload["note"])
        self.assertIn("примусовою українською", payload["note"])
        self.assertIn("не переклад", payload["note"])
        # first Russian stretch is timestamped for the human
        self.assertIn("десь від", payload["note"])

    def test_detector_crash_degrades_to_none(self):
        self.assertIsNone(language_drift_payload("not-a-dict", None))


if __name__ == "__main__":
    unittest.main()
