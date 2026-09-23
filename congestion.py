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
import functools
import glob
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

RAW, COND, STATS, SITE = "data/raw", "data/cond", "data/stats", "_site"
RAW_KEEP_DAYS = 75       # 生データの保存日数
PREDICT_DAYS = 56        # 予測に使う直近の日数
TIMELINE_DAYS = 14       # 横並びタイムラインに出す日数
RAIN_BINS = [0.5, 1, 3, 5, 10, 20, 30]   # 1時間雨量(mm)の区切り
WIND_BINS = [3, 5, 8, 10, 13, 15, 20]    # 風速(m/s)の区切り
MAX_BIN = 25
STATS_VERSION = 2


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
    rows, conds, seen, errors = [], [], set(), []
    for line in LINES:
        try:
            trains = fetch(WEST + f"{line}.json").get("trains", [])
            m = {str(s["info"]["code"]): s["info"]["name"]
                 for s in fetch(WEST + f"{line}_st.json").get("stations", []) if s.get("info")}
        except Exception as e:
            errors.append(f"{LINES[line]}: {e}")
            continue
        delays = [int(t.get("delayMinutes") or 0) for t in trains]
        w = wx.get(line) or []

        def mx(i):
            v = [x[i] for x in w if x[i] is not None]
            return max(v) if v else ""
        conds.append([hhmm, line, len(trains), sum(d >= 1 for d in delays), sum(d >= 5 for d in delays),
                      sum(d >= 10 for d in delays), max(delays, default=0), sum(delays),
                      mx(1), mx(2), mx(3), mx(4), wx_time, "/".join(x[0] for x in w)])
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
                         ";".join(f"{c.get('carNo')}:{c.get('congestion')}:{c.get('status')}:{c.get('temp')}"
                                  for c in cars)])
    if len(errors) == len(LINES):
        print("すべての路線で取得に失敗:\n" + "\n".join(errors), file=sys.stderr)
        sys.exit(1)
    append_csv(f"{RAW}/{now:%Y-%m-%d}.csv",
               ["時刻", "路線", "列車番号", "種別", "行先", "方向", "位置", "遅延分", "号車:乗車率:状態:温度"], rows)
    append_csv(f"{COND}/{now:%Y-%m-%d}.csv",
               ["時刻", "路線", "在線", "遅延1分以上", "遅延5分以上", "遅延10分以上", "最大遅延", "遅延合計",
                "1時間雨量", "10分雨量", "風速", "気温", "観測時刻", "観測地点"], conds)
    if monitor:
        k = next(iter(monitor))
        with open("data/sample_monitor.json", "w", encoding="utf-8") as f:
            json.dump({k: monitor[k]}, f, ensure_ascii=False, indent=1)
    with open("data/status.json", "w", encoding="utf-8") as f:
        json.dump({"last": now.strftime("%Y-%m-%d %H:%M"), "trainsWithData": len(rows),
                   "monitorTrains": len(monitor), "errors": errors, "weatherTime": wx_time,
                   "weatherPoints": {LINES[k]: [x[0] for x in v] for k, v in wx.items()}},
                  f, ensure_ascii=False)
    print(f"記録 {len(rows)}本／天気 {wx_time or '取得失敗'}")


# ================= 読み込み =================
@functools.lru_cache(maxsize=None)
def load_day(ds):
    raw, cond = [], {}
    p = f"{RAW}/{ds}.csv"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
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
                if cl:
                    raw.append((tm, int(tm[:2]) * 60 + int(tm[3:5]), line, no, typ, dest,
                                int(dr) if dr.isdigit() else 0, sec, int(num(delay) or 0), tuple(cl)))
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
                                          "rain": num(row[8]), "wind": num(row[10])}
    return raw, cond


def passages(raw):
    """同じ列車が同じ区間にいる間は1回だけ返す"""
    last = {}
    for r in raw:
        if last.get(r[3]) == r[7]:
            continue
        last[r[3]] = r[7]
        yield r


def all_dates():
    ds = {os.path.basename(p)[:10] for p in glob.glob(f"{RAW}/*.csv") + glob.glob(f"{COND}/*.csv")}
    return sorted(ds)


# ================= 週間・月間の統計 =================
def periods_of(d):
    y, w, _ = d.isocalendar()
    return f"W{y}-{w:02d}", f"M{d:%Y-%m}"


def period_range(pid):
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
    zero = lambda: [0] * 7   # 記録回数, 遅延発生回数, 最大遅延合計, 号車観測数, 多く以上, 大変多く以上, とても混雑
    cst = {k: {"rain": [zero() for _ in range(len(RAIN_BINS) + 1)],
               "wind": [zero() for _ in range(len(WIND_BINS) + 1)],
               "delay": [[0, 0, 0, 0], [0, 0, 0, 0]]} for k in L}

    def si(s):
        if s not in idx:
            idx[s] = len(S)
            S.append(s)
        return idx[s]

    for ds in days:
        d = dt.date.fromisoformat(ds)
        dty = day_type(d)
        raw, cond = load_day(ds)
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
        for r in passages(raw):
            tm, mi, line, no, typ, dest, dr, secn, delay, cars = r
            runs.add(ds + tm)
            hr, s, li = mi // 60, si(secn), L.index(line) if line in L else 0
            meta = tmeta.setdefault(no, {"l": line, "t": typ, "d": dest, "r": dr, "h": Counter()})
            meta["h"][hr] += 1
            c = cond.get((tm, line))
            for car, pct in cars:
                b = min(pct // 10, MAX_BIN)
                lv = lv_of(pct)
                sec[(li, dr, s, car, hr, dty, b)] += 1
                trn[(no, car, dty, b)] += 1
                if b >= 10:
                    tsec[(no, s, car, dty, b)] += 1
                if lv >= 4:
                    secmin[(li, dr, s, car, dty)] += [b, mi]
                hits = [1, lv >= 5, lv >= 6, lv >= 7]
                if line in cst:
                    dd = cst[line]["delay"][1 if delay >= 5 else 0]
                    for i in range(4):
                        dd[i] += hits[i]
                    if c:
                        for key, v, edges in (("rain", c["rain"], RAIN_BINS), ("wind", c["wind"], WIND_BINS)):
                            if v is not None:
                                bb = cst[line][key][bin_of(v, edges)]
                                for i in range(4):
                                    bb[3 + i] += hits[i]
        # 混雑が続いた時間と区間（エピソード）
        by_train = defaultdict(list)
        for r in raw:
            by_train[r[3]].append(r)
        for no, obs in by_train.items():
            obs.sort(key=lambda r: r[1])
            train_days[(no, dty)].add(ds)
            for car in sorted({c for r in obs for c, _ in r[9]}):
                seq = [(r[1], r[7], p) for r in obs for c, p in r[9] if c == car]
                for Lv in LEVELS:
                    cur = None
                    for mi, secn, pct in seq + [(None, None, -1)]:
                        hit = mi is not None and lv_of(pct) >= Lv
                        if cur and (not hit or mi - cur[1] > 12):
                            eps[(no, car, dty, Lv)].append((ds, *cur))
                            cur = None
                        if hit:
                            if cur is None:
                                cur = [mi, mi, secn, secn, pct, secn]
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
            pats.append([no, car, dty, Lv, len(cl), total,
                         int(statistics.median(x[1] for x in cl)), int(statistics.median(x[2] for x in cl)),
                         si(mode(3)), si(mode(4)), si(mode(6)), max(x[5] for x in cl),
                         round(sum(x[5] for x in cl) / len(cl))])
    ps, pe = period_range(pid)
    return {
        "v": STATS_VERSION, "id": pid, "from": ps.isoformat(), "to": pe.isoformat(),
        "days": sorted(days), "runs": len(runs), "L": L, "S": S,
        "sec": hist(sec),        # [路線, 方向, 区間, 号車, 時, 平日0/土休日1, [bin,回数,...]]
        "trn": hist(trn),        # [列車番号, 号車, 平日0/土休日1, [bin,回数,...]]
        "tsec": hist(tsec),      # [列車番号, 区間, 号車, 平日0/土休日1, [bin,回数,...]]
        "secMin": [[*k, v] for k, v in secmin.items()],   # [路線,方向,区間,号車,平日/土休日,[bin,分,...]]
        "pat": pats,  # [列車,号車,平日/土休日,レベル,混んだ日数,走った日数,開始分,終了分,開始区間,終了区間,最混雑区間,最大%,平均%]
        "tmeta": {k: [v["l"], v["t"], v["d"], v["r"], v["h"].most_common(1)[0][0]] for k, v in tmeta.items()},
        "cond": cst,
        "rainBins": RAIN_BINS, "windBins": WIND_BINS,
    }


# ================= 横並びタイムライン =================
def timeline(dates):
    out = {"days": dates, "series": {k: {} for k in LINES}}
    for ds in dates:
        raw, cond = load_day(ds)
        g = defaultdict(lambda: [None, None, 0, 0, 0, 0, 0, 0])   # 雨,風,最大遅延,遅延本数,号車観測,多く,大変多く,とても
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
            for _, p in r[9]:
                lv = lv_of(p)
                x[4] += 1
                x[5] += lv >= 5
                x[6] += lv >= 6
                x[7] += lv >= 7
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
            c = cond.get((tm, line))
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


# ================= 出力 =================
def report(now):
    today = now.date()
    dates = all_dates()
    by_period = defaultdict(list)
    for ds in dates:
        for pid in periods_of(dt.date.fromisoformat(ds)):
            by_period[pid].append(ds)
    os.makedirs(STATS, exist_ok=True)
    for pid, days in by_period.items():
        out = f"{STATS}/{pid}.json"
        if period_range(pid)[1] < today and os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                if json.load(f).get("v") == STATS_VERSION:
                    continue                  # 確定済みの期間は作り直さない
        with open(out, "w", encoding="utf-8") as f:
            json.dump(aggregate(pid, days), f, ensure_ascii=False, separators=(",", ":"))
    cutoff = today - dt.timedelta(days=RAW_KEEP_DAYS)
    for p in glob.glob(f"{RAW}/*.csv") + glob.glob(f"{COND}/*.csv"):
        d = dt.date.fromisoformat(os.path.basename(p)[:10])
        if d < cutoff and all(os.path.exists(f"{STATS}/{x}.json") for x in periods_of(d)):
            os.remove(p)
    dates = all_dates()
    shutil.rmtree(SITE, ignore_errors=True)
    shutil.copytree(STATS, f"{SITE}/stats")
    with open(f"{SITE}/stats/timeline.json", "w", encoding="utf-8") as f:
        json.dump(timeline(dates[-TIMELINE_DAYS:]), f, ensure_ascii=False, separators=(",", ":"))
    with open(f"{SITE}/stats/predict.json", "w", encoding="utf-8") as f:
        json.dump(predict(dates[-PREDICT_DAYS:], now), f, ensure_ascii=False, separators=(",", ":"))
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
    for src, dst in (("stats.html", "index.html"), ("weather.html", "weather.html"), ("predict.html", "predict.html")):
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
