let run = null;
let creating;
const methods = new Set(['Runtime.evaluate', 'Input.dispatchMouseEvent', 'Input.dispatchKeyEvent', 'Input.insertText']);
const offscreen = message => chrome.runtime.sendMessage({target: 'offscreen', ...message});
async function display(text, terminal = false) {
  const current = run;
  if (!current || (current.finishing && !terminal)) return;
  await chrome.action.setBadgeText({text: terminal ? '' : current.phase === 'recording' ? 'REC' : '…'});
  await chrome.action.setTitle({title: text});
  await chrome.storage.session.set({status: text});
  await chrome.tabs.sendMessage(current.tabId, {type: 'display', text, terminal, transcript: current.transcript || '', recording: !terminal && current.phase === 'recording'}).catch(() => {});
}
async function ensureOffscreen() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ['OFFSCREEN_DOCUMENT'], documentUrls: [chrome.runtime.getURL('offscreen.html')],
  });
  if (contexts.length) return;
  creating ||= chrome.offscreen.createDocument({url: 'offscreen.html', reasons: ['USER_MEDIA'], justification: 'Record a push-to-talk browser instruction'});
  try { await creating; } finally { creating = null; }
}
async function active(id) {
  const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  return tab?.id === id;
}
async function finish(text) {
  const previous = run;
  if (!previous || previous.finishing) return;
  previous.finishing = true;
  await display(text, true);
  clearTimeout(previous.timer);
  await offscreen({type: 'cancel', runId: previous.id}).catch(() => {});
  if (previous.attached) await chrome.debugger.detach({tabId: previous.tabId}).catch(() => {});
  if (run === previous) run = null;
}
async function start(tabId) {
  if (run) return;
  const current = run = {id: crypto.randomUUID(), tabId, phase: 'starting', attached: false};
  current.timer = setTimeout(() => { if (run === current) void finish('時間上限に達しました'); }, 120000);
  try {
    if (!await active(tabId)) throw new Error('対象のタブが切り替わりました');
    const tab = await chrome.tabs.get(tabId);
    if (!/^https?:/.test(tab.url || '')) throw new Error('通常の Web ページで開始してください');
    const {token, deviceId} = await chrome.storage.local.get(['token', 'deviceId']);
    if (run !== current || current.finishing) return;
    if (!token) { await finish('設定画面でペアリングしてください'); await chrome.runtime.openOptionsPage(); return; }
    await display('マイクと接続を準備中…');
    await ensureOffscreen();
    if (run !== current || current.finishing) return;
    const result = await offscreen({type: 'start', runId: current.id, token, deviceId});
    if (result?.error) throw new Error(result.error);
    current.started = true;
    if (run === current && current.stopRequested) await offscreen({type: 'commit', runId: current.id});
  } catch (error) { if (run === current) await finish(error.message); }
}
async function commit() {
  if (!run || !['starting', 'recording'].includes(run.phase)) return;
  run.phase = 'processing';
  run.stopRequested = true;
  await display('音声を確定中…');
  if (run?.started) await offscreen({type: 'commit', runId: run.id});
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.target !== 'background') return;
  (async () => {
    if (sender.tab) {
      if (sender.frameId !== 0) return;
      if (message.type === 'hold-start') return start(sender.tab.id);
      if (!run || sender.tab.id !== run.tabId) return;
      if (message.type === 'commit') return commit();
      if (message.type === 'cancel' || (message.type === 'cancel-recording' && ['starting', 'recording'].includes(run.phase))) return finish('中止しました');
      return;
    }
    if (sender.url !== chrome.runtime.getURL('offscreen.html') || message.runId !== run?.id) return;
    const current = run;
    if (message.type === 'recording') {
      if (run.phase === 'starting') { run.phase = 'recording'; await display('録音中 · キーを離して実行 / Esc で中止'); }
    } else if (message.type === 'transcript') {
      current.transcript = message.text;
      await chrome.storage.session.set({lastTranscript: message.text});
      await display('実行中…');
    } else if (message.type === 'status') await display(message.text);
    else if (['finished', 'failed'].includes(message.type)) await finish(message.text);
    else if (message.type === 'rpc') {
      try {
        if (current.finishing || !methods.has(message.method) || !await active(current.tabId)) throw new Error('Stopped');
        if (!current.attached) {
          await chrome.debugger.attach({tabId: current.tabId}, '1.3');
          current.attached = true;
          if (run !== current || current.finishing) {
            await chrome.debugger.detach({tabId: current.tabId}).catch(() => {});
            throw new Error('Stopped');
          }
        }
        const stillActive = await active(current.tabId);
        if (run !== current || current.finishing || !stillActive) throw new Error('Stopped');
        const result = await chrome.debugger.sendCommand({tabId: current.tabId}, message.method, message.params);
        if (run === current) await offscreen({type: 'rpc_result', runId: current.id, id: message.id, result});
      } catch (_) {
        await offscreen({type: 'rpc_result', runId: current.id, id: message.id, error: 'Browser command failed'}).catch(() => {});
        if (run === current) await finish('操作を停止しました。ブラウザ接続または対象タブを確認してください。');
      }
    }
  })().then(() => respond({ok: true}), error => respond({error: error.message}));
  return true;
});
chrome.action.onClicked.addListener(() => { void chrome.runtime.openOptionsPage(); });
chrome.tabs.onActivated.addListener(info => { if (run && info.tabId !== run.tabId) void finish('タブが切り替わったため停止しました'); });
chrome.tabs.onRemoved.addListener(id => { if (run?.tabId === id) void finish('対象タブが閉じられました'); });
chrome.windows.onFocusChanged.addListener(id => {
  if (!run) return;
  const current = run;
  if (id === chrome.windows.WINDOW_ID_NONE) void finish('フォーカスが変わったため停止しました');
  else void active(current.tabId).then(ok => { if (!ok && run === current) void finish('フォーカスが変わったため停止しました'); });
});
chrome.debugger.onDetach.addListener(source => { if (run?.tabId === source.tabId && !run.finishing) void finish('ブラウザ接続が解除されました'); });
chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
