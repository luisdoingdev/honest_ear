from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.bench import LABEL_RANK
from core.models import EvalRun

START = "<!-- evals:start (written by `manage.py write_eval_summary`; edits inside are replaced) -->"
END = "<!-- evals:end -->"


def render(run):
    s = run.summary
    statuses = s["statuses"]
    lines = [
        f"Latest run: **{run}**, {s['clips']} clips "
        f"({statuses['scored']} scored, {statuses['wrong_sentence']} wrong sentence, "
        f"{statuses['too_short']} too short, {statuses['error']} error). "
        f"Metrics use the {s['judged']} scored clips with a human label.",
        "",
        f"- **Agreement** (predicted band = human label): {'—' if s['agreement'] is None else str(s['agreement']) + '%'}",
        f"- **Spearman**, PER vs label (good < okay < off): {'—' if s['spearman'] is None else s['spearman']}",
        "",
        "| Human ↓ / Predicted → | " + " | ".join(LABEL_RANK) + " |",
        "| --- |" + " --- |" * len(LABEL_RANK),
    ]
    for human in LABEL_RANK:
        lines.append(f"| {human} | " + " | ".join(str(s["confusion"][human][band]) for band in LABEL_RANK) + " |")
    if s["top_substitutions"]:
        top = ", ".join(f"`{pair}` ×{times}" for pair, times in s["top_substitutions"][:5])
        lines += ["", f"Most common substitutions: {top}."]
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Writes the latest EvalRun's summary into the Evals section of README.md."

    def add_arguments(self, parser):
        parser.add_argument("--readme", default=str(Path(settings.BASE_DIR) / "README.md"))

    def handle(self, *args, readme, **options):
        run = EvalRun.objects.order_by("-created_at", "-pk").first()
        if run is None:
            raise CommandError("No eval runs yet: run the bench from the admin first.")
        path = Path(readme)
        text = path.read_text()
        block = f"{START}\n{render(run)}\n{END}"
        if START in text and END in text:
            before, rest = text.split(START, 1)
            after = rest.split(END, 1)[1]
            text = f"{before}{block}{after}"
        else:
            text = f"{text.rstrip()}\n\n## Evals\n\n{block}\n"
        path.write_text(text)
        self.stdout.write(self.style.SUCCESS(f"Wrote {run} to {path}"))
