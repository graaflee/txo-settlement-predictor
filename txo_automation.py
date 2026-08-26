"""
TXO 結算價自動化核心腳本

用法：
    python txo_automation.py morning   # 結算日 8:00 早盤播報
    python txo_automation.py closing   # 結算日收盤後結算播報（含重試）

資料來源說明見同資料夾的「資料擷取方式.md」。
5 種預測方法（Max Pain / PCP隱含 / OI重心 / 成交量加權 / 歷史偏誤修正）與加權邏輯，
對應 HTML 裡的 analyzeContract() / computeWeights() / computeEnsemble() / computeHistoricalBias()。
"""

import sys
import json
import csv
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
        return {"w1": .2, "w2": .2, "w3": .2, "w4": .2, "w5": .2, "source": "equal"}

    keys = ["mp", "pcp", "oi", "vw", "hb"]
    mae = []
    for key in keys:
        errs = [abs((r["pred"].get(key) if r["pred"].get(key) is not None else r["actual"]) - r["actual"]) for r in done]
        mae.append(sum(errs) / len(errs))

    eps = 1
    inv = [1 / (m + eps) for m in mae]
    inv_sum = sum(inv)
    weights = {f"w{i+1}": inv[i] / inv_sum for i in range(5)}
    weights["source"] = f"基於近 {len(done)} 筆回測"
    return weights


def compute_ensemble(mp, pcp, oi, vw, hb, weights):
    inputs = [mp, pcp, oi, vw, hb]
    ws = [weights["w1"], weights["w2"], weights["w3"], weights["w4"], weights["w5"]]
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

def find_todays_settling_contract(csv_data, today):
    """在 csv_data（{契約: {...expiry}}）裡找契約到期日等於今天的契約。"""
    today_str = today.strftime("%Y%m%d")
    for contract, data in csv_data.items():
        if data["expiry"] == today_str:
            return contract
    return None


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
# Step 4：8:00 早盤播報
# ============================================================

def morning_report():
    today = datetime.now()
    # TXO 週選結算日：週三(W系列，含月選) 或 週五(F系列，2025-06-27上市)。0=Mon ... 2=Wed, 4=Fri
    if today.weekday() not in (2, 4):
        print("今天不是週三或週五，非 TXO 結算日，不執行早盤播報。")
        return

    # 用前一交易日的盤後 CSV 找出今天要結算的契約
    yesterday = today - timedelta(days=1)
    date_str, data = fetch_latest_csv(yesterday)
    if not data:
        print("找不到最近的盤後 CSV，無法產生早盤播報。")
        return

    contract = find_todays_settling_contract(data, today)
    if not contract:
        print(f"{date_str} 的 CSV 裡找不到今天到期的契約，可能今天不是實際交易日或資料尚未涵蓋新契約，略過播報。")
        return

    update_html_snapshot(data, date_str)

    analysis = analyze_contract(data[contract])
    history = load_history()
    weights = compute_weights(history)
    hb = compute_historical_bias(history, analysis["maxPainStrike"])
    ens = compute_ensemble(analysis["maxPainStrike"], analysis["impliedPrice"],
                            analysis["oiCenter"], analysis["volCenter"], hb, weights)

    night_implied = fetch_night_session_implied(date_str).get(contract)
    night_line = "（無夜盤資料可算隱含價，可能夜盤成交清淡）"
    if night_implied is not None and analysis["impliedPrice"] is not None:
        chg = night_implied - analysis["impliedPrice"]
        direction = "漲" if chg > 0 else ("跌" if chg < 0 else "平")
        night_line = (f"夜盤參考：夜盤隱含價 {night_implied}，比日盤 PCP隱含價 "
                      f"{analysis['impliedPrice']} {direction} {abs(chg):.0f} 點（僅供隔夜方向參考，非結算依據）")

    msg = f"""【TXO 結算日早盤播報】{today.strftime('%Y/%m/%d')}
今天結算契約：{contract}（用 {date_str} 收盤資料計算）

Ensemble 綜合預測：{fmt_or_dash(ens)}
- Max Pain：{fmt_or_dash(analysis['maxPainStrike'])}（權重 {weights['w1']*100:.0f}%）
- PCP隱含：{fmt_or_dash(analysis['impliedPrice'])}（權重 {weights['w2']*100:.0f}%）
- OI重心：{fmt_or_dash(analysis['oiCenter'])}（權重 {weights['w3']*100:.0f}%）
- 成交量加權：{fmt_or_dash(analysis['volCenter'])}（權重 {weights['w4']*100:.0f}%）
- 歷史偏誤修正：{fmt_or_dash(hb)}（權重 {weights['w5']*100:.0f}%）

{night_line}
權重依據：{weights['source']}
"""
    print(msg)
    send_email(f"【TXO早盤播報】{contract} 結算日", msg)

    # 把這次的預測值暫存起來，收盤後 closing_report() 會讀取、補上實際結算價
    pending = {
        "date": today.strftime("%Y/%m/%d"),
        "contract": contract,
        "pred": {
            "mp": analysis["maxPainStrike"],
            "pcp": analysis["impliedPrice"],
            "oi": analysis["oiCenter"],
            "vw": analysis["volCenter"],
            "hb": hb,
            "ens": ens,
        },
        "actual": None,
    }
    pending_file = PROJECT_DIR / "pending_prediction.json"
    pending_file.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")


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
    errs = {k: (pred[k] - settle_price if pred[k] is not None else None) for k in ["mp", "pcp", "oi", "vw", "hb", "ens"]}

    history = load_history()
    record = {**pending, "actual": settle_price}
    history.append(record)
    save_history(history)

    new_weights = compute_weights(history)

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
- Ensemble：{fmt(errs['ens'])}

更新後權重：MP {new_weights['w1']*100:.0f}% / PCP {new_weights['w2']*100:.0f}% / OI {new_weights['w3']*100:.0f}% / 量加權 {new_weights['w4']*100:.0f}% / 偏誤修正 {new_weights['w5']*100:.0f}%
（{new_weights['source']}）
"""
    print(msg)
    send_email(f"【TXO盤後播報】{contract} 結算 {settle_price:.0f}", msg)
    pending_file.unlink()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "morning":
        morning_report()
    elif mode == "closing":
        closing_report()
    else:
        print("用法：python txo_automation.py [morning|closing]")
        sys.exit(1)
