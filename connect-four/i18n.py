#!/usr/bin/env python3
"""Bilingual UI strings (Traditional Chinese / English).

Usage::

    import i18n
    i18n.set_lang("en")
    i18n.t("menu.host")                 # -> "Host a Room"
    i18n.t("game.turn", name="Ann")     # -> "Ann's turn"

Missing keys fall back to English, then to the key itself, so an unfinished
translation never crashes the game.
"""

from __future__ import annotations

from typing import Dict, Optional

import settings

LANGS = ("zh", "en")
LANG_NAMES = {"zh": "中文", "en": "English"}

_STRINGS: Dict[str, Dict[str, str]] = {
    # ---------------------------------------------------------------- app --
    "app.title": {"zh": "四子棋", "en": "Connect Four"},
    "app.subtitle": {"zh": "區域網路對戰", "en": "two players, one Wi-Fi"},
    "app.tagline": {
        "zh": "和同一個 Wi-Fi 的朋友連線對戰，或單挑 AI。",
        "en": "Play with a friend on the same Wi-Fi, or take on the AI.",
    },
    "app.lang_switch": {"zh": "English", "en": "中文"},
    # --------------------------------------------------------------- menu --
    "menu.host": {"zh": "建立房間", "en": "Host a Room"},
    "menu.host_hint": {"zh": "讓別人加入你的遊戲", "en": "Let others join your game"},
    "menu.join": {"zh": "加入房間", "en": "Join a Room"},
    "menu.join_hint": {"zh": "輸入位址或從清單挑選", "en": "Type an address or pick one below"},
    "menu.replays": {"zh": "棋譜回放", "en": "Replays"},
    "menu.replays_hint": {"zh": "重看之前存下的對局", "en": "Rewatch games saved on this device"},
    "menu.watch": {"zh": "AI 對戰 / 觀戰", "en": "Play & Watch AI"},
    "menu.watch_hint": {"zh": "自己對 AI 或機器人，也能看 AI 對戰，不用建房間", "en": "Play the AI or bots, or watch them — no room needed"},
    "menu.quit": {"zh": "離開", "en": "Quit"},
    "menu.rooms": {"zh": "區域網路上的房間", "en": "Rooms on your network"},
    "menu.scanning": {"zh": "正在搜尋區域網路…", "en": "Scanning your network…"},
    "menu.rescan": {"zh": "重新搜尋", "en": "Scan again"},
    "menu.no_rooms": {
        "zh": "沒找到房間。請確認雙方在同一個 Wi-Fi，或手動輸入位址。",
        "en": "No rooms found. Check that you are on the same Wi-Fi, or type an address.",
    },
    "menu.ai_badge": {"zh": "AI", "en": "AI"},
    # --------------------------------------------------------------- join --
    "join.title": {"zh": "加入房間", "en": "Join a Room"},
    "join.host": {"zh": "主機位址", "en": "Host address"},
    "join.host_placeholder": {"zh": "例如 192.168.1.23 或 mymac.local", "en": "e.g. 192.168.1.23 or mymac.local"},
    "join.port": {"zh": "連接埠", "en": "Port"},
    "join.name": {"zh": "你的暱稱", "en": "Your name"},
    "join.name_placeholder": {"zh": "玩家", "en": "Player"},
    "join.role": {"zh": "加入身分", "en": "Join as"},
    "join.role_player": {"zh": "下棋", "en": "Play"},
    "join.role_spectator": {"zh": "觀戰", "en": "Watch"},
    "join.connect": {"zh": "連線", "en": "Connect"},
    "join.connecting": {"zh": "連線中…", "en": "Connecting…"},
    "join.failed": {"zh": "無法連線到 {where}", "en": "Could not reach {where}"},
    "join.default_name": {"zh": "玩家", "en": "Player"},
    # --------------------------------------------------------------- host --
    "host.title": {"zh": "房間已建立", "en": "Room Created"},
    "host.code": {"zh": "房間代碼", "en": "Room code"},
    "host.share": {"zh": "把這個位址告訴朋友", "en": "Share this address with your friend"},
    "host.your_ip": {"zh": "你的區域網路 IP", "en": "Your LAN IP"},
    "host.waiting": {"zh": "等待玩家加入…", "en": "Waiting for a player…"},
    "host.vs_ai": {"zh": "與 AI 對戰", "en": "Play the AI"},
    "host.you_are": {"zh": "你是 {seat}", "en": "You are {seat}"},
    "host.close_room": {"zh": "關閉房間", "en": "Close Room"},
    "host.spectators": {"zh": "觀戰者", "en": "Spectators"},
    "host.none": {"zh": "目前沒有觀戰者", "en": "No spectators yet"},
    # ----------------------------------------------------------- ai levels --
    "level.easy": {"zh": "簡單", "en": "Easy"},
    "level.normal": {"zh": "普通", "en": "Normal"},
    "level.hard": {"zh": "困難", "en": "Hard"},
    "level.learned": {"zh": "學習型", "en": "Learned"},
    "level.title": {"zh": "AI 難度", "en": "AI difficulty"},
    # --------------------------------------------------------------- game --
    "game.you": {"zh": "你", "en": "You"},
    "game.red": {"zh": "紅方", "en": "Red"},
    "game.yellow": {"zh": "黃方", "en": "Yellow"},
    "game.red_short": {"zh": "紅", "en": "Red"},
    "game.yellow_short": {"zh": "黃", "en": "Yel"},
    "game.turn": {"zh": "輪到 {name}", "en": "{name}'s turn"},
    "game.your_turn": {"zh": "你的回合 — 點一列放下棋子", "en": "Your turn — click a column"},
    "game.thinking": {"zh": "{name} 正在思考…", "en": "{name} is thinking…"},
    "game.spectating": {"zh": "觀戰中", "en": "Spectating"},
    "game.watch_hint": {"zh": "你正在觀戰，無法下棋", "en": "You are watching this game"},
    "game.waiting_player": {"zh": "等待對手加入…", "en": "Waiting for an opponent…"},
    "game.score": {"zh": "比分", "en": "Score"},
    "game.moves_count": {"zh": "第 {n} 手", "en": "Move {n}"},
    "game.wins": {"zh": "{name} 獲勝！", "en": "{name} wins!"},
    "game.draw": {"zh": "和局", "en": "Draw"},
    "game.win_line": {"zh": "四子連線", "en": "Four in a row"},
    "game.rematch": {"zh": "再來一局", "en": "Rematch"},
    "game.rematch_voted": {"zh": "已投票，等待對手…", "en": "Vote sent — waiting for the opponent…"},
    "game.rematch_both": {"zh": "雙方都同意，下一局即將開始", "en": "Both players agreed — starting next round"},
    "game.leave": {"zh": "離開房間", "en": "Leave Room"},
    "game.leave_confirm": {"zh": "確定要離開嗎？", "en": "Leave this room?"},
    "game.reconnecting": {"zh": "連線中斷，嘗試重新連線…", "en": "Connection lost — reconnecting…"},
    "game.disconnected": {"zh": "與主機的連線已中斷", "en": "Lost the connection to the host"},
    "game.connection_closed": {"zh": "主機已結束房間", "en": "The host closed the room"},
    "game.opponent_left": {"zh": "對手離開了，等待新玩家…", "en": "Opponent left — waiting for a new player…"},
    "game.you_are_red": {"zh": "你執紅子，先下", "en": "You play Red and move first"},
    "game.you_are_yellow": {"zh": "你執黃子，後下", "en": "You play Yellow and move second"},
    # --------------------------------------------------------------- chat --
    "chat.title": {"zh": "聊天", "en": "Chat"},
    "chat.placeholder": {"zh": "說點什麼…", "en": "Say something…"},
    "chat.send": {"zh": "送出", "en": "Send"},
    "chat.empty": {"zh": "還沒有訊息", "en": "No messages yet"},
    "chat.you": {"zh": "你", "en": "you"},
    "chat.system": {"zh": "系統", "en": "system"},
    "chat.join_player": {"zh": "{name} 加入（{seat}）", "en": "{name} joined as {seat}"},
    "chat.join_spectator": {"zh": "{name} 加入觀戰", "en": "{name} is now watching"},
    "chat.left": {"zh": "{name} 離開了", "en": "{name} left"},
    # ------------------------------------------- system chat (server codes) --
    "sys.new_game": {"zh": "新的一局開始 — 紅方先下", "en": "New game — red starts"},
    "sys.result": {"zh": "{name} 獲勝！", "en": "{name} wins!"},
    "sys.draw": {"zh": "和局", "en": "Draw"},
    "sys.ai_replaced": {"zh": "{name} 加入，取代了 AI。比分已重設。", "en": "{name} replaces the AI. Scores reset."},
    "sys.left": {"zh": "{name} 離開了", "en": "{name} left"},
    "sys.abandoned": {"zh": "對局中止 — 等待玩家", "en": "Match abandoned — waiting for players"},
    # ------------------------------------------------------------ records --
    "records.title": {"zh": "棋譜回放", "en": "Replays"},
    "records.empty": {"zh": "這裡還沒有棋譜。玩完一局就會自動存下來。", "en": "No replays yet — finish a game and it is saved here."},
    "records.empty_dir": {"zh": "找不到棋譜資料夾：{dir}", "en": "No replays folder at {dir}"},
    "records.play": {"zh": "播放", "en": "Play"},
    "records.pause": {"zh": "暫停", "en": "Pause"},
    "records.next": {"zh": "下一手", "en": "Next"},
    "records.prev": {"zh": "上一手", "en": "Prev"},
    "records.restart": {"zh": "重播", "en": "Restart"},
    "records.speed": {"zh": "速度", "en": "Speed"},
    "records.vs": {"zh": "{red} 對 {yellow}", "en": "{red} vs {yellow}"},
    "records.meta": {"zh": "{moves} 手 · {when}", "en": "{moves} moves · {when}"},
    "records.open_folder": {"zh": "開啟資料夾", "en": "Open folder"},
    "records.count": {"zh": "{n} 個棋譜", "en": "{n} replays"},
    "records.not_found": {"zh": "讀不到這個棋譜", "en": "Could not read that replay"},
    "records.saved": {"zh": "已存成棋譜 ✓", "en": "Saved to replays ✓"},
    "records.hint": {"zh": "點選左邊的棋譜，用下方按鈕重播", "en": "Pick a replay on the left, then use the buttons below"},
    # -------------------------------------------------------------- watch --
    "watch.title": {"zh": "AI 對戰 / 觀戰", "en": "Play & Watch AI"},
    "watch.mode_me_ai": {"zh": "我對 AI", "en": "Me vs AI"},
    "watch.mode_ai_ai": {"zh": "AI 對 AI", "en": "AI vs AI"},
    "watch.mode_ai_bot": {"zh": "AI 對機器人", "en": "AI vs Bot"},
    "watch.opponent": {"zh": "對手", "en": "Opponent"},
    "watch.side": {"zh": "我的顏色", "en": "My colour"},
    "watch.side_red": {"zh": "紅（先手）", "en": "Red (first)"},
    "watch.side_yellow": {"zh": "黃（後手）", "en": "Yellow (second)"},
    "watch.again": {"zh": "再來一局", "en": "Play again"},
    "watch.hint_click": {"zh": "點棋盤的一列放下棋子", "en": "Click a column to drop a disc"},
    "watch.bot_level": {"zh": "機器人難度", "en": "Bot level"},
    "watch.speed": {"zh": "速度", "en": "Speed"},
    "watch.pause": {"zh": "暫停", "en": "Pause"},
    "watch.resume": {"zh": "繼續", "en": "Resume"},
    "watch.next": {"zh": "下一局", "en": "Next game"},
    "watch.reset": {"zh": "歸零比分", "en": "Reset score"},
    "watch.score": {"zh": "累計比分", "en": "Running score"},
    "watch.games": {"zh": "第 {n} 局", "en": "Game {n}"},
    "watch.ai": {"zh": "學習型 AI", "en": "Learned AI"},
    "watch.ai_a": {"zh": "AI 甲", "en": "AI A"},
    "watch.ai_b": {"zh": "AI 乙", "en": "AI B"},
    "watch.bot": {"zh": "機器人 · {level}", "en": "Bot · {level}"},
    "watch.thinking": {"zh": "{name} 思考中…", "en": "{name} is thinking…"},
    "watch.won": {"zh": "{name} 獲勝", "en": "{name} won"},
    "watch.no_model": {
        "zh": "還沒有訓練好的模型（connect4_model.npz）— 先執行 python3 ai_train.py。現在學習型 AI 暫時用普通 AI 代替。",
        "en": "No trained model yet (connect4_model.npz) — run python3 ai_train.py. The learned AI plays like Normal until then.",
    },
    "watch.hint_me": {"zh": "想自己對 AI：建立房間 → 與 AI 對戰 → 選「學習型」", "en": "To play it yourself: Host a Room → Play the AI → pick Learned"},
    # ------------------------------------------------------------- common --
    "common.back": {"zh": "返回", "en": "Back"},
    "common.close": {"zh": "關閉", "en": "Close"},
    "common.cancel": {"zh": "取消", "en": "Cancel"},
    "common.ok": {"zh": "好", "en": "OK"},
    "common.copied": {"zh": "已複製到剪貼簿", "en": "Copied to clipboard"},
    "common.copy": {"zh": "複製", "en": "Copy"},
    "common.you": {"zh": "（你）", "en": " (you)"},
    "common.seat_suffix": {"zh": "（{seat}）", "en": " ({seat})"},
    # -------------------------------------------------------------- error --
    "err.spectator": {"zh": "觀戰者不能下棋", "en": "Spectators cannot make moves"},
    "err.not_playing": {"zh": "遊戲還沒開始", "en": "The game has not started"},
    "err.not_your_turn": {"zh": "還沒輪到你", "en": "It is not your turn"},
    "err.bad_move": {"zh": "這裡不能下", "en": "That column is not available"},
    "err.bad_column": {"zh": "這裡不能下", "en": "That column is not available"},
    "err.column_full": {"zh": "這一列已經滿了", "en": "That column is full"},
    "err.game_over": {"zh": "這一局已經結束", "en": "This game is already over"},
    "err.bad_message": {"zh": "無法辨識的訊息", "en": "Unrecognized message"},
    "err.ai_only_host": {"zh": "只有主機能設定 AI", "en": "Only the host can change the AI"},
    "err.seat_taken": {"zh": "座位已被佔用", "en": "That seat is taken"},
    "err.room_full": {"zh": "房間已滿", "en": "The room is full"},
    "err.version": {"zh": "版本不相容，請更新遊戲", "en": "Incompatible version — please update"},
    "err.timeout": {"zh": "連線逾時", "en": "Connection timed out"},
    "err.connection": {"zh": "連線失敗", "en": "Connection failed"},
    "err.host_closed": {"zh": "主機已關閉", "en": "The host is gone"},
    "err.generic": {"zh": "發生錯誤：{msg}", "en": "Something went wrong: {msg}"},
}

_lang = "zh"
_missing: set[str] = set()


def set_lang(lang: str) -> str:
    global _lang
    lang = (lang or "").strip().lower()
    if lang not in LANGS:
        lang = "zh"
    _lang = lang
    return _lang


def get_lang() -> str:
    return _lang


def toggle_lang() -> str:
    return set_lang("en" if _lang == "zh" else "zh")


def init_from_settings() -> str:
    return set_lang(settings.load().get("lang", "zh"))


def t(key: str, **fmt) -> str:
    entry = _STRINGS.get(key)
    if entry is None:
        if key not in _missing:
            _missing.add(key)
        return key
    text = entry.get(_lang) or entry.get("en") or key
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def seat_label(seat: Optional[int]) -> str:
    if seat == 1:
        return t("game.red")
    if seat == 2:
        return t("game.yellow")
    return ""


def seat_short(seat: Optional[int]) -> str:
    if seat == 1:
        return t("game.red_short")
    if seat == 2:
        return t("game.yellow_short")
    return ""


def level_label(level: str) -> str:
    return t("level." + level)


def error_text(code: str, msg: str = "") -> str:
    key = "err." + code
    if key in _STRINGS:
        return t(key)
    return t("err.generic", msg=msg or code)


def missing_keys() -> list:
    return sorted(_missing)
