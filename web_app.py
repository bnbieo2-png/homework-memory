#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import sqlite3
import smtplib
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.message import EmailMessage
from fractions import Fraction
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from learning_enrichment import enrich_card, ensure_learning_schema


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HOMEWORK_NOTEBOOK_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
DB_PATH = Path(os.environ.get("HOMEWORK_NOTEBOOK_DB_PATH", str(DATA_DIR / "mistakes.sqlite3"))).expanduser()
APP_NAME = "家庭错题本"
AUTH_USERNAME = os.environ.get("HOMEWORK_NOTEBOOK_USERNAME", "student").strip().lower() or "student"
APP_USER_ID = "local-student"
APP_SCOPE = "local:student"
APP_CHANNEL = "local"
APP_SESSION = "student"
REVIEW_INTERVALS = [1, 3, 7, 14, 30]
BACKUP_VERSION = 3
SELECTED_EMAIL_RECIPIENT = os.environ.get("HOMEWORK_NOTEBOOK_EMAIL_RECIPIENT", "").strip()
CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
EMAIL_EXPORT_DIR = DATA_DIR / "email_exports"
AUTH_STATE_PATH = DATA_DIR / "auth_state.json"
AUTH_SESSION_COOKIE = "homework_session"
AUTH_PREAUTH_COOKIE = "homework_preauth"
AUTH_STATE_LOCK = threading.Lock()
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_LOCK_SECONDS = 300
SHORT_SESSION_SECONDS = 12 * 60 * 60
TRUSTED_SESSION_SECONDS = 30 * 24 * 60 * 60
PREAUTH_SESSION_SECONDS = 10 * 60
RECORD_TYPE_LABELS = {
    "mistake": "错题",
    "unsolved": "不会做",
    "reinforcement": "强化学习",
    "correct": "答对题",
}

SHANGHAI_TZ = timezone(timedelta(hours=8))

ENGLISH_CATEGORY_DEFINITIONS = {
    "pronunciation": "语音与重音",
    "spelling": "拼写、词形与固定词组",
    "verb_form": "时态与动词形式",
    "sentence": "句型与完整造句",
    "reading": "阅读理解",
    "writing": "写作表达",
}

ENGLISH_FOCUS_DEFINITIONS = [
    {
        "key": "modal_base",
        "category": "verb_form",
        "label": "情态动词后用动词原形",
        "rule": "can、could、should、will、must、would 后面的动词使用原形。",
        "wrong_example": "Could Kelly made a snowman?",
        "correct_example": "Could Kelly make a snowman?",
        "self_check": "先圈出情态动词，再检查它后面的动词是不是原形。",
    },
    {
        "key": "did_base",
        "category": "verb_form",
        "label": "do、does、did 后用动词原形",
        "rule": "do、does、did 已经承担了句子的变化，后面的行为动词要恢复原形。",
        "wrong_example": "Did Amy went there?",
        "correct_example": "Did Amy go there?",
        "self_check": "看到 do、does、did 或它们的否定形式，马上检查后面的动词。",
    },
    {
        "key": "past_tense",
        "category": "verb_form",
        "label": "时间词与一般过去时",
        "rule": "看到 yesterday、last...、...ago 等过去时间，要优先检查动词过去式。",
        "wrong_example": "Yesterday we meet Mr Green.",
        "correct_example": "Yesterday we met Mr Green.",
        "self_check": "先找时间词，再决定动词形式；不规则过去式要单独记。",
    },
    {
        "key": "third_person",
        "category": "verb_form",
        "label": "第三人称单数",
        "rule": "一般现在时中，主语是 he、she、it 或单个人名时，肯定句动词通常加 s，否定句使用 doesn't 加原形。",
        "wrong_example": "He don't like pictures.",
        "correct_example": "He doesn't like pictures.",
        "self_check": "先看主语是不是一个人或一个事物，再检查动词和助动词。",
    },
    {
        "key": "verb_ing",
        "category": "verb_form",
        "label": "固定结构后的动词-ing",
        "rule": "like、be good at、What about 后面表示动作时，按本阶段常见题型使用动词-ing形式。",
        "wrong_example": "He likes take pictures.",
        "correct_example": "He likes taking pictures.",
        "self_check": "看到 like、be good at、What about，先检查后面的动作有没有变成 -ing。",
    },
    {
        "key": "spelling_words",
        "category": "spelling",
        "label": "易错单词拼写",
        "rule": "不要只认得单词，要把容易漏写、换位或混淆的字母完整写出来。",
        "wrong_example": "oaches / kate / Frid",
        "correct_example": "coaches / kites / Friday",
        "self_check": "写完后按音节或字母组从头到尾逐个核对。",
    },
    {
        "key": "fixed_phrases",
        "category": "spelling",
        "label": "固定词组整体记忆",
        "rule": "英语固定搭配要整组记忆，不能只根据中文逐个拼单词。",
        "wrong_example": "made a party / count ten",
        "correct_example": "had a party / count to ten",
        "self_check": "遇到熟悉的中文意思，先回忆完整词组，再下笔。",
    },
    {
        "key": "question_sentence",
        "category": "sentence",
        "label": "一般疑问句与完整回答",
        "rule": "先确定开头的助动词或情态动词，再写主语、动词原形和其他信息；回答要与问句对应。",
        "wrong_example": "Could Kelly ...? No, she is ...",
        "correct_example": "Could Kelly make a snowman? No, she couldn't.",
        "self_check": "先口头说完整句子，再写下来，最后核对问句和回答是否对应。",
    },
    {
        "key": "complete_information",
        "category": "sentence",
        "label": "看图写句信息完整",
        "rule": "看图写句要把人物、动作、时间、地点和交通方式等题目要求的信息写完整。",
        "wrong_example": "Mark will go to Harbin this winter holiday.",
        "correct_example": "Mark will go to Harbin by plane this winter holiday.",
        "self_check": "写完后逐项对照图片和提示词，检查有没有漏掉信息。",
    },
    {
        "key": "vowel_sounds",
        "category": "pronunciation",
        "label": "字母组合发音辨析",
        "rule": "不要只看字母相同，要分别读出单词，再判断画线部分的实际发音。",
        "wrong_example": "看到相同字母组合就直接判断发音相同。",
        "correct_example": "分别读音，再比较音标中的目标部分。",
        "self_check": "先读两个完整单词，只比较画线部分的声音。",
    },
    {
        "key": "th_sounds",
        "category": "pronunciation",
        "label": "th 的清浊发音",
        "rule": "th 常见发音有 /θ/ 和 /ð/，要通过单词实际读音区分。",
        "wrong_example": "that 和 think 的 th 发音相同。",
        "correct_example": "that 中 th 发 /ð/，think 中 th 发 /θ/。",
        "self_check": "读 th 时感受声带是否振动，再判断清音或浊音。",
    },
    {
        "key": "word_stress",
        "category": "pronunciation",
        "label": "多音节单词重音",
        "rule": "多音节单词要按完整读音记住重读音节，不能只看第一个音节。",
        "wrong_example": "把 university 的重音放在开头。",
        "correct_example": "按词典或课堂读音标出真正的重读音节。",
        "self_check": "先慢读各音节，再找读得最响、最清楚的一个。",
    },
    {
        "key": "reading_detail",
        "category": "reading",
        "label": "阅读细节与原因定位",
        "rule": "先圈题干关键词，再回原文找到同义表达，不能只凭印象选择。",
        "wrong_example": "根据常识选择，没有回原文找 show respect。",
        "correct_example": "在原文定位依据后再选择答案。",
        "self_check": "每个答案都要能在原文中指出依据。",
    },
    {
        "key": "reading_main",
        "category": "reading",
        "label": "阅读主旨概括",
        "rule": "主旨答案要覆盖全文主要内容，不能只说一个细节，也不能把范围扩大。",
        "wrong_example": "只根据一个熟悉句子选择范围过大的答案。",
        "correct_example": "综合开头、结尾和反复出现的内容概括全文。",
        "self_check": "问自己：这个答案能不能同时概括大部分段落？",
    },
    {
        "key": "writing_expression",
        "category": "writing",
        "label": "写作中的时态与固定表达",
        "rule": "写作先使用自己确定会写的句型，再统一检查时态、固定搭配和句子完整性。",
        "wrong_example": "I am changed a lot. / We made a party.",
        "correct_example": "I have changed a lot. / We had a party.",
        "self_check": "写完后按时间顺序逐句检查动词，再检查固定词组。",
    },
]


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_code(secret: str, at_time: float | None = None) -> str:
    normalized = re.sub(r"\s+", "", secret).upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    counter = int((time.time() if at_time is None else at_time) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{number:06d}"


def verify_totp(secret: str, code: str, at_time: float | None = None, window: int = 1) -> bool:
    candidate = re.sub(r"\D", "", str(code or ""))
    if len(candidate) != 6:
        return False
    current = time.time() if at_time is None else at_time
    return any(
        hmac.compare_digest(totp_code(secret, current + offset * 30), candidate)
        for offset in range(-window, window + 1)
    )


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(4)[:4]}-{secrets.token_hex(4)[:4]}-{secrets.token_hex(4)[:4]}".upper() for _ in range(count)]


def normalize_recovery_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def access_codes_match(candidate: str, expected: str) -> bool:
    expected_value = str(expected or "").strip()
    candidate_value = str(candidate or "")
    if re.fullmatch(r"[0-9a-fA-F]{16,128}", expected_value):
        candidate_value = re.sub(r"[\s-]+", "", candidate_value).lower()
        expected_value = expected_value.lower()
    return hmac.compare_digest(candidate_value, expected_value)


def recovery_code_hash(code: str) -> str:
    return hashlib.sha256(normalize_recovery_code(code).encode("ascii")).hexdigest()


def _write_auth_state_unlocked(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def load_auth_state(path: Path = AUTH_STATE_PATH) -> dict:
    with AUTH_STATE_LOCK:
        if path.is_file():
            state = json.loads(path.read_text(encoding="utf-8"))
        else:
            state = {
                "version": 1,
                "totp_secret": generate_totp_secret(),
                "totp_confirmed": False,
                "recovery_hashes": [],
            }
            _write_auth_state_unlocked(path, state)
        if not str(state.get("totp_secret") or "").strip():
            state["totp_secret"] = generate_totp_secret()
            _write_auth_state_unlocked(path, state)
        return state


def confirm_totp_setup(path: Path, code: str, at_time: float | None = None) -> list[str] | None:
    with AUTH_STATE_LOCK:
        if not path.is_file():
            return None
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("totp_confirmed") or not verify_totp(str(state.get("totp_secret") or ""), code, at_time):
            return None
        recovery_codes = generate_recovery_codes()
        state["totp_confirmed"] = True
        state["confirmed_at"] = now_iso()
        state["recovery_hashes"] = [recovery_code_hash(item) for item in recovery_codes]
        _write_auth_state_unlocked(path, state)
        return recovery_codes


def consume_recovery_code(path: Path, code: str) -> bool:
    candidate_hash = recovery_code_hash(code)
    with AUTH_STATE_LOCK:
        if not path.is_file():
            return False
        state = json.loads(path.read_text(encoding="utf-8"))
        hashes = list(state.get("recovery_hashes") or [])
        match_index = next(
            (index for index, item in enumerate(hashes) if hmac.compare_digest(str(item), candidate_hash)),
            None,
        )
        if match_index is None:
            return False
        hashes.pop(match_index)
        state["recovery_hashes"] = hashes
        _write_auth_state_unlocked(path, state)
        return True


def _base64url_encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _base64url_decode(payload: str) -> bytes:
    return base64.urlsafe_b64decode(payload + "=" * ((4 - len(payload) % 4) % 4))


def make_session_token(secret: str, purpose: str, lifetime: int, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = {
        "purpose": purpose,
        "issued_at": issued_at,
        "expires_at": issued_at + lifetime,
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _base64url_encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_session_token(token: str, secret: str, purpose: str, now: int | None = None) -> bool:
    try:
        encoded, signature = token.split(".", 1)
        expected = _base64url_encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(_base64url_decode(encoded))
        current = int(time.time() if now is None else now)
        return (
            payload.get("purpose") == purpose
            and int(payload.get("issued_at") or 0) <= current + 60
            and int(payload.get("expires_at") or 0) >= current
        )
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def totp_uri(secret: str) -> str:
    issuer = APP_NAME
    label = f"{issuer}:{AUTH_USERNAME}"
    return (
        f"otpauth://totp/{quote(label)}?secret={quote(secret)}"
        f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )


def totp_qr_data_uri(secret: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    image = qrcode.make(totp_uri(secret), image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    EMAIL_EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mistake_cards (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            subject TEXT,
            unit_name TEXT,
            topic TEXT,
            question_type TEXT,
            knowledge_points_json TEXT NOT NULL,
            grammar_errors_json TEXT NOT NULL,
            misspelled_words_json TEXT NOT NULL,
            wrong_reason TEXT,
            explanation_summary TEXT,
            correct_answer TEXT,
            user_answer TEXT,
            image_path TEXT,
            memory_scope TEXT,
            source_channel TEXT,
            source_peer TEXT,
            source_session TEXT,
            tags_json TEXT NOT NULL,
            source_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            mem0_id TEXT
        )
        """
    )
    for name, ddl in {
        "record_type": "TEXT NOT NULL DEFAULT 'mistake'",
        "answer_status": "TEXT NOT NULL DEFAULT 'incorrect'",
        "question_number": "TEXT",
        "english_focuses_json": "TEXT NOT NULL DEFAULT '[]'",
        "question_text": "TEXT",
        "feynman_explain": "TEXT",
        "feynman_stuck": "TEXT",
        "next_review_at": "TEXT",
        "last_reviewed_at": "TEXT",
        "review_count": "INTEGER DEFAULT 0",
    }.items():
        ensure_column(conn, "mistake_cards", name, ddl)
    ensure_learning_schema(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_cards_created ON mistake_cards(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_cards_review ON mistake_cards(next_review_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS practice_feedback (
            id TEXT PRIMARY KEY,
            card_id TEXT NOT NULL,
            subject TEXT,
            topic TEXT,
            prompt_text TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_practice_feedback_created ON practice_feedback(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_practice_feedback_card ON practice_feedback(card_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS practice_attempts (
            id TEXT PRIMARY KEY,
            card_id TEXT NOT NULL,
            subject TEXT,
            topic TEXT,
            prompt_text TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_practice_attempts_created ON practice_attempts(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_practice_attempts_card ON practice_attempts(card_id)")
    ensure_column(conn, "practice_attempts", "grade_result", "TEXT")
    ensure_column(conn, "practice_attempts", "grade_feedback", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS selected_questions (
            id TEXT PRIMARY KEY,
            source_card_id TEXT NOT NULL,
            subject TEXT,
            topic TEXT,
            prompt_text TEXT NOT NULL,
            answer_text TEXT,
            hint_text TEXT,
            diagram_json TEXT NOT NULL DEFAULT '{}',
            diagram_svg TEXT,
            diagram_caption TEXT,
            sort_order INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_card_id, prompt_text)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_selected_questions_order "
        "ON selected_questions(sort_order, created_at)"
    )
    backfill_english_metadata(conn)
    backfill_practice_attempt_grades(conn)
    conn.commit()
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def decode_json(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).replace("，", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def as_misspellings(value) -> list[dict[str, str]]:
    if not value:
        return []
    if isinstance(value, str):
        items = []
        for pair in value.replace("，", ",").split(","):
            if "->" in pair:
                wrong, correct = pair.split("->", 1)
                items.append({"wrong": wrong.strip(), "correct": correct.strip()})
            elif pair.strip():
                items.append({"wrong": pair.strip(), "correct": ""})
        return items
    items = []
    for item in value:
        if isinstance(item, dict):
            wrong = str(item.get("wrong") or "").strip()
            correct = str(item.get("correct") or "").strip()
            if wrong:
                items.append({"wrong": wrong, "correct": correct})
    return items


def english_card_text(card: dict) -> str:
    return " ".join(
        str(value or "")
        for value in (
            card.get("unit"),
            card.get("topic"),
            card.get("question_text"),
            card.get("user_answer"),
            card.get("correct_answer"),
            card.get("wrong_reason"),
            " ".join(card.get("knowledge_points") or []),
            " ".join(card.get("grammar_errors") or []),
        )
    ).lower()


def infer_english_spelling_pairs(card: dict) -> list[dict[str, str]]:
    pairs = as_misspellings(card.get("misspelled_words"))
    text = english_card_text(card)
    known_targets = [
        ("frid", "Friday"),
        ("firday", "Friday"),
        ("fridge", "fridge"),
        ("witch", "which"),
        ("whitch", "which"),
        ("oaches", "coaches"),
        ("coach", "coaches"),
        ("angry", "angry"),
        ("chinese nation", "China's National Day"),
        ("china’s national day", "China's National Day"),
        ("fly kate", "fly kites"),
        ("kites", "kites"),
        ("skate", "skating"),
    ]
    spelling_signal = bool(re.search(r"(?:拼写|错写|漏写|少写|混淆|首字母)", text))
    for wrong, correct in known_targets:
        if wrong in text and (spelling_signal or wrong.lower() != correct.lower()):
            pairs.append({"wrong": "" if wrong.lower() == correct.lower() else wrong, "correct": correct})
    unique: dict[str, dict[str, str]] = {}
    for item in pairs:
        correct = str(item.get("correct") or "").strip()
        wrong = str(item.get("wrong") or "").strip()
        if not correct:
            continue
        unique[correct.lower()] = {"wrong": wrong, "correct": correct}
    return list(unique.values())


def infer_english_focuses(card: dict) -> list[str]:
    if str(card.get("subject") or "").strip() != "英语":
        return []
    text = english_card_text(card)
    focuses: list[str] = []

    def add(key: str, pattern: str) -> None:
        if re.search(pattern, text, re.IGNORECASE) and key not in focuses:
            focuses.append(key)

    add("modal_base", r"(?:情态动词|\bcould\b|\bwould\b|\bshould\b|\bmust\b|\bcan\b|动词原形)")
    add("did_base", r"(?:\bdid\b|didn[’']?t|\bdoes\b|doesn[’']?t|一般疑问句|一般过去时否定句)")
    add("past_tense", r"(?:一般过去时|过去式|\byesterday\b|\blast\b|\bago\b|went|took|felt|met|bought|had a party)")
    add("third_person", r"(?:第三人称单数|doesn[’']?t|\bhe don[’']?t\b|\bamy often\b)")
    add("verb_ing", r"(?:动词-?ing|动名词|doing sth|like doing|be good at|what about|taking pictures|drinking tea)")
    if infer_english_spelling_pairs(card) or re.search(r"(?:拼写|错写|漏写|少写|which与witch)", text):
        focuses.append("spelling_words")
    add(
        "fixed_phrases",
        r"(?:固定搭配|固定词组|have a party|made a party|count to ten|look for|take a trip|ride a bike|fly kites|take pictures|do morning exercises|the next day)",
    )
    add("question_sentence", r"(?:看图写问答|看图补全对话|看图写句|could的一般疑问句|完整回答|yes,|no, she|一般疑问句)")
    add("complete_information", r"(?:图片信息完整|漏写|交通方式|看图写句|暑假计划作文|旅行计划)")
    add("th_sounds", r"(?:th的|that.{0,10}think|/θ/|/ð/)")
    add("word_stress", r"(?:重音|多音节|university|kangaroo)")
    add("vowel_sounds", r"(?:发音判断|画线部分发音|字母组合.{0,8}发音|ai与a|oo的短音|ow的发音|字母u的发音)")
    add("reading_main", r"(?:主旨|main idea|概括文章中心)")
    add("reading_detail", r"(?:细节理解|定位原文|why do|show respect)")
    add("writing_expression", r"(?:作文|小练笔|写作|不少于|i am changed|made a party)")
    if not focuses:
        focuses.append("question_sentence")
    order = {item["key"]: index for index, item in enumerate(ENGLISH_FOCUS_DEFINITIONS)}
    return sorted(set(focuses), key=lambda key: order.get(key, 999))


def backfill_english_metadata(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM mistake_cards WHERE subject = '英语'").fetchall()
    for row in rows:
        card = {
            "subject": row["subject"],
            "unit": row["unit_name"],
            "topic": row["topic"],
            "question_text": row["question_text"],
            "user_answer": row["user_answer"],
            "correct_answer": row["correct_answer"],
            "wrong_reason": row["wrong_reason"],
            "knowledge_points": decode_json(row["knowledge_points_json"], []),
            "grammar_errors": decode_json(row["grammar_errors_json"], []),
            "misspelled_words": decode_json(row["misspelled_words_json"], []),
        }
        focuses = infer_english_focuses(card)
        misspellings = infer_english_spelling_pairs(card)
        answer_status = "correct" if str(row["record_type"] or "") == "correct" else str(row["answer_status"] or "incorrect")
        conn.execute(
            "UPDATE mistake_cards SET english_focuses_json = ?, misspelled_words_json = ?, answer_status = ? WHERE id = ?",
            (
                json.dumps(focuses, ensure_ascii=False),
                json.dumps(misspellings, ensure_ascii=False),
                answer_status,
                row["id"],
            ),
        )


def row_to_card(row: sqlite3.Row) -> dict:
    raw = decode_json(row["raw_json"], {})
    keys = set(row.keys())
    learning_status = str(row["learning_status"] or "pending") if "learning_status" in keys else "pending"
    learning_pack = decode_json(row["learning_pack_json"], {}) if "learning_pack_json" in keys else {}
    subject = row["subject"] or ""
    question_text = row["question_text"] or raw.get("question_text") or ""
    knowledge_points = decode_json(row["knowledge_points_json"], [])
    correct_answer = row["correct_answer"] or ""
    record_type = row["record_type"] or "mistake"
    answer_status = str(row["answer_status"] or ("correct" if record_type == "correct" else "incorrect"))
    image_path = str(row["image_path"] or "").strip()
    image_file = Path(image_path).expanduser() if image_path else None
    image_available = bool(
        image_file
        and image_file.is_file()
        and image_file.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    )
    learning_eligible = bool(
        record_type != "correct"
        and subject != "待整理"
        and question_text.strip()
        and knowledge_points
        and "待整理" not in knowledge_points
        and correct_answer.strip()
    )
    if record_type == "correct":
        learning_status = "not_needed"
    elif learning_status == "pending" and not learning_eligible:
        learning_status = "incomplete"
    source = decode_json(row["source_json"], {})
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "record_type": record_type,
        "record_type_label": RECORD_TYPE_LABELS.get(record_type, "错题"),
        "answer_status": answer_status,
        "question_number": row["question_number"] or "",
        "subject": subject,
        "unit": row["unit_name"] or "",
        "topic": row["topic"] or "",
        "question_type": row["question_type"] or "",
        "question_text": question_text,
        "knowledge_points": knowledge_points,
        "grammar_errors": decode_json(row["grammar_errors_json"], []),
        "misspelled_words": decode_json(row["misspelled_words_json"], []),
        "english_focuses": decode_json(row["english_focuses_json"], []),
        "wrong_reason": row["wrong_reason"] or "",
        "explanation_summary": row["explanation_summary"] or "",
        "correct_answer": row["correct_answer"] or "",
        "user_answer": row["user_answer"] or "",
        "image_path": image_path,
        "image_available": image_available,
        "feynman_explain": row["feynman_explain"] or raw.get("feynman_explain") or "",
        "feynman_stuck": row["feynman_stuck"] or raw.get("feynman_stuck") or "",
        "next_review_at": row["next_review_at"] or "",
        "last_reviewed_at": row["last_reviewed_at"] or "",
        "review_count": int(row["review_count"] or 0),
        "tags": decode_json(row["tags_json"], []),
        "source_channel": row["source_channel"] or "",
        "source_session": row["source_session"] or "",
        "source": source,
        "learning": {
            "status": learning_status,
            "eligible": learning_eligible,
            "pack": learning_pack,
            "generated_at": str(row["learning_generated_at"] or "") if "learning_generated_at" in keys else "",
            "error": str(row["learning_error"] or "") if "learning_error" in keys else "",
        },
    }


def list_cards(conn: sqlite3.Connection, params: dict[str, list[str]]) -> list[dict]:
    clauses: list[str] = []
    values: list[str] = []
    subject = first_param(params, "subject")
    record_type = first_param(params, "record_type")
    unit = first_param(params, "unit")
    query = first_param(params, "q").lower()
    due = first_param(params, "due")
    date_from = valid_date_filter(first_param(params, "date_from"))
    date_to = valid_date_filter(first_param(params, "date_to"))
    if subject:
        clauses.append("cards.subject = ?")
        values.append(subject)
    if record_type:
        clauses.append("cards.record_type = ?")
        values.append(record_type)
    if unit:
        clauses.append("cards.unit_name = ?")
        values.append(unit)
    if due == "1":
        clauses.append("(cards.next_review_at IS NULL OR cards.next_review_at = '' OR cards.next_review_at <= ?)")
        values.append(now_iso())
    if date_from:
        clauses.append("date(datetime(cards.created_at, '+8 hours')) >= date(?)")
        values.append(date_from)
    if date_to:
        clauses.append("date(datetime(cards.created_at, '+8 hours')) <= date(?)")
        values.append(date_to)
    sql = """
        SELECT cards.*,
               COALESCE(learning.status, 'pending') AS learning_status,
               COALESCE(learning.pack_json, '{}') AS learning_pack_json,
               COALESCE(learning.generated_at, '') AS learning_generated_at,
               COALESCE(learning.error, '') AS learning_error
        FROM mistake_cards AS cards
        LEFT JOIN learning_packs AS learning ON learning.card_id = cards.id
    """
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY cards.created_at DESC"
    cards = [row_to_card(row) for row in conn.execute(sql, values)]
    if query:
        cards = [
            card
            for card in cards
            if query
            in " ".join(
                [
                    card["subject"],
                    card["unit"],
                    card["topic"],
                    card["question_type"],
                    card["question_text"],
                    card["wrong_reason"],
                    card["explanation_summary"],
                    " ".join(card["knowledge_points"]),
                    " ".join(card["tags"]),
                ]
            ).lower()
        ]
    return cards


def first_param(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key) or []
    return values[0].strip() if values else default


def valid_date_filter(value: str) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def normalize_payload(payload: dict) -> dict:
    record_type = str(payload.get("record_type") or "mistake").strip()
    if record_type not in RECORD_TYPE_LABELS:
        raise ValueError("记录类型不正确")
    subject = str(payload.get("subject") or "").strip()
    unit = str(payload.get("unit") or "").strip()
    topic = str(payload.get("topic") or "").strip()
    question_text = str(payload.get("question_text") or "").strip()
    knowledge_points = as_list(payload.get("knowledge_points"))
    if not subject or not topic or not question_text:
        raise ValueError("科目、主题和题目不能为空")
    if not knowledge_points:
        raise ValueError("至少填写一个知识点")
    created_at = str(payload.get("created_at") or now_iso())
    review_days = int(payload.get("review_days") or 1)
    next_review_at = str(payload.get("next_review_at") or (datetime.now(timezone.utc) + timedelta(days=review_days)).isoformat())
    source = {"channel": APP_CHANNEL, "session": APP_SESSION}
    normalized = {
        "id": str(payload.get("id") or uuid.uuid4()),
        "user_id": APP_USER_ID,
        "created_at": created_at,
        "record_type": record_type,
        "answer_status": str(payload.get("answer_status") or ("correct" if record_type == "correct" else "incorrect")).strip(),
        "question_number": str(payload.get("question_number") or "").strip(),
        "subject": subject,
        "unit": unit,
        "topic": topic,
        "question_type": str(payload.get("question_type") or "").strip(),
        "question_text": question_text,
        "knowledge_points": knowledge_points,
        "grammar_errors": as_list(payload.get("grammar_errors")),
        "misspelled_words": as_misspellings(payload.get("misspelled_words")),
        "wrong_reason": str(payload.get("wrong_reason") or ("主动强化该知识点" if record_type == "reinforcement" else "知识点还没有掌握")).strip(),
        "explanation_summary": str(payload.get("explanation_summary") or "").strip(),
        "correct_answer": str(payload.get("correct_answer") or "").strip(),
        "user_answer": str(payload.get("user_answer") or ("未作答" if record_type != "mistake" else "")).strip(),
        "image_path": str(payload.get("image_path") or "").strip(),
        "feynman_explain": str(payload.get("feynman_explain") or "").strip(),
        "feynman_stuck": str(payload.get("feynman_stuck") or "").strip(),
        "next_review_at": next_review_at,
        "last_reviewed_at": str(payload.get("last_reviewed_at") or "").strip(),
        "review_count": int(payload.get("review_count") or 0),
        "tags": as_list(payload.get("tags")),
        "memory_scope": APP_SCOPE,
        "source_channel": APP_CHANNEL,
        "source_peer": "",
        "source_session": APP_SESSION,
        "source": source,
    }
    normalized["misspelled_words"] = infer_english_spelling_pairs(normalized)
    normalized["english_focuses"] = as_list(payload.get("english_focuses")) or infer_english_focuses(normalized)
    return normalized


def save_card(conn: sqlite3.Connection, payload: dict) -> dict:
    card = normalize_payload(payload)
    old = conn.execute(
        "SELECT record_type, subject, unit_name, topic, question_text, knowledge_points_json, wrong_reason, correct_answer FROM mistake_cards WHERE id = ?",
        (card["id"],),
    ).fetchone()
    raw_json = json.dumps(card, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO mistake_cards (
            id, user_id, created_at, record_type, answer_status, question_number,
            english_focuses_json, subject, unit_name, topic, question_type,
            knowledge_points_json, grammar_errors_json, misspelled_words_json,
            wrong_reason, explanation_summary, correct_answer, user_answer,
            image_path, memory_scope, source_channel, source_peer, source_session,
            tags_json, source_json, raw_json, mem0_id, question_text,
            feynman_explain, feynman_stuck, next_review_at, last_reviewed_at, review_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            record_type = excluded.record_type,
            answer_status = excluded.answer_status,
            question_number = excluded.question_number,
            english_focuses_json = excluded.english_focuses_json,
            subject = excluded.subject,
            unit_name = excluded.unit_name,
            topic = excluded.topic,
            question_type = excluded.question_type,
            knowledge_points_json = excluded.knowledge_points_json,
            grammar_errors_json = excluded.grammar_errors_json,
            misspelled_words_json = excluded.misspelled_words_json,
            wrong_reason = excluded.wrong_reason,
            explanation_summary = excluded.explanation_summary,
            correct_answer = excluded.correct_answer,
            user_answer = excluded.user_answer,
            image_path = excluded.image_path,
            tags_json = excluded.tags_json,
            question_text = excluded.question_text,
            feynman_explain = excluded.feynman_explain,
            feynman_stuck = excluded.feynman_stuck,
            next_review_at = excluded.next_review_at,
            last_reviewed_at = excluded.last_reviewed_at,
            review_count = excluded.review_count
        """,
        (
            card["id"],
            card["user_id"],
            card["created_at"],
            card["record_type"],
            card["answer_status"],
            card["question_number"],
            json.dumps(card["english_focuses"], ensure_ascii=False),
            card["subject"],
            card["unit"],
            card["topic"],
            card["question_type"],
            json.dumps(card["knowledge_points"], ensure_ascii=False),
            json.dumps(card["grammar_errors"], ensure_ascii=False),
            json.dumps(card["misspelled_words"], ensure_ascii=False),
            card["wrong_reason"],
            card["explanation_summary"],
            card["correct_answer"],
            card["user_answer"],
            card["image_path"],
            card["memory_scope"],
            card["source_channel"],
            card["source_peer"],
            card["source_session"],
            json.dumps(card["tags"], ensure_ascii=False),
            json.dumps(card["source"], ensure_ascii=False),
            raw_json,
            None,
            card["question_text"],
            card["feynman_explain"],
            card["feynman_stuck"],
            card["next_review_at"],
            card["last_reviewed_at"],
            card["review_count"],
        ),
    )
    current_inputs = (
        card["record_type"],
        card["subject"],
        card["unit"],
        card["topic"],
        card["question_text"],
        json.dumps(card["knowledge_points"], ensure_ascii=False),
        card["wrong_reason"],
        card["correct_answer"],
    )
    old_inputs = tuple(old) if old else None
    if card["record_type"] == "correct":
        conn.execute("DELETE FROM learning_packs WHERE card_id = ?", (card["id"],))
    elif old_inputs != current_inputs:
        conn.execute(
            """
            INSERT INTO learning_packs (card_id, status, pack_json, generated_at, error)
            VALUES (?, 'pending', '{}', NULL, '')
            ON CONFLICT(card_id) DO UPDATE SET status = 'pending', pack_json = '{}', generated_at = NULL, error = ''
            """,
            (card["id"],),
        )
    conn.commit()
    return next(item for item in list_cards(conn, {}) if item["id"] == card["id"])


def normalized_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def duplicate_candidates(conn: sqlite3.Connection, payload: dict, exclude_id: str = "") -> list[dict]:
    card = normalize_payload(payload)
    topic = normalized_text(card["topic"])
    question = normalized_text(card["question_text"])
    if len(question) < 8:
        return []
    matches: list[dict] = []
    for row in conn.execute("SELECT * FROM mistake_cards WHERE subject = ?", (card["subject"],)):
        if row["id"] == exclude_id:
            continue
        other = row_to_card(row)
        other_question = normalized_text(other["question_text"])
        other_topic = normalized_text(other["topic"])
        if len(other_question) < 8:
            continue
        ratio = SequenceMatcher(None, question, other_question).ratio()
        if question == other_question or (topic and topic == other_topic and ratio >= 0.78):
            matches.append(
                {
                    "id": other["id"],
                    "subject": other["subject"],
                    "topic": other["topic"],
                    "question_text": other["question_text"],
                    "created_at": other["created_at"],
                    "similarity": round(ratio, 2),
                }
            )
    return matches[:3]


def review_card(conn: sqlite3.Connection, card_id: str) -> dict:
    row = conn.execute("SELECT * FROM mistake_cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        raise KeyError("找不到这条错题")
    count = int(row["review_count"] or 0) + 1
    interval = REVIEW_INTERVALS[min(count - 1, len(REVIEW_INTERVALS) - 1)]
    reviewed_at = now_iso()
    next_review_at = (datetime.now(timezone.utc) + timedelta(days=interval)).isoformat()
    conn.execute(
        """
        UPDATE mistake_cards
        SET review_count = ?, last_reviewed_at = ?, next_review_at = ?
        WHERE id = ?
        """,
        (count, reviewed_at, next_review_at, card_id),
    )
    conn.commit()
    return row_to_card(conn.execute("SELECT * FROM mistake_cards WHERE id = ?", (card_id,)).fetchone())


def delete_card(conn: sqlite3.Connection, card_id: str) -> None:
    result = conn.execute("DELETE FROM mistake_cards WHERE id = ?", (card_id,))
    conn.execute("DELETE FROM learning_packs WHERE card_id = ?", (card_id,))
    conn.execute("DELETE FROM practice_feedback WHERE card_id = ?", (card_id,))
    conn.execute("DELETE FROM practice_attempts WHERE card_id = ?", (card_id,))
    conn.execute("DELETE FROM selected_questions WHERE source_card_id = ?", (card_id,))
    resequence_selected_questions(conn)
    conn.commit()
    if result.rowcount == 0:
        raise KeyError("找不到这条错题")


def row_to_practice_feedback(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "card_id": row["card_id"],
        "subject": row["subject"] or "",
        "topic": row["topic"] or "",
        "prompt_text": row["prompt_text"],
        "result": row["result"],
        "created_at": row["created_at"],
    }


def list_practice_feedback(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM practice_feedback ORDER BY created_at DESC").fetchall()
    return [row_to_practice_feedback(row) for row in rows]


def save_practice_feedback(conn: sqlite3.Connection, payload: dict) -> dict:
    card_id = str(payload.get("card_id") or "").strip()
    prompt_text = str(payload.get("prompt_text") or "").strip()
    result = str(payload.get("result") or "").strip()
    if not card_id or not prompt_text:
        raise ValueError("缺少题目信息")
    if result not in {"mastered", "needs_work"}:
        raise ValueError("复习结果不正确")
    card = conn.execute("SELECT * FROM mistake_cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        raise KeyError("找不到来源错题")
    feedback = {
        "id": str(uuid.uuid4()),
        "card_id": card_id,
        "subject": str(payload.get("subject") or card["subject"] or "").strip(),
        "topic": str(payload.get("topic") or card["topic"] or "").strip(),
        "prompt_text": prompt_text,
        "result": result,
        "created_at": now_iso(),
    }
    conn.execute(
        """
        INSERT INTO practice_feedback (
            id, card_id, subject, topic, prompt_text, result, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            feedback["id"],
            feedback["card_id"],
            feedback["subject"],
            feedback["topic"],
            feedback["prompt_text"],
            feedback["result"],
            feedback["created_at"],
        ),
    )
    count = int(card["review_count"] or 0) + 1
    if result == "mastered":
        interval = REVIEW_INTERVALS[min(count - 1, len(REVIEW_INTERVALS) - 1)]
    else:
        interval = 1
    reviewed_at = now_iso()
    next_review_at = (datetime.now(timezone.utc) + timedelta(days=interval)).isoformat()
    conn.execute(
        """
        UPDATE mistake_cards
        SET review_count = ?, last_reviewed_at = ?, next_review_at = ?
        WHERE id = ?
        """,
        (count, reviewed_at, next_review_at, card_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM practice_feedback WHERE id = ?", (feedback["id"],)).fetchone()
    return row_to_practice_feedback(row)


SEMANTIC_ANSWERS = (
    ("不成正比例", ("不成正比例", "不是正比例", "不属于正比例")),
    ("成正比例", ("成正比例", "是正比例")),
    ("不变", ("不变", "没有变化", "不会改变", "没改变")),
    ("改变", ("改变", "会改变", "会变", "发生变化")),
    ("不相同", ("不相同", "不一样", "不同")),
    ("相同", ("相同", "一样")),
    ("不能", ("不能", "不可以", "不行")),
    ("能", ("能", "可以")),
    ("错误", ("错误", "不正确", "错")),
    ("正确", ("正确", "对", "√")),
)


def normalize_grading_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"[\s，。！？；：、,.!?;:\"“”'()（）\[\]{}]", "", text)


def semantic_answer(value: str) -> str:
    normalized = normalize_grading_text(value)
    for label, aliases in SEMANTIC_ANSWERS:
        if any(normalize_grading_text(alias) in normalized for alias in aliases):
            return label
    return ""


def numeric_values(value: str) -> list[Fraction]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    occupied: list[tuple[int, int]] = []
    values: list[Fraction] = []

    def add_match(match: re.Match, number: Fraction) -> None:
        occupied.append(match.span())
        values.append(number)

    for match in re.finditer(r"(-?\d+)\s*又\s*(\d+)\s*/\s*(\d+)\s*(%)?", text):
        number = Fraction(int(match.group(1)), 1) + Fraction(int(match.group(2)), int(match.group(3)))
        add_match(match, number / 100 if match.group(4) else number)
    for match in re.finditer(r"(-?\d+)\s*/\s*(\d+)\s*(%)?", text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        number = Fraction(int(match.group(1)), int(match.group(2)))
        add_match(match, number / 100 if match.group(3) else number)
    for match in re.finditer(r"-?\d+(?:\.\d+)?\s*%?", text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        token = match.group(0).replace(" ", "")
        is_percent = token.endswith("%")
        if is_percent:
            token = token[:-1]
        number = Fraction(token)
        add_match(match, number / 100 if is_percent else number)
    return values


def practice_answer_focus(prompt: str, expected_answer: str) -> str:
    answer = unicodedata.normalize("NFKC", expected_answer).strip()
    answer = re.split(r"(?:关键步骤|验算|关键提醒)[：:]", answer, maxsplit=1)[0].strip()
    for marker in ("答案：", "答案:", "答：", "答:"):
        if marker in answer:
            return answer.rsplit(marker, 1)[1].strip()
    if re.match(r"^\s*(?:选|填)\s*[A-Za-z]", answer):
        return re.split(r"[。！？!?]", answer, maxsplit=1)[0].strip()
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])", answer) if part.strip()]
    question_count = max(prompt.count("？") + prompt.count("?"), 1)
    return "".join(sentences[-question_count:]) if sentences else answer


def expected_final_numbers(prompt: str, focus: str) -> list[Fraction]:
    values = numeric_values(focus)
    if not values:
        return []
    multiple = prompt.count("？") + prompt.count("?") > 1 or bool(
        re.search(r"分别|各(?:有|是|多少)|两项|圆锥和削去", prompt)
    )
    if not multiple:
        return values[-1:]
    expected_count = max(prompt.count("？") + prompt.count("?"), 2)
    return values[-expected_count:]


def option_answer(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    match = re.search(r"(?:^|选|答案(?:是|为)?)[\s:：]*([A-D])(?:\b|[。.]|$)", text)
    return match.group(1) if match else ""


def find_practice_reference(conn: sqlite3.Connection, card_id: str, prompt_text: str) -> str:
    row = conn.execute(
        "SELECT pack_json FROM learning_packs WHERE card_id = ? AND status = 'ready'",
        (card_id,),
    ).fetchone()
    if not row:
        return ""
    pack = decode_json(row["pack_json"], {})
    for exercise in pack.get("exercises") or []:
        if str(exercise.get("question") or "").strip() == prompt_text.strip():
            return str(exercise.get("answer") or "").strip()
    return ""


def grade_practice_answer(prompt: str, expected_answer: str, answer_text: str) -> tuple[str, str]:
    if not expected_answer.strip():
        return "ungraded", "这道题暂时缺少参考答案，请先查看讲解。"

    focus = practice_answer_focus(prompt, expected_answer)
    expected_option = option_answer(focus)
    if expected_option:
        correct = option_answer(answer_text) == expected_option
        return (
            ("correct", "选项与正确答案一致。")
            if correct
            else ("incorrect", f"选项不对，正确选项是 {expected_option}。")
        )

    fill_match = re.match(r"^\s*填\s*([A-Za-z]+)", focus)
    if fill_match:
        target = normalize_grading_text(fill_match.group(1))
        correct = normalize_grading_text(answer_text) == target
        return (
            ("correct", "填写内容与正确答案一致。")
            if correct
            else ("incorrect", "填写内容还不正确，请对照下面的答案检查拼写。")
        )

    expected_semantic = semantic_answer(focus)
    actual_semantic = semantic_answer(answer_text)
    final_numbers = expected_final_numbers(prompt, focus)
    actual_numbers = numeric_values(answer_text)
    numbers_match = all(number in actual_numbers for number in final_numbers)
    asks_for_number = bool(re.search(r"多少|几倍|百分之几|各(?:有|是)|分别", prompt))
    if expected_semantic:
        semantic_matches = expected_semantic == actual_semantic
        correct = semantic_matches and (not asks_for_number or not final_numbers or numbers_match)
        return (
            ("correct", "结论和最终结果都正确。")
            if correct
            else ("incorrect", "结论或最终结果还不一致，请对照下面的答案检查。")
        )

    if final_numbers and numbers_match:
        return "correct", "最终结果与正确答案一致。"

    compact_expected = normalize_grading_text(focus)
    compact_actual = normalize_grading_text(answer_text)
    contains_match = len(compact_actual) >= 3 and (
        compact_actual in compact_expected or compact_expected in compact_actual
    )
    similarity = SequenceMatcher(None, compact_actual, compact_expected).ratio() if compact_actual else 0
    if contains_match or (len(compact_actual) >= 6 and similarity >= 0.78):
        return "correct", "答案的关键内容与正确答案一致。"
    return "incorrect", "答案还不一致，请对照正确答案和关键提醒再检查一次。"


def backfill_practice_attempt_grades(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT * FROM practice_attempts WHERE COALESCE(grade_result, '') = ''"
    ).fetchall()
    for row in rows:
        expected = find_practice_reference(conn, row["card_id"], row["prompt_text"])
        result, feedback = grade_practice_answer(row["prompt_text"], expected, row["answer_text"])
        conn.execute(
            "UPDATE practice_attempts SET grade_result = ?, grade_feedback = ? WHERE id = ?",
            (result, feedback, row["id"]),
        )


def row_to_practice_attempt(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "card_id": row["card_id"],
        "subject": row["subject"] or "",
        "topic": row["topic"] or "",
        "prompt_text": row["prompt_text"],
        "answer_text": row["answer_text"],
        "grade_result": row["grade_result"] or "ungraded",
        "grade_feedback": row["grade_feedback"] or "",
        "created_at": row["created_at"],
    }


def list_practice_attempts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM practice_attempts ORDER BY created_at DESC").fetchall()
    return [row_to_practice_attempt(row) for row in rows]


def row_to_selected_question(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "source_card_id": row["source_card_id"],
        "subject": row["subject"] or "",
        "topic": row["topic"] or "",
        "prompt_text": row["prompt_text"],
        "answer_text": row["answer_text"] or "",
        "hint_text": row["hint_text"] or "",
        "diagram": decode_json(row["diagram_json"], {}),
        "diagram_svg": row["diagram_svg"] or "",
        "diagram_caption": row["diagram_caption"] or "",
        "sort_order": int(row["sort_order"] or 0),
        "created_at": row["created_at"],
    }


def list_selected_questions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM selected_questions ORDER BY sort_order, created_at"
    ).fetchall()
    return [row_to_selected_question(row) for row in rows]


def sanitize_diagram_svg(value: object) -> str:
    svg = str(value or "").strip()
    if not svg:
        return ""
    if len(svg) > 120_000 or not re.match(r"^<svg\b", svg, flags=re.IGNORECASE):
        return ""
    if re.search(
        r"<(?:script|foreignObject|iframe|object|embed)\b|\bon\w+\s*=|javascript:",
        svg,
        flags=re.IGNORECASE,
    ):
        return ""
    return svg


def save_selected_question(conn: sqlite3.Connection, payload: dict) -> tuple[dict, bool]:
    source_card_id = str(payload.get("source_card_id") or "").strip()
    prompt_text = str(payload.get("prompt_text") or "").strip()
    if not source_card_id or not prompt_text:
        raise ValueError("缺少自选题信息")
    card = conn.execute(
        "SELECT subject, topic FROM mistake_cards WHERE id = ?", (source_card_id,)
    ).fetchone()
    if not card:
        raise KeyError("找不到来源错题")
    existing = conn.execute(
        "SELECT * FROM selected_questions WHERE source_card_id = ? AND prompt_text = ?",
        (source_card_id, prompt_text),
    ).fetchone()
    if existing:
        return row_to_selected_question(existing), False
    diagram = payload.get("diagram") if isinstance(payload.get("diagram"), dict) else {}
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM selected_questions"
    ).fetchone()[0]
    question = {
        "id": str(uuid.uuid4()),
        "source_card_id": source_card_id,
        "subject": str(payload.get("subject") or card["subject"] or "").strip()[:80],
        "topic": str(payload.get("topic") or card["topic"] or "").strip()[:180],
        "prompt_text": prompt_text[:4000],
        "answer_text": str(payload.get("answer_text") or "").strip()[:6000],
        "hint_text": str(payload.get("hint_text") or "").strip()[:1500],
        "diagram_json": json.dumps(diagram, ensure_ascii=False),
        "diagram_svg": sanitize_diagram_svg(payload.get("diagram_svg")),
        "diagram_caption": str(payload.get("diagram_caption") or "").strip()[:500],
        "sort_order": int(max_order) + 1,
        "created_at": now_iso(),
    }
    conn.execute(
        """
        INSERT INTO selected_questions (
            id, source_card_id, subject, topic, prompt_text, answer_text,
            hint_text, diagram_json, diagram_svg, diagram_caption, sort_order, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(question.values()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM selected_questions WHERE id = ?", (question["id"],)).fetchone()
    return row_to_selected_question(row), True


def delete_selected_question(conn: sqlite3.Connection, question_id: str) -> None:
    result = conn.execute("DELETE FROM selected_questions WHERE id = ?", (question_id,))
    if result.rowcount == 0:
        raise KeyError("找不到这道自选题")
    resequence_selected_questions(conn)
    conn.commit()


def resequence_selected_questions(conn: sqlite3.Connection, ordered_ids: list[str] | None = None) -> None:
    if ordered_ids is None:
        ordered_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM selected_questions ORDER BY sort_order, created_at"
            ).fetchall()
        ]
    for index, question_id in enumerate(ordered_ids, 1):
        conn.execute(
            "UPDATE selected_questions SET sort_order = ? WHERE id = ?", (index, question_id)
        )


def move_selected_question(conn: sqlite3.Connection, question_id: str, direction: str) -> list[dict]:
    if direction not in {"up", "down"}:
        raise ValueError("移动方向不正确")
    ordered_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM selected_questions ORDER BY sort_order, created_at"
        ).fetchall()
    ]
    if question_id not in ordered_ids:
        raise KeyError("找不到这道自选题")
    index = ordered_ids.index(question_id)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(ordered_ids):
        ordered_ids[index], ordered_ids[target] = ordered_ids[target], ordered_ids[index]
        resequence_selected_questions(conn, ordered_ids)
        conn.commit()
    return list_selected_questions(conn)


def save_practice_attempt(conn: sqlite3.Connection, payload: dict) -> dict:
    card_id = str(payload.get("card_id") or "").strip()
    prompt_text = str(payload.get("prompt_text") or "").strip()
    answer_text = str(payload.get("answer_text") or "").strip()
    if not card_id or not prompt_text or not answer_text:
        raise ValueError("请先写下答案或思路")
    card = conn.execute("SELECT * FROM mistake_cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        raise KeyError("找不到来源错题")
    expected_answer = find_practice_reference(conn, card_id, prompt_text)
    if not expected_answer:
        expected_answer = str(payload.get("reference_answer") or "").strip()
    grade_result, grade_feedback = grade_practice_answer(prompt_text, expected_answer, answer_text)
    attempt = {
        "id": str(uuid.uuid4()),
        "card_id": card_id,
        "subject": str(payload.get("subject") or card["subject"] or "").strip(),
        "topic": str(payload.get("topic") or card["topic"] or "").strip(),
        "prompt_text": prompt_text,
        "answer_text": answer_text,
        "grade_result": grade_result,
        "grade_feedback": grade_feedback,
        "created_at": now_iso(),
    }
    conn.execute(
        """
        INSERT INTO practice_attempts (
            id, card_id, subject, topic, prompt_text, answer_text,
            grade_result, grade_feedback, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt["id"],
            attempt["card_id"],
            attempt["subject"],
            attempt["topic"],
            attempt["prompt_text"],
            attempt["answer_text"],
            attempt["grade_result"],
            attempt["grade_feedback"],
            attempt["created_at"],
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM practice_attempts WHERE id = ?", (attempt["id"],)).fetchone()
    saved = row_to_practice_attempt(row)
    if grade_result == "incorrect":
        latest = conn.execute(
            """
            SELECT * FROM practice_feedback
            WHERE card_id = ? AND prompt_text = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (card_id, prompt_text),
        ).fetchone()
        if latest and latest["result"] == "needs_work":
            auto_feedback = row_to_practice_feedback(latest)
        else:
            auto_feedback = save_practice_feedback(
                conn,
                {
                    "card_id": card_id,
                    "subject": attempt["subject"],
                    "topic": attempt["topic"],
                    "prompt_text": prompt_text,
                    "result": "needs_work",
                },
            )
        saved["auto_feedback"] = auto_feedback
    return saved


def build_learning_summary(conn: sqlite3.Connection) -> dict:
    start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    attempts = conn.execute("SELECT * FROM practice_attempts WHERE created_at >= ? ORDER BY created_at DESC", (start,)).fetchall()
    feedback = conn.execute("SELECT * FROM practice_feedback WHERE created_at >= ? ORDER BY created_at DESC", (start,)).fetchall()
    subject_counter: Counter[str] = Counter()
    weak_counter: Counter[str] = Counter()
    for row in feedback:
        subject_counter[row["subject"] or "未标注"] += 1
        if row["result"] == "needs_work":
            weak_counter[row["topic"] or "未命名知识点"] += 1
    return {
        "since": start,
        "attempts": len(attempts),
        "mastered": sum(1 for row in feedback if row["result"] == "mastered"),
        "needs_work": sum(1 for row in feedback if row["result"] == "needs_work"),
        "subjects": [{"name": name, "count": count} for name, count in subject_counter.most_common()],
        "needs_work_topics": [{"name": name, "count": count} for name, count in weak_counter.most_common(8)],
    }


def parse_card_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(SHANGHAI_TZ)


def group_english_exams(cards: list[dict]) -> list[dict]:
    ordered = sorted(cards, key=lambda card: parse_card_datetime(card.get("created_at") or ""))
    groups: list[list[dict]] = []
    for card in ordered:
        current_time = parse_card_datetime(card.get("created_at") or "")
        if groups:
            previous_time = parse_card_datetime(groups[-1][-1].get("created_at") or "")
            same_day = previous_time.date() == current_time.date()
            if same_day and current_time - previous_time <= timedelta(minutes=20):
                groups[-1].append(card)
                continue
        groups.append([card])

    day_counts: Counter[str] = Counter()
    exams: list[dict] = []
    for group in groups:
        local_time = parse_card_datetime(group[0].get("created_at") or "")
        day_key = local_time.date().isoformat()
        day_counts[day_key] += 1
        suffix = f" · 第{day_counts[day_key]}份" if day_counts[day_key] > 1 else ""
        incorrect = [card for card in group if card.get("answer_status") != "correct"]
        correct = [card for card in group if card.get("answer_status") == "correct"]
        uncertain = [card for card in group if card.get("answer_status") == "uncertain"]
        focus_counter: Counter[str] = Counter(
            focus for card in incorrect for focus in card.get("english_focuses") or []
        )
        focus_labels = {
            item["key"]: item["label"] for item in ENGLISH_FOCUS_DEFINITIONS
        }
        top_focuses = [focus_labels.get(key, key) for key, _ in focus_counter.most_common(3)]
        exam_id = f"english-{day_key}-{day_counts[day_key]}"
        for card in group:
            card["_english_exam_id"] = exam_id
        exams.append(
            {
                "id": exam_id,
                "title": f"{local_time.month}月{local_time.day}日英语试卷{suffix}",
                "date": day_key,
                "recorded_questions": len(group),
                "incorrect": len(incorrect),
                "correct": len(correct),
                "uncertain": len(uncertain),
                "complete_paper": bool(correct),
                "top_focuses": top_focuses,
                "card_ids": [card["id"] for card in group],
            }
        )
    return list(reversed(exams))


def english_focus_memory_items(key: str, cards: list[dict]) -> list[str]:
    text = " ".join(english_card_text(card) for card in cards)
    if key == "spelling_words":
        words = [
            item.get("correct", "")
            for card in cards
            for item in card.get("misspelled_words") or []
            if item.get("correct")
        ]
        return list(dict.fromkeys(words))[:12]
    candidates = {
        "past_tense": [
            ("go", "went"), ("take", "took"), ("meet", "met"), ("feel", "felt"),
            ("have", "had"), ("buy", "bought"), ("make", "made"),
        ],
        "fixed_phrases": [
            ("have a party", "have a party"), ("count to ten", "count to ten"),
            ("look for", "look for"), ("take a trip", "take a trip"),
            ("ride a bike", "ride a bike"), ("fly kites", "fly kites"),
            ("take pictures", "take pictures"), ("do morning exercises", "do morning exercises"),
        ],
    }.get(key, [])
    items = [f"{left} → {right}" if left != right else right for left, right in candidates if left in text or right in text]
    return items[:12]


def english_focus_status(attempts: list[sqlite3.Row]) -> tuple[str, int, int]:
    correct = [row for row in attempts if row["grade_result"] == "correct"]
    incorrect = [row for row in attempts if row["grade_result"] == "incorrect"]
    latest_incorrect = max((str(row["created_at"] or "") for row in incorrect), default="")
    correct_days = {
        parse_card_datetime(str(row["created_at"] or "")).date().isoformat()
        for row in correct
        if str(row["created_at"] or "") > latest_incorrect
    }
    if len(correct_days) >= 2:
        status = "基本掌握"
    elif correct:
        status = "巩固中"
    elif incorrect:
        status = "继续加强"
    else:
        status = "待学习"
    return status, len(correct), len(incorrect)


def build_english_overview(conn: sqlite3.Connection) -> dict:
    cards = list_cards(conn, {"subject": ["英语"]})
    exams = group_english_exams(cards)
    exam_lookup = {
        card_id: exam
        for exam in exams
        for card_id in exam["card_ids"]
    }
    mistake_cards = [card for card in cards if card.get("answer_status") != "correct"]
    correct_cards = [card for card in cards if card.get("answer_status") == "correct"]
    attempts = conn.execute(
        "SELECT * FROM practice_attempts WHERE subject = '英语' ORDER BY created_at DESC"
    ).fetchall()
    focus_definitions = {item["key"]: item for item in ENGLISH_FOCUS_DEFINITIONS}
    focuses: list[dict] = []
    for definition in ENGLISH_FOCUS_DEFINITIONS:
        focus_cards = [
            card for card in mistake_cards if definition["key"] in (card.get("english_focuses") or [])
        ]
        if not focus_cards:
            continue
        card_ids = {card["id"] for card in focus_cards}
        focus_attempts = [row for row in attempts if row["card_id"] in card_ids]
        status, correct_attempts, incorrect_attempts = english_focus_status(focus_attempts)
        exam_count = len({exam_lookup[card["id"]]["id"] for card in focus_cards if card["id"] in exam_lookup})
        score = len(focus_cards) * 12 + exam_count * 8 + incorrect_attempts * 6 - correct_attempts * 2
        if status == "基本掌握":
            score -= 50
        elif status == "巩固中":
            score -= 12
        elif status == "继续加强":
            score += 20
        evidence = []
        for card in focus_cards[:5]:
            exam = exam_lookup.get(card["id"], {})
            evidence.append(
                {
                    "card_id": card["id"],
                    "exam": exam.get("title", "历史英语记录"),
                    "question_number": card.get("question_number") or "",
                    "topic": card.get("topic") or "英语题",
                    "question": card.get("question_text") or "",
                    "user_answer": card.get("user_answer") or "未记录",
                    "correct_answer": card.get("correct_answer") or "未记录",
                }
            )
        focuses.append(
            {
                **definition,
                "category_label": ENGLISH_CATEGORY_DEFINITIONS[definition["category"]],
                "mistake_count": len(focus_cards),
                "exam_count": exam_count,
                "status": status,
                "correct_attempts": correct_attempts,
                "incorrect_attempts": incorrect_attempts,
                "priority_score": score,
                "evidence": evidence,
                "memory_items": english_focus_memory_items(definition["key"], focus_cards),
                "card_ids": [card["id"] for card in focus_cards],
            }
        )
    focuses.sort(key=lambda item: (-item["priority_score"], item["label"]))
    for index, focus in enumerate(focuses):
        focus["priority"] = "优先攻坚" if index < 3 else "按计划复习"

    category_rows = []
    for key, label in ENGLISH_CATEGORY_DEFINITIONS.items():
        category_cards = {
            card["id"]
            for card in mistake_cards
            if any(focus_definitions.get(focus, {}).get("category") == key for focus in card.get("english_focuses") or [])
        }
        if category_cards:
            category_rows.append({"key": key, "label": label, "count": len(category_cards)})
    return {
        "total_records": len(cards),
        "mistake_records": len(mistake_cards),
        "correct_records": len(correct_cards),
        "exam_count": len(exams),
        "practice_correct": sum(1 for row in attempts if row["grade_result"] == "correct"),
        "practice_incorrect": sum(1 for row in attempts if row["grade_result"] == "incorrect"),
        "priority_summary": "、".join(item["label"] for item in focuses[:3]),
        "strength_note": (
            "答对题已经纳入整卷判断。"
            if correct_cards
            else "现有旧数据主要保存错题，暂不根据缺失的答对题判断强项；以后新试卷会完整记录。"
        ),
        "categories": category_rows,
        "focuses": focuses,
        "exams": exams,
    }


def build_summary_markdown(summary: dict) -> str:
    lines = ["# 本周学习小结", "", f"- 已提交练习：{summary['attempts']} 次", f"- 我会了：{summary['mastered']} 次", f"- 还要加强：{summary['needs_work']} 次", ""]
    if summary["subjects"]:
        lines.extend(["## 按科目", *[f"- {item['name']}：{item['count']} 次" for item in summary["subjects"]], ""])
    if summary["needs_work_topics"]:
        lines.extend(["## 还要加强的知识点", *[f"- {item['name']}：{item['count']} 次" for item in summary["needs_work_topics"]], ""])
    return "\n".join(lines)


def table_rows_for_backup(conn: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def build_backup(conn: sqlite3.Connection) -> dict:
    return {
        "version": BACKUP_VERSION,
        "exported_at": now_iso(),
        "mistake_cards": table_rows_for_backup(conn, "mistake_cards"),
        "learning_packs": table_rows_for_backup(conn, "learning_packs"),
        "practice_feedback": table_rows_for_backup(conn, "practice_feedback"),
        "practice_attempts": table_rows_for_backup(conn, "practice_attempts"),
        "selected_questions": table_rows_for_backup(conn, "selected_questions"),
    }


def restore_backup(conn: sqlite3.Connection, backup: dict) -> dict:
    if not isinstance(backup, dict) or not isinstance(backup.get("mistake_cards"), list):
        raise ValueError("备份文件不正确")
    counts: dict[str, int] = {}
    for table in (
        "mistake_cards",
        "learning_packs",
        "practice_feedback",
        "practice_attempts",
        "selected_questions",
    ):
        rows = backup.get(table) or []
        if not isinstance(rows, list):
            raise ValueError("备份文件内容不正确")
        allowed = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        restored = 0
        for row in rows:
            identity = "card_id" if table == "learning_packs" else "id"
            if not isinstance(row, dict) or not row.get(identity):
                continue
            columns = [name for name in row if name in allowed]
            if not columns:
                continue
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                [row[name] for name in columns],
            )
            restored += 1
        counts[table] = restored
    conn.commit()
    return counts


def build_stats(cards: list[dict]) -> dict:
    weak_counter: Counter[str] = Counter()
    subject_counter: Counter[str] = Counter()
    grammar_counter: Counter[str] = Counter()
    spelling_counter: Counter[str] = Counter()
    record_type_counter: Counter[str] = Counter()
    learning_counter: Counter[str] = Counter()
    due_count = 0
    current = now_iso()
    for card in cards:
        subject_counter[card["subject"] or "未标注"] += 1
        record_type_counter[card.get("record_type_label") or "错题"] += 1
        if card.get("answer_status") == "correct":
            continue
        learning_counter[card.get("learning", {}).get("status") or "pending"] += 1
        for item in card["knowledge_points"]:
            weak_counter[item] += 1
        if card["unit"]:
            weak_counter[f"{card['subject']} / {card['unit']}"] += 1
        for item in card["grammar_errors"]:
            grammar_counter[item] += 1
        for item in card["misspelled_words"]:
            wrong = item.get("wrong") or ""
            correct = item.get("correct") or ""
            if wrong:
                spelling_counter[f"{wrong} -> {correct}".strip()] += 1
        if not card["next_review_at"] or card["next_review_at"] <= current:
            due_count += 1
    return {
        "total": len(cards),
        "due": due_count,
        "subjects": [{"name": name, "count": count} for name, count in subject_counter.most_common()],
        "record_types": [{"name": name, "count": count} for name, count in record_type_counter.most_common()],
        "learning_ready": learning_counter["ready"],
        "learning_pending": learning_counter["pending"] + learning_counter["generating"] + learning_counter["failed"],
        "learning_incomplete": learning_counter["incomplete"],
        "weak_points": [{"name": name, "count": count} for name, count in weak_counter.most_common(12)],
        "grammar": [{"name": name, "count": count} for name, count in grammar_counter.most_common(8)],
        "spelling": [{"name": name, "count": count} for name, count in spelling_counter.most_common(8)],
    }


def export_cards(cards: list[dict], export_type: str, fmt: str) -> tuple[bytes, str, str]:
    title = {"questions": "错题重做", "answers": "答案批改", "full": "完整错题本"}.get(export_type, "完整错题本")
    if fmt == "csv":
        output = io.StringIO()
        fields = ["记录类型", "科目", "单元", "主题", "题型", "题目", "孩子答案", "正确答案", "错因", "知识点强化", "费曼解释", "下次复习"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for card in cards:
            writer.writerow(row_for_export(card, export_type))
        return output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", f"{title}.csv"
    if fmt == "json":
        return json.dumps(cards_for_export(cards, export_type), ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", f"{title}.json"
    markdown = build_markdown(cards, export_type, title)
    return markdown.encode("utf-8"), "text/markdown; charset=utf-8", f"{title}.md"


def row_for_export(card: dict, export_type: str) -> dict:
    base = {
        "记录类型": card.get("record_type_label") or "错题",
        "科目": card["subject"],
        "单元": card["unit"],
        "主题": card["topic"],
        "题型": card["question_type"],
        "题目": card["question_text"],
        "孩子答案": "",
        "正确答案": "",
        "错因": "",
        "知识点强化": "",
        "费曼解释": "",
        "下次复习": readable_date(card["next_review_at"]),
    }
    if export_type in {"answers", "full"}:
        base["孩子答案"] = card["user_answer"]
        base["正确答案"] = card["correct_answer"]
        base["错因"] = card["wrong_reason"]
    if export_type == "full":
        base["费曼解释"] = card["feynman_explain"]
        base["知识点强化"] = card.get("learning", {}).get("pack", {}).get("knowledge_summary", "")
    return base


def cards_for_export(cards: list[dict], export_type: str) -> list[dict]:
    return [row_for_export(card, export_type) for card in cards]


def build_markdown(cards: list[dict], export_type: str, title: str) -> str:
    lines = [f"# {title}", ""]
    for index, card in enumerate(cards, 1):
        lines.append(f"## {index}. {card['subject']} - {card['topic']}")
        lines.append(f"- 记录类型：{card.get('record_type_label') or '错题'}")
        if card["unit"]:
            lines.append(f"- 单元：{card['unit']}")
        if card["question_type"]:
            lines.append(f"- 题型：{card['question_type']}")
        if card["question_text"]:
            lines.extend(["", "### 题目", card["question_text"]])
        if export_type in {"answers", "full"}:
            lines.extend(["", "### 答案", f"- 孩子答案：{card['user_answer'] or '未记录'}", f"- 正确答案：{card['correct_answer'] or '未记录'}"])
            if card["wrong_reason"]:
                lines.extend(["", "### 错因", card["wrong_reason"]])
        if export_type == "full":
            learning_pack = card.get("learning", {}).get("pack", {})
            if learning_pack.get("knowledge_summary"):
                lines.extend(["", "### 知识点强化", learning_pack["knowledge_summary"]])
                if learning_pack.get("self_check"):
                    lines.append(f"- 自检提醒：{learning_pack['self_check']}")
            if card["feynman_explain"]:
                lines.extend(["", "### 我来讲一遍", card["feynman_explain"]])
            if card["feynman_stuck"]:
                lines.extend(["", "### 还不确定", card["feynman_stuck"]])
        lines.append("")
    return "\n".join(lines)


def build_print_html(cards: list[dict], export_type: str) -> str:
    title = {"questions": "错题重做", "answers": "答案批改", "full": "完整错题本"}.get(export_type, "完整错题本")
    sections = []
    for index, card in enumerate(cards, 1):
        answer = ""
        feynman = ""
        learning = ""
        if export_type in {"answers", "full"}:
            answer = f"""
            <h3>答案</h3>
            <p><strong>孩子答案：</strong>{esc(card["user_answer"] or "未记录")}</p>
            <p><strong>正确答案：</strong>{esc(card["correct_answer"] or "未记录")}</p>
            <p><strong>错因：</strong>{esc(card["wrong_reason"] or "未记录")}</p>
            """
        if export_type == "full":
            pack = card.get("learning", {}).get("pack", {})
            if pack.get("knowledge_summary"):
                methods = "".join(f"<li>{esc(item)}</li>" for item in pack.get("core_method") or [])
                learning = f"""
                <h3>知识点强化</h3>
                <p>{esc(pack.get("knowledge_summary", ""))}</p>
                <ul>{methods}</ul>
                <p><strong>自检：</strong>{esc(pack.get("self_check", ""))}</p>
                """
            feynman = f"""
            <h3>费曼笔记</h3>
            <p>{esc(card["feynman_explain"] or "未记录")}</p>
            <p><strong>还不确定：</strong>{esc(card["feynman_stuck"] or "无")}</p>
            """
        sections.append(
            f"""
            <section>
              <h2>{index}. {esc(card["subject"])} - {esc(card["topic"])}</h2>
              <p class="meta">{esc(card.get("record_type_label") or "错题")} · {esc(card["unit"])} {esc(card["question_type"])}</p>
              <h3>题目</h3>
              <p>{esc(card["question_text"] or "未记录题干")}</p>
              {answer}
              {learning}
              {feynman}
            </section>
            """
        )
    return PRINT_TEMPLATE.replace("{{title}}", esc(title)).replace("{{sections}}", "\n".join(sections))


def selected_question_figure(question: dict) -> str:
    caption = esc(question.get("diagram_caption") or "题目图形")
    svg = sanitize_diagram_svg(question.get("diagram_svg"))
    if svg:
        return f'<figure class="question-figure">{svg}<figcaption>{caption}</figcaption></figure>'
    diagram = question.get("diagram") or {}
    if diagram.get("kind") == "source_image":
        card_id = quote(str(diagram.get("card_id") or question.get("source_card_id") or ""))
        if card_id:
            return (
                '<figure class="question-figure">'
                f'<img src="/api/cards/{card_id}/image" alt="{caption}">'
                f'<figcaption>{caption}</figcaption></figure>'
            )
    return ""


def build_selected_print_html(questions: list[dict], export_type: str) -> str:
    answers_only = export_type == "answers"
    title = "自选考题答案" if answers_only else "自选考题"
    if not questions:
        sections = '<div class="empty-print">还没有加入自选考题。</div>'
    else:
        blocks = []
        for index, question in enumerate(questions, 1):
            figure = "" if answers_only else selected_question_figure(question)
            if answers_only:
                answer = esc(question.get("answer_text") or "暂无参考答案")
                body = f'<div class="answer"><h3>参考答案</h3><p>{answer}</p></div>'
            else:
                body = '<div class="answer-space"><span>答：</span></div>'
            blocks.append(
                f"""
                <section class="exam-question">
                  <div class="question-head">
                    <h2>{index}. {esc(question.get("topic") or "练习题")}</h2>
                    <span>{esc(question.get("subject") or "未标科目")}</span>
                  </div>
                  <p class="question-text">{esc(question.get("prompt_text") or "")}</p>
                  {figure}
                  {body}
                </section>
                """
            )
        sections = "\n".join(blocks)
    return (
        SELECTED_PRINT_TEMPLATE.replace("{{title}}", esc(title))
        .replace("{{subtitle}}", f"共 {len(questions)} 道题")
        .replace("{{sections}}", sections)
    )


def generate_selected_pdf(
    base_url: str,
    export_type: str,
    *,
    output_dir: Path = EMAIL_EXPORT_DIR,
    popen_factory=subprocess.Popen,
) -> Path:
    if export_type not in {"questions", "answers"}:
        raise ValueError("PDF类型不正确")
    if not CHROME_BINARY.is_file():
        raise RuntimeError("这台电脑没有找到可用的 Chrome，暂时无法生成 PDF")
    output_dir.mkdir(parents=True, exist_ok=True)
    label = "题目" if export_type == "questions" else "答案"
    filename = f"自选考题-{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"
    output_path = output_dir / filename
    print_url = f"{base_url.rstrip('/')}/print-selected?type={quote(export_type)}"
    command = [
        str(CHROME_BINARY),
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={output_path}",
        print_url,
    ]
    stdout = ""
    stderr = ""
    with tempfile.TemporaryDirectory(prefix="homework-pdf-") as profile_dir:
        command.insert(-2, f"--user-data-dir={profile_dir}")
        process = popen_factory(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        previous_size = -1
        stable_checks = 0
        while time.monotonic() < deadline:
            if output_path.is_file() and output_path.stat().st_size >= 100:
                current_size = output_path.stat().st_size
                stable_checks = stable_checks + 1 if current_size == previous_size else 0
                previous_size = current_size
                if stable_checks >= 2 and output_path.read_bytes()[:5] == b"%PDF-":
                    break
            if process.poll() is not None:
                break
            time.sleep(0.25)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, AttributeError):
                process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                process.kill()
            stdout, stderr = process.communicate(timeout=5)
    if not output_path.is_file():
        detail = (stderr or stdout or "PDF生成失败").strip()
        raise RuntimeError(detail[-800:])
    if output_path.stat().st_size < 100 or output_path.read_bytes()[:5] != b"%PDF-":
        raise RuntimeError("生成的 PDF 文件不完整")
    return output_path


def load_mail_config(*, environ: dict[str, str] | None = None) -> dict[str, str | int]:
    values = dict(os.environ if environ is None else environ)
    required = (
        "HOMEWORK_NOTEBOOK_SMTP_HOST",
        "HOMEWORK_NOTEBOOK_SMTP_USER",
        "HOMEWORK_NOTEBOOK_SMTP_PASSWORD",
        "HOMEWORK_NOTEBOOK_MAIL_FROM",
    )
    if not all(str(values.get(key) or "").strip() for key in required):
        raise RuntimeError("邮件功能尚未配置；请参考 .env.example 设置邮件服务器信息")
    return {
        "host": str(values["HOMEWORK_NOTEBOOK_SMTP_HOST"]).strip(),
        "port": int(str(values.get("HOMEWORK_NOTEBOOK_SMTP_PORT") or "465")),
        "user": str(values["HOMEWORK_NOTEBOOK_SMTP_USER"]).strip(),
        "password": str(values["HOMEWORK_NOTEBOOK_SMTP_PASSWORD"]).strip(),
        "from": str(values["HOMEWORK_NOTEBOOK_MAIL_FROM"]).strip(),
    }


def connect_smtp(
    host: str,
    port: int,
    *,
    context: ssl.SSLContext | None = None,
    timeout: int = 30,
):
    context = context or ssl.create_default_context()
    return smtplib.SMTP_SSL(host, port, context=context, timeout=timeout)


def send_pdf_with_mailer(
    pdf_path: Path,
    recipient: str,
    subject: str,
    *,
    config_loader=load_mail_config,
    connector=connect_smtp,
) -> None:
    config = config_loader()
    message = EmailMessage()
    message["From"] = str(config["from"])
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content("这是从家庭错题本发送的自选考题 PDF，请查收附件。")
    message.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf_path.name,
    )
    context = ssl.create_default_context()
    with connector(
        str(config["host"]),
        int(config["port"]),
        context=context,
        timeout=30,
    ) as server:
        server.login(str(config["user"]), str(config["password"]))
        server.send_message(message)


def email_selected_questions(
    questions: list[dict],
    export_type: str,
    base_url: str,
    *,
    pdf_generator=generate_selected_pdf,
    mail_sender=send_pdf_with_mailer,
) -> dict:
    if not questions:
        raise ValueError("请先加入自选考题")
    if not SELECTED_EMAIL_RECIPIENT:
        raise RuntimeError("邮件收件人尚未配置；请设置 HOMEWORK_NOTEBOOK_EMAIL_RECIPIENT")
    label = "题目" if export_type == "questions" else "答案"
    pdf_path = pdf_generator(base_url, export_type)
    subject = f"家庭错题本-自选考题-{label}"
    mail_sender(pdf_path, SELECTED_EMAIL_RECIPIENT, subject)
    return {
        "recipient": SELECTED_EMAIL_RECIPIENT,
        "filename": pdf_path.name,
        "type": export_type,
    }


def readable_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def esc(value: str) -> str:
    return html.escape(str(value)).replace("\n", "<br>")


def card_image_file(conn: sqlite3.Connection, card_id: str) -> tuple[Path, str]:
    row = conn.execute("SELECT image_path FROM mistake_cards WHERE id = ?", (card_id,)).fetchone()
    if not row or not str(row["image_path"] or "").strip():
        raise FileNotFoundError("这道题没有原图")
    path = Path(str(row["image_path"])).expanduser()
    if not path.is_file():
        raise FileNotFoundError("原图文件不存在")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise ValueError("原图不是支持的图片格式")
    return path, content_type


def auth_page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · {APP_NAME}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; color: #34405b; background: #fff8e8; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; }}
    .panel {{ width: min(100%, 460px); padding: 28px; border: 2px solid #e8d8b8; border-radius: 20px 20px 15px 20px; background: #fffef8; box-shadow: 0 6px 0 rgba(215,193,149,.48), 0 18px 45px rgba(87,104,132,.1); }}
    .brand {{ display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }}
    .mark {{ width: 48px; height: 48px; display: grid; place-items: center; border-radius: 15px; background: #ffdc70; color: #415377; font-size: 22px; font-weight: 800; }}
    h1 {{ margin: 0; font-size: 24px; }}
    h2 {{ margin: 22px 0 8px; font-size: 17px; }}
    p {{ margin: 8px 0; line-height: 1.65; color: #606a7d; }}
    label {{ display: grid; gap: 7px; margin-top: 16px; font-weight: 650; }}
    input[type=text], input[type=password] {{ width: 100%; min-height: 48px; padding: 10px 12px; border: 2px solid #d9caa9; border-radius: 12px; background: #fff; color: #34405b; font: inherit; font-size: 16px; }}
    input:focus {{ outline: 3px solid #d8ecff; border-color: #5f91c8; }}
    .check {{ display: flex; align-items: center; gap: 9px; font-weight: 500; }}
    .check input {{ width: 19px; height: 19px; }}
    button, .button {{ width: 100%; min-height: 48px; margin-top: 20px; display: grid; place-items: center; border: 0; border-radius: 12px; background: #5f91c8; color: white; font: inherit; font-weight: 750; text-decoration: none; cursor: pointer; }}
    .error {{ padding: 11px 12px; border: 1px solid #e7aaa4; border-radius: 10px; background: #fff0ed; color: #9e3935; }}
    .hint {{ font-size: 13px; color: #747c8d; }}
    .qr {{ display: block; width: min(260px, 100%); margin: 18px auto; padding: 10px; border: 1px solid #eadbbd; border-radius: 14px; background: white; }}
    .secret {{ padding: 12px; border-radius: 10px; background: #edf6ff; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 15px; line-height: 1.7; text-align: center; overflow-wrap: anywhere; user-select: all; }}
    .codes {{ display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 8px; margin: 16px 0; padding: 0; list-style: none; }}
    .codes li {{ padding: 10px 6px; border-radius: 9px; background: #edf6ff; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; text-align: center; user-select: all; }}
    @media (max-width: 480px) {{ .panel {{ padding: 22px 18px; }} .codes {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body><main class="panel"><div class="brand"><div class="mark">学</div><div><h1>{APP_NAME}</h1><p class="hint">默认仅在本机保存</p></div></div>{body}</main></body>
</html>"""


def login_page_html(*, totp_confirmed: bool, error: str = "", locked_seconds: int = 0) -> str:
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    if locked_seconds > 0:
        error_html = f'<p class="error" role="alert">尝试次数过多，请等待约 {max(1, locked_seconds // 60 + 1)} 分钟后再试。</p>'
    otp_field = ""
    if totp_confirmed:
        otp_field = """
        <label>手机动态验证码
          <input type="text" name="otp" inputmode="numeric" autocomplete="one-time-code" maxlength="20" required autofocus placeholder="6 位数字或备用恢复码">
        </label>
        <p class="hint">打开手机验证器查看当前的 6 位数字；手机不在身边时可输入一组备用恢复码。</p>"""
    body = f"""
      <h2>安全登录</h2>
      <p>{'请输入访问码和手机动态验证码。' if totp_confirmed else '首次绑定前，请先用原来的账号和访问码确认身份。'}</p>
      {error_html}
      <form action="/auth/login" method="post">
        <label>用户名<input type="text" name="username" value="{html.escape(AUTH_USERNAME)}" autocomplete="username" required></label>
        <label>访问码<input type="password" name="password" autocomplete="current-password" required {'autofocus' if not totp_confirmed else ''}></label>
        {otp_field}
        <label class="check"><input type="checkbox" name="remember" value="1" checked> 在这台设备上保持登录 30 天</label>
        <button type="submit">继续</button>
      </form>"""
    return auth_page_shell("安全登录", body)


def setup_page_html(secret: str, error: str = "") -> str:
    qr_uri = totp_qr_data_uri(secret)
    qr_html = f'<img class="qr" src="{qr_uri}" alt="手机动态验证码绑定二维码">' if qr_uri else ""
    grouped_secret = " ".join(secret[index : index + 4] for index in range(0, len(secret), 4))
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""
      <h2>绑定手机动态验证码</h2>
      <p>1. 打开 Microsoft Authenticator、Google Authenticator 或其他验证器。</p>
      <p>2. 选择“添加账户”或“扫描二维码”。</p>
      {qr_html}
      <p class="hint">如果无法扫码，可以选择手动输入，并填写下面这串密钥：</p>
      <div class="secret">{html.escape(grouped_secret)}</div>
      <p>3. 输入手机上显示的 6 位数字，完成绑定。</p>
      {error_html}
      <form action="/auth/setup" method="post">
        <label>手机动态验证码<input type="text" name="otp" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required autofocus placeholder="000000"></label>
        <label class="check"><input type="checkbox" name="remember" value="1" checked> 在这台设备上保持登录 30 天</label>
        <button type="submit">完成绑定</button>
      </form>"""
    return auth_page_shell("绑定手机动态验证码", body)


def recovery_codes_page_html(codes: list[str]) -> str:
    code_items = "".join(f"<li>{html.escape(code)}</li>" for code in codes)
    body = f"""
      <h2>绑定成功</h2>
      <p>请现在保存下面的备用恢复码。手机丢失或无法使用时，每个恢复码可以登录一次。</p>
      <ul class="codes">{code_items}</ul>
      <p class="error">离开本页后不会再次显示。建议截图或抄写后放在安全的地方，不要发送给别人。</p>
      <a class="button" href="/">我已保存，进入错题本</a>"""
    return auth_page_shell("绑定成功", body)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "HomeworkNotebook/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json({"status": "ok"})
            return
        if parsed.path == "/login":
            self.handle_login_page()
            return
        if parsed.path == "/auth/setup":
            self.handle_setup_page()
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if not self.ensure_authorized():
            return
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self.send_html(INDEX_HTML)
            elif parsed.path == "/api/cards":
                with connect_db() as conn:
                    self.send_json({"ok": True, "cards": list_cards(conn, params)})
            elif parsed.path == "/api/practice-feedback":
                with connect_db() as conn:
                    self.send_json({"ok": True, "feedback": list_practice_feedback(conn)})
            elif parsed.path == "/api/practice-attempts":
                with connect_db() as conn:
                    self.send_json({"ok": True, "attempts": list_practice_attempts(conn)})
            elif parsed.path == "/api/selected-questions":
                with connect_db() as conn:
                    self.send_json({"ok": True, "questions": list_selected_questions(conn)})
            elif re.fullmatch(r"/api/cards/[^/]+/image", parsed.path):
                card_id = unquote(parsed.path.split("/")[3])
                with connect_db() as conn:
                    image_file, content_type = card_image_file(conn, card_id)
                self.send_inline_bytes(image_file.read_bytes(), content_type)
            elif parsed.path == "/api/stats":
                with connect_db() as conn:
                    cards = list_cards(conn, params)
                    stats = build_stats(cards)
                    stats["available_subjects"] = sorted(
                        {card["subject"] for card in list_cards(conn, {}) if card.get("subject")}
                    )
                    self.send_json({"ok": True, "stats": stats})
            elif parsed.path == "/api/summary":
                with connect_db() as conn:
                    summary = build_learning_summary(conn)
                    if first_param(params, "format") == "md":
                        self.send_bytes(build_summary_markdown(summary).encode("utf-8"), "text/markdown; charset=utf-8", "本周学习小结.md")
                    else:
                        self.send_json({"ok": True, "summary": summary})
            elif parsed.path == "/api/english-overview":
                with connect_db() as conn:
                    self.send_json({"ok": True, "overview": build_english_overview(conn)})
            elif parsed.path == "/api/backup":
                with connect_db() as conn:
                    payload = json.dumps(build_backup(conn), ensure_ascii=False, indent=2).encode("utf-8")
                    self.send_bytes(payload, "application/json; charset=utf-8", "错题本备份.json")
            elif parsed.path == "/api/export":
                with connect_db() as conn:
                    cards = list_cards(conn, params)
                    payload, content_type, filename = export_cards(cards, first_param(params, "type", "full"), first_param(params, "format", "md"))
                    self.send_bytes(payload, content_type, filename)
            elif parsed.path == "/print":
                with connect_db() as conn:
                    cards = list_cards(conn, params)
                    self.send_html(build_print_html(cards, first_param(params, "type", "full")))
            elif parsed.path == "/print-selected":
                with connect_db() as conn:
                    questions = list_selected_questions(conn)
                    self.send_html(
                        build_selected_print_html(
                            questions, first_param(params, "type", "questions")
                        )
                    )
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/auth/login":
            self.handle_login_post()
            return
        if parsed.path == "/auth/setup":
            self.handle_setup_post()
            return
        if parsed.path == "/auth/logout":
            self.redirect(
                "/login",
                cookies=[self.clear_cookie(AUTH_SESSION_COOKIE), self.clear_cookie(AUTH_PREAUTH_COOKIE)],
            )
            return
        if not self.ensure_authorized():
            return
        try:
            if parsed.path == "/api/cards":
                payload = self.read_json()
                with connect_db() as conn:
                    if not payload.get("force_duplicate"):
                        candidates = duplicate_candidates(conn, payload)
                        if candidates:
                            self.send_json({"ok": True, "duplicate_candidates": candidates})
                            return
                    self.send_json({"ok": True, "card": save_card(conn, payload)})
            elif parsed.path == "/api/cards/check-duplicate":
                payload = self.read_json()
                with connect_db() as conn:
                    self.send_json({"ok": True, "duplicate_candidates": duplicate_candidates(conn, payload, str(payload.get("id") or ""))})
            elif parsed.path == "/api/practice-feedback":
                payload = self.read_json()
                with connect_db() as conn:
                    self.send_json({"ok": True, "feedback": save_practice_feedback(conn, payload)})
            elif parsed.path == "/api/practice-attempts":
                payload = self.read_json()
                with connect_db() as conn:
                    self.send_json({"ok": True, "attempt": save_practice_attempt(conn, payload)})
            elif parsed.path == "/api/selected-questions/email":
                payload = self.read_json()
                with connect_db() as conn:
                    questions = list_selected_questions(conn)
                result = email_selected_questions(
                    questions,
                    str(payload.get("type") or "questions"),
                    f"http://127.0.0.1:{self.server.server_port}",
                )
                self.send_json({"ok": True, "email": result})
            elif parsed.path == "/api/selected-questions":
                payload = self.read_json()
                with connect_db() as conn:
                    question, added = save_selected_question(conn, payload)
                    self.send_json({"ok": True, "question": question, "added": added})
            elif re.fullmatch(r"/api/selected-questions/[^/]+/move", parsed.path):
                question_id = unquote(parsed.path.split("/")[3])
                payload = self.read_json()
                with connect_db() as conn:
                    questions = move_selected_question(
                        conn, question_id, str(payload.get("direction") or "")
                    )
                    self.send_json({"ok": True, "questions": questions})
            elif parsed.path == "/api/restore":
                payload = self.read_json()
                with connect_db() as conn:
                    self.send_json({"ok": True, "restored": restore_backup(conn, payload.get("backup") or payload)})
            elif parsed.path.startswith("/api/cards/") and parsed.path.endswith("/learning-pack"):
                card_id = unquote(parsed.path.split("/")[3])
                payload = self.read_json()
                self.send_json(
                    {
                        "ok": True,
                        "learning": enrich_card(
                            card_id,
                            db_path=DB_PATH,
                            force=bool(payload.get("force")),
                            avoid_previous=bool(payload.get("fresh")),
                        ),
                    }
                )
            elif parsed.path.startswith("/api/cards/") and parsed.path.endswith("/review"):
                card_id = unquote(parsed.path.split("/")[3])
                with connect_db() as conn:
                    self.send_json({"ok": True, "card": review_card(conn, card_id)})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def do_PUT(self) -> None:
        if not self.ensure_authorized():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/cards/"):
                card_id = unquote(parsed.path.split("/")[3])
                payload = self.read_json()
                payload["id"] = card_id
                with connect_db() as conn:
                    old = conn.execute("SELECT * FROM mistake_cards WHERE id = ?", (card_id,)).fetchone()
                    if not old:
                        raise KeyError("找不到这条错题")
                    old_card = row_to_card(old)
                    merged = {**old_card, **payload}
                    merged["created_at"] = old_card["created_at"]
                    self.send_json({"ok": True, "card": save_card(conn, merged)})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def do_DELETE(self) -> None:
        if not self.ensure_authorized():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/selected-questions/"):
                question_id = unquote(parsed.path.split("/")[3])
                with connect_db() as conn:
                    delete_selected_question(conn, question_id)
                self.send_json({"ok": True})
            elif parsed.path.startswith("/api/cards/"):
                card_id = unquote(parsed.path.split("/")[3])
                with connect_db() as conn:
                    delete_card(conn, card_id)
                self.send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def read_form(self) -> dict[str, str]:
        length = min(int(self.headers.get("Content-Length") or 0), 16_384)
        values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        return {key: str(items[-1]) for key, items in values.items() if items}

    def auth_state_path(self) -> Path:
        return Path(getattr(self.server, "auth_state_path", AUTH_STATE_PATH))

    def session_secret(self) -> str:
        return str(getattr(self.server, "session_secret", ""))

    def get_cookie(self, name: str) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:  # noqa: BLE001
            return ""
        morsel = cookie.get(name)
        return morsel.value if morsel else ""

    def make_cookie(self, name: str, value: str, max_age: int) -> str:
        return (
            f"{name}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; "
            "SameSite=Strict"
        )

    def clear_cookie(self, name: str) -> str:
        return f"{name}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"

    def redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def has_session(self, purpose: str) -> bool:
        cookie_name = AUTH_SESSION_COOKIE if purpose == "authenticated" else AUTH_PREAUTH_COOKIE
        return verify_session_token(
            self.get_cookie(cookie_name),
            self.session_secret(),
            purpose,
        )

    def client_key(self) -> str:
        return str(self.headers.get("X-Real-IP") or self.client_address[0])

    def login_lock_remaining(self) -> int:
        now = time.time()
        lock = getattr(self.server, "login_attempts_lock", AUTH_STATE_LOCK)
        attempts = getattr(self.server, "login_attempts", {})
        with lock:
            entry = attempts.get(self.client_key()) or {}
            blocked_until = float(entry.get("blocked_until") or 0)
            if blocked_until <= now:
                if blocked_until:
                    attempts.pop(self.client_key(), None)
                return 0
            return max(1, int(blocked_until - now))

    def record_login_failure(self) -> None:
        now = time.time()
        lock = getattr(self.server, "login_attempts_lock", AUTH_STATE_LOCK)
        attempts = getattr(self.server, "login_attempts", {})
        with lock:
            entry = attempts.get(self.client_key()) or {"count": 0, "started_at": now, "blocked_until": 0}
            if now - float(entry.get("started_at") or 0) > LOGIN_LOCK_SECONDS:
                entry = {"count": 0, "started_at": now, "blocked_until": 0}
            entry["count"] = int(entry.get("count") or 0) + 1
            if entry["count"] >= LOGIN_ATTEMPT_LIMIT:
                entry["blocked_until"] = now + LOGIN_LOCK_SECONDS
                entry["count"] = 0
                entry["started_at"] = now
            attempts[self.client_key()] = entry

    def clear_login_failures(self) -> None:
        lock = getattr(self.server, "login_attempts_lock", AUTH_STATE_LOCK)
        attempts = getattr(self.server, "login_attempts", {})
        with lock:
            attempts.pop(self.client_key(), None)

    def handle_login_page(self, error: str = "", status: int = 200) -> None:
        if self.has_session("authenticated"):
            self.redirect("/")
            return
        state = load_auth_state(self.auth_state_path())
        remaining = self.login_lock_remaining()
        self.send_html(
            login_page_html(
                totp_confirmed=bool(state.get("totp_confirmed")),
                error=error,
                locked_seconds=remaining,
            ),
            status=HTTPStatus.TOO_MANY_REQUESTS if remaining else status,
        )

    def handle_login_post(self) -> None:
        remaining = self.login_lock_remaining()
        if remaining:
            self.handle_login_page(status=HTTPStatus.TOO_MANY_REQUESTS)
            return
        form = self.read_form()
        state = load_auth_state(self.auth_state_path())
        access_code = str(getattr(self.server, "access_code", ""))
        credentials_ok = hmac.compare_digest(form.get("username", "").strip().lower(), AUTH_USERNAME) and access_codes_match(
            form.get("password", ""), access_code
        )
        if not credentials_ok:
            self.record_login_failure()
            login_error = (
                "账号、访问码或动态验证码不正确。"
                if state.get("totp_confirmed")
                else "账号或访问码不正确，请重新复制后再试。"
            )
            self.handle_login_page(login_error, status=HTTPStatus.UNAUTHORIZED)
            return
        if not state.get("totp_confirmed"):
            self.clear_login_failures()
            token = make_session_token(self.session_secret(), "preauth", PREAUTH_SESSION_SECONDS)
            self.redirect(
                "/auth/setup",
                cookies=[self.make_cookie(AUTH_PREAUTH_COOKIE, token, PREAUTH_SESSION_SECONDS)],
            )
            return
        otp = form.get("otp", "")
        otp_ok = verify_totp(str(state.get("totp_secret") or ""), otp)
        if not otp_ok and normalize_recovery_code(otp):
            otp_ok = consume_recovery_code(self.auth_state_path(), otp)
        if not otp_ok:
            self.record_login_failure()
            self.handle_login_page("账号、访问码或动态验证码不正确。", status=HTTPStatus.UNAUTHORIZED)
            return
        self.clear_login_failures()
        lifetime = TRUSTED_SESSION_SECONDS if form.get("remember") == "1" else SHORT_SESSION_SECONDS
        token = make_session_token(self.session_secret(), "authenticated", lifetime)
        self.redirect(
            "/",
            cookies=[
                self.make_cookie(AUTH_SESSION_COOKIE, token, lifetime),
                self.clear_cookie(AUTH_PREAUTH_COOKIE),
            ],
        )

    def handle_setup_page(self, error: str = "", status: int = 200) -> None:
        if not self.has_session("preauth"):
            self.redirect("/login")
            return
        state = load_auth_state(self.auth_state_path())
        if state.get("totp_confirmed"):
            self.redirect("/login")
            return
        self.send_html(setup_page_html(str(state["totp_secret"]), error), status=status)

    def handle_setup_post(self) -> None:
        if not self.has_session("preauth"):
            self.redirect("/login")
            return
        remaining = self.login_lock_remaining()
        if remaining:
            self.handle_setup_page(status=HTTPStatus.TOO_MANY_REQUESTS)
            return
        form = self.read_form()
        recovery_codes = confirm_totp_setup(self.auth_state_path(), form.get("otp", ""))
        if not recovery_codes:
            self.record_login_failure()
            self.handle_setup_page("动态验证码不正确，请查看手机上的最新数字后重试。", status=HTTPStatus.UNAUTHORIZED)
            return
        self.clear_login_failures()
        lifetime = TRUSTED_SESSION_SECONDS if form.get("remember") == "1" else SHORT_SESSION_SECONDS
        token = make_session_token(self.session_secret(), "authenticated", lifetime)
        self.send_html(
            recovery_codes_page_html(recovery_codes),
            extra_headers=[
                ("Set-Cookie", self.make_cookie(AUTH_SESSION_COOKIE, token, lifetime)),
                ("Set-Cookie", self.clear_cookie(AUTH_PREAUTH_COOKIE)),
            ],
        )

    def ensure_authorized(self) -> bool:
        access_code = getattr(self.server, "access_code", "")
        if not access_code:
            return True
        if self.has_session("authenticated"):
            return True
        if urlparse(self.path).path.startswith("/api/"):
            self.send_json({"ok": False, "error": "需要重新登录"}, status=HTTPStatus.UNAUTHORIZED)
        else:
            self.redirect("/login")
        return False

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(
        self,
        html_text: str,
        status: int = 200,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, payload: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_inline_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "private, max-age=60")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>家庭错题本</title>
  <style>
    :root {
      --bg: #fff8e8;
      --panel: #fffef8;
      --ink: #34405b;
      --muted: #6d7485;
      --line: #e8d8b8;
      --line-strong: #d7c195;
      --side: #5f91c8;
      --side-soft: #ffdc70;
      --teal: #438f91;
      --teal-soft: #e5f5f3;
      --orange: #ee884b;
      --orange-soft: #fff0e2;
      --green: #6da45a;
      --green-soft: #eaf6e4;
      --red: #c85a55;
      --red-soft: #fff0ed;
      --blue-soft: #edf6ff;
      --yellow-soft: #fff5c9;
      --shadow: 0 5px 0 rgba(215, 193, 149, 0.48), 0 12px 30px rgba(87, 104, 132, 0.07);
      --shadow-soft: 0 3px 0 rgba(232, 216, 184, 0.58);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
      font-size: 16px;
      letter-spacing: 0;
      line-height: 1.5;
    }
    button, input, textarea, select { font: inherit; }
    button, summary { -webkit-tap-highlight-color: transparent; }
    .app { display: grid; grid-template-columns: 228px minmax(0, 1fr); min-height: 100vh; }
    aside {
      background: var(--side);
      color: #fffdf6;
      padding: 24px 16px;
      display: flex;
      flex-direction: column;
      gap: 22px;
      border-right: 3px solid #4f80b6;
      box-shadow: 8px 0 24px rgba(71, 102, 145, 0.09);
      z-index: 2;
    }
    .brand { display: flex; align-items: center; gap: 11px; padding: 0 6px; }
    .brand-mark {
      width: 40px;
      height: 40px;
      display: grid;
      place-items: center;
      flex: 0 0 auto;
      border: 2px solid rgba(255, 255, 255, 0.72);
      border-radius: 12px 12px 9px 12px;
      background: var(--side-soft);
      color: #415377;
      font-size: 20px;
      font-weight: 700;
      box-shadow: 0 4px 0 rgba(63, 101, 145, 0.26);
    }
    .brand strong { display: block; font-size: 19px; line-height: 1.3; font-family: "Kaiti SC", "KaiTi", "STKaiti", sans-serif; }
    .brand span { display: block; margin-top: 2px; color: #edf5ff; font-size: 12px; }
    nav { display: grid; gap: 6px; }
    nav button, .side-action {
      min-height: 44px;
      border: 1px solid transparent;
      border-radius: 12px 12px 9px 12px;
      color: #fffdf6;
      background: transparent;
      text-align: left;
      padding: 10px 13px;
      cursor: pointer;
      transition: background-color 160ms ease, border-color 160ms ease, transform 160ms ease;
    }
    nav button.active { background: var(--side-soft); border-color: #fff0b3; color: #405171; font-weight: 700; box-shadow: 0 3px 0 rgba(65, 91, 128, 0.2); transform: rotate(-0.5deg); }
    nav button:hover, .side-action:hover { background: rgba(255, 255, 255, 0.15); }
    nav button.active:hover { background: var(--side-soft); }
    .side-stats {
      margin-top: auto;
      display: grid;
      gap: 9px;
      padding: 13px;
      border: 2px solid rgba(255, 255, 255, 0.28);
      border-radius: 14px 14px 11px 14px;
      background: rgba(255, 255, 255, 0.13);
      font-size: 13px;
      color: #f5f9ff;
    }
    .side-stats b { color: #fff3b8; }
    .side-action { width: 100%; padding: 8px 0 0; min-height: 44px; color: #fffdf6; }
    main { padding: 28px clamp(20px, 3vw, 40px); display: grid; gap: 20px; align-content: start; min-width: 0; }
    main > *, .view, .panel, .card { min-width: 0; }
    .topbar {
      display: grid;
      grid-template-columns: minmax(210px, 1fr) minmax(110px, 160px) minmax(110px, 160px) minmax(140px, 170px) minmax(140px, 170px) auto;
      gap: 10px;
      align-items: end;
    }
    .page-heading {
      grid-column: 1 / -1;
      display: flex;
      align-items: baseline;
      gap: 14px;
      flex-wrap: wrap;
    }
    .topbar h1 {
      width: max-content;
      max-width: 100%;
      margin: 0 0 5px;
      font-family: "Kaiti SC", "KaiTi", "STKaiti", sans-serif;
      font-size: 34px;
      line-height: 1.15;
      letter-spacing: 0;
      box-shadow: inset 0 -9px 0 rgba(255, 220, 112, 0.72);
    }
    .motivation-quote {
      margin: 0 0 7px;
      color: #62779a;
      font-family: "Kaiti SC", "KaiTi", "STKaiti", sans-serif;
      font-size: 17px;
      font-weight: 600;
      line-height: 1.45;
    }
    .filter-field { display: grid; gap: 4px; min-width: 0; font-size: 12px; color: var(--muted); }
    .search, select, input, textarea {
      width: 100%;
      border: 2px solid var(--line);
      border-radius: 12px 12px 9px 12px;
      background: #fffefb;
      color: var(--ink);
      padding: 10px 12px;
      min-height: 48px;
      transition: border-color 160ms ease, box-shadow 160ms ease, background-color 160ms ease;
    }
    textarea { min-height: 92px; resize: vertical; line-height: 1.5; }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 13px; }
    label span { color: var(--muted); }
    .btn {
      min-height: 44px;
      border: 2px solid transparent;
      border-radius: 12px 12px 9px 12px;
      padding: 10px 14px;
      color: white;
      background: #4d8fca;
      cursor: pointer;
      white-space: nowrap;
      font-weight: 600;
      transition: background-color 160ms ease, border-color 160ms ease, box-shadow 160ms ease, transform 160ms ease;
    }
    .btn.secondary { border-color: var(--line-strong); background: #fffefb; color: #536079; }
    .btn.orange { background: var(--orange); }
    .btn.green { border-color: #b8d7ad; background: var(--green-soft); color: #456b3b; }
    .btn.danger { border-color: #efc1b9; background: var(--red-soft); color: #a54b47; }
    .btn.selected { outline: 3px solid rgba(39, 106, 115, 0.22); box-shadow: inset 0 0 0 2px rgba(255, 255, 255, 0.42); }
    .btn:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn:hover:not(:disabled) { box-shadow: 0 3px 0 rgba(90, 104, 130, 0.15); transform: translateY(-1px); }
    .btn:active:not(:disabled) { transform: translateY(0); box-shadow: none; }
    input:hover, select:hover, textarea:hover { border-color: var(--line-strong); }
    input:focus-visible, select:focus-visible, textarea:focus-visible,
    button:focus-visible, summary:focus-visible {
      outline: 3px solid rgba(77, 143, 202, 0.28);
      outline-offset: 2px;
      border-color: #4d8fca;
    }
    .grid { display: grid; gap: 14px; }
    .two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .four { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .panel {
      background: var(--panel);
      border: 2px solid var(--line);
      border-radius: 19px 19px 15px 19px;
      box-shadow: var(--shadow);
      padding: 21px;
    }
    .panel h2 { width: max-content; max-width: 100%; margin: 0 0 16px; font-family: "Kaiti SC", "KaiTi", "STKaiti", sans-serif; font-size: 23px; letter-spacing: 0; box-shadow: inset 0 -7px 0 rgba(255, 220, 112, 0.56); }
    .cards { display: grid; gap: 14px; }
    .card {
      border: 2px solid var(--line);
      border-radius: 15px 15px 12px 15px;
      background: #fffefb;
      padding: 17px;
      display: grid;
      gap: 12px;
      box-shadow: var(--shadow-soft);
      transition: border-color 160ms ease, box-shadow 160ms ease;
    }
    .card:nth-child(4n + 1) { border-color: #efd59a; }
    .card:nth-child(4n + 2) { border-color: #bcdedb; }
    .card:nth-child(4n + 3) { border-color: #c8d9ed; }
    .card:nth-child(4n + 4) { border-color: #efc8bf; }
    .card:hover { border-color: #9fbfdc; box-shadow: 0 5px 0 rgba(184, 205, 228, 0.5); }
    .card-head { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; }
    .card h3 { margin: 0; font-size: 18px; line-height: 1.4; }
    .meta { color: var(--muted); font-size: 14px; display: flex; gap: 8px; flex-wrap: wrap; }
    .tagrow { display: flex; gap: 6px; flex-wrap: wrap; }
    .tag {
      padding: 4px 8px;
      border-radius: 999px;
      background: var(--teal-soft);
      color: #285f63;
      font-size: 13px;
    }
    .card-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .empty { color: var(--muted); padding: 28px 22px; text-align: center; border: 2px dashed #dfbf67; background: var(--yellow-soft); border-radius: 14px; }
    .view { display: none; }
    .view.active { display: grid; gap: 16px; }
    .stats-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .stat { background: var(--blue-soft); border: 2px solid #c5ddef; border-radius: 15px 15px 12px 15px; padding: 17px; box-shadow: var(--shadow-soft); }
    .stat:nth-child(2n) { background: var(--yellow-soft); border-color: #ead38a; }
    .stat b { display: block; font-size: 28px; line-height: 1.25; color: #4d79ae; font-variant-numeric: tabular-nums; }
    .list { display: grid; gap: 8px; }
    .list-row { display: flex; justify-content: space-between; gap: 10px; border-bottom: 1px solid var(--line); padding: 8px 0; }
    .list-row:last-child { border-bottom: 0; }
    .toast {
      position: fixed;
      right: 20px;
      bottom: 20px;
      max-width: min(420px, calc(100vw - 40px));
      background: #466f9e;
      color: #fff;
      border-radius: 14px 14px 11px 14px;
      padding: 12px 14px;
      display: none;
      box-shadow: var(--shadow);
      z-index: 50;
    }
    .toast.show { display: block; }
    .split-export { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .export-box { border: 2px solid var(--line); border-radius: 15px 15px 12px 15px; padding: 16px; background: #fffefb; display: grid; gap: 10px; box-shadow: var(--shadow-soft); }
    .export-box h3 { margin: 0; font-size: 16px; }
    .inline-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .section-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; }
    .section-head h2 { margin: 0; }
    .subject-tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
    .subject-tabs button {
      min-height: 44px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: #fffefb;
      color: var(--ink);
      padding: 8px 12px;
      cursor: pointer;
    }
    .subject-tabs button.active {
      border-color: #e0bd52;
      background: var(--yellow-soft);
      color: #536079;
    }
    .practice-categories {
      display: flex;
      gap: 18px;
      flex-wrap: wrap;
      margin: -2px 0 16px;
      padding: 11px 2px;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    .practice-categories strong { color: var(--ink); font-size: 14px; }
    .practice-source { color: var(--muted); font-size: 13px; }
    .practice-question { margin: 0; line-height: 1.65; white-space: pre-wrap; }
    .practice-question-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
    .practice-question-row .practice-question { flex: 1; min-width: 0; }
    .selected-add { flex: 0 0 auto; }
    .practice-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .practice-status { color: var(--muted); font-size: 13px; }
    .practice-answer { min-height: 82px; }
    .topbar.focused > :not(.page-heading) { display: none; }
    .single-review-head { align-items: flex-start; }
    .single-review-head > div { min-width: 0; }
    .single-review-head h2 { margin-bottom: 5px; }
    .single-source {
      display: grid;
      gap: 7px;
      padding: 13px 15px;
      border-left: 5px solid var(--teal);
      background: var(--teal-soft);
    }
    .single-source p { margin: 0; line-height: 1.65; white-space: pre-wrap; }
    .single-source .meta { display: flex; }
    .source-figure, .practice-diagram { margin: 4px 0 0; }
    .card-figure {
      width: min(100%, 520px);
      margin: 10px 0 3px;
      padding-left: 12px;
      border-left: 4px solid var(--teal);
    }
    .card-figure img {
      display: block;
      width: 100%;
      max-height: 380px;
      object-fit: contain;
      object-position: left center;
      background: #fff;
      border: 1px solid #c9d8e7;
      border-radius: 6px;
    }
    .card-figure figcaption { margin-top: 6px; color: var(--muted); font-size: 12px; }
    .figure-missing {
      margin: 10px 0 3px;
      padding: 10px 12px;
      border-left: 4px solid #c48443;
      background: #fff7e8;
      color: #765126;
      line-height: 1.55;
    }
    .figure-missing strong { display: block; color: #8a5424; }
    .source-figure img {
      display: block;
      width: min(100%, 460px);
      max-height: 360px;
      object-fit: contain;
      object-position: left center;
      background: #fff;
    }
    .source-figure figcaption, .practice-diagram figcaption {
      margin-top: 7px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .practice-diagram {
      width: min(100%, 560px);
      padding: 10px;
      background: #fbfdff;
      border: 1px solid #c9d8e7;
      border-radius: 8px;
    }
    .practice-diagram svg { display: block; width: 100%; height: auto; }
    .practice-diagram.square svg { aspect-ratio: 6 / 5; }
    .practice-diagram.wide { width: min(100%, 720px); }
    .practice-diagram.wide svg { aspect-ratio: 52 / 27; }
    .practice-diagram.semicircles { width: min(100%, 660px); }
    .practice-diagram.semicircles svg { aspect-ratio: 5 / 3; }
    .practice-diagram.paths { width: min(100%, 560px); }
    .practice-diagram.paths svg { aspect-ratio: 6 / 5; }
    .practice-diagram.source img {
      display: block;
      width: 100%;
      max-height: 500px;
      object-fit: contain;
      background: #fff;
    }
    .single-practice-heading { margin: 18px 0 2px; font-size: 18px; }
    .selected-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .selected-count { color: var(--muted); font-size: 14px; }
    .selected-email-target { flex-basis: 100%; color: var(--muted); font-size: 12px; text-align: right; }
    .selected-card { position: relative; }
    .selected-order { min-width: 34px; height: 34px; display: inline-grid; place-items: center; border-radius: 50%; background: var(--yellow-soft); color: #6d5b24; font-weight: 700; }
    .icon-btn { min-width: 42px; padding: 8px 10px; font-size: 18px; line-height: 1; }
    .answer-result { border-left: 5px solid #739bc4; background: #f5f9fd; padding: 0 15px; line-height: 1.65; overflow: hidden; }
    .answer-result.correct { border-left-color: var(--green); background: #f2faee; }
    .answer-result.incorrect { border-left-color: var(--red); background: #fff5f2; }
    .answer-verdict { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 10px; align-items: center; padding: 13px 0; }
    .answer-mark { font-size: 27px; line-height: 1; font-weight: 800; color: #557da5; text-align: center; }
    .answer-result.correct .answer-mark, .answer-result.correct .answer-verdict strong { color: #4f8042; }
    .answer-result.incorrect .answer-mark, .answer-result.incorrect .answer-verdict strong { color: #b44f49; }
    .answer-verdict strong { display: block; font-size: 18px; }
    .answer-verdict p, .answer-section p { margin: 3px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
    .answer-verdict p { color: var(--muted); font-size: 14px; }
    .answer-section { padding: 12px 0 13px; border-top: 1px solid rgba(100, 120, 145, 0.2); }
    .answer-label { display: block; color: #657087; font-size: 13px; font-weight: 700; margin-bottom: 3px; }
    .answer-result.correct .answer-section { border-top-color: rgba(90, 135, 78, 0.2); }
    .answer-result.incorrect .answer-section { border-top-color: rgba(180, 79, 73, 0.2); }
    .english-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    .english-summary-item { padding: 15px 16px; border-right: 1px solid var(--line); }
    .english-summary-item:last-child { border-right: 0; }
    .english-summary-item span { display: block; color: var(--muted); font-size: 13px; }
    .english-summary-item strong { display: block; margin-top: 3px; font-size: 24px; color: #4d79ae; }
    .english-priority { margin-top: 16px; padding: 14px 16px; border-left: 5px solid var(--orange); background: var(--orange-soft); line-height: 1.65; }
    .english-priority p { margin: 3px 0; }
    .english-categories { display: flex; gap: 9px; flex-wrap: wrap; margin-top: 14px; }
    .english-category-item { display: inline-flex; align-items: center; gap: 7px; padding: 8px 11px; border: 1px solid var(--line); background: #fff; color: var(--muted); font-size: 13px; }
    .english-category-item strong { color: var(--ink); font-size: 16px; }
    .diagnosis-list, .exam-list { display: grid; }
    .diagnosis-row, .exam-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: center; padding: 17px 0; border-top: 1px solid var(--line); }
    .diagnosis-row:first-child, .exam-row:first-child { border-top: 0; }
    .diagnosis-row h3, .exam-row h3 { margin: 0 0 5px; font-size: 17px; }
    .diagnosis-row p, .exam-row p { margin: 5px 0 0; color: var(--muted); }
    .diagnosis-metrics { display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 13px; }
    .diagnosis-metrics strong { color: var(--ink); }
    .priority-label { color: #a5542c; font-weight: 700; }
    .lesson-sheet { display: grid; gap: 0; border-top: 1px solid var(--line); }
    .lesson-section { padding: 17px 0; border-bottom: 1px solid var(--line); }
    .lesson-section h3 { margin: 0 0 9px; font-size: 17px; }
    .lesson-section p { margin: 6px 0; white-space: pre-wrap; line-height: 1.7; }
    .lesson-compare { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .lesson-example { padding: 13px 14px; border-left: 5px solid var(--red); background: var(--red-soft); }
    .lesson-example.correct { border-left-color: var(--green); background: var(--green-soft); }
    .lesson-example span { display: block; margin-bottom: 5px; color: var(--muted); font-size: 13px; }
    .lesson-example strong { overflow-wrap: anywhere; }
    .evidence-list { display: grid; gap: 10px; }
    .evidence-item { padding: 11px 0 0; border-top: 1px dashed var(--line); }
    .evidence-item:first-child { border-top: 0; padding-top: 0; }
    .evidence-item p { margin: 3px 0; }
    .memory-line { padding: 12px 14px; background: var(--yellow-soft); border-left: 5px solid #d5a934; line-height: 1.8; }
    .english-start-actions { justify-content: flex-end; margin-top: 18px; }
    .learning-grid { display: grid; gap: 14px; }
    .learning-block { border: 2px solid #c5ddef; border-radius: 13px; background: var(--blue-soft); padding: 14px 16px; line-height: 1.65; }
    .learning-block p { margin: 5px 0; }
    .learning-block ul { margin: 6px 0 0; padding-left: 22px; }
    .learning-exercises { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .exercise-box { border: 2px solid var(--line); border-radius: 13px; padding: 13px; background: #fffefb; display: grid; gap: 8px; align-content: start; }
    .exercise-box h4 { margin: 0; color: var(--teal); }
    .exercise-box p { margin: 0; white-space: pre-wrap; line-height: 1.55; }
    .status-ready { background: #e6f1e4; color: #3f6539; }
    .status-pending, .status-generating { background: #f5ecd9; color: #815b20; }
    .status-failed { background: #f7e3e3; color: #923f3f; }
    .status-incomplete { background: #ece9e2; color: #655f55; }
    .summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .summary-number { border: 2px solid #c5ddef; border-radius: 13px; padding: 14px; background: var(--blue-soft); box-shadow: var(--shadow-soft); }
    .summary-number:nth-child(2n) { border-color: #ead38a; background: var(--yellow-soft); }
    .summary-number b { display: block; font-size: 24px; color: #4d79ae; font-variant-numeric: tabular-nums; }
	    .backup-input { display: none; }
	    details summary { cursor: pointer; color: #3f719f; font-weight: 700; }
	    .answer-summary {
	      margin: 14px 0 4px;
	      padding: 12px 14px;
	      border-left: 4px solid var(--green);
	      background: #f2faee;
	    }
	    .answer-summary .answer-label { margin-bottom: 5px; color: #4f7047; }
	    .answer-summary strong { display: block; color: #314d2d; font-size: 18px; line-height: 1.65; }
	    .solution-process { margin-top: 18px; }
	    .solution-process > h4 { margin: 0 0 4px; color: var(--ink); font-size: 17px; }
	    .solution-step { padding: 13px 0 15px; border-top: 1px solid rgba(96, 112, 135, 0.18); }
	    .solution-step h5 { margin: 0 0 8px; color: #304f73; font-size: 16px; }
	    .solution-step p { margin: 6px 0; line-height: 1.75; }
	    .solution-step.key-step {
	      margin: 8px 0;
	      padding: 14px 16px 15px;
	      border-top: 0;
	      border-left: 4px solid #4f80b6;
	      background: #f1f7fc;
	    }
	    .solution-step.final-step {
	      margin-top: 5px;
	      padding: 14px 16px;
	      border-top: 0;
	      border-left: 4px solid var(--green);
	      background: #f2faee;
	    }
	    .solution-step.final-step h5 { color: #42643b; }
	    .solution-step.final-step p { color: #314d2d; font-weight: 700; }
	    .answer-section .solution-step:first-child, .exercise-box details .solution-step:first-child { border-top: 0; padding-top: 5px; }
	    .solution-formula {
	      margin: 9px 0;
	      text-align: center;
	      color: #1f2b3a;
	      font-family: Georgia, "Times New Roman", serif;
	      font-size: 21px;
	      line-height: 1.8;
	      overflow-wrap: anywhere;
	    }
	    .math-fraction {
	      display: inline-flex;
	      flex-direction: column;
	      align-items: center;
	      justify-content: center;
	      min-width: 1.25em;
	      margin: 0 0.12em;
	      vertical-align: middle;
	      line-height: 1.05;
	    }
	    .math-fraction > span:first-child { width: 100%; padding: 0 0.12em 0.1em; border-bottom: 1.5px solid currentColor; text-align: center; }
	    .math-fraction > span:last-child { padding-top: 0.1em; }
    @media (min-width: 861px) {
      aside { position: sticky; top: 0; height: 100vh; }
    }
    @media (max-width: 860px) {
      .app { grid-template-columns: 1fr; }
      aside {
        position: sticky;
        top: 0;
        z-index: 30;
        padding: 11px 16px 9px;
        gap: 9px;
        border-right: 0;
        border-bottom: 3px solid #4f80b6;
        box-shadow: 0 8px 22px rgba(71, 102, 145, 0.16);
      }
      .brand { padding: 0 2px; }
      .brand-mark { width: 36px; height: 36px; border-radius: 9px; font-size: 18px; }
      .brand strong { font-size: 17px; }
      nav { display: flex; gap: 6px; overflow-x: auto; margin: 0 -4px; padding: 0 4px 3px; scrollbar-width: none; }
      nav::-webkit-scrollbar { display: none; }
      nav button { flex: 0 0 auto; min-height: 44px; padding: 8px 12px; border-color: rgba(255, 255, 255, 0.24); border-radius: 14px 14px 10px 14px; }
      .side-stats { display: none; }
      main { padding: 18px 16px calc(28px + env(safe-area-inset-bottom)); gap: 16px; }
      .topbar { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
      .page-heading, .topbar .search, #clearFilter { grid-column: 1 / -1; }
      .topbar h1 { font-size: 31px; }
      .two, .three, .four, .stats-grid, .split-export, .summary-grid { grid-template-columns: 1fr; }
      .card-head { display: grid; }
      .english-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .english-summary-item:nth-child(2) { border-right: 0; }
      .english-summary-item:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
    }
    @media (max-width: 520px) {
      .brand span { display: none; }
      .page-heading { display: grid; gap: 2px; }
      .motivation-quote { margin: 0 0 5px; font-size: 15px; }
      .panel { padding: 15px; border-radius: 17px 17px 13px 17px; }
      .card { padding: 14px; }
      .learning-exercises { grid-template-columns: 1fr; }
      .section-head { align-items: flex-start; }
      .single-review-head { flex-direction: column; }
      .practice-question-row { flex-direction: column; }
      .selected-add { align-self: flex-start; }
      .english-summary, .lesson-compare { grid-template-columns: 1fr; }
      .english-summary-item { border-right: 0; border-bottom: 1px solid var(--line); }
      .english-summary-item:last-child { border-bottom: 0; }
      .diagnosis-row, .exam-row { grid-template-columns: 1fr; }
      .diagnosis-row .btn { justify-self: start; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">学</div>
        <div>
          <strong>家庭错题本</strong>
          <span>每天学会一点点</span>
        </div>
      </div>
      <nav>
        <button class="active" data-view="review">复习</button>
        <button data-view="english">英语学习</button>
        <button data-view="practice">综合复习题</button>
        <button data-view="learning">知识加油站</button>
        <button data-view="notebook">错题本</button>
        <button data-view="selected">自选考题 <span id="selectedNavCount">0</span></button>
        <button data-view="weak">学习小结</button>
        <button data-view="export">导出</button>
      </nav>
      <div class="side-stats">
        <div>总记录 <b id="sideTotal">0</b></div>
        <div>待复习 <b id="sideDue">0</b></div>
        <button class="side-action" id="refreshBtn">刷新</button>
        <form action="/auth/logout" method="post">
          <button class="side-action" type="submit">安全退出</button>
        </form>
      </div>
    </aside>
    <main>
      <div class="topbar">
        <div class="page-heading">
          <h1 id="pageTitle">复习</h1>
          <p class="motivation-quote" id="motivationQuote" aria-live="polite"></p>
        </div>
        <input class="search" id="searchInput" placeholder="搜索科目、单元、知识点">
        <select id="subjectFilter"><option value="">全部科目</option></select>
        <select id="recordTypeFilter">
          <option value="">全部类型</option>
          <option value="mistake">错题</option>
          <option value="unsolved">不会做</option>
          <option value="reinforcement">强化学习</option>
        </select>
        <label class="filter-field"><span>开始日期</span><input id="dateFromFilter" type="date" title="包含当天"></label>
        <label class="filter-field"><span>结束日期</span><input id="dateToFilter" type="date" title="包含当天"></label>
        <button class="btn secondary" id="clearFilter">清空</button>
      </div>

      <section class="view active" id="view-review">
        <div class="panel">
          <h2>今天的复习小任务</h2>
          <div class="cards" id="dueCards"></div>
        </div>
      </section>

      <section class="view" id="view-english">
        <div class="panel">
          <div class="section-head">
            <div>
              <h2>英语学情诊断</h2>
              <p class="meta">先看整张试卷暴露的问题，再按知识点集中学习和练习。</p>
            </div>
          </div>
          <div class="english-summary" id="englishSummary"></div>
          <div class="english-priority" id="englishPriority"></div>
          <div class="english-categories" id="englishCategories" aria-label="英语错误分类"></div>
        </div>
        <div class="panel">
          <div class="section-head">
            <div>
              <h2>待攻克知识点</h2>
              <p class="meta">一道错题可以同时归入多个知识点；优先解决反复出现、练习仍答错的内容。</p>
            </div>
          </div>
          <div class="diagnosis-list" id="englishFocusList"></div>
        </div>
        <div class="panel">
          <div class="section-head">
            <div>
              <h2>英语试卷记录</h2>
              <p class="meta">旧试卷以已保存的错题为主；以后新试卷会同时记录答对题，用来判断强项和进步。</p>
            </div>
          </div>
          <div class="exam-list" id="englishExamList"></div>
        </div>
      </section>

      <section class="view" id="view-english-focus">
        <div class="panel">
          <div class="section-head single-review-head">
            <div>
              <div class="practice-source">英语知识点学习</div>
              <h2 id="englishFocusTitle">知识点学习卡</h2>
              <p class="meta" id="englishFocusMeta"></p>
            </div>
            <button class="btn secondary" id="backToEnglish">返回英语学习</button>
          </div>
          <div id="englishLesson"></div>
          <div class="inline-actions english-start-actions">
            <button class="btn orange" id="startEnglishPractice">开始专项练习</button>
          </div>
        </div>
        <div class="panel" id="englishPracticeSection" hidden>
          <div class="section-head">
            <div>
              <h2 id="englishPracticeTitle">专项练习</h2>
              <p class="meta">按基础、变化和应用逐步完成；答错会自动加入“还要加强”。</p>
            </div>
          </div>
          <div class="cards" id="englishFocusPracticeCards"></div>
        </div>
      </section>

      <section class="view" id="view-practice">
        <div class="panel">
          <div class="section-head">
            <h2>数学、英语综合练习</h2>
            <button class="btn secondary" id="shufflePractice">换一批</button>
          </div>
          <div class="subject-tabs" id="practiceSubjects"></div>
          <div class="practice-categories" id="practiceCategories" hidden></div>
          <div class="cards" id="practiceCards"></div>
        </div>
      </section>

      <section class="view" id="view-single">
        <div class="panel">
          <div class="section-head single-review-head">
            <div>
              <div class="practice-source">单题专项复习</div>
              <h2 id="singlePracticeTitle">这道错题的同类练习</h2>
              <p class="meta" id="singlePracticeMeta"></p>
            </div>
            <div class="card-actions">
              <button class="btn orange" id="freshSinglePractice" hidden>再来一组</button>
              <button class="btn secondary" id="backToReview">返回今天任务</button>
            </div>
          </div>
          <div class="single-source" id="singlePracticeSource"></div>
          <h3 class="single-practice-heading" id="singlePracticeHeading">针对练习</h3>
          <div class="cards" id="singlePracticeCards"></div>
        </div>
      </section>

      <section class="view" id="view-learning">
        <div class="panel">
          <div class="section-head">
            <div>
              <h2>知识加油站</h2>
              <p class="meta">每条错题都有知识讲解、自检提醒和三道由易到难的小练习。</p>
            </div>
          </div>
          <div class="learning-grid" id="learningCards"></div>
        </div>
      </section>

      <section class="view" id="view-add">
        <form class="panel grid" id="cardForm">
          <h2 id="formTitle">编辑错题</h2>
          <input type="hidden" id="cardId">
          <div class="grid four">
            <label><span>记录类型</span><select id="recordType" required>
              <option value="mistake">错题</option>
              <option value="unsolved">不会做</option>
              <option value="reinforcement">强化学习</option>
            </select></label>
            <label><span>科目</span><input id="subject" required placeholder="数学 / 英语"></label>
            <label><span>单元</span><input id="unit" placeholder="函数 / 一般过去时"></label>
            <label><span>题型</span><input id="questionType" placeholder="选择题 / 改错题"></label>
          </div>
          <div class="grid two">
            <label><span>主题</span><input id="topic" required placeholder="例如：圆柱侧面积"></label>
            <label><span>知识点</span><input id="knowledgePoints" placeholder="用逗号分开"></label>
          </div>
          <label><span>题目</span><textarea id="questionText" required placeholder="把题目粘贴或概括到这里"></textarea></label>
          <div class="grid two">
            <label><span>孩子答案</span><textarea id="userAnswer"></textarea></label>
            <label><span>正确答案</span><textarea id="correctAnswer"></textarea></label>
          </div>
          <label><span>错因</span><textarea id="wrongReason" placeholder="为什么错：概念不清、步骤漏了、审题错了……"></textarea></label>
          <label><span>讲解摘要</span><textarea id="explanationSummary" placeholder="下次遇到同类题，应该先看什么、怎么做"></textarea></label>
          <div class="grid two">
            <label><span>费曼笔记：用自己的话讲一遍</span><textarea id="feynmanExplain"></textarea></label>
            <label><span>还不确定的地方</span><textarea id="feynmanStuck"></textarea></label>
          </div>
          <div class="grid three">
            <label><span>标签</span><input id="tags" placeholder="易错, 复习重点"></label>
            <label><span>语法错误</span><input id="grammarErrors" placeholder="英语可填：时态错误"></label>
            <label><span>拼写错误</span><input id="misspelledWords" placeholder="wented->went, studing->studying"></label>
          </div>
          <div class="inline-actions">
            <button class="btn" type="submit">保存</button>
            <button class="btn secondary" type="button" id="resetForm">重新填写</button>
          </div>
        </form>
      </section>

      <section class="view" id="view-notebook">
        <div class="panel">
          <h2>全部错题</h2>
          <div class="cards" id="allCards"></div>
        </div>
      </section>

      <section class="view" id="view-selected">
        <div class="panel">
          <div class="section-head">
            <div>
              <h2>自选考题</h2>
              <p class="meta">从单题练习中挑选题目，按当前顺序组成一份试卷。</p>
            </div>
            <div class="selected-toolbar">
              <span class="selected-count" id="selectedCount">0 道题</span>
              <button class="btn orange" id="printSelectedQuestions">题目 PDF</button>
              <button class="btn" id="emailSelectedQuestions">发送题目到邮箱</button>
              <button class="btn secondary" id="printSelectedAnswers">答案 PDF</button>
              <button class="btn" id="emailSelectedAnswers">发送答案到邮箱</button>
              <span class="selected-email-target">邮件功能需在本机单独配置</span>
            </div>
          </div>
          <div class="cards" id="selectedQuestionCards"></div>
        </div>
      </section>

      <section class="view" id="view-weak">
        <div class="stats-grid">
          <div class="stat"><span>总记录</span><b id="statTotal">0</b></div>
          <div class="stat"><span>待复习</span><b id="statDue">0</b></div>
          <div class="stat"><span>科目数</span><b id="statSubjects">0</b></div>
          <div class="stat"><span>强化内容已准备</span><b id="statLearningReady">0</b></div>
        </div>
        <div class="panel">
          <div class="section-head">
            <h2>本周学习情况</h2>
            <button class="btn secondary" id="exportSummary">导出本周小结</button>
          </div>
          <div class="summary-grid">
            <div class="summary-number"><span>已提交练习</span><b id="weekAttempts">0</b></div>
            <div class="summary-number"><span>我会了</span><b id="weekMastered">0</b></div>
            <div class="summary-number"><span>还要加强</span><b id="weekNeedsWork">0</b></div>
          </div>
          <div class="grid two" style="margin-top: 14px;">
            <div><h3>按科目</h3><div class="list" id="weekSubjects"></div></div>
            <div><h3>还要加强的知识点</h3><div class="list" id="weekWeakPoints"></div></div>
          </div>
        </div>
        <div class="grid two">
          <div class="panel"><h2>最值得再练的知识点</h2><div class="list" id="weakList"></div></div>
          <div class="panel"><h2>科目分布</h2><div class="list" id="subjectList"></div></div>
          <div class="panel"><h2>记录类型</h2><div class="list" id="recordTypeList"></div></div>
          <div class="panel"><h2>英语语法</h2><div class="list" id="grammarList"></div></div>
          <div class="panel"><h2>拼写错误</h2><div class="list" id="spellingList"></div></div>
        </div>
      </section>

      <section class="view" id="view-export">
        <div class="split-export">
          <div class="export-box">
            <h3>只导出题目</h3>
            <p class="meta">给孩子重新做，答案不放在一起。</p>
            <div class="inline-actions">
              <button class="btn" data-export="questions" data-format="md">Markdown</button>
              <button class="btn secondary" data-export="questions" data-format="csv">CSV</button>
              <button class="btn orange" data-print="questions">PDF</button>
            </div>
          </div>
          <div class="export-box">
            <h3>只导出答案</h3>
            <p class="meta">给家长批改用，和题目分开。</p>
            <div class="inline-actions">
              <button class="btn" data-export="answers" data-format="md">Markdown</button>
              <button class="btn secondary" data-export="answers" data-format="csv">CSV</button>
              <button class="btn orange" data-print="answers">PDF</button>
            </div>
          </div>
          <div class="export-box">
            <h3>完整错题本</h3>
            <p class="meta">归档用，包含费曼笔记。</p>
            <div class="inline-actions">
              <button class="btn" data-export="full" data-format="md">Markdown</button>
              <button class="btn secondary" data-export="full" data-format="csv">CSV</button>
              <button class="btn orange" data-print="full">PDF</button>
            </div>
          </div>
          <div class="export-box">
            <h3>备份与恢复</h3>
            <p class="meta">备份会保存错题、复习答案和学习结果；恢复时会合并回现在的记录。</p>
            <div class="inline-actions">
              <button class="btn" id="downloadBackup">下载备份</button>
              <button class="btn secondary" id="chooseRestore">恢复备份</button>
              <input class="backup-input" id="restoreFile" type="file" accept="application/json,.json">
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const state = { cards: [], stats: {}, summary: {}, englishOverview: {}, englishFocusKey: '', englishFocusQuestions: [], practiceFeedback: {}, practiceAttempts: {}, practiceEditing: {}, selectedQuestions: [], visiblePractice: [], singlePracticeCardId: '', singlePracticeQuestions: [], singlePracticeReturnView: 'review', view: 'review', practiceSubject: '', practiceSeed: 0, learningBusy: {} };
    const $ = (id) => document.getElementById(id);

    const ENGLISH_CATEGORY_DEFINITIONS = [
      { key: 'spelling', label: '单词拼写' },
      { key: 'tense', label: '时态与动词变化' },
      { key: 'verb_form', label: '动词形式与固定搭配' },
      { key: 'sentence_pattern', label: '句型与用词' },
      { key: 'pronunciation', label: '语音与重音' },
      { key: 'expression', label: '书面表达与信息完整' }
    ];

    const ENGLISH_SPELLING_WORDS = [
      { word: 'which', meaning: '哪一个', wrong: 'whitch' },
      { word: 'witch', meaning: '女巫', wrong: 'wicht' },
      { word: 'Friday', meaning: '星期五', wrong: 'Firday' },
      { word: 'river', meaning: '河流', wrong: 'rivre' },
      { word: 'fridge', meaning: '冰箱', wrong: 'frige' },
      { word: 'skate', meaning: '滑冰（动词）', wrong: 'skeat' },
      { word: 'skating', meaning: '滑冰（动名词）', wrong: 'skeating' }
    ];

    function englishCategory(card) {
      const text = [
        card.topic, card.question_text, card.correct_answer, card.wrong_reason,
        ...(card.knowledge_points || []), ...(card.grammar_errors || [])
      ].filter(Boolean).join(' ').toLowerCase();
      if (/(发音|重音|音节|音标)/.test(text)) return 'pronunciation';
      if (/(一般过去时|一般现在时|一般将来时|过去式|第三人称单数|\bdid(?:n't)?\b|\bwill\b)/.test(text)) return 'tense';
      if ((card.misspelled_words || []).some(item => item?.correct) || /(拼写|错写|漏写字母|which与witch)/.test(text)) return 'spelling';
      if (/(情态动词|动词原形|动词不定式|动词-ing|doing sth|be good at|like doing)/.test(text)) return 'verb_form';
      if (/(there be|there was|some与any|no和not|物主代词|人称代词|不可数名词|固定搭配|the next day)/.test(text)) return 'sentence_pattern';
      return 'expression';
    }

    function englishCategoryDefinition(key) {
      return ENGLISH_CATEGORY_DEFINITIONS.find(item => item.key === key) || ENGLISH_CATEGORY_DEFINITIONS.at(-1);
    }

    function spellingTargetsFromCard(card) {
      const targets = [];
      for (const item of card.misspelled_words || []) {
        const correct = String(item?.correct || '').trim();
        if (!correct) continue;
        const known = ENGLISH_SPELLING_WORDS.find(entry => entry.word.toLowerCase() === correct.toLowerCase());
        targets.push({
          ...(known || { word: correct, meaning: '', wrong: String(item?.wrong || '').trim() }),
          wrong: String(item?.wrong || '').trim() || known?.wrong || '',
          card
        });
      }
      const focusedText = [
        String(card.topic || '').includes('拼写') ? card.topic : '',
        ...(card.knowledge_points || []).filter(item => /拼写|which与witch/.test(String(item))),
        /拼写|错写|少写|漏写/.test(String(card.wrong_reason || '')) ? card.wrong_reason : ''
      ].filter(Boolean).join(' ').toLowerCase();
      for (const known of ENGLISH_SPELLING_WORDS) {
        if (focusedText.includes(known.word.toLowerCase())) targets.push({ ...known, card });
      }
      const unique = new Map();
      for (const target of targets) unique.set(target.word.toLowerCase(), target);
      return [...unique.values()];
    }

    function englishSpellingTargets(cards) {
      const unique = new Map();
      for (const card of cards) {
        for (const target of spellingTargetsFromCard(card)) {
          if (!unique.has(target.word.toLowerCase())) unique.set(target.word.toLowerCase(), target);
        }
      }
      return [...unique.values()];
    }

    function spellingSkeleton(word) {
      let hidden = false;
      const value = [...word].map((letter, index) => {
        if (index > 0 && /[aeiou]/i.test(letter)) {
          hidden = true;
          return '_';
        }
        return letter;
      }).join('');
      if (hidden) return value;
      const middle = Math.max(1, Math.floor(word.length / 2));
      return word.slice(0, middle) + '_' + word.slice(middle + 1);
    }

    function buildSpellingPractice(cards, limit = 3, varied = false, offset = 0) {
      const targets = englishSpellingTargets(cards);
      if (!targets.length) return [];
      return Array.from({ length: Math.min(limit, varied ? limit : targets.length) }, (_, index) => {
        const target = targets[(index + offset) % targets.length];
        const variant = varied ? index % 3 : 0;
        const prompt = variant === 0
          ? `根据中文提示拼写单词：${target.meaning || '本次易错词'} ______（${target.word.length}个字母，${target.word[0]}开头）`
          : variant === 1
            ? `改正错词：${target.wrong || spellingSkeleton(target.word)} → ______`
            : `补全缺失的字母：${spellingSkeleton(target.word)}`;
        const hint = target.meaning
          ? `这个词表示“${target.meaning}”，注意容易写错或漏写的字母。`
          : `这个词共 ${target.word.length} 个字母。`;
        return {
          source_id: target.card.id,
          subject: '英语',
          topic: `单词拼写 · ${target.word}`,
          source: `${cards.length} 道单词拼写错题`,
          prompt,
          answer: target.word,
          hint,
          diagram: null,
          grouped: true,
          category_key: 'spelling'
        };
      });
    }

    function params(extra = {}) {
      const q = $('searchInput').value.trim();
      const subject = $('subjectFilter').value;
      const recordType = $('recordTypeFilter').value;
      const dateFrom = $('dateFromFilter').value;
      const dateTo = $('dateToFilter').value;
      const p = new URLSearchParams();
      if (q) p.set('q', q);
      if (subject) p.set('subject', subject);
      if (recordType) p.set('record_type', recordType);
      if (dateFrom) p.set('date_from', dateFrom);
      if (dateTo) p.set('date_to', dateTo);
      for (const [key, value] of Object.entries(extra)) {
        if (value) p.set(key, value);
      }
      return p.toString();
    }

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || '操作失败');
      return data;
    }

    async function load() {
      const cardData = await api('/api/cards?' + params());
      state.cards = cardData.cards;
      const statsData = await api('/api/stats?' + params());
      state.stats = statsData.stats;
      const feedbackData = await api('/api/practice-feedback');
      state.practiceFeedback = indexPracticeFeedback(feedbackData.feedback || []);
      const attemptData = await api('/api/practice-attempts');
      state.practiceAttempts = indexPracticeAttempts(attemptData.attempts || []);
      const selectedData = await api('/api/selected-questions');
      state.selectedQuestions = selectedData.questions || [];
      const summaryData = await api('/api/summary');
      state.summary = summaryData.summary || {};
      const englishData = await api('/api/english-overview');
      state.englishOverview = englishData.overview || {};
      renderAll();
    }

    function renderAll() {
      renderSubjectOptions();
      renderStats();
      renderCards('allCards', state.cards.filter(card => card.answer_status !== 'correct'));
      renderCards('dueCards', getDueCards(state.cards), true);
      renderLists();
      renderSummary();
      renderPractice();
      renderSinglePractice();
      renderSelectedQuestions();
      renderLearning();
      renderEnglishOverview();
      renderEnglishFocus();
    }

    function getDueCards(cards) {
      const current = new Date().toISOString();
      return cards.filter(card => card.answer_status !== 'correct' && (!card.next_review_at || card.next_review_at <= current));
    }

    function renderSubjectOptions() {
      const current = $('subjectFilter').value;
      const subjects = state.stats.available_subjects || [...new Set(state.cards.map(card => card.subject).filter(Boolean))].sort();
      $('subjectFilter').innerHTML = '<option value="">全部科目</option>' + subjects.map(item => `<option ${item === current ? 'selected' : ''}>${escapeHtml(item)}</option>`).join('');
    }

    function renderStats() {
      const stats = state.stats || {};
      $('sideTotal').textContent = stats.total || 0;
      $('sideDue').textContent = stats.due || 0;
      $('statTotal').textContent = stats.total || 0;
      $('statDue').textContent = stats.due || 0;
      $('statSubjects').textContent = (stats.subjects || []).length;
      $('statLearningReady').textContent = stats.learning_ready || 0;
    }

    function renderCards(targetId, cards, dueOnly = false) {
      const target = $(targetId);
      if (!cards.length) {
        target.innerHTML = `<div class="empty">${dueOnly ? '今天没有必须复习的错题' : '还没有错题记录'}</div>`;
        return;
      }
      target.innerHTML = cards.map(card => {
        const learningStatus = card.learning?.status || 'pending';
        const singlePracticeLabel = learningStatus === 'ready'
          ? '单题练习'
          : learningStatus === 'generating' || state.learningBusy[card.id]
            ? '正在生成…'
            : learningStatus === 'incomplete'
              ? '补全后练习'
              : '生成练习';
        return `
        <article class="card">
          <div class="card-head">
            <div>
              <h3>${escapeHtml(card.subject)} · ${escapeHtml(card.topic || '未命名错题')}</h3>
              <div class="meta">
                <span>${escapeHtml(card.record_type_label || '错题')}</span>
                <span>${escapeHtml(card.unit || '未标单元')}</span>
                <span>${escapeHtml(card.question_type || '未标题型')}</span>
                <span>录入 ${formatFullDate(card.created_at) || '未记录'}</span>
                <span>复习 ${card.review_count || 0} 次</span>
                <span>下次 ${formatDate(card.next_review_at) || '今天'}</span>
              </div>
            </div>
            <div class="card-actions">
              <button class="btn orange" ${learningStatus === 'generating' || state.learningBusy[card.id] ? 'disabled' : ''} onclick="openSinglePractice('${card.id}')">${singlePracticeLabel}</button>
              <button class="btn green" onclick="markReviewed('${card.id}')">已复习</button>
              <button class="btn secondary" onclick="editCard('${card.id}')">编辑</button>
              <button class="btn danger" onclick="deleteCard('${card.id}')">删除</button>
            </div>
          </div>
          ${card.question_text ? `<p>${escapeHtml(card.question_text)}</p>` : ''}
          ${renderCardFigure(card)}
          <div class="tagrow">
            <span class="tag status-${escapeHtml(card.learning?.status || 'pending')}">${escapeHtml(learningStatusLabel(card.learning?.status))}</span>
            ${(card.knowledge_points || []).concat(card.tags || []).map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}
          </div>
	          <details>
	            <summary>看看答案和我的讲解</summary>
	            <p><strong>孩子答案：</strong>${escapeHtml(card.user_answer || '未记录')}</p>
	            <div class="answer-summary">
	              <span class="answer-label">正确答案</span>
	              <strong>${renderInlineMath(card.correct_answer || '未记录')}</strong>
	            </div>
	            <div class="solution-process">
	              <h4>解题过程</h4>
	              ${renderSolutionProcess(card.feynman_explain || card.explanation_summary || '未记录')}
	            </div>
	            <p><strong>错因：</strong>${escapeHtml(card.wrong_reason || '未记录')}</p>
	            ${card.learning?.pack?.knowledge_summary ? `<p><strong>知识点强化：</strong>${escapeHtml(card.learning.pack.knowledge_summary)}</p>` : ''}
	          </details>
        </article>
      `}).join('');
    }

    function cardNeedsFigure(card) {
      if (card.subject !== '数学') return false;
      if ((card.tags || []).includes('图形题')) return true;
      const text = [card.topic, card.question_text, ...(card.knowledge_points || [])].filter(Boolean).join(' ');
      return /(如图|图中|右图|根据图片|观察图形|图形见来源|阴影|圆弧|半圆|平面图形面积|立体图形表面积|圆片|圆点|树状图|小棒|火柴棒|第\s*\d+\s*个图形)/.test(text);
    }

    function renderCardFigure(card) {
      if (!cardNeedsFigure(card)) return '';
      if (card.image_available) {
        const url = '/api/cards/' + encodeURIComponent(card.id) + '/image';
        return `<figure class="card-figure"><img src="${url}" alt="${escapeAttr(card.topic || '原题图形')}" loading="lazy"><figcaption>原题图形</figcaption></figure>`;
      }
      return `<div class="figure-missing"><strong>原题图形待补</strong><span>这道题依赖图形，原图片文件目前不可用，补图前不建议直接作答。</span></div>`;
    }

    function renderLists() {
      renderList('weakList', state.stats.weak_points);
      renderList('subjectList', state.stats.subjects);
      renderList('recordTypeList', state.stats.record_types);
      renderList('grammarList', state.stats.grammar);
      renderList('spellingList', state.stats.spelling);
    }

    function renderList(id, items = []) {
      $(id).innerHTML = items.length ? items.map(item => `<div class="list-row"><span>${escapeHtml(item.name)}</span><strong>${item.count}</strong></div>`).join('') : '<div class="empty">暂无数据</div>';
    }

    function renderSummary() {
      const summary = state.summary || {};
      $('weekAttempts').textContent = summary.attempts || 0;
      $('weekMastered').textContent = summary.mastered || 0;
      $('weekNeedsWork').textContent = summary.needs_work || 0;
      renderList('weekSubjects', summary.subjects || []);
      renderList('weekWeakPoints', summary.needs_work_topics || []);
    }

    function englishFocusByKey(key) {
      return (state.englishOverview.focuses || []).find(item => item.key === key);
    }

    function renderEnglishOverview() {
      const overview = state.englishOverview || {};
      const summary = $('englishSummary');
      const priority = $('englishPriority');
      const categories = $('englishCategories');
      const focusList = $('englishFocusList');
      const examList = $('englishExamList');
      if (!summary || !priority || !categories || !focusList || !examList) return;
      summary.innerHTML = [
        ['英语试卷', overview.exam_count || 0],
        ['已整理错题', overview.mistake_records || 0],
        ['专项答对', overview.practice_correct || 0],
        ['专项答错', overview.practice_incorrect || 0]
      ].map(([label, value]) => `<div class="english-summary-item"><span>${label}</span><strong>${value}</strong></div>`).join('');
      priority.innerHTML = overview.mistake_records
        ? `<p><strong>当前优先攻坚：</strong>${escapeHtml(overview.priority_summary || '等待更多练习结果')}</p><p>${escapeHtml(overview.strength_note || '')}</p>`
        : '<p>还没有英语试卷记录。</p>';
      categories.innerHTML = (overview.categories || []).map(item => `
        <span class="english-category-item">${escapeHtml(item.label)} <strong>${item.count}</strong></span>`).join('');
      const focuses = overview.focuses || [];
      focusList.innerHTML = focuses.length ? focuses.map(focus => `
        <div class="diagnosis-row">
          <div>
            <h3>${escapeHtml(focus.label)} <span class="priority-label">${escapeHtml(focus.priority)}</span></h3>
            <div class="diagnosis-metrics">
              <span>来自 <strong>${focus.mistake_count}</strong> 道错题</span>
              <span>涉及 <strong>${focus.exam_count}</strong> 份试卷</span>
              <span>练习 <strong>${focus.correct_attempts} 对 / ${focus.incorrect_attempts} 错</strong></span>
              <span>状态 <strong>${escapeHtml(focus.status)}</strong></span>
            </div>
            <p>${escapeHtml(focus.rule)}</p>
          </div>
          <button class="btn ${focus.priority === '优先攻坚' ? 'orange' : 'secondary'}" onclick="openEnglishFocus('${escapeAttr(focus.key)}')">先学习再练习</button>
        </div>`).join('') : '<div class="empty">还没有可整理的英语知识点</div>';
      const exams = overview.exams || [];
      examList.innerHTML = exams.length ? exams.map(exam => {
        const countText = exam.complete_paper
          ? `共记录 ${exam.recorded_questions} 题 · ${exam.correct} 对 · ${exam.incorrect} 错`
          : `旧数据已整理 ${exam.incorrect} 道错题`;
        const focusesText = (exam.top_focuses || []).length ? `主要问题：${exam.top_focuses.join('、')}` : '暂无错误分类';
        return `<div class="exam-row"><div><h3>${escapeHtml(exam.title)}</h3><p>${escapeHtml(countText)}</p><p>${escapeHtml(focusesText)}</p></div><span class="tag">${exam.complete_paper ? '整卷记录' : '旧错题记录'}</span></div>`;
      }).join('') : '<div class="empty">还没有英语试卷记录</div>';
    }

    function openEnglishFocus(key) {
      if (!englishFocusByKey(key)) return;
      state.englishFocusKey = key;
      state.englishFocusQuestions = [];
      $('englishPracticeSection').hidden = true;
      switchView('english-focus');
      renderEnglishFocus();
    }

    function renderEnglishFocus() {
      const focus = englishFocusByKey(state.englishFocusKey);
      if (!focus) return;
      $('englishFocusTitle').textContent = focus.label;
      $('englishFocusMeta').textContent = `${focus.category_label} · ${focus.mistake_count} 道原错题 · ${focus.status}`;
      const evidence = (focus.evidence || []).map(item => `
        <div class="evidence-item">
          <div class="meta"><span>${escapeHtml(item.exam)}</span><span>${escapeHtml(item.question_number || item.topic)}</span></div>
          <p>${escapeHtml(item.question)}</p>
          <p><strong>原来写：</strong>${escapeHtml(item.user_answer)}　<strong>正确：</strong>${escapeHtml(item.correct_answer)}</p>
        </div>`).join('');
      const memory = (focus.memory_items || []).length
        ? `<div class="lesson-section"><h3>这次一起记住</h3><div class="memory-line">${escapeHtml(focus.memory_items.join('　·　'))}</div></div>`
        : '';
      $('englishLesson').innerHTML = `
        <div class="lesson-sheet">
          <div class="lesson-section"><h3>今天要学什么</h3><p>${escapeHtml(focus.rule)}</p></div>
          <div class="lesson-section"><h3>为什么安排这个知识点</h3><div class="evidence-list">${evidence || '<p>来自近期英语错题。</p>'}</div></div>
          <div class="lesson-section"><h3>正确和错误对比</h3><div class="lesson-compare"><div class="lesson-example"><span>容易这样写错</span><strong>${escapeHtml(focus.wrong_example)}</strong></div><div class="lesson-example correct"><span>应该这样写</span><strong>${escapeHtml(focus.correct_example)}</strong></div></div></div>
          ${memory}
          <div class="lesson-section"><h3>做题时这样检查</h3><p>${escapeHtml(focus.self_check)}</p></div>
        </div>`;
      if (state.englishFocusQuestions.length) {
        $('englishPracticeSection').hidden = false;
        renderEnglishFocusPractice();
      }
    }

    function buildEnglishFocusQuestions(focus) {
      const cards = (focus.card_ids || [])
        .map(id => state.cards.find(card => card.id === id))
        .filter(card => card && card.learning?.status === 'ready')
        .sort(comparePracticePriority);
      if (focus.key === 'spelling_words') {
        return buildSpellingPractice(cards, 6, true, state.practiceSeed).map((item, index) => ({
          ...item,
          topic: `${focus.label} · ${['基础', '变化', '应用'][index % 3]}`,
          grouped: true,
          category_key: focus.key
        }));
      }
      const questions = [];
      const used = new Set();
      const stages = ['基础', '变化', '应用'];
      for (let level = 0; level < 3; level += 1) {
        let stageCount = 0;
        for (const card of cards) {
          const exercise = (card.learning?.pack?.exercises || [])[level];
          if (!exercise || used.has(exercise.question)) continue;
          used.add(exercise.question);
          questions.push({
            ...practiceItem(card, exercise.question, exercise.answer, `${focus.label} · ${stages[level]}`, exercise.hint || '', exercise.diagram || null),
            source: `${focus.mistake_count} 道同类错题`,
            grouped: true,
            category_key: focus.key
          });
          stageCount += 1;
          if (stageCount >= 2) break;
        }
      }
      return questions;
    }

    function startEnglishFocusPractice() {
      const focus = englishFocusByKey(state.englishFocusKey);
      if (!focus) return;
      state.englishFocusQuestions = buildEnglishFocusQuestions(focus);
      $('englishPracticeSection').hidden = false;
      renderEnglishFocusPractice();
      $('englishPracticeSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function renderEnglishFocusPractice() {
      const focus = englishFocusByKey(state.englishFocusKey);
      const questions = state.englishFocusQuestions || [];
      $('englishPracticeTitle').textContent = `${focus?.label || '英语'}专项练习`;
      $('englishFocusPracticeCards').innerHTML = questions.length ? questions.map((item, index) => `
        <article class="card">
          <div class="card-head"><h3>${index + 1}. ${escapeHtml(item.topic)}</h3><span class="tag">英语</span></div>
          <div class="practice-source">归类整理：${escapeHtml(item.source || focus.label)}</div>
          <p class="practice-question">${escapeHtml(item.prompt)}</p>
          ${item.hint ? `<details><summary>需要时看提示</summary><p>${escapeHtml(item.hint)}</p></details>` : ''}
          <div class="practice-action-panel" id="englishFocusPracticeActions${index}">${renderPracticeActions(item, index, 'english')}</div>
        </article>`).join('') : '<div class="empty">这组旧记录还没有准备好练习题，请先到知识加油站生成强化内容。</div>';
    }

    function learningStatusLabel(status) {
      return { ready: '强化内容已生成', generating: '正在生成强化内容', failed: '强化内容生成失败', pending: '等待生成强化内容', incomplete: '资料待补全' }[status] || '等待生成强化内容';
    }

    function renderLearning() {
      const target = $('learningCards');
      const learningCards = state.cards.filter(card => card.answer_status !== 'correct');
      if (!learningCards.length) {
        target.innerHTML = '<div class="empty">还没有学习记录</div>';
        return;
      }
      target.innerHTML = learningCards.map(card => {
        const learning = card.learning || {};
        const pack = learning.pack || {};
        const busy = state.learningBusy[card.id];
        const status = learning.status || 'pending';
        if (status !== 'ready') {
          const incomplete = status === 'incomplete';
          const message = incomplete
            ? '这条旧记录缺少完整题目、知识点或答案。为避免生成错误练习，系统不会自动猜。'
            : status === 'failed'
              ? `上次生成失败：${escapeHtml(learning.error || '未知原因')}`
              : '系统会在后台生成，也可以现在立即生成。';
          return `
            <article class="card">
              <div class="card-head">
                <div>
                  <h3>${escapeHtml(card.subject)} · ${escapeHtml(card.topic || '未命名记录')}</h3>
                  <div class="meta"><span>${escapeHtml(card.record_type_label || '错题')}</span><span>${escapeHtml((card.knowledge_points || []).join('、'))}</span></div>
                </div>
                <span class="tag status-${escapeHtml(status)}">${escapeHtml(learningStatusLabel(status))}</span>
              </div>
              <p class="meta">${message}</p>
              <div>${incomplete
                ? `<button class="btn secondary" onclick="editCard('${card.id}')">补全记录</button>`
                : `<button class="btn" ${busy || status === 'generating' ? 'disabled' : ''} onclick="generateLearning('${card.id}', ${status === 'failed' ? 'true' : 'false'})">${busy || status === 'generating' ? '正在生成…' : '现在生成'}</button>`}
              </div>
            </article>`;
        }
        const methods = (pack.core_method || []).map(item => `<li>${escapeHtml(item)}</li>`).join('');
        const traps = (pack.common_traps || []).map(item => `<li>${escapeHtml(item)}</li>`).join('');
        const exercises = (pack.exercises || []).map(item => `
          <div class="exercise-box">
            <h4>${escapeHtml(item.level || '练习')}</h4>
            <p>${escapeHtml(item.question)}</p>
            ${renderPracticeDiagram(item.diagram, `${card.id}-${item.level || 'practice'}-learning`)}
            ${item.hint ? `<details><summary>看提示</summary><p>${escapeHtml(item.hint)}</p></details>` : ''}
	            <details><summary>看答案</summary>${renderSolutionProcess(item.answer)}</details>
          </div>`).join('');
        return `
          <article class="card">
            <div class="card-head">
              <div>
                <h3>${escapeHtml(card.subject)} · ${escapeHtml(card.topic || '未命名记录')}</h3>
                <div class="meta"><span>${escapeHtml(card.record_type_label || '错题')}</span><span>${escapeHtml((card.knowledge_points || []).join('、'))}</span></div>
              </div>
              <div class="card-actions">
                <span class="tag status-ready">强化内容已生成</span>
                <button class="btn secondary" ${busy ? 'disabled' : ''} onclick="generateLearning('${card.id}', true)">${busy ? '正在生成…' : '重新生成'}</button>
              </div>
            </div>
            <div class="learning-block">
              <p><strong>核心知识：</strong>${escapeHtml(pack.knowledge_summary || '')}</p>
              ${methods ? `<p><strong>解题方法：</strong></p><ul>${methods}</ul>` : ''}
              ${traps ? `<p><strong>常见错误：</strong></p><ul>${traps}</ul>` : ''}
              <p><strong>下次自检：</strong>${escapeHtml(pack.self_check || '')}</p>
            </div>
            <div class="learning-exercises">${exercises}</div>
          </article>`;
      }).join('');
    }

    async function generateLearning(cardId, force = false) {
      if (state.learningBusy[cardId]) return;
      state.learningBusy[cardId] = true;
      renderLearning();
      toast('正在生成知识点强化和相似练习，大约需要几秒钟');
      try {
        await api('/api/cards/' + encodeURIComponent(cardId) + '/learning-pack', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force })
        });
        await load();
        switchView('learning');
        toast('强化内容已经生成');
      } catch (err) {
        await load();
        switchView('learning');
        toast(err.message || '生成失败，请稍后重试');
      } finally {
        delete state.learningBusy[cardId];
        renderLearning();
      }
    }

    function questionsForCard(card) {
      if (!card || card.learning?.status !== 'ready') return [];
      if (card.subject === '英语' && englishCategory(card) === 'spelling') {
        const spellingQuestions = buildSpellingPractice([card], 3, true);
        if (spellingQuestions.length) return spellingQuestions;
      }
      return (card.learning.pack?.exercises || []).map(item => practiceItem(
        card,
        item.question,
        item.answer,
        item.level || '练习',
        item.hint || '',
        item.diagram || null
      ));
    }

    function renderSinglePractice() {
      const card = state.cards.find(item => item.id === state.singlePracticeCardId);
      const source = $('singlePracticeSource');
      const heading = $('singlePracticeHeading');
      const target = $('singlePracticeCards');
      const freshButton = $('freshSinglePractice');
      $('backToReview').textContent = state.singlePracticeReturnView === 'notebook' ? '返回错题本' : '返回今天任务';
      if (!card) {
        freshButton.hidden = true;
        state.singlePracticeQuestions = [];
        $('singlePracticeTitle').textContent = '这道错题的同类练习';
        $('singlePracticeMeta').textContent = '';
        source.innerHTML = '<p>请从今天的复习小任务中选择一道错题。</p>';
        heading.hidden = true;
        target.innerHTML = '';
        return;
      }

      const learningStatus = card.learning?.status || 'pending';
      const busy = Boolean(state.learningBusy[card.id]);
      const spellingCategory = card.subject === '英语' && englishCategory(card) === 'spelling';
      const spellingTargets = spellingCategory ? spellingTargetsFromCard(card) : [];
      const spellingOnly = spellingCategory && spellingTargets.length > 0;
      const spellingWords = spellingTargets.map(item => item.word);
      freshButton.hidden = learningStatus !== 'ready';
      freshButton.disabled = busy;
      freshButton.textContent = busy ? '正在生成…' : '再来一组';
      $('singlePracticeTitle').textContent = spellingOnly
        ? '英语 · 单词拼写'
        : `${card.subject || '未标科目'} · ${card.topic || '未命名错题'}`;
      $('singlePracticeMeta').textContent = spellingOnly
        ? `易错词 · ${spellingWords.join('、')}`
        : [card.unit, card.question_type, ...(card.knowledge_points || [])].filter(Boolean).join(' · ');
      source.innerHTML = spellingOnly
        ? `<span class="answer-label">本次易错词</span><p>${escapeHtml(spellingWords.join('、'))}</p>`
        : `
          <span class="answer-label">原错题</span>
          <p>${escapeHtml(card.question_text || card.topic || '这条记录还没有完整题目。')}</p>
          ${renderSourceFigure(card)}
          ${cardNeedsFigure(card) && !card.image_available ? renderCardFigure(card) : ''}
        `;

      if (learningStatus !== 'ready') {
        state.singlePracticeQuestions = [];
        heading.hidden = true;
        if (learningStatus === 'incomplete') {
          target.innerHTML = `
            <div class="empty">这条错题缺少完整题目、知识点或答案，补全后才能准确出题。<br><br>
              <button class="btn secondary" onclick="editCard('${card.id}')">补全错题</button>
            </div>`;
          return;
        }
        const busy = learningStatus === 'generating' || state.learningBusy[card.id];
        const failed = learningStatus === 'failed';
        target.innerHTML = busy
          ? '<div class="empty">正在根据这道错题生成练习，请稍等…</div>'
          : `<div class="empty">${failed ? '刚才没有生成成功，可以再试一次。' : '正在准备这道错题的练习。'}<br><br>
              <button class="btn" onclick="generateSinglePractice('${card.id}', ${failed ? 'true' : 'false'})">${failed ? '重新生成' : '开始生成'}</button>
            </div>`;
        return;
      }

      state.singlePracticeQuestions = questionsForCard(card);
      heading.hidden = false;
      heading.textContent = card.subject === '英语' && englishCategory(card) === 'spelling'
        ? `这道错题的单词拼写专项 · ${state.singlePracticeQuestions.length} 题`
        : `针对这道错题的 ${state.singlePracticeQuestions.length} 道练习`;
      if (!state.singlePracticeQuestions.length) {
        target.innerHTML = `<div class="empty">这道错题还没有练习题。<br><br><button class="btn" onclick="generateSinglePractice('${card.id}', true)">重新生成</button></div>`;
        return;
      }
      target.innerHTML = state.singlePracticeQuestions.map((item, index) => `
        <article class="card" data-practice-index="${index}">
          <div class="card-head">
            <h3>${index + 1}. ${escapeHtml(item.topic || '同类练习')}</h3>
            <span class="tag">${escapeHtml(item.subject)}</span>
          </div>
          <div class="practice-question-row">
            <p class="practice-question">${escapeHtml(item.prompt)}</p>
            ${renderSelectedAddButton(item, index)}
          </div>
          ${renderPracticeDiagram(item.diagram, `${item.source_id}-${index}-single`)}
          ${item.hint ? `<details><summary>需要时看提示</summary><p>${escapeHtml(item.hint)}</p></details>` : ''}
          <div class="practice-action-panel" id="singlePracticeActions${index}">
            ${renderPracticeActions(item, index, 'single')}
          </div>
        </article>
      `).join('');
    }

    function selectedQuestionKey(sourceId, prompt) {
      return `${sourceId}\n${prompt}`;
    }

    function selectedQuestionExists(item) {
      const key = selectedQuestionKey(item.source_id, item.prompt);
      return state.selectedQuestions.some(question => (
        selectedQuestionKey(question.source_card_id, question.prompt_text) === key
      ));
    }

    function renderSelectedAddButton(item, index) {
      const selected = selectedQuestionExists(item);
      return `<button class="btn ${selected ? 'green' : 'secondary'} selected-add" data-add-selected="${index}" ${selected ? 'disabled' : ''} onclick="addSelectedQuestion(${index})">${selected ? '已加入自选考题' : '加入自选考题'}</button>`;
    }

    async function addSelectedQuestion(index) {
      const item = state.singlePracticeQuestions[index];
      if (!item || selectedQuestionExists(item)) return;
      const card = document.querySelector(`#singlePracticeCards article[data-practice-index="${index}"]`);
      const diagram = card?.querySelector('.practice-diagram');
      const data = await api('/api/selected-questions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_card_id: item.source_id,
          subject: item.subject,
          topic: item.topic,
          prompt_text: item.prompt,
          answer_text: item.answer,
          hint_text: item.hint,
          diagram: item.diagram || {},
          diagram_svg: diagram?.querySelector('svg')?.outerHTML || '',
          diagram_caption: diagram?.querySelector('figcaption')?.textContent || ''
        })
      });
      if (!state.selectedQuestions.some(question => question.id === data.question.id)) {
        state.selectedQuestions.push(data.question);
      }
      const button = card?.querySelector(`[data-add-selected="${index}"]`);
      if (button) {
        button.textContent = '已加入自选考题';
        button.classList.remove('secondary');
        button.classList.add('green');
        button.disabled = true;
      }
      renderSelectedQuestions();
      toast(data.added ? '已加入自选考题' : '这道题已经在自选考题中');
    }

    function renderSelectedQuestions() {
      const questions = state.selectedQuestions || [];
      $('selectedNavCount').textContent = String(questions.length);
      $('selectedCount').textContent = `${questions.length} 道题`;
      $('printSelectedQuestions').disabled = !questions.length;
      $('printSelectedAnswers').disabled = !questions.length;
      $('emailSelectedQuestions').disabled = !questions.length;
      $('emailSelectedAnswers').disabled = !questions.length;
      const target = $('selectedQuestionCards');
      if (!questions.length) {
        target.innerHTML = '<div class="empty">还没有自选考题。请到“单题练习”中挑选合适的题目。</div>';
        return;
      }
      target.innerHTML = questions.map((question, index) => `
        <article class="card selected-card">
          <div class="card-head">
            <div style="display:flex; align-items:center; gap:10px; min-width:0;">
              <span class="selected-order">${index + 1}</span>
              <div>
                <h3>${escapeHtml(question.topic || '练习题')}</h3>
                <div class="practice-source">${escapeHtml(question.subject || '未标科目')}</div>
              </div>
            </div>
            <div class="card-actions">
              <button class="btn secondary icon-btn" title="上移" aria-label="上移" ${index === 0 ? 'disabled' : ''} onclick="moveSelectedQuestion('${question.id}', 'up')">↑</button>
              <button class="btn secondary icon-btn" title="下移" aria-label="下移" ${index === questions.length - 1 ? 'disabled' : ''} onclick="moveSelectedQuestion('${question.id}', 'down')">↓</button>
              <button class="btn danger" onclick="removeSelectedQuestion('${question.id}')">移除</button>
            </div>
          </div>
          <p class="practice-question">${escapeHtml(question.prompt_text)}</p>
          ${renderPracticeDiagram(question.diagram, `${question.source_card_id}-${index}-selected`)}
        </article>
      `).join('');
    }

    async function moveSelectedQuestion(id, direction) {
      const data = await api('/api/selected-questions/' + encodeURIComponent(id) + '/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ direction })
      });
      state.selectedQuestions = data.questions || [];
      renderSelectedQuestions();
    }

    async function removeSelectedQuestion(id) {
      await api('/api/selected-questions/' + encodeURIComponent(id), { method: 'DELETE' });
      state.selectedQuestions = state.selectedQuestions.filter(question => question.id !== id);
      renderSelectedQuestions();
      toast('已从自选考题中移除');
    }

    function printSelectedQuestions(type) {
      if (!state.selectedQuestions.length) {
        toast('请先加入自选考题');
        return;
      }
      window.open('/print-selected?type=' + encodeURIComponent(type), '_blank');
    }

    async function emailSelectedQuestions(type, button) {
      if (!state.selectedQuestions.length) {
        toast('请先加入自选考题');
        return;
      }
      const label = type === 'questions' ? '题目' : '答案';
      if (!window.confirm(`确定发送${label} PDF吗？收件地址以本机配置为准。`)) return;
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = '正在生成并发送…';
      try {
        const data = await api('/api/selected-questions/email', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type })
        });
        toast(`${label} PDF已发送到 ${data.email.recipient}`);
      } catch (err) {
        toast(err.message || '邮件发送失败');
        window.alert(err.message || '邮件发送失败');
      } finally {
        button.disabled = !state.selectedQuestions.length;
        button.textContent = originalText;
      }
    }

    async function openSinglePractice(cardId) {
      state.singlePracticeCardId = cardId;
      state.singlePracticeReturnView = state.view === 'notebook' ? 'notebook' : 'review';
      switchView('single');
      renderSinglePractice();
      const card = state.cards.find(item => item.id === cardId);
      const status = card?.learning?.status || 'pending';
      if (status === 'ready' || status === 'incomplete' || status === 'generating') return;
      await generateSinglePractice(cardId, status === 'failed');
    }

    async function generateSinglePractice(cardId, force = false, fresh = false) {
      if (state.learningBusy[cardId]) return;
      state.learningBusy[cardId] = true;
      renderCards('dueCards', getDueCards(state.cards), true);
      renderSinglePractice();
      toast(fresh ? '正在生成新一组练习，大约需要几秒钟' : '正在根据这道错题生成练习，大约需要几秒钟');
      try {
        await api('/api/cards/' + encodeURIComponent(cardId) + '/learning-pack', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force, fresh })
        });
        await load();
        switchView('single');
        toast(fresh ? '新一组练习已经准备好了' : '单题练习已经准备好了');
      } catch (err) {
        await load();
        switchView('single');
        toast(err.message || '生成失败，请稍后重试');
      } finally {
        delete state.learningBusy[cardId];
        renderAll();
      }
    }

    function renderPractice() {
      const questions = buildPracticeQuestions(state.cards);
      const subjects = [...new Set(questions.map(item => item.subject).filter(Boolean))].sort();
      if (!subjects.length) {
        $('practiceSubjects').innerHTML = '';
        $('practiceCategories').hidden = true;
        $('practiceCategories').innerHTML = '';
        $('practiceCards').innerHTML = '<div class="empty">还没有可生成练习的错题</div>';
        return;
      }
      if (!state.practiceSubject || !subjects.includes(state.practiceSubject)) state.practiceSubject = subjects[0];
      $('practiceSubjects').innerHTML = subjects.map(subject => {
        const count = Math.min(questions.filter(item => item.subject === subject).length, practiceLimit(subject));
        const active = subject === state.practiceSubject ? 'active' : '';
        return `<button class="${active}" onclick="setPracticeSubject('${escapeAttr(subject)}')">${escapeHtml(subject)} ${count}题</button>`;
      }).join('');
      const subjectQuestions = questions.filter(item => item.subject === state.practiceSubject);
      const offset = subjectQuestions.length ? state.practiceSeed % subjectQuestions.length : 0;
      const rotated = subjectQuestions.slice(offset).concat(subjectQuestions.slice(0, offset));
      const visible = rotated.slice(0, practiceLimit(state.practiceSubject));
      state.visiblePractice = visible;
      renderPracticeCategories(state.practiceSubject);
      $('practiceCards').innerHTML = visible.map((item, index) => `
        <article class="card">
          <div class="card-head">
            <div>
              <h3>${index + 1}. ${escapeHtml(item.topic || '同类练习')}</h3>
              <div class="practice-source">${item.grouped ? '归类整理：' : '来自错题：'}${escapeHtml(item.source || item.topic || '未命名错题')}</div>
            </div>
            <span class="tag">${escapeHtml(item.subject)}</span>
          </div>
          <p class="practice-question">${escapeHtml(item.prompt)}</p>
          ${renderPracticeDiagram(item.diagram, `${item.source_id}-${index}-practice`)}
          ${item.hint ? `<details><summary>需要时看提示</summary><p>${escapeHtml(item.hint)}</p></details>` : ''}
          <div class="practice-action-panel" id="practiceActions${index}">
            ${renderPracticeActions(item, index)}
          </div>
        </article>
      `).join('');
    }

    function renderPracticeCategories(subject) {
      const target = $('practiceCategories');
      if (subject !== '英语') {
        target.hidden = true;
        target.innerHTML = '';
        return;
      }
      const cards = state.cards.filter(card => card.subject === '英语' && card.learning?.status === 'ready');
      const groups = new Map(ENGLISH_CATEGORY_DEFINITIONS.map(item => [item.key, []]));
      for (const card of cards) groups.get(englishCategory(card)).push(card);
      const spellingWords = englishSpellingTargets(groups.get('spelling')).map(item => item.word);
      target.innerHTML = ENGLISH_CATEGORY_DEFINITIONS
        .filter(item => groups.get(item.key).length)
        .map(item => {
          const detail = item.key === 'spelling' && spellingWords.length
            ? `<br>易错词：${escapeHtml(spellingWords.join('、'))}`
            : '';
          return `<span><strong>${escapeHtml(item.label)} ${groups.get(item.key).length} 道</strong>${detail}</span>`;
        }).join('');
      target.hidden = false;
    }

    function practiceLimit(subject) {
      return subject === '数学' ? 10 : 10;
    }

    function practiceItems(mode = 'practice') {
      if (mode === 'single') return state.singlePracticeQuestions;
      if (mode === 'english') return state.englishFocusQuestions;
      return state.visiblePractice;
    }

    function practiceDomPrefix(mode = 'practice') {
      if (mode === 'single') return 'singlePractice';
      if (mode === 'english') return 'englishFocusPractice';
      return 'practice';
    }

    function renderPracticeActions(item, index, mode = 'practice') {
      const key = practiceKey(item.source_id, item.prompt);
      const prefix = practiceDomPrefix(mode);
      const feedback = state.practiceFeedback[key];
      const attempt = state.practiceAttempts[key];
      const mastered = feedback && feedback.result === 'mastered';
      const needsWork = feedback && feedback.result === 'needs_work';
      const status = feedback ? `<span class="practice-status">已记录：${mastered ? '我会了' : '还要加强'}</span>` : '';
      if (!attempt || state.practiceEditing[key]) {
        const existing = attempt ? escapeHtml(attempt.answer_text) : '';
        return `
          <label><span>写下答案或解题思路</span><textarea class="practice-answer" id="${prefix}Answer${index}" placeholder="先自己做一遍，再提交查看答案">${existing}</textarea></label>
          <div class="practice-actions"><button class="btn" onclick="submitPracticeAnswer(${index}, '${mode}')">提交答案</button></div>
        `;
      }
      const grade = attempt.grade_result || 'ungraded';
      const correct = grade === 'correct';
      const incorrect = grade === 'incorrect';
      const gradeClass = correct ? 'correct' : (incorrect ? 'incorrect' : 'ungraded');
      const gradeMark = correct ? '✓' : (incorrect ? '!' : '·');
      const gradeTitle = correct ? '答对了' : (incorrect ? '答错了' : '已经提交');
      const gradeNote = attempt.grade_feedback || (correct ? '最终答案正确。' : '请对照下面的答案检查。');
      return `
        <div class="answer-result ${gradeClass}" role="status" aria-live="polite">
          <div class="answer-verdict">
            <span class="answer-mark" aria-hidden="true">${gradeMark}</span>
            <div><strong>${gradeTitle}</strong><p>${escapeHtml(gradeNote)}</p></div>
          </div>
          <div class="answer-section">
            <span class="answer-label">你的答案</span>
            <p>${escapeHtml(attempt.answer_text)}</p>
          </div>
	          <div class="answer-section">
	            <span class="answer-label">正确答案和思路</span>
	            ${renderSolutionProcess(item.answer || '请对照原错题检查。')}
	          </div>
          ${item.hint ? `<div class="answer-section"><span class="answer-label">关键提醒</span><p>${escapeHtml(item.hint)}</p></div>` : ''}
        </div>
        <div class="practice-actions">
          <button class="btn green ${mastered ? 'selected' : ''}" onclick="recordPractice(${index}, 'mastered', '${mode}')">我会了</button>
          <button class="btn orange ${needsWork ? 'selected' : ''}" onclick="recordPractice(${index}, 'needs_work', '${mode}')">还要加强</button>
          <button class="btn secondary" onclick="editPracticeAnswer(${index}, '${mode}')">重新作答</button>
          ${status}
        </div>
      `;
    }

    function setPracticeSubject(subject) {
      state.practiceSubject = subject;
      state.practiceSeed = 0;
      renderPractice();
    }

    function buildPracticeQuestions(cards) {
      const readyCards = [...cards]
        .filter(card => card.learning?.status === 'ready')
        .sort(comparePracticePriority);
      const regularQuestions = readyCards
        .filter(card => card.subject !== '英语')
        .flatMap(card => {
          const generated = card.learning.pack?.exercises || [];
          return generated.map(item => practiceItem(
            card,
            item.question,
            item.answer,
            `${card.topic || '同类练习'} · ${item.level || '练习'}`,
            item.hint || '',
            item.diagram || null
          ));
        });
      return regularQuestions.concat(buildEnglishReviewQuestions(readyCards.filter(card => card.subject === '英语')));
    }

    function buildEnglishReviewQuestions(cards) {
      const groups = new Map(ENGLISH_CATEGORY_DEFINITIONS.map(item => [item.key, []]));
      for (const card of cards) groups.get(englishCategory(card)).push(card);
      const questions = buildSpellingPractice(groups.get('spelling'), 3, false, state.practiceSeed);
      const limits = {
        tense: 3,
        verb_form: 1,
        sentence_pattern: 1,
        pronunciation: 1,
        expression: 1
      };
      for (const definition of ENGLISH_CATEGORY_DEFINITIONS) {
        const limit = limits[definition.key] || 0;
        if (!limit) continue;
        const group = groups.get(definition.key);
        const seenFocus = new Set();
        for (const card of group) {
          const focus = String((card.knowledge_points || [])[0] || card.topic || definition.label);
          if (seenFocus.has(focus)) continue;
          const exercise = (card.learning?.pack?.exercises || [])[0];
          if (!exercise) continue;
          seenFocus.add(focus);
          questions.push({
            source_id: card.id,
            subject: '英语',
            topic: `${definition.label} · ${focus}`,
            source: `${group.length} 道${definition.label}错题`,
            prompt: exercise.question,
            answer: exercise.answer,
            hint: exercise.hint || '',
            diagram: null,
            grouped: true,
            category_key: definition.key
          });
          if (seenFocus.size >= limit) break;
        }
      }
      if (questions.length < 10) {
        const used = new Set(questions.map(item => item.prompt));
        for (const card of cards) {
          const definition = englishCategoryDefinition(englishCategory(card));
          for (const exercise of card.learning?.pack?.exercises || []) {
            if (used.has(exercise.question)) continue;
            used.add(exercise.question);
            questions.push({
              source_id: card.id,
              subject: '英语',
              topic: `${definition.label} · ${String((card.knowledge_points || [])[0] || card.topic || '专项练习')}`,
              source: `${definition.label}错题合并练习`,
              prompt: exercise.question,
              answer: exercise.answer,
              hint: exercise.hint || '',
              diagram: null,
              grouped: true,
              category_key: definition.key
            });
            if (questions.length >= 10) return questions;
          }
        }
      }
      return questions.slice(0, 10);
    }

    function comparePracticePriority(left, right) {
      return practicePriority(right) - practicePriority(left);
    }

    function practicePriority(card) {
      const records = Object.values(state.practiceFeedback).filter(item => item.card_id === card.id);
      const latest = records.sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
      let score = 0;
      if (latest && latest.result === 'needs_work') score += 100;
      if (!latest) score += 40;
      if (!card.next_review_at || card.next_review_at <= new Date().toISOString()) score += 25;
      score += Math.min(card.review_count || 0, 8);
      return score;
    }

    function cardText(card) {
      return [card.topic, card.question_text, card.correct_answer, card.wrong_reason, ...(card.knowledge_points || [])].join(' ');
    }

    function practiceItem(card, prompt, answer, topic = '', hint = '', diagram = null) {
      return {
        source_id: card.id,
        subject: card.subject || '未标科目',
        topic: topic || card.topic || '同类练习',
        source: card.topic || card.question_text || '错题',
        prompt,
        answer,
        hint,
        diagram
      };
    }

    function renderSourceFigure(card) {
      if (!card?.image_available || !card?.id) return '';
      const url = '/api/cards/' + encodeURIComponent(card.id) + '/image';
      return `<figure class="source-figure"><img src="${url}" alt="原错题图形" loading="eager"><figcaption>原题图形</figcaption></figure>`;
    }

    function diagramNumber(value, fallback) {
      const number = Number(value);
      return Number.isFinite(number) ? number : fallback;
    }

    function renderPracticeDiagram(diagram, key = 'practice') {
      if (!diagram) return '';
      if (diagram.kind === 'source_image' && diagram.card_id) {
        const url = '/api/cards/' + encodeURIComponent(diagram.card_id) + '/image';
        return `<figure class="practice-diagram source"><img src="${url}" alt="这道练习沿用的原题图形" loading="eager"><figcaption>本题沿用原题图形；变化的数字和提问以本题题干为准</figcaption></figure>`;
      }
      if (diagram.kind === 'square_quarter_semicircle') return renderQuarterSemicircleDiagram(diagram, key);
      if (diagram.kind === 'closed_cylinder_strip_net') return renderClosedCylinderStripDiagram(diagram);
      if (diagram.kind === 'coordinate_triangle_scale') return renderCoordinateTriangleDiagram(diagram);
      if (diagram.kind === 'solid_immersion') return renderImmersionDiagram(diagram, key);
      if (diagram.kind === 'overlapping_right_triangles') return renderOverlappingTriangleDiagram(diagram);
      if (diagram.kind === 'right_triangle_semicircles') return renderRightTriangleSemicircleDiagram(diagram, key);
      if (diagram.kind === 'square_cross_paths') return renderSquareCrossPathsDiagram(diagram);
      if (diagram.kind === 'rectangle_with_square') return renderRectangleWithSquareDiagram(diagram);
      if (diagram.kind === 'parallelogram_split_triangles') return renderParallelogramSplitDiagram(diagram);
      if (diagram.kind === 'multi_circle_chain') return renderMultiCircleChainDiagram(diagram);
      if (diagram.kind === 'nested_semicircle_perimeter') return renderNestedSemicircleDiagram(diagram);
      if (diagram.kind === 'equal_area_right_trapezoid') return renderEqualAreaTrapezoidDiagram(diagram);
      if (diagram.kind === 'open_cylinder_net') return renderOpenCylinderNetDiagram(diagram);
      if (diagram.kind === 'cuboid_cylindrical_hole') return renderCuboidHoleDiagram(diagram, key);
      if (diagram.kind !== 'square_quarter_arcs') return '';
      const side = Math.max(1, diagramNumber(diagram.side, 4));
      const radiusA = Math.min(side, Math.max(0.1, diagramNumber(diagram.radius_a, side * 0.7)));
      const radiusD = Math.min(side, Math.max(0.1, diagramNumber(diagram.radius_d, side)));
      const x = 66;
      const y = 30;
      const size = 228;
      const radiusAPx = radiusA / side * size;
      const radiusDPx = radiusD / side * size;
      const safeKey = String(key).replace(/[^a-zA-Z0-9_-]/g, '') || 'practice';
      const squareClip = `square-${safeKey}`;
      const dClip = `d-circle-${safeKey}`;
      const outsideMask = `outside-${safeKey}`;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram square">
          <svg viewBox="0 0 360 300" role="img" aria-label="正方形内由两个四分之一圆形成的阴影面积图">
            <defs>
              <clipPath id="${squareClip}"><rect x="${x}" y="${y}" width="${size}" height="${size}" /></clipPath>
              <clipPath id="${dClip}"><circle cx="${x + size}" cy="${y}" r="${radiusDPx}" /></clipPath>
              <mask id="${outsideMask}" maskUnits="userSpaceOnUse" x="0" y="0" width="360" height="300">
                <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="white" />
                <circle cx="${x}" cy="${y}" r="${radiusAPx}" fill="black" />
                <circle cx="${x + size}" cy="${y}" r="${radiusDPx}" fill="black" />
              </mask>
            </defs>
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="#ffffff" />
            <g clip-path="url(#${squareClip})">
              <circle cx="${x}" cy="${y}" r="${radiusAPx}" fill="#a9cbe8" clip-path="url(#${dClip})" />
              <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="#f2cf83" mask="url(#${outsideMask})" />
              <circle cx="${x}" cy="${y}" r="${radiusAPx}" fill="none" stroke="#405d7d" stroke-width="2.5" />
              <circle cx="${x + size}" cy="${y}" r="${radiusDPx}" fill="none" stroke="#405d7d" stroke-width="2.5" />
            </g>
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="none" stroke="#27384c" stroke-width="3" />
            <g fill="#27384c" font-size="15" font-family="Arial, sans-serif">
              <text x="${x - 18}" y="${y - 8}">A</text>
              <text x="${x - 18}" y="${y + size + 20}">B</text>
              <text x="${x + size + 8}" y="${y + size + 20}">C</text>
              <text x="${x + size + 8}" y="${y - 8}">D</text>
              <text x="${x + size * 0.47}" y="${y + size * 0.31}" font-size="18" font-weight="700">S₁</text>
              <text x="${x + size * 0.2}" y="${y + size * 0.82}" font-size="18" font-weight="700">S₂</text>
            </g>
          </svg>
          <figcaption>边长 ${format(side)} cm；A 圆弧半径 ${format(radiusA)} cm；D 圆弧半径 ${format(radiusD)} cm</figcaption>
        </figure>`;
    }

    function renderQuarterSemicircleDiagram(diagram, key = 'practice') {
      const side = Math.max(1, diagramNumber(diagram.side, 4));
      const x = 72;
      const y = 28;
      const size = 230;
      const bottom = y + size;
      const right = x + size;
      const half = size / 2;
      const safeKey = String(key).replace(/[^a-zA-Z0-9_-]/g, '') || 'practice';
      const quarterPath = `M ${x} ${y} A ${size} ${size} 0 0 0 ${right} ${bottom} L ${right} ${y} Z`;
      const semicirclePath = `M ${x} ${bottom} A ${half} ${half} 0 0 1 ${right} ${bottom} Z`;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram square">
          <svg viewBox="0 0 380 310" role="img" aria-label="正方形内四分之一圆和半圆的阴影面积差图">
            <defs>
              <mask id="quarter-only-${safeKey}">
                <rect width="380" height="310" fill="black" />
                <path d="${quarterPath}" fill="white" />
                <path d="${semicirclePath}" fill="black" />
              </mask>
              <mask id="semicircle-only-${safeKey}">
                <rect width="380" height="310" fill="black" />
                <path d="${semicirclePath}" fill="white" />
                <path d="${quarterPath}" fill="black" />
              </mask>
            </defs>
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="#ffffff" />
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="#a9cbe8" mask="url(#quarter-only-${safeKey})" />
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="#f2cf83" mask="url(#semicircle-only-${safeKey})" />
            <path d="${quarterPath}" fill="none" stroke="#405d7d" stroke-width="3" />
            <path d="${semicirclePath}" fill="none" stroke="#405d7d" stroke-width="3" />
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="none" stroke="#27384c" stroke-width="3" />
            <g fill="#27384c" font-family="Arial, sans-serif">
              <text x="${x - 20}" y="${y - 7}" font-size="15">A</text>
              <text x="${x - 20}" y="${bottom + 20}" font-size="15">B</text>
              <text x="${right + 8}" y="${bottom + 20}" font-size="15">C</text>
              <text x="${right + 8}" y="${y - 7}" font-size="15">D</text>
              <text x="${right - 62}" y="${y + 68}" font-size="20" font-weight="700">S₁</text>
              <text x="${x + 35}" y="${bottom - 28}" font-size="20" font-weight="700">S₂</text>
            </g>
          </svg>
          <figcaption>正方形边长 ${format(side)} cm；圆弧 AC 的圆心是 D；半圆直径是 BC</figcaption>
        </figure>`;
    }

    function renderClosedCylinderStripDiagram(diagram) {
      const radius = Math.max(0.1, diagramNumber(diagram.radius, 4));
      const totalLength = Math.max(0.1, diagramNumber(diagram.total_length, 33.12));
      const height = Math.max(0.1, diagramNumber(diagram.height, radius * 4));
      const totalWidth = 400;
      const stripHeight = 190;
      const x = 48;
      const y = 44;
      const circleColumn = totalWidth * (2 * radius / totalLength);
      const sideWidth = totalWidth - circleColumn;
      const circleRadius = Math.min(circleColumn / 2 - 4, stripHeight / 4 - 5);
      const cx = x + circleColumn / 2;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
          <svg viewBox="0 0 500 300" role="img" aria-label="长方形纸片剪出两个圆和圆柱侧面的展开图">
            <rect x="${x}" y="${y}" width="${totalWidth}" height="${stripHeight}" fill="#f7fafc" stroke="#27384c" stroke-width="3" />
            <line x1="${x + circleColumn}" y1="${y}" x2="${x + circleColumn}" y2="${y + stripHeight}" stroke="#405d7d" stroke-width="2.5" />
            <circle cx="${cx}" cy="${y + stripHeight * 0.25}" r="${circleRadius}" fill="#dcefd3" stroke="#405d7d" stroke-width="2.5" />
            <circle cx="${cx}" cy="${y + stripHeight * 0.75}" r="${circleRadius}" fill="#dcefd3" stroke="#405d7d" stroke-width="2.5" />
            <rect x="${x + circleColumn}" y="${y}" width="${sideWidth}" height="${stripHeight}" fill="#a9cbe8" fill-opacity="0.75" />
            <path d="M ${x} ${y - 16} L ${x + totalWidth} ${y - 16} M ${x} ${y - 22} L ${x} ${y - 10} M ${x + totalWidth} ${y - 22} L ${x + totalWidth} ${y - 10}" stroke="#647589" stroke-width="1.5" />
            <g fill="#27384c" font-family="Arial, sans-serif" font-size="15">
              <text x="${x + totalWidth / 2}" y="${y - 25}" text-anchor="middle">总长 ${format(totalLength)} cm</text>
              <text x="${x + circleColumn + sideWidth / 2}" y="${y + stripHeight / 2}" text-anchor="middle" font-size="17" font-weight="700">圆柱侧面</text>
              <text x="${x + totalWidth / 2}" y="${y + stripHeight + 30}" text-anchor="middle">原纸片宽 = 圆柱高 ${format(height)} cm</text>
            </g>
          </svg>
          <figcaption>左侧两个圆是上、下底面；右侧长方形卷成侧面；底面半径 ${format(radius)} cm</figcaption>
        </figure>`;
    }

    function renderCoordinateTriangleDiagram(diagram) {
      const points = (Array.isArray(diagram.points) ? diagram.points : [])
        .slice(0, 3)
        .map((point, index) => ({
          label: String(point.label || ['A', 'B', 'C'][index]).replace(/[^A-Za-z0-9]/g, ''),
          x: Math.max(0, diagramNumber(point.x, index + 1)),
          y: Math.max(0, diagramNumber(point.y, index + 1)),
        }));
      if (points.length !== 3) return '';
      const scaleRatio = Math.max(1, diagramNumber(diagram.scale, 10000));
      const maxValue = Math.max(6, Math.ceil(Math.max(...points.flatMap(point => [point.x, point.y]))) + 1);
      const left = 65;
      const top = 25;
      const size = 235;
      const step = size / maxValue;
      const px = value => left + value * step;
      const py = value => top + size - value * step;
      const lines = Array.from({length: maxValue + 1}, (_, index) => `
        <path d="M ${px(index)} ${top} L ${px(index)} ${top + size} M ${left} ${py(index)} L ${left + size} ${py(index)}" stroke="#d8e0e8" stroke-width="1" />
        <text x="${px(index)}" y="${top + size + 18}" text-anchor="middle">${index}</text>
        <text x="${left - 12}" y="${py(index) + 5}" text-anchor="middle">${index}</text>`).join('');
      const polygon = points.map(point => `${px(point.x)},${py(point.y)}`).join(' ');
      const labels = points.map(point => `
        <circle cx="${px(point.x)}" cy="${py(point.y)}" r="4.5" fill="#27384c" />
        <text x="${px(point.x) + 8}" y="${py(point.y) - 9}" font-size="16" font-weight="700">${point.label}（${point.x}，${point.y}）</text>`).join('');
      return `
        <figure class="practice-diagram square">
          <svg viewBox="0 0 380 320" role="img" aria-label="带比例尺的坐标方格三角形">
            <g fill="#5d6d80" font-family="Arial, sans-serif" font-size="11">${lines}</g>
            <path d="M ${left} ${top + size} L ${left + size + 18} ${top + size} M ${left} ${top + size} L ${left} ${top - 18}" stroke="#27384c" stroke-width="2.5" />
            <polygon points="${polygon}" fill="#a9cbe8" fill-opacity="0.72" stroke="#405d7d" stroke-width="3" />
            <g fill="#27384c" font-family="Arial, sans-serif">${labels}</g>
          </svg>
          <figcaption>每小格边长 1 cm；比例尺 1∶${scaleRatio}</figcaption>
        </figure>`;
    }

    function renderImmersionDiagram(diagram, key = 'practice') {
      const containerRadius = Math.max(0.1, diagramNumber(diagram.container_radius, 6));
      const solids = (Array.isArray(diagram.solids) ? diagram.solids : []).slice(0, 2);
      const bucketX = 72;
      const bucketY = 48;
      const bucketWidth = 220;
      const bucketHeight = 185;
      const safeKey = String(key).replace(/[^a-zA-Z0-9_-]/g, '') || 'practice';
      const shapes = solids.map((solid, index) => {
        const radius = Math.max(0.1, diagramNumber(solid.radius, 2));
        const height = Math.max(0.1, diagramNumber(solid.height, 6));
        const centerX = bucketX + bucketWidth * (solids.length === 1 ? 0.5 : (0.37 + index * 0.28));
        const baseY = bucketY + bucketHeight - 18;
        const width = Math.max(26, Math.min(70, radius / containerRadius * bucketWidth));
        const drawnHeight = Math.max(62, Math.min(125, height / (containerRadius * 2) * bucketHeight));
        if (solid.kind === 'cone') {
          return `<path d="M ${centerX} ${baseY - drawnHeight} L ${centerX - width / 2} ${baseY} L ${centerX + width / 2} ${baseY} Z" fill="#f2cf83" stroke="#8b642d" stroke-width="2.5" />
            <text x="${centerX}" y="${baseY - drawnHeight / 2}" text-anchor="middle" font-size="13">圆锥</text>`;
        }
        return `<rect x="${centerX - width / 2}" y="${baseY - drawnHeight}" width="${width}" height="${drawnHeight}" fill="#dcefd3" stroke="#4f7251" stroke-width="2.5" />
          <ellipse cx="${centerX}" cy="${baseY - drawnHeight}" rx="${width / 2}" ry="8" fill="#edf7e9" stroke="#4f7251" stroke-width="2.5" />
          <text x="${centerX}" y="${baseY - drawnHeight / 2}" text-anchor="middle" font-size="13">圆柱</text>`;
      }).join('');
      const descriptions = solids.map(solid => `${solid.label || (solid.kind === 'cone' ? '圆锥' : '圆柱')} r=${solid.radius} cm，h=${solid.height} cm`).join('；');
      return `
        <figure class="practice-diagram square">
          <svg viewBox="0 0 380 300" role="img" aria-label="物体完全浸入圆柱形水桶后的水面上升图">
            <defs><clipPath id="bucket-${safeKey}"><rect x="${bucketX}" y="${bucketY}" width="${bucketWidth}" height="${bucketHeight}" /></clipPath></defs>
            <path d="M ${bucketX} ${bucketY} L ${bucketX} ${bucketY + bucketHeight} Q ${bucketX + bucketWidth / 2} ${bucketY + bucketHeight + 24} ${bucketX + bucketWidth} ${bucketY + bucketHeight} L ${bucketX + bucketWidth} ${bucketY}" fill="#ffffff" stroke="#27384c" stroke-width="3" />
            <rect x="${bucketX}" y="${bucketY + 78}" width="${bucketWidth}" height="${bucketHeight - 78}" fill="#a9cbe8" fill-opacity="0.65" clip-path="url(#bucket-${safeKey})" />
            <path d="M ${bucketX} ${bucketY + 78} Q ${bucketX + bucketWidth / 2} ${bucketY + 66} ${bucketX + bucketWidth} ${bucketY + 78}" fill="none" stroke="#477fa8" stroke-width="2.5" />
            <g fill="#27384c" font-family="Arial, sans-serif">${shapes}</g>
            <path d="M ${bucketX - 12} ${bucketY + bucketHeight + 32} L ${bucketX + bucketWidth + 12} ${bucketY + bucketHeight + 32}" stroke="#647589" stroke-width="1.5" />
            <text x="${bucketX + bucketWidth / 2}" y="${bucketY + bucketHeight + 55}" text-anchor="middle" fill="#27384c" font-family="Arial, sans-serif" font-size="15">水桶底面半径 ${containerRadius} cm</text>
          </svg>
          <figcaption>物体完全浸没：${descriptions}</figcaption>
        </figure>`;
    }

    function renderOverlappingTriangleDiagram(diagram) {
      const height = Math.max(1, diagramNumber(diagram.height, 8));
      const shift = Math.max(0.1, diagramNumber(diagram.shift, 6));
      const drop = Math.min(height - 0.1, Math.max(0.1, diagramNumber(diagram.drop, 3)));
      const base = height * shift / drop;
      const scale = Math.min(16, 420 / (base + shift), 150 / height);
      const heightPx = height * scale;
      const basePx = base * scale;
      const shiftPx = shift * scale;
      const dropPx = drop * scale;
      const xA = 48;
      const bottom = 205;
      const top = bottom - heightPx;
      const xE = xA + shiftPx;
      const xC = xA + basePx;
      const xD = xE + basePx;
      const yG = top + dropPx;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
          <svg viewBox="0 0 520 270" role="img" aria-label="两个相同直角三角形叠放形成的阴影面积图">
            <polygon points="${xE},${top} ${xE},${yG} ${xC},${bottom} ${xD},${bottom}" fill="#a9cbe8" />
            <path d="M ${xA} ${top} L ${xA} ${bottom} L ${xC} ${bottom} Z" fill="none" stroke="#27384c" stroke-width="3" />
            <path d="M ${xE} ${top} L ${xE} ${bottom} L ${xD} ${bottom} Z" fill="none" stroke="#27384c" stroke-width="3" />
            <path d="M ${xA - 7} ${top} L ${xA - 7} ${bottom}" stroke="#6b7b8f" stroke-width="1.5" />
            <path d="M ${xA - 12} ${top} L ${xA - 2} ${top} M ${xA - 12} ${bottom} L ${xA - 2} ${bottom}" stroke="#6b7b8f" stroke-width="1.5" />
            <path d="M ${xA} ${bottom + 17} L ${xE} ${bottom + 17}" stroke="#6b7b8f" stroke-width="1.5" />
            <path d="M ${xA} ${bottom + 12} L ${xA} ${bottom + 22} M ${xE} ${bottom + 12} L ${xE} ${bottom + 22}" stroke="#6b7b8f" stroke-width="1.5" />
            <path d="M ${xE + 9} ${top} L ${xE + 9} ${yG}" stroke="#6b7b8f" stroke-width="1.5" />
            <g fill="#27384c" font-size="14" font-family="Arial, sans-serif">
              <text x="${xA - 17}" y="${top - 7}">A</text>
              <text x="${xA - 17}" y="${bottom + 18}">B</text>
              <text x="${xC - 4}" y="${bottom + 18}">C</text>
              <text x="${xD + 7}" y="${bottom + 18}">D</text>
              <text x="${xE - 4}" y="${top - 7}">E</text>
              <text x="${xE - 4}" y="${bottom + 18}">F</text>
              <text x="${xE + 12}" y="${yG + 5}">G</text>
              <text x="${xA - 34}" y="${top + heightPx / 2}">${format(height)}</text>
              <text x="${xA + shiftPx / 2 - 6}" y="${bottom + 38}">${format(shift)}</text>
              <text x="${xE + 14}" y="${top + dropPx / 2 + 5}">${format(drop)}</text>
              <text x="${xE + basePx * 0.48}" y="${top + heightPx * 0.48 + dropPx * 0.5}" text-anchor="middle" font-size="18" font-weight="700">阴影</text>
            </g>
          </svg>
          <figcaption>AB = ${format(height)} cm；BF = ${format(shift)} cm；EG = ${format(drop)} cm；两个直角三角形相同</figcaption>
        </figure>`;
    }

    function renderRightTriangleSemicircleDiagram(diagram, key = 'practice') {
      const legAC = Math.max(0.1, diagramNumber(diagram.leg_ac, 4));
      const legBC = Math.max(0.1, diagramNumber(diagram.leg_bc, 2));
      const scale = Math.min(310 / legAC, 150 / legBC);
      const acPx = legAC * scale;
      const bcPx = legBC * scale;
      const xC = 400;
      const yC = 220;
      const xA = xC - acPx;
      const yB = yC - bcPx;
      const safeKey = String(key).replace(/[^a-zA-Z0-9_-]/g, '') || 'practice';
      const upperClip = `upper-semicircle-${safeKey}`;
      const leftClip = `left-semicircle-${safeKey}`;
      const triangleClip = `triangle-${safeKey}`;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram semicircles">
          <svg viewBox="0 0 500 300" role="img" aria-label="直角三角形两条直角边外画半圆形成的阴影面积图">
            <defs>
              <clipPath id="${upperClip}"><rect x="0" y="0" width="500" height="${yC}" /></clipPath>
              <clipPath id="${leftClip}"><rect x="0" y="0" width="${xC}" height="300" /></clipPath>
              <clipPath id="${triangleClip}"><polygon points="${xA},${yC} ${xC},${yB} ${xC},${yC}" /></clipPath>
            </defs>
            <g clip-path="url(#${upperClip})">
              <circle cx="${(xA + xC) / 2}" cy="${yC}" r="${acPx / 2}" fill="#a9cbe8" />
            </g>
            <polygon points="${xA},${yC} ${xC},${yB} ${xC},${yC}" fill="#ffffff" />
            <g clip-path="url(#${triangleClip})">
              <g clip-path="url(#${leftClip})">
                <circle cx="${xC}" cy="${(yB + yC) / 2}" r="${bcPx / 2}" fill="#f2cf83" />
              </g>
            </g>
            <g clip-path="url(#${upperClip})">
              <circle cx="${(xA + xC) / 2}" cy="${yC}" r="${acPx / 2}" fill="none" stroke="#405d7d" stroke-width="2.5" />
            </g>
            <g clip-path="url(#${leftClip})">
              <circle cx="${xC}" cy="${(yB + yC) / 2}" r="${bcPx / 2}" fill="none" stroke="#405d7d" stroke-width="2.5" />
            </g>
            <polygon points="${xA},${yC} ${xC},${yB} ${xC},${yC}" fill="#ffffff" stroke="#27384c" stroke-width="3" />
            <g clip-path="url(#${triangleClip})">
              <g clip-path="url(#${leftClip})">
                <circle cx="${xC}" cy="${(yB + yC) / 2}" r="${bcPx / 2}" fill="#f2cf83" stroke="none" />
              </g>
            </g>
            <g clip-path="url(#${leftClip})">
              <circle cx="${xC}" cy="${(yB + yC) / 2}" r="${bcPx / 2}" fill="none" stroke="#405d7d" stroke-width="2.5" />
            </g>
            <path d="M ${xA} ${yC} L ${xC} ${yB} L ${xC} ${yC} Z" fill="none" stroke="#27384c" stroke-width="3" />
            <path d="M ${xC - 16} ${yC} L ${xC - 16} ${yC - 16} L ${xC} ${yC - 16}" fill="none" stroke="#6b7b8f" stroke-width="2" />
            <g fill="#27384c" font-size="15" font-family="Arial, sans-serif">
              <text x="${xA - 18}" y="${yC + 21}">A</text>
              <text x="${xC + 10}" y="${yB - 7}">B</text>
              <text x="${xC + 10}" y="${yC + 21}">C</text>
              <text x="${xA + acPx * 0.38}" y="${yC - acPx * 0.36}" text-anchor="middle" font-size="17" font-weight="700">阴影</text>
              <text x="${xC - bcPx * 0.28}" y="${yC - bcPx * 0.2}" text-anchor="middle" font-size="14" font-weight="700">阴影</text>
              <text x="${xA + acPx / 2}" y="${yC + 42}" text-anchor="middle">${format(legAC)}</text>
              <text x="${xC + 27}" y="${yB + bcPx / 2}">${format(legBC)}</text>
            </g>
          </svg>
          <figcaption>AC = ${format(legAC)} cm；BC = ${format(legBC)} cm；∠ACB = 90°</figcaption>
        </figure>`;
    }

    function renderSquareCrossPathsDiagram(diagram) {
      const side = Math.max(1, diagramNumber(diagram.side, 10));
      const pathWidth = Math.min(side / 2 - 0.05, Math.max(0.1, diagramNumber(diagram.path_width, 2)));
      const x = 72;
      const y = 28;
      const size = 240;
      const road = pathWidth / side * size;
      const first = size / 3 - road / 2;
      const second = size * 2 / 3 - road / 2;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram paths">
          <svg viewBox="0 0 400 330" role="img" aria-label="正方形草坪中横竖各两条等宽小路形成的九块草地">
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="#dcefd3" />
            <g fill="#aeb8c4">
              <rect x="${x + first}" y="${y}" width="${road}" height="${size}" />
              <rect x="${x + second}" y="${y}" width="${road}" height="${size}" />
              <rect x="${x}" y="${y + first}" width="${size}" height="${road}" />
              <rect x="${x}" y="${y + second}" width="${size}" height="${road}" />
            </g>
            <rect x="${x}" y="${y}" width="${size}" height="${size}" fill="none" stroke="#27384c" stroke-width="3" />
            <g stroke="#647589" stroke-width="1.5" fill="none">
              <path d="M ${x} ${y + size + 18} L ${x + size} ${y + size + 18}" />
              <path d="M ${x} ${y + size + 12} L ${x} ${y + size + 24} M ${x + size} ${y + size + 12} L ${x + size} ${y + size + 24}" />
              <path d="M ${x + first} ${y - 12} L ${x + first + road} ${y - 12}" />
              <path d="M ${x + first} ${y - 17} L ${x + first} ${y - 7} M ${x + first + road} ${y - 17} L ${x + first + road} ${y - 7}" />
            </g>
            <g fill="#27384c" font-family="Arial, sans-serif" font-size="14">
              <text x="${x + size / 2}" y="${y + size + 42}" text-anchor="middle">边长 ${format(side)} 米</text>
              <text x="${x + first + road / 2}" y="${y - 17}" text-anchor="middle">${format(pathWidth)} 米</text>
              <text x="${x + size / 6}" y="${y + size / 6 + 5}" text-anchor="middle" font-size="13" font-weight="700">草坪</text>
              <text x="${x + size / 2}" y="${y + size / 2 + 5}" text-anchor="middle" font-size="13" font-weight="700">草坪</text>
              <text x="${x + size * 5 / 6}" y="${y + size * 5 / 6 + 5}" text-anchor="middle" font-size="13" font-weight="700">草坪</text>
            </g>
          </svg>
          <figcaption>正方形边长 ${format(side)} 米；横、竖各两条小路，每条宽 ${format(pathWidth)} 米</figcaption>
        </figure>`;
    }

    function renderRectangleWithSquareDiagram(diagram) {
      const length = Math.max(1, diagramNumber(diagram.length, 15));
      const rightPartWidth = Math.min(length - 0.1, Math.max(0.1, diagramNumber(diagram.right_part_width, 11)));
      const squareSide = length - rightPartWidth;
      const scale = Math.min(330 / length, 165 / squareSide);
      const width = length * scale;
      const height = squareSide * scale;
      const x = (470 - width) / 2;
      const y = 74;
      const squareRight = x + height;
      const right = x + width;
      const bottom = y + height;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
          <svg viewBox="0 0 470 300" role="img" aria-label="阴影正方形嵌在长方形左侧的周长题图">
            <rect x="${x}" y="${y}" width="${height}" height="${height}" fill="#a9cbe8" />
            <rect x="${x}" y="${y}" width="${width}" height="${height}" fill="none" stroke="#27384c" stroke-width="3" />
            <path d="M ${x} 42 L ${right} 42 M ${x} 37 L ${x} 47 M ${right} 37 L ${right} 47" stroke="#6b7b8f" stroke-width="1.5" />
            <path d="M ${x} 42 l 8 -5 M ${x} 42 l 8 5 M ${right} 42 l -8 -5 M ${right} 42 l -8 5" stroke="#6b7b8f" stroke-width="1.5" fill="none" />
            <path d="M ${squareRight} ${bottom + 32} L ${right} ${bottom + 32} M ${squareRight} ${bottom + 27} L ${squareRight} ${bottom + 37} M ${right} ${bottom + 27} L ${right} ${bottom + 37}" stroke="#6b7b8f" stroke-width="1.5" />
            <path d="M ${squareRight} ${bottom + 32} l 8 -5 M ${squareRight} ${bottom + 32} l 8 5 M ${right} ${bottom + 32} l -8 -5 M ${right} ${bottom + 32} l -8 5" stroke="#6b7b8f" stroke-width="1.5" fill="none" />
            <g fill="#27384c" font-size="15" font-family="Arial, sans-serif">
              <text x="${x - 20}" y="${y + 5}">A</text>
              <text x="${right + 8}" y="${y + 5}">B</text>
              <text x="${right + 8}" y="${bottom + 5}">C</text>
              <text x="${x - 20}" y="${bottom + 5}">D</text>
              <text x="${(x + right) / 2}" y="31" text-anchor="middle">${format(length)} cm</text>
              <text x="${(squareRight + right) / 2}" y="${bottom + 57}" text-anchor="middle">${format(rightPartWidth)} cm</text>
              <text x="${x + height / 2}" y="${y + height / 2 + 5}" text-anchor="middle" font-size="17" font-weight="700">正方形</text>
            </g>
          </svg>
          <figcaption>长方形长 ${format(length)} cm；阴影部分是正方形；右侧空白部分宽 ${format(rightPartWidth)} cm</figcaption>
        </figure>`;
    }

    function renderParallelogramSplitDiagram(diagram) {
      const totalArea = Math.max(1, diagramNumber(diagram.total_area, 30));
      const leftSegment = Math.max(0.1, diagramNumber(diagram.left_segment, 1));
      const rightSegment = Math.max(0.1, diagramNumber(diagram.right_segment, 2));
      const xA = 120;
      const yTop = 62;
      const xD = 420;
      const xB = 55;
      const yBottom = 232;
      const xC = 355;
      const splitRatio = leftSegment / (leftSegment + rightSegment);
      const xP = xA + (xD - xA) * splitRatio;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
          <svg viewBox="0 0 500 300" role="img" aria-label="平行四边形分成甲乙丙三个三角形的面积比图">
            <polygon points="${xA},${yTop} ${xB},${yBottom} ${xC},${yBottom}" fill="#f2cf83" />
            <polygon points="${xA},${yTop} ${xP},${yTop} ${xC},${yBottom}" fill="#a9cbe8" />
            <polygon points="${xP},${yTop} ${xD},${yTop} ${xC},${yBottom}" fill="#dcefd3" />
            <path d="M ${xA} ${yTop} L ${xD} ${yTop} L ${xC} ${yBottom} L ${xB} ${yBottom} Z" fill="none" stroke="#27384c" stroke-width="3" />
            <path d="M ${xA} ${yTop} L ${xC} ${yBottom} M ${xP} ${yTop} L ${xC} ${yBottom}" fill="none" stroke="#405d7d" stroke-width="2.5" />
            <g stroke="#647589" stroke-width="1.5" fill="none">
              <path d="M ${xA} ${yTop - 22} L ${xP} ${yTop - 22}" />
              <path d="M ${xP} ${yTop - 22} L ${xD} ${yTop - 22}" />
              <path d="M ${xA} ${yTop - 28} L ${xA} ${yTop - 16} M ${xP} ${yTop - 28} L ${xP} ${yTop - 16} M ${xD} ${yTop - 28} L ${xD} ${yTop - 16}" />
            </g>
            <g fill="#27384c" font-family="Arial, sans-serif">
              <text x="${(xA + xP) / 2}" y="${yTop - 31}" text-anchor="middle" font-size="15">${format(leftSegment)} cm</text>
              <text x="${(xP + xD) / 2}" y="${yTop - 31}" text-anchor="middle" font-size="15">${format(rightSegment)} cm</text>
              <text x="${(xA + xB + xC) / 3 - 6}" y="${(yTop + yBottom + yBottom) / 3 + 13}" text-anchor="middle" font-size="22" font-weight="700">甲</text>
              <text x="${(xA + xP + xC) / 3}" y="${(yTop + yTop + yBottom) / 3}" text-anchor="middle" font-size="22" font-weight="700">乙</text>
              <text x="${(xP + xD + xC) / 3 + 7}" y="${(yTop + yTop + yBottom) / 3}" text-anchor="middle" font-size="22" font-weight="700">丙</text>
              <text x="${xA - 18}" y="${yTop - 7}" font-size="15">A</text>
              <text x="${xB - 18}" y="${yBottom + 20}" font-size="15">B</text>
              <text x="${xC + 9}" y="${yBottom + 20}" font-size="15">C</text>
              <text x="${xD + 9}" y="${yTop - 7}" font-size="15">D</text>
            </g>
          </svg>
          <figcaption>平行四边形面积 ${format(totalArea)} cm²；上边两段分别为 ${format(leftSegment)} cm、${format(rightSegment)} cm</figcaption>
        </figure>`;
    }

    function renderMultiCircleChainDiagram(diagram) {
      const diameters = (Array.isArray(diagram.diameters) ? diagram.diameters : [6, 8, 10, 12])
        .map(value => Math.max(0.1, diagramNumber(value, 1)));
      const total = diameters.reduce((sum, value) => sum + value, 0);
      const abLength = Math.max(0.1, diagramNumber(diagram.ab_length, total));
      const scale = Math.min(360 / total, 115 / Math.max(...diameters));
      const startX = (500 - total * scale) / 2;
      const centerY = 142;
      let offset = 0;
      const circles = diameters.map((diameter, index) => {
        const radius = diameter * scale / 2;
        const cx = startX + offset * scale + radius;
        offset += diameter;
        const fill = index % 2 === 0 ? '#dcefd3' : '#a9cbe8';
        return `<circle cx="${cx}" cy="${centerY}" r="${radius}" fill="${fill}" fill-opacity="0.86" stroke="#405d7d" stroke-width="2.5" />`;
      }).join('');
      const endX = startX + total * scale;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
          <svg viewBox="0 0 500 260" role="img" aria-label="多个圆沿线段AB依次排列的周长和图">
            ${circles}
            <path d="M ${startX - 10} ${centerY} L ${endX + 10} ${centerY}" stroke="#27384c" stroke-width="2" />
            <circle cx="${startX}" cy="${centerY}" r="3.5" fill="#27384c" />
            <circle cx="${endX}" cy="${centerY}" r="3.5" fill="#27384c" />
            <g fill="#27384c" font-family="Arial, sans-serif" font-size="16">
              <text x="${startX - 18}" y="${centerY - 10}">A</text>
              <text x="${endX + 8}" y="${centerY - 10}">B</text>
              <text x="250" y="235" text-anchor="middle">AB = ${format(abLength)} cm</text>
            </g>
          </svg>
          <figcaption>${diameters.length}个圆的直径首尾相接，直径总和等于 AB = ${format(abLength)} cm</figcaption>
        </figure>`;
    }

    function renderNestedSemicircleDiagram(diagram) {
      const outerRadius = Math.max(0.2, diagramNumber(diagram.outer_radius, 5));
      const innerRadius = Math.min(outerRadius - 0.1, Math.max(0.1, diagramNumber(diagram.inner_radius, 2)));
      const outerPx = 165;
      const innerPx = outerPx * innerRadius / outerRadius;
      const left = 75;
      const right = left + outerPx * 2;
      const baseY = 220;
      const innerLeft = right - innerPx * 2;
      const outerCenter = left + outerPx;
      const innerCenter = right - innerPx;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram semicircles">
          <svg viewBox="0 0 500 300" role="img" aria-label="大半圆内扣去一个共用右端点的小半圆形成的阴影周长图">
            <path d="M ${left} ${baseY} A ${outerPx} ${outerPx} 0 0 1 ${right} ${baseY} L ${left} ${baseY} Z" fill="#a9cbe8" stroke="#405d7d" stroke-width="3" />
            <path d="M ${innerLeft} ${baseY} A ${innerPx} ${innerPx} 0 0 1 ${right} ${baseY} L ${innerLeft} ${baseY} Z" fill="#ffffff" stroke="#405d7d" stroke-width="3" />
            <path d="M ${left} ${baseY} L ${innerLeft} ${baseY}" stroke="#27384c" stroke-width="3" />
            <circle cx="${outerCenter}" cy="${baseY}" r="3.5" fill="#27384c" />
            <circle cx="${innerCenter}" cy="${baseY}" r="3.5" fill="#27384c" />
            <g fill="#27384c" font-family="Arial, sans-serif" font-size="15">
              <text x="${outerCenter - 10}" y="${baseY + 24}">O₁</text>
              <text x="${innerCenter - 10}" y="${baseY + 24}">O₂</text>
              <text x="${left + outerPx * 0.5}" y="${baseY - outerPx * 0.45}" text-anchor="middle" font-size="19" font-weight="700">阴影</text>
            </g>
          </svg>
          <figcaption>大半圆半径 ${format(outerRadius)} cm；小半圆半径 ${format(innerRadius)} cm；两半圆共用右端点</figcaption>
        </figure>`;
    }

    function renderEqualAreaTrapezoidDiagram(diagram) {
      const leftSide = Math.max(0.1, diagramNumber(diagram.left_side, 10));
      const rightSide = Math.max(0.1, diagramNumber(diagram.right_side, 8));
      const width = Math.max(0.1, diagramNumber(diagram.width, 8));
      const xB = 80;
      const yB = 235;
      const heightPx = 180;
      const widthPx = 300;
      const xC = xB + widthPx;
      const yA = yB - heightPx;
      const yD = yB - heightPx * rightSide / leftSide;
      const be = (2 * leftSide - rightSide) / 3;
      const bf = width * (2 * rightSide - leftSide) / (3 * rightSide);
      const yE = yB - heightPx * be / leftSide;
      const xF = xB + widthPx * bf / width;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
	          <svg viewBox="0 0 500 310" role="img" aria-label="直角梯形内三块面积相等并求三角形BEF面积的图">
	            <polygon points="${xB},${yA} ${xB},${yE} ${xC},${yD}" fill="#a9cbe8" />
	            <polygon points="${xF},${yB} ${xC},${yB} ${xC},${yD}" fill="#dcefd3" />
	            <polygon points="${xB},${yE} ${xB},${yB} ${xF},${yB} ${xC},${yD}" fill="#f2cf83" />
	            <polygon data-part="triangle-bef" points="${xB},${yE} ${xB},${yB} ${xF},${yB}" fill="#f3a765" />
	            <path d="M ${xB} ${yA} L ${xB} ${yB} L ${xC} ${yB} L ${xC} ${yD} Z" fill="none" stroke="#27384c" stroke-width="3" />
	            <path d="M ${xB} ${yE} L ${xC} ${yD} M ${xF} ${yB} L ${xC} ${yD}" stroke="#405d7d" stroke-width="2.5" fill="none" />
	            <path data-segment="EF" d="M ${xB} ${yE} L ${xF} ${yB}" stroke="#9d4f1f" stroke-width="3.5" fill="none" />
	            <circle cx="${xB}" cy="${yE}" r="4" fill="#27384c" />
	            <circle cx="${xF}" cy="${yB}" r="4" fill="#27384c" />
	            <g fill="#27384c" font-family="Arial, sans-serif" font-size="15">
              <text x="${xB - 22}" y="${yA - 7}">A</text><text x="${xB - 22}" y="${yB + 20}">B</text>
              <text x="${xC + 10}" y="${yB + 20}">C</text><text x="${xC + 10}" y="${yD - 7}">D</text>
              <text x="${xB - 22}" y="${yE + 5}">E</text><text x="${xF - 4}" y="${yB + 22}">F</text>
              <text x="${xB - 52}" y="${(yA + yB) / 2}">AB=${format(leftSide)}</text>
              <text x="${xC + 18}" y="${(yD + yB) / 2}">CD=${format(rightSide)}</text>
              <text x="${(xB + xC) / 2}" y="${yB + 48}" text-anchor="middle">BC=${format(width)} cm</text>
            </g>
          </svg>
	          <figcaption>△AED、△FCD与四边形EBFD面积相等；橙色部分是所求的△BEF</figcaption>
	        </figure>`;
    }

    function renderOpenCylinderNetDiagram(diagram) {
      const radius = Math.max(0.1, diagramNumber(diagram.radius, 2));
      const totalLength = Math.max(0.1, diagramNumber(diagram.total_length, 16.56));
      const side = 105;
      const rectWidth = side * Math.PI;
      const x = 24;
      const y = 72;
      const circleX = x + rectWidth + side / 2;
      const endX = x + rectWidth + side;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
          <svg viewBox="0 0 500 270" role="img" aria-label="长方形侧面和一个圆形底面组成的无盖圆柱水桶展开图">
            <rect x="${x}" y="${y}" width="${rectWidth}" height="${side}" fill="#a9cbe8" stroke="#27384c" stroke-width="3" />
            <rect x="${x + rectWidth}" y="${y}" width="${side}" height="${side}" fill="#ffffff" stroke="#27384c" stroke-width="3" />
            <circle cx="${circleX}" cy="${y + side / 2}" r="${side / 2 - 5}" fill="#dcefd3" stroke="#405d7d" stroke-width="2.5" />
            <g stroke="#647589" stroke-width="1.5" fill="none">
              <path d="M ${x} ${y + side + 25} L ${endX} ${y + side + 25}" />
              <path d="M ${x} ${y + side + 18} L ${x} ${y + side + 32} M ${endX} ${y + side + 18} L ${endX} ${y + side + 32}" />
            </g>
            <g fill="#27384c" font-family="Arial, sans-serif" font-size="15">
              <text x="${x + rectWidth / 2}" y="${y + side / 2 + 5}" text-anchor="middle">圆柱侧面</text>
              <text x="${circleX}" y="${y + side / 2 + 5}" text-anchor="middle">底面</text>
              <text x="${(x + endX) / 2}" y="${y + side + 52}" text-anchor="middle">总长 ${format(totalLength)} dm</text>
            </g>
          </svg>
          <figcaption>底面半径 ${format(radius)} dm；圆形直径等于长方形的宽</figcaption>
        </figure>`;
    }

    function renderCuboidHoleDiagram(diagram, key = 'practice') {
      const length = Math.max(0.1, diagramNumber(diagram.length, 10));
      const width = Math.max(0.1, diagramNumber(diagram.width, 8));
      const height = Math.max(0.1, diagramNumber(diagram.height, 6));
      const radius = Math.max(0.1, diagramNumber(diagram.radius, 2));
      const x1 = 62;
      const y1 = 78;
      const boxWidth = 310;
      const boxHeight = 155;
      const depthX = 62;
      const depthY = -40;
      const holeX = x1 + boxWidth * 0.6;
      const holeTopY = y1 + depthY * 0.42;
      const holeBottomY = y1 + boxHeight - 18;
      const holeRx = Math.max(18, Math.min(52, radius / Math.min(length, width) * 180));
      const holeRy = holeRx * 0.34;
      const format = value => Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
      return `
        <figure class="practice-diagram wide">
          <svg viewBox="0 0 520 320" role="img" aria-label="长方体中从上到下挖圆柱形通孔的表面积图">
            <polygon points="${x1},${y1} ${x1 + depthX},${y1 + depthY} ${x1 + boxWidth + depthX},${y1 + depthY} ${x1 + boxWidth},${y1}" fill="#eaf1f7" stroke="#27384c" stroke-width="2.5" />
            <polygon points="${x1 + boxWidth},${y1} ${x1 + boxWidth + depthX},${y1 + depthY} ${x1 + boxWidth + depthX},${y1 + boxHeight + depthY} ${x1 + boxWidth},${y1 + boxHeight}" fill="#dce7f0" stroke="#27384c" stroke-width="2.5" />
            <rect x="${x1}" y="${y1}" width="${boxWidth}" height="${boxHeight}" fill="#f7fafc" stroke="#27384c" stroke-width="3" />
            <ellipse cx="${holeX}" cy="${holeTopY}" rx="${holeRx}" ry="${holeRy}" fill="#ffffff" stroke="#405d7d" stroke-width="2.5" />
            <path d="M ${holeX - holeRx} ${holeTopY} L ${holeX - holeRx} ${holeBottomY} M ${holeX + holeRx} ${holeTopY} L ${holeX + holeRx} ${holeBottomY}" stroke="#647589" stroke-width="2" stroke-dasharray="6 5" />
            <ellipse cx="${holeX}" cy="${holeBottomY}" rx="${holeRx}" ry="${holeRy}" fill="none" stroke="#647589" stroke-width="2" stroke-dasharray="6 5" />
            <g fill="#27384c" font-family="Arial, sans-serif" font-size="15">
              <text x="${x1 + boxWidth / 2}" y="${y1 + boxHeight + 34}" text-anchor="middle">长 ${format(length)} cm</text>
              <text x="${x1 + boxWidth + 45}" y="${y1 + boxHeight / 2}">高 ${format(height)} cm</text>
              <text x="${x1 + boxWidth + depthX - 8}" y="${y1 + boxHeight + depthY + 28}">宽 ${format(width)} cm</text>
              <text x="${holeX}" y="${holeTopY - holeRy - 12}" text-anchor="middle">r = ${format(radius)} cm</text>
            </g>
          </svg>
          <figcaption>圆柱孔从长方体上表面贯穿到下表面，π取3</figcaption>
        </figure>`;
    }

    function buildMathPractice(card, index) {
      const text = cardText(card);
      if (text.includes('平行四边形') && text.includes('圆')) {
        return practiceItem(card, '一个平行四边形底 12cm，高 5cm。若一个圆的面积和它相等，这个圆的面积是多少？', '平行四边形面积 = 12 × 5 = 60cm²，所以圆的面积也是 60cm²。');
      }
      if (text.includes('圆柱削成最大圆锥') || text.includes('等底等高')) {
        return practiceItem(card, '一个圆柱体积是 45cm³，把它削成最大的圆锥。圆锥体积是多少？削去多少？', '最大圆锥与圆柱等底等高，体积是圆柱的 1/3，所以圆锥 15cm³，削去 30cm³。');
      }
      if (text.includes('被除数') || text.includes('商不变') || text.includes('余数变化')) {
        return practiceItem(card, '47 ÷ 9 = 5……2。如果被除数和除数同时扩大 100 倍，商和余数分别是多少？', '商不变，还是 5；余数也扩大 100 倍，是 200。');
      }
      if (text.includes('糖水浓度') || text.includes('含盐率') || text.includes('含糖率') || text.includes('相同浓度')) {
        return practiceItem(card, '有 120g 含糖率 10% 的糖水，要变成含糖率 20%，需要加入多少克糖？', '原来糖 12g。设加 x 克糖：(12+x) ÷ (120+x) = 20%，解得 x = 15g。');
      }
      if (text.includes('30的因数') || text.includes('质数合数')) {
        return practiceItem(card, '从 30 的因数中，选两个质数和两个合数，写出一个比例。', '示例：2:3 = 10:15。2、3 是质数，10、15 是合数。');
      }
      if (text.includes('师徒') || text.includes('按比例分配') || text.includes('材料配比')) {
        return practiceItem(card, '甲、乙按 3:2 分配 250 个零件，甲应分到多少个？', '总份数 3+2=5，甲占 3/5，250 × 3/5 = 150 个。');
      }
      if (text.includes('优惠') || text.includes('折扣') || text.includes('买几送几')) {
        return practiceItem(card, '一本书原价 40 元，买 5 本。甲店八折；乙店买四送一。去哪家更便宜？', '甲店：40×5×0.8=160 元；乙店：付 4 本钱=160 元。一样便宜。');
      }
      if (text.includes('阴影面积') || text.includes('组合图形面积')) {
        return practiceItem(card, '两个图形相减求阴影面积时，如果公共部分一样，应该先看什么？请用一句话说明。', '先找“相同部分”，把相同部分抵消，再比较剩下部分的面积。');
      }
      if (text.includes('直角三角形') || text.includes('求角')) {
        return practiceItem(card, '一个直角三角形中，一个锐角是 35°，另一个锐角是多少度？', '90° - 35° = 55°。直角三角形两个锐角和是 90°。');
      }
      if (text.includes('三根木棒') || text.includes('三角形三边')) {
        return practiceItem(card, '下面哪组小棒能围成三角形？A. 2cm、3cm、5cm  B. 4cm、6cm、9cm', '选 B。任意两边之和要大于第三边；4+6>9，可以围成。');
      }
      if (text.includes('仓库') || text.includes('变化前后关系')) {
        return practiceItem(card, '甲、乙原来质量比是 5:4。甲给乙 10 吨后，甲乙相等。原来甲、乙各多少吨？', '设每份 x，原来甲 5x、乙 4x。5x-10=4x+10，所以 x=20。甲 100 吨，乙 80 吨。');
      }
      if (text.includes('计划生产') || text.includes('工作总量不变')) {
        return practiceItem(card, '计划每天做 60 个，30 天完成。实际每天做 72 个，多少天完成？', '总量 60×30=1800 个；1800÷72=25 天。');
      }
      if (text.includes('圆柱侧面积') || text.includes('侧面展开图')) {
        return practiceItem(card, '一张长 15cm、宽 12cm 的长方形纸卷成圆柱侧面，这个圆柱的侧面积是多少？', '侧面积就是这张长方形纸的面积：15×12=180cm²。');
      }
      if (text.includes('比例顺序') || text.includes('内项积')) {
        return practiceItem(card, '如果 a:b = 9:8，那么 b:a 应该是多少？不要把顺序写反。', 'b:a = 8:9。比的顺序变了，前后项也要交换。');
      }
      if (text.includes('负数比较') || text.includes('数轴距离')) {
        return practiceItem(card, '把 -1.5、-0.5、0、2、-2 按从小到大排列，并说出哪个数离 0 最近。', '从小到大：-2、-1.5、-0.5、0、2。离 0 最近的是 0；如果只看非零数，是 -0.5。');
      }
      if (text.includes('等体积') || text.includes('圆柱圆锥体积')) {
        return practiceItem(card, '圆柱和圆锥等底等体积。圆柱高 6dm，圆锥高多少 dm？', '圆锥高是圆柱高的 3 倍，所以是 18dm。');
      }
      if (text.includes('小数除法') || text.includes('平方') || text.includes('运算顺序')) {
        return practiceItem(card, '计算：3.14 × (0.6 ÷ 0.3)²', '先算括号：0.6÷0.3=2；再平方：2²=4；最后 3.14×4=12.56。');
      }
      if (text.includes('解比例') || text.includes('分数除法')) {
        return practiceItem(card, '解比例：3/4 : x = 0.6 : 1/2', '0.6 ÷ 1/2 = 1.2，3/4 ÷ x = 1.2，所以 x = 5/8。');
      }
      if (text.includes('效率') || text.includes('时间减少')) {
        return practiceItem(card, '原来 5 小时完成一项任务，现在 4 小时完成，效率提高了百分之几？', '效率从 1/5 变成 1/4，提高量是 1/4 - 1/5 = 1/20；相对原效率提高 (1/20) ÷ (1/5) = 25%。');
      }
      if (text.includes('正方体表面积') || text.includes('正比例')) {
        return practiceItem(card, '正方体的表面积和棱长成正比例吗？请判断并说明。', '不成正比例。表面积 = 6a²，表面积÷棱长 = 6a，不是固定不变。');
      }
      if (text.includes('单位1')) {
        return practiceItem(card, '乙数比甲数少 20%，那么甲数比乙数多百分之几？', '把甲看作 100，乙是 80；甲比乙多 20，20÷80=25%。');
      }
      return practiceItem(card, `重做这条错题的同类题：\n${card.question_text || card.topic || '请根据原错题重新列式计算。'}\n\n做完后，用一句话写出这题最容易错在哪里。`, card.correct_answer || card.wrong_reason || '对照原错题检查答案和错因。', card.topic || `数学练习 ${index + 1}`);
    }

    function buildEnglishPractice(card, index) {
      const text = cardText(card).toLowerCase();
      if (text.includes('there was no') || text.includes('no和not')) {
        return practiceItem(card, '选择：There was ____ library in my old school.\nA. no   B. not', '选 A。There was no + 名词，表示“没有……”。');
      }
      if (text.includes('大小写') || text.includes('because it')) {
        return practiceItem(card, '改错：Because It tells us many interesting stories.', 'Because it tells us many interesting stories. 句中 it 不需要大写。');
      }
      if (text.includes('skate')) {
        return practiceItem(card, '填空：I like ice-________ in winter. / He can ice-________ very well.', 'I like ice-skating in winter. / He can ice-skate very well. 注意 skating 和 skate 的用法。');
      }
      if (text.includes('happy yesterday') || text.includes('was he')) {
        return practiceItem(card, '看图问答常见句型：Was he sad yesterday? 请写一个否定回答。', "No, he wasn't. / No, he was not. 注意 wasn't 的拼写和问答对应。");
      }
      if (text.includes('went') || text.includes('go')) {
        return practiceItem(card, '改错：I goed to the park and studing English yesterday.', 'I went to the park and studied English yesterday. go 的过去式是 went，study 的过去式是 studied。');
      }
      if (text.includes('planned') || text.includes('stopped')) {
        return practiceItem(card, '写出过去式：plan → ______，stop → ______。', 'planned，stopped。重读闭音节结尾要双写最后一个辅音字母再加 -ed。');
      }
      return practiceItem(card, `重做这条英语错题，并把正确句子抄一遍：\n${card.question_text || card.topic || '请根据原错题完成。'}`, card.correct_answer || card.wrong_reason || '对照原错题检查语法、拼写和大小写。', card.topic || `英语练习 ${index + 1}`);
    }

    function practiceKey(cardId, prompt) {
      return `${cardId || ''}::${prompt || ''}`;
    }

    function indexPracticeFeedback(items) {
      const indexed = {};
      items.forEach(item => {
        const key = practiceKey(item.card_id, item.prompt_text);
        if (!indexed[key] || item.created_at > indexed[key].created_at) {
          indexed[key] = item;
        }
      });
      return indexed;
    }

    function indexPracticeAttempts(items) {
      const indexed = {};
      items.forEach(item => {
        const key = practiceKey(item.card_id, item.prompt_text);
        if (!indexed[key] || item.created_at > indexed[key].created_at) {
          indexed[key] = item;
        }
      });
      return indexed;
    }

    async function submitPracticeAnswer(index, mode = 'practice') {
      const item = practiceItems(mode)[index];
      const prefix = practiceDomPrefix(mode);
      const input = $(prefix + 'Answer' + index);
      const answerText = input ? input.value.trim() : '';
      if (!item || !answerText) {
        toast('先写下答案或思路');
        return;
      }
      const data = await api('/api/practice-attempts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          card_id: item.source_id,
          subject: item.subject,
          topic: item.topic,
          prompt_text: item.prompt,
          answer_text: answerText,
          reference_answer: item.answer || ''
        })
      });
      const key = practiceKey(item.source_id, item.prompt);
      state.practiceAttempts[key] = data.attempt;
      if (data.attempt.auto_feedback) state.practiceFeedback[key] = data.attempt.auto_feedback;
      delete state.practiceEditing[key];
      const actionPanel = $(prefix + 'Actions' + index);
      if (actionPanel) actionPanel.innerHTML = renderPracticeActions(item, index, mode);
      toast(data.attempt.grade_result === 'correct' ? '答对了！' : '答错了，已经自动加入“还要加强”');
      if (item.subject === '英语') {
        const englishData = await api('/api/english-overview');
        state.englishOverview = englishData.overview || {};
        renderEnglishOverview();
        renderEnglishFocus();
      }
    }

    function editPracticeAnswer(index, mode = 'practice') {
      const item = practiceItems(mode)[index];
      if (!item) return;
      state.practiceEditing[practiceKey(item.source_id, item.prompt)] = true;
      const actionPanel = $(practiceDomPrefix(mode) + 'Actions' + index);
      if (actionPanel) actionPanel.innerHTML = renderPracticeActions(item, index, mode);
    }

    async function recordPractice(index, result, mode = 'practice') {
      const item = practiceItems(mode)[index];
      if (!item) return;
      const data = await api('/api/practice-feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          card_id: item.source_id,
          subject: item.subject,
          topic: item.topic,
          prompt_text: item.prompt,
          result
        })
      });
      state.practiceFeedback[practiceKey(item.source_id, item.prompt)] = data.feedback;
      const actionPanel = $(practiceDomPrefix(mode) + 'Actions' + index);
      if (actionPanel) actionPanel.innerHTML = renderPracticeActions(item, index, mode);
      toast(result === 'mastered' ? '已记录：我会了' : '已记录：还要加强');
      const summaryData = await api('/api/summary');
      state.summary = summaryData.summary || {};
      renderSummary();
      if (item.subject === '英语') {
        const englishData = await api('/api/english-overview');
        state.englishOverview = englishData.overview || {};
        renderEnglishOverview();
        renderEnglishFocus();
      }
    }

    async function markReviewed(id) {
      await api('/api/cards/' + encodeURIComponent(id) + '/review', { method: 'POST' });
      toast('已安排下一次复习');
      await load();
    }

    async function deleteCard(id) {
      if (!window.confirm('确定删除这条错题吗？')) return;
      await api('/api/cards/' + encodeURIComponent(id), { method: 'DELETE' });
      toast('错题已删除');
      await load();
    }

    function editCard(id) {
      const card = state.cards.find(item => item.id === id);
      if (!card) return;
      $('cardId').value = card.id;
      $('recordType').value = card.record_type || 'mistake';
      $('subject').value = card.subject;
      $('unit').value = card.unit;
      $('questionType').value = card.question_type;
      $('topic').value = card.topic;
      $('knowledgePoints').value = (card.knowledge_points || []).join(', ');
      $('questionText').value = card.question_text;
      $('userAnswer').value = card.user_answer;
      $('correctAnswer').value = card.correct_answer;
      $('wrongReason').value = card.wrong_reason;
      $('explanationSummary').value = card.explanation_summary;
      $('feynmanExplain').value = card.feynman_explain;
      $('feynmanStuck').value = card.feynman_stuck;
      $('tags').value = (card.tags || []).join(', ');
      $('grammarErrors').value = (card.grammar_errors || []).join(', ');
      $('misspelledWords').value = (card.misspelled_words || []).map(item => `${item.wrong}->${item.correct}`).join(', ');
      $('formTitle').textContent = '编辑错题';
      switchView('add');
    }

    function resetForm() {
      $('cardForm').reset();
      $('cardId').value = '';
      $('formTitle').textContent = '编辑错题';
    }

    async function submitForm(event) {
      event.preventDefault();
      const payload = {
        record_type: $('recordType').value,
        subject: $('subject').value,
        unit: $('unit').value,
        question_type: $('questionType').value,
        topic: $('topic').value,
        knowledge_points: $('knowledgePoints').value,
        question_text: $('questionText').value,
        user_answer: $('userAnswer').value,
        correct_answer: $('correctAnswer').value,
        wrong_reason: $('wrongReason').value,
        explanation_summary: $('explanationSummary').value,
        feynman_explain: $('feynmanExplain').value,
        feynman_stuck: $('feynmanStuck').value,
        tags: $('tags').value,
        grammar_errors: $('grammarErrors').value,
        misspelled_words: $('misspelledWords').value
      };
      const id = $('cardId').value;
      const method = id ? 'PUT' : 'POST';
      const path = id ? '/api/cards/' + encodeURIComponent(id) : '/api/cards';
      const result = await api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (result.duplicate_candidates && result.duplicate_candidates.length) {
        const names = result.duplicate_candidates.map(item => `${item.subject}：${item.topic || '未命名错题'}`).join('\n');
        if (!window.confirm(`发现可能已经录入过相同错题：\n${names}\n\n仍要保存一份新记录吗？`)) {
          toast('已取消保存，请先检查原来的错题');
          return;
        }
        payload.force_duplicate = true;
        await api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      }
      resetForm();
      toast('错题已保存');
      await load();
      switchView('notebook');
    }

    function exportFile(type, format) {
      window.location.href = '/api/export?' + params({ type, format });
    }

    function printFile(type) {
      window.open('/print?' + params({ type }), '_blank');
    }

    function downloadBackup() {
      window.location.href = '/api/backup';
    }

    function exportWeeklySummary() {
      window.location.href = '/api/summary?format=md';
    }

    async function restoreBackup(file) {
      if (!file) return;
      const text = await file.text();
      let backup;
      try {
        backup = JSON.parse(text);
      } catch (_) {
        toast('请选择正确的备份文件');
        return;
      }
      if (!window.confirm('恢复会把备份中的错题和复习记录合并回来，确定继续吗？')) return;
      const data = await api('/api/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backup })
      });
      await load();
      toast(`已恢复 ${data.restored.mistake_cards || 0} 条错题`);
    }

    function switchView(view) {
      state.view = view;
      document.querySelectorAll('nav button').forEach(btn => btn.classList.toggle('active', btn.dataset.view === view));
      document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
      $('view-' + view).classList.add('active');
      $('pageTitle').textContent = { review: '复习', english: '英语学习', 'english-focus': '英语专项', practice: '综合复习题', single: '单题练习', learning: '知识加油站', add: '编辑错题', notebook: '错题本', selected: '自选考题', weak: '学习小结', export: '导出' }[view];
      document.querySelector('.topbar').classList.toggle('focused', view === 'english-focus' || ['single', 'selected'].includes(view));
      $('motivationQuote').hidden = view !== 'review';
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    }

    const motivationQuotes = [
      '今天不求一下子全会，只要比昨天多懂一点。',
      '每一道改对的题，都在帮未来的你省力气。',
      '慢一点没关系，想明白最重要。',
      '错题不是扣分项，是发现进步方向的地图。',
      '认真走好这一小步，难题就会少一点。',
      '会做一道题是收获，弄懂为什么更厉害。',
      '今天的耐心，会变成明天的底气。',
      '别怕题目难，你正在学习怎样解决它。',
      '把不会的变成会的，就是很了不起的进步。',
      '每次复习一点点，知识就会记得更牢。',
      '先开始，再慢慢变得更好。',
      '认真订正一次，胜过匆忙做十次。',
      '你的大脑正在因为思考而变得更强。',
      '不和别人比速度，只和昨天的自己比进步。',
      '难题像台阶，一步一步就能走上去。',
      '允许自己暂时不会，也相信自己最终能会。',
      '今天多问一个为什么，明天就多一份明白。',
      '学习不是比赛，是一次次把自己练得更棒。',
      '每一个被发现的错误，都是一次升级机会。',
      '先把这一题弄懂，进步就已经发生了。'
    ];

    function pickMotivationQuote() {
      let lastIndex = -1;
      try {
        lastIndex = Number(window.localStorage.getItem('homeworkLastMotivationQuote'));
      } catch (_) {}
      let nextIndex = Math.floor(Math.random() * motivationQuotes.length);
      if (Number.isInteger(lastIndex) && lastIndex >= 0 && lastIndex < motivationQuotes.length && nextIndex === lastIndex) {
        nextIndex = (nextIndex + 1 + Math.floor(Math.random() * (motivationQuotes.length - 1))) % motivationQuotes.length;
      }
      try {
        window.localStorage.setItem('homeworkLastMotivationQuote', String(nextIndex));
      } catch (_) {}
      return motivationQuotes[nextIndex];
    }

    function toast(message) {
      $('toast').textContent = message;
      $('toast').classList.add('show');
      setTimeout(() => $('toast').classList.remove('show'), 1800);
    }

	    function escapeHtml(value) {
	      return String(value || '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])).replace(/\n/g, '<br>');
	    }

	    function renderInlineMath(value) {
	      return escapeHtml(value).replace(/(\d+)\s*\/\s*(\d+)/g, (_, top, bottom) => (
	        `<span class="math-fraction"><span>${top}</span><span>${bottom}</span></span>`
	      ));
	    }

	    function renderSolutionText(value) {
	      const withoutCodeMarks = String(value || '').replace(/`+/g, '');
	      return renderInlineMath(withoutCodeMarks).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
	    }

	    function renderSolutionProcess(value) {
	      const text = String(value || '').trim();
	      if (!text) return '<p>未记录</p>';
	      const steps = [];
	      let current = null;
	      text.split('\n').forEach(rawLine => {
	        const line = rawLine.trim();
	        if (!line) return;
	        const headingMatch = line.match(/^(\d+)(?:\.\s+|、\s*)(.+)$/);
	        if (headingMatch) {
	          current = { number: headingMatch[1], title: headingMatch[2], lines: [] };
	          steps.push(current);
	          return;
	        }
	        if (!current) {
	          current = { number: '', title: '解题思路', lines: [] };
	          steps.push(current);
	        }
	        current.lines.push(line);
	      });
	      return steps.map(step => {
	        const body = step.lines.map(line => {
	          const formula = /[=＝×÷+\-－−]/.test(line)
	            && line.length <= 90
	            && !/[：:]$/.test(line)
	            && !/[，。；！？]/.test(line);
	          return formula
	            ? `<div class="solution-formula">${renderSolutionText(line)}</div>`
	            : `<p>${renderSolutionText(line)}</p>`;
	        }).join('');
	        const heading = step.number ? `${step.number}. ${step.title}` : step.title;
	        let stepClass = '';
	        if (/公共部分|核心关系|关键关系|记住方法|比较/.test(step.title)) stepClass = ' key-step';
	        if (/写出答案|得出结论/.test(step.title)) stepClass = ' final-step';
	        return `<section class="solution-step${stepClass}"><h5>${renderSolutionText(heading)}</h5>${body}</section>`;
	      }).join('');
	    }

    function escapeAttr(value) {
      return String(value || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    }

    function formatDate(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value.slice(0, 10);
      return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
    }

    function formatFullDate(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value.slice(0, 10);
      return date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
    }

    $('motivationQuote').textContent = `“${pickMotivationQuote()}”`;
    document.querySelectorAll('nav button').forEach(btn => btn.addEventListener('click', () => switchView(btn.dataset.view)));
    document.querySelectorAll('[data-export]').forEach(btn => btn.addEventListener('click', () => exportFile(btn.dataset.export, btn.dataset.format)));
    document.querySelectorAll('[data-print]').forEach(btn => btn.addEventListener('click', () => printFile(btn.dataset.print)));
    $('cardForm').addEventListener('submit', submitForm);
    $('resetForm').addEventListener('click', resetForm);
    $('refreshBtn').addEventListener('click', load);
    $('shufflePractice').addEventListener('click', () => { state.practiceSeed += practiceLimit(state.practiceSubject); renderPractice(); });
    $('freshSinglePractice').addEventListener('click', () => {
      if (state.singlePracticeCardId) generateSinglePractice(state.singlePracticeCardId, true, true);
    });
    $('backToReview').addEventListener('click', () => switchView(state.singlePracticeReturnView || 'review'));
    $('backToEnglish').addEventListener('click', () => switchView('english'));
    $('startEnglishPractice').addEventListener('click', startEnglishFocusPractice);
    $('printSelectedQuestions').addEventListener('click', () => printSelectedQuestions('questions'));
    $('printSelectedAnswers').addEventListener('click', () => printSelectedQuestions('answers'));
    $('emailSelectedQuestions').addEventListener('click', event => emailSelectedQuestions('questions', event.currentTarget));
    $('emailSelectedAnswers').addEventListener('click', event => emailSelectedQuestions('answers', event.currentTarget));
    $('downloadBackup').addEventListener('click', downloadBackup);
    $('chooseRestore').addEventListener('click', () => $('restoreFile').click());
    $('restoreFile').addEventListener('change', event => restoreBackup(event.target.files[0]).catch(err => toast(err.message)));
    $('exportSummary').addEventListener('click', exportWeeklySummary);
    $('searchInput').addEventListener('input', () => load().catch(err => toast(err.message)));
    $('subjectFilter').addEventListener('change', () => load().catch(err => toast(err.message)));
    $('recordTypeFilter').addEventListener('change', () => load().catch(err => toast(err.message)));
    $('dateFromFilter').addEventListener('change', () => {
      $('dateToFilter').min = $('dateFromFilter').value;
      if ($('dateToFilter').value && $('dateToFilter').value < $('dateFromFilter').value) $('dateToFilter').value = $('dateFromFilter').value;
      load().catch(err => toast(err.message));
    });
    $('dateToFilter').addEventListener('change', () => load().catch(err => toast(err.message)));
    $('clearFilter').addEventListener('click', () => {
      $('searchInput').value = '';
      $('subjectFilter').value = '';
      $('recordTypeFilter').value = '';
      $('dateFromFilter').value = '';
      $('dateToFilter').value = '';
      $('dateToFilter').min = '';
      load();
    });
    load().catch(err => toast(err.message));
  </script>
</body>
</html>
"""


PRINT_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{{title}}</title>
  <style>
    body { margin: 36px; color: #222; font-family: "PingFang SC", "Songti SC", sans-serif; line-height: 1.6; }
    header { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 2px solid #222; margin-bottom: 24px; }
    h1 { font-size: 28px; margin: 0 0 8px; }
    h2 { font-size: 20px; margin-top: 0; }
    h3 { font-size: 15px; margin-bottom: 4px; }
    section { break-inside: avoid; border-bottom: 1px solid #ddd; padding: 18px 0; }
    .meta { color: #666; }
    .actions { margin-bottom: 16px; }
    button { padding: 9px 14px; border: 0; background: #276a73; color: #fff; border-radius: 6px; }
    @media print {
      .actions { display: none; }
      body { margin: 18mm; }
    }
  </style>
</head>
<body>
  <div class="actions"><button onclick="window.print()">打印或另存为 PDF</button></div>
  <header><h1>{{title}}</h1><span>家庭错题本</span></header>
  {{sections}}
</body>
</html>
"""


SELECTED_PRINT_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{{title}}</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 30px auto; max-width: 900px; color: #1f2d3d; font-family: "PingFang SC", "Microsoft YaHei", sans-serif; line-height: 1.7; }
    header { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; border-bottom: 2px solid #1f2d3d; padding-bottom: 10px; margin-bottom: 22px; }
    h1 { font-size: 28px; margin: 0; }
    h2 { font-size: 18px; margin: 0; }
    h3 { font-size: 15px; margin: 0 0 7px; }
    p { white-space: pre-wrap; }
    .actions { display: flex; gap: 10px; margin-bottom: 18px; }
    button { padding: 9px 14px; border: 0; background: #276a73; color: #fff; border-radius: 6px; cursor: pointer; }
    .exam-question { break-inside: avoid; border-bottom: 1px solid #ccd3d8; padding: 18px 0 24px; }
    .question-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
    .question-head span { color: #667581; font-size: 13px; white-space: nowrap; }
    .question-text { font-size: 16px; margin: 12px 0; }
    .question-figure { width: min(100%, 650px); margin: 16px auto; text-align: center; }
    .question-figure svg, .question-figure img { display: block; width: 100%; max-height: 430px; object-fit: contain; }
    .question-figure figcaption { color: #667581; font-size: 12px; margin-top: 6px; }
    .answer-space { min-height: 92px; border-bottom: 1px dashed #c7cdd1; color: #667581; padding-top: 12px; }
    .answer p { margin: 0; }
    .empty-print { padding: 50px 0; text-align: center; color: #667581; }
    @page { size: A4; margin: 16mm; }
    @media print {
      .actions { display: none; }
      body { margin: 0; max-width: none; }
      header { margin-top: 0; }
    }
  </style>
</head>
<body>
  <div class="actions"><button onclick="window.print()">打印或另存为 PDF</button></div>
  <header><h1>{{title}}</h1><span>{{subtitle}}</span></header>
  {{sections}}
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Local web notebook for homework mistakes.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HOMEWORK_NOTEBOOK_PORT", "18765")))
    parser.add_argument("--access-code", default=os.environ.get("HOMEWORK_NOTEBOOK_ACCESS_CODE", ""))
    parser.add_argument("--session-secret", default=os.environ.get("HOMEWORK_NOTEBOOK_SESSION_SECRET", ""))
    parser.add_argument("--auth-state", default=os.environ.get("HOMEWORK_NOTEBOOK_AUTH_STATE", str(AUTH_STATE_PATH)))
    args = parser.parse_args()
    if args.access_code.lower().startswith(("replace-with-", "change-me")):
        raise SystemExit("请先把示例访问码替换成自己的随机长访问码")
    if args.session_secret.lower().startswith(("replace-with-", "change-me")):
        raise SystemExit("请先把示例会话密钥替换成自己的随机长密钥")
    if args.access_code and len(re.sub(r"\s+", "", args.access_code)) < 16:
        raise SystemExit("访问码至少需要 16 个字符")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.access_code:
        raise SystemExit("监听本机以外的地址前，必须先设置 HOMEWORK_NOTEBOOK_ACCESS_CODE")
    connect_db().close()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    server.access_code = args.access_code
    server.session_secret = args.session_secret or secrets.token_hex(32)
    server.auth_state_path = Path(args.auth_state)
    server.login_attempts = {}
    server.login_attempts_lock = threading.Lock()
    if args.access_code:
        load_auth_state(server.auth_state_path)
    print_start_urls(args.host, args.port, bool(args.access_code))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


def print_start_urls(host: str, port: int, access_protected: bool) -> None:
    print("错题本已启动：")
    print(f"- 本机访问：http://127.0.0.1:{port}")
    if host in {"0.0.0.0", "::"}:
        for address in get_lan_addresses():
            print(f"- 内网访问：http://{address}:{port}")
    elif host != "127.0.0.1":
        print(f"- 当前监听：http://{host}:{port}")
    if access_protected:
        print(f"- 已开启访问码和手机动态验证码，用户名：{AUTH_USERNAME}")


def get_lan_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


if __name__ == "__main__":
    raise SystemExit(main())
