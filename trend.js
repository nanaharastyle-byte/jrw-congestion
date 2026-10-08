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
  .tr-tbl tr.gap td{background:transparent;font-size:10px}
  .tr-tbl tr.tr-st{cursor:pointer}.tr-tbl tr.tr-st td:first-child u{color:var(--acc,#0072bc)}
  .tr-save{width:100%;margin-top:8px;padding:9px;border:0;border-radius:8px;background:var(--acc,#0072bc);color:#fff;font-weight:700;font-size:14px}`;
  document.head.appendChild(css);

  // 路線ごとの駅順（記録が抜けた駅を「記録なし」として表に入れるため）
  const LST = ['長浜 田村 坂田 米原 彦根 南彦根 河瀬 稲枝 能登川 安土 近江八幡 篠原 野洲 守山 栗東 草津 南草津 瀬田 石山 膳所 大津 山科 京都 西大路 桂川 向日町 長岡京 山崎 島本 高槻 摂津富田 JR総持寺 茨木 千里丘 岸辺 吹田 東淀川 新大阪 大阪',
    '京都 山科 大津京 唐崎 比叡山坂本 おごと温泉 堅田 小野 和邇 蓬莱 志賀 比良 近江舞子 北小松 近江高島 安曇川 新旭 近江今津 近江中庄 マキノ 永原 近江塩津',
    '京都 梅小路京都西 丹波口 二条 円町 花園 太秦 嵯峨嵐山 保津峡 馬堀 亀岡 並河 千代川 八木 吉富 園部',
    '京都 東福寺 稲荷 JR藤森 桃山 六地蔵 木幡 黄檗 宇治 JR小倉 新田 城陽 長池 山城青谷 山城多賀 玉水 棚倉 上狛 木津 平城山 奈良'].map(x => x.split(' '));
  const nrm = s => String(s || '').replace(/Ｊ\s*Ｒ/g, 'JR').trim();
  // 路線ごとの駅（お気に入り登録のとき、どの路線の駅かを判定する）
  const LINE_ST = {
    hokurikubiwako: ['琵琶湖線', '長浜 田村 坂田 米原 彦根 南彦根 河瀬 稲枝 能登川 安土 近江八幡 篠原 野洲 守山 栗東 草津 南草津 瀬田 石山 膳所 大津 山科 京都'],
    kyoto: ['JR京都線', '京都 西大路 桂川 向日町 長岡京 山崎 島本 高槻 摂津富田 JR総持寺 茨木 千里丘 岸辺 吹田 東淀川 新大阪 大阪'],
    sagano: ['嵯峨野線', '京都 梅小路京都西 丹波口 二条 円町 花園 太秦 嵯峨嵐山 保津峡 馬堀 亀岡 並河 千代川 八木 吉富 園部'],
    nara: ['奈良線', '京都 東福寺 稲荷 JR藤森 桃山 六地蔵 木幡 黄檗 宇治 JR小倉 新田 城陽 長池 山城青谷 山城多賀 玉水 棚倉 上狛 木津 平城山 奈良'],
    kosei: ['湖西線', '京都 山科 大津京 唐崎 比叡山坂本 おごと温泉 堅田 小野 和邇 蓬莱 志賀 比良 近江舞子 北小松 近江高島 安曇川 新旭 近江今津 近江中庄 マキノ 永原 近江塩津']};
  for (const k in LINE_ST) LINE_ST[k][1] = LINE_ST[k][1].split(' ');
  function lineOf(rows, i) {
    const st = names(rows[i][1])[0], cand = Object.keys(LINE_ST).filter(k => LINE_ST[k][1].includes(st));
    if (cand.length <= 1) return cand[0] || '';
    for (const j of [i - 1, i + 1, i - 2, i + 2]) {                 // 前後の駅・区間と同じ路線を選ぶ
      if (!rows[j]) continue;
      const ns = names(rows[j][1]).filter(n => n !== st), k = cand.find(c => ns.length && ns.every(n => LINE_ST[c][1].includes(n)));
      if (k) return k;
    }
    return cand[0];
  }
  // ===== お気に入り（駅・時刻・列車）。巡回ページと共通の保存場所 =====
  const PKEY = 'jrw-fav-points';
  const getPts = () => { try { return JSON.parse(localStorage.getItem(PKEY) || '[]'); } catch (e) { return []; } };
  const putPts = a => { try { localStorage.setItem(PKEY, JSON.stringify(a)); return true; } catch (e) { alert('この端末には保存できませんでした'); return false; } };
  function favDialog(pt) {
    const label = `${pt.lineName} ${pt.station}駅 ${pt.time.replace(/^0/, '')} ${pt.dirName}`;
    const groups = [...new Set(getPts().map(p => p.group))];
    const d = document.createElement('div'); d.className = 'tr-ov'; d.style.zIndex = 60;
    d.innerHTML = `<div class="tr-box" role="dialog" aria-label="お気に入りに登録"><div class="tr-head"><h3>★ お気に入りに登録</h3><button class="tr-x" aria-label="閉じる">×</button></div>
      <div class="tr-sum"><b>${esc(label)}</b><br>${esc(pt.no)} ${esc(pt.typ)} ${esc(pt.dest)}行${pt.max >= 0 ? `・この駅で最大${pt.max}%` : ''}${pt.cars.length ? `（${pt.cars.join('・')}号車）` : ''}</div>
      <div class="tr-sub">登録するグループ</div>
      <select class="tr-sel" id="fgsel"><option value="__new">＋ 新しいグループ</option>${groups.map(g => `<option>${esc(g)}</option>`).join('')}</select>
      <input id="fgnew" class="tr-sel" value="${esc(label)}" aria-label="新しいグループ名">
      <button class="tr-save" id="fgok">登録する</button></div>`;
    document.body.appendChild(d);
    const sel = d.querySelector('#fgsel'), nw = d.querySelector('#fgnew');
    sel.onchange = () => { nw.style.display = sel.value === '__new' ? '' : 'none'; };
    d.querySelector('.tr-x').onclick = () => d.remove();
    d.onclick = e => { if (e.target === d) d.remove(); };
    d.querySelector('#fgok').onclick = () => {
      const g = sel.value === '__new' ? (nw.value.trim() || label) : sel.value;
      const a = getPts();
      if (a.some(p => p.group === g && p.no === pt.no && p.station === pt.station)) { alert('同じグループに登録済みです'); return; }
      a.push({ ...pt, id: Date.now(), group: g, label });
      if (putPts(a)) { d.remove(); alert(`「${g}」に登録しました。巡回ページの「お気に入り駅・列車を経由」で使えます。`); }
    };
  }
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
  // 日ごとの全走行区間。今日の分は、1時間ごとの集計のあとに走った区間を直近の記録（recent.json）から足す
  async function sources(no) {
    const days = {};
    let meta = {};
    try {
      const v = await (await fetch(`stats/trainlog/${encodeURIComponent(no)}.json`, { cache: 'no-store' })).json();
      for (const [ds, d] of Object.entries(v.days || {})) days[ds] = { typ: d.typ, dest: d.dest, dir: d.dir, rows: d.rows.map(r => [r[0], r[1], r[2], r[3]]) };
      meta = v;
    } catch (e) { }
    try {
      const r = await (await fetch(RECENT + (gh ? '' : '?t=' + Date.now()), { cache: 'no-store' })).json();
      const v = r.trains && r.trains[no];
      if (v && v.rows.length) {
        const ds = r.t.slice(0, 10), add = bySection(v.rows.map(x => [x[0], x[1], String(x[2]).split(';').map(c => c.split(':').map(Number)), x[3]]));
        const d = days[ds] || (days[ds] = { typ: v.typ, dest: v.dest, dir: v.dir, rows: [] });
        const last = d.rows.length ? d.rows[d.rows.length - 1][0] : '';
        for (const row of add) {
          const lr = d.rows[d.rows.length - 1];
          if (lr && lr[1] === row[1]) {                       // 同じ区間の続きなら最大値でまとめる
            lr[2] = Math.max(lr[2], row[2]); const m = Object.fromEntries(lr[3]);
            for (const [c, p] of row[3]) m[c] = Math.max(m[c] ?? -1, p);
            lr[3] = Object.entries(m).map(([c, p]) => [+c, p]);
          } else if (row[0] >= last) d.rows.push(row);
        }
        d.live = r.t.slice(11, 16);
      }
    } catch (e) { }
    return Object.keys(days).sort().reverse().map(ds => {
      const d = days[ds], dd = new Date(ds + 'T12:00:00+09:00');
      const mx = Math.max(-1, ...d.rows.flatMap(r => r[3].map(c => c[1])));
      const dl = Math.max(0, ...d.rows.map(r => r[2] || 0));
      return { label: `${dd.getMonth() + 1}/${dd.getDate()}(${WD[dd.getDay()]})　最大${mx}%${dl ? `・遅れ最大${dl}分` : ''}${d.live ? `（${d.live}まで）` : ''}`,
        typ: d.typ, dest: d.dest, dir: d.dir, date: ds, rows: d.rows };
    });
  }
  function table(src, T, no) {
    const rows = fillGaps(src.rows);
    const cars = [...new Set(src.rows.flatMap(r => r[3].map(c => c[0])))].sort((a, b) => a - b);
    let peak = null;
    for (const r of src.rows) for (const [c, p] of r[3]) if (!peak || p > peak[1]) peak = [c, p, r[0], r[1]];
    let h = `<div class="tr-sub">${esc(no)} ${esc(src.typ)} ${esc(src.dest)}行</div>`;
    if (peak) h += `<div class="tr-sum">最も混んだ号車：<b>${peak[0]}号車 ${peak[1]}%</b>（${esc(peak[2])} ${esc(peak[3])}）</div>`;
    h += '<div class="tr-wrap"><table class="tr-tbl"><tr><th>時刻</th><th>駅・区間</th>' + cars.map(c => `<th>${c}号車</th>`).join('') + '</tr>';
    rows.forEach((r, ri) => { r.__i = ri; });
    for (const r of rows) {
      if (!r[3]) { h += `<tr class="gap"><td></td><td class="s">${esc(r[1])}</td><td colspan="${cars.length}" style="color:var(--sub,#5b6577)">記録なし</td></tr>`; continue; }
      const m = Object.fromEntries(r[3]);
      const isSt = String(r[1]).endsWith('駅');
      h += `<tr${isSt ? ` class="tr-st" data-ri="${r.__i}"` : ''}><td>${isSt ? '<u>' + esc(r[0]) + '</u>' : esc(r[0])}</td><td class="s">${isSt ? '☆ ' : ''}${esc(r[1])}${r[2] > 0 ? ` <span class="tr-dl">遅（${r[2]}分）</span>` : ''}</td>` + cars.map(c => {
        const p = m[c]; if (p == null || p < 0) return '<td>-</td>';
        const l = lvOf(p, T); return `<td style="background:var(--l${l});color:${l === 1 || l === 4 ? '#1a2230' : '#fff'}" title="${SHORT[l]}">${p}</td>`;
      }).join('') + '</tr>';
    }
    return h + '</table></div><div class="tr-sub">☆の付いた駅の行をタップすると、その駅・時刻・列車をお気に入りに登録できます（巡回ルートの経由点に使えます）。<br>数字は乗車率(%)。同じ駅・区間に複数の記録があるときは最も高い値。赤の「遅」はその区間での遅れ。「記録なし」はその駅で位置が記録されなかった（短時間で通過した等）ことを示します。</div>';
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
    const show = () => {
      const src = list[+sel.value];
      view.innerHTML = table(src, T, no);
      const rows = fillGaps(src.rows);
      view.querySelectorAll('.tr-st').forEach(tr => tr.onclick = () => {
        const i = +tr.dataset.ri, r = rows[i], ln = lineOf(rows, i), st = names(r[1])[0];
        const m = Object.fromEntries(r[3] || []), mx = Math.max(-1, ...Object.values(m));
        const hot = Object.entries(m).filter(([, p]) => p >= (T[4] || 130)).map(([c]) => +c).sort((a, b) => a - b);
        favDialog({ line: ln, lineName: (LINE_ST[ln] || [''])[0], station: st, time: String(r[0]).slice(0, 5), dir: String(src.dir ?? ''),
          dirName: src.dir == 0 ? '上り' : src.dir == 1 ? '下り' : '', no, typ: src.typ, dest: src.dest, max: mx, cars: hot, date: src.date || '' });
      });
    };
    sel.onchange = show; show();
  };
})();
