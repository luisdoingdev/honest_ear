"""Tests for core/bench.py: Spearman, the run summary, and a full run through the admin (services mocked)."""

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from . import bench, pronunciation, scoring
from .models import EvalClip, EvalResult, EvalRun


class SpearmanTests(SimpleTestCase):
    def test_monotonic_is_plus_or_minus_one(self):
        self.assertEqual(bench.spearman([0.1, 0.2, 0.9], [0, 1, 2]), 1.0)
        self.assertEqual(bench.spearman([0.9, 0.2, 0.1], [0, 1, 2]), -1.0)

    def test_ties_get_average_ranks(self):
        # Labels tied in pairs; PER rises with them.
        self.assertEqual(bench.spearman([0.05, 0.1, 0.3, 0.4], [0, 0, 2, 2]), 0.894)

    def test_undefined_without_enough_or_varied_data(self):
        self.assertIsNone(bench.spearman([0.1], [0]))
        self.assertIsNone(bench.spearman([0.1, 0.5], [1, 1]))


def result(status="scored", human="good", band="good", per=0.1, subs=()):
    ops = [{"op": "sub", "expected": e, "heard": h} for e, h in subs]
    return EvalResult(status=status, human_label=human, predicted_band=band, per=per, alignment=[{"word": "x", "ops": ops}])


class SummaryTests(SimpleTestCase):
    def test_agreement_confusion_and_substitutions(self):
        summary = bench.summarize(
            [
                result(human="good", band="good", per=0.05, subs=[("ɣ", "ɡ")]),
                result(human="okay", band="good", per=0.12, subs=[("ɣ", "ɡ"), ("β", "b")]),
                result(human="off", band="off", per=0.6),
                result(status="wrong_sentence", human="off", band="", per=None),
                result(human="", band="okay", per=0.2),
            ]
        )
        self.assertEqual(summary["clips"], 5)
        self.assertEqual(summary["judged"], 3, "only scored clips with a human label count")
        self.assertEqual(summary["agreement"], 66.7)
        self.assertEqual(summary["confusion"]["okay"]["good"], 1)
        self.assertEqual(summary["confusion"]["off"]["off"], 1)
        self.assertEqual(summary["statuses"]["wrong_sentence"], 1)
        self.assertEqual(summary["top_substitutions"][0], ("ɣ→ɡ", 2))

    def test_nothing_judged(self):
        summary = bench.summarize([result(status="too_short", per=None, band="")])
        self.assertIsNone(summary["agreement"])
        self.assertIsNone(summary["spearman"])


@override_settings(DEEPGRAM_API_KEY="test-key", SCORER_URL="https://scorer.example/", SCORER_SECRET="s")
class RunBenchTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.heard = {}
        for target, name in [("core.scoring.phonemes_for", "scorer"), ("core.transcription.transcribe", "deepgram")]:
            patcher = mock.patch(target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        # Each clip's audio is its own key, so the fakes answer per clip.
        self.scorer.side_effect = lambda audio, _: self.heard[audio]["scorer"]
        self.deepgram.side_effect = lambda audio, _: self.heard[audio]["deepgram"]

    def clip(self, name, text, label, transcript, phonemes, duration=2.0):
        clip = EvalClip(intended_text=text, language="es", human_label=label)
        clip.audio.save(f"{name}.wav", ContentFile(name.encode()), save=True)
        self.heard[name.encode()] = {
            "deepgram": transcript,
            "scorer": {"phonemes": phonemes, "inference_ms": 1.0, "duration_s": duration},
        }
        return clip

    def test_a_run_scores_gates_and_summarizes_with_a_config_snapshot(self):
        clips = [
            self.clip("good", "hola amigo", "good", "Hola amigo", "o l a a m i ɣ o"),
            self.clip("off", "hola amigo", "off", "hola amigo", "e l a i m e k u"),
            self.clip("wrong", "hola amigo", "off", "el perro corre", "e l p e r o"),
            self.clip("short", "hola amigo", "okay", "hola", "o l a", duration=0.8),
        ]
        run = bench.run_bench(clips)

        self.assertEqual(run.config, pronunciation.load_config())
        self.assertEqual(run.config_version, str(run.config["version"]))
        by_name = {r.clip.audio.name.split("/")[-1].split(".")[0]: r for r in run.results.all()}
        self.assertEqual((by_name["good"].status, by_name["good"].per, by_name["good"].predicted_band), ("scored", 0.0, "good"))
        self.assertEqual(by_name["off"].predicted_band, "off")
        self.assertEqual(by_name["wrong"].status, "wrong_sentence")
        self.assertEqual(by_name["short"].status, "too_short")
        self.assertEqual(by_name["good"].alignment[0]["word"], "hola")
        self.assertEqual(run.summary["judged"], 2)
        self.assertEqual(run.summary["agreement"], 100.0)

    def test_the_label_at_run_time_is_kept(self):
        clip = self.clip("a", "hola", "good", "hola", "o l a")
        run = bench.run_bench([clip])
        EvalClip.objects.filter(pk=clip.pk).update(human_label="off")
        self.assertEqual(run.results.get().human_label, "good")

    def test_unfinished_clips_and_service_failures_become_errors_not_crashes(self):
        blank = EvalClip(intended_text="", human_label="")
        blank.audio.save("blank.wav", ContentFile(b"blank"), save=True)
        down = self.clip("down", "hola", "good", "hola", "o l a")
        self.heard[b"down"]["scorer"] = None
        self.scorer.side_effect = lambda audio, _: (
            (_ for _ in ()).throw(scoring.ScorerError("Scorer timed out after 10s (cold start?)"))
            if audio == b"down" else self.heard[audio]["scorer"]
        )

        run = bench.run_bench([blank, down])

        errors = {r.clip_id: r.error for r in run.results.all()}
        self.assertIn("No intended text", errors[blank.pk])
        self.assertIn("Scorer timed out", errors[down.pk])
        self.assertEqual(run.summary["statuses"]["error"], 2)

    def test_run_bench_admin_action(self):
        clip = self.clip("a", "hola", "good", "hola", "o l a")
        self.client.force_login(get_user_model().objects.create_superuser("bench", "b@example.com", "pw-123456"))

        res = self.client.post(
            "/admin/core/evalclip/", {"action": "run_bench", "_selected_action": [clip.pk]}, follow=True
        )

        run = EvalRun.objects.get()
        self.assertContains(res, f"Run {run.pk} done: 1 clip(s), 1 scored and labeled, agreement 100.0%")
        self.assertContains(self.client.get("/admin/core/evalrun/"), "Agreement %")
