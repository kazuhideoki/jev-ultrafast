const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');
const deferred = () => {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
};
const tick = () => new Promise(resolve => setImmediate(resolve));
function runtime() {
  let listener;
  const noop = async () => {};
  const event = {addListener() {}};
  const commands = [];
  const chrome = {
    runtime: {onMessage: {addListener(fn) { listener = fn; }},
      getURL: p => 'chrome-extension://test/' + p, sendMessage: noop, openOptionsPage: noop,
      getContexts: async () => [{contextType: 'OFFSCREEN_DOCUMENT'}]},
    offscreen: {createDocument: noop}, // Chrome 116: no hasDocument API.
    storage: {local: {setAccessLevel: noop}, session: {set: noop}},
    action: {setBadgeText: noop, setTitle: noop, onClicked: event},
    tabs: {query: async () => [{id: 1}], sendMessage: noop, onActivated: event, onRemoved: event},
    windows: {onFocusChanged: event},
    debugger: {attach: noop, detach: noop, sendCommand: async (...args) => {commands.push(args); return {};}, onDetach: event},
  };
  const context = vm.createContext({chrome, setTimeout, clearTimeout, console});
  vm.runInContext(source, context);
  return {chrome, context, commands, send: (msg, sender) => new Promise(resolve => listener(msg, sender, resolve))};
}
test('Esc while final active-tab check is pending prevents a new mutation', async () => {
  const env = runtime();
  const query = deferred(), badge = deferred();
  let queries = 0;
  env.chrome.tabs.query = () => ++queries === 2 ? query.promise : Promise.resolve([{id: 1}]);
  env.chrome.action.setBadgeText = () => badge.promise;
  vm.runInContext("run = {id:'one',tabId:1,phase:'processing',attached:true}", env.context);
  const rpc = env.send({target:'background',type:'rpc',runId:'one',id:1,method:'Input.insertText',params:{text:'value'}},
    {url:'chrome-extension://test/offscreen.html'});
  await tick();
  assert.equal(queries, 2);
  const cancel = env.send({target:'background',type:'cancel'}, {tab:{id:1},frameId:0});
  query.resolve([{id:1}]);
  await rpc;
  assert.equal(env.commands.length, 0, 'must not dispatch after cancellation');
  badge.resolve(); await cancel;
});
test('offscreen reuse works using the Chrome 116 runtime API', async () => {
  const env = runtime();
  let creates = 0;
  env.chrome.offscreen.createDocument = async () => {creates++;};
  await vm.runInContext('ensureOffscreen()', env.context);
  assert.equal(creates, 0);
  env.chrome.runtime.getContexts = async () => [];
  await vm.runInContext('ensureOffscreen()', env.context);
  assert.equal(creates, 1);
});

for (const impostor of [
  {type:'rpc', id:1, method:'Runtime.evaluate', params:{expression:'1'}},
  {type:'challenge', nonce:'a'.repeat(64), proof:'0'.repeat(64)},
]) test(`untrusted loopback server cannot release audio or execute ${impostor.type}`, async () => {
  let listener, socket;
  const notices = [], sent = [];
  class WebSocket {
    static OPEN = 1;
    constructor() { socket = this; this.readyState = 1; }
    send(raw) { sent.push(JSON.parse(raw)); }
    close() { this.readyState = 3; }
  }
  const context = vm.createContext({
    crypto: require('node:crypto').webcrypto, TextEncoder, Uint8Array, WebSocket,
    setTimeout, clearTimeout,
    chrome: {runtime: {
      onMessage: {addListener(fn) {listener = fn;}},
      sendMessage: async msg => {notices.push(msg);},
    }},
    navigator: {mediaDevices: {getUserMedia: () => new Promise(() => {})}},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../extension/offscreen.js'), 'utf8'), context);
  listener({target:'offscreen', type:'start', runId:'one', token:'pairing-secret'}, {}, () => {});
  for (let i=0; i<100 && !socket; i++) await new Promise(r => setTimeout(r, 1));
  assert.ok(socket);
  socket.onopen();
  vm.runInContext("current.pending.push({type:'audio',audio:'private-audio'})", context);
  socket.onmessage({data:JSON.stringify(impostor)});
  for (let i=0; i<100 && !notices.length; i++) await new Promise(r => setTimeout(r, 1));
  assert.equal(notices.length, 1);
  assert.equal(notices[0].type, 'failed');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, 'hello');
  assert.ok(!JSON.stringify(sent).includes('pairing-secret'));
  assert.ok(!JSON.stringify(sent).includes('private-audio'));
});
