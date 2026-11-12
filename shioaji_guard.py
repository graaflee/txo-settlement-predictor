"""Shioaji 使用守則：出差期間不呼叫 API，並避免多個專案同時登入互相卡住。

1) 出差日曆（兩個專案共用）：C:\\Users\\A113010\\.shioaji_shared\\trips.json
   {"trips": [{"start": "2026-11-16", "end": "2026-11-18", "note": "出差"}]}   日期含頭尾，以台北時間的日期判斷。
2) 跨程序鎖：同一台電腦上，同一時間只允許一個程序處於 Shioaji 登入狀態。
   原因：Shioaji 會在 ~/.shioaji 共用合約快取，兩個程序同時登入會互相逾時（實測遇過 exclusive access lost）。
   用法：with shioaji_session(api_key, secret_key) as api: ...  （TXO 專案想採用時直接複製這個檔案即可）
"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

SHARED_DIR = Path.home() / ".shioaji_shared"
TRIPS_PATH = SHARED_DIR / "trips.json"
LOCK_PATH = SHARED_DIR / "login.lock"
STALE_LOCK_SEC = 15 * 60

TAIPEI_TZ = timezone(timedelta(hours=8))


class TripModeError(Exception):
    """出差期間，不呼叫 Shioaji。"""


class LockTimeout(Exception):
    pass


def today_taipei() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def trip_on(day: str, trips_path: Path = TRIPS_PATH) -> Optional[dict]:
    """回傳涵蓋 day 的出差紀錄，沒有則回傳 None。"""
    if not trips_path.exists():
        return None
    try:
        trips = json.loads(trips_path.read_text(encoding="utf-8")).get("trips", [])
    except (OSError, ValueError):
        return None
    for t in trips:
        if t.get("start", "") <= day <= t.get("end", ""):
            return t
    return None


def assert_not_trip(day: Optional[str] = None, trips_path: Path = TRIPS_PATH) -> None:
    trip = trip_on(day or today_taipei(), trips_path)
    if trip:
        raise TripModeError(f"出差期間（{trip['start']} ~ {trip['end']}）不呼叫 Shioaji")


@contextmanager
def file_lock(lock_path: Path = LOCK_PATH, timeout: float = 180.0, poll: float = 1.0):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}".encode())
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > STALE_LOCK_SEC:
                    lock_path.unlink()  # 持有者當掉沒釋放，視為過期
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise LockTimeout(f"等待 Shioaji 登入鎖逾時（{timeout:.0f} 秒），另一個程序可能還在使用")
            time.sleep(poll)
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass


@contextmanager
def shioaji_session(api_key: str, secret_key: str, *, simulation: bool = False, check_trip: bool = True,
                    lock_path: Path = LOCK_PATH):
    """出差檢查 → 取得跨程序鎖 → 登入 → 交給呼叫端 → 登出並釋放鎖。"""
    if check_trip:
        assert_not_trip()
    import shioaji as sj
    with file_lock(lock_path):
        api = sj.Shioaji(simulation=simulation)
        try:
            api.login(api_key=api_key, secret_key=secret_key)
            yield api
        finally:
            try:
                api.logout()
            except Exception:
                pass
