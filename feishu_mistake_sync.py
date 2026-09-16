#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from learning_enrichment import enrich_pending, ensure_learning_schema


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HOMEWORK_NOTEBOOK_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
DB_PATH = DATA_DIR / "mistakes.sqlite3"
STATE_PATH = DATA_DIR / "feishu_sync_state.json"
SOURCE_IMAGE_DIR = DATA_DIR / "source-images"
QUESTION_IMAGE_DIR = DATA_DIR / "question-images"
IMAGE_ORIENTATION_HELPER = BASE_DIR / "detect_image_orientation.swift"
IMAGE_NORMALIZATION_VERSION = "v2"
QUESTION_CROP_VERSION = "v2"
OPENCLAW_AGENT_IDS = tuple(
    item.strip()
    for item in os.environ.get("HOMEWORK_NOTEBOOK_OPENCLAW_AGENT_IDS", "main").split(",")
    if item.strip()
)
APP_USER_ID = "openclaw-student"
APP_SCOPE_PREFIX = "feishu"
CONFIRMED_MARKER = "【错题记录：已确认】"
EXAM_CONFIRMED_MARKER = "【试卷记录：已确认】"
CONFIRMED_MARKERS = (CONFIRMED_MARKER, EXAM_CONFIRMED_MARKER)
PENDING_MARKER = "【错题记录：待确认】"
NO_RECORD_MARKER = "【错题记录：无需记录】"
NOTEBOOK_HOST = "127.0.0.1"
NOTEBOOK_PORT = 18765
VISUAL_RECORD_PATTERN = re.compile(
    r"(?:如图|图中|右图|根据图片|观察.{0,8}图形|第\s*\d+\s*个图形|图形中|图形的周长|"
    r"阴影|圆弧|展开图|圆片|圆点|树状图|小棒|火柴棒)"
)

MATH_HINTS = (
    "数学",
    "圆柱",
    "圆锥",
    "比例",
    "百分",
    "浓度",
    "面积",
    "体积",
    "函数",
    "方程",
    "周长",
    "厘米",
    "平方",
    "π",
)
ENGLISH_HINTS = (
    "英语",
    "单词",
    "语法",
    "拼写",
    "句子",
    "过去式",
    "because",
    "there was",
    "wasn't",
    "skate",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def esc_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def ensure_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
            mem0_id TEXT,
            feynman_explain TEXT,
            feynman_stuck TEXT,
            next_review_at TEXT,
            last_reviewed_at TEXT,
            review_count INTEGER NOT NULL DEFAULT 0
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
        "review_count": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        ensure_column(conn, name, ddl)
    ensure_learning_schema(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_cards_created ON mistake_cards(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_cards_source ON mistake_cards(source_channel, source_peer)")
    conn.commit()
    return conn


def ensure_column(conn: sqlite3.Connection, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(mistake_cards)").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE mistake_cards ADD COLUMN {column} {ddl}")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"processed": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": []}


def save_state(state: dict[str, Any]) -> None:
    processed = list(dict.fromkeys(state.get("processed", [])))[-2000:]
    state["processed"] = processed
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_feishu_session_files(override_files: list[str] | None = None) -> list[Path]:
    if override_files:
        return [Path(item) for item in override_files if Path(item).exists()]
    files: list[Path] = []
    for agent_id in OPENCLAW_AGENT_IDS:
        sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"
        session_index = sessions_dir / "sessions.json"
        if not session_index.exists():
            continue
        data = json.loads(session_index.read_text(encoding="utf-8") or "{}")
        for key, item in data.items():
            if not key.startswith(f"agent:{agent_id}:feishu:"):
                continue
            session_file = item.get("sessionFile")
            if session_file:
                path = Path(session_file)
            else:
                session_id = item.get("sessionId")
                if not session_id:
                    continue
                path = sessions_dir / f"{session_id}.jsonl"
            if path.exists():
                files.append(path)
    return sorted(set(files), key=lambda p: p.stat().st_mtime)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def clean_user_text(text: str) -> str:
    text = re.sub(r"^\[message_id:[^\]]+\]\s*", "", text.strip())
    text = re.sub(r"^ou_[^:]+:\s*", "", text)
    return text.replace("<media:image>", "").strip()


def media_paths(message: dict[str, Any]) -> list[str]:
    paths = message.get("MediaPaths") or []
    if not paths and message.get("MediaPath"):
        paths = [message.get("MediaPath")]
    return [str(path) for path in paths if path]


def detect_image_rotation(image_path: Path, *, runner=subprocess.run) -> int:
    if not IMAGE_ORIENTATION_HELPER.is_file():
        return 0
    try:
        completed = runner(
            ["/usr/bin/swift", str(IMAGE_ORIENTATION_HELPER), str(image_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            return 0
        result = json.loads(completed.stdout or "{}")
        if not result.get("reliable"):
            return 0
        rotation = int(result.get("rotation") or 0)
        return rotation if rotation in {0, 90, 180, 270} else 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return 0


def normalize_image_path(
    source_value: str,
    *,
    output_dir: Path = SOURCE_IMAGE_DIR,
    detector=detect_image_rotation,
    rotator=subprocess.run,
) -> str:
    source_path = Path(str(source_value or "")).expanduser()
    if not source_path.is_file() or source_path.suffix.lower() not in {
        ".jpg", ".jpeg", ".png", ".webp", ".heic",
    }:
        return str(source_value or "")
    try:
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()[:24]
        suffix = source_path.suffix.lower()
        destination = output_dir / f"feishu-{IMAGE_NORMALIZATION_VERSION}-{digest}{suffix}"
        if destination.is_file():
            return str(destination)
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary = output_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp{suffix}"
        shutil.copy2(source_path, temporary)
        rotation = detector(source_path)
        if rotation:
            completed = rotator(
                ["/usr/bin/sips", "--rotate", str(rotation), str(temporary)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if completed.returncode != 0:
                temporary.unlink(missing_ok=True)
                return str(source_value or "")
        temporary.replace(destination)
        return str(destination)
    except (OSError, subprocess.TimeoutExpired):
        return str(source_value or "")


def latest_prior_media(records: list[dict[str, Any]], user_record: dict[str, Any]) -> list[str]:
    """Return every image in the contiguous Feishu batch before this message."""
    target_id = str(user_record.get("id") or "")
    target_index = len(records)
    for index, record in enumerate(records):
        if record is user_record or (target_id and str(record.get("id") or "") == target_id):
            target_index = index
            break
    batches: list[list[str]] = []
    found_media = False
    for record in reversed(records[:target_index]):
        message = record.get("message") or {}
        if (
            record.get("type") == "message"
            and message.get("role") == "user"
            and message.get("sourceChannel") == "feishu"
        ):
            paths = media_paths(message)
            if paths:
                batches.append(paths)
                found_media = True
            elif found_media:
                break
    return [path for batch in reversed(batches) for path in batch]


def extract_message_id(text: str) -> str:
    match = re.search(r"\[message_id:\s*([^\]]+)\]", text)
    return match.group(1).strip() if match else ""


def pair_feishu_messages(records: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, str]]:
    pairs: list[tuple[dict[str, Any], str, str]] = []
    current_user: dict[str, Any] | None = None
    assistant_parts: list[str] = []
    assistant_id = ""
    for record in records:
        if record.get("type") != "message":
            continue
        message = record.get("message") or {}
        role = message.get("role")
        if role == "user" and message.get("sourceChannel") == "feishu":
            if current_user and assistant_parts:
                pairs.append((current_user, "\n\n".join(assistant_parts), assistant_id))
            current_user = record
            assistant_parts = []
            assistant_id = ""
        elif role == "assistant" and current_user:
            text = message_text(message)
            if text:
                assistant_parts.append(text)
                assistant_id = str(record.get("id") or assistant_id)
    if current_user and assistant_parts:
        pairs.append((current_user, "\n\n".join(assistant_parts), assistant_id))
    return pairs


def collection_status(assistant_text: str) -> str:
    if any(marker in assistant_text for marker in CONFIRMED_MARKERS):
        return "confirmed"
    if PENDING_MARKER in assistant_text:
        return "pending"
    if NO_RECORD_MARKER in assistant_text:
        return "no_record"
    return "unmarked"


def split_confirmed_records(assistant_text: str) -> list[str]:
    """Split a page-level reply into one independently saved block per mistake."""
    marker_pattern = "|".join(re.escape(marker) for marker in CONFIRMED_MARKERS)
    matches = list(re.finditer(marker_pattern, assistant_text))
    if not matches:
        return []
    blocks: list[str] = []
    start = 0
    for match in matches:
        block = assistant_text[start : match.end()].strip()
        if block:
            page_matches = list(
                re.finditer(r"(?m)^\s*#{1,6}\s*第\s*(\d+)\s*页\s*$", assistant_text[: match.end()])
            )
            if page_matches:
                block = f"{block}\n\n<!-- source-page:{page_matches[-1].group(1)} -->"
            blocks.append(block)
        start = match.end()
    return blocks


def extract_source_page(text: str) -> int:
    labeled = extract_labeled(text, "原图页码", "来源页码")
    match = re.search(r"\d+", labeled)
    if not match:
        match = re.search(r"<!--\s*source-page:(\d+)\s*-->", text)
    if not match:
        headings = list(re.finditer(r"(?m)^\s*#{1,6}\s*第\s*(\d+)\s*页\s*$", text))
        match = headings[-1] if headings else None
    if not match:
        return 1
    try:
        return max(1, int(match.group(1) if match.lastindex else match.group(0)))
    except (TypeError, ValueError):
        return 1


def extract_crop_box(text: str) -> list[float]:
    value = extract_labeled(text, "题目区域", "原题区域", "裁剪区域")
    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", value)]
    if len(numbers) < 4:
        return []
    x, y, width, height = numbers[:4]
    if not (
        0 <= x < 100
        and 0 <= y < 100
        and 0 < width <= 100
        and 0 < height <= 100
        and x + width <= 100.5
        and y + height <= 100.5
    ):
        return []
    return [x, y, width, height]


def crop_question_image(
    source_value: str,
    crop_box: list[float] | tuple[float, float, float, float],
    *,
    output_dir: Path = QUESTION_IMAGE_DIR,
    runner=subprocess.run,
) -> str:
    source_path = Path(str(source_value or "")).expanduser()
    if not source_path.is_file() or len(crop_box) != 4:
        return str(source_value or "")
    try:
        details = runner(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(source_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if details.returncode != 0:
            return str(source_value or "")
        width_match = re.search(r"pixelWidth:\s*(\d+)", details.stdout)
        height_match = re.search(r"pixelHeight:\s*(\d+)", details.stdout)
        if not width_match or not height_match:
            return str(source_value or "")
        image_width = int(width_match.group(1))
        image_height = int(height_match.group(1))
        x_pct, y_pct, width_pct, height_pct = crop_box
        x = max(0, round(image_width * x_pct / 100))
        y = max(0, round(image_height * y_pct / 100))
        crop_width = min(image_width - x, max(1, round(image_width * width_pct / 100)))
        crop_height = min(image_height - y, max(1, round(image_height * height_pct / 100)))
        digest_source = f"{source_path}:{x}:{y}:{crop_width}:{crop_height}".encode()
        digest = hashlib.sha256(digest_source).hexdigest()[:24]
        suffix = source_path.suffix.lower() or ".jpg"
        destination = output_dir / f"question-{QUESTION_CROP_VERSION}-{digest}{suffix}"
        if destination.is_file():
            return str(destination)
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary = output_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp{suffix}"
        completed = runner(
            [
                "/usr/bin/sips",
                "--cropToHeightWidth",
                str(crop_height),
                str(crop_width),
                "--cropOffset",
                str(y),
                str(x),
                str(source_path),
                "--out",
                str(temporary),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            return str(source_value or "")
        temporary.replace(destination)
        return str(destination)
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return str(source_value or "")


def should_collect(user_message: dict[str, Any], assistant_text: str) -> bool:
    del user_message
    return collection_status(assistant_text) == "confirmed"


def extract_labeled(text: str, *labels: str) -> str:
    for label in labels:
        match = re.search(
            rf"(?:^|\n)\s*(?:[-*>]\s*)?(?:<font\b[^>]*>\s*)?"
            rf"(?:\*\*)?{re.escape(label)}\s*"
            rf"(?:\*\*\s*[:：]|[:：]\s*\*\*|[:：])\s*(.+)",
            text,
        )
        if match:
            value = match.group(1).strip().strip("*").strip()
            return re.sub(r"\s*</font>\s*$", "", value).strip()
    return ""


def extract_markdown_section(text: str, *headings: str) -> str:
    for heading in headings:
        match = re.search(
            rf"(?ms)^\s*#{{2,6}}\s*{re.escape(heading)}\s*$\n"
            rf"(.*?)"
            rf"(?=^\s*(?:#{{2,6}}\s+|(?:\*\*)?(?:正确答案|错误原因|错因|讲解摘要)\s*(?:\*\*)?\s*[:：]|<font\b)|\Z)",
            text,
        )
        if match:
            return match.group(1).strip()
    return ""


def extract_list(text: str, *labels: str) -> list[str]:
    value = extract_labeled(text, *labels)
    if not value:
        return []
    return [
        item.strip()
        for item in re.split(r"[,，、;/；]", value)
        if item.strip()
    ]


def normalize_record_type(value: str, user_text: str, assistant_text: str) -> str:
    if any(hint in str(value or "") for hint in ("答对题", "正确题", "已掌握题")):
        return "correct"
    text = f"{value}\n{user_text}\n{assistant_text}".lower()
    if any(hint in text for hint in ("强化学习", "强化练习", "巩固", "想强化", "专项练习")):
        return "reinforcement"
    if any(hint in text for hint in ("不会做", "不会", "没思路", "卡住", "未作答", "不知道怎么")):
        return "unsolved"
    return "mistake"


def build_card(
    user_record: dict[str, Any],
    assistant_text: str,
    assistant_id: str,
    session_file: Path,
    records: list[dict[str, Any]] | None = None,
    record_index: int = 1,
    record_count: int = 1,
    source_key: str = "",
) -> dict[str, Any]:
    message = user_record.get("message") or {}
    user_text_raw = str(message.get("content") or "")
    user_text = clean_user_text(user_text_raw)
    sender_id = str(message.get("senderId") or "")
    message_id = extract_message_id(user_text_raw)
    image_paths = media_paths(message)
    if not image_paths and records:
        image_paths = latest_prior_media(records, user_record)
    source_page = extract_source_page(assistant_text)
    image_path = image_paths[min(source_page - 1, len(image_paths) - 1)] if image_paths else ""
    crop_box = extract_crop_box(assistant_text)
    record_type = normalize_record_type(
        extract_labeled(assistant_text, "记录类型"), user_text, assistant_text
    )
    answer_status_label = extract_labeled(assistant_text, "答题状态")
    answer_status = "correct" if record_type == "correct" else "incorrect"
    if any(hint in answer_status_label for hint in ("待确认", "不确定")):
        answer_status = "uncertain"
    question_number = extract_labeled(assistant_text, "题号", "试卷题号")
    subject = extract_labeled(assistant_text, "科目") or guess_subject(f"{user_text}\n{assistant_text}")
    topic = extract_labeled(assistant_text, "题型", "主题") or guess_topic(
        f"{user_text}\n{assistant_text}", subject
    )
    unit = extract_labeled(assistant_text, "单元") or guess_unit(topic, subject)
    correct_answer = extract_answer(assistant_text)
    question_text = extract_labeled(assistant_text, "题目原文", "原题", "题目")
    user_answer = extract_labeled(assistant_text, "学生作答", "你的答案", "孩子作答", "原答案")
    wrong_reason = extract_labeled(assistant_text, "错误原因", "错因")
    explanation = extract_labeled(assistant_text, "讲解摘要", "讲解")
    detailed_solution = extract_markdown_section(assistant_text, "解题过程", "正确做法")
    knowledge_points = extract_list(assistant_text, "知识点") or guess_knowledge_points(topic, subject)
    visual_label = extract_labeled(assistant_text, "图形题", "需要图形")
    visual_question = visual_label.strip().lower() in {"是", "有", "yes", "true", "1"}
    if not visual_label.strip():
        visual_question = bool(
            subject == "数学"
            and image_path
            and VISUAL_RECORD_PATTERN.search(f"{topic}\n{question_text}\n{' '.join(knowledge_points)}")
        )
    record_key = source_key or message_id or str(user_record.get("id") or "")
    source = {
        "channel": "feishu",
        "peer_id": sender_id,
        "message_id": message_id,
        "assistant_message_id": assistant_id,
        "session_file": str(session_file),
        "record_index": record_index,
        "record_count": record_count,
        "record_key": record_key,
        "source_page": source_page,
        "crop_box": crop_box,
        "visual_question": visual_question,
    }
    card = {
        "id": str(uuid.uuid4()),
        "user_id": APP_USER_ID,
        "created_at": record_time(user_record),
        "record_type": record_type,
        "answer_status": answer_status,
        "question_number": question_number,
        "english_focuses": [],
        "subject": subject,
        "unit": unit,
        "topic": topic,
        "question_type": "飞书确认收集",
        "knowledge_points": knowledge_points,
        "grammar_errors": guess_grammar_errors(assistant_text, subject),
        "misspelled_words": [],
        "wrong_reason": wrong_reason or ("本题作答正确，用于整卷能力判断。" if record_type == "correct" else "已确认有错，具体错因见讲解。"),
        "explanation_summary": explanation or assistant_text[:900],
        "question_text": question_text,
        "correct_answer": correct_answer,
        "user_answer": user_answer or ("作答正确" if record_type == "correct" else ("未作答" if record_type != "mistake" else "未完整记录")),
        "image_path": image_path,
        "memory_scope": f"{APP_SCOPE_PREFIX}:{sender_id}" if sender_id else "feishu:unknown",
        "source_channel": "feishu",
        "source_peer": sender_id,
        "source_session": record_key,
        "tags": [
            "飞书确认收集",
            "识别已确认",
            {"mistake": "错题", "unsolved": "不会做", "reinforcement": "强化学习", "correct": "答对题"}[record_type],
            *(["图形题"] if visual_question else []),
        ],
        "source": source,
        "raw": {"user_message": message, "assistant_text": assistant_text},
        "feynman_explain": detailed_solution or explanation,
        "feynman_stuck": "",
        "next_review_at": now_iso(),
        "last_reviewed_at": "",
        "review_count": 0,
    }
    return card


def record_time(record: dict[str, Any]) -> str:
    message = record.get("message") or {}
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
    timestamp = record.get("timestamp")
    if timestamp:
        try:
            return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return now_iso()


def guess_subject(text: str) -> str:
    lower = text.lower()
    if any(hint.lower() in lower for hint in ENGLISH_HINTS):
        return "英语"
    if any(hint.lower() in lower for hint in MATH_HINTS):
        return "数学"
    return "待整理"


def guess_topic(text: str, subject: str) -> str:
    checks = [
        ("圆柱侧面积", ("圆柱", "侧面积")),
        ("圆柱圆锥体积", ("圆锥", "体积")),
        ("比例应用", ("比例",)),
        ("百分数应用", ("百分",)),
        ("浓度问题", ("浓度", "含糖率", "含盐率")),
        ("函数图像", ("函数", "图像")),
        ("英语拼写", ("拼写", "单词")),
        ("英语语法", ("语法", "句子")),
    ]
    for topic, hints in checks:
        if all(hint in text for hint in hints):
            return topic
    if subject == "英语":
        return "飞书英语错题"
    if subject == "数学":
        return "飞书数学错题"
    return "飞书待整理错题"


def guess_unit(topic: str, subject: str) -> str:
    if "圆柱" in topic or "圆锥" in topic:
        return "圆柱与圆锥"
    if "比例" in topic:
        return "比例"
    if "百分" in topic or "浓度" in topic:
        return "百分数应用题"
    if "函数" in topic:
        return "函数"
    if subject == "英语":
        return "英语错题"
    return "飞书自动收集"


def guess_knowledge_points(topic: str, subject: str) -> list[str]:
    if topic == "圆柱侧面积":
        return ["圆柱侧面积", "圆的周长", "侧面展开图"]
    if "圆柱圆锥" in topic:
        return ["圆柱体积", "圆锥体积"]
    if "比例" in topic:
        return ["比例", "比的应用"]
    if "浓度" in topic:
        return ["浓度问题", "百分数"]
    if "百分" in topic:
        return ["百分数", "单位1"]
    if "函数" in topic:
        return ["函数", "图像判断"]
    if subject == "英语":
        return ["英语错题"]
    return ["待整理"]


def guess_grammar_errors(text: str, subject: str) -> list[str]:
    if subject != "英语":
        return []
    errors: list[str] = []
    if "过去式" in text:
        errors.append("一般过去时")
    if "大小写" in text:
        errors.append("大小写")
    if "拼写" in text:
        errors.append("拼写")
    return errors


def extract_answer(text: str) -> str:
    labeled = extract_labeled(text, "正确答案", "答案")
    if labeled:
        return labeled
    patterns = [
        r"(?:^|\n)\s*(?:[-*]\s*)?(?:\*\*)?正确答案(?:\*\*)?\s*[:：]\s*([^\n]+)",
        r"正确答案是[:：]\s*\*\*([^*\n]+)\*\*",
        r"正确答案是[:：]\s*([^\n。]+)",
        r"答案是[:：]\s*\*\*([^*\n]+)\*\*",
        r"答案是[:：]\s*([^\n。]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip().strip("*").strip()
    return ""


def card_is_complete(card: dict[str, Any]) -> bool:
    knowledge_points = card.get("knowledge_points") or []
    source = card.get("source") or {}
    visual_ready = not source.get("visual_question") or bool(
        card.get("image_path") and source.get("crop_box")
    )
    return bool(
        card.get("subject")
        and card.get("subject") != "待整理"
        and card.get("record_type") in {"mistake", "unsolved", "reinforcement", "correct"}
        and card.get("question_text")
        and knowledge_points
        and "待整理" not in knowledge_points
        and card.get("correct_answer")
        and card.get("wrong_reason")
        and visual_ready
    )


def already_saved(conn: sqlite3.Connection, card: dict[str, Any], key: str) -> bool:
    source = card["source"]
    message_id = source.get("message_id") or ""
    record_index = int(source.get("record_index") or 1)
    source_session = card.get("source_session") or key
    image_path = card.get("image_path") or ""
    row = conn.execute("SELECT 1 FROM mistake_cards WHERE source_session = ? LIMIT 1", (source_session,)).fetchone()
    if row:
        return True
    # Records saved before multi-question support used the bare message id and
    # image path as their dedupe key. Treat that legacy row as the first card,
    # while allowing later questions from the same page to be added.
    if record_index == 1 and message_id:
        rows = conn.execute("SELECT raw_json, source_json FROM mistake_cards WHERE source_channel = 'feishu'").fetchall()
        for row in rows:
            blob = f"{row['raw_json'] or ''}\n{row['source_json'] or ''}"
            if message_id and message_id in blob:
                return True
    if record_index == 1 and image_path:
        image_name = Path(image_path).name
        rows = conn.execute("SELECT image_path, raw_json, source_json FROM mistake_cards WHERE source_channel = 'feishu'").fetchall()
        for row in rows:
            saved_path = row["image_path"] or ""
            if saved_path == image_path or (image_name and image_name in saved_path):
                return True
            blob = f"{row['raw_json'] or ''}\n{row['source_json'] or ''}"
            if image_name and image_name in blob:
                return True
    return False


def save_card(conn: sqlite3.Connection, card: dict[str, Any]) -> None:
    card["image_path"] = normalize_image_path(str(card.get("image_path") or ""))
    crop_box = (card.get("source") or {}).get("crop_box") or []
    if crop_box:
        card["image_path"] = crop_question_image(card["image_path"], crop_box)
    conn.execute(
        """
        INSERT INTO mistake_cards (
            id, user_id, created_at, record_type, answer_status, question_number,
            english_focuses_json, subject, unit_name, topic, question_type,
            knowledge_points_json, grammar_errors_json, misspelled_words_json,
            wrong_reason, explanation_summary, question_text, correct_answer, user_answer,
            image_path, memory_scope, source_channel, source_peer, source_session,
            tags_json, source_json, raw_json, mem0_id, feynman_explain, feynman_stuck,
            next_review_at, last_reviewed_at, review_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card["id"],
            card["user_id"],
            card["created_at"],
            card["record_type"],
            card["answer_status"],
            card["question_number"],
            esc_json(card["english_focuses"]),
            card["subject"],
            card["unit"],
            card["topic"],
            card["question_type"],
            esc_json(card["knowledge_points"]),
            esc_json(card["grammar_errors"]),
            esc_json(card["misspelled_words"]),
            card["wrong_reason"],
            card["explanation_summary"],
            card["question_text"],
            card["correct_answer"],
            card["user_answer"],
            card["image_path"],
            card["memory_scope"],
            card["source_channel"],
            card["source_peer"],
            card["source_session"],
            esc_json(card["tags"]),
            esc_json(card["source"]),
            esc_json(card["raw"]),
            None,
            card["feynman_explain"],
            card["feynman_stuck"],
            card["next_review_at"],
            card["last_reviewed_at"],
            card["review_count"],
        ),
    )
    conn.commit()


def sync_once(session_files: list[str] | None = None) -> dict[str, Any]:
    state = load_state()
    processed: set[str] = set(state.get("processed") or [])
    conn = ensure_db()
    added: list[dict[str, str]] = []
    skipped = 0
    pending = 0
    rejected = 0
    for session_file in iter_feishu_session_files(session_files):
        records = read_jsonl(session_file)
        for user_record, assistant_text, assistant_id in pair_feishu_messages(records):
            message = user_record.get("message") or {}
            message_id = extract_message_id(str(message.get("content") or ""))
            key = message_id or str(user_record.get("id") or "")
            if not key:
                skipped += 1
                continue
            confirmed_records = split_confirmed_records(assistant_text)
            if not confirmed_records:
                if key in processed:
                    skipped += 1
                    continue
                if collection_status(assistant_text) == "pending":
                    pending += 1
                processed.add(key)
                skipped += 1
                continue
            for record_index, record_text in enumerate(confirmed_records, start=1):
                record_key = f"{key}#record:{record_index}"
                if record_key in processed:
                    skipped += 1
                    continue
                card = build_card(
                    user_record,
                    record_text,
                    assistant_id,
                    session_file,
                    records,
                    record_index=record_index,
                    record_count=len(confirmed_records),
                    source_key=record_key,
                )
                if not card_is_complete(card):
                    processed.add(record_key)
                    rejected += 1
                    continue
                if already_saved(conn, card, record_key):
                    processed.add(record_key)
                    skipped += 1
                    continue
                save_card(conn, card)
                processed.add(record_key)
                added.append({"id": card["id"], "record_type": card["record_type"], "subject": card["subject"], "topic": card["topic"]})
    state["processed"] = list(processed)
    state["last_run_at"] = now_iso()
    save_state(state)
    return {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "pending": pending,
        "rejected": rejected,
        "processed": len(processed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Feishu homework mistakes into the local notebook.")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--session-file", action="append", help="只同步指定会话文件，主要用于验证")
    parser.add_argument("--enrich", action="store_true", help="明确允许将错题内容交给已配置的 AI 生成强化内容")
    parser.add_argument("--enrich-limit", type=int, default=3, help="每轮最多生成几条强化内容")
    parser.add_argument("--no-notebook", action="store_true", help="循环同步时不同时启动本机错题本页面")
    parser.add_argument("--notebook-port", type=int, default=NOTEBOOK_PORT)
    args = parser.parse_args()
    if not args.loop:
        print(json.dumps(sync_once(args.session_file), ensure_ascii=False, indent=2))
        return 0
    notebook_server = None
    if not args.no_notebook:
        from http.server import ThreadingHTTPServer

        from web_app import AppHandler, connect_db as connect_web_db

        connect_web_db().close()
        notebook_server = ThreadingHTTPServer((NOTEBOOK_HOST, args.notebook_port), AppHandler)
        notebook_server.access_code = ""
        threading.Thread(target=notebook_server.serve_forever, name="homework-notebook", daemon=True).start()
        print(
            json.dumps(
                {"ok": True, "notebook": f"http://{NOTEBOOK_HOST}:{args.notebook_port}"},
                ensure_ascii=False,
            ),
            flush=True,
        )
    while True:
        try:
            result = sync_once(args.session_file)
            enrichment = {"generated": [], "failed": []}
            if args.enrich:
                enrichment = enrich_pending(limit=max(1, args.enrich_limit))
            if result["added"] or enrichment.get("generated") or enrichment.get("failed"):
                result["enrichment"] = enrichment
                print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        time.sleep(max(3, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
