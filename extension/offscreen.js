let current;
const notify = (session, event) => chrome.runtime.sendMessage({target: 'background', runId: session.id, ...event});
function transmit(session, event) {
  if (session.authenticated && session.ws.readyState === WebSocket.OPEN) {
    if (session.ws.bufferedAmount > 1048576) {
      void notify(session, {type: 'failed', text: '音声送信が滞っています。再接続してください。'});
      return;
    }
    session.ws.send(JSON.stringify(event));
  }
  else {
    if (session.pending.length >= 100) {
      void notify(session, {type: 'failed', text: '接続待ちが長すぎます。再接続してください。'});
      return;
    }
    session.pending.push(event);
  }
}
async function release(session) {
  session.released = true;
  clearTimeout(session.timer);
  clearInterval(session.captureTimer);
  session.stream?.getTracks().forEach(track => track.stop());
  if (session.context && session.context.state !== 'closed') await session.context.close();
}
function watchCapture(session) {
  const fail = () => {
    if (current !== session || session.released || session.committed) return;
    current = null;
    // Notify immediately; background.finish gates RPCs before its first await.
    void notify(session, {type: 'failed', text: 'マイク入力が停止したため操作を停止しました。再接続してください。'});
    session.ws?.close();
    void release(session);
  };
  for (const track of session.stream.getAudioTracks()) {
    track.addEventListener('ended', fail);
    track.addEventListener('mute', fail);
  }
  session.context.addEventListener('statechange', () => {
    if (session.context.state !== 'running') fail();
  });
  session.processor.onprocessorerror = fail;
  session.lastAudioAt = performance.now();
  session.captureTimer = setInterval(() => {
    if (performance.now() - session.lastAudioAt > 2000) fail();
  }, 500);
  if (session.stream.getAudioTracks().some(track => track.readyState !== 'live' || track.muted)) fail();
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
    const clientNonce = crypto.randomUUID();
    const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(message.token),
      {name: 'HMAC', hash: 'SHA-256'}, false, ['sign', 'verify']);
    if (current !== session) return;
    const proofBytes = (role, nonce) => new TextEncoder().encode(`jev-voice-v1:${role}:${clientNonce}:${nonce}`);
    session.ws = new WebSocket('ws://127.0.0.1:8767');
    let handshake = 'challenge';
    let incoming = Promise.resolve();
    session.ws.onopen = () => session.ws.send(JSON.stringify({type: 'hello', nonce: clientNonce}));
    session.ws.onmessage = event => {
      incoming = incoming.then(async () => {
        if (current !== session) return;
        const data = JSON.parse(event.data);
        if (handshake === 'challenge') {
          if (data.type !== 'challenge' || !/^[a-f0-9]{64}$/.test(data.nonce || '') ||
              !/^[a-f0-9]{64}$/.test(data.proof || '')) throw new Error('Invalid server proof');
          const signature = Uint8Array.from(data.proof.match(/../g), hex => parseInt(hex, 16));
          if (!await crypto.subtle.verify('HMAC', key, signature, proofBytes('server', data.nonce))) {
            throw new Error('Invalid server proof');
          }
          const signed = await crypto.subtle.sign('HMAC', key, proofBytes('client', data.nonce));
          if (current !== session) return;
          const proof = [...new Uint8Array(signed)].map(byte => byte.toString(16).padStart(2, '0')).join('');
          session.ws.send(JSON.stringify({type: 'authenticate', proof}));
          handshake = 'ready';
          return;
        }
        if (handshake === 'ready') {
          if (data.type !== 'ready') throw new Error('Pairing not completed');
          session.authenticated = true;
          handshake = 'authenticated';
          transmit(session, {type: 'start', continuous: message.continuous === true});
          for (const pending of session.pending) transmit(session, pending);
          session.pending = [];
          return;
        }
        if (data.type === 'rpc') {
          // Let later gate events overtake a command waiting on browser attachment/focus.
          // The browser owner still has only one outstanding RPC.
          void notify(session, data).catch(() => session.ws.close());
        } else await notify(session, data);
      }).catch(async () => {
        if (current !== session) return;
        current = null;
        await release(session); session.ws.close();
        await notify(session, {type: 'failed', text: 'サーバーを認証できません。接続先とペアリング設定を確認してください。'});
      });
    };
    session.ws.onclose = () => {
      // Process the final result queued before close before reporting a lost connection.
      incoming = incoming.finally(async () => {
        if (current !== session) return;
        current = null; await release(session);
        await notify(session, {type: 'failed', text: 'サーバー接続が終了しました。接続・ペアリング設定を確認してください。'});
      });
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
      session.lastAudioAt = performance.now();
      const bytes = new Uint8Array(event.data.buffer);
      let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
      transmit(session, {type: 'audio', audio: btoa(binary)});
    };
    source.connect(processor); processor.connect(session.context.destination);
    await session.context.resume();
    session.recording = true;
    watchCapture(session);
    if (current !== session) return;
    await notify(session, {type: 'recording'});
    if (!message.continuous) session.timer = setTimeout(() => { void notify(session, {type: 'failed', text: '録音は30秒以内にしてください'}); }, 30000);
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
