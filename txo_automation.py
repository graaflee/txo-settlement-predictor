"""
TXO 結算價自動化核心腳本

用法（每個交易日 Mon-Fri 都跑，不限結算日）：
    python txo_automation.py predict_am   # 約 07:00，盤後/夜盤收盤後的預測更新
    python txo_automation.py predict_pm   # 約 14:00，日盤收盤後的預測更新（一定要用當天CSV）
    python txo_automation.py closing      # 只在真正結算日排程（週三/週五），收盤後結算播報（含重試）

predict_am/predict_pm 都會找「最近一個還沒結算的契約」播報，平常交易日也會有內容；
只有 predict_am 算出來的最近契約「到期日剛好是今天」時，才會存 pending_prediction.json
給 closing_report() 用，回測仍然只在真正結算日才會有記錄。

資料來源說明見同資料夾的「資料擷取方式.md」。
6 種預測方法（Max Pain / PCP隱含 / OI重心 / 成交量加權 / 歷史偏誤修正 / 夜盤隱含價）與加權邏輯，
對應 HTML 裡的 analyzeContract() / computeWeights() / computeEnsemble() / computeHistoricalBias()。
"""

import sys
import json
import csv
import subprocess
import io
import re
import time
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from pathlib import Path
from urllib import request, parse

PROJECT_DIR = Path(__file__).resolve().parent
HISTORY_FILE = PROJECT_DIR / "backtest_history.json"
EMAIL_CONFIG_FILE = PROJECT_DIR / "email_config.json"  # 不進 git（見 .gitignore），裡面存 Gmail 應用程式密碼
HTML_FILE = PROJECT_DIR / "TXO結算價預測 (1).html"

# 另一個 Claude session 維護的券商信件情報資料庫（見該目錄的 README.md）。
# 這批資料要靠 LLM 讀 Gmail/PDF 才能更新，不是本腳本能自動維護的，只負責讀取現有內容；
# 讀不到檔案或當天沒資料都安靜略過，不影響本腳本主流程。
BROKER_REPORTS_DIR = Path(r"C:\Users\A113010\Documents\Claude基本功\broker-reports")
BROKER_RELEVANT_KEYWORDS = ("大盤", "指數期貨", "選擇權")


def get_broker_context(date_str):
    """讀 broker-reports/records/YYYY-MM.json，抓出跟大盤/指數期貨/選擇權相關、
    日期符合的報告摘要，當作播報信裡的質化補充參考。date_str 格式 'YYYY/MM/DD'。"""
    try:
        date_dash = date_str.replace("/", "-")
        year_month = date_dash[:7]
        record_file = BROKER_REPORTS_DIR / "records" / f"{year_month}.json"
        if not record_file.exists():
            return []
        records = json.loads(record_file.read_text(encoding="utf-8"))
        return [
            r for r in records
            if r.get("date") == date_dash
            and any(kw in (r.get("category") or "") for kw in BROKER_RELEVANT_KEYWORDS)
        ]
    except Exception:
        return []


def format_broker_context(items):
    if not items:
        return ""
    lines = [f"- [{b.get('broker','?')}] {b.get('subject','')}：{b.get('summary','')}" for b in items]
    return "\n【當天券商報告參考（另一套 Gmail 抓信系統維護，非本腳本自動更新）】\n" + "\n".join(lines)


def update_html_snapshot(csv_data, date_str):
    """把早盤播報抓到的最新 CSV 資料同步進網頁：更新 RAW_DATA、預設契約、契約選擇按鈕、
    交易日期，效果等同於使用者手動在網頁上傳同一份 CSV（複用同樣的欄位格式與按鈕產生規則，
    對應 HTML 裡的 rebuildContractButtons()）。找不到 HTML 檔案或寫入失敗都只印警告，不中斷主流程。"""
    if not HTML_FILE.exists():
        print(f"（找不到 {HTML_FILE.name}，略過網頁同步。）")
        return
    try:
        html = HTML_FILE.read_text(encoding="utf-8")
        lines = html.split("\n")

        contracts = sorted(csv_data.keys())
        if not contracts:
            print("（CSV 沒有解析出任何契約，略過網頁同步。）")
            return
        first_contract = contracts[0]

        raw_data_json = json.dumps(csv_data, ensure_ascii=False)
        found_raw_data = found_current = found_header_date = found_bar = False

        buttons_html = []
        for i, key in enumerate(contracts):
            expiry = csv_data[key].get("expiry", "") or ""
            exp_fmt = f"{expiry[4:6]}/{expiry[6:8]}" if len(expiry) == 8 else ""
            active = " active" if i == 0 else ""
            buttons_html.append(
                f'    <button class="contract-btn{active}" data-contract="{key}" onclick="selectContract(this)">'
                f'{key}<br><span style="font-size:9px;opacity:.6">{exp_fmt}</span></button>'
            )

        out_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("const RAW_DATA = "):
                out_lines.append(f"const RAW_DATA = {raw_data_json};")
                found_raw_data = True
            elif line.startswith("let currentContract = "):
                out_lines.append(f"let currentContract = '{first_contract}';")
                found_current = True
            elif '<span id="headerDateVal">' in line:
                display_date = date_str.replace("/", " / ")
                out_lines.append(re.sub(r'(<span id="headerDateVal">)[^<]*(</span>)',
                                         rf'\g<1>{display_date}\g<2>', line))
                found_header_date = True
            elif line.strip() == '<div class="contract-bar">':
                out_lines.append(line)          # <div class="contract-bar">
                i += 1
                out_lines.append(lines[i])       # <label>選擇契約</label>
                i += 1
                while lines[i].strip() != "</div>":
                    i += 1
                out_lines.extend(buttons_html)
                out_lines.append(lines[i])       # </div>
                found_bar = True
            else:
                out_lines.append(line)
            i += 1

        if not (found_raw_data and found_current and found_header_date and found_bar):
            print(f"（網頁同步：找不到預期的標記位置 raw_data={found_raw_data} "
                  f"current={found_current} header_date={found_header_date} bar={found_bar}，"
                  "已放棄寫入避免弄壞檔案。）")
            return

        HTML_FILE.write_text("\n".join(out_lines), encoding="utf-8")
        print(f"（已同步網頁快照：{len(contracts)} 個契約，交易日期 {date_str}）")
    except Exception as e:
        print(f"（網頁同步失敗，不影響回測紀錄本身：{e}）")


def sync_bt_history_to_html():
    """把 backtest_history.json 的內容烘進網頁的 BAKED_BT_HISTORY，使用者在自己電腦打開網頁時
    （見 HTML 裡的 mergeBakedBtHistory()）會自動併入瀏覽器的 localStorage，不用手動同步。
    跟 update_html_snapshot() 分開處理：盤後結算播報沒有重新抓 CSV，不需要走那條路徑，
    只需要更新回測歷史這一個標記。"""
    if not HTML_FILE.exists():
        return
    try:
        html = HTML_FILE.read_text(encoding="utf-8")
        lines = html.split("\n")
        history = load_history()
        history_json = json.dumps(history, ensure_ascii=False)
        found = False
        for i, line in enumerate(lines):
            if line.startswith("const BAKED_BT_HISTORY = "):
                lines[i] = f"const BAKED_BT_HISTORY = {history_json};"
                found = True
                break
        if not found:
            print("（網頁同步：找不到 BAKED_BT_HISTORY 標記，略過回測歷史同步。）")
            return
        HTML_FILE.write_text("\n".join(lines), encoding="utf-8")
        print(f"（已同步 {len(history)} 筆回測歷史進網頁）")
    except Exception as e:
        print(f"（回測歷史同步失敗，不影響紀錄本身：{e}）")


def send_email(subject, body):
    """用 Gmail SMTP 寄一封通知信給自己，讓手機上的 Gmail app 推播出來。
    設定檔不存在或內容不完整時，只印出警告、不讓整支腳本因為寄信失敗而中斷。"""
    if not EMAIL_CONFIG_FILE.exists():
        print(f"（找不到 {EMAIL_CONFIG_FILE.name}，略過寄信。請照說明建立這個檔案。）")
        return
    try:
        cfg = json.loads(EMAIL_CONFIG_FILE.read_text(encoding="utf-8"))
        from_email, app_password, to_email = cfg["from_email"], cfg["app_password"], cfg["to_email"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"（{EMAIL_CONFIG_FILE.name} 格式不對，略過寄信：{e}）")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(from_email, app_password)
            server.send_message(msg)
        print("（已寄出通知信）")
    except Exception as e:
        print(f"（寄信失敗，不影響回測紀錄本身：{e}）")


def git_commit_and_push(message):
    """排程跑完後，把這次產生的變更（網頁快照、回測歷史）commit + push 回 GitHub。
    2026-09-09 發現：網頁的即時資料只存在工作目錄裡、從沒進版控，導致一次 git revert
    意外把當天排程剛同步的資料洗掉。加這個之後，每次排程跑完都會留下一個版本快照，
    之後任何 git 操作要動到這兩個檔案，都至少有紀錄可以救回來。
    這一步失敗（沒網路、git 沒設定等）只印警告，此時播報跟寄信都已經做完了，不影響主流程。"""
    try:
        subprocess.run(["git", "add", "TXO結算價預測 (1).html", "backtest_history.json"],
                        cwd=PROJECT_DIR, check=True, capture_output=True, text=True, encoding="utf-8")
        commit = subprocess.run(["git", "commit", "-m", message],
                                 cwd=PROJECT_DIR, capture_output=True, text=True, encoding="utf-8")
        if commit.returncode != 0:
            out = commit.stdout + commit.stderr
            if "nothing to commit" in out or "no changes added to commit" in out:
                print("（git：這次沒有變更需要 commit）")
            else:
                print(f"（git commit 失敗，不影響主流程：{commit.stderr.strip() or commit.stdout.strip()}）")
            return
        push = subprocess.run(["git", "push"], cwd=PROJECT_DIR, capture_output=True, text=True, encoding="utf-8")
        if push.returncode != 0:
            print(f"（已本機 commit，但 push 失敗（可能沒網路），之後可以手動 push：{push.stderr.strip()}）")
        else:
            print("（已自動 commit + push 這次的網頁快照與回測歷史）")
    except Exception as e:
        print(f"（git 自動 commit 失敗，不影響主流程：{e}）")


def fmt_or_dash(v):
    """None 顯示成「—」，數字四捨五入成整數字串，讓播報訊息裡不會冒出 Python 的 None 字樣。"""
    return "—" if v is None else f"{v:.0f}"


def js_round(x):
    """比照 JS 的 Math.round()：永遠四捨五入（round half up），跟 Python 內建 round() 的
    banker's rounding（round half to even）不同，避免兩邊算出差 1 點的數字。"""
    import math
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)

OPT_DATA_URL = "https://www.taifex.com.tw/cht/3/optDataDown"
SETTLE_PRICE_URL = "https://www.taifex.com.tw/cht/5/optIndxFSP"  # 選擇權專用結算價頁面（不是期貨的 futIndxFSP，那個沒有週五 F 系列契約）
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"}


# ============================================================
# 資料下載與解析
# ============================================================

def http_post(url, fields):
    data = parse.urlencode(fields).encode("utf-8")
    req = request.Request(url, data=data, headers=HEADERS, method="POST")
    with request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw


def fetch_txo_csv(date_str):
    """下載指定日期(YYYY/MM/DD)的 TXO 選擇權盤後 CSV，回傳 {契約: {calls:{}, puts:{}, expiry: str}}。
    若當天沒有交易資料（假日、尚未公布），回傳 None。"""
    raw = http_post(OPT_DATA_URL, {
        "down_type": "1",
        "commodity_id": "TXO",
        "commodity_id2": "",
        "queryStartDate": date_str,
        "queryEndDate": date_str,
    })
    text = raw.decode("big5", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return None

    header = rows[0]
    idx = {name: header.index(name) for name in
           ["契約", "到期月份(週別)", "履約價", "買賣權", "成交量", "結算價",
            "未沖銷契約數", "交易時段", "契約到期日", "開盤價", "最高價", "最低價", "收盤價",
            "最後最佳買價", "最後最佳賣價"]
           if name in header}

    def to_num(v):
        v = v.strip()
        if v in ("", "-"):
            return None
        try:
            n = float(v)
            return None if n < 0 else n
        except ValueError:
            return None

    result = {}
    for row in rows[1:]:
        if len(row) < 10:
            continue
        if row[idx["交易時段"]].strip() != "一般":
            continue
        contract = row[idx["到期月份(週別)"]].strip()
        if not contract:
            continue
        if contract not in result:
            result[contract] = {"calls": {}, "puts": {}, "expiry": row[idx["契約到期日"]].strip()}
        try:
            strike = int(float(row[idx["履約價"]]))
        except ValueError:
            continue
        entry = {
            "s": to_num(row[idx["結算價"]]),
            "v": to_num(row[idx["成交量"]]) or 0,
            "oi": to_num(row[idx["未沖銷契約數"]]) or 0,
            "b": to_num(row[idx["最後最佳買價"]]),
            "a": to_num(row[idx["最後最佳賣價"]]),
            "c": to_num(row[idx["收盤價"]]),
        }
        opt_type = row[idx["買賣權"]].strip()
        if opt_type == "買權":
            result[contract]["calls"][strike] = entry
        elif opt_type == "賣權":
            result[contract]["puts"][strike] = entry
    return result if result else None


def fetch_night_session_implied(date_str):
    """抓同一份 CSV 裡'盤後'(夜盤)列的資料，用跟日盤 PCP 隱含價一樣的公式（履約價 + 買權收盤 - 賣權收盤，
    取平均），算出夜盤的隱含現貨價，回傳 {契約: 夜盤隱含價}，供早盤播報跟日盤 PCP 隱含價比較當隔夜方向參考。
    夜盤沒有未沖銷契約數，所以不能算 Max Pain / OI重心，只能算這個。單一選擇權的原始收盤價本身沒有參考意義，
    不直接使用。"""
    raw = http_post(OPT_DATA_URL, {
        "down_type": "1",
        "commodity_id": "TXO",
        "commodity_id2": "",
        "queryStartDate": date_str,
        "queryEndDate": date_str,
    })
    text = raw.decode("big5", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return {}
    header = rows[0]
    idx = {name: header.index(name) for name in
           ["到期月份(週別)", "履約價", "買賣權", "收盤價", "交易時段"] if name in header}

    by_contract = {}
    for row in rows[1:]:
        if len(row) < 10 or row[idx["交易時段"]].strip() != "盤後":
            continue
        contract = row[idx["到期月份(週別)"]].strip()
        close = row[idx["收盤價"]].strip()
        if close in ("", "-"):
            continue
        try:
            strike = int(float(row[idx["履約價"]]))
            price = float(close)
        except ValueError:
            continue
        opt_type = row[idx["買賣權"]].strip()
        by_contract.setdefault(contract, {"calls": {}, "puts": {}})
        if opt_type == "買權":
            by_contract[contract]["calls"][strike] = price
        elif opt_type == "賣權":
            by_contract[contract]["puts"][strike] = price

    result = {}
    for contract, sides in by_contract.items():
        implied = [k + sides["calls"][k] - sides["puts"][k]
                   for k in sides["calls"] if k in sides["puts"]]
        if implied:
            result[contract] = js_round(sum(implied) / len(implied))
    return result


def fetch_settlement_prices(year, month):
    """查詢台期所「臺指選擇權（TXO）最後結算價」表，回傳 {契約: 最後結算價}。
    這個表同時涵蓋週三(W系列)、週五(F系列，2025-06-27上市)、月選共 3 種結算契約，
    不能用期貨的 futIndxFSP 頁面（那個只涵蓋 TX 期貨傳統的週三/月結算，沒有週五選擇權）。"""
    raw = http_post(SETTLE_PRICE_URL, {
        "start_year": str(year),
        "start_month": f"{month:02d}",
        "end_year": str(year),
        "end_month": f"{month:02d}",
        "commodityIds": "2",
    })
    text = raw.decode("big5", errors="ignore")
    rows = re.findall(
        r'<TD[^>]*align=middle>(\d{4}/\d{2}/\d{2})</TD>\s*<TD[^>]*align=middle>\s*([\w]+)\s*</TD>\s*<TD[^>]*align=right>([\d.]+)</TD>',
        text,
    )
    return {contract.strip(): float(price) for _date, contract, price in rows}


# ============================================================
# 5 種方法計算（對應 HTML 的 analyzeContract）
# ============================================================

def analyze_contract(data):
    calls, puts = data["calls"], data["puts"]
    all_strikes = sorted(set(calls) | set(puts))
    if not all_strikes:
        return None

    # 1) Max Pain
    pain = []
    for ts in all_strikes:
        total = 0
        for k in all_strikes:
            coi = calls.get(k, {}).get("oi", 0)
            poi = puts.get(k, {}).get("oi", 0)
            if ts > k:
                total += (ts - k) * coi
            if ts < k:
                total += (k - ts) * poi
        pain.append((ts, total))
    min_pain = min(p for _, p in pain)
    max_pain_strike = next(ts for ts, p in pain if p == min_pain)

    # 2) PCP implied price
    implied = []
    for k in all_strikes:
        cs, ps = calls.get(k, {}).get("s"), puts.get(k, {}).get("s")
        if cs and ps and cs > 0 and ps > 0:
            implied.append(k + cs - ps)
    implied_price = js_round(sum(implied) / len(implied)) if implied else None

    # 3) OI-weighted center
    call_oi_sum = sum(calls.get(k, {}).get("oi", 0) for k in all_strikes)
    put_oi_sum = sum(puts.get(k, {}).get("oi", 0) for k in all_strikes)
    call_center = js_round(sum(k * calls.get(k, {}).get("oi", 0) for k in all_strikes) / call_oi_sum) if call_oi_sum else None
    put_center = js_round(sum(k * puts.get(k, {}).get("oi", 0) for k in all_strikes) / put_oi_sum) if put_oi_sum else None
    oi_center = js_round((call_center + put_center) / 2) if call_center and put_center else None

    # 3b) Volume-weighted center
    call_v_sum = sum(calls.get(k, {}).get("v", 0) for k in all_strikes)
    put_v_sum = sum(puts.get(k, {}).get("v", 0) for k in all_strikes)
    call_vcenter = js_round(sum(k * calls.get(k, {}).get("v", 0) for k in all_strikes) / call_v_sum) if call_v_sum else None
    put_vcenter = js_round(sum(k * puts.get(k, {}).get("v", 0) for k in all_strikes) / put_v_sum) if put_v_sum else None
    vol_center = js_round((call_vcenter + put_vcenter) / 2) if call_vcenter and put_vcenter else None

    return {
        "maxPainStrike": max_pain_strike,
        "impliedPrice": implied_price,
        "oiCenter": oi_center,
        "volCenter": vol_center,
        "expiry": data["expiry"],
    }


def compute_historical_bias(history, max_pain_strike, n=10):
    if max_pain_strike is None:
        return None
    done = [r for r in history if r.get("actual") is not None and r.get("pred", {}).get("mp") is not None][-n:]
    if len(done) < 2:
        return None
    avg_bias = sum(r["actual"] - r["pred"]["mp"] for r in done) / len(done)
    return js_round(max_pain_strike + avg_bias)


def compute_weights(history, n=10):
    done = [r for r in history if r.get("actual") is not None][-n:]
    if len(done) < 2:
        return {"w1": 1/6, "w2": 1/6, "w3": 1/6, "w4": 1/6, "w5": 1/6, "w6": 1/6, "source": "equal"}

    # 只用「這個方法真的有給過預測值」的那幾筆算 MAE，不要把缺值當成誤差 0（不然一個方法
    # 還沒累積出真實預測值時，會被 inverse-MAE 誤判成「完美」，把權重拉爆）。
    # 一筆預測值都沒有的方法，直接給權重 0，不參與 inverse-MAE 計算。
    keys = ["mp", "pcp", "oi", "vw", "hb", "ns"]
    mae = []
    has_data = []
    for key in keys:
        errs = [abs(r["pred"][key] - r["actual"]) for r in done if r["pred"].get(key) is not None]
        if errs:
            mae.append(sum(errs) / len(errs))
            has_data.append(True)
        else:
            mae.append(None)
            has_data.append(False)

    eps = 1
    inv = [1 / (m + eps) if has_data[i] else 0 for i, m in enumerate(mae)]
    inv_sum = sum(inv)
    if inv_sum == 0:
        return {"w1": 1/6, "w2": 1/6, "w3": 1/6, "w4": 1/6, "w5": 1/6, "w6": 1/6, "source": "equal"}
    weights = {f"w{i+1}": inv[i] / inv_sum for i in range(6)}
    weights["source"] = f"基於近 {len(done)} 筆回測"
    return weights


def compute_ensemble(mp, pcp, oi, vw, hb, ns, weights):
    inputs = [mp, pcp, oi, vw, hb, ns]
    ws = [weights["w1"], weights["w2"], weights["w3"], weights["w4"], weights["w5"], weights["w6"]]
    total, wsum = 0, 0
    for val, w in zip(inputs, ws):
        if val is not None:
            total += val * w
            wsum += w
    return js_round(total / wsum) if wsum > 0 else None


# ============================================================
# 歷史檔讀寫
# ============================================================

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# 判斷今天是否為結算日、要結算哪個契約
# ============================================================

def find_nearest_contract(csv_data, today):
    """在 csv_data（{契約: {...expiry}}）裡找到期日 >= 今天、且最接近今天的契約。
    回傳 (contract, expiry_date)；找不到（例如資料是舊的、全部已過期）回傳 (None, None)。
    2026-09-09 起取代 find_todays_settling_contract()：預測不再限定「今天正好結算」，
    平常交易日也要能找到「最近一個還沒結算的週選」來持續更新預測。"""
    candidates = []
    for contract, data in csv_data.items():
        expiry_str = data.get("expiry", "") or ""
        if len(expiry_str) != 8:
            continue
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y%m%d")
        except ValueError:
            continue
        if expiry_date.date() >= today.date():
            candidates.append((expiry_date, contract))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][0]


def fetch_latest_csv(start_date, max_back_days=5):
    """從 start_date 開始往前找最近一份有資料的 CSV（跳過假日/尚未公布的日子）。"""
    d = start_date
    for _ in range(max_back_days):
        date_str = d.strftime("%Y/%m/%d")
        data = fetch_txo_csv(date_str)
        if data:
            return date_str, data
        d -= timedelta(days=1)
    return None, None


# ============================================================
# Step 4：每個交易日兩次的預測更新播報
# 2026-09-09 改版：不再限定「今天正好結算才播報」，改成每個交易日跑兩次——
#   predict_am()：約 07:00（盤後/夜盤收盤後），用最新可得的盤後 CSV（通常是前一交易日）
#   predict_pm()：約 14:00（日盤收盤後），一定要用「今天自己」的 CSV，資料還沒公布就先跳過等重試
# 兩次都預測「最近一個還沒結算的契約」，不是只有結算日當天才有東西可報。
# 只有 predict_am() 在算出來的最近契約「到期日剛好是今天」時，才會寫 pending_prediction.json
# 給 closing_report() 用——這樣回測書籍仍然只在真正的結算日才會有記錄（維持原本的設計），
# predict_pm() 純粹是資訊性的更新，完全不碰 pending_prediction.json，兩者不會互相覆蓋。
# ============================================================

def generate_prediction_report(session_label, use_today_csv, save_pending):
    today = datetime.now()

    if use_today_csv:
        date_str = today.strftime("%Y/%m/%d")
        data = fetch_txo_csv(date_str)
        if not data:
            print(f"{date_str} 的盤後 CSV 尚未公布，稍後排程會再試一次，暫不播報。")
            return
    else:
        date_str, data = fetch_latest_csv(today)
        if not data:
            print("找不到最近的盤後 CSV，無法產生播報。")
            return

    contract, expiry_date = find_nearest_contract(data, today)
    if not contract:
        print(f"{date_str} 的 CSV 裡找不到任何尚未到期的契約，略過播報。")
        return
    is_settlement_today = expiry_date.date() == today.date()

    update_html_snapshot(data, date_str)
    sync_bt_history_to_html()

    analysis = analyze_contract(data[contract])
    history = load_history()
    weights = compute_weights(history)
    hb = compute_historical_bias(history, analysis["maxPainStrike"])

    # 注意：要用「今天」的日期查夜盤，不是 date_str。台期所的夜盤歸屬日規則是
    # 「T-1 15:00 ~ T 05:00」算在 T 的盤後資料裡，所以「今天開盤前的那個晚上」要查今天的檔案
    # （2026-08-26 實測發現查錯日期的 bug）。夜盤隱含價是第 6 種正式方法，不只是參考文字。
    night_implied = fetch_night_session_implied(today.strftime("%Y/%m/%d")).get(contract)

    ens = compute_ensemble(analysis["maxPainStrike"], analysis["impliedPrice"],
                            analysis["oiCenter"], analysis["volCenter"], hb, night_implied, weights)

    night_line = "（無夜盤資料可算隱含價，可能夜盤成交清淡）"
    if night_implied is not None and analysis["impliedPrice"] is not None:
        chg = night_implied - analysis["impliedPrice"]
        direction = "漲" if chg > 0 else ("跌" if chg < 0 else "平")
        night_line = (f"夜盤隱含價比日盤 PCP隱含價 "
                      f"{analysis['impliedPrice']} {direction} {abs(chg):.0f} 點")

    broker_context = format_broker_context(get_broker_context(date_str))
    contract_line = (f"{contract}（今天結算，用 {date_str} 資料計算）" if is_settlement_today
                      else f"{contract}（{expiry_date.strftime('%m/%d')} 結算，用 {date_str} 資料計算）")

    msg = f"""【TXO {session_label}播報】{today.strftime('%Y/%m/%d')}
最近待結算契約：{contract_line}

Ensemble 綜合預測：{fmt_or_dash(ens)}
- Max Pain：{fmt_or_dash(analysis['maxPainStrike'])}（權重 {weights['w1']*100:.0f}%）
- PCP隱含：{fmt_or_dash(analysis['impliedPrice'])}（權重 {weights['w2']*100:.0f}%）
- OI重心：{fmt_or_dash(analysis['oiCenter'])}（權重 {weights['w3']*100:.0f}%）
- 成交量加權：{fmt_or_dash(analysis['volCenter'])}（權重 {weights['w4']*100:.0f}%）
- 歷史偏誤修正：{fmt_or_dash(hb)}（權重 {weights['w5']*100:.0f}%）
- 夜盤隱含價：{fmt_or_dash(night_implied)}（權重 {weights['w6']*100:.0f}%）{'　' + night_line if night_implied is not None else ''}

權重依據：{weights['source']}
{broker_context}
"""
    print(msg)
    send_email(f"【TXO {session_label}播報】{contract}{'（今天結算）' if is_settlement_today else ''}", msg)

    if is_settlement_today and save_pending:
        pending = {
            "date": today.strftime("%Y/%m/%d"),
            "contract": contract,
            "pred": {
                "mp": analysis["maxPainStrike"],
                "pcp": analysis["impliedPrice"],
                "oi": analysis["oiCenter"],
                "vw": analysis["volCenter"],
                "hb": hb,
                "ns": night_implied,
                "ens": ens,
            },
            "actual": None,
        }
        pending_file = PROJECT_DIR / "pending_prediction.json"
        pending_file.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
        print("（今天是這個契約的結算日，已存下預測值供盤後結算比對）")

    git_commit_and_push(f"{session_label} {today.strftime('%Y/%m/%d')} {contract}")


def predict_am():
    """約 07:00 執行：盤後/夜盤收盤後的預測更新。save_pending=True——只有這個時段的執行
    會在結算日寫 pending_prediction.json 給 closing_report() 用。"""
    generate_prediction_report("盤後收盤", use_today_csv=False, save_pending=True)


def predict_pm():
    """約 14:00 執行：日盤收盤後的預測更新，一定要用當天自己的 CSV（還沒公布就先跳過等重試）。
    save_pending=False——2026-09-09 發現：這個時段如果也寫 pending_prediction.json，
    跟它自己的 14:00/14:30/15:00 重試、以及 closing_report() 的 15:00/16:00/17:00 重試
    時間點重疊，可能出現「回測先消化掉早上那筆並刪除檔案，這裡的重試又重新寫一份新的，
    回測下一次重試又把它當成新資料再記一次」的重複記錄。只讓 predict_am() 寫這個檔案就
    不會有這個問題，predict_pm() 純粹是資訊性更新。"""
    generate_prediction_report("早盤收盤", use_today_csv=True, save_pending=False)


METHOD_NAMES = {"mp": "Max Pain", "pcp": "PCP隱含", "oi": "OI重心",
                 "vw": "成交量加權", "hb": "歷史偏誤修正", "ns": "夜盤隱含價"}
POSITION_BASED = {"mp", "oi", "vw"}   # 部位型：反映前一天收盤的舊部位
FORWARD_LOOKING = {"pcp", "ns"}       # 前瞻型：反映即時/接近結算時點的市場預期


def build_discrepancy_report(record, history, n=10):
    """比對這次結算誤差跟近期回測歷史，判斷趨勢、方法排名，產生類似人工分析的差異報告文字。
    邏輯是把 2026-08-26/08-28 兩次手動分析時用的推論方式（趨勢盤中部位型方法系統性落後、
    比較近期哪個方法最準）寫成規則，不是每次都重新用 LLM 分析。"""
    pred, actual = record["pred"], record["actual"]
    errs = {k: (pred[k] - actual) for k in METHOD_NAMES if pred.get(k) is not None}
    lines = []

    # 1) 這次表現排名
    ranked_this = sorted(errs.items(), key=lambda kv: abs(kv[1]))
    if ranked_this:
        best_key, best_err = ranked_this[0]
        worst_key, worst_err = ranked_this[-1]
        lines.append(f"這次最準：{METHOD_NAMES[best_key]}（誤差 {best_err:+.0f} 點）；"
                     f"最差：{METHOD_NAMES[worst_key]}（誤差 {worst_err:+.0f} 點）")

    # 2) 趨勢判斷：看最近幾次「已結算」的實際價格是否連續同向
    done = [r for r in history if r.get("actual") is not None]
    recent_actuals = [r["actual"] for r in done[-4:]]
    trend = None
    if len(recent_actuals) >= 3:
        diffs = [recent_actuals[i + 1] - recent_actuals[i] for i in range(len(recent_actuals) - 1)]
        if all(d > 0 for d in diffs):
            trend = "up"
        elif all(d < 0 for d in diffs):
            trend = "down"
    if trend == "up":
        lines.append(f"近 {len(recent_actuals)} 個結算日連續上漲（{recent_actuals[0]:.0f} → {recent_actuals[-1]:.0f}），屬於連續趨勢盤。")
    elif trend == "down":
        lines.append(f"近 {len(recent_actuals)} 個結算日連續下跌（{recent_actuals[0]:.0f} → {recent_actuals[-1]:.0f}），屬於連續趨勢盤。")

    # 3) 部位型 vs 前瞻型 系統性偏差判斷（只在確認是趨勢盤時才下這個結論，避免亂歸因）
    if trend is not None and errs:
        pos_errs = [errs[k] for k in errs if k in POSITION_BASED]
        fwd_errs = [errs[k] for k in errs if k in FORWARD_LOOKING]
        if pos_errs and fwd_errs:
            pos_avg = sum(pos_errs) / len(pos_errs)
            fwd_avg = sum(fwd_errs) / len(fwd_errs)
            if abs(pos_avg) > abs(fwd_avg) * 1.3:
                direction = "低估" if pos_avg < 0 else "高估"
                lines.append(f"部位型方法（Max Pain/OI重心/成交量加權）平均{direction} {abs(pos_avg):.0f} 點，"
                             f"比前瞻型方法（PCP隱含/夜盤隱含價，平均誤差 {fwd_avg:+.0f} 點）差不少——"
                             f"符合「連續趨勢盤時部位型方法系統性落後」的已知模式。")

    # 4) 近期（近 n 筆）各方法平均表現排名
    window = done[-n:]
    if len(window) >= 2:
        method_mae = {}
        for key in METHOD_NAMES:
            vals = [abs(r["pred"][key] - r["actual"]) for r in window if r["pred"].get(key) is not None]
            if vals:
                method_mae[key] = sum(vals) / len(vals)
        if method_mae:
            best_recent = min(method_mae, key=method_mae.get)
            worst_recent = max(method_mae, key=method_mae.get)
            lines.append(f"近 {len(window)} 筆回測平均表現：「{METHOD_NAMES[best_recent]}」最好"
                         f"（平均誤差 {method_mae[best_recent]:.0f} 點），"
                         f"「{METHOD_NAMES[worst_recent]}」最差（平均誤差 {method_mae[worst_recent]:.0f} 點）。")

    return "\n".join(f"- {l}" for l in lines) if lines else "（回測紀錄還不夠多，之後累積更多筆才能看出趨勢）"


# ============================================================
# Step 5：盤後結算播報
# ============================================================
# 重試策略：不在單一次執行裡用 time.sleep() 卡著等（雲端排程 session 一次執行時間有限，
# 不適合裡面睡 1-2 小時），改成排程本身在收盤後那個時段設定多個觸發時間點（例如
# 15:00 / 16:00 / 17:00），每次執行只試一次；已經記錄過的話會自動跳過，不會重複入帳。

def closing_report(max_retries=1, retry_interval_sec=0):
    pending_file = PROJECT_DIR / "pending_prediction.json"
    if not pending_file.exists():
        # 可能是同一天稍早已經成功執行過（pending 用完會被刪除），檢查歷史檔避免誤判成失敗
        history = load_history()
        today_str = datetime.now().strftime("%Y/%m/%d")
        if any(r["date"] == today_str and r.get("actual") is not None for r in history):
            print(f"今天（{today_str}）的盤後結算已經記錄過了，不用重複執行。")
        else:
            print("找不到今天的早盤預測紀錄（pending_prediction.json），無法產生盤後播報。請確認早盤播報有先執行過、且有 commit/push 回 repo。")
        return

    pending = json.loads(pending_file.read_text(encoding="utf-8"))
    contract = pending["contract"]
    today = datetime.now()

    settle_price = None
    for attempt in range(max_retries):
        prices = fetch_settlement_prices(today.year, today.month)
        if contract in prices:
            settle_price = prices[contract]
            break
        if attempt < max_retries - 1:
            time.sleep(retry_interval_sec)

    if settle_price is None:
        print(f"今天（{contract}）的結算價目前還查不到，稍後排程會再試一次。如果到收盤後好幾個小時都還是這樣，可能要手動上台期所官網確認。")
        return

    pred = pending["pred"]
    errs = {k: (pred[k] - settle_price if pred.get(k) is not None else None) for k in ["mp", "pcp", "oi", "vw", "hb", "ns", "ens"]}

    history = load_history()
    record = {**pending, "actual": settle_price}
    history.append(record)
    save_history(history)
    sync_bt_history_to_html()

    new_weights = compute_weights(history)
    discrepancy = build_discrepancy_report(record, history)
    broker_context = format_broker_context(get_broker_context(record["date"]))

    def fmt(v):
        return "—" if v is None else f"{v:+.0f}"

    msg = f"""【TXO 結算日盤後播報】{today.strftime('%Y/%m/%d')}
契約：{contract}　實際結算價：{settle_price:.0f}

各方法誤差（預測 - 實際）：
- Max Pain：{fmt(errs['mp'])}
- PCP隱含：{fmt(errs['pcp'])}
- OI重心：{fmt(errs['oi'])}
- 成交量加權：{fmt(errs['vw'])}
- 歷史偏誤修正：{fmt(errs['hb'])}
- 夜盤隱含價：{fmt(errs['ns'])}
- Ensemble：{fmt(errs['ens'])}

更新後權重：MP {new_weights['w1']*100:.0f}% / PCP {new_weights['w2']*100:.0f}% / OI {new_weights['w3']*100:.0f}% / 量加權 {new_weights['w4']*100:.0f}% / 偏誤修正 {new_weights['w5']*100:.0f}% / 夜盤 {new_weights['w6']*100:.0f}%
（{new_weights['source']}）

【差異分析】
{discrepancy}
{broker_context}
"""
    print(msg)
    send_email(f"【TXO盤後播報】{contract} 結算 {settle_price:.0f}", msg)
    pending_file.unlink()

    git_commit_and_push(f"盤後結算 {record['date']} {contract} 實際{settle_price:.0f}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "predict_am":
        predict_am()
    elif mode == "predict_pm":
        predict_pm()
    elif mode == "closing":
        closing_report()
    else:
        print("用法：python txo_automation.py [predict_am|predict_pm|closing]")
        sys.exit(1)
