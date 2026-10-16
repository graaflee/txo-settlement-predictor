"""
TXO 自動化常駐程式：Windows 登入後自動啟動（開機啟動資料夾），取代工作排程器。

工作排程器啟動的程序在這台電腦上看不到專案資料夾（2026-09-23 起所有排程失敗），
但使用者桌面 session 裡跑的程式正常，所以改成登入後在桌面 session 常駐，自己判斷時間點執行：
  am      平日 07:00~13:30 之間，登入後第一次成功就好（預測更新；結算日會存 pending_prediction.json）
  pm      平日 14:00~18:00，每 10 分鐘試一次直到成功（日盤收盤 CSV 公布後才會成功）
  closing 平日 15:00~19:00，只有 pending_prediction.json 是今天的才跑，每 10 分鐘試一次直到記錄成功
失敗（丟例外）時寄信通知，每個工作每天最多寄一次。
"""

import sys
import json
import time
import socket
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / "runner_state.json"
LOG_FILE = PROJECT_DIR / "runner.log"
PENDING_FILE = PROJECT_DIR / "pending_prediction.json"
LOCK_PORT = 47831
RETRY_SEC = 600
LOOP_SEC = 30

WINDOWS = {
    "am": ((7, 0), (13, 30)),
    "pm": ((14, 0), (18, 0)),
    "closing": ((15, 0), (19, 0)),
}


def due_jobs(now, state, pending_date):
    """回傳此刻該執行的工作名稱清單（純函式，方便測試）。"""
    if now.weekday() >= 5:
        return []
    hm = (now.hour, now.minute)
    jobs = []
    for name in ("am", "pm", "closing"):
        start, end = WINDOWS[name]
        if state.get(name) or not (start <= hm < end):
            continue
        if name == "closing" and pending_date != now.strftime("%Y/%m/%d"):
            continue
        jobs.append(name)
    return jobs


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_state(today):
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if state.get("date") == today:
            return state
    except Exception:
        pass
    return {"date": today}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def read_pending_date():
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8")).get("date")
    except Exception:
        return None


def main():
    sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1, encoding="utf-8")

    # 單一執行個體：綁住本機埠，已有另一份在跑就直接結束
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log("已有另一份 runner 在執行，結束。")
        return

    # 背景執行沒有 console：避免 git 等子程序一直閃黑視窗
    _orig_run = subprocess.run

    def _quiet_run(*a, **kw):
        kw.setdefault("creationflags", 0x08000000)
        return _orig_run(*a, **kw)

    subprocess.run = _quiet_run

    sys.path.insert(0, str(PROJECT_DIR))
    import txo_automation as ta

    funcs = {"am": ta.predict_am, "pm": ta.predict_pm, "closing": ta.closing_report}
    last_try = {}
    log("runner 啟動")

    while True:
        now = datetime.now()
        state = load_state(now.strftime("%Y/%m/%d"))
        for name in due_jobs(now, state, read_pending_date()):
            if time.time() - last_try.get(name, 0) < RETRY_SEC:
                continue
            last_try[name] = time.time()
            log(f"執行 {name}")
            try:
                ok = funcs[name]()
            except Exception:
                err = traceback.format_exc()
                log(f"{name} 發生例外：\n{err}")
                if not state.get(f"{name}_alerted"):
                    state[f"{name}_alerted"] = True
                    save_state(state)
                    ta.send_email(f"[TXO automation FAILED] {name}",
                                  f"runner 執行 {name} 時發生例外：\n\n{err}")
                continue
            if ok:
                state[name] = True
                save_state(state)
                log(f"{name} 完成")
            else:
                log(f"{name} 尚未成功（資料可能還沒公布），稍後重試")
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()
