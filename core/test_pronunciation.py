"""Unit tests for core/pronunciation.py: the gates, the aligner, PER and the bands. No network, no database."""

import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from . import pronunciation as p

CONFIG = {
    "version": "test-1",
    "min_duration_s": 1.5,
    "wrong_sentence_min_overlap": 0.5,
    "bands": {"good": 0.15, "okay": 0.35},
}


def expected(*words):
    """[("hola", "o l a"), ...] → the aligner's input: [(word, [phonemes]), ...]."""
    return [(word, phonemes.split()) for word, phonemes in words]


class WrongSentenceGateTests(SimpleTestCase):
    def test_overlap_is_the_share_of_intended_words_found_in_the_transcript(self):
        self.assertEqual(p.word_overlap("hola cómo estás", "Hola, ¿cómo estás?"), 1.0)
        self.assertEqual(p.word_overlap("hola cómo estás amigo", "hola amigo"), 0.5)
        self.assertEqual(p.word_overlap("hola", "adiós"), 0.0)

    def test_repeated_words_only_count_as_often_as_they_were_meant(self):
        self.assertEqual(p.word_overlap("no no", "no"), 0.5)
        self.assertEqual(p.word_overlap("no", "no no no"), 1.0)

    def test_order_does_not_matter_accents_do(self):
        self.assertEqual(p.word_overlap("dos personas", "personas dos"), 1.0)
        self.assertEqual(p.word_overlap("más", "mas"), 0.0)

    def test_empty_intended_text_overlaps_nothing(self):
        self.assertEqual(p.word_overlap("", "hola"), 0.0)


class AlignerTests(SimpleTestCase):
    def ops(self, alignment):
        return [(o["op"], o["expected"], o["heard"]) for word in alignment for o in word["ops"]]

    def test_identical_is_all_matches(self):
        alignment = p.align(expected(("hola", "o l a")), "o l a".split())
        self.assertEqual(self.ops(alignment), [("match", "o", "o"), ("match", "l", "l"), ("match", "a", "a")])

    def test_substitution(self):
        alignment = p.align(expected(("bogotá", "b o ɣ o t a")), "b o ɡ o t a".split())
        self.assertEqual(self.ops(alignment)[2], ("sub", "ɣ", "ɡ"))
        self.assertEqual(p.counts(alignment), {"match": 5, "sub": 1, "ins": 0, "del": 0})

    def test_deletion_and_insertion(self):
        alignment = p.align(expected(("mesa", "m e s a")), "m s a a".split())
        # A dropped "e" and one extra "a" (either "a" may be the extra one), not two substitutions.
        self.assertEqual(p.counts(alignment), {"match": 3, "sub": 0, "ins": 1, "del": 1})
        self.assertEqual(self.ops(alignment)[:2], [("match", "m", "m"), ("del", "e", None)])

    def test_multi_character_phonemes_are_single_tokens(self):
        alignment = p.align(expected(("check", "tʃ ɛ k")), "ʃ ɛ k".split())
        self.assertEqual(self.ops(alignment)[0], ("sub", "tʃ", "ʃ"))

    def test_ops_are_grouped_by_intended_word(self):
        alignment = p.align(expected(("dos", "d o s"), ("mesas", "m e s a s")), "d o s m e s a".split())
        self.assertEqual([w["word"] for w in alignment], ["dos", "mesas"])
        self.assertEqual([o["op"] for o in alignment[0]["ops"]], ["match"] * 3)
        self.assertEqual([o["op"] for o in alignment[1]["ops"]], ["match"] * 4 + ["del"])

    def test_insertions_before_the_first_phoneme_belong_to_the_first_word(self):
        alignment = p.align(expected(("hola", "o l a")), "e o l a".split())
        self.assertEqual(alignment[0]["ops"][0], {"op": "ins", "expected": None, "heard": "e"})

    def test_nothing_heard_is_all_deletions(self):
        alignment = p.align(expected(("sí", "s i")), [])
        self.assertEqual(p.counts(alignment), {"match": 0, "sub": 0, "ins": 0, "del": 2})


class PhonemeErrorRateTests(SimpleTestCase):
    def per(self, intended, heard):
        return p.phoneme_error_rate(p.align(expected(*intended), heard.split()))

    def test_perfect_is_zero(self):
        self.assertEqual(self.per([("hola", "o l a")], "o l a"), 0.0)

    def test_errors_over_expected_length(self):
        # One substitution in six expected phonemes.
        self.assertAlmostEqual(self.per([("bogotá", "b o ɣ o t a")], "b o ɡ o t a"), 1 / 6)
        # A deletion and an insertion in four.
        self.assertEqual(self.per([("mesa", "m e s a")], "m s a a"), 0.5)

    def test_many_insertions_can_exceed_one(self):
        self.assertEqual(self.per([("sí", "s i")], "s i a e o u"), 2.0)

    def test_nothing_expected_has_no_rate(self):
        self.assertIsNone(p.phoneme_error_rate([]))


class BandTests(SimpleTestCase):
    def test_thresholds_come_from_the_config(self):
        self.assertEqual(p.band(0.0, CONFIG), "good")
        self.assertEqual(p.band(0.15, CONFIG), "good")
        self.assertEqual(p.band(0.16, CONFIG), "okay")
        self.assertEqual(p.band(0.35, CONFIG), "okay")
        self.assertEqual(p.band(0.36, CONFIG), "off")

    def test_changing_the_config_moves_the_bands(self):
        stricter = {**CONFIG, "bands": {"good": 0.05, "okay": 0.10}}
        self.assertEqual(p.band(0.12, stricter), "off")


class ConfigTests(SimpleTestCase):
    def write(self, config):
        path = Path(tempfile.mkdtemp()) / "scoring.json"
        path.write_text(json.dumps(config))
        return path

    def test_the_shipped_config_is_valid_and_versioned(self):
        config = p.load_config()
        self.assertTrue(config["version"])
        self.assertLess(config["bands"]["good"], config["bands"]["okay"])

    def test_rejects_a_config_missing_keys_or_with_bands_out_of_order(self):
        missing = {k: v for k, v in CONFIG.items() if k != "version"}
        with self.assertRaisesMessage(p.ConfigError, "version"):
            p.load_config(self.write(missing))
        swapped = {**CONFIG, "bands": {"good": 0.4, "okay": 0.2}}
        with self.assertRaisesMessage(p.ConfigError, "good"):
            p.load_config(self.write(swapped))


class ScoreTests(SimpleTestCase):
    """The whole decision for one clip, given what the pipeline heard (no network)."""

    def score(self, **overrides):
        args = {
            "intended_text": "hola amigo",
            "language": "es",
            "transcript": "Hola, amigo.",
            "heard_phonemes": "o l a a m i ɣ o",
            "duration_s": 2.0,
            "config": CONFIG,
            "expected_words": expected(("hola", "o l a"), ("amigo", "a m i ɣ o")),
        }
        return p.score(**{**args, **overrides})

    def test_a_good_clip_is_scored(self):
        result = self.score()
        self.assertEqual((result["status"], result["per"], result["band"]), ("scored", 0.0, "good"))
        self.assertEqual([w["word"] for w in result["alignment"]], ["hola", "amigo"])

    def test_clips_under_the_minimum_duration_are_too_short(self):
        result = self.score(duration_s=1.49)
        self.assertEqual((result["status"], result["per"], result["band"]), ("too_short", None, None))

    def test_the_wrong_sentence_is_gated_before_scoring(self):
        result = self.score(transcript="el perro corre")
        self.assertEqual((result["status"], result["per"], result["band"]), ("wrong_sentence", None, None))
        self.assertEqual(result["overlap"], 0.0)

    def test_at_the_overlap_threshold_it_is_still_scored(self):
        self.assertEqual(self.score(transcript="hola")["status"], "scored")


class ExpectedPhonemesTests(SimpleTestCase):
    """Real espeak-ng (installed in the image), word by word, in the model's own symbols."""

    def test_spanish_word_by_word(self):
        self.assertEqual(
            p.expected_phonemes("¡Hola, cómo estás!", "es"),
            [("hola", ["o", "l", "a"]), ("cómo", ["k", "o", "m", "o"]), ("estás", ["e", "s", "t", "a", "s"])],
        )

    def test_english(self):
        self.assertEqual(p.expected_phonemes("check", "en"), [("check", ["tʃ", "ɛ", "k"])])
