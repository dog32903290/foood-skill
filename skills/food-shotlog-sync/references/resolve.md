# DaVinci Resolve：依場記表建立 AB 校對時間線

## 前提與操作界線

使用者已要求建立時間線才執行 apply。先準備可檢查的離線 plan；原始影片與既有專案保留。腳本只建立新的 review project；切換前存檔並備份當前專案到 work。重跑僅能續建 session 記錄 UUID 的自有專案與時間線。找不到 SDK、需要登入或遇到 API 不相容時保留表格與 plan，回報具體阻塞。

需實際安裝 Resolve 並可使用外部 scripting API。以使用者電腦安裝的 SDK README／Python module 為準，不把專有 SDK 複製進 repo。macOS 預設搜尋：

`/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules`

另提供 Windows／Linux 預設位置，可在 `resolve.script_modules_path` 或 `RESOLVE_SCRIPT_API` 設定 Modules 或其父目錄。Resolve 必須啟動，edition／偏好設定須允許 scripting；不同版本的支援由本機 SDK 確認。

若 bundled Python 無法載入 Resolve 的原生模組，使用與已裝 SDK 相容的系統 Python 執行 `scripts/foodlog_cli.py`。輸入／輸出使用 UTF-8；可設 `PYTHONUTF8=1`。不要因 SDK 載入失敗就替使用者換 Python 或改全域設定。

## 正常路徑

```bash
foodlog --json --config /path/foodlog.local.json sheet import-pairs /path/approved.xlsx
foodlog --json resolve status
foodlog --json --config /path/foodlog.local.json resolve plan
# 宿主讀取 plan 並確認已在使用者要求的範圍
foodlog --json --config /path/foodlog.local.json resolve apply --execute
foodlog --json --config /path/foodlog.local.json resolve inspect
foodlog --json --config /path/foodlog.local.json resolve capture --output /path/evidence/resolve-001.png
```

擷取後必須實際開圖。存檔、切換／重載 owned project、重開正確 timeline，再跑 inspect 與截圖。可透過宿主可用的真正 UI 工具完成重開；CLI 不自動把存檔當成重開證據。檢查有無 Media Offline、A／B 位置、波形、marker 及錯誤範圍。將實際觀察填 `assets/resolve-ui-review.example.json`，全部成立才登錄：

```bash
foodlog --json --config /path/foodlog.local.json resolve ui-review /path/ui-review.json
foodlog --json --config /path/foodlog.local.json resolve export --directory /path/new-resolve-export
```

macOS 截圖需螢幕錄製權限；auto window discovery 需 `capture` extra，或傳入當次真實 `--window-id`。Windows／Linux 使用宿主現有真正螢幕工具，再以圖片 hash 登錄。不能使用合成時間線示意圖當作實際 UI。

## 排放規則

- 預設 V1 A 機（下）、V2 B 機（上）。A1 原音參考靜音，A2 B 原音可聽。
- 只用同一機的影音互相 linked；不對整條 AB 鎖死。每組間距預設約 1 秒。
- 原音波形配對：藍色 clip 與 marker。時間配對：橘色 clip、黃色 marker。Orange 不是有效 marker color。
- B 為有音軌的實速參考；本版不支援雙邊皆慢動作的自動建置。
- 每卡獨立 bin；媒體按完整路徑比對，不以 basename 去重。
- 時間線紀錄幀 = `timeline.GetStartFrame() + relative_frame`。`01:00:00:00` 不能假設等於相對第 0 幀，亦不要硬編 108000。
- 所有入出點量化到來源／時間線幀率；readback 允許 SDK 的 1 幀差異，影音結束與來源 trim 必須吻合。

## 慢動作的易錯處

Sony XML 的 capture FPS 與 format FPS 不同時，原檔播放長度不是實拍長度。倍率 `ratio = capture_fps / format_fps`，實拍長度為原檔長度／ratio。例：59.94 capture、29.97 format，40 秒原檔對應 20 秒實拍。

在新的校對專案，先把 A MediaPoolItem FPS conform 到 capture FPS，再建立時間線、按 native source frame 裁切。原始檔與舊專案不受修改。這個 review timeline 的目的是看同時拍攝，並不替使用者決定成片的慢動作美感。

已知 Resolve 21.1 上，對裁過的 S&Q TimelineItem 呼叫 `SetSpeed(200)` 會重設 source out，導致 A／B／audio 長度不一致。不能只看 clip 數量正確就通過。此 adapter 避開這條路。

`AppendToTimeline` 在已觀察的 21.1 行為採 `endFrame = startFrame + N` 才得到 N 幀；SDK 版本不同可能有邊界差異，故必須讀回 source start、duration、end。若不吻合立即停止，不偷偷擴大容差。

## 中斷、版本與完成判定

`resolve_state.json` 保存 plan 指紋、project UUID、timeline UUID、每組 checkpoint。`apply --execute --resume` 先確認身份，再以位置＋來源＋trim 查已存在項，避免重複追加。plan 改變或同一位置有別的片段時停止；另建 work／project。

數值 inspect 驗證軌道、來源、FPS、in/out、online 狀態與 marker；`ui-review` 登錄實際觀察和圖片 hash。hash 只證明觀察對應的檔案，不會自動證明畫面正確。交付時分開報告 API 檢查與畫面檢查是否完成。

DRT／DRP 是時間線／專案封裝，並未內嵌原始影片；換電腦仍需掛載素材或 relink。此流程不自動輸出成片、不修改現有剪輯、不建立 proxy。

設計來自 macOS、Sony XML、29.97／59.94 與 Resolve Studio 21.1 的實際製作經驗。公開重構版已保留 readback 與出錯停止設計；其他版本、混合幀率、Windows／Linux 未逐一實機驗證，不宣稱通用幀級保證。VFR 疑慮會阻擋 placement，請先另製 CFR 中介檔並重新盤點。
