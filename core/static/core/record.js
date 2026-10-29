(() => {
  const MAX_SECONDS = 60;
  const MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

  const $ = (id) => document.getElementById(id);
  const buttons = { start: $("start"), record: $("record"), stop: $("stop"), play: $("play") };
  const statusEl = $("status");
  const timerEl = $("timer");
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const audio = new Audio();

  let stream = null;
  let recorder = null;
  let chunks = [];
  let recording = null;
  let timerId = null;

  function show(...visible) {
    for (const [name, button] of Object.entries(buttons)) button.hidden = !visible.includes(name);
  }

  function setStatus(text, isError = false) {
    statusEl.textContent = text;
    statusEl.classList.toggle("status-error", isError);
  }

  function startTimer() {
    const startedAt = Date.now();
    timerEl.textContent = "0:00";
    timerEl.hidden = false;
    timerId = setInterval(() => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      timerEl.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
      if (seconds >= MAX_SECONDS) buttons.stop.click();
    }, 250);
  }

  function stopTimer() {
    clearInterval(timerId);
    timerEl.hidden = true;
  }

  async function errorMessage(response) {
    try {
      return (await response.json()).error;
    } catch {
      return `HTTP ${response.status}`;
    }
  }

  async function deleteRecording() {
    if (!recording) return;
    const { id } = recording;
    recording = null;
    await fetch(`/api/recordings/${id}/`, { method: "DELETE", headers: { "X-CSRFToken": csrfToken } });
  }

  async function upload() {
    stopTimer();
    show();
    setStatus("Uploading…");

    const type = recorder.mimeType || chunks[0]?.type || "audio/webm";
    const form = new FormData();
    form.append("audio", new Blob(chunks, { type }), "recording");

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

      setStatus("Uploaded. Tap Play to hear it. It's deleted when it finishes.");
      show("play");
    } catch (err) {
      await deleteRecording();
      setStatus(`Upload failed: ${err.message}`, true);
      show("record");
    }
  }

  if (!window.isSecureContext) {
    setStatus("Recording needs a secure connection. Open this page over HTTPS.", true);
    show();
    return;
  }

  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    setStatus("This browser can't record audio. Try a recent Chrome or Safari.", true);
    show();
    return;
  }

  buttons.start.addEventListener("click", async () => {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      setStatus("Ready. Tap Record when you are.");
      show("record");
    } catch {
      setStatus("Microphone access was denied. Allow it in your browser settings and try again.", true);
    }
  });

  buttons.record.addEventListener("click", () => {
    const mimeType = MIME_TYPES.find((t) => MediaRecorder.isTypeSupported(t));
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunks = [];
    recorder.addEventListener("dataavailable", (e) => e.data.size && chunks.push(e.data));
    recorder.addEventListener("stop", upload);
    recorder.start();
    startTimer();
    setStatus("Recording…");
    show("stop");
  });

  buttons.stop.addEventListener("click", () => {
    if (recorder?.state === "recording") recorder.stop();
  });

  buttons.play.addEventListener("click", () => {
    show();
    setStatus("Playing…");
    audio.play().catch(() => {
      setStatus("Couldn't play the recording.", true);
      show("play");
    });
  });

  audio.addEventListener("ended", async () => {
    URL.revokeObjectURL(audio.src);
    audio.removeAttribute("src");
    try {
      await deleteRecording();
      setStatus("Deleted. Record again?");
    } catch {
      setStatus("Played, but deleting failed. It will be removed automatically.", true);
    }
    buttons.record.textContent = "Record again";
    show("record");
  });
})();
