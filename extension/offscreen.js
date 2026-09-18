let current;
const notify = (session, event) => chrome.runtime.sendMessage({target: 'background', runId: session.id, ...event});
function transmit(session, event) {
  if (session.ws.readyState === WebSocket.OPEN) session.ws.send(JSON.stringify(event));
  else session.pending.push(event);
}
async function release(session) {
  clearTimeout(session.timer);
  session.stream?.getTracks().forEach(track => track.stop());
  if (session.context && session.context.state !== 'closed') await session.context.close();
}
async function commit(session) {
  if (session.committed) return;
  session.stopRequested = true;
  if (!session.recording) return;
  if (session.flushing) return;
  session.flushing = true;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Audio flush timed out')), 2000);
    session.flushed = () => { clearTimeout(timer); resolve(); };
    session.processor.port.postMessage('flush');
  });
  session.committed = true;
  await release(session);
  transmit(session, {type: 'commit'});
}
async function start(message) {
  if (current) throw new Error('Already running');
  const session = current = {id: message.runId, pending: [], recording: false, committed: false};
  try {
    session.ws = new WebSocket('ws://127.0.0.1:8767');
    session.ws.onopen = () => {
      session.ws.send(JSON.stringify({token: message.token}));
      for (const event of session.pending) session.ws.send(JSON.stringify(event));
      session.pending = [];
    };
    session.ws.onmessage = event => {
      const data = JSON.parse(event.data);
      if (current !== session || data.type === 'ready') return;
      void notify(session, data);
    };
    session.ws.onclose = () => {
      if (current === session) {
        current = null; void release(session);
        void notify(session, {type: 'failed', text: 'サーバー接続が終了しました。接続・ペアリング設定を確認してください。'});
      }
    };
    session.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, ...(message.deviceId ? {deviceId: {exact: message.deviceId}} : {})}, video: false});
    if (current !== session) { await release(session); return; }
    session.context = new AudioContext({sampleRate: 24000});
    await session.context.audioWorklet.addModule('pcm-worklet.js');
    if (current !== session) { await release(session); return; }
    const source = session.context.createMediaStreamSource(session.stream);
    const processor = session.processor = new AudioWorkletNode(session.context, 'pcm');
    processor.port.onmessage = event => {
      if (event.data === 'flushed') { session.flushed?.(); return; }
      if (current !== session || session.committed) return;
      const bytes = new Uint8Array(event.data.buffer);
      let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
      transmit(session, {type: 'audio', audio: btoa(binary)});
    };
    source.connect(processor); processor.connect(session.context.destination);
    await session.context.resume();
    session.recording = true;
    await notify(session, {type: 'recording'});
    session.timer = setTimeout(() => { void notify(session, {type: 'failed', text: '録音は30秒以内にしてください'}); }, 30000);
    if (session.stopRequested) await commit(session);
  } catch (_) {
    if (current === session) await notify(session, {type: 'failed', text: 'マイクまたは接続を開始できません。拡張の設定画面を確認してください。'});
    await release(session); session.ws?.close();
    if (current === session) current = null;
  }
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.target !== 'offscreen') return;
  (async () => {
    if (message.type === 'start') return start(message);
    const session = current;
    if (!session || session.id !== message.runId) return;
    if (message.type === 'commit') await commit(session);
    else if (message.type === 'rpc_result') transmit(session, message);
    else if (message.type === 'cancel') {
      current = null;
      await release(session);
      session.ws?.close();
    }
  })().then(() => respond({ok: true}), error => respond({error: error.message}));
  return true;
});
