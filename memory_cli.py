#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mem0 import Memory


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LEDGER_DB = DATA_DIR / "mistakes.sqlite3"
QDRANT_DIR = DATA_DIR / "qdrant"
MEM0_HOME = DATA_DIR / "mem0_home"
MEM0_HISTORY_DB = DATA_DIR / "mem0_history.db"
DEFAULT_USER_ID = "student-default"
DEFAULT_CHANNEL = "telegram"
WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'-]*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    QDRANT_DIR.mkdir(parents=True, exist_ok=True)
    MEM0_HOME.mkdir(parents=True, exist_ok=True)


def first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_source(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    return source


def looks_like_word(value: str) -> bool:
    return bool(WORD_RE.fullmatch(value.strip()))


def normalize_misspelled_words(
    subject: str,
    user_answer: str,
    correct_answer: str,
    items: list[dict[str, str]],
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        wrong = str(item.get("wrong") or "").strip()
        correct = str(item.get("correct") or "").strip()
        if (
            subject == "英语"
            and wrong
            and not correct
            and looks_like_word(wrong)
            and looks_like_word(correct_answer)
            and wrong.lower() != correct_answer.strip().lower()
        ):
            correct = correct_answer.strip()
        if not wrong:
            continue
        key = (wrong.lower(), correct.lower())
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"wrong": wrong, "correct": correct})
    if (
        subject == "英语"
        and not normalized
        and looks_like_word(user_answer)
        and looks_like_word(correct_answer)
        and user_answer.strip().lower() != correct_answer.strip().lower()
    ):
        normalized.append(
            {
                "wrong": user_answer.strip(),
                "correct": correct_answer.strip(),
            }
        )
    return normalized


def derive_memory_scope(
    user_id: str,
    source: dict[str, Any],
    *,
    channel: str | None = None,
    peer_id: str | None = None,
    explicit_scope: str | None = None,
) -> str:
    if explicit_scope:
        return explicit_scope
    source_channel = first_text(channel, source.get("channel"), DEFAULT_CHANNEL)
    source_peer = first_text(
        peer_id,
        source.get("peer_id"),
        source.get("sender_id"),
        source.get("sender"),
        source.get("from"),
        source.get("user_id"),
        source.get("session"),
        user_id,
    )
    return f"{source_channel}:{source_peer}"


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def backfill_scope_fields(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, user_id, source_json, memory_scope, source_channel, source_peer, source_session
        FROM mistake_cards
        """
    ).fetchall()
    for row in rows:
        source = json.loads(row["source_json"] or "{}")
        channel = first_text(source.get("channel"), DEFAULT_CHANNEL)
        peer = first_text(
            source.get("peer_id"),
            source.get("sender_id"),
            source.get("sender"),
            source.get("from"),
            source.get("user_id"),
        )
        session = first_text(source.get("session"), source.get("chat_id"))
        scope = row["memory_scope"] or derive_memory_scope(str(row["user_id"]), source)
        conn.execute(
            """
            UPDATE mistake_cards
            SET memory_scope = ?, source_channel = ?, source_peer = ?, source_session = ?
            WHERE id = ?
            """,
            (scope, channel, peer, session, row["id"]),
        )
    conn.commit()


def connect_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(LEDGER_DB)
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
    ensure_column(conn, "mistake_cards", "memory_scope", "TEXT")
    ensure_column(conn, "mistake_cards", "source_channel", "TEXT")
    ensure_column(conn, "mistake_cards", "source_peer", "TEXT")
    ensure_column(conn, "mistake_cards", "source_session", "TEXT")
    ensure_column(conn, "mistake_cards", "record_type", "TEXT NOT NULL DEFAULT 'mistake'")
    ensure_column(conn, "mistake_cards", "question_text", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mistake_cards_user_created ON mistake_cards(user_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mistake_cards_scope_created ON mistake_cards(memory_scope, created_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_cards_subject ON mistake_cards(subject)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_cards_unit ON mistake_cards(unit_name)")
    backfill_scope_fields(conn)
    conn.commit()
    return conn


def build_mem0() -> Memory:
    ensure_dirs()
    os.environ["MEM0_DIR"] = str(MEM0_HOME)
    config = {
        "llm": {
            "provider": "ollama",
            "config": {
                "model": "qwen3.5:4b",
                "ollama_base_url": "http://127.0.0.1:11434",
                "temperature": 0,
                "max_tokens": 512,
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": "all-minilm:l6",
                "ollama_base_url": "http://127.0.0.1:11434",
                "embedding_dims": 384,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "openclaw_homework_memories",
                "path": str(QDRANT_DIR),
                "on_disk": True,
                "embedding_model_dims": 384,
            },
        },
        "history_db_path": str(MEM0_HISTORY_DB),
    }
    return Memory.from_config(config)


@dataclass
class MistakeCard:
    id: str
    user_id: str
    memory_scope: str
    created_at: str
    record_type: str
    subject: str
    unit: str
    topic: str
    question_type: str
    knowledge_points: list[str]
    grammar_errors: list[str]
    misspelled_words: list[dict[str, str]]
    wrong_reason: str
    explanation_summary: str
    question_text: str
    correct_answer: str
    user_answer: str
    image_path: str
    source_channel: str
    source_peer: str
    source_session: str
    tags: list[str]
    source: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MistakeCard":
        normalized = dict(payload)
        source = normalize_source(normalized)
        user_id = first_text(normalized.get("user_id"), DEFAULT_USER_ID)
        source_channel = first_text(normalized.get("source_channel"), source.get("channel"), DEFAULT_CHANNEL)
        source_peer = first_text(
            normalized.get("source_peer"),
            source.get("peer_id"),
            source.get("sender_id"),
            source.get("sender"),
            source.get("from"),
            source.get("user_id"),
        )
        source_session = first_text(
            normalized.get("source_session"),
            source.get("session"),
            source.get("chat_id"),
        )
        memory_scope = derive_memory_scope(
            user_id,
            source,
            channel=source_channel,
            peer_id=source_peer or source_session,
            explicit_scope=first_text(normalized.get("memory_scope")),
        )
        source["channel"] = source_channel
        if source_peer:
            source["peer_id"] = source_peer
        if source_session:
            source["session"] = source_session
        normalized["user_id"] = user_id
        normalized["source"] = source
        normalized["memory_scope"] = memory_scope
        normalized["source_channel"] = source_channel
        normalized["source_peer"] = source_peer
        normalized["source_session"] = source_session
        card_id = normalized.get("id") or str(uuid.uuid4())
        created_at = normalized.get("created_at") or now_iso()
        record_type = str(normalized.get("record_type") or "mistake")
        if record_type not in {"mistake", "unsolved", "reinforcement"}:
            record_type = "mistake"
        subject = str(normalized.get("subject") or "")
        unit = str(normalized.get("unit") or "")
        topic = str(normalized.get("topic") or "")
        question_type = str(normalized.get("question_type") or "")
        knowledge_points = [str(x) for x in normalized.get("knowledge_points") or []]
        grammar_errors = [str(x) for x in normalized.get("grammar_errors") or []]
        misspelled_words = []
        for item in normalized.get("misspelled_words") or []:
            if isinstance(item, dict):
                misspelled_words.append(
                    {
                        "wrong": str(item.get("wrong") or ""),
                        "correct": str(item.get("correct") or ""),
                    }
                )
            else:
                misspelled_words.append({"wrong": str(item), "correct": ""})
        misspelled_words = normalize_misspelled_words(
            subject,
            str(normalized.get("user_answer") or ""),
            str(normalized.get("correct_answer") or ""),
            misspelled_words,
        )
        tags = [str(x) for x in normalized.get("tags") or []]
        return cls(
            id=card_id,
            user_id=user_id,
            memory_scope=memory_scope,
            created_at=created_at,
            record_type=record_type,
            subject=subject,
            unit=unit,
            topic=topic,
            question_type=question_type,
            knowledge_points=knowledge_points,
            grammar_errors=grammar_errors,
            misspelled_words=misspelled_words,
            wrong_reason=str(normalized.get("wrong_reason") or ""),
            explanation_summary=str(normalized.get("explanation_summary") or ""),
            question_text=str(normalized.get("question_text") or ""),
            correct_answer=str(normalized.get("correct_answer") or ""),
            user_answer=str(normalized.get("user_answer") or ""),
            image_path=str(normalized.get("image_path") or ""),
            source_channel=source_channel,
            source_peer=source_peer,
            source_session=source_session,
            tags=tags,
            source=source,
            raw=normalized,
        )


def build_memory_text(card: MistakeCard) -> str:
    record_label = {
        "mistake": "错题",
        "unsolved": "不会做",
        "reinforcement": "强化学习",
    }.get(card.record_type, "错题")
    misspelled = ", ".join(
        f"{item.get('wrong', '')}->{item.get('correct', '')}" for item in card.misspelled_words if item.get("wrong")
    )
    parts = [
        f"这是一条学生学习记录，类型是：{record_label}。",
        f"科目：{card.subject or '未标注'}。",
        f"单元：{card.unit or '未标注'}。",
        f"主题：{card.topic or '未标注'}。",
        f"题型：{card.question_type or '未标注'}。",
        f"知识点：{'、'.join(card.knowledge_points) or '未标注'}。",
        f"题目：{card.question_text or '未记录'}。",
        f"错因：{card.wrong_reason or '未标注'}。",
        f"讲解摘要：{card.explanation_summary or '未标注'}。",
        f"学生答案：{card.user_answer or '未记录'}。",
        f"正确答案：{card.correct_answer or '未记录'}。",
    ]
    if card.grammar_errors:
        parts.append(f"语法错误：{'、'.join(card.grammar_errors)}。")
    if misspelled:
        parts.append(f"拼错单词：{misspelled}。")
    if card.tags:
        parts.append(f"标签：{'、'.join(card.tags)}。")
    parts.append(f"记录时间：{card.created_at}。")
    return "".join(parts)


def payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["raw_json"])
    payload["mem0_id"] = row["mem0_id"]
    payload["id"] = row["id"]
    payload["created_at"] = row["created_at"]
    payload["memory_scope"] = row["memory_scope"]
    payload["source_channel"] = row["source_channel"]
    payload["source_peer"] = row["source_peer"]
    payload["source_session"] = row["source_session"]
    source = normalize_source(payload)
    if row["source_channel"]:
        source["channel"] = row["source_channel"]
    if row["source_peer"]:
        source["peer_id"] = row["source_peer"]
    if row["source_session"]:
        source["session"] = row["source_session"]
    payload["source"] = source
    return payload


def save_card(conn: sqlite3.Connection, card: MistakeCard, mem0_id: str | None) -> None:
    conn.execute(
        """
        INSERT INTO mistake_cards (
            id, user_id, created_at, record_type, subject, unit_name, topic, question_type,
            knowledge_points_json, grammar_errors_json, misspelled_words_json,
            wrong_reason, explanation_summary, question_text, correct_answer, user_answer,
            image_path, memory_scope, source_channel, source_peer, source_session,
            tags_json, source_json, raw_json, mem0_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            user_id = excluded.user_id,
            created_at = excluded.created_at,
            record_type = excluded.record_type,
            subject = excluded.subject,
            unit_name = excluded.unit_name,
            topic = excluded.topic,
            question_type = excluded.question_type,
            knowledge_points_json = excluded.knowledge_points_json,
            grammar_errors_json = excluded.grammar_errors_json,
            misspelled_words_json = excluded.misspelled_words_json,
            wrong_reason = excluded.wrong_reason,
            explanation_summary = excluded.explanation_summary,
            question_text = excluded.question_text,
            correct_answer = excluded.correct_answer,
            user_answer = excluded.user_answer,
            image_path = excluded.image_path,
            memory_scope = excluded.memory_scope,
            source_channel = excluded.source_channel,
            source_peer = excluded.source_peer,
            source_session = excluded.source_session,
            tags_json = excluded.tags_json,
            source_json = excluded.source_json,
            raw_json = excluded.raw_json,
            mem0_id = excluded.mem0_id
        """,
        (
            card.id,
            card.user_id,
            card.created_at,
            card.record_type,
            card.subject,
            card.unit,
            card.topic,
            card.question_type,
            json.dumps(card.knowledge_points, ensure_ascii=False),
            json.dumps(card.grammar_errors, ensure_ascii=False),
            json.dumps(card.misspelled_words, ensure_ascii=False),
            card.wrong_reason,
            card.explanation_summary,
            card.question_text,
            card.correct_answer,
            card.user_answer,
            card.image_path,
            card.memory_scope,
            card.source_channel,
            card.source_peer,
            card.source_session,
            json.dumps(card.tags, ensure_ascii=False),
            json.dumps(card.source, ensure_ascii=False),
            json.dumps(card.raw, ensure_ascii=False),
            mem0_id,
        ),
    )
    conn.commit()


def load_payload(path: str | None) -> dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def json_print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def parse_days(value: int | None) -> str | None:
    if not value:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=value)
    return cutoff.isoformat()


def resolve_scope_args(args: argparse.Namespace) -> tuple[str, str]:
    user_id = first_text(getattr(args, "user_id", None), DEFAULT_USER_ID)
    explicit_scope = first_text(getattr(args, "scope", None))
    channel = first_text(getattr(args, "channel", None), DEFAULT_CHANNEL)
    peer_id = first_text(getattr(args, "peer_id", None), getattr(args, "session", None))
    source = {
        "channel": channel,
        "peer_id": getattr(args, "peer_id", None),
        "session": getattr(args, "session", None),
    }
    return user_id, derive_memory_scope(
        user_id,
        source,
        channel=channel,
        peer_id=peer_id,
        explicit_scope=explicit_scope,
    )


def add_scope_arguments(parser: argparse.ArgumentParser, *, include_user_id: bool = True) -> None:
    if include_user_id:
        parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--channel")
    parser.add_argument("--peer-id")
    parser.add_argument("--session")
    parser.add_argument("--scope")


def cmd_init(args: argparse.Namespace) -> int:
    connect_db().close()
    if args.skip_mem0:
        json_print({"ok": True, "message": "ledger_ready_only"})
        return 0
    memory = build_mem0()
    user_id, memory_scope = resolve_scope_args(args)
    json_print(
        {
            "ok": True,
            "message": "mem0_ready",
            "collection": "openclaw_homework_memories",
            "history_db": str(MEM0_HISTORY_DB),
            "user_id": user_id,
            "memory_scope": memory_scope,
            "memory_count": len(memory.get_all(user_id=memory_scope)["results"]),
        }
    )
    return 0


def cmd_add_card(args: argparse.Namespace) -> int:
    payload = load_payload(args.file)
    if args.user_id and (args.user_id != DEFAULT_USER_ID or not payload.get("user_id")):
        payload["user_id"] = args.user_id
    source = normalize_source(payload)
    if args.channel:
        source["channel"] = args.channel
    if args.peer_id:
        source["peer_id"] = args.peer_id
    if args.session:
        source["session"] = args.session
    if source:
        payload["source"] = source
    if args.scope:
        payload["memory_scope"] = args.scope
    card = MistakeCard.from_payload(payload)
    conn = connect_db()
    memory = build_mem0()
    memory_text = build_memory_text(card)
    add_result = memory.add(
        [{"role": "user", "content": memory_text}],
        user_id=card.memory_scope,
        metadata={
            "record_id": card.id,
            "memory_scope": card.memory_scope,
            "source_channel": card.source_channel,
            "subject": card.subject,
            "unit_name": card.unit,
            "topic": card.topic,
            "question_type": card.question_type,
        },
        infer=False,
    )
    mem0_id = None
    results = add_result.get("results") or []
    if results:
        mem0_id = results[0].get("id")
    save_card(conn, card, mem0_id)
    json_print(
        {
            "ok": True,
            "record_id": card.id,
            "mem0_id": mem0_id,
            "user_id": card.user_id,
            "memory_scope": card.memory_scope,
            "channel": card.source_channel,
            "subject": card.subject,
            "unit": card.unit,
            "topic": card.topic,
        }
    )
    return 0


def fetch_rows_by_ids(conn: sqlite3.Connection, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM mistake_cards WHERE id IN ({placeholders}) ORDER BY created_at DESC",
        ids,
    ).fetchall()
    by_id = {row["id"]: payload_from_row(row) for row in rows}
    return [by_id[row_id] for row_id in ids if row_id in by_id]


def filter_cards(
    cards: list[dict[str, Any]],
    subject: str | None,
    unit: str | None,
    cutoff: str | None,
    *,
    memory_scope: str | None = None,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    filtered = []
    for card in cards:
        if memory_scope and card.get("memory_scope") != memory_scope:
            continue
        source = normalize_source(card)
        if channel and first_text(card.get("source_channel"), source.get("channel")) != channel:
            continue
        if subject and card.get("subject") != subject:
            continue
        if unit and card.get("unit") != unit:
            continue
        created_at = str(card.get("created_at") or "")
        if cutoff and created_at and created_at < cutoff:
            continue
        filtered.append(card)
    return filtered


def cmd_search(args: argparse.Namespace) -> int:
    conn = connect_db()
    memory = build_mem0()
    user_id, memory_scope = resolve_scope_args(args)
    filters: dict[str, Any] = {}
    if args.subject:
        filters["subject"] = args.subject
    if args.unit:
        filters["unit_name"] = args.unit
    cutoff = parse_days(args.days)

    result = memory.search(
        args.query,
        user_id=memory_scope,
        limit=max(args.limit * 3, args.limit),
        filters=filters or None,
        rerank=False,
    )
    matched_record_ids: list[str] = []
    ranked = []
    for item in result.get("results") or []:
        metadata = item.get("metadata") or {}
        record_id = metadata.get("record_id")
        if not record_id:
            row = conn.execute("SELECT id FROM mistake_cards WHERE mem0_id = ?", (item.get("id"),)).fetchone()
            record_id = row["id"] if row else None
        if record_id and record_id not in matched_record_ids:
            matched_record_ids.append(record_id)
            ranked.append(
                {
                    "record_id": record_id,
                    "score": item.get("score"),
                    "memory": item.get("memory"),
                }
            )
        if len(matched_record_ids) >= args.limit:
            break

    cards = fetch_rows_by_ids(conn, matched_record_ids)
    cards = filter_cards(
        cards,
        args.subject,
        args.unit,
        cutoff,
        memory_scope=memory_scope,
        channel=args.channel,
    )
    cards = cards[: args.limit]
    json_print(
        {
            "ok": True,
            "query": args.query,
            "user_id": user_id,
            "memory_scope": memory_scope,
            "filters": filters,
            "matches": cards,
            "ranked": ranked,
        }
    )
    return 0


def cmd_find_unit(args: argparse.Namespace) -> int:
    conn = connect_db()
    user_id, memory_scope = resolve_scope_args(args)
    clauses = ["memory_scope = ?", "unit_name = ?"]
    params: list[Any] = [memory_scope, args.unit]
    if args.subject:
        clauses.append("subject = ?")
        params.append(args.subject)
    cutoff = parse_days(args.days)
    if cutoff:
        clauses.append("created_at >= ?")
        params.append(cutoff)
    rows = conn.execute(
        f"SELECT * FROM mistake_cards WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        [*params, args.limit],
    ).fetchall()
    json_print(
        {
            "ok": True,
            "user_id": user_id,
            "memory_scope": memory_scope,
            "unit": args.unit,
            "matches": [payload_from_row(row) for row in rows],
        }
    )
    return 0


def cmd_grammar_stats(args: argparse.Namespace) -> int:
    conn = connect_db()
    user_id, memory_scope = resolve_scope_args(args)
    clauses = ["memory_scope = ?", "subject = ?"]
    params: list[Any] = [memory_scope, args.subject]
    cutoff = parse_days(args.days)
    if cutoff:
        clauses.append("created_at >= ?")
        params.append(cutoff)
    rows = conn.execute(
        f"SELECT grammar_errors_json FROM mistake_cards WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    counter: Counter[str] = Counter()
    for row in rows:
        for item in json.loads(row["grammar_errors_json"]):
            if item:
                counter[item] += 1
    json_print(
        {
            "ok": True,
            "user_id": user_id,
            "memory_scope": memory_scope,
            "subject": args.subject,
            "days": args.days,
            "stats": [{"name": name, "count": count} for name, count in counter.most_common()],
        }
    )
    return 0


def cmd_spelling_stats(args: argparse.Namespace) -> int:
    conn = connect_db()
    user_id, memory_scope = resolve_scope_args(args)
    clauses = ["memory_scope = ?", "subject = ?"]
    params: list[Any] = [memory_scope, args.subject]
    cutoff = parse_days(args.days)
    if cutoff:
        clauses.append("created_at >= ?")
        params.append(cutoff)
    rows = conn.execute(
        f"SELECT misspelled_words_json FROM mistake_cards WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    counter: Counter[tuple[str, str]] = Counter()
    for row in rows:
        for item in json.loads(row["misspelled_words_json"]):
            wrong = (item.get("wrong") or "").strip()
            correct = (item.get("correct") or "").strip()
            if wrong:
                counter[(wrong, correct)] += 1
    stats = [
        {"wrong": wrong, "correct": correct, "count": count}
        for (wrong, correct), count in counter.most_common()
    ]
    json_print(
        {
            "ok": True,
            "user_id": user_id,
            "memory_scope": memory_scope,
            "subject": args.subject,
            "days": args.days,
            "stats": stats,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw homework memory tools powered by Mem0.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Prepare ledger and Mem0 store.")
    add_scope_arguments(init_parser)
    init_parser.add_argument("--skip-mem0", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    add_parser = subparsers.add_parser("add-card", help="Add one structured mistake card.")
    add_parser.add_argument("--file", help="Path to a JSON file. If omitted, reads stdin.")
    add_scope_arguments(add_parser)
    add_parser.set_defaults(func=cmd_add_card)

    search_parser = subparsers.add_parser("search", help="Semantic search against stored mistakes.")
    search_parser.add_argument("query")
    add_scope_arguments(search_parser)
    search_parser.add_argument("--subject")
    search_parser.add_argument("--unit")
    search_parser.add_argument("--days", type=int)
    search_parser.add_argument("--limit", type=int, default=8)
    search_parser.set_defaults(func=cmd_search)

    unit_parser = subparsers.add_parser("find-unit", help="List mistakes from one unit.")
    unit_parser.add_argument("unit")
    add_scope_arguments(unit_parser)
    unit_parser.add_argument("--subject")
    unit_parser.add_argument("--days", type=int)
    unit_parser.add_argument("--limit", type=int, default=20)
    unit_parser.set_defaults(func=cmd_find_unit)

    grammar_parser = subparsers.add_parser("grammar-stats", help="Count grammar mistakes.")
    add_scope_arguments(grammar_parser)
    grammar_parser.add_argument("--subject", default="英语")
    grammar_parser.add_argument("--days", type=int)
    grammar_parser.set_defaults(func=cmd_grammar_stats)

    spelling_parser = subparsers.add_parser("spelling-stats", help="Count repeated misspellings.")
    add_scope_arguments(spelling_parser)
    spelling_parser.add_argument("--subject", default="英语")
    spelling_parser.add_argument("--days", type=int)
    spelling_parser.set_defaults(func=cmd_spelling_stats)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
