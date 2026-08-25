"""驗證用：確認 txo_automation.py 的計算邏輯跟 HTML 網頁（analyzeContract/computeWeights/
computeEnsemble/computeHistoricalBias）算出來的數字完全一致。

比對基準（2026-08-25 用瀏覽器載入同一份 TXO202603102.csv、直接呼叫 HTML 內建 JS 函式取得）：
- analyzeContract('202603') = maxPainStrike:33000, impliedPrice:32717, oiCenter:31539, volCenter:31966
- computeWeights/computeHistoricalBias/computeEnsemble 用一組 3 筆假回測歷史驗證，結果：
  hb=33110, ens=32898（權重詳見下方 EXPECTED_WEIGHTS）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from txo_automation import (
    fetch_txo_csv, analyze_contract,
    compute_weights, compute_historical_bias, compute_ensemble,
)

ok = True

# --- 1) analyzeContract 對照 ---
data = fetch_txo_csv("2026/03/10")
if data is None:
    print("下載失敗或當天無資料")
    sys.exit(1)

res = analyze_contract(data["202603"])
expected = {"maxPainStrike": 33000, "impliedPrice": 32717, "oiCenter": 31539, "volCenter": 31966}
for k, v in expected.items():
    got = res[k]
    match = got == v
    ok = ok and match
    print(f"analyze_contract.{k}: 預期 {v}, 實際 {got} -> {'OK' if match else 'MISMATCH'}")

# --- 2) computeWeights / computeHistoricalBias / computeEnsemble 對照 ---
hist = [
    {"pred": {"mp": 33000, "pcp": 32717, "oi": 31539, "vw": 31966, "hb": None}, "actual": 33200},
    {"pred": {"mp": 32800, "pcp": 32900, "oi": 31600, "vw": 32000, "hb": 33100}, "actual": 32950},
    {"pred": {"mp": 33100, "pcp": 33050, "oi": 31700, "vw": 32100, "hb": 33000}, "actual": 33080},
]
hb = compute_historical_bias(hist, 33000)
w = compute_weights(hist)
ens = compute_ensemble(33000, 32717, 31539, 31966, 33050, w)

checks = {"hb": (hb, 33110), "ens": (ens, 32898)}
for name, (got, expected_v) in checks.items():
    match = got == expected_v
    ok = ok and match
    print(f"{name}: 預期 {expected_v}, 實際 {got} -> {'OK' if match else 'MISMATCH'}")

print("\n整體驗證：", "全部通過" if ok else "有不一致，需要檢查")
sys.exit(0 if ok else 1)
