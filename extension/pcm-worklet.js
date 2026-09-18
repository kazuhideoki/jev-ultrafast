class PCM extends AudioWorkletProcessor {
  constructor() {
    super(); this.samples = []; this.position = 0; this.input = []; this.stopped = false;
    this.port.onmessage = () => {
      this.stopped = true;
      if (this.samples.length) this.port.postMessage(new Int16Array(this.samples));
      this.samples = [];
      this.port.postMessage('flushed');
    };
  }
  process(inputs) {
    if (this.stopped) return true;
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    this.input.push(...channel);
    const ratio = sampleRate / 24000;
    while (this.position + 1 < this.input.length) {
      const i = Math.floor(this.position), f = this.position - i;
      const v = this.input[i] * (1 - f) + this.input[i + 1] * f;
      this.samples.push(Math.round(Math.max(-1, Math.min(1, v)) * 32767));
      this.position += ratio;
      if (this.samples.length === 2400) { this.port.postMessage(new Int16Array(this.samples)); this.samples = []; }
    }
    const consumed = Math.floor(this.position);
    this.input.splice(0, consumed); this.position -= consumed;
    return true;
  }
}
registerProcessor('pcm', PCM);
