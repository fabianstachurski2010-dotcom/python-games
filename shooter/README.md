# Barrage 彈幕突擊 (Python)

縱向彈幕射擊：你的砲台自己開火，你只負責閃。敵人一波一波下來，每 5 波有一隻頭目、
每 5 波換一個區域；擦過子彈會得分，連段會把分數加倍，被撞到扣一條命並掉一級火力，
真的被圍死時還有一顆炸彈可以把整個畫面清掉。
遊戲本體是單一檔案 `shooter.py`，Tkinter 深色介面，只用標準庫，沒有第三方套件。

程式分三層：純邏輯 `ShooterGame`（完全不碰 UI，可無視窗測試）、圖形介面 `ShooterGUI`
（含 `Pool` 物件池）、內建閃避 bot `bot_control` + 無視窗對打 `run_simulate`。

另外附三個可以單獨使用的檔案（都放同一資料夾）：

| 檔案 | 做什麼 |
|---|---|
| `shooter_train.py` | 用 numpy 演化策略訓練一個會自己打彈幕的神經網路，存成 `shooter_model.npz` |
| `shooter_play.py` | 載入訓練好的模型，讓它上場打給你看（也可以無視窗跑） |
| `shooter_audio.py` | 從零合成 12 個音效（標準庫 `wave`），播放時自動挑最快的後端 |

## 執行

```bash
python3 shooter.py                       # 玩（普通）
python3 shooter.py -d easy               # 簡單：子彈慢、敵人軟、波數少
python3 shooter.py -d hard               # 困難：子彈快 25%、敵人硬、波數多
python3 shooter.py --lives 5 --bombs 4   # 五條命、四顆炸彈
python3 shooter.py --sound               # 開音效（見下面的「音效」）
python3 shooter.py --seed 7              # 固定波次與敵人組成（可重現）
python3 shooter.py --no-highscore        # 不讀也不寫 shooter/highscore.json
python3 shooter.py --simulate 60         # 無視窗：內建 bot 打 60 秒並印出結果
python3 shooter.py --text 20             # 無視窗：印出第 20 秒的 ASCII 畫面
python3 shooter.py --selftest            # 跑邏輯測試（179 項）
```

沒有 tkinter 或開不了視窗時，`--simulate`、`--text`、`--selftest` 仍可正常使用。

## 操作

| 按鍵 | 動作 |
| --- | --- |
| `W` `A` `S` `D` / 方向鍵 | 移動（放開會滑行減速；最後按的方向為準） |
| 滑鼠移動 | 移動：飛向指標（碰到鍵盤就交還給鍵盤） |
| `Shift` | 集中模式：速度降到 132 px/s、畫出小小的判定點 |
| `Space` / 左鍵 | 衝刺：0.15 秒無敵、約 1.15 秒冷卻，往最後的行進方向衝 |
| `X` | 炸彈：清光敵彈與光束、全場敵人受 14 點傷害、1.4 秒無敵 |
| `P` | 暫停 |
| `R` / `N` / `F2` | 重新開始 |
| `1` / `2` / `3` | 簡單 / 普通 / 困難（會直接開新的一局） |
| `Esc` | 離開（這一局的分數仍會進排行榜） |

面板上方是分數、區域＋波數＋連段、最高分、火力、難度；下面一列是生命（♥）、護盾
（綠圈）、炸彈（`X ◆◆`）與衝刺冷卻條；頭目出現時多一條 BOSS 血條，畫面最底部是
**這一波的清除進度條**。畫面上的中文字：開場是標題＋操作＋排行榜、暫停顯示「暫停」、
結束顯示「遊戲結束」、分數、區域、命中率、無傷清波、最高連段與排行榜。

## 訓練一個會玩的網路（`shooter_train.py`）

跟 `pong_train.py`／`minesweeper_train.py` 同一套形狀：精英保留 + 前 50% 變異，每一代
都存檔，Ctrl+C 會保留目前最好的。用 numpy，不需要其他套件。

```bash
python3 shooter_train.py                    # 訓練（預設 45 代 × 48 個個體 × 20 秒一局）
python3 shooter_train.py --jobs 4           # 用 4 個行程平行評估
python3 shooter_train.py --resume --seconds 30    # 從存檔繼續，練更長的局
python3 shooter_train.py --test             # 測存檔，並和手寫 bot 比較
python3 shooter_train.py --selftest         # 訓練器自己的 24 項檢查
```

網路只看「玩家看得到的數字」：自己的座標／生命／護盾／火力／炸彈／衝刺冷卻、最近的
8 顆敵彈、最近的 3 個敵人、最近的 2 道光束，加上波數／敵人數／彈數／波次進度／連段
（69 個輸入）→ 32 個隱藏層 → 4 個輸出（dx、dy、衝刺、炸彈）。它**不會**用到
`bot_control`；那份手寫策略只在 `--test` 當比較基準。

獎勵是 `存活秒數 + 2×擊破 + 30×已清波數 + 6×無傷清波 + 0.002×分數 + 0.5×擦彈`，
也就是「先活著、再推進度、順手賺分」。

實際的訓練是兩輪：先 45 代 × 20 秒局（約 13 分鐘），再 `--resume` 25 代 × 30 秒局
（約 9 分鐘），一共 70 代。最後在 **20 局沒見過的 30 秒局**上跟手寫 bot 對照：

| | fitness | 存活 | 波數 | 擊破 | 分數 | 命中率 | 擦彈 |
|---|---|---|---|---|---|---|---|
| 演化出來的網路 | **182.0** | 29.5/30s（90% 活著） | 3.9 | 16.4 | 8548 | 10.8% | 6.5 |
| 手寫的 `bot_control` | 176.6 | 30.0/30s（100% 活著） | 3.9 | 13.4 | 8384 | 12.3% | 0.9 |

網路贏在擊破數、分數與擦彈——它自己學到「貼著子彈擦過去換分數」這件事；
手寫 bot 比較保守（每局都活滿、擦彈很少）。單局示範：`--simulate 40` 的那一局
打到第 5 波（第一隻頭目）還剩三條命，分數 14520、擊破 22、連續 4 波無傷。

## 看它玩（`shooter_play.py`）

```bash
python3 shooter_play.py                # 開視窗看 AI 打（結束後 3 秒自動開下一局）
python3 shooter_play.py -d normal      # 換難度
python3 shooter_play.py --once         # 打完一局就停
python3 shooter_play.py --simulate 60  # 無視窗，印出過程與結果
python3 shooter_play.py --sound        # 加音效
```

AI 在駕駛時方向鍵、Shift、Space、X 都交給它，但 `P` 暫停、`R` 重來、`1`/`2`/`3` 換難度、
`Esc` 離開照常可用。

## 音效（`shooter_audio.py`）

12 個音效全部用標準庫 `wave` + 三角函式從零合成（射擊、命中、爆炸、受傷、護盾、
撿寶、換波、頭目、雷射、炸彈、無傷清波、結束），每個都寫成 16-bit 單聲道 WAV。
播放後端按速度自動挑：

1. `pygame.mixer` —— 真正的混音器，可重疊發聲、每次呼叫約 30 µs
2. `AppKit.NSSound` —— macOS 內建的 PyObjC，每次約 0.4 ms
3. 命令列播放器（`afplay`／`paplay`／`aplay`／`ffplay`／`play`）—— 不需任何套件，
   但每個音效要開一個行程，而 `afplay` 啟動要約 0.8 秒，所以聲音會遲到
4. 都沒有 —— 就只在頭目／炸彈／死亡／無傷清波時響系統鈴

```bash
python3 shooter_audio.py                    # 這台機器會用哪個後端、有哪些音效
python3 shooter_audio.py --play boom        # 聽一個
python3 shooter_audio.py --backend command --play over   # 強制用某個後端
python3 shooter_audio.py --write /tmp/sfx   # 把 wav 全部倒出來
python3 shooter_audio.py --selftest         # 合成與混音的 74 項檢查
python3 shooter_play.py --sound --volume 0.4
```

第一次開啟音效時要花約 0.3–1 秒把 12 個音效算出來，之後會快取在系統暫存資料夾
（`barrage-sfx-v1/`），冷啟動之後再開就是幾十毫秒。音效會依事件做速率限制
（例如射擊 0.11 秒內只響一次），子彈再多也不會變成噪音。

## 玩法與數值

- **自動射擊**：火力 1 是單發、2 是並排兩發、3 加一發高傷害中央彈、4 是三向、
  5 是五向扇形；被撞到掉一級。每清完一波會在中央掉一顆火力強化。
- **擦彈（graze）**：敵彈從 22 px 內擦過就算，得 12×連段分，並讓衝刺冷卻少 0.06 秒；
  同一顆子彈只算一次。判定半徑只有 4.6 px，外觀大得多，這是彈幕遊戲的禮貌。
- **連段**：1.6 秒內再擊破就 +1，最高 ×8，分數 = 敵人分數 ×連段；被撞到歸零；
  達成 ×4／×6／×8 時會在機身旁邊喊出來。
- **炸彈**：開局 2 顆、上限 5 顆；清掉每顆敵彈得 8 分，擊破頭目 +1 顆，
  每 3 波再 +1 顆。
- **無傷清波**：整波沒被打到就額外給 400×波數，並記一次 perfect。
- **區域**：每 5 波換一區（深空 → 星雲 → 熔核 → 極地），星空顏色、背景底色、
  面板強調色與標題卡都會跟著換。

### 敵人

| 種類 | 行為 |
| --- | --- |
| `drone` | 左右擺盪，瞄準單發 |
| `weaver` | 大幅蛇行；第 6 波起三向散射與**牆壁反彈彈**交替 |
| `hunter` | 追著你的橫向位置，丟高速針彈 |
| `turret` | 下降到定點後轉圈連發環形彈 |
| `splitter` | 第 5 波起出現；死亡時**裂成 3 顆 mote** |
| `mote` | 沒有槍，只會追著你撞過來 |
| `sniper` | 第 6 波起出現；先畫出**虛線預告線**，0.9 秒後射出貫穿光束 |
| `boss` | 每 5 波一隻，四種彈幕輪替：旋轉環、瞄準散彈、雙臂螺旋、**掃射光束** |

敵彈有四種：`orb`（紅，慢）、`needle`（黃，快而細）、`seeker`（綠，會轉向追你，
4 秒後消失）、`bouncer`（藍，撞左右牆反彈，反彈次數用完就消失）。

### 難度

`DIFFICULTIES`：簡單／普通／困難 = 敵彈速度 ×0.80／1.00／1.25、開火間隔
×1.30／1.00／0.78、敵人血量 ×0.85／1.00／1.25、每波數量 ×0.80／1.00／1.25。
光束的預告時間固定 0.9 秒（不隨難度變長，否則困難反而更好躲）。

## 特色

- 固定時間步：`step()` 就是 1/60 秒，速度單位是 px/秒，所以邏輯與畫面更新率無關
- **Canvas 物件池**：Tk 每格重建數百個圓形的成本很高（實測 450 個物件
  delete-and-recreate 約 4.2 ms/格，重複使用隱藏物件約 1.4 ms/格），
  所以子彈、粒子、星空都放在 `Pool` 裡依 (形狀, 顏色, 半徑) 分桶重複使用；
  一般波次約 2–3 ms/格。收尾時會掃過**每一個桶**而不只是這格用到的桶 ——
  否則歸零的桶會把上一格的子彈和塵埃永遠留在畫面上（已修，並有回歸測試）
- 主觀手感：衝刺無敵、受傷閃爍、擊破爆炸粒子、螢幕震動、漂浮的 `+分數` 與
  `COMBO x4`、三層視差星空、推進器尾焰、頭目標題卡與血條、四區配色
- 統計與紀錄：命中率、無傷清波、最高連段、受傷次數、用掉幾顆炸彈，
  以及存在 `shooter/highscore.json` 的**前五名排行榜**（分數／波數／擊破／命中率／難度，
  關窗或按 Esc 中離也會記錄；`--no-highscore` 可關）
- 內建 bot 會用「未來 0.11 / 0.24 秒的彈道位置」做排斥力場閃避、避開敵人船體、
  **垂直離開光束線**、對齊最靠近的敵人、適時衝刺，並在被 5 顆以上子彈貼身時丟炸彈

## 結構

`shooter.py`

- `ShooterGame` — 純邏輯與規則：`start`、`step`、`drop_bomb`、`_step_player`、
  `_fire_player`、`_wave_plan`、`_start_wave`、`spawn_enemy`、`_move_enemy`、
  `_move_boss`、`_fire_aimed`／`_fire_ring`、`_aim_laser`／`_ray_hits`／`_step_lasers`、
  `_step_bullets`（導引與反彈）、`_collide`、`hurt_player`、`kill_enemy`、`collect`、
  `record_run`、`_load_scores`／`_save_scores`、`ascii_frame`、`hud_text`
- `ShooterGUI` — Tkinter 介面。按鍵用「延遲 50 ms 才移除按鍵」避開 macOS 自動重複的
  KeyPress/KeyRelease 交錯；`_draw` 分成星空／物件池層／光束／敵人／強化／玩家／HUD／
  覆蓋層；換區時清空物件池並重畫底色；`close()` 會取消待執行的 `after` 迴圈
  （否則關窗後那一次回呼會對著已摧毀的 canvas 丟 `TclError`）
- `Pool` — Canvas 物件池（`take` / `end_frame` / `clear`）
- `bot_control` / `run_simulate` — 無視窗對打，供 `--simulate` 與測試使用

`shooter_train.py`

- `observe(game)` —— 把遊戲狀態壓成 69 個數字（只用玩家看得到的資訊）
- `Brain` —— 一層 tanh 隱藏層；`act()` 回傳四個控制、`load()` 讀 `.npz`
- `fitness_of` / `episode` / `evaluate` —— 獎勵與一局的評估（多行程可平行）
- `train` —— 精英保留 + 變異，每代存檔；`report` / `report_baseline` —— 測試與比較

`shooter_play.py`

- `AIBarrage(ShooterGUI)` —— 覆寫 `_direction()` 讓網路駕駛，並在遊戲結束後自動開下一局
- `run_headless` —— `--simulate` 用的無視窗版本

`shooter_audio.py`

- `render(name)` —— 每個音效的合成配方（`_tone` 掃頻、`_noise` 濾波雜訊、`_sequence` 琶音）
- `write_wav` / `master_files` —— 寫檔與快取
- `PygameBackend` / `NSSoundBackend` / `CommandBackend` / `NullBackend` —— 播放後端，
  以及負責速率限制與事件對應的 `SoundBank`

`--selftest` 的 179 項檢查涵蓋：開場與 `start()`、自動射擊與五段火力扇形、鍵盤／滑鼠／
集中／衝刺（無敵與冷卻）、命中、護盾、掉火力、撞船體、死亡與凍結、擦彈只算一次、
四種強化與上限、波次計畫與清波獎勵、無傷清波、頭目生成／進場／四種彈幕／血量與擊殺、
三種難度的數值關係、子彈剔除與各種上限、連段與歸零、暫停與重來、最高分與前五名排行榜
（含舊格式存檔）、物件池的隱藏／重用／歸零（幽靈物件的回歸測試）、七種敵人的行為
（分裂、追撞、光束預告與命中判定、導引彈轉向與到期、反彈彈碰牆與次數）、炸彈的
清彈／計分／傷害／充能與上限、四區輪替與提示、統計數字，以及一段 40 秒的隨機亂玩
（每一格檢查邊界、各種上限、計數範圍與 NaN）；最後再跑 bot 在簡單難度活滿 60 秒、
以及 ASCII 畫面的格式，並確認遊戲送出的每個事件都有對應音效（或刻意靜音）。

`shooter_train.py --selftest`（24 項）檢查狀態向量的形狀與範圍、網路的輸出範圍與
決定性、獎勵的單調性（活更久／擊破／清波／分數都要加分）、存讀檔一致。
`shooter_audio.py --selftest`（74 項）檢查 12 個音效的長度、振幅、頭尾淡入淡出、
WAV 標頭與量化後仍聽得見、四個後端的選擇邏輯、速率限制、以及關閉後清掉暫存檔。
