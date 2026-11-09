# Pong (Python)

經典 Pong：左側是你，右側是電腦（或雙人對戰時的另一位玩家），先到目標分數者勝。
單一檔案 `pong.py`，Tkinter 深色介面，分數與所有訊息都直接畫在同一個 Canvas 上，
不需要任何第三方套件。

程式分三層：純物理 `PongGame`、圖形介面 `PongGUI`、無視窗對打 `run_simulate`。

## 執行

```bash
python3 pong.py                      # 玩（AI 速度 normal）
python3 pong.py -d easy              # AI 慢一點（3.0 px/格）
python3 pong.py -d hard              # AI 快一點（6.0 px/格）
python3 pong.py --ai-speed 5.0       # 自訂 AI 速度，會覆蓋 -d
python3 pong.py --win-score 11       # 先到 11 分
python3 pong.py --two-player         # 開局就是雙人模式
python3 pong.py --seed 7             # 固定發球與反彈的隨機
python3 pong.py --simulate 30        # 無視窗：內建追蹤器對打 30 秒並印出結果
python3 pong.py --selftest           # 跑邏輯測試（27 項）
```

沒有 tkinter 或開不了視窗時，`--simulate` 與 `--selftest` 仍可正常使用。

## 操作

| 按鍵 | 動作 |
| --- | --- |
| ↑ / ↓ 或 W / S | 移動左側球拍 |
| 滑鼠移動 | 直接移動左側球拍（與鍵盤並存，最後動的那個生效） |
| Space 或點擊畫面 | 發球、開始下一分 |
| `1` / `2` / `3` | AI 速度 easy / normal / hard |
| `T` | 切換雙人模式（會重開一局） |
| `P` | 暫停 |
| `N`、`R` 或 `F2` | 新的一局 |
| `Esc` | 離開 |

雙人模式：左邊用 W / S，右邊用 ↑ / ↓。介面沒有選單列，難度與雙人模式都用鍵盤切換。

畫面上的中文字：發球前顯示「按空白鍵發球」、暫停顯示「暫停」、結束時顯示
「你贏了／你輸了」（雙人是「左邊贏了／右邊贏了」）與「按 N 或點擊重來」。
兩側比數以大字畫在球場上方，左邊藍色 `#5ea1ff`、右邊紅色 `#ff6b6b`。

## 特色

- 固定時間步：`step()` 代表 1/60 秒的一格，速度單位是「像素/格」，所以邏輯與畫面更新率無關
- 球每被球拍打到一次加速 7%，上限 12 px/格；擊球點離球拍中心越遠，垂直速度變化越大（上限 ±7）
- 球以子步進移動（每步最多半個球半徑），高速球不會穿過球拍
- AI 一路追球但速度有上限（easy 3.0 / normal 4.2 / hard 6.0 px/格），
  所以用球拍邊緣打出角度、把球送到角落就能得分
- 發球往分數落後的一方（平手時隨機）；得分後球回到中央等下一球
- 達到目標分數立即結束，之後 `step()` 與 `serve()` 都不再有任何效果

## 結構

`pong.py`

- `PongGame` — 純物理與規則，完全不碰 UI：`serve`、`score_point`、`step`、
  `_advance_ball`、`_move_paddle`、`_move_ai`、`tracking_direction`、`move_paddle_to`
- `PongGUI` — Canvas 繪製與鍵盤/滑鼠控制。按住方向鍵的偵測用「延遲 50 ms 才移除按鍵」
  的做法，避開 macOS 自動重複時 KeyPress/KeyRelease 交錯造成的抖動
- `run_simulate` — 無視窗對打（左側由 `tracking_direction` 操控），供 `--simulate` 與測試使用

`--selftest` 的 27 項檢查涵蓋：開局待發球、發球後球有速度且不能重複發球、上下牆反彈、
球拍回擊與每次加速、擊球點造成角度變化、漏球計分、得分後回中央、AI 追球方向正確且
遵守速度上限、球拍不會離開球桌、鍵盤上下與滑鼠輸入（含夾限）、雙人模式會驅動右拍、
比賽在目標分數結束後完全凍結、以及 AI 對打能打完一整場並記錄最長來回拍數。
