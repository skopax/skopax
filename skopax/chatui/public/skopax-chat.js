(function () {
  var theme = 'dark';
  try { theme = localStorage.getItem('vite-ui-theme') || 'dark'; } catch (e) {}
  if (theme === 'system') theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  var root = document.documentElement;
  root.classList.add(theme === 'light' ? 'light' : 'dark');
  root.style.colorScheme = theme === 'light' ? 'light' : 'dark';
  root.style.background = theme === 'light' ? '#f4f5f7' : '#0f1115';
  var framed = false;
  try { framed = window.top !== window.self; } catch (e) { framed = true; }
  if (framed) root.classList.add('in-frame');
})();
(function () {
  if (document.documentElement.classList.contains('in-frame') || typeof window.fetch !== 'function') return;
  function draw(line) {
    if (!line || !line.text || !line.href || document.getElementById('skopax-attribution') || !document.body) return;
    var foot = document.createElement('footer'), link = document.createElement('a');
    foot.id = 'skopax-attribution';
    foot.className = 'skopax-attribution';
    link.setAttribute('rel', 'noopener noreferrer');
    link.setAttribute('href', String(line.href));
    link.textContent = String(line.text);
    foot.appendChild(link);
    document.body.appendChild(foot);
  }
  try {
    window.fetch('/chat/public/skopax-brand.json', {credentials: 'same-origin'})
      .then(function (r) { return r && r.ok ? r.json() : null; })
      .then(function (d) { draw(d && d.attribution); })
      .catch(function () {});
  } catch (e) {}
})();
