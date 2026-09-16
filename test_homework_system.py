from __future__ import annotations

import base64
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import feishu_mistake_sync
import learning_enrichment
import web_app


def fake_openclaw_output() -> str:
    pack = {
        "knowledge_summary": "圆柱侧面积等于底面周长乘高。",
        "core_method": ["先求底面周长", "再乘圆柱的高"],
        "common_traps": ["把半径误当成直径", "把侧面积和表面积混淆"],
        "self_check": "最后检查是否只求侧面积以及单位是否统一。",
        "exercises": [
            {"level": "基础", "question": "半径2厘米、高5厘米的圆柱侧面积是多少？", "hint": "先求周长。", "answer": "2×π×2×5=20π平方厘米。"},
            {"level": "变化", "question": "直径6厘米、高4厘米的圆柱侧面积是多少？", "hint": "可直接用πdh。", "answer": "π×6×4=24π平方厘米。"},
            {"level": "综合", "question": "一个圆柱侧面展开是长12厘米、宽7厘米的长方形，侧面积是多少？", "hint": "展开图面积就是侧面积。", "answer": "12×7=84平方厘米。"},
        ],
    }
    return json.dumps(
        {
            "status": "ok",
            "result": {"finalAssistantVisibleText": json.dumps(pack, ensure_ascii=False)},
        },
        ensure_ascii=False,
    )


def fake_fresh_openclaw_output() -> str:
    pack = json.loads(json.loads(fake_openclaw_output())["result"]["finalAssistantVisibleText"])
    for index, exercise in enumerate(pack["exercises"], start=1):
        exercise["question"] = f"第{index}道新练习：{exercise['question']}"
    return json.dumps(
        {"status": "ok", "result": {"finalAssistantVisibleText": json.dumps(pack, ensure_ascii=False)}},
        ensure_ascii=False,
    )


class HomeworkSystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "mistakes.sqlite3"
        self.original_web_db = web_app.DB_PATH
        web_app.DB_PATH = self.db_path
        with web_app.connect_db():
            pass

    def tearDown(self) -> None:
        web_app.DB_PATH = self.original_web_db
        self.tmp.cleanup()

    def sample_payload(self, record_type: str = "unsolved") -> dict:
        return {
            "record_type": record_type,
            "subject": "数学",
            "unit": "圆柱与圆锥",
            "topic": "圆柱侧面积",
            "question_type": "计算题",
            "knowledge_points": ["圆柱侧面积", "圆的周长"],
            "question_text": "一个圆柱底面半径3厘米，高5厘米，求侧面积。",
            "user_answer": "未作答",
            "correct_answer": "30π平方厘米",
            "wrong_reason": "不会把侧面展开图与底面周长联系起来",
            "explanation_summary": "侧面积等于底面周长乘高。",
        }

    def save_sample(self, record_type: str = "unsolved") -> dict:
        with web_app.connect_db() as conn:
            return web_app.save_card(conn, self.sample_payload(record_type))

    def test_feishu_card_records_message_time_subject_and_type(self) -> None:
        user_record = {
            "id": "u1",
            "message": {
                "role": "user",
                "sourceChannel": "feishu",
                "senderId": "ou_test",
                "timestamp": 1785628800000,
                "content": "[message_id: om_1] 这道题我不会做",
            },
        }
        assistant = """记录类型：不会做
科目：数学
单元：圆柱与圆锥
题型：圆柱侧面积
知识点：圆柱侧面积、圆的周长
题目原文：一个圆柱底面半径3厘米，高5厘米，求侧面积。
学生作答：未作答
### 正确做法

1. 求底面周长
2 × π × 3 = 6π

2. 求侧面积
6π × 5 = 30π

正确答案：30π平方厘米
错误原因：不知道侧面积公式
讲解摘要：侧面积等于底面周长乘高。
【错题记录：已确认】"""
        card = feishu_mistake_sync.build_card(user_record, assistant, "a1", Path("session.jsonl"))
        self.assertEqual(card["record_type"], "unsolved")
        self.assertEqual(card["subject"], "数学")
        self.assertEqual(card["question_text"], "一个圆柱底面半径3厘米，高5厘米，求侧面积。")
        self.assertEqual(card["user_answer"], "未作答")
        self.assertIn("1. 求底面周长", card["feynman_explain"])
        self.assertIn("6π × 5 = 30π", card["feynman_explain"])
        self.assertNotIn("错误原因", card["feynman_explain"])
        self.assertTrue(card["created_at"].startswith("2026-08-02"))
        self.assertTrue(feishu_mistake_sync.card_is_complete(card))
        with web_app.connect_db() as conn:
            feishu_mistake_sync.save_card(conn, card)
            stored = web_app.list_cards(conn, {})[0]
        self.assertEqual(stored["record_type"], "unsolved")
        self.assertEqual(stored["question_text"], card["question_text"])

    def test_feishu_image_is_copied_and_rotated_before_storage(self) -> None:
        source = Path(self.tmp.name) / "sideways.jpg"
        source.write_bytes(b"representative image bytes")
        rotations = []

        def fake_rotator(command, **_kwargs):
            rotations.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        normalized = feishu_mistake_sync.normalize_image_path(
            str(source),
            output_dir=Path(self.tmp.name) / "source-images",
            detector=lambda _path: 270,
            rotator=fake_rotator,
        )
        normalized_path = Path(normalized)
        self.assertTrue(normalized_path.is_file())
        self.assertNotEqual(normalized_path, source)
        self.assertEqual(normalized_path.read_bytes(), source.read_bytes())
        self.assertEqual(rotations[0][1:3], ["--rotate", "270"])

    def test_feishu_image_rotation_failure_keeps_the_original(self) -> None:
        source = Path(self.tmp.name) / "sideways.jpg"
        source.write_bytes(b"representative image bytes")

        normalized = feishu_mistake_sync.normalize_image_path(
            str(source),
            output_dir=Path(self.tmp.name) / "source-images",
            detector=lambda _path: 90,
            rotator=lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
        )
        self.assertEqual(normalized, str(source))

    def test_orientation_helper_maps_detected_text_direction_to_the_same_rotation(self) -> None:
        helper = (Path(feishu_mistake_sync.__file__).parent / "detect_image_orientation.swift").read_text()
        self.assertIn('(\"up\", .up, 0)', helper)
        self.assertIn('(\"right\", .right, 90)', helper)
        self.assertIn('(\"down\", .down, 180)', helper)
        self.assertIn('(\"left\", .left, 270)', helper)

    def test_feishu_multi_page_batch_uses_the_page_for_each_record(self) -> None:
        records = [
            {
                "id": "page-1",
                "type": "message",
                "message": {"role": "user", "sourceChannel": "feishu", "MediaPath": "/tmp/page-1.jpg"},
            },
            {
                "id": "page-2",
                "type": "message",
                "message": {"role": "user", "sourceChannel": "feishu", "MediaPath": "/tmp/page-2.jpg"},
            },
            {
                "id": "done",
                "type": "message",
                "message": {
                    "role": "user",
                    "sourceChannel": "feishu",
                    "senderId": "ou_test",
                    "content": "[message_id: om_done] 发完了",
                },
            },
        ]
        assistant = """记录类型：错题
科目：数学
单元：图形
题型：面积
知识点：面积
原图页码：2
题目区域：5%, 10%, 90%, 20%
题目原文：求面积。
学生作答：10
正确答案：12
错误原因：漏算
讲解摘要：重新分块。
【错题记录：已确认】"""
        card = feishu_mistake_sync.build_card(
            records[-1], assistant, "a1", Path("session.jsonl"), records
        )
        self.assertEqual(card["image_path"], "/tmp/page-2.jpg")
        self.assertEqual(card["source"]["source_page"], 2)
        self.assertEqual(card["source"]["crop_box"], [5.0, 10.0, 90.0, 20.0])

    def test_feishu_split_records_inherits_the_current_page_heading(self) -> None:
        assistant = """## 第 1 页
题目原文：第一题
【错题记录：已确认】

题目原文：第二题
【错题记录：已确认】

## 第 2 页
题目原文：第三题
【错题记录：已确认】"""
        blocks = feishu_mistake_sync.split_confirmed_records(assistant)
        self.assertEqual([feishu_mistake_sync.extract_source_page(item) for item in blocks], [1, 1, 2])

    def test_question_crop_uses_percentages_and_creates_an_independent_image(self) -> None:
        source = Path(self.tmp.name) / "page.jpg"
        source.write_bytes(b"page image")
        commands = []

        def fake_runner(command, **_kwargs):
            commands.append(command)
            if "pixelWidth" in command:
                return SimpleNamespace(returncode=0, stdout="pixelWidth: 1000\npixelHeight: 1500\n", stderr="")
            output = Path(command[command.index("--out") + 1])
            output.write_bytes(b"cropped image")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        cropped = feishu_mistake_sync.crop_question_image(
            str(source),
            [5, 20, 90, 30],
            output_dir=Path(self.tmp.name) / "question-images",
            runner=fake_runner,
        )
        self.assertTrue(Path(cropped).is_file())
        crop_command = commands[1]
        self.assertEqual(crop_command[1:7], ["--cropToHeightWidth", "450", "900", "--cropOffset", "300", "50"])

    def test_manual_record_creates_pending_learning_pack(self) -> None:
        card = self.save_sample("reinforcement")
        self.assertEqual(card["record_type"], "reinforcement")
        self.assertEqual(card["record_type_label"], "强化学习")
        self.assertEqual(card["learning"]["status"], "pending")
        with web_app.connect_db() as conn:
            stats = web_app.build_stats(web_app.list_cards(conn, {}))
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["record_types"][0], {"name": "强化学习", "count": 1})

    def test_learning_pack_generation_and_page_data(self) -> None:
        card = self.save_sample()
        result = learning_enrichment.enrich_card(
            card["id"],
            db_path=self.db_path,
            runner=lambda _: fake_openclaw_output(),
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["pack"]["exercises"]), 3)
        for exercise in result["pack"]["exercises"]:
            self.assertTrue(exercise["answer"].startswith("1. "))
            self.assertIn("\n\n2. ", exercise["answer"])
        with web_app.connect_db() as conn:
            loaded = web_app.list_cards(conn, {})[0]
        self.assertEqual(loaded["learning"]["status"], "ready")
        self.assertIn("圆柱侧面积", loaded["learning"]["pack"]["knowledge_summary"])

    def test_learning_prompt_requires_the_shared_gpt_solution_format(self) -> None:
        prompt = learning_enrichment.build_learning_prompt(self.sample_payload())
        self.assertIn("每一步第一行写“数字. 短标题”", prompt)
        self.assertIn("一道数学算式独占一行", prompt)
        self.assertIn("最后一块必须写", prompt)
        self.assertIn("先用孩子能懂的话写出“核心关系”", prompt)
        self.assertIn("哪些公共部分可以同时抵消", prompt)
        self.assertIn("不要使用反引号或星号", prompt)

    def test_learning_generation_uses_configurable_openclaw_settings(self) -> None:
        self.assertEqual(learning_enrichment.OPENCLAW_AGENT, "main")
        self.assertEqual(learning_enrichment.OPENCLAW_MODEL, "")
        self.assertEqual(learning_enrichment.OPENCLAW_THINKING, "")
        self.assertGreaterEqual(learning_enrichment.GENERATION_TIMEOUT_SECONDS, 300)

    def test_fresh_practice_pack_avoids_old_questions_and_keeps_attempts(self) -> None:
        card = self.save_sample()
        old_learning = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        old_question = old_learning["pack"]["exercises"][0]["question"]
        with web_app.connect_db() as conn:
            web_app.save_practice_attempt(
                conn,
                {
                    "card_id": card["id"],
                    "subject": "数学",
                    "topic": "圆柱侧面积",
                    "prompt_text": old_question,
                    "answer_text": "20π平方厘米",
                },
            )
        prompts = []
        new_learning = learning_enrichment.enrich_card(
            card["id"],
            db_path=self.db_path,
            force=True,
            avoid_previous=True,
            runner=lambda prompt: prompts.append(prompt) or fake_fresh_openclaw_output(),
        )
        new_questions = [item["question"] for item in new_learning["pack"]["exercises"]]
        self.assertIn(old_question, prompts[0])
        self.assertNotIn(old_question, new_questions)
        with web_app.connect_db() as conn:
            self.assertEqual(len(web_app.list_practice_attempts(conn)), 1)

    def test_failed_fresh_practice_pack_keeps_the_old_questions(self) -> None:
        card = self.save_sample()
        old_learning = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        with self.assertRaisesRegex(ValueError, "不能与上一组重复"):
            learning_enrichment.enrich_card(
                card["id"],
                db_path=self.db_path,
                force=True,
                avoid_previous=True,
                runner=lambda _: fake_openclaw_output(),
            )
        with web_app.connect_db() as conn:
            loaded = web_app.list_cards(conn, {})[0]
        self.assertEqual(loaded["learning"]["status"], "ready")
        self.assertEqual(loaded["learning"]["pack"], old_learning["pack"])

    def test_invalid_generation_is_marked_failed(self) -> None:
        card = self.save_sample()
        invalid = json.dumps({"status": "ok", "result": {"finalAssistantVisibleText": "{}"}})
        with self.assertRaises(ValueError):
            learning_enrichment.enrich_card(card["id"], db_path=self.db_path, runner=lambda _: invalid)
        with sqlite3.connect(self.db_path) as conn:
            status, error = conn.execute(
                "SELECT status, error FROM learning_packs WHERE card_id = ?", (card["id"],)
            ).fetchone()
        self.assertEqual(status, "failed")
        self.assertTrue(error)

    def test_generated_exercise_cannot_depend_on_a_missing_picture(self) -> None:
        pack = json.loads(json.loads(fake_openclaw_output())["result"]["finalAssistantVisibleText"])
        pack["exercises"][0]["question"] = "根据图片写一句话。"
        invalid = json.dumps(
            {"status": "ok", "result": {"finalAssistantVisibleText": json.dumps(pack, ensure_ascii=False)}},
            ensure_ascii=False,
        )
        card = self.save_sample()
        with self.assertRaisesRegex(ValueError, "依赖未提供的图片"):
            learning_enrichment.enrich_card(card["id"], db_path=self.db_path, runner=lambda _: invalid)

    def test_shadow_area_practice_includes_matching_diagrams(self) -> None:
        payload = {
            **self.sample_payload("reinforcement"),
            "unit": "图形的面积问题",
            "topic": "阴影面积差",
            "question_text": "如图，在正方形ABCD中，以A、D为圆心作圆弧，求阴影面积S₁-S₂。",
            "knowledge_points": ["阴影面积差", "公共部分抵消", "圆弧"],
            "correct_answer": "13π/4-9",
        }
        with web_app.connect_db() as conn:
            card = web_app.save_card(conn, payload)
        result = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        self.assertEqual(len(result["pack"]["exercises"]), 3)
        for exercise in result["pack"]["exercises"]:
            self.assertEqual(exercise["diagram"]["kind"], "square_quarter_arcs")
            self.assertIn("如图", exercise["question"])
            self.assertIn(str(exercise["diagram"]["side"]), exercise["question"])

    def test_single_practice_renders_original_and_generated_figures(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("renderSourceFigure(card)", html)
        self.assertIn("renderPracticeDiagram(item.diagram", html)
        self.assertIn("/image';", html)

    def test_overlapping_triangle_practice_includes_matching_diagrams(self) -> None:
        payload = {
            **self.sample_payload("mistake"),
            "unit": "图形的面积问题",
            "topic": "重叠三角形阴影面积",
            "question_text": "如图，两个相同的直角三角形ABC和EFD叠在一起，求阴影面积。",
            "knowledge_points": ["全等图形面积相等", "公共部分抵消", "梯形面积"],
            "correct_answer": "39平方厘米",
        }
        with web_app.connect_db() as conn:
            card = web_app.save_card(conn, payload)
        result = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        exercises = result["pack"]["exercises"]
        self.assertEqual(len(exercises), 3)
        for exercise in exercises:
            diagram = exercise["diagram"]
            self.assertEqual(diagram["kind"], "overlapping_right_triangles")
            self.assertIn(f"AB={diagram['height']}", exercise["question"])
            self.assertIn(f"BF={diagram['shift']}", exercise["question"])
            self.assertIn(f"EG={diagram['drop']}", exercise["question"])

    def test_right_triangle_semicircle_practice_includes_matching_diagrams(self) -> None:
        payload = {
            **self.sample_payload("reinforcement"),
            "unit": "图形的面积问题",
            "topic": "半圆与直角三角形阴影面积",
            "question_text": "如图，在Rt△ABC中，以AC、BC为直径画半圆，求阴影面积。",
            "knowledge_points": ["半圆面积", "直角三角形面积", "阴影面积"],
            "correct_answer": "5π/2-4",
        }
        with web_app.connect_db() as conn:
            card = web_app.save_card(conn, payload)
        result = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        exercises = result["pack"]["exercises"]
        self.assertEqual(len(exercises), 3)
        for exercise in exercises:
            diagram = exercise["diagram"]
            self.assertEqual(diagram["kind"], "right_triangle_semicircles")
            self.assertIn(f"AC={diagram['leg_ac']}", exercise["question"])
            self.assertIn(f"BC={diagram['leg_bc']}", exercise["question"])

    def test_square_cross_path_practice_includes_matching_diagrams(self) -> None:
        payload = {
            **self.sample_payload("reinforcement"),
            "unit": "图形的面积问题",
            "topic": "正方形草坪与小路面积",
            "question_text": "如图，正方形草坪中修了横、竖各两条等宽小路，求未覆盖面积。",
            "knowledge_points": ["正方形面积", "小路面积", "平移拼接"],
            "correct_answer": "36平方米",
        }
        with web_app.connect_db() as conn:
            card = web_app.save_card(conn, payload)
        result = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        exercises = result["pack"]["exercises"]
        self.assertEqual(len(exercises), 3)
        for exercise in exercises:
            diagram = exercise["diagram"]
            self.assertEqual(diagram["kind"], "square_cross_paths")
            self.assertIn(f"边长为{diagram['side']}", exercise["question"])
            self.assertIn(f"{diagram['path_width']}米", exercise["question"])

    def test_parallelogram_split_practice_redraws_each_new_question(self) -> None:
        payload = {
            **self.sample_payload("mistake"),
            "unit": "平面图形面积",
            "topic": "等高三角形面积比",
            "question_text": "如图，平行四边形面积是20平方厘米，求甲、乙、丙三个三角形的面积比。",
            "knowledge_points": ["对角线平分面积", "等高三角形面积比"],
            "correct_answer": "5∶2∶3",
            "image_path": "/tmp/parallelogram.jpg",
        }
        with web_app.connect_db() as conn:
            card = web_app.save_card(conn, payload)
        result = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        exercises = result["pack"]["exercises"]
        self.assertEqual(len(exercises), 3)
        self.assertEqual(len({item["question"] for item in exercises}), 3)
        self.assertEqual(len({json.dumps(item["diagram"], sort_keys=True) for item in exercises}), 3)
        for exercise in exercises:
            diagram = exercise["diagram"]
            self.assertEqual(diagram["kind"], "parallelogram_split_triangles")
            self.assertIn(f"{diagram['total_area']}", exercise["question"])
            self.assertIn(f"{diagram['left_segment']}", exercise["question"])
            self.assertIn(f"{diagram['right_segment']}", exercise["question"])
        fresh_exercises = learning_enrichment.build_diagram_exercises(
            card, [item["question"] for item in exercises]
        )
        self.assertEqual(len(fresh_exercises), 3)
        self.assertTrue(
            {item["question"] for item in exercises}.isdisjoint(
                {item["question"] for item in fresh_exercises}
            )
        )
        for exercise in fresh_exercises:
            self.assertEqual(exercise["diagram"]["kind"], "parallelogram_split_triangles")

    def test_frontend_renders_parallelogram_split_diagram(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("renderParallelogramSplitDiagram(diagram)", html)
        self.assertIn("平行四边形分成甲乙丙三个三角形的面积比图", html)

    def test_todays_other_visual_math_cards_redraw_new_figures(self) -> None:
        cases = [
            (
                "多圆周长和",
                "如图，已知AB=50厘米，求图中各圆的周长之和。",
                ["圆的周长", "直径和"],
                "multi_circle_chain",
                "113.04厘米",
            ),
            (
                "重叠半圆阴影周长",
                "如图，将两个半圆拼放，求阴影部分的周长。",
                ["半圆周长", "阴影边界"],
                "nested_semicircle_perimeter",
                "27.98厘米",
            ),
            (
                "等面积条件求三角形面积",
                "如图，在直角梯形ABCD中，三角形AED、三角形FCD和四边形EBFD面积相等。",
                ["梯形面积", "面积相等"],
                "equal_area_right_trapezoid",
                "4平方厘米",
            ),
            (
                "圆柱展开图求容积",
                "如图，长方形和圆形铁皮恰好做一个无盖圆柱形水桶，求容积。",
                ["圆柱展开图", "圆柱体积"],
                "open_cylinder_net",
                "6.28立方分米",
            ),
            (
                "挖孔后的表面积变化",
                "如图，在长方体中挖一个圆柱孔，求挖孔后的表面积变化。",
                ["长方体表面积", "圆柱孔"],
                "cuboid_cylindrical_hole",
                "约12.8%",
            ),
        ]
        for topic, question_text, knowledge_points, expected_kind, expected_answer in cases:
            with self.subTest(topic=topic):
                payload = {
                    **self.sample_payload("reinforcement"),
                    "unit": "图形专项",
                    "topic": topic,
                    "question_text": question_text,
                    "knowledge_points": knowledge_points,
                    "correct_answer": "已核对",
                    "image_path": f"/tmp/{expected_kind}.jpg",
                }
                with web_app.connect_db() as conn:
                    card = web_app.save_card(conn, payload)
                result = learning_enrichment.enrich_card(
                    card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
                )
                exercises = result["pack"]["exercises"]
                self.assertEqual(len(exercises), 3)
                self.assertEqual(
                    len({json.dumps(item["diagram"], sort_keys=True) for item in exercises}),
                    3,
                )
                self.assertIn(expected_answer, exercises[0]["answer"])
                for exercise in exercises:
                    self.assertEqual(exercise["diagram"]["kind"], expected_kind)

    def test_rectangle_with_square_perimeter_practice_redraws_each_new_question(self) -> None:
        payload = {
            **self.sample_payload("mistake"),
            "unit": "长方形和正方形",
            "topic": "长方形与正方形组合图形",
            "question_text": "如图，阴影部分是一个正方形，长方形ABCD的周长是多少厘米？",
            "knowledge_points": ["正方形边长", "长方形周长"],
            "correct_answer": "28厘米",
            "image_path": "/tmp/rectangle-with-square.jpg",
        }
        with web_app.connect_db() as conn:
            card = web_app.save_card(conn, payload)
        result = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        exercises = result["pack"]["exercises"]
        self.assertEqual(len(exercises), 3)
        self.assertEqual(
            len({json.dumps(item["diagram"], sort_keys=True) for item in exercises}),
            3,
        )
        for exercise in exercises:
            self.assertEqual(exercise["diagram"]["kind"], "rectangle_with_square")
            self.assertIn("如图", exercise["question"])
            self.assertNotEqual(exercise["diagram"]["length"], 12)
        self.assertIn("38（厘米）", exercises[0]["answer"])

    def test_frontend_renders_all_today_visual_practice_diagrams(self) -> None:
        html = web_app.INDEX_HTML
        for renderer in (
            "renderMultiCircleChainDiagram(diagram)",
            "renderNestedSemicircleDiagram(diagram)",
            "renderEqualAreaTrapezoidDiagram(diagram)",
            "renderOpenCylinderNetDiagram(diagram)",
            "renderCuboidHoleDiagram(diagram, key)",
            "renderRectangleWithSquareDiagram(diagram)",
        ):
            self.assertIn(renderer, html)

    def test_new_worksheet_visual_cards_generate_new_matching_diagrams(self) -> None:
        cases = [
            (
                "组合图形面积",
                "边长为2的正方形中，画半径为2的四分之一圆和半径为1的半圆，求S₁-S₂。",
                ["面积作差与公共部分"],
                "square_quarter_semicircle",
            ),
            (
                "圆柱展开图",
                "长33.12厘米的长方形剪下两个圆和一个长方形做成圆柱，求表面积。",
                ["圆柱表面积"],
                "closed_cylinder_strip_net",
            ),
            (
                "图形实际面积",
                "比例尺为1∶10000，三角形顶点为A（4，6）、B（2，3）、C（4，3），求实际面积。",
                ["面积与比例尺平方"],
                "coordinate_triangle_scale",
            ),
            (
                "浸水问题",
                "圆柱形水桶中完全浸没一个圆锥，求水面升高多少厘米。",
                ["体积不变与水面升高"],
                "solid_immersion",
            ),
        ]
        for topic, question_text, knowledge_points, expected_kind in cases:
            with self.subTest(topic=topic):
                payload = {
                    **self.sample_payload("reinforcement"),
                    "unit": "图形专项",
                    "topic": topic,
                    "question_text": question_text,
                    "knowledge_points": knowledge_points,
                    "image_path": f"/tmp/{expected_kind}.jpg",
                }
                with web_app.connect_db() as conn:
                    card = web_app.save_card(conn, payload)
                result = learning_enrichment.enrich_card(
                    card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
                )
                exercises = result["pack"]["exercises"]
                self.assertEqual(len(exercises), 3)
                self.assertEqual(len({json.dumps(item["diagram"], sort_keys=True) for item in exercises}), 3)
                for exercise in exercises:
                    self.assertEqual(exercise["diagram"]["kind"], expected_kind)
                    self.assertIn("如图", exercise["question"])

    def test_frontend_renders_new_worksheet_practice_diagrams(self) -> None:
        html = web_app.INDEX_HTML
        for renderer in (
            "renderQuarterSemicircleDiagram(diagram, key)",
            "renderClosedCylinderStripDiagram(diagram)",
            "renderCoordinateTriangleDiagram(diagram)",
            "renderImmersionDiagram(diagram, key)",
        ):
            self.assertIn(renderer, html)

    def test_frontend_uses_the_visual_tag_to_show_the_source_figure(self) -> None:
        self.assertIn("(card.tags || []).includes('图形题')", web_app.INDEX_HTML)
        self.assertIn("变化的数字和提问以本题题干为准", web_app.INDEX_HTML)

    def test_equal_area_trapezoid_draws_target_triangle_bef(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn('data-part="triangle-bef"', html)
        self.assertIn('data-segment="EF"', html)
        self.assertIn('橙色部分是所求的△BEF', html)

    def test_unknown_visual_math_practice_always_reuses_the_source_image(self) -> None:
        payload = {
            **self.sample_payload("reinforcement"),
            "unit": "图形的面积问题",
            "topic": "组合图形面积",
            "question_text": "如图，求组合图形中阴影部分的面积。",
            "knowledge_points": ["组合图形面积"],
            "image_path": "/tmp/source-figure.jpg",
        }
        with web_app.connect_db() as conn:
            card = web_app.save_card(conn, payload)
        result = learning_enrichment.enrich_card(
            card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output()
        )
        for exercise in result["pack"]["exercises"]:
            self.assertEqual(
                exercise["diagram"],
                {"kind": "source_image", "card_id": card["id"]},
            )

    def test_visual_feishu_record_requires_an_image_region(self) -> None:
        card = {
            "subject": "数学",
            "record_type": "unsolved",
            "question_text": "观察图形，求第8个图形中的圆点数。",
            "knowledge_points": ["图形规律"],
            "correct_answer": "36个",
            "wrong_reason": "没有找到规律",
            "image_path": "/tmp/page.jpg",
            "source": {"visual_question": True, "crop_box": []},
        }
        self.assertFalse(feishu_mistake_sync.card_is_complete(card))
        card["source"]["crop_box"] = [5, 20, 90, 30]
        self.assertTrue(feishu_mistake_sync.card_is_complete(card))

    def test_english_spelling_is_classified_as_a_word_only_practice(self) -> None:
        card = {
            "subject": "英语",
            "topic": "Friday 拼写",
            "question_text": "Last F_____, I went there on foot.",
            "knowledge_points": ["Friday的拼写"],
            "grammar_errors": ["拼写"],
            "misspelled_words": [{"wrong": "Fridy", "correct": "Friday"}],
            "wrong_reason": "Friday 少写了字母。",
        }
        self.assertEqual(learning_enrichment.classify_english_card(card), "spelling")
        prompt = learning_enrichment.build_learning_prompt(card)
        self.assertIn("单词拼写", prompt)
        self.assertIn("不沿用原题上下文", prompt)

    def test_english_tense_is_classified_before_a_surface_spelling_error(self) -> None:
        card = {
            "subject": "英语",
            "topic": "go 的过去式",
            "knowledge_points": ["一般过去时", "不规则动词"],
            "grammar_errors": ["goed 应为 went"],
            "misspelled_words": [{"wrong": "goed", "correct": "went"}],
        }
        self.assertEqual(learning_enrichment.classify_english_card(card), "tense")

    def test_english_comprehensive_review_groups_categories_and_spelling_words(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn('id="practiceCategories"', html)
        self.assertIn("function buildEnglishReviewQuestions(cards)", html)
        self.assertIn("function buildSpellingPractice(cards", html)
        self.assertIn("易错词：", html)
        self.assertIn("归类整理：", html)

    def test_english_card_can_belong_to_multiple_learning_focuses(self) -> None:
        card = {
            "subject": "英语",
            "topic": "选词填空",
            "question_text": "根据词库完成介绍Green Farm的短文。",
            "user_answer": "meat；don't；take",
            "correct_answer": "met；doesn't；taking",
            "wrong_reason": "混淆过去式、第三人称单数和动词-ing形式。",
            "knowledge_points": ["过去式", "第三人称单数及动名词"],
            "grammar_errors": ["一般过去时"],
            "misspelled_words": [],
        }
        focuses = web_app.infer_english_focuses(card)
        self.assertIn("past_tense", focuses)
        self.assertIn("third_person", focuses)
        self.assertIn("verb_ing", focuses)

    def test_english_overview_groups_one_exam_and_builds_child_lessons(self) -> None:
        timestamp = "2026-08-10T01:00:00+00:00"
        with web_app.connect_db() as conn:
            first = web_app.save_card(
                conn,
                {
                    **self.sample_payload(),
                    "subject": "英语",
                    "topic": "过去式",
                    "question_text": "Yesterday, Lily ____ her teacher. (meet)",
                    "user_answer": "meet",
                    "correct_answer": "met",
                    "knowledge_points": ["一般过去时", "过去式"],
                    "created_at": timestamp,
                },
            )
            web_app.save_card(
                conn,
                {
                    **self.sample_payload(),
                    "subject": "英语",
                    "topic": "动词形式",
                    "question_text": "He doesn't ____ pictures. (take)",
                    "user_answer": "takes",
                    "correct_answer": "take",
                    "knowledge_points": ["does后接动词原形"],
                    "created_at": "2026-08-10T01:05:00+00:00",
                },
            )
            overview = web_app.build_english_overview(conn)
        self.assertEqual(overview["exam_count"], 1)
        self.assertEqual(overview["mistake_records"], 2)
        past = next(item for item in overview["focuses"] if item["key"] == "past_tense")
        self.assertEqual(past["evidence"][0]["card_id"], first["id"])
        self.assertTrue(past["rule"])
        self.assertTrue(past["self_check"])

    def test_correct_english_question_supports_exam_analysis_without_entering_review(self) -> None:
        with web_app.connect_db() as conn:
            card = web_app.save_card(
                conn,
                {
                    **self.sample_payload(),
                    "record_type": "correct",
                    "answer_status": "correct",
                    "subject": "英语",
                    "topic": "阅读细节",
                    "question_text": "Where did Amy go?",
                    "user_answer": "She went to the park.",
                    "correct_answer": "She went to the park.",
                    "knowledge_points": ["定位原文信息"],
                },
            )
            overview = web_app.build_english_overview(conn)
        self.assertEqual(card["learning"]["status"], "not_needed")
        self.assertEqual(overview["correct_records"], 1)

    def test_incorrect_attempt_is_automatically_marked_needs_work(self) -> None:
        card = self.save_sample()
        with web_app.connect_db() as conn:
            attempt = web_app.save_practice_attempt(
                conn,
                {
                    "card_id": card["id"],
                    "subject": "数学",
                    "topic": "圆柱侧面积",
                    "prompt_text": "半径3厘米、高5厘米的圆柱侧面积是多少？",
                    "answer_text": "15平方厘米",
                    "reference_answer": "30π平方厘米",
                },
            )
        self.assertEqual(attempt["grade_result"], "incorrect")
        self.assertEqual(attempt["auto_feedback"]["result"], "needs_work")

    def test_english_learning_page_teaches_before_showing_special_practice(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn('data-view="english"', html)
        self.assertIn('id="view-english-focus"', html)
        self.assertIn('id="englishPracticeSection" hidden', html)
        self.assertIn("function openEnglishFocus(key)", html)
        self.assertIn("function startEnglishFocusPractice()", html)
        self.assertIn("let stageCount = 0", html)
        self.assertIn("if (stageCount >= 2) break", html)
        self.assertIn("答错了，已经自动加入“还要加强”", html)

    def test_feishu_correct_exam_record_is_collected_separately(self) -> None:
        assistant = """<!--
记录类型：答对题
答题状态：答对
题号：第1页第2题
科目：英语
单元：阅读理解
题型：细节理解
知识点：定位原文信息
题目原文：Where did Amy go?
学生作答：She went to the park.
正确答案：She went to the park.
原图页码：1
题目区域：5%, 10%, 90%, 8%
【试卷记录：已确认】
-->"""
        blocks = feishu_mistake_sync.split_confirmed_records(assistant)
        self.assertEqual(len(blocks), 1)
        record = {
            "id": "correct-1",
            "message": {
                "role": "user",
                "sourceChannel": "feishu",
                "senderId": "ou_test",
                "content": "[message_id: om_correct] 发完了",
            },
        }
        card = feishu_mistake_sync.build_card(record, blocks[0], "assistant-1", Path("session.jsonl"))
        self.assertEqual(card["record_type"], "correct")
        self.assertEqual(card["answer_status"], "correct")
        self.assertEqual(card["question_number"], "第1页第2题")

    def test_pending_learning_packs_are_generated_as_a_batch(self) -> None:
        card_ids = [self.save_sample()["id"] for _ in range(3)]
        result = learning_enrichment.enrich_pending(
            db_path=self.db_path,
            limit=3,
            runner=lambda _: fake_openclaw_output(),
            max_workers=3,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["generated"], card_ids[::-1])

    def test_editing_learning_inputs_invalidates_old_pack(self) -> None:
        card = self.save_sample()
        learning_enrichment.enrich_card(card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output())
        edited = {**self.sample_payload(), "id": card["id"], "question_text": "一个圆柱直径6厘米，高5厘米，求侧面积。"}
        with web_app.connect_db() as conn:
            updated = web_app.save_card(conn, edited)
        self.assertEqual(updated["learning"]["status"], "pending")
        self.assertEqual(updated["learning"]["pack"], {})

    def test_incomplete_old_record_is_not_sent_to_the_model(self) -> None:
        card = self.save_sample()
        with web_app.connect_db() as conn:
            conn.execute(
                "UPDATE mistake_cards SET question_text = '', correct_answer = '', knowledge_points_json = ? WHERE id = ?",
                (json.dumps(["待整理"], ensure_ascii=False), card["id"]),
            )
            conn.commit()
            loaded = web_app.list_cards(conn, {})[0]
            pending = learning_enrichment.pending_card_ids(conn, 10)
        self.assertEqual(loaded["learning"]["status"], "incomplete")
        self.assertFalse(loaded["learning"]["eligible"])
        self.assertEqual(pending, [])

    def test_date_range_uses_shanghai_calendar_days_and_is_inclusive(self) -> None:
        timestamps = [
            "2026-06-30T15:59:00+00:00",
            "2026-06-30T16:00:00+00:00",
            "2026-07-01T16:00:00+00:00",
        ]
        with web_app.connect_db() as conn:
            for index, timestamp in enumerate(timestamps):
                payload = {**self.sample_payload(), "created_at": timestamp, "topic": f"日期测试{index}"}
                web_app.save_card(conn, payload)
            from_july = web_app.list_cards(conn, {"date_from": ["2026-07-01"]})
            through_july_first = web_app.list_cards(conn, {"date_to": ["2026-07-01"]})
            july_first_only = web_app.list_cards(
                conn,
                {"date_from": ["2026-07-01"], "date_to": ["2026-07-01"]},
            )
        self.assertEqual({card["topic"] for card in from_july}, {"日期测试1", "日期测试2"})
        self.assertEqual({card["topic"] for card in through_july_first}, {"日期测试0", "日期测试1"})
        self.assertEqual([card["topic"] for card in july_first_only], ["日期测试1"])

    def test_practice_feedback_does_not_reorder_the_visible_questions(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn('id="practiceActions${index}"', html)
        function_body = html.split("async function recordPractice(index, result, mode = 'practice') {", 1)[1].split(
            "async function markReviewed", 1
        )[0]
        self.assertIn("actionPanel.innerHTML = renderPracticeActions(item, index, mode)", function_body)
        self.assertNotIn("renderPractice();", function_body)

    def test_today_review_has_a_single_question_practice_flow(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn('id="view-single"', html)
        self.assertIn("openSinglePractice('${card.id}')", html)
        self.assertIn('id="singlePracticeCards"', html)
        self.assertIn('id="backToReview"', html)
        self.assertIn("questionsForCard(card)", html)
        self.assertIn('id="freshSinglePractice"', html)
        self.assertIn("generateSinglePractice(state.singlePracticeCardId, true, true)", html)
        self.assertIn("function getDueCards(cards)", html)

    def test_single_practice_can_add_questions_to_a_saved_exam_collection(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn('data-view="selected"', html)
        self.assertIn('id="view-selected"', html)
        self.assertIn("加入自选考题", html)
        self.assertIn("async function addSelectedQuestion(index)", html)
        self.assertIn("renderSelectedQuestions()", html)
        self.assertIn("题目 PDF", html)
        self.assertIn("答案 PDF", html)
        self.assertIn("发送题目到邮箱", html)
        self.assertIn("发送答案到邮箱", html)
        self.assertIn("邮件功能需在本机单独配置", html)
        self.assertIn("async function emailSelectedQuestions(type, button)", html)
        self.assertIn("['single', 'selected'].includes(view)", html)
        delete_handler = web_app.AppHandler.do_DELETE.__code__
        self.assertIn("/api/selected-questions/", delete_handler.co_consts)

    def test_selected_questions_are_persistent_unique_ordered_and_printable(self) -> None:
        card = self.save_sample()
        first_payload = {
            "source_card_id": card["id"],
            "subject": "数学",
            "topic": "圆柱侧面积练习",
            "prompt_text": "半径4厘米、高6厘米的圆柱侧面积是多少？",
            "answer_text": "1. 求周长\n2×π×4=8π\n\n2. 写出答案\n48π平方厘米",
            "hint_text": "先求底面周长。",
            "diagram": {"kind": "sample"},
            "diagram_svg": '<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="30"/></svg>',
            "diagram_caption": "圆柱示意图",
        }
        second_payload = {
            **first_payload,
            "prompt_text": "直径10厘米、高3厘米的圆柱侧面积是多少？",
            "topic": "第二道练习",
        }
        with web_app.connect_db() as conn:
            first, added = web_app.save_selected_question(conn, first_payload)
            duplicate, duplicate_added = web_app.save_selected_question(conn, first_payload)
            second, second_added = web_app.save_selected_question(conn, second_payload)
            self.assertTrue(added)
            self.assertFalse(duplicate_added)
            self.assertTrue(second_added)
            self.assertEqual(first["id"], duplicate["id"])
            self.assertEqual(len(web_app.list_selected_questions(conn)), 2)
            moved = web_app.move_selected_question(conn, second["id"], "up")
            self.assertEqual([item["id"] for item in moved], [second["id"], first["id"]])
            backup = web_app.build_backup(conn)
            question_html = web_app.build_selected_print_html(moved, "questions")
            answer_html = web_app.build_selected_print_html(moved, "answers")
        self.assertEqual(len(backup["selected_questions"]), 2)
        self.assertIn("直径10厘米", question_html)
        self.assertIn("<svg", question_html)
        self.assertNotIn("48π平方厘米", question_html)
        self.assertIn("48π平方厘米", answer_html)

    def test_selected_question_svg_rejects_active_content(self) -> None:
        self.assertEqual(
            web_app.sanitize_diagram_svg('<svg onload="alert(1)"><circle/></svg>'), ""
        )
        self.assertEqual(web_app.sanitize_diagram_svg("<script>alert(1)</script>"), "")

    def test_selected_pdf_generation_and_email_use_configured_recipient(self) -> None:
        fake_chrome = Path(self.tmp.name) / "chrome"
        fake_chrome.write_text("test", encoding="utf-8")
        original_chrome = web_app.CHROME_BINARY
        web_app.CHROME_BINARY = fake_chrome
        commands = []

        def fake_chrome_process(command, **_kwargs):
            commands.append(command)
            output_arg = next(item for item in command if item.startswith("--print-to-pdf="))
            Path(output_arg.split("=", 1)[1]).write_bytes(b"%PDF-" + b"x" * 200)
            return SimpleNamespace(
                pid=123,
                poll=lambda: 0,
                communicate=lambda timeout=None: ("", ""),
            )

        try:
            pdf = web_app.generate_selected_pdf(
                "http://127.0.0.1:18765",
                "questions",
                output_dir=Path(self.tmp.name) / "email",
                popen_factory=fake_chrome_process,
            )
        finally:
            web_app.CHROME_BINARY = original_chrome
        self.assertTrue(pdf.is_file())
        self.assertIn("/print-selected?type=questions", commands[0][-1])

        sent = []
        original_recipient = web_app.SELECTED_EMAIL_RECIPIENT
        web_app.SELECTED_EMAIL_RECIPIENT = "recipient@example.com"
        try:
            result = web_app.email_selected_questions(
                [{"prompt_text": "测试题"}],
                "questions",
                "http://127.0.0.1:18765",
                pdf_generator=lambda *_args: pdf,
                mail_sender=lambda path, recipient, subject: sent.append((path, recipient, subject)),
            )
        finally:
            web_app.SELECTED_EMAIL_RECIPIENT = original_recipient
        self.assertEqual(result["recipient"], "recipient@example.com")
        self.assertEqual(sent[0][1], "recipient@example.com")
        self.assertIn("题目", sent[0][2])

    def test_mail_sender_uses_environment_config_and_attaches_pdf(self) -> None:
        pdf = Path(self.tmp.name) / "自选考题-题目.pdf"
        pdf.write_bytes(b"%PDF-test")
        sent = []

        class FakeServer:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def login(self, user, password):
                self.login_values = (user, password)

            def send_message(self, message):
                sent.append((self.login_values, message))

        web_app.send_pdf_with_mailer(
            pdf,
            "recipient@example.com",
            "家庭错题本-自选考题-题目",
            config_loader=lambda: {
                "host": "smtp.example.com",
                "port": 465,
                "user": "sender@example.com",
                "password": "secret",
                "from": "sender@example.com",
            },
            connector=lambda *_args, **_kwargs: FakeServer(),
        )
        login_values, message = sent[0]
        self.assertEqual(login_values, ("sender@example.com", "secret"))
        self.assertEqual(message["To"], "recipient@example.com")
        attachment = next(message.iter_attachments())
        self.assertEqual(attachment.get_filename(), pdf.name)
        self.assertEqual(attachment.get_content_type(), "application/pdf")

    def test_mail_config_can_be_loaded_from_environment(self) -> None:
        config = web_app.load_mail_config(
            environ={
                "HOMEWORK_NOTEBOOK_SMTP_HOST": "smtp.example.com",
                "HOMEWORK_NOTEBOOK_SMTP_PORT": "465",
                "HOMEWORK_NOTEBOOK_SMTP_USER": "sender@example.com",
                "HOMEWORK_NOTEBOOK_SMTP_PASSWORD": "secret",
                "HOMEWORK_NOTEBOOK_MAIL_FROM": "sender@example.com",
            }
        )
        self.assertEqual(config["host"], "smtp.example.com")
        self.assertEqual(config["port"], 465)

    def test_notebook_cards_also_show_the_single_question_practice_button(self) -> None:
        html = web_app.INDEX_HTML
        render_cards = html.split("function renderCards(targetId, cards, dueOnly = false) {", 1)[1].split(
            "function renderLists", 1
        )[0]
        button = "onclick=\"openSinglePractice('${card.id}')\""
        self.assertIn(button, render_cards)
        self.assertNotIn("${dueOnly ? `<button", render_cards)
        self.assertIn("state.view === 'notebook' ? 'notebook' : 'review'", html)
        self.assertIn("'返回错题本'", html)

    def test_existing_practice_page_is_named_comprehensive_review(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn('<button data-view="practice">综合复习题</button>', html)
        self.assertIn("practice: '综合复习题'", html)

    def test_manual_entry_is_removed_from_navigation_but_editing_remains(self) -> None:
        html = web_app.INDEX_HTML
        self.assertNotIn('<button data-view="add">录入</button>', html)
        self.assertIn("onclick=\"editCard('${card.id}')\"", html)
        self.assertIn("add: '编辑错题'", html)

    def test_review_and_notebook_cards_render_required_geometry_figures(self) -> None:
        html = web_app.INDEX_HTML
        render_cards = html.split("function renderCards(targetId, cards, dueOnly = false) {", 1)[1].split(
            "function cardNeedsFigure", 1
        )[0]
        self.assertIn("renderCardFigure(card)", render_cards)
        self.assertIn("function cardNeedsFigure(card)", html)
        self.assertIn("原题图形待补", html)

    def test_solution_process_appears_directly_below_correct_answer(self) -> None:
        html = web_app.INDEX_HTML
        answer_index = html.index('<span class="answer-label">正确答案</span>')
        process_index = html.index("<h4>解题过程</h4>")
        reason_index = html.index("<strong>错因：</strong>")
        self.assertLess(answer_index, process_index)
        self.assertLess(process_index, reason_index)
        self.assertIn("card.feynman_explain || card.explanation_summary", html)
        self.assertIn("function renderSolutionProcess(value)", html)
        self.assertIn("function renderSolutionText(value)", html)
        self.assertIn("current.lines.push(line)", html)
        self.assertIn("(?:\\.\\s+|、\\s*)", html)
        self.assertIn("!/[：:]$/.test(line)", html)
        self.assertIn("solution-step${stepClass}", html)
        self.assertIn("key-step", html)
        self.assertIn("final-step", html)
        self.assertNotIn('<section class="solution-step"><h5>解题思路</h5>', html)
        self.assertIn('class="solution-formula"', html)
        self.assertIn('class="math-fraction"', html)
        self.assertIn("<details><summary>看答案</summary>${renderSolutionProcess(item.answer)}</details>", html)
        self.assertIn("${renderSolutionProcess(item.answer || '请对照原错题检查。')}", html)

    def test_subject_filter_keeps_all_subject_choices_after_filtering(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("state.stats.available_subjects", html)
        with web_app.connect_db() as conn:
            web_app.save_card(conn, self.sample_payload())
            web_app.save_card(conn, {**self.sample_payload(), "subject": "英语", "topic": "英语测试"})
            filtered_cards = web_app.list_cards(conn, {"subject": ["数学"]})
            all_cards = web_app.list_cards(conn, {})
        stats = web_app.build_stats(filtered_cards)
        stats["available_subjects"] = sorted({card["subject"] for card in all_cards})
        self.assertEqual([item["name"] for item in stats["subjects"]], ["数学"])
        self.assertEqual(stats["available_subjects"], ["数学", "英语"])

    def test_practice_grading_handles_common_answer_formats(self) -> None:
        cases = [
            ("在含糖率为25%的糖水中加入同浓度糖水，会不会改变？", "加入部分浓度相同，所以含糖率不变。", "不会改变", "correct"),
            ("在含糖率为25%的糖水中加入同浓度糖水，会不会改变？", "所以含糖率不变。", "会改变", "incorrect"),
            ("要同时加入多少克水？", "应加入的水为18-6=12克。", "12克", "correct"),
            ("要同时加入多少克水？", "应加入的水为18-6=12克。", "18克", "incorrect"),
            ("用正确形式填空：She likes _____. (skate)", "She likes skating. 关键步骤：skate 去 e 加 ing。", "skating", "correct"),
            ("用正确形式填空：She likes _____. (skate)", "She likes skating. 关键步骤：skate 去 e 加 ing。", "skateing", "incorrect"),
            ("选择正确答案。A. no B. not", "选 A。no 可以直接修饰名词。", "A", "correct"),
            ("解比例，x是多少？", "答案：x=5/4。", "x=1.25", "correct"),
        ]
        for prompt, expected, actual, result in cases:
            with self.subTest(actual=actual):
                self.assertEqual(web_app.grade_practice_answer(prompt, expected, actual)[0], result)

    def test_saved_practice_attempt_includes_a_grade(self) -> None:
        card = self.save_sample()
        learning_enrichment.enrich_card(card["id"], db_path=self.db_path, runner=lambda _: fake_openclaw_output())
        question = "半径2厘米、高5厘米的圆柱侧面积是多少？"
        with web_app.connect_db() as conn:
            attempt = web_app.save_practice_attempt(
                conn,
                {
                    "card_id": card["id"],
                    "subject": "数学",
                    "topic": "圆柱侧面积",
                    "prompt_text": question,
                    "answer_text": "20π平方厘米",
                },
            )
        self.assertEqual(attempt["grade_result"], "correct")
        self.assertTrue(attempt["grade_feedback"])

    def test_grouped_spelling_practice_uses_its_reference_answer(self) -> None:
        card = self.save_sample()
        with web_app.connect_db() as conn:
            attempt = web_app.save_practice_attempt(
                conn,
                {
                    "card_id": card["id"],
                    "subject": "英语",
                    "topic": "单词拼写 · which",
                    "prompt_text": "根据中文提示拼写单词：哪一个 ______",
                    "answer_text": "which",
                    "reference_answer": "which",
                },
            )
        self.assertEqual(attempt["grade_result"], "correct")

    def test_practice_result_layout_separates_verdict_answer_and_hint(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("答对了", html)
        self.assertIn("答错了", html)
        self.assertIn("正确答案和思路", html)
        self.assertIn("关键提醒", html)


class DynamicAuthenticationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.tmp.name)
        self.original_web_db = web_app.DB_PATH
        web_app.DB_PATH = self.temp_path / "mistakes.sqlite3"
        web_app.connect_db().close()
        self.auth_path = self.temp_path / "auth_state.json"
        self.server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.AppHandler)
        self.server.access_code = "test-access-code"
        self.server.session_secret = "session-secret-for-tests"
        self.server.auth_state_path = self.auth_path
        self.server.login_attempts = {}
        self.server.login_attempts_lock = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        web_app.DB_PATH = self.original_web_db
        self.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, str] | None = None,
        cookie: str = "",
    ) -> tuple[int, list[tuple[str, str]], str]:
        body = urlencode(form or {}) if form is not None else None
        headers = {}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read().decode("utf-8")
        result = (response.status, response.getheaders(), payload)
        conn.close()
        return result

    @staticmethod
    def cookie_from_headers(headers: list[tuple[str, str]], name: str) -> str:
        prefix = f"{name}="
        for header_name, value in headers:
            if header_name.lower() == "set-cookie" and value.startswith(prefix):
                return value.split(";", 1)[0]
        return ""

    def test_totp_matches_known_vector(self) -> None:
        secret = base64.b32encode(b"12345678901234567890").decode("ascii")
        self.assertEqual(web_app.totp_code(secret, 59), "287082")
        self.assertTrue(web_app.verify_totp(secret, "287082", 59, window=0))
        self.assertFalse(web_app.verify_totp(secret, "287083", 59, window=0))

    def test_session_token_rejects_tampering_and_expiry(self) -> None:
        token = web_app.make_session_token("secret", "authenticated", 60, now=1000)
        self.assertTrue(web_app.verify_session_token(token, "secret", "authenticated", now=1030))
        self.assertFalse(web_app.verify_session_token(token, "secret", "authenticated", now=1061))
        self.assertFalse(web_app.verify_session_token(token + "x", "secret", "authenticated", now=1030))
        self.assertFalse(web_app.verify_session_token(token, "secret", "preauth", now=1030))

    def test_recovery_code_can_only_be_used_once(self) -> None:
        state = web_app.load_auth_state(self.auth_path)
        fixed_time = 1_800_000_000
        codes = web_app.confirm_totp_setup(
            self.auth_path,
            web_app.totp_code(state["totp_secret"], fixed_time),
            fixed_time,
        )
        self.assertEqual(len(codes or []), 8)
        self.assertTrue(web_app.consume_recovery_code(self.auth_path, codes[0]))
        self.assertFalse(web_app.consume_recovery_code(self.auth_path, codes[0]))

    def test_first_login_setup_and_authenticated_access(self) -> None:
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})

        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers).get("Location"), "/login")

        status, _, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 401)
        self.assertIn("需要重新登录", body)

        status, headers, _ = self.request(
            "POST",
            "/auth/login",
            form={"username": "student", "password": "test-access-code"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers).get("Location"), "/auth/setup")
        preauth_cookie = self.cookie_from_headers(headers, web_app.AUTH_PREAUTH_COOKIE)
        self.assertTrue(preauth_cookie)

        status, _, body = self.request("GET", "/auth/setup", cookie=preauth_cookie)
        self.assertEqual(status, 200)
        self.assertIn("绑定手机动态验证码", body)
        self.assertIn("手动输入", body)

        state = web_app.load_auth_state(self.auth_path)
        status, headers, body = self.request(
            "POST",
            "/auth/setup",
            form={"otp": web_app.totp_code(state["totp_secret"]), "remember": "1"},
            cookie=preauth_cookie,
        )
        self.assertEqual(status, 200)
        self.assertIn("绑定成功", body)
        self.assertIn("备用恢复码", body)
        session_cookie = self.cookie_from_headers(headers, web_app.AUTH_SESSION_COOKIE)
        self.assertTrue(session_cookie)

        status, _, body = self.request("GET", "/api/stats", cookie=session_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, headers, _ = self.request("POST", "/auth/logout", cookie=session_cookie)
        self.assertEqual(status, 303)
        cleared = [value for name, value in headers if name.lower() == "set-cookie"]
        self.assertTrue(any("homework_session=;" in value and "Max-Age=0" in value for value in cleared))

    def test_confirmed_account_requires_password_and_current_totp(self) -> None:
        state = web_app.load_auth_state(self.auth_path)
        code = web_app.totp_code(state["totp_secret"])
        self.assertIsNotNone(web_app.confirm_totp_setup(self.auth_path, code))

        status, _, _ = self.request(
            "POST",
            "/auth/login",
            form={"username": "student", "password": "test-access-code", "otp": "000000"},
        )
        self.assertEqual(status, 401)

        current_state = web_app.load_auth_state(self.auth_path)
        status, headers, _ = self.request(
            "POST",
            "/auth/login",
            form={
                "username": "student",
                "password": "test-access-code",
                "otp": web_app.totp_code(current_state["totp_secret"]),
            },
        )
        self.assertEqual(status, 303)
        self.assertTrue(self.cookie_from_headers(headers, web_app.AUTH_SESSION_COOKIE))

    def test_repeated_failures_temporarily_lock_login(self) -> None:
        for _ in range(web_app.LOGIN_ATTEMPT_LIMIT):
            self.request(
                "POST",
                "/auth/login",
                form={"username": "student", "password": "wrong"},
            )
        status, _, body = self.request("GET", "/login")
        self.assertEqual(status, 429)
        self.assertIn("尝试次数过多", body)

    def test_first_login_accepts_formatted_hex_access_code(self) -> None:
        self.server.access_code = "f69f76c646436a6888f23c0caf0c9d4b"
        status, headers, _ = self.request(
            "POST",
            "/auth/login",
            form={
                "username": " STUDENT ",
                "password": " F69F 76C6 4643 6A68 88F2 3C0C AF0C 9D4B ",
            },
        )
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers).get("Location"), "/auth/setup")

    def test_first_login_error_does_not_claim_totp_is_wrong(self) -> None:
        status, _, body = self.request(
            "POST",
            "/auth/login",
            form={"username": "student", "password": "wrong"},
        )
        self.assertEqual(status, 401)
        self.assertIn("账号或访问码不正确", body)
        self.assertNotIn("动态验证码不正确", body)


if __name__ == "__main__":
    unittest.main()
