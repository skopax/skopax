var CHAT = {card: '', thread: '', framed: false};
var CHAT_CARD_COOKIE = 'skopax_chat_card';
var CHAT_THEME_KEY = 'vite-ui-theme';
var CHAT_READY_MS = 12000;
var CHAT_RETRIES = 1;
function chatTheme() {
  var set = document.documentElement.dataset.theme;
  if (set === 'light' || set === 'dark') return set;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function chatReveal(wrap) { if (wrap) wrap.classList.add('ready'); }
function chatWatch(wrap, frame, tries) {
  var started = Date.now();
  tries = tries || 0;
  var tick = function () {
    if (!wrap.isConnected || wrap.classList.contains('ready') || !frame.isConnected) return;
    var doc = null;
    try { doc = frame.contentDocument; } catch (e) {}
    if (doc && doc.querySelector('#chat-input, textarea')) { chatReveal(wrap); return; }
    if (Date.now() - started > CHAT_READY_MS) {
      if (tries >= CHAT_RETRIES) { chatReveal(wrap); return; }
      var fresh = frame.cloneNode(false);
      frame.replaceWith(fresh);
      var note = wrap.querySelector('.chat-loading');
      if (note) note.lastChild.textContent = 'Reconnecting the chat…';
      chatWatch(wrap, fresh, tries + 1);
      return;
    }
    setTimeout(tick, 120);
  };
  tick();
}
function chatPath() { return CHAT.thread ? 'chat/thread/' + CHAT.thread : CHAT.card ? 'chat/new/' + CHAT.card : 'chat'; }
function chatRef(a) { return a.num ? String(a.num) : a.key; }
function openChatPath(parts) {
  CHAT.thread = parts[0] === 'thread' && /^[A-Za-z0-9_-]{1,64}$/.test(parts[1] || '') ? parts[1] : '';
  CHAT.card = parts[0] === 'new' ? parts.slice(1).join('/') : '';
}
async function loadChat() {
  var box = document.getElementById('chat');
  if (!box) return;
  var me = null;
  try {
    var r = await fetch('/api/me', {headers: {'Accept': 'application/json'}, credentials: 'same-origin'});
    if (r.ok) me = await r.json();
  } catch (e) {}
  if (!me || !me.chat) { CHAT.framed = false; box.innerHTML = '<p class="chat-login">The chat is not set up on this service: it needs login.</p>'; return; }
  if (!me.user) {
    CHAT.framed = false;
    box.innerHTML = '<p class="chat-login">Log in to chat with ' + esc(brandName()) + '. <a href="/login?next=' + encodeURIComponent(location.pathname + '#t=' + chatPath()) + '">Log in</a></p>';
    return;
  }
  if (CHAT.framed && !CHAT.card && !CHAT.thread) return;
  var src = '/chat/';
  if (CHAT.thread) {
    src += 'thread/' + encodeURIComponent(CHAT.thread);
  } else if (CHAT.card) {
    document.cookie = CHAT_CARD_COOKIE + '=' + encodeURIComponent(CHAT.card) + '; path=/chat; max-age=20; samesite=lax';
    src += '?card=' + encodeURIComponent(CHAT.card);
  }
  try { localStorage.setItem(CHAT_THEME_KEY, chatTheme()); } catch (e) {}
  box.innerHTML = '<div class="chat-wrap"><div class="chat-loading" role="status"><span class="chat-spin" aria-hidden="true"></span>Loading the chat…</div>' +
    '<iframe class="chat-frame" title="Chat with ' + esc(brandName()) + '" src="' + esc(src) + '"></iframe></div>' +
    '<p class="chat-open"><a href="/chat/" target="_blank" rel="noopener">Open the chat in its own tab \u2197</a> \u00b7 Language and light or dark are in <a href="#t=prefs">Settings</a>.</p>';
  chatWatch(box.querySelector('.chat-wrap'), box.querySelector('.chat-frame'));
  CHAT.framed = true;
  CHAT.card = '';
  CHAT.thread = '';
  markTab();
}
