// ページの設定（プルダウン・チェック・時刻など）をこの端末のブラウザに覚えておき、
// 再読み込みしても元に戻す。「設定を初期化」を押したときだけ初期設定に戻る。
(function () {
  const KEY = 'jrw-prefs:' + location.pathname.replace(/\/$/, '/index.html');
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } };
  const store = d => { try { localStorage.setItem(KEY, JSON.stringify(d)); } catch (e) { } };
  const fields = () => [...document.querySelectorAll('header select[id], header input[id]')].filter(el => el.type !== 'search');
  let restoring = true, applying = false;
  function save() {
    if (restoring) return;
    const d = {};
    for (const el of fields()) d[el.id] = el.type === 'checkbox' ? el.checked : el.value;
    const seg = document.querySelector('.seg button[aria-pressed="true"]');
    if (seg && seg.dataset.v) d.__seg = seg.dataset.v;
    store(d);
  }
  // 保存した値を戻す。選択肢があとから読み込まれるプルダウンは、選択肢が揃ったときに戻す
  const pending = load();
  function apply() {
    applying = true;
    let changed = false;
    for (const el of fields()) {
      if (!(el.id in pending)) continue;
      const v = pending[el.id];
      if (el.type === 'checkbox') { if (el.checked !== v) { el.checked = v; changed = true; el.dispatchEvent(new Event('change', { bubbles: true })); } delete pending[el.id]; continue; }
      if (el.tagName === 'SELECT' && ![...el.options].some(o => o.value === String(v))) continue;   // まだ選択肢がない
      if (el.value !== String(v)) { el.value = v; changed = true; el.dispatchEvent(new Event('change', { bubbles: true })); }
      delete pending[el.id];
    }
    if (pending.__seg) {
      const b = document.querySelector(`.seg button[data-v="${pending.__seg}"]`);
      if (b) { if (b.getAttribute('aria-pressed') !== 'true') b.click(); delete pending.__seg; }
    }
    applying = false;
    return changed;
  }
  function start() {
    apply();
    const obs = new MutationObserver(() => { if (Object.keys(pending).length) apply(); });
    for (const el of document.querySelectorAll('header select')) obs.observe(el, { childList: true, subtree: true });
    setTimeout(() => { restoring = false; save(); }, 4000);    // 読み込みが落ち着いたら保存を始める
    document.addEventListener('change', e => { if (!applying && e.target.closest('header')) { restoring = false; save(); } });
    document.addEventListener('click', e => { if (e.target.closest('.seg')) { restoring = false; setTimeout(save, 0); } });
    // 「設定を初期化」ボタン
    const h = document.querySelector('header');
    if (h && !document.getElementById('prefreset')) {
      const b = document.createElement('button');
      b.id = 'prefreset'; b.type = 'button'; b.textContent = '設定を初期化';
      b.style.cssText = 'margin-top:8px;border:1px solid var(--rule);background:var(--bg);color:var(--ink);border-radius:6px;padding:4px 10px;font-size:12px';
      b.onclick = () => { if (confirm('このページの設定を初期設定に戻しますか？')) { try { localStorage.removeItem(KEY); } catch (e) { } location.reload(); } };
      h.appendChild(b);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
