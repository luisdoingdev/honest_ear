// Turns the microphone into 16 kHz mono 16-bit PCM for Deepgram's live captions.
// Raw PCM streams the same way on every browser; MediaRecorder's MP4 on iPhones doesn't.
const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 1280; // 80 ms per message, what Deepgram recommends for Flux

class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.chunk = new Int16Array(CHUNK_SAMPLES);
    this.length = 0;
    this.sum = 0;
    this.count = 0;
    this.progress = 0;
  }

  process(inputs) {
    const samples = inputs[0][0];
    if (!samples) return true;
    for (const sample of samples) {
      // Averaging the samples that fold into each output sample filters out what 16 kHz can't hold.
      this.sum += sample;
      this.count += 1;
      this.progress += 1;
      if (this.progress < this.ratio) continue;
      this.progress -= this.ratio;
      const value = Math.max(-1, Math.min(1, this.sum / this.count));
      this.chunk[this.length++] = value < 0 ? value * 0x8000 : value * 0x7fff;
      this.sum = 0;
      this.count = 0;
      if (this.length === CHUNK_SAMPLES) {
        this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
        this.chunk = new Int16Array(CHUNK_SAMPLES);
        this.length = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm16-downsampler", Pcm16Downsampler);
