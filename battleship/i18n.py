#!/usr/bin/env python3
"""Bilingual UI strings (Traditional Chinese / English) — pure stdlib.

Usage
    from i18n import t, set_lang, get_lang, language_name, ship_label
    set_lang("en")
    t("battle.your_turn")                    -> "Your turn — fire!"
    t("battle.sunk", ship=t("ship.destroyer"))

Missing keys fall back to English, then to the key itself, so the UI never
crashes on a typo.  To add a string, add the same key to both dicts.
"""

from __future__ import annotations

import json
import os

LANGUAGES = {"zh": "繁體中文", "en": "English"}
DEFAULT_LANG = "zh"
LANG_ORDER = ("zh", "en")

STRINGS: dict[str, dict[str, str]] = {
    # -- app / menu ------------------------------------------------------- #
    "app.title": {"zh": "海戰棋", "en": "Battleship"},
    "app.subtitle": {"zh": "區域網路對戰", "en": "LAN multiplayer"},
    "app.tagline": {
        "zh": "經典 10×10 海戰棋 · 艦隊 5/4/3/3/2",
        "en": "Classic 10×10 · fleet 5/4/3/3/2",
    },
    "menu.host": {"zh": "建立房間", "en": "Host a room"},
    "menu.join": {"zh": "加入房間", "en": "Join a room"},
    "menu.ai": {"zh": "單機練習", "en": "Play vs AI"},
    "menu.help": {"zh": "玩法說明", "en": "How to play"},
    "menu.quit": {"zh": "離開", "en": "Quit"},
    "menu.host_desc": {
        "zh": "開一個房間，把 4 位房間碼給同網路的對手",
        "en": "Open a room and share the 4-digit code with a player on your network",
    },
    "menu.join_desc": {
        "zh": "輸入房間碼加入，或手動輸入主機 IP",
        "en": "Enter a room code, or type the host IP manually",
    },
    "menu.ai_desc": {
        "zh": "跟電腦對戰，練習擺船與砲擊",
        "en": "Practice your placement and gunnery against the computer",
    },
    "menu.language": {"zh": "語言", "en": "Language"},
    "menu.net_hint": {
        "zh": "同一個 Wi-Fi 或區域網路",
        "en": "Same Wi-Fi or local network",
    },
    # -- common ----------------------------------------------------------- #
    "common.name": {"zh": "暱稱", "en": "Nickname"},
    "common.your_name": {"zh": "你的暱稱", "en": "Your nickname"},
    "common.room_code": {"zh": "房間碼", "en": "Room code"},
    "common.ip": {"zh": "主機 IP", "en": "Host IP"},
    "common.port": {"zh": "連接埠", "en": "Port"},
    "common.cancel": {"zh": "取消", "en": "Cancel"},
    "common.back": {"zh": "返回", "en": "Back"},
    "common.ready": {"zh": "準備", "en": "Ready"},
    "common.not_ready": {"zh": "尚未準備", "en": "Not ready"},
    "common.waiting": {"zh": "等待中…", "en": "Waiting…"},
    "common.connecting": {"zh": "連線中…", "en": "Connecting…"},
    "common.connected": {"zh": "已連線", "en": "Connected"},
    "common.disconnected": {"zh": "已斷線", "en": "Disconnected"},
    "common.you": {"zh": "你", "en": "You"},
    "common.opponent": {"zh": "對手", "en": "Opponent"},
    "common.send": {"zh": "送出", "en": "Send"},
    "common.copy": {"zh": "複製", "en": "Copy"},
    "common.copied": {"zh": "已複製", "en": "Copied"},
    "common.close": {"zh": "關閉", "en": "Close"},
    "common.ok": {"zh": "好", "en": "OK"},
    "common.retry": {"zh": "重試", "en": "Retry"},
    "common.leave": {"zh": "離開房間", "en": "Leave room"},
    "common.players": {"zh": "玩家", "en": "Players"},
    "common.wins": {"zh": "勝場", "en": "Wins"},
    "common.vs": {"zh": "對", "en": "vs"},
    "common.quit_confirm": {"zh": "確定要離開遊戲嗎？", "en": "Leave the game?"},
    # -- host / join ------------------------------------------------------- #
    "host.title": {"zh": "等待對手加入", "en": "Waiting for an opponent"},
    "host.code_hint": {
        "zh": "請對手在「加入房間」輸入這組房間碼",
        "en": 'Ask your opponent to enter this code under "Join a room"',
    },
    "host.same_network": {
        "zh": "兩台裝置必須在同一個區域網路",
        "en": "Both devices must be on the same LAN",
    },
    "host.local_ip": {"zh": "本機 IP", "en": "This machine"},
    "host.manual_hint": {
        "zh": "如果搜尋不到，請對手手動輸入這個 IP",
        "en": "If discovery fails, your opponent can enter this IP manually",
    },
    "join.title": {"zh": "加入房間", "en": "Join a room"},
    "join.code_label": {"zh": "房間碼", "en": "Room code"},
    "join.code_placeholder": {"zh": "4 位數字", "en": "4 digits"},
    "join.searching": {"zh": "正在搜尋房間…", "en": "Searching for rooms…"},
    "join.search": {"zh": "搜尋房間", "en": "Search rooms"},
    "join.found": {"zh": "找到房間", "en": "Room found"},
    "join.not_found": {
        "zh": "找不到房間，請確認在同一個網路，或改用 IP 連線",
        "en": "No room found — check you are on the same network, or use the IP option",
    },
    "join.manual": {"zh": "改用手動 IP", "en": "Use IP instead"},
    "join.ip_placeholder": {"zh": "例如 192.168.1.20", "en": "e.g. 192.168.1.20"},
    "join.button": {"zh": "加入", "en": "Join"},
    "join.need_code": {"zh": "請輸入 4 位房間碼", "en": "Enter a 4-digit room code"},
    "join.bad_ip": {"zh": "IP 格式不正確", "en": "Invalid IP address"},
    "join.connecting_host": {
        "zh": "正在連線到 %(host)s:%(port)s…",
        "en": "Connecting to %(host)s:%(port)s…",
    },
    # -- lobby ------------------------------------------------------------- #
    "lobby.title": {"zh": "大廳", "en": "Lobby"},
    "lobby.waiting_player": {
        "zh": "等待另一位玩家加入…",
        "en": "Waiting for another player…",
    },
    "lobby.both_here": {"zh": "雙方都到齊了", "en": "Both players are here"},
    "lobby.waiting_ready": {
        "zh": "等待對手擺好艦隊…",
        "en": "Waiting for your opponent to finish placing…",
    },
    "lobby.you_ready": {"zh": "你已準備", "en": "You are ready"},
    "lobby.begin_hint": {
        "zh": "雙方準備後自動開戰",
        "en": "The battle starts once both fleets are ready",
    },
    # -- placement --------------------------------------------------------- #
    "placement.title": {"zh": "部署艦隊", "en": "Deploy your fleet"},
    "placement.subtitle": {
        "zh": "把 5 艘船放到你的 10×10 海域",
        "en": "Place your 5 ships on your 10×10 grid",
    },
    "placement.your_waters": {"zh": "我方海域", "en": "Your waters"},
    "placement.drag_hint": {
        "zh": "拖曳船艦放置 · 按 R 或右鍵旋轉",
        "en": "Drag a ship to place it · press R or right-click to rotate",
    },
    "placement.random": {"zh": "隨機排列", "en": "Random"},
    "placement.clear": {"zh": "清空", "en": "Clear"},
    "placement.ready_btn": {"zh": "準備完成", "en": "Ready"},
    "placement.waiting": {"zh": "等待對手…", "en": "Waiting for opponent…"},
    "placement.opponent_ready": {"zh": "對手已準備好", "en": "Opponent is ready"},
    "placement.remaining": {"zh": "尚未放置的船", "en": "Ships left to place"},
    "placement.all_placed": {"zh": "所有船艦已就位", "en": "All ships placed"},
    "placement.confirm_ready": {
        "zh": "確定要這樣嗎？",
        "en": "Ready with this layout?",
    },
    "placement.rotate_hint": {"zh": "R 旋轉", "en": "R to rotate"},
    "placement.invalid": {"zh": "這個位置放不下", "en": "Can't place the ship there"},
    "placement.turn_first": {"zh": "你先攻", "en": "You fire first"},
    "placement.turn_second": {"zh": "對手先攻", "en": "Opponent fires first"},
    "placement.ready_status": {
        "zh": "你：%(you)s · 對手：%(them)s",
        "en": "You: %(you)s · Opponent: %(them)s",
    },
    # -- battle ------------------------------------------------------------ #
    "battle.title": {"zh": "戰鬥中", "en": "Battle"},
    "battle.your_waters": {"zh": "我方海域", "en": "Your waters"},
    "battle.enemy_waters": {"zh": "敵方海域", "en": "Enemy waters"},
    "battle.your_turn": {"zh": "輪到你開火", "en": "Your turn — fire!"},
    "battle.enemy_turn": {"zh": "對手正在瞄準…", "en": "Opponent is aiming…"},
    "battle.click_hint": {
        "zh": "點擊敵方海域的格子開火",
        "en": "Click a cell in enemy waters to fire",
    },
    "battle.hit": {"zh": "命中！", "en": "Hit!"},
    "battle.miss": {"zh": "沒中", "en": "Miss"},
    "battle.sunk": {"zh": "擊沉 %(ship)s！", "en": "You sank the %(ship)s!"},
    "battle.sunk_by_enemy": {
        "zh": "你的 %(ship)s 被擊沉",
        "en": "Your %(ship)s was sunk",
    },
    "battle.hit_again": {
        "zh": "命中，可以再開火一次",
        "en": "Hit — fire again",
    },
    "battle.remaining_enemy": {
        "zh": "敵方剩餘船格 %(n)d",
        "en": "Enemy ship cells left: %(n)d",
    },
    "battle.turn_banner": {"zh": "第 %(n)d 回合", "en": "Round %(n)d"},
    "battle.log_title": {"zh": "戰報", "en": "Battle log"},
    "battle.chat_title": {"zh": "聊天", "en": "Chat"},
    "battle.chat_placeholder": {"zh": "輸入訊息…", "en": "Type a message…"},
    "battle.waiting_start": {
        "zh": "等待戰鬥開始…",
        "en": "Waiting for the battle to start…",
    },
    "battle.you_are": {"zh": "你是 %(name)s", "en": "You are %(name)s"},
    # -- log / chat -------------------------------------------------------- #
    "log.start": {
        "zh": "戰鬥開始，%(name)s 先攻",
        "en": "Battle started — %(name)s fires first",
    },
    "log.hit": {
        "zh": "%(name)s 砲擊 %(cell)s：命中",
        "en": "%(name)s fired at %(cell)s: hit",
    },
    "log.miss": {
        "zh": "%(name)s 砲擊 %(cell)s：未中",
        "en": "%(name)s fired at %(cell)s: miss",
    },
    "log.sunk": {
        "zh": "%(name)s 擊沉 %(ship)s",
        "en": "%(name)s sank the %(ship)s",
    },
    "log.over": {"zh": "%(name)s 獲得勝利", "en": "%(name)s wins"},
    "log.forfeit": {
        "zh": "%(name)s 離線，對手獲勝",
        "en": "%(name)s left — the opponent wins",
    },
    "log.join": {"zh": "%(name)s 加入房間", "en": "%(name)s joined"},
    "log.leave": {"zh": "%(name)s 離開房間", "en": "%(name)s left"},
    "log.rematch": {"zh": "再戰一局", "en": "Rematch"},
    "chat.system": {"zh": "系統", "en": "System"},
    "chat.joined": {"zh": "加入", "en": "joined"},
    "chat.left": {"zh": "離開", "en": "left"},
    # -- game over --------------------------------------------------------- #
    "over.win": {"zh": "勝利！", "en": "Victory!"},
    "over.lose": {"zh": "戰敗", "en": "Defeat"},
    "over.forfeit_win": {
        "zh": "對手離線，你獲勝",
        "en": "Opponent left — you win",
    },
    "over.forfeit_lose": {"zh": "你離線了", "en": "You left the match"},
    "over.stats": {
        "zh": "你發射 %(shots)d 發，命中 %(hits)d 發",
        "en": "%(shots)d shots, %(hits)d hits",
    },
    "over.rematch_btn": {"zh": "再戰一局", "en": "Rematch"},
    "over.rematch_wait": {
        "zh": "等待對手同意再戰…",
        "en": "Waiting for your opponent to accept…",
    },
    "over.rematch_ready": {
        "zh": "對手想再戰",
        "en": "Opponent wants a rematch",
    },
    "over.new_game": {"zh": "回到主選單", "en": "Main menu"},
    "over.wins_record": {
        "zh": "目前戰績 %(you)d : %(them)d",
        "en": "Score %(you)d : %(them)d",
    },
    # -- help -------------------------------------------------------------- #
    "help.title": {"zh": "玩法說明", "en": "How to play"},
    "help.place": {
        "zh": "開始前先部署艦隊：航空母艦 (5)、戰艦 (4)、巡洋艦 (3)、潛水艇 (3)、驅逐艦 (2) 放到 10×10 海域。船不能重疊，可以相鄰。",
        "en": "Before battle, deploy your fleet: carrier (5), battleship (4), cruiser (3), submarine (3), destroyer (2) on a 10×10 grid. Ships may touch but not overlap.",
    },
    "help.fire": {
        "zh": "雙方輪流開火，點擊敵方海域的格子。",
        "en": "Players take turns firing by clicking a cell in enemy waters.",
    },
    "help.extra": {
        "zh": "命中可以再開火一次；沒中就換對手。",
        "en": "A hit lets you fire again; a miss passes the turn.",
    },
    "help.win": {
        "zh": "先把對方 5 艘船全部擊沉的人獲勝。",
        "en": "The first player to sink all 5 enemy ships wins.",
    },
    "help.net": {
        "zh": "區域網路對戰：一台電腦按「建立房間」，另一台輸入 4 位房間碼加入（需同一個 Wi-Fi）。",
        "en": "LAN play: one player hosts a room; the other joins with the 4-digit code (same Wi-Fi).",
    },
    "help.keys": {
        "zh": "快捷鍵：R 旋轉船艦 · Esc 返回 · Enter 送出聊天",
        "en": "Shortcuts: R rotate, Esc back, Enter send chat",
    },
    # -- ships ------------------------------------------------------------- #
    "ship.carrier": {"zh": "航空母艦", "en": "Carrier"},
    "ship.battleship": {"zh": "戰艦", "en": "Battleship"},
    "ship.cruiser": {"zh": "巡洋艦", "en": "Cruiser"},
    "ship.submarine": {"zh": "潛水艇", "en": "Submarine"},
    "ship.destroyer": {"zh": "驅逐艦", "en": "Destroyer"},
    # -- AI ---------------------------------------------------------------- #
    "ai.title": {"zh": "單機練習", "en": "Practice vs AI"},
    "ai.difficulty": {"zh": "難度", "en": "Difficulty"},
    "ai.easy": {"zh": "簡單", "en": "Easy"},
    "ai.normal": {"zh": "普通", "en": "Normal"},
    "ai.hard": {"zh": "困難", "en": "Hard"},
    "ai.start": {"zh": "開始", "en": "Start"},
    "ai.thinking": {"zh": "電腦思考中…", "en": "AI is thinking…"},
    # -- network / discovery ----------------------------------------------- #
    "net.discovery": {
        "zh": "正在搜尋同網路的房間…",
        "en": "Searching your network…",
    },
    "net.discovery_none": {"zh": "沒找到房間", "en": "No rooms found"},
    "sys.opponent_joined": {"zh": "對手已加入", "en": "Opponent joined"},
    "sys.opponent_left": {"zh": "對手已離開", "en": "Opponent left"},
    "sys.waiting_rematch": {"zh": "等待對手…", "en": "Waiting for opponent…"},
    "sys.rematch_start": {"zh": "新的一局開始", "en": "A new game begins"},
    "sys.host_left": {
        "zh": "房主已離開，房間關閉",
        "en": "Host left — room closed",
    },
    # -- errors ------------------------------------------------------------ #
    "err.title": {"zh": "發生錯誤", "en": "Something went wrong"},
    "err.connection_lost": {
        "zh": "與伺服器的連線中斷",
        "en": "Lost connection to the server",
    },
    "err.connect_failed": {
        "zh": "無法連線到房間",
        "en": "Could not connect to the room",
    },
    "err.room_not_found": {"zh": "找不到這個房間", "en": "Room not found"},
    "err.room_full": {"zh": "房間已滿", "en": "Room is full"},
    "err.already_started": {"zh": "遊戲已經開始", "en": "The game already started"},
    "err.not_your_turn": {"zh": "還沒輪到你", "en": "Not your turn"},
    "err.already_shot": {
        "zh": "這個格子已經打過了",
        "en": "That cell was already fired at",
    },
    "err.out_of_bounds": {"zh": "超出棋盤", "en": "Outside the board"},
    "err.bad_placement": {
        "zh": "艦隊擺放不合法",
        "en": "Invalid fleet placement",
    },
    "err.wrong_fleet": {
        "zh": "艦隊船數不正確",
        "en": "Wrong fleet composition",
    },
    "err.overlap": {"zh": "船艦重疊了", "en": "Ships overlap"},
    "err.canceled": {"zh": "已取消", "en": "Canceled"},
    "err.timeout": {"zh": "連線逾時", "en": "Connection timed out"},
    "err.protocol": {"zh": "通訊協定錯誤", "en": "Protocol error"},
    "err.disconnected": {"zh": "對手已離線", "en": "Opponent disconnected"},
    "err.name_too_long": {"zh": "暱稱太長", "en": "Nickname is too long"},
    "err.bad_code": {"zh": "房間碼格式不正確", "en": "Invalid room code"},
    "err.server_error": {"zh": "伺服器錯誤", "en": "Server error"},
    "err.room_closed": {"zh": "房間已關閉", "en": "Room is closed"},
    "err.no_players": {"zh": "房間裡沒有人", "en": "No players in the room"},
    "err.bad_message": {"zh": "訊息格式錯誤", "en": "Malformed message"},
    # -- appended by ui.py (UI-only strings) -------------------------------- #
    "ai.label": {"zh": "電腦", "en": "Computer"},
    "battle.chat_empty": {"zh": "還沒有聊天訊息", "en": "No chat yet"},
    "battle.fleet_title": {"zh": "我方艦隊", "en": "Your fleet"},
    "battle.log_empty": {
        "zh": "戰鬥紀錄會出現在這裡",
        "en": "Battle events will appear here",
    },
    "join.need_port": {
        "zh": "請輸入連接埠 (1-65535)",
        "en": "Enter a port number (1-65535)",
    },
    "join.no_rooms": {
        "zh": "沒有找到房間 — 請確認在同一個 Wi-Fi 網路",
        "en": "No rooms found — make sure you are on the same Wi-Fi",
    },
    "join.room_meta": {"zh": "%(host)s:%(port)s · %(names)s",
                       "en": "%(host)s:%(port)s · %(names)s"},
    "join.rooms_title": {"zh": "找到的房間", "en": "Discovered rooms"},
    "placement.need_all": {
        "zh": "請先擺放全部 5 艘船",
        "en": "Place all 5 ships first",
    },
    "room.state.lobby": {"zh": "等待玩家", "en": "Waiting"},
    "room.state.placing": {"zh": "擺放中", "en": "Placing"},
    "room.state.battle": {"zh": "戰鬥中", "en": "Battle"},
    "room.state.over": {"zh": "已結束", "en": "Finished"},
    "host.manual_only": {
        "zh": "自動探索無法啟動 — 請手動輸入 IP 與連接埠加入",
        "en": "Auto-discovery is unavailable — join by entering the IP and port",
    },
    # -- learned AI + watch mode ------------------------------------------- #
    "ai.learned": {"zh": "學習型", "en": "Learned"},
    "ai.no_model": {
        "zh": "找不到學習型 AI 的模型 — 請先在這個資料夾執行 python3 ai_train.py",
        "en": "Learned AI model not found — run python3 ai_train.py in this folder first",
    },
    "ai.no_numpy": {
        "zh": "學習型 AI 需要 numpy — 請執行 pip3 install numpy",
        "en": "The learned AI needs numpy — run pip3 install numpy",
    },
    "menu.watch": {"zh": "觀戰 AI", "en": "Watch AI"},
    "watch.title": {"zh": "觀戰 AI 對戰", "en": "Watch AI play"},
    "watch.desc": {
        "zh": "選兩個電腦對打。學習型 對 學習型 = AI 對 AI；學習型 對 簡單/普通/困難 = AI 對機器人。",
        "en": "Pick two computer players. Learned vs Learned = AI vs AI; "
              "Learned vs Easy/Normal/Hard = AI vs bot.",
    },
    "watch.left": {"zh": "左方玩家 A", "en": "Left player A"},
    "watch.right": {"zh": "右方玩家 B", "en": "Right player B"},
    "watch.speed": {"zh": "速度", "en": "Speed"},
    "watch.slow": {"zh": "慢", "en": "Slow"},
    "watch.normal": {"zh": "中", "en": "Normal"},
    "watch.fast": {"zh": "快", "en": "Fast"},
    "watch.turbo": {"zh": "極速", "en": "Turbo"},
    "watch.start": {"zh": "開始觀戰", "en": "Start watching"},
    "watch.pause": {"zh": "暫停", "en": "Pause"},
    "watch.resume": {"zh": "繼續", "en": "Resume"},
    "watch.turn": {"zh": "%(name)s 開火中", "en": "%(name)s is firing"},
    "watch.winner": {"zh": "%(name)s 獲勝", "en": "%(name)s wins"},
    "watch.next": {"zh": "下一局", "en": "Next game"},
    "watch.waters": {"zh": "%(name)s 的海域", "en": "%(name)s's waters"},
    "watch.fleet_title": {"zh": "雙方艦隊 (A | B)", "en": "Both fleets (A | B)"},
    "watch.remaining": {
        "zh": "剩餘船格  A %(a)d · B %(b)d",
        "en": "Cells left  A %(a)d · B %(b)d",
    },
    "watch.stats": {
        "zh": "發射數  A %(a)d · B %(b)d",
        "en": "Shots fired  A %(a)d · B %(b)d",
    },
    "watch.score": {"zh": "勝場  A %(a)d : %(b)d B", "en": "Wins  A %(a)d : %(b)d B"},
    "watch.over_detail": {
        "zh": "A 發射 %(sa)d 發 · B 發射 %(sb)d 發",
        "en": "A fired %(sa)d · B fired %(sb)d",
    },
}

_lang = DEFAULT_LANG


# --------------------------------------------------------------------------- #
# Language state
# --------------------------------------------------------------------------- #

def set_lang(code: str | None) -> str:
    """Switch the active language; unknown codes fall back to the default."""
    global _lang
    if code in LANGUAGES:
        _lang = code
    return _lang


def get_lang() -> str:
    return _lang


def toggle_lang() -> str:
    return set_lang("en" if _lang == "zh" else "zh")


def language_name(code: str | None = None) -> str:
    return LANGUAGES.get(code or _lang, str(code))


def t(key: str, **kwargs) -> str:
    """Translate *key* into the active language and format placeholders."""
    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(_lang) or entry.get(DEFAULT_LANG) or entry.get("en") or key
    if kwargs:
        try:
            return text % kwargs
        except (KeyError, TypeError, ValueError):
            return text
    return text


def ship_name(name: str) -> str:
    return t(f"ship.{name}")


def ship_label(name: str) -> str:
    """e.g. '航空母艦 (5)' — used by the ship dock."""
    from engine import FLEET_BY_NAME

    return f"{ship_name(name)} ({FLEET_BY_NAME.get(name, '?')})"


def err_text(code: str | None) -> str:
    """Localised message for a protocol/rule error code."""
    if not code:
        return t("err.server_error")
    key = f"err.{code}"
    if key in STRINGS:
        return t(key)
    return str(code)


# --------------------------------------------------------------------------- #
# Persisted preference (optional)
# --------------------------------------------------------------------------- #

def prefs_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".battleship.json")


def load_saved_lang(path: str | None = None) -> str:
    try:
        with open(path or prefs_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return set_lang(data.get("lang", DEFAULT_LANG))
    except (OSError, ValueError, AttributeError):
        return _lang


def save_lang(path: str | None = None) -> None:
    try:
        with open(path or prefs_path(), "w", encoding="utf-8") as handle:
            json.dump({"lang": _lang}, handle)
    except OSError:
        pass
