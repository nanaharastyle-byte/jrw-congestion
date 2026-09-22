#!/usr/bin/env python3
"""
JR西日本 車両別混雑ロガー（琵琶湖線・JR京都線・奈良線・嵯峨野線）

  python congestion.py collect  … 5分ごと: 列車位置と号車別の乗車率を data/raw/日付.csv に追記
  python congestion.py report   … 1時間ごと: 週間・月間の集計を作り、_site/ に統計ページを出力

データの出どころ（JR西日本 列車走行位置サービス）
  /api/v3/{路線}.json            列車の位置・遅延（delayMinutes）
  /api/v3/{路線}_st.json         駅の一覧
  /api/v3/trainmonitorinfo.json  号車ごとの乗車率(%)・状態・車内温度
"""
import csv
import datetime as dt
import glob
import json
import os
import shutil
import sys
import urllib.request
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ORIGIN = "https://www.train-guide.westjr.co.jp/api/v3/"
LINES = {"hokurikubiwako": "琵琶湖線", "kyoto": "JR京都線", "nara": "奈良線", "sagano": "嵯峨野線"}
RAW = "data/raw"
STATS = "data/stats"
SITE = "_site"
RAW_KEEP_DAYS = 75          # 生データの保存日数（確定した週・月の集計は data/stats に残ります）
MAX_BIN = 25                # 乗車率は10%刻みで集計（250%以上はまとめる）


def get(name):
    req = urllib.request.Request(ORIGIN + name, headers={
        "User-Agent": "Mozilla/5.0 (personal congestion logger)",
        "Referer": "https://www.train-guide.westjr.co.jp/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


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


# ================= 収集 =================
def collect(now):
    monitor = get("trainmonitorinfo.json").get("trains", {})
    rows, seen, errors = [], set(), []
    for line in LINES:
        try:
            trains = get(f"{line}.json").get("trains", [])
            m = {str(s["info"]["code"]): s["info"]["name"]
                 for s in get(f"{line}_st.json").get("stations", []) if s.get("info")}
        except Exception as e:
            errors.append(f"{LINES[line]}: {e}")
            continue
        for t in trains:
            no = str(t.get("no", ""))
            cars = [c for u in (monitor.get(no) or []) for c in (u.get("cars") or [])]
            if not cars or (no, t.get("pos")) in seen:
                continue                      # 混雑データなし／他路線で記録済み
            seen.add((no, t.get("pos")))
            cars.sort(key=lambda c: c.get("carNo", 0))
            dest = t.get("dest")
            rows.append([now.strftime("%H:%M"), line, no, t.get("displayType", ""),
                         dest.get("text", "") if isinstance(dest, dict) else str(dest or ""),
                         t.get("direction", ""), section_name(t.get("pos"), m), t.get("delayMinutes", ""),
                         ";".join(f"{c.get('carNo')}:{c.get('congestion')}:{c.get('status')}:{c.get('temp')}"
                                  for c in cars)])
    if len(errors) == len(LINES):
        print("すべての路線で取得に失敗:\n" + "\n".join(errors), file=sys.stderr)
        sys.exit(1)
    os.makedirs(RAW, exist_ok=True)
    path = f"{RAW}/{now:%Y-%m-%d}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["時刻", "路線", "列車番号", "種別", "行先", "方向", "位置", "遅延分", "号車:乗車率:状態:温度"])
        w.writerows(rows)
    if monitor:                               # 形式確認用の見本
        k = next(iter(monitor))
        with open("data/sample_monitor.json", "w", encoding="utf-8") as f:
            json.dump({k: monitor[k]}, f, ensure_ascii=False, indent=1)
    with open("data/status.json", "w", encoding="utf-8") as f:
        json.dump({"last": now.strftime("%Y-%m-%d %H:%M"), "trainsWithData": len(rows),
                   "monitorTrains": len(monitor), "errors": errors}, f, ensure_ascii=False)
    print(f"記録 {len(rows)}本（混雑データのある列車 全体{len(monitor)}本）")


# ================= 集計 =================
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
    e = (s.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return s, e


def aggregate(pid, files):
    sec, trn, tsec = Counter(), Counter(), Counter()
    tmeta, S, idx = {}, [], {}
    runs, days = set(), set()

    def si(s):
        if s not in idx:
            idx[s] = len(S)
            S.append(s)
        return idx[s]

    for path in sorted(files):
        d = dt.date.fromisoformat(os.path.basename(path)[:10])
        dty = day_type(d)
        days.add(d.isoformat())
        last = {}
        with open(path, encoding="utf-8") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if len(row) < 9:
                    continue
                tm, line, no, typ, dest, dr, secn, _delay, cars = row[:9]
                runs.add(d.isoformat() + tm)
                if last.get(no) == secn:
                    continue                  # 同じ区間にいる間は1回だけ数える
                last[no] = secn
                hr = int(tm[:2])
                s = si(secn)
                meta = tmeta.setdefault(no, {"l": line, "t": typ, "d": dest, "r": dr, "h": Counter()})
                meta["h"][hr] += 1
                for c in cars.split(";"):
                    p = c.split(":")
                    try:
                        car, pct = int(p[0]), int(p[1])
                    except (ValueError, IndexError):
                        continue
                    if pct < 0:
                        continue
                    b = min(pct // 10, MAX_BIN)
                    sec[(line, int(dr or 0), s, car, hr, dty, b)] += 1
                    trn[(no, car, dty, b)] += 1
                    if b >= 10:
                        tsec[(no, s, car, dty, b)] += 1
    def hist(cnt):                            # 同じ条件の行をまとめ、[乗車率bin, 回数, ...] の形にする
        g = defaultdict(list)
        for k, v in sorted(cnt.items()):
            g[k[:-1]] += [k[-1], v]
        return [[*k, v] for k, v in g.items()]

    L = list(LINES)
    sec = Counter({(L.index(k[0]), *k[1:]): v for k, v in sec.items()})
    ps, pe = period_range(pid)
    return {
        "id": pid, "from": ps.isoformat(), "to": pe.isoformat(), "days": sorted(days), "runs": len(runs),
        "L": L, "S": S,
        "sec": hist(sec),     # [路線, 方向, 区間, 号車, 時, 平日0/土休日1, [bin,回数,...]]
        "trn": hist(trn),     # [列車番号, 号車, 平日0/土休日1, [bin,回数,...]]
        "tsec": hist(tsec),   # [列車番号, 区間, 号車, 平日0/土休日1, [bin,回数,...]]（乗車率100%以上のみ）
        "tmeta": {k: [v["l"], v["t"], v["d"], v["r"], v["h"].most_common(1)[0][0]] for k, v in tmeta.items()},
    }


def report(now):
    today = now.date()
    by_period = defaultdict(list)
    for p in glob.glob(f"{RAW}/*.csv"):
        for pid in periods_of(dt.date.fromisoformat(os.path.basename(p)[:10])):
            by_period[pid].append(p)
    os.makedirs(STATS, exist_ok=True)
    for pid, files in by_period.items():
        closed = period_range(pid)[1] < today
        out = f"{STATS}/{pid}.json"
        if closed and os.path.exists(out):
            continue                          # 確定済みの期間は作り直さない
        with open(out, "w", encoding="utf-8") as f:
            json.dump(aggregate(pid, files), f, ensure_ascii=False, separators=(",", ":"))
    cutoff = today - dt.timedelta(days=RAW_KEEP_DAYS)
    for p in glob.glob(f"{RAW}/*.csv"):       # 古い生データの削除（集計確定済みのものだけ）
        d = dt.date.fromisoformat(os.path.basename(p)[:10])
        if d < cutoff and all(os.path.exists(f"{STATS}/{x}.json") for x in periods_of(d)):
            os.remove(p)
    shutil.rmtree(SITE, ignore_errors=True)
    shutil.copytree(STATS, f"{SITE}/stats")
    ids = sorted((os.path.basename(p)[:-5] for p in glob.glob(f"{STATS}/*.json")), reverse=True)
    status = {}
    if os.path.exists("data/status.json"):
        with open("data/status.json", encoding="utf-8") as f:
            status = json.load(f)
    with open(f"{SITE}/stats/index.json", "w", encoding="utf-8") as f:
        json.dump({"periods": ids, "built": now.strftime("%Y-%m-%d %H:%M"), "status": status, "lines": LINES},
                  f, ensure_ascii=False)
    if os.path.exists("data/sample_monitor.json"):
        shutil.copy("data/sample_monitor.json", f"{SITE}/sample_monitor.json")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.html"), f"{SITE}/index.html")
    with open("data/last_publish.txt", "w") as f:
        f.write(now.strftime("%Y-%m-%d %H"))
    print("統計ページを作成:", ", ".join(ids) or "（データなし）")


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
