#!/usr/bin/env python3
"""
30秒ごとの記録（データベース）＋在線モニターのライブ更新（GitHubだけで動く版）

GitHub Actions の中で約1時間動き続け、30秒ごとに JR西日本のデータを取得して
live ブランチに次を保存します（ブランチは常に1コミットだけに保ち、履歴は増やしません）。
  latest.json          在線モニター用の最新データ（30秒ごと）
  raw/日付/時.csv.gz   統計用の記録（1時間ごとのファイル）。列車の位置・混雑・遅延が変わったとき
                       （止まっている列車は5分に1回）だけ1行追加
取得間隔は30秒が基本。JR側のデータ更新が1分おきなど遅い場合は、自動でそれに合わせます。
次の回が始まると古い回は止められますが、その直前に記録を保存してから終わります（途切れ防止）。
"""
import csv
import datetime as dt
import glob
import gzip
import io
import json
import os
import shutil
import signal
import statistics
import subprocess
import time

import congestion as C

MINUTES = float(os.environ.get("LIVE_MINUTES", "130"))  # 1回の実行で動き続ける最長時間（次の回が始まれば交代）
INTERVAL = int(os.environ.get("LIVE_INTERVAL", "30"))   # 取得間隔の基本（秒）
FLUSH = 60                                               # 記録ファイルを書き出す間隔（秒）
RESAMPLE = 300                                           # 変化がなくても記録し直す間隔（秒）
KEEP_DAYS = 75                                           # 記録の保存日数
WORK = "/tmp/live"
HEADER = ["時刻", "路線", "列車番号", "種別", "行先", "方向", "位置", "遅延分", "号車:乗車率"]
_st = {}
_jr = {"last": None, "t": None, "gaps": []}       # JR側データの更新間隔の計測


class Stop(Exception):
    pass


def _on_signal(signum, frame):
    raise Stop()


def stations(line):
    c = _st.get(line)
    if c and time.time() - c[0] < 3600:
        return c[1]
    j = C.fetch(C.WEST + f"{line}_st.json")
    st = {str(s["info"]["code"]): s["info"]["name"] for s in j.get("stations", []) if s.get("info")}
    _st[line] = (time.time(), st)
    return st


def snapshot(now):
    """在線モニター用データと、統計用の行（候補）を返す"""
    errors, lines, mon, rows, seen = [], {}, {}, [], set()
    try:
        monitor = C.fetch(C.WEST + "trainmonitorinfo.json").get("trains", {})
    except Exception as e:
        monitor = {}
        errors.append(f"混雑データ: {e}")
    hms = now.strftime("%H:%M:%S")
    for line in C.LINES:
        try:
            tj = C.fetch(C.WEST + f"{line}.json")
            trains = tj.get("trains", [])
            if line == "kyoto" and tj.get("update") and tj["update"] != _jr["last"]:
                if _jr["last"] is not None:
                    _jr["gaps"] = (_jr["gaps"] + [time.time() - _jr["t"]])[-30:]
                _jr["last"], _jr["t"] = tj["update"], time.time()
            st = stations(line)
        except Exception as e:
            errors.append(f"{C.LINES[line]}: {e}")
            continue
        lines[line] = {"st": st, "trains": [
            {**{k: t.get(k) for k in ("no", "pos", "direction", "displayType", "nickname", "delayMinutes",
                                      "numberOfCars", "typeChange")},
             "dest": (t.get("dest") or {}).get("text", "") if isinstance(t.get("dest"), dict) else str(t.get("dest") or "")}
            for t in trains]}
        for t in trains:
            no = str(t.get("no", ""))
            cars = sorted((c for u in (monitor.get(no) or []) for c in (u.get("cars") or [])),
                          key=lambda c: c.get("carNo", 0))
            if not cars:
                continue
            mon[no] = [[c.get("carNo"), c.get("congestion"), c.get("temp")] for c in cars]
            if (no, t.get("pos")) in seen:
                continue
            seen.add((no, t.get("pos")))
            dest = t.get("dest")
            rows.append([hms, line, no, t.get("displayType", ""),
                         dest.get("text", "") if isinstance(dest, dict) else str(dest or ""),
                         t.get("direction", ""), C.section_name(t.get("pos"), st), t.get("delayMinutes", ""),
                         ";".join(f"{c.get('carNo')}:{c.get('congestion')}" for c in cars)])
    data = {"t": now.strftime("%Y-%m-%d %H:%M:%S"), "live": "github", "lines": lines, "monitor": mon, "errors": errors,
            "jrUpdateSec": round(statistics.median(_jr["gaps"])) if len(_jr["gaps"]) >= 3 else None,
            "intervalSec": interval()}
    return data, rows


def interval():
    """JR側の更新が遅ければ（中央値55秒以上）、取得間隔をそれに合わせる"""
    if len(_jr["gaps"]) >= 5:
        m = statistics.median(_jr["gaps"])
        if m >= 55:
            return min(120, int(round(m / 30) * 30))
    return INTERVAL


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=WORK, check=check, capture_output=True, text=True)


def hour_path(key):
    ds, hh = key
    return f"{WORK}/raw/{ds}/{hh}.csv.gz"


def read_hour(key):
    p = hour_path(key)
    if not os.path.exists(p):
        return []
    with gzip.open(p, "rt", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        return [row for row in r]


def write_hour(key, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(hour_path(key)), exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    w.writerows(rows)
    with gzip.open(hour_path(key), "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(buf.getvalue())
    cutoff = (dt.date.fromisoformat(key[0]) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for d in glob.glob(f"{WORK}/raw/*"):
        if os.path.basename(d)[:10] < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def main():
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    remote = os.environ.get("LIVE_REMOTE") or f"https://x-access-token:{token}@github.com/{repo}.git"
    time.sleep(float(os.environ.get("LIVE_START_DELAY", "15")))   # 前の回が保存を終えるのを待つ
    first = subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "live", remote, WORK],
                           capture_output=True).returncode != 0
    if first:                                            # live ブランチがまだない
        os.makedirs(WORK, exist_ok=True)
        git("init", "-q")
        git("checkout", "-q", "-b", "live")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898283+github-actions[bot]@users.noreply.github.com")
    os.makedirs(f"{WORK}/raw", exist_ok=True)
    for old in glob.glob(f"{WORK}/raw/*.csv.gz"):         # 旧形式（1日1ファイル）は日付フォルダへ移す
        ds = os.path.basename(old)[:10]
        os.makedirs(f"{WORK}/raw/{ds}", exist_ok=True)
        os.replace(old, f"{WORK}/raw/{ds}/all.csv.gz")

    state = {"key": None, "rows": [], "first": first}
    last, flushed, total = {}, time.time(), 0

    def save(now, force_flush=False):
        nonlocal flushed
        if state["key"] and (force_flush or time.time() - flushed >= FLUSH):
            write_hour(state["key"], state["rows"])
            flushed = time.time()
        git("add", "-A")
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            git("commit", "-q", "-m", f"live {now:%Y-%m-%d %H:%M:%S}", *([] if state["first"] else ["--amend"]))
            git("push", "-q", "-f", remote, "live")
            state["first"] = False

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    end = time.time() + MINUTES * 60
    now = dt.datetime.now(C.JST)
    try:
        while time.time() < end:
            start = time.time()
            now = dt.datetime.now(C.JST)
            if 1 <= now.hour < 4:
                print("深夜帯のため終了")
                break
            key = (now.date().isoformat(), now.strftime("%H"))
            if key != state["key"]:                        # 時間が変わったら前の時間のファイルを確定
                if state["key"]:
                    write_hour(state["key"], state["rows"])
                state["key"], state["rows"] = key, read_hour(key)
            try:
                data, cand = snapshot(now)
                for r in cand:                             # 変化があったとき、または5分ぶりのときだけ記録
                    k = (r[1], r[6], r[7], r[8])
                    lk = last.get(r[2])
                    if lk is None or lk[0] != k or start - lk[1] >= RESAMPLE:
                        state["rows"].append(r)
                        last[r[2]] = (k, start)
                        total += 1
                if data["lines"]:
                    with open(f"{WORK}/latest.json", "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            except Exception as e:
                print("取得失敗:", e)
            try:
                save(now)
            except subprocess.CalledProcessError as e:
                print("保存失敗:", e.stderr)
            time.sleep(max(3, interval() - (time.time() - start)))
    except Stop:
        print("次の回に交代するため、記録を保存して終了")
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            save(now, force_flush=True)
        except Exception as e:
            print("最終保存失敗:", e)
    print(f"この回の記録 {total}行／JR更新間隔の中央値 {statistics.median(_jr['gaps']) if _jr['gaps'] else '-'}秒")


if __name__ == "__main__":
    main()
