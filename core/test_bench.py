"""Tests for core/bench.py: Spearman, the run summary, and a full run through the admin (services mocked)."""

import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
import httpx
from django.test import SimpleTestCase, TestCase, override_settings

from . import bench, pronunciation, scoring, transcription
from .transcription import TranscriptionError
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


@override_settings(SCORER_URL="https://scorer.example/", SCORER_SECRET="s", TRANSCRIBER_URL="http://transcriber:8000/")
class RunBenchTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.heard = {}
        for target, name in [
            ("core.scoring.phonemes_for", "scorer"),
            ("core.transcription.transcribe", "whisper"),
            ("core.transcription.model_name", "model"),
        ]:
            patcher = mock.patch(target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        # Each clip's audio is its own key, so the fakes answer per clip.
        self.scorer.side_effect = lambda audio, _: self.heard[audio]["scorer"]
        self.whisper.side_effect = lambda audio, _, language: (self.heard[audio]["whisper"], language)
        self.model.return_value = "faster-whisper-medium-int8"

    def clip(self, name, text, label, transcript, phonemes, duration=2.0):
        clip = EvalClip(intended_text=text, language="es", human_label=label)
        clip.audio.save(f"{name}.wav", ContentFile(name.encode()), save=True)
        self.heard[name.encode()] = {
            "whisper": transcript,
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

        self.assertEqual(run.config, {**pronunciation.load_config(), "transcriber": "local: faster-whisper-medium-int8"})
        self.assertEqual(run.config_version, str(run.config["version"]))
        by_name = {r.clip.audio.name.split("/")[-1].split(".")[0]: r for r in run.results.all()}
        self.assertEqual((by_name["good"].status, by_name["good"].per, by_name["good"].predicted_band), ("scored", 0.0, "good"))
        self.assertEqual(by_name["off"].predicted_band, "off")
        self.assertEqual(by_name["wrong"].status, "wrong_sentence")
        self.assertEqual(by_name["short"].status, "too_short")
        self.assertEqual(by_name["good"].alignment[0]["word"], "hola")
        self.assertEqual(run.summary["judged"], 2)
        self.assertEqual(run.summary["agreement"], 100.0)
        # Whisper is told each clip's language instead of detecting it.
        self.assertEqual({c.args[2] for c in self.whisper.call_args_list}, {"es"})

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


class ClipDetailTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(get_user_model().objects.create_superuser("bench", "b@example.com", "pw-123456"))
        self.clip = EvalClip(intended_text="hola bogotá", language="es", human_label="good")
        self.clip.audio.save("c.wav", ContentFile(b"x"), save=True)

    def page(self):
        return self.client.get(f"/admin/core/evalclip/{self.clip.pk}/change/")

    def test_before_any_run(self):
        self.assertContains(self.page(), "Not run yet")

    def test_latest_alignment_per_word_with_mismatches_marked(self):
        old = EvalRun.objects.create(config_version="1", config={})
        EvalResult.objects.create(run=old, clip=self.clip, status="scored", per=0.9, predicted_band="off", alignment=[])
        run = EvalRun.objects.create(config_version="1", config={})
        EvalResult.objects.create(
            run=run, clip=self.clip, human_label="good", status="scored", per=1 / 9, predicted_band="good",
            alignment=[
                {"word": "hola", "ops": [{"op": "match", "expected": "o", "heard": "o"}, {"op": "del", "expected": "l", "heard": None}]},
                {"word": "<b>bogotá</b>", "ops": [{"op": "sub", "expected": "ɣ", "heard": "ɡ"}, {"op": "ins", "expected": None, "heard": "a"}]},
            ],
        )
        page = self.page()
        self.assertContains(page, "core/admin/eval.css")
        self.assertContains(page, f"run {run.pk}")
        self.assertContains(page, "PER <strong>0.111</strong> → predicted <strong>Good</strong>")
        self.assertContains(page, '<span class="ev-op ev-sub"><span class="ev-exp">ɣ</span><span class="ev-heard">ɡ</span></span>', html=False)
        self.assertContains(page, '<span class="ev-op ev-del"><span class="ev-exp">l</span><span class="ev-heard"><span class="ev-gap">–</span></span></span>', html=False)
        self.assertContains(page, "&lt;b&gt;bogotá&lt;/b&gt;", msg_prefix="words are escaped")

    def test_gated_results_say_why(self):
        run = EvalRun.objects.create(config_version="1", config={})
        EvalResult.objects.create(run=run, clip=self.clip, status="wrong_sentence", alignment=[])
        self.assertContains(self.page(), "<strong>Wrong sentence</strong>", html=False)


class ReportTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.run_ = EvalRun.objects.create(config_version="1", config={})
        rows = [
            ("good one", "good", "good", 0.05),
            ("heard as off", "good", "off", 0.7),
            ("slightly off", "okay", "good", 0.1),
            ("truly off", "off", "off", 0.8),
        ]
        results = []
        for text, human, band, per in rows:
            clip = EvalClip(intended_text=text, human_label=human)
            clip.audio.save(f"{text}.wav", ContentFile(b"x"), save=True)
            alignment = [{"word": "w", "ops": [{"op": "sub", "expected": "ɣ", "heard": "ɡ"}]}]
            results.append(EvalResult.objects.create(
                run=self.run_, clip=clip, human_label=human, status="scored", per=per, predicted_band=band, alignment=alignment,
            ))
        self.run_.summary = bench.summarize(results)
        self.run_.save()
        self.url = f"/admin/core/evalrun/{self.run_.pk}/report/"

    def test_only_staff_can_see_it(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_report_page(self):
        self.client.force_login(get_user_model().objects.create_superuser("bench", "b@example.com", "pw-123456"))
        page = self.client.get(self.url)

        self.assertContains(page, "50.0%")
        self.assertContains(page, "Human ↓ / Predicted →")
        body = page.content.decode()
        # The worst disagreement (good heard as off, two bands apart) comes before the one-band miss.
        self.assertLess(body.index("heard as off"), body.index("slightly off"))
        self.assertContains(page, "2 bands")
        self.assertNotIn("good one</a>", body, "agreements are not listed as disagreements")
        clip = EvalClip.objects.get(intended_text="heard as off")
        self.assertContains(page, f"/admin/core/evalclip/{clip.pk}/change/")
        self.assertContains(page, "ɣ→ɡ")
        self.assertContains(self.client.get("/admin/core/evalrun/"), f'href="{self.url}"')


class WriteEvalSummaryTests(TestCase):
    def readme(self, text):
        path = Path(tempfile.mkdtemp()) / "README.md"
        path.write_text(text)
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        return path

    def make_run(self, agreement):
        summary = bench.summarize([result(human="good", band="good", per=0.05, subs=[("ɣ", "ɡ")])])
        summary["agreement"] = agreement
        return EvalRun.objects.create(config_version="1", config={}, summary=summary)

    def test_adds_an_evals_section_then_replaces_only_its_own_block(self):
        path = self.readme("# Honest Ear\n\nIntro.\n")
        self.make_run(100.0)
        call_command("write_eval_summary", readme=str(path), stdout=StringIO())
        first = path.read_text()
        self.assertIn("## Evals", first)
        self.assertIn("**Agreement** (predicted band = human label): 100.0%", first)
        self.assertIn("`ɣ→ɡ` ×1", first)

        path.write_text(first + "\n## Later section\n\nKept.\n")
        self.make_run(50.0)
        call_command("write_eval_summary", readme=str(path), stdout=StringIO())
        second = path.read_text()
        self.assertIn("50.0%", second)
        self.assertNotIn("100.0%", second)
        self.assertEqual(second.count("## Evals"), 1)
        self.assertIn("## Later section\n\nKept.", second)

    def test_without_runs_it_says_so(self):
        with self.assertRaisesMessage(CommandError, "No eval runs yet"):
            call_command("write_eval_summary", readme=str(self.readme("x")), stdout=StringIO())


@override_settings(TRANSCRIBER_URL="http://transcriber:8000/", SCORER_SECRET="shared", TRANSCRIBER_TIMEOUT_SECONDS=45)
@mock.patch("core.transcription.httpx.post")
class TranscriberClientTests(SimpleTestCase):
    def reply(self, status=200, body=None):
        body = body if body is not None else {"transcript": "hola amigo", "language": "es", "model": "faster-whisper-medium-int8"}
        return httpx.Response(status, json=body, request=httpx.Request("POST", "http://transcriber:8000/"))

    def test_sends_the_clip_language_and_secret(self, post):
        post.return_value = self.reply()
        self.assertEqual(transcription.transcribe(b"clip", "audio/wav", "es"), ("hola amigo", "es"))
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args, ("http://transcriber:8000/",))
        self.assertEqual(kwargs["params"], {"language": "es"})
        self.assertEqual(kwargs["headers"], {"Content-Type": "audio/wav", "X-Scorer-Secret": "shared"})
        self.assertEqual((kwargs["content"], kwargs["timeout"]), (b"clip", 45))

    def test_without_a_language_whisper_detects_it(self, post):
        post.return_value = self.reply(body={"transcript": "hello", "language": "en"})
        self.assertEqual(transcription.transcribe(b"clip", "audio/wav"), ("hello", "en"))
        self.assertEqual(post.call_args.kwargs["params"], {})

    def test_failures_become_transcription_errors(self, post):
        for outcome, message in [
            (httpx.ReadTimeout("slow"), "timed out after 45s"),
            (httpx.ConnectError("down"), "request failed: ConnectError"),
            (self.reply(status=401, body={}), "rejected the secret"),
            (self.reply(status=422, body={}), "HTTP 422"),
            (self.reply(body={"nope": 1}), "had no transcript"),
        ]:
            with self.subTest(message=message):
                post.reset_mock(side_effect=True, return_value=True)
                post.side_effect, post.return_value = (outcome, None) if isinstance(outcome, Exception) else (None, outcome)
                with self.assertRaisesMessage(TranscriptionError, message):
                    transcription.transcribe(b"x", "audio/wav", "es")

    @override_settings(SCORER_SECRET="")
    def test_not_configured_without_the_shared_secret(self, post):
        self.assertFalse(transcription.is_configured())


@override_settings(TRANSCRIBER_URL="http://transcriber:8000/", SCORER_URL="https://scorer.example/", SCORER_SECRET="")
class BenchNotConfiguredTests(TestCase):
    def test_a_missing_transcriber_is_an_error_result(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media), mock.patch("core.transcription.model_name", return_value="m"):
            clip = EvalClip(intended_text="hola", language="es", human_label="good")
            clip.audio.save("b.wav", ContentFile(b"b"), save=True)
            self.assertIn("Transcriber not configured", bench.run_bench([clip]).results.get().error)
