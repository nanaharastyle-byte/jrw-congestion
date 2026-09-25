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
  .tr-wrap{overflow-x:auto}.tr-sum{font-size:13px;margin:6px 0}
  .tr-sel{width:100%;font-size:14px;padding:7px;border-radius:8px;border:1px solid var(--rule,#d5dce6);background:var(--bg,#eef2f6);color:inherit;margin:4px 0 6px}
  .tr-dl{color:#fff;background:#c23b22;border-radius:3px;padding:0 4px;font-weight:700;font-size:10px;white-space:nowrap}
  .tr-tbl tr.gap td{background:transparent;font-size:10px}`;
  document.head.appendChild(css);

  // 路線ごとの駅順（記録が抜けた駅を「記録なし」として表に入れるため）
  const LST = ['長浜 田村 坂田 米原 彦根 南彦根 河瀬 稲枝 能登川 安土 近江八幡 篠原 野洲 守山 栗東 草津 南草津 瀬田 石山 膳所 大津 山科 京都 西大路 桂川 向日町 長岡京 山崎 島本 高槻 摂津富田 JR総持寺 茨木 千里丘 岸辺 吹田 東淀川 新大阪 大阪',
    '京都 梅小路京都西 丹波口 二条 円町 花園 太秦 嵯峨嵐山 保津峡 馬堀 亀岡 並河 千代川 八木 吉富 園部',
    '京都 東福寺 稲荷 JR藤森 桃山 六地蔵 木幡 黄檗 宇治 JR小倉 新田 城陽 長池 山城青谷 山城多賀 玉水 棚倉 上狛 木津 平城山 奈良'].map(x => x.split(' '));
  const nrm = s => String(s || '').replace(/Ｊ\s*Ｒ/g, 'JR').trim();
  const names = sec => { sec = nrm(sec); return sec.endsWith('駅') ? [sec.slice(0, -1)] : sec.split('〜'); };
  const WD = '日月火水木金土';
  async function getT() {
    try { const i = await (await fetch('stats/index.json', { cache: 'no-store' })).json(); return i.T || T0; } catch (e) { return T0; }
  }
  // 同じ区間が続く記録を1行にまとめる（号車ごとに最大の乗車率、最大の遅れ）
  function bySection(rows) {
    const out = [];
    for (const [t, sec, cars, dl] of rows) {
      const s = nrm(sec); let r = out[out.length - 1];
      if (!r || r[1] !== s) { r = [String(t).slice(0, 5), s, 0, {}]; out.push(r); }
      r[2] = Math.max(r[2], Number(dl) || 0);
      for (const [c, p] of cars) if (p != null && p >= 0) r[3][c] = Math.max(r[3][c] ?? -1, p);
    }
    return out.map(r => [r[0], r[1], r[2], Object.entries(r[3]).map(([c, p]) => [+c, p])]);
  }
  // 前後の記録の間で飛ばされた駅を「記録なし」として挟む
  function fillGaps(rows) {
    const out = [];
    for (let i = 0; i < rows.length; i++) {
      if (i > 0) {
        const a = names(rows[i - 1][1]).at(-1), b = names(rows[i][1])[0];
        for (const L of LST) {
          const ia = L.indexOf(a), ib = L.indexOf(b);
          if (ia >= 0 && ib >= 0 && Math.abs(ia - ib) > 1 && Math.abs(ia - ib) <= 10) {
            const st = ia < ib ? 1 : -1;
            for (let k = ia + st; k !== ib; k += st) out.push(['', L[k] + '駅', 0, null]);
            break;
          }
        }
      }
      out.push(rows[i]);
    }
    return out;
  }
  async function sources(no) {
    const list = [];
    try {
      const r = await (await fetch(RECENT + (gh ? '' : '?t=' + Date.now()), { cache: 'no-store' })).json();
      const v = r.trains && r.trains[no];
      if (v && v.rows.length) list.push({ label: `直近1時間（${r.t.slice(11, 16)}時点）`, typ: v.typ, dest: v.dest,
        rows: bySection(v.rows.map(x => [x[0], x[1], String(x[2]).split(';').map(c => c.split(':').map(Number)), x[3]])) });
    } catch (e) { }
    try {
      const v = await (await fetch(`stats/trainlog/${encodeURIComponent(no)}.json`, { cache: 'no-store' })).json();
      for (const ds of Object.keys(v.days || {}).sort().reverse()) {
        const d = v.days[ds], dd = new Date(ds + 'T12:00:00+09:00');
        const mx = Math.max(-1, ...d.rows.flatMap(r => r[3].map(c => c[1])));
        const dl = Math.max(0, ...d.rows.map(r => r[2] || 0));
        list.push({ label: `${dd.getMonth() + 1}/${dd.getDate()}(${WD[dd.getDay()]})　最大${mx}%${dl ? `・遅れ最大${dl}分` : ''}`,
          typ: d.typ, dest: d.dest, rows: d.rows.map(r => [r[0], r[1], r[2], r[3]]) });
      }
    } catch (e) { }
    return list;
  }
  function table(src, T, no) {
    const rows = fillGaps(src.rows);
    const cars = [...new Set(src.rows.flatMap(r => r[3].map(c => c[0])))].sort((a, b) => a - b);
    let peak = null;
    for (const r of src.rows) for (const [c, p] of r[3]) if (!peak || p > peak[1]) peak = [c, p, r[0], r[1]];
    let h = `<div class="tr-sub">${esc(no)} ${esc(src.typ)} ${esc(src.dest)}行</div>`;
    if (peak) h += `<div class="tr-sum">最も混んだ号車：<b>${peak[0]}号車 ${peak[1]}%</b>（${esc(peak[2])} ${esc(peak[3])}）</div>`;
    h += '<div class="tr-wrap"><table class="tr-tbl"><tr><th>時刻</th><th>駅・区間</th>' + cars.map(c => `<th>${c}号車</th>`).join('') + '</tr>';
    for (const r of rows) {
      if (!r[3]) { h += `<tr class="gap"><td></td><td class="s">${esc(r[1])}</td><td colspan="${cars.length}" style="color:var(--sub,#5b6577)">記録なし</td></tr>`; continue; }
      const m = Object.fromEntries(r[3]);
      h += `<tr><td>${esc(r[0])}</td><td class="s">${esc(r[1])}${r[2] > 0 ? ` <span class="tr-dl">遅（${r[2]}分）</span>` : ''}</td>` + cars.map(c => {
        const p = m[c]; if (p == null || p < 0) return '<td>-</td>';
        const l = lvOf(p, T); return `<td style="background:var(--l${l});color:${l === 1 || l === 4 ? '#1a2230' : '#fff'}" title="${SHORT[l]}">${p}</td>`;
      }).join('') + '</tr>';
    }
    return h + '</table></div><div class="tr-sub">数字は乗車率(%)。同じ駅・区間に複数の記録があるときは最も高い値。赤の「遅」はその区間での遅れ。「記録なし」はその駅で位置が記録されなかった（短時間で通過した等）ことを示します。</div>';
  }
  window.showTrend = async function (no, title) {
    const ov = document.createElement('div'); ov.className = 'tr-ov';
    ov.innerHTML = `<div class="tr-box" role="dialog" aria-label="号車別の混雑推移"><div class="tr-head"><h3>${esc(title || no)}</h3><button class="tr-x" aria-label="閉じる">×</button></div><div class="tr-body"><div class="tr-sub">読み込み中…</div></div></div>`;
    document.body.appendChild(ov);
    ov.onclick = e => { if (e.target === ov) ov.remove(); };
    ov.querySelector('.tr-x').onclick = () => ov.remove();
    const [T, list] = await Promise.all([getT(), sources(no)]);
    const body = ov.querySelector('.tr-body');
    if (!list.length) { body.innerHTML = '<div class="tr-sub">この列車の記録が見つかりませんでした</div>'; return; }
    body.innerHTML = `<select class="tr-sel" aria-label="日付">${list.map((s, i) => `<option value="${i}">${esc(s.label)}</option>`).join('')}</select><div class="tr-view"></div>`;
    const sel = body.querySelector('.tr-sel'), view = body.querySelector('.tr-view');
    const show = () => { view.innerHTML = table(list[+sel.value], T, no); };
    sel.onchange = show; show();
  };
})();
