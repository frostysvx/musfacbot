"""Мини-игра «Угадай интервал» для Telegram-бота.

Бот синтезирует два звука (снизу вверх), присылает аудио и кнопки с интервалами.
Кто первым нажмёт правильный вариант, получает очко. Рейтинг хранится в SQLite.
"""
import html
import io
import json
import math
import os
import random
import sqlite3
import struct
import threading
import wave

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN") or "ВСТАВЬ_НОВЫЙ_ТОКЕН"
# На Railway подключи Volume (например /data) и задай DB_PATH=/data/game.db,
# иначе рейтинг обнулится после редеплоя.
DB_PATH = os.environ.get("DB_PATH", "game.db")

# полутоны -> (короткое название, полное название)
INTERVALS = {
    1: ("м2", "малая секунда"),
    2: ("б2", "большая секунда"),
    3: ("м3", "малая терция"),
    4: ("б3", "большая терция"),
    5: ("ч4", "чистая кварта"),
    6: ("тритон", "тритон"),
    7: ("ч5", "чистая квинта"),
    8: ("м6", "малая секста"),
    9: ("б6", "большая секста"),
    10: ("м7", "малая септима"),
    11: ("б7", "большая септима"),
    12: ("ч8", "чистая октава"),
}

# chat_id -> текущий вопрос
QUESTIONS = {}
_db_lock = threading.Lock()


# ---------- Telegram ----------

def _tg(method, data=None, files=None):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=data, files=files, timeout=30,
    )
    if r.status_code != 200:
        print(f"{method}: {r.status_code} {r.text}", flush=True)
    return r


def _answer_cb(cb_id, text=""):
    _tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


# ---------- База (рейтинг) ----------

def db_init():
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS scores ("
            "chat_id INTEGER, user_id INTEGER, name TEXT, "
            "points INTEGER DEFAULT 0, PRIMARY KEY (chat_id, user_id))"
        )


def _add_point(chat_id, user_id, name):
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO scores (chat_id, user_id, name, points) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET points = points + 1, name = excluded.name",
            (chat_id, user_id, name),
        )


def format_top(chat_id):
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT name, points FROM scores WHERE chat_id = ? "
            "ORDER BY points DESC LIMIT 10",
            (chat_id,),
        ).fetchall()
    if not rows:
        return "Пока никто не играл. Жми /interval 🎵"
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Рейтинг интервалов</b>\n"]
    for i, (name, pts) in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {html.escape(name)} — {pts}")
    return "\n".join(lines)


# ---------- Звук ----------

def make_interval_wav(low_midi, semitones):
    rate = 22050

    def tone(midi, dur):
        f = 440 * 2 ** ((midi - 69) / 12)
        out = []
        for i in range(int(rate * dur)):
            t = i / rate
            env = math.exp(-2.5 * t) * min(1.0, t * 80)  # атака + затухание
            s = (math.sin(2 * math.pi * f * t)
                 + 0.4 * math.sin(2 * math.pi * 2 * f * t)
                 + 0.15 * math.sin(2 * math.pi * 3 * f * t)) * env
            out.append(s)
        return out

    samples = tone(low_midi, 1.0) + tone(low_midi + semitones, 1.6)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s / 1.6)) * 32000))
            for s in samples
        )
        w.writeframes(frames)
    return buf.getvalue()


# ---------- Игра ----------

def _keyboard(qid):
    buttons = [
        {"text": INTERVALS[s][0], "callback_data": f"iv:{qid}:{s}"}
        for s in sorted(INTERVALS)
    ]
    return {"inline_keyboard": [buttons[i:i + 3] for i in range(0, len(buttons), 3)]}


def send_interval_question(chat_id):
    semi = random.randint(1, 12)
    low = random.randint(52, 64)  # нижний звук: от E3 до E4
    qid = random.randint(1, 10 ** 6)
    QUESTIONS[chat_id] = {"qid": qid, "semi": semi, "solved": False, "wrong": set()}
    wav = make_interval_wav(low, semi)
    _tg(
        "sendAudio",
        data={
            "chat_id": chat_id,
            "title": "Какой интервал?",
            "caption": "🎵 Какой интервал? Кто первый угадает, получает очко",
            "reply_markup": json.dumps(_keyboard(qid)),
        },
        files={"audio": ("interval.wav", wav, "audio/wav")},
    )


def handle_callback(cb):
    """Вызывать из handle_update, если в апдейте есть callback_query."""
    data = cb.get("data", "")
    if not data.startswith("iv:"):
        return False
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    user = cb["from"]
    name = user.get("first_name") or "Аноним"

    if data == "iv:next":
        _answer_cb(cb["id"])
        send_interval_question(chat_id)
        return True

    _, qid, semi = data.split(":")
    q = QUESTIONS.get(chat_id)
    if not q or q["qid"] != int(qid):
        _answer_cb(cb["id"], "Этот вопрос уже неактуален")
        return True
    if q["solved"]:
        _answer_cb(cb["id"], "Уже угадали 🙂")
        return True
    if user["id"] in q["wrong"]:
        _answer_cb(cb["id"], "Ты уже пробовал(а) в этом вопросе")
        return True

    if int(semi) == q["semi"]:
        q["solved"] = True
        _add_point(chat_id, user["id"], name)
        _answer_cb(cb["id"], "Верно! 🎉")
        _tg("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps(
                {"inline_keyboard": [[{"text": "🎲 Ещё", "callback_data": "iv:next"}]]}
            ),
        })
        short, full = INTERVALS[q["semi"]]
        _tg("sendMessage", {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text": f"✅ {html.escape(name)} угадал(а): <b>{short}</b> ({full}), +1",
        })
    else:
        q["wrong"].add(user["id"])
        _answer_cb(cb["id"], "Не то 😅 Вторая попытка в этом вопросе недоступна")
    return True
