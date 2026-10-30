(() => {
  const MAX_SECONDS = 60;
  const CAPTION_WORDS = 8;
  // After Stop, Deepgram finalizes the last words; don't wait longer than this for them.
  const FINALIZE_TIMEOUT_MS = 2000;
  const MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

  const $ = (id) => document.getElementById(id);
  const session = document.querySelector(".session");
  const els = {
    status: $("status"),
    transcript: $("transcript"),
    captions: $("captions"),
    spinner: $("spinner"),
    play: $("play"),
    mic: $("mic"),
  };
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const liveCaptionsEnabled = session.dataset.liveCaptions === "true";
  const audio = new Audio();

  let phase = "idle";
  let stream = null;
  let recorder = null;
  let chunks = [];
  let recording = null;
  let live = null;
  let clockId = null;

  // idle → listening → transcribing → revealed → playing → played (then listening again).
  function setPhase(next) {
    phase = next;
    session.dataset.phase = next;
    const labels = { idle: gettext("Start session"), listening: gettext("Stop") };
    els.mic.setAttribute("aria-label", labels[next] ?? gettext("Record again"));
    els.mic.disabled = next === "transcribing" || next === "playing";
    els.captions.hidden = next !== "listening";
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

  function showCaptions(words) {
    els.captions.textContent = words.slice(-CAPTION_WORDS).join(" ");
  }

  async function errorMessage(response) {
    try {
      return (await response.json()).error;
    } catch {
      return `HTTP ${response.status}`;
    }
  }

  // Live captions: the server hands out a short-lived Deepgram token, the browser streams PCM to Flux.
  // Flux is turn-based: every TurnInfo carries the whole transcript of one turn so far, so each turn's
  // text is replaced in place rather than appended. Any failure leaves state.ok false, and the server
  // transcribes the upload after Stop instead.
  async function startLive(audioContext) {
    const state = { ok: false, turns: [], text: "", socket: null, source: null, closed: null };
    if (!liveCaptionsEnabled) return state;
    try {
      const [tokenResponse] = await Promise.all([
        fetch("/api/transcription/token/", { method: "POST", headers: { "X-CSRFToken": csrfToken } }),
        audioContext.audioWorklet.addModule(session.dataset.worklet),
      ]);
      if (!tokenResponse.ok || phase !== "listening") return state;
      const { url, protocols } = await tokenResponse.json();

      const socket = new WebSocket(url, protocols);
      const pending = [];
      state.socket = socket;
      state.closed = new Promise((resolve) => socket.addEventListener("close", resolve));
      socket.addEventListener("open", () => {
        state.ok = true;
        pending.splice(0).forEach((chunk) => socket.send(chunk));
      });
      socket.addEventListener("error", () => (state.ok = false));
      socket.addEventListener("message", (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "Error") state.ok = false;
        if (data.type !== "TurnInfo") return;
        state.turns[data.turn_index] = data.transcript;
        state.text = state.turns.filter(Boolean).join(" ");
        showCaptions(state.text.split(/\s+/));
      });

      const node = new AudioWorkletNode(audioContext, "pcm16-downsampler");
      node.port.onmessage = ({ data }) => {
        if (socket.readyState === WebSocket.OPEN) socket.send(data);
        else if (socket.readyState === WebSocket.CONNECTING) pending.push(data);
      };
      state.source = audioContext.createMediaStreamSource(stream);
      // A silent output keeps the worklet running in browsers that skip nodes with no destination.
      const mute = audioContext.createGain();
      mute.gain.value = 0;
      state.source.connect(node).connect(mute).connect(audioContext.destination);
    } catch {
      state.ok = false;
    }
    return state;
  }

  async function finishLive(state, audioContext) {
    state.source?.disconnect();
    audioContext.close().catch(() => {});
    const { socket } = state;
    if (socket?.readyState === WebSocket.OPEN) {
      // Closing alone drops the turn still in progress; ForceEndTurn makes Flux send its final EndOfTurn first.
      socket.send(JSON.stringify({ type: "ForceEndTurn" }));
      socket.send(JSON.stringify({ type: "CloseStream" }));
      const timeout = new Promise((resolve) => setTimeout(resolve, FINALIZE_TIMEOUT_MS));
      await Promise.race([state.closed, timeout]);
      socket.close();
    } else {
      socket?.close();
      state.ok = false;
    }
    return state.ok ? state.text : null;
  }

  async function deleteRecording() {
    if (!recording) return;
    const { id } = recording;
    recording = null;
    await fetch(`/api/recordings/${id}/`, { method: "DELETE", headers: { "X-CSRFToken": csrfToken } });
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

    const mimeType = MIME_TYPES.find((t) => MediaRecorder.isTypeSupported(t));
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunks = [];
    recorder.addEventListener("dataavailable", (e) => e.data.size && chunks.push(e.data));
    const stopped = new Promise((resolve) => recorder.addEventListener("stop", resolve));
    recorder.start();

    els.transcript.hidden = true;
    showCaptions([]);
    setPhase("listening");
    startClock();

    const liveStarted = startLive(audioContext);
    live = { audioContext, stopped, liveStarted };
  }

  async function stopListening() {
    if (phase !== "listening") return;
    clearInterval(clockId);
    setPhase("transcribing");
    setStatus(gettext("Transcribing…"));
    if (recorder.state === "recording") recorder.stop();

    const { audioContext, stopped, liveStarted } = live;
    const [, liveText] = await Promise.all([stopped, liveStarted.then((s) => finishLive(s, audioContext))]);
    await upload(liveText);
  }

  async function upload(liveText) {
    const type = recorder.mimeType || chunks[0]?.type || "audio/webm";
    const form = new FormData();
    form.append("audio", new Blob(chunks, { type }), "recording");
    if (liveText !== null) form.append("live_transcript", "1");

    try {
      const created = await fetch("/api/recordings/", {
        method: "POST",
        body: form,
        headers: { "X-CSRFToken": csrfToken },
      });
      if (!created.ok) throw new Error(await errorMessage(created));
      recording = await created.json();

      // Download now so the Play tap can start playback immediately (iOS requires that).
      const downloaded = await fetch(recording.audio_url, { cache: "no-store" });
      if (!downloaded.ok) throw new Error(await errorMessage(downloaded));
      if (audio.src) URL.revokeObjectURL(audio.src);
      audio.src = URL.createObjectURL(await downloaded.blob());

      if (liveText !== null) showTranscript(liveText);
      else showTranscript(recording.transcript ?? null, recording.transcript_error);
      setStatus(gettext("Tap Play to hear it. It's deleted when it finishes."));
      setPhase("revealed");
    } catch (err) {
      await deleteRecording();
      setStatus(interpolate(gettext("Upload failed: %s"), [err.message]), true);
      setPhase("played");
    }
  }

  if (!window.isSecureContext) {
    setStatus(gettext("Recording needs a secure connection. Open this page over HTTPS."), true);
    els.mic.disabled = true;
    return;
  }

  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder || !window.AudioContext) {
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
      await deleteRecording();
      setStatus(gettext("Deleted. Tap the mic to record again."));
    } catch {
      setStatus(gettext("Played, but deleting failed. It will be removed automatically."), true);
    }
    setPhase("played");
  });
})();
