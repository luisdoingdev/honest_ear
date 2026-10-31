"""Pronunciation scoring for the eval bench, as plain functions: no network, no database.

For one clip, given what the pipeline heard (Whisper's transcript and wav2vec2's phonemes):
1. Gates: clips shorter than `min_duration_s` are `too_short`; clips whose transcript shares too few words
   with the intended text are `wrong_sentence`. Neither is scored.
2. Expected phonemes: the intended text, word by word, through espeak-ng (the phonemizer this model's
   training labels came from), so both sides use the same IPA symbols.
3. Alignment: Levenshtein with backtrace, one op per phoneme (match / sub / ins / del), grouped by word.
4. PER = (sub + ins + del) / expected length, then a band (good / okay / off) from the versioned config.
"""

import json
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "eval_config" / "scoring.json"
# espeak-ng voices: Latin American Spanish (the audience) and US English.
ESPEAK_VOICES = {"es": "es-419", "en": "en-us"}
BANDS = ("good", "okay", "off")
# espeak-ng isn't thread-safe, and the recording websocket phonemizes from worker threads.
_espeak = threading.Lock()


class ConfigError(ValueError):
    pass


def load_config(path=CONFIG_PATH):
    config = json.loads(Path(path).read_text())
    for key in ("version", "min_duration_s", "wrong_sentence_min_overlap", "bands"):
        if key not in config:
            raise ConfigError(f"Scoring config {path} is missing {key!r}")
    bands = config["bands"]
    if not 0 <= bands.get("good", -1) < bands.get("okay", -1):
        raise ConfigError(f"Scoring config {path}: bands need 0 <= good < okay (got {bands})")
    return config


def words(text):
    # Case and punctuation don't count; accents do (más ≠ mas).
    return re.findall(r"[\w']+", unicodedata.normalize("NFC", text).lower())


def word_overlap(intended_text, transcript):
    """Share of the intended words (each counted as often as meant) that appear in the transcript, in any order."""
    intended = Counter(words(intended_text))
    total = sum(intended.values())
    if not total:
        return 0.0
    return sum((intended & Counter(words(transcript))).values()) / total


def expected_phonemes(text, language):
    """[(word, [phoneme, ...]), ...]: each word phonemized on its own, so word boundaries survive."""
    from phonemizer import phonemize

    text_words = words(text)
    if not text_words:
        return []
    from phonemizer.separator import Separator

    with _espeak:
        phonemized = phonemize(
            text_words,
            language=ESPEAK_VOICES[language],
            backend="espeak",
            # Each line is one word; a distinct word separator keeps phonemizer's word-count check quiet.
            separator=Separator(phone=" ", word=" | ", syllable=""),
            strip=True,
            preserve_punctuation=False,
            with_stress=False,
        )
    return [(word, [t for t in phonemes.split() if t != "|"]) for word, phonemes in zip(text_words, phonemized)]


def align(expected_words, heard):
    """Levenshtein alignment of the expected phonemes (by word) against the heard ones, with backtrace.

    Returns [{"word": w, "ops": [{"op", "expected", "heard"}, ...]}, ...] in word order. An inserted phoneme
    belongs to the word of the expected phoneme before it (the first word when nothing came before).
    """
    flat = [(index, phoneme) for index, (_, phonemes) in enumerate(expected_words) for phoneme in phonemes]
    n, m = len(flat), len(heard)
    # Cost is (edits, -matches): fewest edits first, then, among equally cheap alignments, the most matches.
    # "m e s a" heard as "m s a a" is then a dropped "e" plus an extra "a", not two substitutions.
    cost = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        cost[i][0] = (i, 0)
    for j in range(m + 1):
        cost[0][j] = (j, 0)

    def diagonal(i, j):
        edits, negative_matches = cost[i - 1][j - 1]
        same = flat[i - 1][1] == heard[j - 1]
        return (edits + (not same), negative_matches - same)

    def deletion(i, j):
        return (cost[i - 1][j][0] + 1, cost[i - 1][j][1])

    def insertion(i, j):
        return (cost[i][j - 1][0] + 1, cost[i][j - 1][1])

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = min(diagonal(i, j), deletion(i, j), insertion(i, j))

    # Backtrace from the end along any step that reproduces the optimal cost.
    ops = []
    i, j = n, m
    while i or j:
        if i and j and cost[i][j] == diagonal(i, j):
            kind = "match" if flat[i - 1][1] == heard[j - 1] else "sub"
            ops.append((flat[i - 1][0], {"op": kind, "expected": flat[i - 1][1], "heard": heard[j - 1]}))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == deletion(i, j):
            ops.append((flat[i - 1][0], {"op": "del", "expected": flat[i - 1][1], "heard": None}))
            i -= 1
        else:
            word = flat[i - 1][0] if i else 0
            ops.append((word, {"op": "ins", "expected": None, "heard": heard[j - 1]}))
            j -= 1
    ops.reverse()

    grouped = [{"word": word, "ops": []} for word, _ in expected_words]
    for index, op in ops:
        grouped[index]["ops"].append(op)
    return grouped


def counts(alignment):
    tally = Counter(op["op"] for word in alignment for op in word["ops"])
    return {kind: tally.get(kind, 0) for kind in ("match", "sub", "ins", "del")}


def phoneme_error_rate(alignment):
    """(sub + ins + del) / expected phonemes; None when nothing was expected. Can exceed 1 with many insertions."""
    c = counts(alignment)
    expected_length = c["match"] + c["sub"] + c["del"]
    if not expected_length:
        return None
    return (c["sub"] + c["ins"] + c["del"]) / expected_length


def band(per, config):
    if per <= config["bands"]["good"]:
        return "good"
    if per <= config["bands"]["okay"]:
        return "okay"
    return "off"


def score(intended_text, language, transcript, heard_phonemes, duration_s, config, expected_words=None):
    """The decision for one clip: {"status", "per", "band", "overlap", "counts", "alignment"}.

    status is "scored", "too_short" or "wrong_sentence"; only scored clips have a PER and a band.
    """
    result = {"status": "scored", "per": None, "band": None, "overlap": None, "counts": None, "alignment": []}
    if duration_s < config["min_duration_s"]:
        return {**result, "status": "too_short"}
    result["overlap"] = round(word_overlap(intended_text, transcript), 3)
    if result["overlap"] < config["wrong_sentence_min_overlap"]:
        return {**result, "status": "wrong_sentence"}

    if expected_words is None:
        expected_words = expected_phonemes(intended_text, language)
    alignment = align(expected_words, heard_phonemes.split())
    per = phoneme_error_rate(alignment)
    return {
        **result,
        "per": per,
        "band": band(per, config) if per is not None else None,
        "counts": counts(alignment),
        "alignment": alignment,
    }
