const status = document.querySelector('#status');
const level = document.querySelector('#level');
const devices = document.querySelector('#device');
chrome.storage.local.get(['token', 'deviceId']).then(async ({token, deviceId}) => {
  document.querySelector('#token').value = token || '';
  await listDevices(deviceId);
});
async function listDevices(selected) {
  const inputs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
  devices.replaceChildren(new Option('システムの既定のマイク', ''));
  for (const input of inputs) if (input.deviceId && input.deviceId !== 'default') {
    devices.add(new Option(input.label || 'マイク', input.deviceId));
  }
  devices.value = selected || '';
}
document.querySelector('#save').onclick = async () => {
  const token = document.querySelector('#token').value.trim();
  if (token.length < 32) { status.textContent = 'ペアリングトークンを確認してください。'; return; }
  await chrome.storage.local.set({token, deviceId: devices.value}); status.textContent = '保存しました。';
};
document.querySelector('#microphone').onclick = async () => {
  const button = document.querySelector('#microphone');
  button.disabled = true;
  let stream, context;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: devices.value ? {deviceId: {exact: devices.value}} : true});
    await listDevices(devices.value);
    context = new AudioContext();
    await context.resume();
    const analyser = context.createAnalyser();
    context.createMediaStreamSource(stream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    status.textContent = 'いま話してください。5秒間、選択したマイクの入力音量を確認します。';
    let peak = 0;
    for (let i = 0; i < 50; i++) {
      analyser.getFloatTimeDomainData(samples);
      const rms = Math.sqrt(samples.reduce((sum, v) => sum + v * v, 0) / samples.length);
      peak = Math.max(peak, rms);
      level.value = Math.min(1, rms * 10);
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    status.textContent = peak > .001
      ? '音声入力を確認しました。マイクを変更した場合は「保存」を押してください。'
      : '入力音量がほぼありません。マイクを選び直し、ミュートや macOS の入力設定を確認してください。';
  } catch (_) { status.textContent = 'マイクを使用できません。マイクの選択とブラウザ・macOS の権限を確認してください。'; }
  finally {
    stream?.getTracks().forEach(track => track.stop());
    if (context) await context.close();
    button.disabled = false;
  }
};

chrome.storage.session.get('lastTranscript').then(({lastTranscript}) => {
  document.querySelector('#transcript').textContent = lastTranscript || 'まだありません';
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.lastTranscript) {
    document.querySelector('#transcript').textContent = changes.lastTranscript.newValue || 'まだありません';
  }
});
