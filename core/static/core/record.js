(() => {
  const MAX_SECONDS = 60;
  // Close codes from core.consumers.
  const CLOSE_MESSAGES = {
    4503: () => gettext("Recording is turned off right now."),
    4413: () => gettext("Recording is too long."),
  };

  const $ = (id) => document.getElementById(id);
  const session = document.querySelector(".session");
  const els = {
    status: $("status"),
    transcript: $("transcript"),
    phonemes: $("phonemes"),
    phonemesText: $("phonemes-text"),
    phonemesDrift: $("phonemes-drift"),
    phonemePairs: $("phoneme-pairs"),
    phonemesLegend: $("phonemes-legend"),
    spinner: $("spinner"),
    play: $("play"),
    mic: $("mic"),
  };
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const socketUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/record/`;
  const audio = new Audio();

  let phase = "idle";
  let stream = null;
  let recording = null;
  // The current recording's socket, microphone graph and results; replaced on every new recording.
  let take = null;
  let clockId = null;

  // idle → listening → transcribing → revealed → playing → played (then listening again).
  function setPhase(next) {
    phase = next;
    session.dataset.phase = next;
    const labels = { idle: gettext("Start session"), listening: gettext("Stop") };
    els.mic.setAttribute("aria-label", labels[next] ?? gettext("Record again"));
    els.mic.disabled = next === "transcribing" || next === "playing";
    els.spinner.hidden = next !== "transcribing";
    els.play.hidden = next !== "revealed";
  }

  function setStatus(text, isError = false) {
    els.status.textContent = text;
    els.status.classList.toggle("status-error", isError);
  }

  function clock(seconds) {
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function startClock() {
    const startedAt = Date.now();
    const tick = () => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      setStatus(interpolate(gettext("Listening… %s"), [clock(seconds)]));
      if (seconds >= MAX_SECONDS) stopListening();
    };
    tick();
    clockId = setInterval(tick, 250);
  }

  // transcript is null when transcription is off or failed (then error explains why).
  // phonemes is null when the scorer is off, cold or down: the section stays hidden.
  // alignment (from the server) pairs each word's expected phonemes with the heard ones; without it,
  // the heard phonemes are shown as one plain line.
  function showPhonemes(phonemes, alignment) {
    const paired = Boolean(phonemes && alignment && alignment.length);
    els.phonemes.hidden = !phonemes;
    els.phonemesText.hidden = paired;
    els.phonemesText.textContent = paired ? "" : phonemes || "";
    els.phonemePairs.hidden = !paired;
    els.phonemesLegend.hidden = !paired;
    els.phonemesDrift.hidden = !paired;
    els.phonemePairs.replaceChildren(...(paired ? alignment.map(pairedWord) : []));
    if (paired) els.phonemesDrift.textContent = driftSummary(alignment);
  }

  function pairedWord({ word, ops }) {
    const column = document.createElement("div");
    column.className = "pair-word";
    const label = document.createElement("span");
    label.className = "pair-label";
    label.textContent = word;
    label.lang = document.documentElement.lang;
    const row = document.createElement("span");
    row.className = "pair-ops";
    for (const op of ops) {
      const cell = document.createElement("span");
      cell.className = `pair-op pair-${op.op}`;
      cell.dataset.op = op.op;
      for (const [side, phoneme] of [["exp", op.expected], ["heard", op.heard]]) {
        const part = document.createElement("span");
        part.className = `pair-${side}`;
        part.textContent = phoneme ?? "–";
        cell.append(part);
      }
      row.append(cell);
    }
    column.append(label, row);
    return column;
  }

  function driftSummary(alignment) {
    const ops = alignment.flatMap((w) => w.ops);
    const expected = ops.filter((op) => op.op !== "ins").length;
    const drifted = ops.filter((op) => op.op !== "match").length;
    if (!drifted) return gettext("Every sound matched.");
    return interpolate(gettext("%s changes across %s expected sounds."), [drifted, expected]);
  }

  function showTranscript(transcript, error) {
    els.transcript.replaceChildren();
    els.transcript.classList.toggle("transcript-empty", !transcript);
    if (transcript) {
      transcript.split(/\s+/).forEach((word, i) => {
        const span = document.createElement("span");
        span.className = "word";
        span.style.animationDelay = `${i * 80}ms`;
        span.textContent = `${word} `;
        els.transcript.append(span);
      });
    } else {
      els.transcript.textContent = error || (transcript === "" ? gettext("No speech detected.") : "");
    }
    els.transcript.hidden = !els.transcript.textContent;
  }

  // One socket per recording (core.consumers): PCM goes up while listening, then {"type": "stop"}.
  // The server pushes the saved recording, then the phonemes and the transcript as each is ready, then the
  // pairs. Messages from an earlier recording's socket are ignored.
  function openSocket(current) {
    const socket = new WebSocket(socketUrl);
    socket.binaryType = "arraybuffer";
    const pending = [];
    current.send = (data) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(data);
      else if (socket.readyState === WebSocket.CONNECTING) pending.push(data);
    };
    socket.addEventListener("open", () => pending.splice(0).forEach((data) => socket.send(data)));
    socket.addEventListener("message", (event) => take === current && handle(current, JSON.parse(event.data)));
    socket.addEventListener("close", (event) => take === current && closed(current, event));
    current.socket = socket;
  }

  async function handle(current, message) {
    switch (message.type) {
      case "recording":
        await received(current, message);
        break;
      case "phonemes":
        current.phonemes = message.phonemes;
        showPhonemes(message.phonemes, []);
        break;
      case "transcript":
        showTranscript(message.transcript ?? null, message.error);
        break;
      case "pairs":
        showPhonemes(current.phonemes, message.alignment);
        break;
      case "error":
        current.failed = true;
        fail(message.error);
        break;
    }
  }

  async function received(current, message) {
    recording = { id: message.id };
    // Results keep arriving while the audio downloads; the transcript fills in when Whisper is done.
    showTranscript(undefined, gettext("Transcribing…"));
    try {
      // Download now so the Play tap can start playback immediately (iOS requires that).
      const downloaded = await fetch(message.audio_url, { cache: "no-store" });
      if (!downloaded.ok) throw new Error(`HTTP ${downloaded.status}`);
      if (audio.src) URL.revokeObjectURL(audio.src);
      audio.src = URL.createObjectURL(await downloaded.blob());
    } catch (err) {
      fail(interpolate(gettext("Couldn't load the recording: %s"), [err.message]));
      return;
    }
    if (take !== current) return;
    current.saved = true;
    setStatus(gettext("Tap Play to hear it. It's deleted when it finishes."));
    setPhase("revealed");
  }

  function closed(current, event) {
    if (current.failed || current.saved || event.code === 1000) return;
    // Closed before the recording was saved: say why, and leave the mic ready for another try.
    current.failed = true;
    stopMicrophone(current);
    const message = CLOSE_MESSAGES[event.code]?.() ?? gettext("The connection was lost. Try again.");
    fail(message);
  }

  function fail(message) {
    clearInterval(clockId);
    setStatus(message, true);
    setPhase("played");
  }

  async function startMicrophone(current, audioContext) {
    current.audioContext = audioContext;
    await audioContext.audioWorklet.addModule(session.dataset.worklet);
    const node = new AudioWorkletNode(audioContext, "pcm16-downsampler");
    node.port.onmessage = ({ data }) => current.send(data);
    current.source = audioContext.createMediaStreamSource(stream);
    // A silent output keeps the worklet running in browsers that skip nodes with no destination.
    const mute = audioContext.createGain();
    mute.gain.value = 0;
    current.source.connect(node).connect(mute).connect(audioContext.destination);
  }

  function stopMicrophone(current) {
    current.source?.disconnect();
    current.audioContext?.close().catch(() => {});
  }

  async function deleteRecording() {
    if (!recording) return;
    const { id } = recording;
    recording = null;
    const response = await fetch(`/api/recordings/${id}/`, { method: "DELETE", headers: { "X-CSRFToken": csrfToken } });
    // 200 {"kept": true}: eval mode kept the audio (see the keep_audio flag); 204: deleted.
    return response.status === 200 && (await response.json()).kept === true;
  }

  async function startListening() {
    // Created inside the tap, before any await: iPhones only start audio from a direct user gesture.
    const audioContext = new AudioContext();
    // Recording again before playing: the earlier recording isn't kept.
    deleteRecording().catch(() => {});
    try {
      stream ??= await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      audioContext.close().catch(() => {});
      setStatus(gettext("Microphone access was denied. Allow it in your browser settings and try again."), true);
      return;
    }

    take?.socket?.close();
    const current = { phonemes: null };
    take = current;
    openSocket(current);
    els.transcript.hidden = true;
    els.phonemes.hidden = true;
    setPhase("listening");
    startClock();
    try {
      await startMicrophone(current, audioContext);
    } catch {
      // The socket may already have failed (and said why) while the microphone was starting.
      if (current.failed || take !== current) return;
      current.failed = true;
      current.socket.close();
      fail(gettext("This browser can't record audio. Try a recent Chrome or Safari."));
    }
  }

  function stopListening() {
    if (phase !== "listening") return;
    clearInterval(clockId);
    setPhase("transcribing");
    setStatus(gettext("Saving…"));
    stopMicrophone(take);
    take.send(JSON.stringify({ type: "stop" }));
  }

  if (!window.isSecureContext) {
    setStatus(gettext("Recording needs a secure connection. Open this page over HTTPS."), true);
    els.mic.disabled = true;
    return;
  }

  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode || !window.WebSocket) {
    setStatus(gettext("This browser can't record audio. Try a recent Chrome or Safari."), true);
    els.mic.disabled = true;
    return;
  }

  els.mic.addEventListener("click", () => {
    if (phase === "listening") stopListening();
    else startListening();
  });

  els.play.addEventListener("click", () => {
    setPhase("playing");
    setStatus(gettext("Playing…"));
    audio.play().catch(() => {
      setStatus(gettext("Couldn't play the recording."), true);
      setPhase("revealed");
    });
  });

  audio.addEventListener("ended", async () => {
    URL.revokeObjectURL(audio.src);
    audio.removeAttribute("src");
    try {
      const kept = await deleteRecording();
      setStatus(
        kept
          ? gettext("Kept for evaluation. Tap the mic to record again.")
          : gettext("Deleted. Tap the mic to record again."),
      );
    } catch {
      setStatus(gettext("Played, but deleting failed. It will be removed automatically."), true);
    }
    setPhase("played");
  });
})();
