"""Exercise the real extension dispatch gate with delayed browser API promises."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_extension_rechecks_epoch_after_async_browser_preflight():
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let listener, deferActive = false, resolveActive;
let commands = 0;
const replies = [];
const event = {addListener() {}};
const chrome = {
  runtime: {
    onMessage: {addListener(fn) {listener = fn;}},
    sendMessage: async m => {replies.push(m); return {ok: true};},
    getURL: s => 'chrome-extension://fixture/' + s,
    getContexts: async () => [{}],
  },
  storage: {
    local: {get: async () => ({token: 'offline'}), setAccessLevel() {}},
    session: {set: async () => {}},
  },
  action: {setBadgeText: async () => {}, setTitle: async () => {}, onClicked: event},
  tabs: {
    query: async () => {
      if (deferActive) {deferActive = false; await new Promise(r => {resolveActive = r;});}
      return [{id: 7}];
    },
    get: async () => ({url: 'https://example.test'}), sendMessage: async () => {},
    onActivated: event, onRemoved: event,
  },
  windows: {onFocusChanged: event, WINDOW_ID_NONE: -1},
  debugger: {
    attach: async () => {}, detach: async () => {}, onDetach: event,
    sendCommand: async () => {commands++; return {};},
  },
};
vm.runInThisContext(fs.readFileSync('extension/background.js', 'utf8'));
function send(message, sender) {
  return new Promise(resolve => listener({target:'background', ...message}, sender, resolve));
}
(async () => {
  await send({type:'toggle-continuous'}, {tab:{id:7}, frameId:0});
  const start = replies.find(m => m.type === 'start');
  const sender = {url:chrome.runtime.getURL('offscreen.html')};
  const msg = m => ({runId:start.runId, ...m});
  await send(msg({type:'gate', epoch:1, paused:false}), sender);
  deferActive = true;
  const rpc = send(msg({type:'rpc', id:1, epoch:1, method:'Input.insertText', params:{text:'old'}}), sender);
  await send(msg({type:'gate', epoch:2, paused:true}), sender);
  resolveActive();
  await rpc;
  assert.equal(commands, 0);
  assert.equal(replies.find(m => m.type === 'rpc_result').error, 'superseded');
  await send({type:'cancel'}, {tab:{id:7}, frameId:0});
})().catch(e => {console.error(e); process.exitCode=1;});
'''
    # vm code deliberately uses the real background.js; no extension implementation is copied.
    script = script.replace("vm.runInThisContext", "global.chrome = chrome; vm.runInThisContext")
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, timeout=10, capture_output=True, text=True)


def test_capture_health_stops_session_and_ignores_intentional_release():
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let now = 0, timer;
const notices = [];
const context = vm.createContext({
  chrome: {runtime: {sendMessage: async event => notices.push(event), onMessage: {addListener() {}}}},
  performance: {now: () => now},
  setInterval: callback => {timer = callback; return 1;}, clearInterval() {}, clearTimeout() {},
});
vm.runInContext(fs.readFileSync('extension/offscreen.js', 'utf8'), context);
for (const failure of ['ended', 'mute', 'context', 'processor', 'timeout', 'released']) {
  const handlers = {};
  let closed = 0, stopped = 0;
  const track = {readyState:'live', muted:false, addEventListener:(k, v)=>handlers[k]=v, stop:()=>stopped++};
  const session = {id:'fixture', stream:{getAudioTracks:()=>[track], getTracks:()=>[track]},
    context:{state:'running', addEventListener:(k,v)=>handlers[k]=v, close:async()=>{}},
    processor:{}, ws:{close:()=>closed++}};
  context.fixture = session;
  vm.runInContext('current = fixture; watchCapture(fixture)', context);
  const before = notices.length;
  if (failure === 'context') {session.context.state='suspended'; handlers.statechange();}
  else if (failure === 'processor') session.processor.onprocessorerror();
  else if (failure === 'timeout') {now += 2100; timer();}
  else if (failure === 'released') {session.released=true; handlers.ended();}
  else handlers[failure]();
  assert.equal(notices.length, before + (failure === 'released' ? 0 : 1));
  assert.equal(closed, failure === 'released' ? 0 : 1);
  assert.equal(stopped, failure === 'released' ? 0 : 1);
}
'''
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, timeout=10, capture_output=True, text=True)
