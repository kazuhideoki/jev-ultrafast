(() => {
  let held = false;
  let recording = false;
  let continuous = false;
  let overlay;
  const send = type => chrome.runtime.sendMessage({target: 'background', type}).catch(() => {});
  // One push-to-talk shortcut. Listen to modifier release as well on macOS.
  window.addEventListener('keydown', e => {
    if (e.isTrusted && e.code === 'KeyJ' && e.metaKey && e.shiftKey && !e.altKey && !e.ctrlKey) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (!e.repeat) send('toggle-continuous');
    }
    if (e.isTrusted && e.code === 'KeyI' && e.metaKey && e.shiftKey && !e.altKey && !e.ctrlKey) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (!held && !e.repeat) { held = true; send('hold-start'); }
    }
    if (e.isTrusted && e.key === 'Escape') { held = false; send('cancel'); }
  }, true);
  window.addEventListener('keyup', e => {
    if (e.isTrusted && held && (e.code === 'KeyI' || !e.metaKey || !e.shiftKey)) {
      e.preventDefault(); held = false; send('commit');
    }
  }, true);
  window.addEventListener('blur', () => {
    if (!continuous && (held || recording)) { held = false; send('cancel-recording'); }
  });
  window.addEventListener('pagehide', () => { if (!continuous && (held || recording)) send('cancel-recording'); });
  chrome.runtime.onMessage.addListener(message => {
    if (message.type !== 'display') return;
    recording = message.recording;
    continuous = message.continuous;
    if (!overlay?.isConnected) {
      overlay = document.createElement('div');
      overlay.setAttribute('aria-hidden', 'true');
      const shadow = overlay.attachShadow({mode: 'closed'});
      const label = document.createElement('div');
      label.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:2147483647;background:#17221f;color:#fff;padding:12px 18px;border-radius:12px;font:14px system-ui;max-width:440px;pointer-events:none;box-shadow:0 3px 20px #0004';
      const state = document.createElement('div');
      const transcript = document.createElement('div');
      transcript.style.cssText = 'margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto;color:#d7eee3';
      label.append(state, transcript);
      shadow.append(label); overlay.label = state; overlay.transcript = transcript;
      document.documentElement.append(overlay);
    }
    overlay.label.textContent = message.text;
    overlay.transcript.textContent = [message.transcript ? `指示：${message.transcript}` : '', message.goal ? `目標：${message.goal}` : '', message.partial ? `聞き取り：${message.partial}` : ''].filter(Boolean).join('\n');
    overlay.transcript.hidden = !overlay.transcript.textContent;
    clearTimeout(overlay.timer);
    if (message.terminal) overlay.timer = setTimeout(() => overlay.remove(), 15000);
  });
  send('restore');
})();
