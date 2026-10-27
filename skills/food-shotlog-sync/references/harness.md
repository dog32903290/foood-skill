# CLI 合約與資料格式

## 安裝與設定

Repo 根目錄 `pip install -e .` 安裝公共 Python 套件；選用 extra 為 `asr`、`mlx`、`environment`、`capture`。另裝 ffmpeg／ffprobe。只複製 skill 到別台電腦時，也可在有相同依賴的 Python 執行 `scripts/foodlog_cli.py`。

```bash
foodlog --json status
foodlog --json init --source-root /path/project/01_video --work-dir /path/project/outputs/run-001 --output /path/project/foodlog.local.json --card A001:A:A001 --card B001:B:B001
foodlog --json --config /path/project/foodlog.local.json inventory
```

路徑可含空格與中文，shell 中需引號。預設掃 source root 下一層 A001／B001 等卡目錄，遞迴查影片。設定相對路徑以設定檔所在位置為基準；卡路徑以 source_root 為基準。工作目錄必須在所有卡目錄外。設定樣本在 `assets/project.example.json`；刪除不使用的 optional model 欄位或換成自己的已安裝模型。

素材 ID 例如 `A001/CLIP/SHOT0001.MP4`，不是 basename。Sony sidecar 預期同目錄 `SHOT0001M01.XML`。找不到 capture FPS／含時區的拍攝時間時不猜測；必要時從相機證據補設定或人工校對。當前 adapter 不自動解析其他廠牌 sidecar。

## 命令面

全域 `--json`、`--config` 放在子命令前。機器讀取 stdout 的 JSON；模型進度可能在 stderr。`--help` 是文字說明。

| 命令 | 效果／前提 |
|---|---|
| `status` | Python、依賴、ffmpeg；有 config 時列 session head |
| `list capabilities` | 可用命令與模型／來源寫入界線 |
| `inventory [--limit N]` | 讀影片、XML，建立不可變 inventory；limit 表示不完整盤點 |
| `list clips [--limit N]` | 列完整 ID、時長、倍率 |
| `audio prepare` | 依來源指紋產生多聲道 PCM 與量測 |
| `model-download BACKEND MODEL` | 明確下載 allowlist 內模型；不需 config |
| `asr [--clip-id ID]` | 本機逐段轉錄；多個 ID 可重複旗標 |
| `environment --backend ast [--clip-id ID]` | 本機聲音分類候選；另可用 `local-audio` |
| `polish` | 本機文字服務順稿建議，原稿保留 |
| `review pack [--clip-id ID] [--frames 4] [--batch-size 12]` | 擷取實際影格、縮圖總覽、hash manifest |
| `review local` | local vision endpoint 機器初稿，可省略 |
| `review import REVIEW.json` | 登錄實際看過圖的 Codex／人工紀錄 |
| `sync [--clock-bias-seconds N]` | 原音波形、時鐘校正、無原音時間配對 |
| `sheet export --output NEW.xlsx [--template OLD.xlsx]` | 新工作簿；原表格另存並追加欄位與明細 |
| `sheet inspect OLD.xlsx` | 唯讀列出工作表、前 7 列與 hash；不需 config |
| `sheet import-pairs BOOK.xlsx [--sheet NAME]` | 以校對後配對表取代 session 的 pairs head |
| `resolve status` | 連接真實 SDK、讀目前專案；不需 config |
| `resolve plan` | 離線規劃新專案與幀位置，不改 Resolve |
| `resolve apply --execute [--resume]` | 建立新的校對專案；需使用者已要求此操作 |
| `resolve inspect` | 唯讀核對真實軌道、入出點、原檔、FPS、marker |
| `resolve capture --output NEW.png` | macOS 實際視窗截圖；可指定 `--window-id` |
| `resolve ui-review REVIEW.json` | 先檢查 API，再登錄真實截圖觀察 |
| `resolve export --directory NEW_DIR` | 新 DRT／DRP 與 API 核對 JSON |
| `validate` | 來源 size/mtime、ID、時間範圍、配對、圖片 hash |
| `logs` | session、bundles、trajectory 位置 |

`asr`、`environment`、`review pack` 未指定 ID 時處理全 inventory。`review local` 處理目前 pack。讀取與列出命令不等同完成辨識或 UI 驗證。

## 工件、重跑與失敗

每個 stage 產生新的 `work/bundles/<timestamp-uuid>/`，含 `<stage>.json` 及 `manifest.json`（設定指紋、工件 hash）。`session.json` 指向各 stage 最新版本；`trajectory.jsonl` 記錄成功發布的 stage。快取在 `work/cache`，不同來源指紋／設定使用不同鍵。命令錯誤在 JSON 回覆中，trajectory 不是完整失敗日誌。

工作階段使用作業系統排他鎖；同一 work 不可平行執行寫入命令。程式中斷後重新執行同 stage 可使用已完成快取，未發布的影格包另建新包。來源清單／指紋改變時請換新 work，舊 bundles 保留。來源指紋為檔案大小與奈秒 mtime 加 metadata，不宣稱完整 bitwise checksum 或雲端檔案驗證。

例如成功回覆：

```json
{"ok":true,"protocol":"foodlog/v1","version":"0.1.0","command":"inventory","result":{"clips":120,"artifact":"/work/bundles/id/inventory.json"}}
```

失敗使用非零 exit code、`error.type`、`message`、`repair_hint`、`context`。可處理的例子：

| type | 動作 |
|---|---|
| `dependency_missing` / `backend_missing` | 安裝指定 extra／ffmpeg 或修正執行路徑 |
| `source_missing` / `source_changed` | 確認磁碟；變更來源時另開 session |
| `stage_missing` | 完成訊息指定的前置 stage |
| `output_exists` | 選新路徑，不能覆寫使用者檔案 |
| `clock_inconsistent` | 分開拍攝時段／檢查相機校時與誤配 |
| `comparison_budget` | 縮小單次卡組或提供有證據的 clock bias；不要直接無限加大 |
| `ambiguous_clip` / `template_mapping_ambiguous` | 按卡別、完整路徑人工對應，不按檔名猜 |
| `resolve_api_unavailable` | 檢查實際 SDK、版本與腳本設定，保留離線 plan |
| `placement_mismatch` / `ambiguous_resume` | 停止，檢查既有 owned project；不要重複 append |

## 配對欄位與時間單位

pairs JSON：`group` 唯一字串、`a_id`／`b_id`、`a_in/out`／`b_in/out` 原檔秒數、`duration` 實拍重疊秒、`ratio` A 原檔播放秒／實拍秒、`lag` 波形偏移或 null、`basis`、`subject`。

原音波形：`B秒 = A秒 + lag`。相機時差定義 `bias = A拍攝時間 − B拍攝時間 − lag`。無原音與慢動作：`B秒 = A原檔秒 / ratio + A拍攝時間 − B拍攝時間 − bias`；此時不把推算量填進原音 lag 欄。

波形確認要求跨不同位置的至少 3 個一致錨點；預設 lag spread 0.04 秒。先以 3 組校正時鐘，再檢查後續波形與全域偏移是否矛盾。存在時鐘重置／漂移時分 session。時間配對保留預設 2 秒不確定範圍，不能承諾幀級同步。沒有足夠時鐘證據時只交付已成立的波形配對。

XLSX 配對表預設 `AB機同步對照`，第 7 列欄名，第 8 列起資料；13 欄依序：

1. 配對編號
2. 同步依據：`原音波形`／`拍攝時間（無原音）`／`拍攝時間`
3. A機卡別／原檔名
4. B機卡別／原檔名
5. 內容／場景
6. A片內入點
7. A片內出點
8. B片內入點
9. B片內出點
10. 實拍重疊（秒）
11. B－A偏移（秒）
12. A播放倍率
13. 配對內容／同步錨點

第 6–9 欄是 Excel duration／時間值或 `h:m:s` 文字；純數字視為 Excel 日數。第 10–12 欄為秒／倍數的純數字。舊表 `CARD/basename` 在同卡唯一時才可映射到新完整 ID。不同卡同檔名永不合併。

模板 adapter 預期每卡同名工作表、一鏡一列、精確檔名；新增欄在原有欄位後面、標題列 7。會檢查原儲存格值在存讀前後一致；不保證任意原生物件、外掛／樞紐／複雜圖表完整往返。宏工作簿不支援。模型文字以字串寫入，不當成 Excel 公式。
