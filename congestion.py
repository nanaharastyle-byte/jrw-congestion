#!/usr/bin/env python3
"""
JR西日本 車両別混雑ロガー（琵琶湖線・JR京都線・奈良線・嵯峨野線）

GitHub Actions から5分ごとに実行され、
  1. 列車走行位置のデータを取得
  2. 列車ごと・号車ごとの混雑レベル（7段階）を記録
  3. 集計ページ docs/index.html を作り直す
を行います。

保存先
  data/agg.json         … 累積集計（区間×号車×時間帯×混雑レベルの通過回数）
  data/detail/日付.csv  … 生データ（直近14日分のみ保持）
  docs/index.html       … 集計ページ（GitHub Pages で iPhone から閲覧）
  docs/sample_raw.json  … 最新の元データ見本（形式確認用）
"""
import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ORIGIN = "https://www.train-guide.westjr.co.jp"
LINES = {"hokurikubiwako": "琵琶湖線", "kyoto": "JR京都線", "nara": "奈良線", "sagano": "嵯峨野線"}

# サイトの凡例に合わせた7段階（数値→表示）。数値の対応が違っていたらここを直します。
LABELS = {1: "空席があります", 2: "ほぼ、席はうまっています", 3: "ゆったり立つことができます",
          4: "吊り革につかまることができます", 5: "多くの方が乗っています",
          6: "大変多くの方が乗っています", 7: "とても混雑しています"}
LABELS_EXP = {**LABELS, 2: "空席があります", 3: "ほぼ、席はうまっています", 4: "ほぼ、席はうまっています"}

DATA, DOCS = "data", "docs"
KEEP_DAYS = 14
CONG_KEY = re.compile(r"congest|crowd|konzatsu|混雑", re.I)


# ---------- 取得 ----------
def get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (personal congestion logger)",
        "Referer": ORIGIN + "/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def find_cong(obj):
    """混雑データらしき項目を探す（最初に見つかったもの）"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if CONG_KEY.search(k) and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = find_cong(v)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_cong(v)
            if r is not None:
                return r
    return None


def to_level(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        return int(m.group()) if m else None
    if isinstance(v, dict):
        for k in ("level", "congestion", "value", "rate", "status", "lv"):
            if k in v:
                return to_level(v[k])
        for k, x in v.items():
            if CONG_KEY.search(k):
                return to_level(x)
    return None


def car_levels(val):
    """号車ごとのレベル一覧（1号車から順）"""
    if isinstance(val, list):
        return [to_level(x) for x in val]
    if isinstance(val, dict):
        for x in val.values():
            if isinstance(x, list):
                return car_levels(x)
        if val and all(re.fullmatch(r"\d+", str(k)) for k in val):
            return [to_level(val[k]) for k in sorted(val, key=int)]
        return [to_level(val)]
    if isinstance(val, str):
        nums = re.findall(r"\d+", val)
        if len(nums) > 1:
            return [int(n) for n in nums]
        if re.fullmatch(r"[0-7]{2,}", val.strip()):
            return [int(ch) for ch in val.strip()]
    return [to_level(val)]


def station_map(st):
    m = {}
    for s in (st or {}).get("stations", []):
        info = s.get("info") or {}
        if "code" in info:
            m[str(info["code"])] = info.get("name", "")
    return m


def section_name(pos, m):
    if not pos:
        return "位置不明"
    a, _, b = str(pos).partition("_")
    if a in m and b in m:
        return f"{m[a]}〜{m[b]}"
    if a in m:
        return f"{m[a]}駅"
    return f"コード{pos}"


def day_type(d):
    try:
        import jpholiday
        if jpholiday.is_holiday(d):
            return "土休日"
    except ImportError:
        pass
    return "土休日" if d.weekday() >= 5 else "平日"


# ---------- 記録 ----------
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def collect(now, fetch=get):
    agg = load(f"{DATA}/agg.json", {"counts": {}, "meta": {}})
    state = load(f"{DATA}/state.json", {})
    meta = agg["meta"]
    counts = agg["counts"]
    new_state, rows, sample, errors = {}, [], None, []
    dtype, hour = day_type(now.date()), now.hour
    unknown = 0

    for line, lname in LINES.items():
        try:
            trains = fetch(f"{ORIGIN}/api/v3/{line}.json")
            stations = fetch(f"{ORIGIN}/api/v3/{line}_st.json")
        except Exception as e:
            errors.append(f"{lname}: {e}")
            continue
        m = station_map(stations)
        for t in trains.get("trains", []):
            cong = find_cong(t)
            if sample is None or (cong is not None and sample.get("_noCong")):
                sample = {**t, "_noCong": cong is None}
            if cong is None:
                continue
            levels = car_levels(cong)
            sec = section_name(t.get("pos"), m)
            ttype = str(t.get("displayType") or t.get("type") or "")
            tgroup = "特急" if "特急" in ttype else "一般"
            direction = str(t.get("direction", ""))
            dest = (t.get("dest") or {}).get("text", "") if isinstance(t.get("dest"), dict) else str(t.get("dest", ""))
            key = f"{line}|{t.get('no')}|{dest}"
            new_state[key] = sec
            rows.append([now.strftime("%Y-%m-%d %H:%M"), lname, t.get("no"), ttype, dest, direction, sec,
                         t.get("delayMinutes", ""), "".join("-" if v is None else str(v) for v in levels)])
            # 同じ列車が同じ区間にいる間は1回だけ数える（停車中の重複カウント防止）
            if state.get(key) == sec:
                continue
            for i, lv in enumerate(levels, start=1):
                if lv is None:
                    continue
                if lv not in LABELS:
                    unknown += 1
                    continue
                k = "|".join([line, direction, sec, tgroup, str(i), str(hour), dtype, str(lv)])
                counts[k] = counts.get(k, 0) + 1

    if len(errors) == len(LINES):
        print("すべての路線で取得に失敗しました:\n" + "\n".join(errors), file=sys.stderr)
        sys.exit(1)

    stamp = now.strftime("%Y-%m-%d %H:%M")
    meta.setdefault("first", stamp)
    meta["last"] = stamp
    meta["runs"] = meta.get("runs", 0) + 1
    meta["unknownLevels"] = meta.get("unknownLevels", 0) + unknown
    meta["lastErrors"] = errors
    meta["lastTrainsWithData"] = len(rows)

    save(f"{DATA}/agg.json", agg)
    save(f"{DATA}/state.json", new_state)
    if sample:
        save(f"{DOCS}/sample_raw.json", sample)

    # 生データ（1行＝1列車、混雑は号車順の数字列 例: 5567776）
    os.makedirs(f"{DATA}/detail", exist_ok=True)
    path = f"{DATA}/detail/{now:%Y-%m-%d}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["時刻", "路線", "列車番号", "種別", "行先", "方向", "位置", "遅延分", "号車別混雑(1号車から)"])
        w.writerows(rows)
    cutoff = (now.date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for p in glob.glob(f"{DATA}/detail/*.csv"):
        if os.path.basename(p)[:10] < cutoff:
            os.remove(p)
    return agg


# ---------- 集計ページ ----------
def build_report(agg):
    recs = []
    for k, c in agg["counts"].items():
        line, d, sec, tg, car, hr, dtp, lv = k.split("|")
        recs.append([line, d, sec, tg, int(car), int(hr), dtp, int(lv), c])
    payload = json.dumps({"recs": recs, "meta": agg["meta"], "lines": LINES,
                          "labels": LABELS, "labelsExp": LABELS_EXP}, ensure_ascii=False, separators=(",", ":"))
    os.makedirs(DOCS, exist_ok=True)
    with open(f"{DOCS}/index.html", "w", encoding="utf-8") as f:
        f.write(REPORT.replace("__DATA__", payload))


REPORT = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>混雑区間レポート</title>
<style>
:root{--bg:#eef2f6;--paper:#fff;--ink:#1a2230;--sub:#5b6577;--rule:#d5dce6;--l6:#a3336b;--l7:#3b3f9e;--acc:#0072bc}
@media (prefers-color-scheme:dark){:root{--bg:#141a22;--paper:#1d2530;--ink:#e8edf3;--sub:#9aa6b8;--rule:#2e3947}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;font-size:15px;line-height:1.5;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
header{background:var(--paper);border-bottom:1px solid var(--rule);padding:12px}
h1{font-size:18px;margin:0}
.meta{color:var(--sub);font-size:12px;margin-top:4px}
.f{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}
select{width:100%;font-size:14px;padding:6px;border-radius:6px;border:1px solid var(--rule);background:var(--bg);color:var(--ink)}
main{padding:10px;max-width:760px;margin:0 auto}
h2{font-size:15px;margin:14px 0 6px}
.item{background:var(--paper);border:1px solid var(--rule);border-radius:6px;padding:9px 10px;margin-bottom:6px;display:grid;grid-template-columns:2.2em 1fr auto;gap:8px;align-items:center}
.rank{font-weight:700;color:var(--sub);text-align:right}
.sec{font-weight:700}
.sub{color:var(--sub);font-size:12px}
.num{text-align:right;font-weight:700;font-size:18px}
.num small{display:block;font-size:11px;color:var(--sub);font-weight:400}
.bar{height:5px;border-radius:3px;background:var(--rule);margin-top:4px;overflow:hidden}
.bar i{display:block;height:100%}
.warn{background:#fff4d6;color:#6b4c00;border-radius:6px;padding:8px;font-size:13px;margin-bottom:8px}
.empty{color:var(--sub);padding:20px 0;text-align:center}
.legend{font-size:12px;color:var(--sub)}
</style></head><body>
<header><h1>混雑区間レポート</h1><div class="meta" id="meta"></div>
<div class="f">
<select id="line"></select>
<select id="lv"><option value="6,7">大変多く＋とても混雑</option><option value="7">とても混雑のみ</option><option value="6">大変多くのみ</option><option value="5,6,7">多く以上</option></select>
<select id="day"><option value="">全日</option><option>平日</option><option>土休日</option></select>
<select id="hr"><option value="">全時間帯</option><option value="5-9">朝 5〜9時台</option><option value="10-15">昼 10〜15時台</option><option value="16-19">夕 16〜19時台</option><option value="20-24">夜 20時以降</option></select>
<select id="tg"><option value="">全種別</option><option>一般</option><option>特急</option></select>
<select id="by"><option value="car">区間×号車</option><option value="sec">区間ごと</option><option value="carOnly">号車ごと</option></select>
</div></header>
<main><div id="warn"></div><div id="out"></div>
<p class="legend">回数＝その区間を通過した列車のうち、選んだ混雑レベルだった回数（1列車×1区間＝1回）。割合＝同じ条件での全通過回数に対する比率。ピーク＝最も多かった時間帯。</p>
</main>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
const DIR={"0":"上り","1":"下り"};
$('line').innerHTML='<option value="">全路線</option>'+Object.entries(D.lines).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');
const m=D.meta;
$('meta').textContent=m.first?`記録期間 ${m.first} 〜 ${m.last}（取得 ${m.runs}回）`:'まだデータがありません';
let w='';
if(m.lastErrors&&m.lastErrors.length)w+=`<div class="warn">直近の取得エラー: ${m.lastErrors.join(' / ')}</div>`;
if(m.unknownLevels)w+=`<div class="warn">想定外の混雑値が ${m.unknownLevels} 件ありました。値の対応を見直す必要があります。</div>`;
if(m.runs&&!m.lastTrainsWithData)w+=`<div class="warn">直近の取得で混雑データのある列車がありませんでした（深夜帯なら正常です）。</div>`;
$('warn').innerHTML=w;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render(){
 const line=$('line').value,lvs=$('lv').value.split(',').map(Number),day=$('day').value,tg=$('tg').value,by=$('by').value;
 const [h0,h1]=$('hr').value?$('hr').value.split('-').map(Number):[0,99];
 const g={};
 for(const [l,d,sec,t,car,hr,dt,lv,c] of D.recs){
  if(line&&l!==line)continue;if(day&&dt!==day)continue;if(tg&&t!==tg)continue;if(hr<h0||hr>h1)continue;
  const key=by==='car'?[l,d,sec,car].join('|'):by==='sec'?[l,d,sec].join('|'):[l,d,car].join('|');
  const o=g[key]||(g[key]={l,d,sec,car,hit:0,all:0,hrs:{}});
  o.all+=c;if(lvs.includes(lv)){o.hit+=c;o.hrs[hr]=(o.hrs[hr]||0)+c;}
 }
 const list=Object.values(g).filter(o=>o.hit>0).sort((a,b)=>b.hit-a.hit||b.hit/b.all-a.hit/a.all).slice(0,60);
 if(!list.length){$('out').innerHTML='<div class="empty">条件に合う記録はまだありません</div>';return;}
 const max=list[0].hit,col=lvs.length===1&&lvs[0]===6?'var(--l6)':'var(--l7)';
 $('out').innerHTML='<h2>混雑回数ランキング</h2>'+list.map((o,i)=>{
  const pk=Object.entries(o.hrs).sort((a,b)=>b[1]-a[1])[0];
  const title=by==='carOnly'?`${o.car}号車`:esc(o.sec)+(by==='car'?`　${o.car}号車`:'');
  return `<div class="item"><div class="rank">${i+1}</div><div><div class="sec">${title}</div>
  <div class="sub">${D.lines[o.l]} ${DIR[o.d]??'方向'+o.d}　ピーク ${pk[0]}時台</div>
  <div class="bar"><i style="width:${o.hit/max*100}%;background:${col}"></i></div></div>
  <div class="num">${o.hit}回<small>${Math.round(o.hit/o.all*100)}%</small></div></div>`}).join('');
}
['line','lv','day','hr','tg','by'].forEach(id=>$(id).onchange=render);render();
</script></body></html>"""


if __name__ == "__main__":
    now = dt.datetime.now(JST)
    if 2 <= now.hour < 4:   # 運行のない深夜は取得しない
        print("深夜帯のためスキップ")
        sys.exit(0)
    build_report(collect(now))
    print("完了", now.strftime("%Y-%m-%d %H:%M"))
