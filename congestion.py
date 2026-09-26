#!/usr/bin/env python3
"""
JR西日本 車両別混雑ロガー ＋ 天気・遅延・混雑予測
（琵琶湖線・JR京都線・奈良線・嵯峨野線）

  python congestion.py collect  … 5分ごと
      data/raw/日付.csv   列車ごとの位置・遅延・号車別乗車率（混雑データのある列車）
      data/cond/日付.csv  路線ごとの遅延状況（全列車）と雨量・風速（気象庁アメダス）
  python congestion.py report   … 1時間ごと
      週間・月間の統計、天気×遅延の統計、混雑予測を作り _site/ に出力

データの出どころ
  JR西日本 列車走行位置 /api/v3/{路線}.json, {路線}_st.json, trainmonitorinfo.json
  気象庁 アメダス /bosai/amedas/..., 天気予報 /bosai/forecast/...
"""
import csv
import datetime as dt
import bisect
import functools
import glob
import gzip
import json
import os
import shutil
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
WEST = "https://www.train-guide.westjr.co.jp/api/v3/"
JMA = "https://www.jma.go.jp/bosai/"
LINES = {"hokurikubiwako": "琵琶湖線", "kyoto": "JR京都線", "nara": "奈良線", "sagano": "嵯峨野線"}

# 乗車率(%)→7段階の暫定基準（公式アイコンとずれていればここを直す。各ページもこの値を使います）
T = [40, 60, 80, 100, 130, 160]
LEVELS = (4, 5, 6, 7)

# 路線ごとに使うアメダス地点（都道府県ブロック番号, 地点名）。複数地点の最大値を路線の値にします
WX_POINTS = {
    "hokurikubiwako": [("60", "彦根"), ("60", "大津")],
    "kyoto": [("61", "京都"), ("62", "大阪")],
    "nara": [("61", "京都"), ("61", "京田辺")],
    "sagano": [("61", "京都"), ("61", "亀岡"), ("61", "園部")],
}
# 降水確率に使う予報区（府県予報区コード, 一次細分区域コード）
FORECAST_AREAS = {"hokurikubiwako": ("250000", "250010"), "kyoto": ("260000", "260010"),
                  "nara": ("260000", "260010"), "sagano": ("260000", "260010")}

# 路線ごとの駅（駅ナンバリング, 駅名）。統計・在線・時刻表の路線の振り分けに使う
LINE_STATIONS = {
    "kyoto": [("JR-A31", "京都"), ("JR-A32", "西大路"), ("JR-A33", "桂川"), ("JR-A34", "向日町"), ("JR-A35", "長岡京"),
              ("JR-A36", "山崎"), ("JR-A37", "島本"), ("JR-A38", "高槻"), ("JR-A39", "摂津富田"), ("JR-A40", "JR総持寺"),
              ("JR-A41", "茨木"), ("JR-A42", "千里丘"), ("JR-A43", "岸辺"), ("JR-A44", "吹田"), ("JR-A45", "東淀川"),
              ("JR-A46", "新大阪"), ("JR-A47", "大阪")],
    "hokurikubiwako": [("JR-A09", "長浜"), ("JR-A10", "田村"), ("JR-A11", "坂田"), ("JR-A12", "米原"), ("JR-A13", "彦根"),
                       ("JR-A14", "南彦根"), ("JR-A15", "河瀬"), ("JR-A16", "稲枝"), ("JR-A17", "能登川"), ("JR-A18", "安土"),
                       ("JR-A19", "近江八幡"), ("JR-A20", "篠原"), ("JR-A21", "野洲"), ("JR-A22", "守山"), ("JR-A23", "栗東"),
                       ("JR-A24", "草津"), ("JR-A25", "南草津"), ("JR-A26", "瀬田"), ("JR-A27", "石山"), ("JR-A28", "膳所"),
                       ("JR-A29", "大津"), ("JR-A30", "山科"), ("JR-A31", "京都")],
    "sagano": [("JR-E01", "京都"), ("JR-E02", "梅小路京都西"), ("JR-E03", "丹波口"), ("JR-E04", "二条"), ("JR-E05", "円町"),
               ("JR-E06", "花園"), ("JR-E07", "太秦"), ("JR-E08", "嵯峨嵐山"), ("JR-E09", "保津峡"), ("JR-E10", "馬堀"),
               ("JR-E11", "亀岡"), ("JR-E12", "並河"), ("JR-E13", "千代川"), ("JR-E14", "八木"), ("JR-E15", "吉富"),
               ("JR-E16", "園部")],
    "nara": [("JR-D01", "京都"), ("JR-D02", "東福寺"), ("JR-D03", "稲荷"), ("JR-D04", "JR藤森"), ("JR-D05", "桃山"),
             ("JR-D06", "六地蔵"), ("JR-D07", "木幡"), ("JR-D08", "黄檗"), ("JR-D09", "宇治"), ("JR-D10", "JR小倉"),
             ("JR-D11", "新田"), ("JR-D12", "城陽"), ("JR-D13", "長池"), ("JR-D14", "山城青谷"), ("JR-D15", "山城多賀"),
             ("JR-D16", "玉水"), ("JR-D17", "棚倉"), ("JR-D18", "上狛"), ("JR-D19", "木津"), ("JR-D21", "平城山"),
             ("JR-D22", "奈良")],
}
_LSET = {k: {n for _, n in v} for k, v in LINE_STATIONS.items()}
# 運行情報（遅延の原因）の路線キー候補
TRAFFIC_KEYS = {"hokurikubiwako": ["biwako", "hokurikubiwako", "hokuriku"], "kyoto": ["kyoto"],
                "nara": ["nara"], "sagano": ["sagano", "sanin1", "sanin"]}
DAILY_KEEP = 35          # 日別の統計を残す日数


def norm(name):
    return str(name).replace("ＪＲ", "JR").strip()


def sec_names(sec):
    sec = norm(sec)
    return [sec[:-1]] if sec.endswith("駅") else sec.split("〜")


def line_fix(line, sec):
    """区間の駅がすべて含まれる路線に振り分け直す（琵琶湖線のデータに京都線の区間が混じるのを防ぐ）"""
    names = sec_names(sec)
    if line in _LSET and all(n in _LSET[line] for n in names):
        return line
    for k, st in _LSET.items():
        if all(n in st for n in names):
            return k
    return line


RAW, COND, STATS, SITE = "data/raw", "data/cond", "data/stats", "_site"
DB_RAW = os.environ.get("DB_RAW", "/tmp/db/raw")   # live.py が30秒ごとに記録したデータ（liveブランチから取り出す）
RAW_KEEP_DAYS = 75       # 生データの保存日数
PREDICT_DAYS = 56        # 予測に使う直近の日数
TIMELINE_DAYS = 14       # 横並びタイムラインに出す日数
RAIN_BINS = [0.5, 1, 3, 5, 10, 20, 30]   # 1時間雨量(mm)の区切り
WIND_BINS = [3, 5, 8, 10, 13, 15, 20]    # 風速(m/s)の区切り
MAX_BIN = 25
STATS_VERSION = 5


def is_exp(typ):
    return 1 if "特急" in str(typ) else 0


def lv_of(p):
    return 1 + sum(p >= t for t in T)


def bin_of(v, edges):
    return sum(v >= e for e in edges)


def fetch(url, text=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal train logger)",
                                               "Referer": "https://www.train-guide.westjr.co.jp/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        b = r.read().decode("utf-8")
    return b if text else json.loads(b)


def section_name(pos, m):
    a, _, b = str(pos or "").partition("_")
    if a in m and b in m:
        return f"{m[a]}〜{m[b]}"
    if a in m:
        return f"{m[a]}駅"
    return f"不明({pos})"


def day_type(d):
    try:
        import jpholiday
        if jpholiday.is_holiday(d):
            return 1
    except ImportError:
        pass
    return 1 if d.weekday() >= 5 else 0      # 0=平日 1=土休日


def set_output(k, v):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a") as f:
            f.write(f"{k}={v}\n")


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ================= 天気（気象庁） =================
def amedas_table():
    p = "data/amedastable.json"
    if os.path.exists(p) and dt.datetime.now().timestamp() - os.path.getmtime(p) < 7 * 86400:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    t = fetch(JMA + "amedas/const/amedastable.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f, ensure_ascii=False)
    return t


def fetch_weather():
    """路線ごとに [(地点名, 1時間雨量, 10分雨量, 風速, 気温), ...] を返す"""
    try:
        table = amedas_table()
        ts = dt.datetime.fromisoformat(fetch(JMA + "amedas/data/latest_time.txt", text=True).strip())
        m = fetch(JMA + f"amedas/data/map/{ts:%Y%m%d%H%M}00.json")
    except Exception as e:
        print("天気の取得に失敗:", e)
        return "", {}

    def code_of(pref, name):
        hit = [c for c, v in table.items() if v.get("kjName") == name]
        pref_hit = [c for c in hit if c.startswith(pref)]
        return pref_hit[0] if pref_hit else (hit[0] if len(hit) == 1 else None)

    def val(e, k):
        x = e.get(k)
        return x[0] if isinstance(x, list) and x and x[0] is not None else None

    out = {}
    for line, pts in WX_POINTS.items():
        out[line] = []
        for pref, name in pts:
            c = code_of(pref, name)
            if c and c in m:
                e = m[c]
                out[line].append((name, val(e, "precipitation1h"), val(e, "precipitation10m"),
                                  val(e, "wind"), val(e, "temp")))
    return ts.strftime("%H:%M"), out


def forecast_pops():
    out, cache = {}, {}
    for line, (office, area) in FORECAST_AREAS.items():
        try:
            if office not in cache:
                cache[office] = fetch(JMA + f"forecast/data/forecast/{office}.json")
            for ts in cache[office][0]["timeSeries"]:
                for a in ts["areas"]:
                    if "pops" in a and a["area"]["code"] == area:
                        out[line] = {"area": a["area"]["name"], "t": ts["timeDefines"],
                                     "p": [int(x) if str(x).isdigit() else None for x in a["pops"]]}
        except Exception as e:
            print("予報の取得に失敗:", line, e)
    return out


# ================= 収集 =================
def live_fresh(now):
    """live.py（30秒ごとの記録）が直近10分以内に動いていれば True"""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return False
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/latest.json?ref=live",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            t = json.loads(r.read().decode("utf-8"))["t"]
        return (now - dt.datetime.fromisoformat(t).replace(tzinfo=JST)).total_seconds() < 600
    except Exception:
        return False

def append_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def collect(now):
    hhmm = now.strftime("%H:%M")
    monitor = fetch(WEST + "trainmonitorinfo.json").get("trains", {})
    wx_time, wx = fetch_weather()
    try:
        traffic = fetch(WEST + "area_kinki_trafficinfo.json").get("lines", {}) or {}
    except Exception as e:
        traffic = {}
        print("運行情報の取得に失敗:", e)
    rows, conds, seen, errors = [], [], set(), []
    latest = {"t": now.strftime("%Y-%m-%d %H:%M"), "lines": {}, "monitor": {}, "errors": errors}
    for line in LINES:
        try:
            trains = fetch(WEST + f"{line}.json").get("trains", [])
            m = {str(s["info"]["code"]): s["info"]["name"]
                 for s in fetch(WEST + f"{line}_st.json").get("stations", []) if s.get("info")}
        except Exception as e:
            errors.append(f"{LINES[line]}: {e}")
            continue
        latest["lines"][line] = {"st": m, "trains": [
            {**{k: t.get(k) for k in ("no", "pos", "direction", "displayType", "nickname", "delayMinutes",
                                      "numberOfCars", "typeChange")},
             "dest": (t.get("dest") or {}).get("text", "") if isinstance(t.get("dest"), dict) else str(t.get("dest") or "")}
            for t in trains]}
        for t in trains:
            u = monitor.get(str(t.get("no", "")))
            if u:
                latest["monitor"][str(t.get("no"))] = [[c.get("carNo"), c.get("congestion"), c.get("temp")]
                                                       for x in u for c in (x.get("cars") or [])]
        delays = [int(t.get("delayMinutes") or 0) for t in trains]
        w = wx.get(line) or []

        def mx(i):
            v = [x[i] for x in w if x[i] is not None]
            return max(v) if v else ""
        conds.append([hhmm, line, len(trains), sum(d >= 1 for d in delays), sum(d >= 5 for d in delays),
                      sum(d >= 10 for d in delays), max(delays, default=0), sum(delays),
                      mx(1), mx(2), mx(3), mx(4), wx_time, "/".join(x[0] for x in w),
                      *next(((i.get("status", ""), i.get("cause", "")) for k in TRAFFIC_KEYS.get(line, [])
                             for i in [traffic.get(k)] if i), ("", ""))])
        for t in trains:
            no = str(t.get("no", ""))
            cars = [c for u in (monitor.get(no) or []) for c in (u.get("cars") or [])]
            if not cars or (no, t.get("pos")) in seen:
                continue
            seen.add((no, t.get("pos")))
            cars.sort(key=lambda c: c.get("carNo", 0))
            dest = t.get("dest")
            rows.append([hhmm, line, no, t.get("displayType", ""),
                         dest.get("text", "") if isinstance(dest, dict) else str(dest or ""),
                         t.get("direction", ""), section_name(t.get("pos"), m), t.get("delayMinutes", ""),
                         ";".join(f"{c.get('carNo')}:{c.get('congestion')}:{c.get('status')}:{c.get('temp')}:"
                                  + ".".join(str(x) for x in (c.get('types') or [])) for c in cars)])
    if len(errors) == len(LINES):
        print("すべての路線で取得に失敗:\n" + "\n".join(errors), file=sys.stderr)
        sys.exit(1)
    if live_fresh(now):
        print("30秒ごとの記録が動いているため、5分ごとの列車記録は省略")
    else:
        append_csv(f"{RAW}/{now:%Y-%m-%d}.csv",
                   ["時刻", "路線", "列車番号", "種別", "行先", "方向", "位置", "遅延分", "号車:乗車率:状態:温度"], rows)
    append_csv(f"{COND}/{now:%Y-%m-%d}.csv",
               ["時刻", "路線", "在線", "遅延1分以上", "遅延5分以上", "遅延10分以上", "最大遅延", "遅延合計",
                "1時間雨量", "10分雨量", "風速", "気温", "観測時刻", "観測地点", "運行情報", "原因"], conds)
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, separators=(",", ":"))
    if monitor:
        k = next(iter(monitor))
        with open("data/sample_monitor.json", "w", encoding="utf-8") as f:
            json.dump({k: monitor[k]}, f, ensure_ascii=False, indent=1)
    with open("data/status.json", "w", encoding="utf-8") as f:
        json.dump({"last": now.strftime("%Y-%m-%d %H:%M"), "trainsWithData": len(rows),
                   "monitorTrains": len(monitor), "errors": errors, "weatherTime": wx_time,
                   "weatherPoints": {LINES[k]: [x[0] for x in v] for k, v in wx.items()},
                   "trafficNow": {k: {"status": v.get("status"), "cause": v.get("cause")} for k, v in traffic.items()}},
                  f, ensure_ascii=False)
    print(f"記録 {len(rows)}本／天気 {wx_time or '取得失敗'}")


# ================= 読み込み =================
@functools.lru_cache(maxsize=None)
def load_day(ds):
    raw, cond = [], {}
    for p in [f"{RAW}/{ds}.csv", f"{DB_RAW}/{ds}.csv.gz"] + sorted(glob.glob(f"{DB_RAW}/{ds}/*.csv.gz")):
        if not os.path.exists(p):
            continue
        with (gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")) as f:
                r = csv.reader(f)
                next(r, None)
                for row in r:
                    if len(row) < 9:
                        continue
                    tm, line, no, typ, dest, dr, sec, delay, cars = row[:9]
                    cl = []
                    for c in cars.split(";"):
                        x = c.split(":")
                        try:
                            car, pct = int(x[0]), int(x[1])
                        except (ValueError, IndexError):
                            continue
                        if pct >= 0:
                            cl.append((car, pct))
                    raw.append((tm, int(tm[:2]) * 60 + int(tm[3:5]), line_fix(line, sec), no, typ, dest,
                                int(dr) if dr.isdigit() else 0, sec, int(num(delay) or 0), tuple(cl)))
    raw.sort(key=lambda r: r[0])                 # 5分ごと・30秒ごとの記録を時刻順に並べる
    p = f"{COND}/{ds}.csv"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if len(row) < 12:
                    continue
                cond[(row[0], row[1])] = {"n": int(row[2]), "d1": int(row[3]), "d5": int(row[4]),
                                          "d10": int(row[5]), "dmax": int(row[6]),
                                          "rain": num(row[8]), "wind": num(row[10]),
                                          "info": row[14] if len(row) > 14 else "",
                                          "cause": row[15] if len(row) > 15 else ""}
    return raw, cond


@functools.lru_cache(maxsize=None)
def cond_index(ds):
    idx = defaultdict(lambda: ([], []))
    for (tm, line), c in sorted(load_day(ds)[1].items()):
        m = int(tm[:2]) * 60 + int(tm[3:5])
        idx[line][0].append(m)
        idx[line][1].append(c)
    return idx


def cond_at(ds, line, mi):
    """その時刻の直前（10分以内）の天気・遅延の記録"""
    ms, cs = cond_index(ds).get(line, ([], []))
    i = bisect.bisect_right(ms, mi) - 1
    return cs[i] if i >= 0 and mi - ms[i] <= 10 else None


def passages(raw):
    """同じ列車が同じ区間にいる間は1回だけ返す"""
    last = {}
    for r in raw:
        if last.get(r[3]) == r[7]:
            continue
        last[r[3]] = r[7]
        yield r


def all_dates():
    ds = {os.path.basename(p)[:10] for p in glob.glob(f"{RAW}/*.csv") + glob.glob(f"{COND}/*.csv")
          + glob.glob(f"{DB_RAW}/*.csv.gz")}
    ds |= {os.path.basename(os.path.dirname(p)) for p in glob.glob(f"{DB_RAW}/*/*.csv.gz")}
    return sorted(ds)


# ================= 週間・月間の統計 =================
def periods_of(d):
    y, w, _ = d.isocalendar()
    return f"W{y}-{w:02d}", f"M{d:%Y-%m}", f"D{d.isoformat()}"


def period_range(pid):
    if pid[0] == "D":
        d = dt.date.fromisoformat(pid[1:])
        return d, d
    if pid[0] == "W":
        y, w = map(int, pid[1:].split("-"))
        s = dt.date.fromisocalendar(y, w, 1)
        return s, s + dt.timedelta(days=6)
    y, m = map(int, pid[1:].split("-"))
    s = dt.date(y, m, 1)
    return s, (s.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)


def hist(cnt):
    g = defaultdict(list)
    for k, v in sorted(cnt.items()):
        g[k[:-1]] += [k[-1], v]
    return [[*k, v] for k, v in g.items()]


def aggregate(pid, days):
    L = list(LINES)
    sec, trn, tsec = Counter(), Counter(), Counter()
    secmin = defaultdict(list)
    tmeta, S, idx = {}, [], {}
    runs = set()
    eps = defaultdict(list)
    train_days = defaultdict(set)
    zero = lambda: [0] * 14  # [11]運行情報あり [12]原因に「雨」 [13]原因に「風」 ／ 記録回数, 遅延発生回数, 最大遅延合計, 号車観測数, 多く以上, 大変多く以上, とても混雑, (うち特急)観測数, 多く, 大変多く, とても
    cst = {k: {"rain": [zero() for _ in range(len(RAIN_BINS) + 1)],
               "wind": [zero() for _ in range(len(WIND_BINS) + 1)],
               "delay": [[0] * 8, [0] * 8]} for k in L}

    def si(s):
        if s not in idx:
            idx[s] = len(S)
            S.append(s)
        return idx[s]

    # 停車駅の推定：同じ列車がその駅で2日以上観測されたら停車駅とみなす
    stop_days = defaultdict(set)
    for ds in days:
        for r in load_day(ds)[0]:
            if r[7].endswith("駅"):
                stop_days[(r[3], r[7])].add(ds)
    is_stop = lambda no, secn: secn.endswith("駅") and len(stop_days.get((no, secn), ())) >= 2

    for ds in days:
        d = dt.date.fromisoformat(ds)
        dty = day_type(d)
        raw, cond = load_day(ds)
        last_stop = {}
        for (tm, line), c in cond.items():
            runs.add(ds + tm)
            if line not in cst:
                continue
            for key, v, edges in (("rain", c["rain"], RAIN_BINS), ("wind", c["wind"], WIND_BINS)):
                if v is None:
                    continue
                b = cst[line][key][bin_of(v, edges)]
                b[0] += 1
                b[1] += c["d5"] > 0
                b[2] += c["dmax"]
                b[11] += bool(c.get("info") or c.get("cause"))
                b[12] += "雨" in c.get("cause", "")
                b[13] += "風" in c.get("cause", "")
        for r in passages(raw):
            tm, mi, line, no, typ, dest, dr, secn, delay, cars = r
            runs.add(ds + tm)
            hr, s, li, tg = mi // 60, si(secn), L.index(line) if line in L else 0, is_exp(typ)
            if is_stop(no, secn):
                last_stop[no] = secn
            org = si(last_stop[no]) if no in last_stop else -1     # 混雑の起点（直前の停車駅）
            meta = tmeta.setdefault(no, {"l": line, "t": typ, "d": dest, "r": dr, "h": Counter()})
            meta["h"][hr] += 1
            c = cond_at(ds, line, mi)
            for car, pct in cars:
                b = min(pct // 10, MAX_BIN)
                lv = lv_of(pct)
                sec[(li, dr, s, car, hr, dty, tg, b)] += 1
                trn[(no, car, dty, b)] += 1
                if b >= 10:
                    tsec[(no, s, car, dty, b)] += 1
                if lv >= 4:
                    secmin[(li, dr, s, car, dty, tg)] += [b, mi, org]
                hits = [1, lv >= 5, lv >= 6, lv >= 7]
                if line in cst:
                    dd = cst[line]["delay"][1 if delay >= 5 else 0]
                    for i in range(4):
                        dd[i] += hits[i]
                        dd[4 + i] += hits[i] * tg
                    if c:
                        for key, v, edges in (("rain", c["rain"], RAIN_BINS), ("wind", c["wind"], WIND_BINS)):
                            if v is not None:
                                bb = cst[line][key][bin_of(v, edges)]
                                for i in range(4):
                                    bb[3 + i] += hits[i]
                                    bb[7 + i] += hits[i] * tg
        # 混雑が続いた時間と区間（エピソード）
        by_train = defaultdict(list)
        for r in raw:
            by_train[r[3]].append(r)
        for no, obs in by_train.items():
            obs.sort(key=lambda r: r[1])
            train_days[(no, dty)].add(ds)
            lst, ls = [], None
            for r in obs:
                if is_stop(no, r[7]):
                    ls = r[7]
                lst.append(ls)
            for car in sorted({c for r in obs for c, _ in r[9]}):
                seq = [(r[1], r[7], p, lst[k]) for k, r in enumerate(obs) for c, p in r[9] if c == car]
                for Lv in LEVELS:
                    cur = None
                    for mi, secn, pct, org in seq + [(None, None, -1, None)]:
                        hit = mi is not None and lv_of(pct) >= Lv
                        if cur and (not hit or mi - cur[1] > 12):
                            eps[(no, car, dty, Lv)].append((ds, *cur))
                            cur = None
                        if hit:
                            if cur is None:
                                cur = [mi, mi, secn, secn, pct, secn, org]
                            else:
                                cur[1], cur[3] = mi, secn
                                if pct > cur[4]:
                                    cur[4], cur[5] = pct, secn
    pats = []
    for (no, car, dty, Lv), lst in eps.items():
        lst.sort(key=lambda e: e[1])
        clusters = []
        for e in lst:
            for cl in clusters:
                if abs(e[1] - cl[0][1]) <= 20 and e[0] not in {x[0] for x in cl}:
                    cl.append(e)
                    break
            else:
                clusters.append([e])
        total = len(train_days[(no, dty)])
        for cl in clusters:
            mode = lambda i: Counter(x[i] for x in cl).most_common(1)[0][0]
            orgs = [x[7] for x in cl if x[7]]
            pats.append([no, car, dty, Lv, len(cl), total,
                         int(statistics.median(x[1] for x in cl)), int(statistics.median(x[2] for x in cl)),
                         si(mode(3)), si(mode(4)), si(mode(6)), max(x[5] for x in cl),
                         round(sum(x[5] for x in cl) / len(cl)),
                         si(Counter(orgs).most_common(1)[0][0]) if orgs else -1])
    ps, pe = period_range(pid)
    return {
        "v": STATS_VERSION, "id": pid, "from": ps.isoformat(), "to": pe.isoformat(),
        "days": sorted(days), "runs": len(runs), "L": L, "S": S,
        "sec": hist(sec),        # [路線, 方向, 区間, 号車, 時, 平日0/土休日1, 特急1/他0, [bin,回数,...]]
        "trn": hist(trn),        # [列車番号, 号車, 平日0/土休日1, [bin,回数,...]]
        "tsec": hist(tsec),      # [列車番号, 区間, 号車, 平日0/土休日1, [bin,回数,...]]
        "secMin": [[*k, v] for k, v in secmin.items()],   # [路線,方向,区間,号車,平日/土休日,特急,[bin,分,起点駅,...]]
        "pat": pats,  # [列車,号車,平日/土休日,レベル,混んだ日数,走った日数,開始分,終了分,開始区間,終了区間,最混雑区間,最大%,平均%,起点駅]
        "tmeta": {k: [v["l"], v["t"], v["d"], v["r"], v["h"].most_common(1)[0][0]] for k, v in tmeta.items()},
        "cond": cst,
        "rainBins": RAIN_BINS, "windBins": WIND_BINS,
    }


# ================= 横並びタイムライン =================
def timeline(dates):
    out = {"days": dates, "series": {k: {} for k in LINES}}
    for ds in dates:
        raw, cond = load_day(ds)
        g = defaultdict(lambda: [None, None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])   # 雨,風,最大遅延,遅延本数,号車観測,多く,大変多く,とても,(特急分)×4
        for (tm, line), c in cond.items():
            if line not in LINES:
                continue
            x = g[(line, (int(tm[:2]) * 60 + int(tm[3:5])) // 30)]
            for i, k in ((0, "rain"), (1, "wind")):
                if c[k] is not None:
                    x[i] = max(x[i] or 0, c[k])
            x[2] = max(x[2], c["dmax"])
            x[3] = max(x[3], c["d5"])
        for r in passages(raw):
            x = g[(r[2], r[1] // 30)]
            tg = is_exp(r[4])
            for _, p in r[9]:
                lv = lv_of(p)
                for off, w in ((4, 1), (8, tg)):
                    x[off] += w
                    x[off + 1] += w * (lv >= 5)
                    x[off + 2] += w * (lv >= 6)
                    x[off + 3] += w * (lv >= 7)
        for (line, slot), x in sorted(g.items(), key=lambda kv: kv[0][1]):
            out["series"][line].setdefault(ds, []).append([slot, *x])
    return out


# ================= 混雑予測 =================
def predict(dates, now):
    S, idx = [], {}

    def si(s):
        if s not in idx:
            idx[s] = len(S)
            S.append(s)
        return idx[s]

    rows = defaultdict(lambda: [0, 0, 0, 0, [], 0, 0, 0, 0])   # n, h5,h6,h7, 分, 雨n, 雨h5,h6,h7
    prior = defaultdict(lambda: [0, 0, 0, 0])                    # (路線,平日/土休日,時) → 観測,h5,h6,h7
    fac = defaultdict(lambda: [0, 0, 0, 0])                      # (路線,種類,状態) → 観測,h5,h6,h7
    tmeta = {}
    for ds in dates:
        dty = day_type(dt.date.fromisoformat(ds))
        raw, cond = load_day(ds)
        for tm, mi, line, no, typ, dest, dr, secn, delay, cars in passages(raw):
            tmeta.setdefault(no, [line, typ, dest, dr])
            c = cond_at(ds, line, mi)
            rainy = bool(c and c["rain"] is not None and c["rain"] >= 1.0)
            for car, pct in cars:
                lv = lv_of(pct)
                h = (lv >= 5, lv >= 6, lv >= 7)
                x = rows[(no, dty, si(secn), car)]
                x[0] += 1
                x[4].append(mi)
                for i in range(3):
                    x[1 + i] += h[i]
                if rainy:
                    x[5] += 1
                    for i in range(3):
                        x[6 + i] += h[i]
                for key in ((line, dty, mi // 60),):
                    p = prior[key]
                    p[0] += 1
                    for i in range(3):
                        p[1 + i] += h[i]
                for key in ((line, "rain", int(rainy)), (line, "delay", int(delay >= 5))):
                    p = fac[key]
                    p[0] += 1
                    for i in range(3):
                        p[1 + i] += h[i]

    def factor(line, kind):
        a, b = fac.get((line, kind, 1)), fac.get((line, kind, 0))
        out = {}
        for i, Lv in enumerate((5, 6, 7)):
            if a and b and a[0] >= 200 and b[0] >= 200 and b[1 + i] > 0 and a[1 + i] > 0:
                out[Lv] = round(min(3.0, max(0.5, (a[1 + i] / a[0]) / (b[1 + i] / b[0]))), 2)
            else:
                out[Lv] = 1.0
        return out

    pr = defaultdict(dict)
    for (line, dty, hr), p in prior.items():
        pr[line].setdefault(dty, {})[hr] = p
    today = now.date()
    return {
        "built": now.strftime("%Y-%m-%d %H:%M"), "days": len(dates), "from": dates[0] if dates else "",
        "S": S, "tmeta": tmeta,
        # [列車,平日/土休日,区間,号車,日数,多く以上の日数,大変多く以上,とても混雑,典型時刻(分),雨の日数,雨h5,h6,h7]
        "rows": [[no, dty, s, car, x[0], x[1], x[2], x[3], int(statistics.median(x[4])), x[5], x[6], x[7], x[8]]
                 for (no, dty, s, car), x in rows.items() if x[1] > 0],
        "prior": pr,
        "rainF": {k: factor(k, "rain") for k in LINES},
        "delayF": {k: factor(k, "delay") for k in LINES},
        "pops": forecast_pops(),
        "dates": [[(today + dt.timedelta(days=i)).isoformat(), day_type(today + dt.timedelta(days=i))]
                  for i in range(7)],
    }


# ================= 巡回ルート用の観測ダイヤ =================
def patrol(dates):
    """列車ごとに、駅の時刻（複数日の中央値）と区間ごとの混雑率を作る"""
    L = list(LINES)
    S, idx = [], {}

    def si(s):
        if s not in idx:
            idx[s] = len(S)
            S.append(s)
        return idx[s]

    data = {}
    for ds in dates:
        dty = day_type(dt.date.fromisoformat(ds))
        raw, _ = load_day(ds)
        for tm, mi, line, no, typ, dest, dr, secn, delay, cars in passages(raw):
            tr = data.setdefault((dty, no), {"days": set(), "meta": (typ, dest, dr), "sec": {}})
            tr["days"].add(ds)
            x = tr["sec"].setdefault(secn, {"mins": [], "days": set(), "h6": Counter(), "h7": Counter(), "line": Counter()})
            if ds in x["days"]:
                continue
            x["days"].add(ds)
            x["mins"].append(mi)
            x["line"][line] += 1
            for car, p in cars:
                lv = lv_of(p)
                if lv >= 6:
                    x["h6"][car] += 1
                if lv >= 7:
                    x["h7"][car] += 1
    trains = [[], []]
    for (dty, no), tr in data.items():
        if len(tr["days"]) < 2:
            continue                               # 2日以上走っている定期列車だけ
        stops, crowd = [], []
        for secn, x in tr["sec"].items():
            n = len(x["days"])
            med = int(statistics.median(x["mins"]))
            li = L.index(x["line"].most_common(1)[0][0]) if x["line"] else 0
            c6, c7 = x["h6"].most_common(1), x["h7"].most_common(1)
            crowd.append([med, round(c6[0][1] / n, 2) if c6 else 0, round(c7[0][1] / n, 2) if c7 else 0,
                          c6[0][0] if c6 else 0, c7[0][0] if c7 else 0, n, li])
            if secn.endswith("駅") and n >= 2:     # 2日以上その駅で観測＝停車している可能性が高い
                stops.append([si(secn[:-1]), med, n, li])
        if len(stops) < 2:
            continue
        stops.sort(key=lambda s: s[1])
        crowd.sort()
        trains[dty].append([no, *tr["meta"], len(tr["days"]), stops, crowd])
    return {"S": S, "L": L, "days": len(dates), "from": dates[0] if dates else "", "trains": trains}


# ================= 時刻表（実測）・走行記録 =================
def merged_patterns(month_file):
    """月間統計の混雑パターンを、列車・平日/土休日・レベルごとに号車をまとめた形にする"""
    out = {}
    if not os.path.exists(month_file):
        return out
    with open(month_file, encoding="utf-8") as f:
        M = json.load(f)
    S = M.get("S", [])
    for p in M.get("pat", []):
        no, car, dty, Lv, dh, dtot, st, en, ss, es, ps, mx, avg = p[:13]
        key = (no, dty, Lv)
        cur = out.get(key)
        if cur is None or dh > cur["days"]:
            out[key] = cur = {"days": dh, "total": dtot, "s": st, "e": en, "from": S[ss], "to": S[es],
                              "peak": S[ps], "cars": {}}
        cur["cars"][car] = max(cur["cars"].get(car, 0), mx)
        cur["s"], cur["e"] = min(cur["s"], st), max(cur["e"], en)
    res = defaultdict(dict)
    for (no, dty, Lv), v in out.items():
        res[no].setdefault(str(dty), {})[str(Lv)] = [v["s"], v["e"], v["from"], v["to"], v["peak"],
                                                      sorted(v["cars"].items()), v["days"], v["total"]]
    return res


def timetable(dates, pats):
    """各駅・方向ごとに、列車の到着時刻（複数日の中央値）と、発車直後の混雑をまとめる"""
    acc = {}
    train_days = defaultdict(set)
    for ds in dates:
        dty = day_type(dt.date.fromisoformat(ds))
        raw, _ = load_day(ds)
        by_train = defaultdict(list)
        for r in raw:
            by_train[r[3]].append(r)
        for no, obs in by_train.items():
            obs.sort(key=lambda r: r[0])
            train_days[(dty, no)].add(ds)
            done = set()
            for i, r in enumerate(obs):
                if not r[7].endswith("駅"):
                    continue
                stn = norm(r[7][:-1])
                if stn in done:
                    continue
                done.add(stn)
                nxt = next((x for x in obs[i + 1:] if x[7] != r[7]), None)   # 発車直後の区間
                use = nxt if nxt and nxt[1] - r[1] <= 6 and nxt[9] else r
                k = (dty, r[2], stn, r[6], no)
                a = acc.setdefault(k, {"m": [], "days": set(), "hit": Counter(), "cars": defaultdict(list),
                                       "typ": r[4], "dest": r[5]})
                a["m"].append(r[1])
                a["days"].add(ds)
                if use[9]:
                    mx = max(p for _, p in use[9])
                    for Lv in LEVELS:
                        a["hit"][Lv] += lv_of(mx) >= Lv
                    for car, p in use[9]:
                        a["cars"][car].append(p)
    out = {k: {"stations": [[i, n] for i, n in v], "tt": {"0": {}, "1": {}}, "pat": {}} for k, v in LINE_STATIONS.items()}
    for (dty, line, stn, dr, no), a in acc.items():
        if line not in out or stn not in _LSET[line]:
            continue
        n = len(a["days"])
        if n < 2:                       # 2日以上その駅で観測された列車だけ（通過の可能性を減らす）
            continue
        row = [int(statistics.median(a["m"])), no, a["typ"], a["dest"], n, len(train_days[(dty, no)]),
               *[round(a["hit"][Lv] / n, 2) for Lv in LEVELS],
               [[c, int(statistics.median(v))] for c, v in sorted(a["cars"].items())]]
        out[line]["tt"][str(dty)].setdefault(stn, {}).setdefault(str(dr), []).append(row)
        if no in pats:
            out[line]["pat"][no] = pats[no]
    for line in out.values():
        for d in line["tt"].values():
            for st in d.values():
                for rows in st.values():
                    rows.sort()
    return out


def trainlogs(dates, keep=14):
    """列車ごと・日ごとの走行記録。同じ区間の記録は1行にまとめ、号車ごとの最大乗車率と最大の遅れを残す"""
    logs = {}
    for ds in reversed(dates[-keep:]):
        raw, _ = load_day(ds)
        by = defaultdict(list)
        for r in raw:
            by[r[3]].append(r)
        for no, obs in by.items():
            if not any(r[9] for r in obs):
                continue                                  # 混雑データのない列車は対象外
            obs.sort(key=lambda r: r[0])
            rows = []
            for r in obs:
                sec = norm(r[7])
                if rows and rows[-1][1] == sec:
                    row = rows[-1]
                else:
                    row = [r[0][:5], sec, 0, {}]
                    rows.append(row)
                row[2] = max(row[2], r[8])
                for c, p in r[9]:
                    row[3][c] = max(row[3].get(c, -1), p)
            lg = logs.setdefault(no, {"typ": obs[0][4], "dest": obs[0][5], "days": {}})
            lg["days"][ds] = {"typ": obs[0][4], "dest": obs[0][5],
                              "rows": [[t, sec, dl, sorted(cs.items())] for t, sec, dl, cs in rows]}
    return logs


# ================= 出力 =================
def ensure_db():
    """30秒ごとの記録（liveブランチ）がまだ取り出されていなければ、ここで取り出す"""
    if glob.glob(f"{DB_RAW}/*/*.csv.gz") or glob.glob(f"{DB_RAW}/*.csv.gz"):
        return
    import subprocess
    root = os.path.dirname(os.path.dirname(DB_RAW.rstrip("/"))) or "/tmp/db"
    try:
        subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", "live"], check=True, capture_output=True, timeout=300)
        os.makedirs(os.path.dirname(DB_RAW.rstrip("/")), exist_ok=True)
        arc = subprocess.run(["git", "archive", "FETCH_HEAD", "raw"], check=True, capture_output=True, timeout=300)
        subprocess.run(["tar", "-x", "-C", os.path.dirname(DB_RAW.rstrip("/"))], input=arc.stdout, check=True, timeout=300)
        print("30秒ごとの記録を取り出しました:", len(glob.glob(f"{DB_RAW}/*/*.csv.gz")), "ファイル")
    except Exception as e:
        print("30秒ごとの記録を取り出せませんでした:", e)


def report(now):
    ensure_db()
    today = now.date()
    dates = all_dates()
    by_period = defaultdict(list)
    for ds in dates:
        for pid in periods_of(dt.date.fromisoformat(ds)):
            by_period[pid].append(ds)
    os.makedirs(STATS, exist_ok=True)
    for pid, days in by_period.items():
        out = f"{STATS}/{pid}.json"
        if pid[0] == "D" and (today - period_range(pid)[0]).days > DAILY_KEEP:
            if os.path.exists(out):
                os.remove(out)
            continue
        if period_range(pid)[1] < today and os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                if json.load(f).get("v") == STATS_VERSION:
                    continue                  # 確定済みの期間は作り直さない
        with open(out, "w", encoding="utf-8") as f:
            json.dump(aggregate(pid, days), f, ensure_ascii=False, separators=(",", ":"))
    cutoff = today - dt.timedelta(days=RAW_KEEP_DAYS)
    for p in glob.glob(f"{RAW}/*.csv") + glob.glob(f"{COND}/*.csv"):
        d = dt.date.fromisoformat(os.path.basename(p)[:10])
        if d < cutoff and all(os.path.exists(f"{STATS}/{x}.json") for x in periods_of(d)[:2]):
            os.remove(p)
    dates = all_dates()
    shutil.rmtree(SITE, ignore_errors=True)
    shutil.copytree(STATS, f"{SITE}/stats")
    with open(f"{SITE}/stats/timeline.json", "w", encoding="utf-8") as f:
        json.dump(timeline(dates[-TIMELINE_DAYS:]), f, ensure_ascii=False, separators=(",", ":"))
    with open(f"{SITE}/stats/predict.json", "w", encoding="utf-8") as f:
        json.dump(predict(dates[-PREDICT_DAYS:], now), f, ensure_ascii=False, separators=(",", ":"))
    month = f"{STATS}/M{today:%Y-%m}.json"
    tt = timetable(dates[-28:], merged_patterns(month))
    for line, v in tt.items():
        with open(f"{SITE}/stats/timetable_{line}.json", "w", encoding="utf-8") as f:
            json.dump(v, f, ensure_ascii=False, separators=(",", ":"))
    os.makedirs(f"{SITE}/stats/trainlog", exist_ok=True)
    for no, v in trainlogs(dates).items():
        with open(f"{SITE}/stats/trainlog/{no}.json", "w", encoding="utf-8") as f:
            json.dump(v, f, ensure_ascii=False, separators=(",", ":"))
    with open(f"{SITE}/stats/patrol.json", "w", encoding="utf-8") as f:
        json.dump(patrol(dates[-PREDICT_DAYS:]), f, ensure_ascii=False, separators=(",", ":"))
    ids = sorted((os.path.basename(p)[:-5] for p in glob.glob(f"{STATS}/*.json")), reverse=True)
    status = {}
    if os.path.exists("data/status.json"):
        with open("data/status.json", encoding="utf-8") as f:
            status = json.load(f)
    with open(f"{SITE}/stats/index.json", "w", encoding="utf-8") as f:
        json.dump({"periods": ids, "built": now.strftime("%Y-%m-%d %H:%M"), "status": status,
                   "lines": LINES, "T": T}, f, ensure_ascii=False)
    if os.path.exists("data/sample_monitor.json"):
        shutil.copy("data/sample_monitor.json", f"{SITE}/sample_monitor.json")
    here = os.path.dirname(os.path.abspath(__file__))
    for src, dst in (("stats.html", "index.html"), ("weather.html", "weather.html"), ("predict.html", "predict.html"),
                     ("monitor.html", "monitor.html"), ("patrol.html", "patrol.html"),
                     ("schools.json", "stats/schools.json"), ("station_notes.json", "stats/station_notes.json"),
                     ("incidents.json", "stats/incidents.json"), ("timetable.html", "timetable.html"),
                     ("trend.js", "trend.js")):
        if os.path.exists(os.path.join(here, src)):
            shutil.copy(os.path.join(here, src), f"{SITE}/{dst}")
    with open("data/last_publish.txt", "w") as f:
        f.write(now.strftime("%Y-%m-%d %H"))
    print("ページを作成:", ", ".join(ids) or "（データなし）")


def cleanup_old_version():
    for p in ("data/agg.json", "data/state.json"):
        if os.path.exists(p):
            os.remove(p)
    for d in ("data/detail", "docs"):
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    now = dt.datetime.now(JST)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    force = os.environ.get("FORCE") == "true"
    if cmd == "collect":
        cleanup_old_version()
        os.makedirs("data", exist_ok=True)
        if 2 <= now.hour < 4 and not force:
            print("深夜帯のため取得をスキップ")
        else:
            collect(now)
        last = ""
        if os.path.exists("data/last_publish.txt"):
            with open("data/last_publish.txt") as f:
                last = f.read().strip()
        set_output("publish", "true" if force or last != now.strftime("%Y-%m-%d %H") else "false")
    elif cmd == "report":
        report(now)
