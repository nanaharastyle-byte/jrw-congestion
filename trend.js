// 列車をタップしたときに、号車ごとの乗車率の推移を表で表示する共通部品
// 1) 直近1時間の記録（liveブランチの recent.json）→ 2) 最新の走行記録（stats/trainlog/列車番号.json）の順に探す
(function () {
  const T0 = [40, 60, 80, 100, 130, 160];
  const SHORT = ['', '空席', 'ほぼ満席', 'ゆったり立', '吊り革', '多い', '大変多い', 'とても混雑'];
  const lvOf = (p, T) => { let l = 1; for (const t of T) if (p >= t) l++; return l; };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const gh = location.hostname.endsWith('.github.io');
  const owner = gh ? location.hostname.split('.')[0] : '';
  const repo = gh ? (location.pathname.split('/')[1] || '') : '';
  const RECENT = gh ? `https://raw.githubusercontent.com/${owner}/${repo}/live/recent.json` : 'recent.json';

  const css = document.createElement('style');
  css.textContent = `
  .tr-ov{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;display:flex;align-items:flex-end;justify-content:center}
  .tr-box{background:var(--paper,#fff);color:var(--ink,#1a2230);width:100%;max-width:760px;max-height:88%;overflow:auto;border-radius:14px 14px 0 0;
   padding:14px 12px calc(14px + env(safe-area-inset-bottom));box-shadow:0 -4px 20px rgba(0,0,0,.2)}
  .tr-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
  .tr-head h3{margin:0;font-size:16px}.tr-x{border:0;background:var(--bg,#eef2f6);color:inherit;border-radius:50%;width:32px;height:32px;font-size:18px}
  .tr-sub{font-size:12px;color:var(--sub,#5b6577);margin:4px 0 8px}
  .tr-tbl{border-collapse:collapse;font-size:11px}.tr-tbl th{position:sticky;top:0;background:var(--paper,#fff);color:var(--sub,#5b6577);font-weight:400;padding:3px;white-space:nowrap}
  .tr-tbl td{padding:3px 4px;text-align:center;white-space:nowrap;border-bottom:1px solid var(--rule,#d5dce6)}
  .tr-tbl td.s{text-align:left;color:var(--sub,#5b6577)}
  .tr-wrap{overflow-x:auto}.tr-sum{font-size:13px;margin:6px 0}`;
  document.head.appendChild(css);

  async function getT() {
    try { const i = await (await fetch('stats/index.json', { cache: 'no-store' })).json(); return i.T || T0; } catch (e) { return T0; }
  }
  async function load(no) {
    try {
      const r = await (await fetch(RECENT + (gh ? '' : '?t=' + Date.now()), { cache: 'no-store' })).json();
      const v = r.trains && r.trains[no];
      if (v && v.rows.length) return { src: `直近1時間の記録（${r.t.slice(11, 16)}時点・数分遅れ）`, typ: v.typ, dest: v.dest,
        rows: v.rows.map(x => [x[0], x[1], String(x[2]).split(';').map(c => c.split(':').map(Number))]) };
    } catch (e) { }
    try {
      const v = await (await fetch(`stats/trainlog/${encodeURIComponent(no)}.json`, { cache: 'no-store' })).json();
      return { src: `最新の走行記録（${v.date.slice(5).replace('-', '/')}）`, typ: v.typ, dest: v.dest, rows: v.rows };
    } catch (e) { return null; }
  }
  function sample(rows, n) {
    if (rows.length <= n) return rows;
    const out = []; for (let i = 0; i < n; i++) out.push(rows[Math.round(i * (rows.length - 1) / (n - 1))]); return out;
  }
  window.showTrend = async function (no, title) {
    const ov = document.createElement('div'); ov.className = 'tr-ov';
    ov.innerHTML = `<div class="tr-box" role="dialog" aria-label="号車別の混雑推移"><div class="tr-head"><h3>${esc(title || no)}</h3><button class="tr-x" aria-label="閉じる">×</button></div><div class="tr-sub">読み込み中…</div></div>`;
    document.body.appendChild(ov);
    const close = () => ov.remove();
    ov.onclick = e => { if (e.target === ov) close(); };
    ov.querySelector('.tr-x').onclick = close;
    const [T, d] = await Promise.all([getT(), load(no)]);
    const box = ov.querySelector('.tr-box');
    if (!d) { box.querySelector('.tr-sub').textContent = 'この列車の記録が見つかりませんでした'; return; }
    const cars = [...new Set(d.rows.flatMap(r => r[2].map(c => c[0])))].sort((a, b) => a - b);
    let peak = null;
    for (const r of d.rows) for (const [c, p] of r[2]) if (!peak || p > peak[1]) peak = [c, p, r[0], r[1]];
    const rows = sample(d.rows, 45);
    let h = `<div class="tr-sub">${esc(no)} ${esc(d.typ)} ${esc(d.dest)}行／${esc(d.src)}</div>`;
    if (peak) h += `<div class="tr-sum">最も混んだ号車：<b>${peak[0]}号車 ${peak[1]}%</b>（${esc(String(peak[2]).slice(0, 5))} ${esc(peak[3])}）</div>`;
    h += '<div class="tr-wrap"><table class="tr-tbl"><tr><th>時刻</th><th>区間</th>' + cars.map(c => `<th>${c}号車</th>`).join('') + '</tr>';
    for (const r of rows) {
      const m = Object.fromEntries(r[2]);
      h += `<tr><td>${esc(String(r[0]).slice(0, 5))}</td><td class="s">${esc(r[1])}</td>` + cars.map(c => {
        const p = m[c]; if (p == null || p < 0) return '<td>-</td>';
        const l = lvOf(p, T); return `<td style="background:var(--l${l});color:${l === 1 || l === 4 ? '#1a2230' : '#fff'}" title="${SHORT[l]}">${p}</td>`;
      }).join('') + '</tr>';
    }
    h += '</table></div><div class="tr-sub">数字は乗車率(%)。色は混雑の7段階。変化があった時点の記録を表示しています。</div>';
    box.innerHTML = box.querySelector('.tr-head').outerHTML + h;
    box.querySelector('.tr-x').onclick = close;
  };
})();
