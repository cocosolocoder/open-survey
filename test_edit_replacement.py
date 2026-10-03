#!/usr/bin/env python3
"""回归测试：问卷草稿整份替换（PUT /api/surveys/{id}）的公开行为。

通过真实启动的 HTTP 服务观察结果：既检查保存响应的状态与内容，
也检查随后 GET 详情 / 列表读到的问卷，避免只凭一次成功响应下结论。

运行：python3 test_edit_replacement.py
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent / "app.py"


def start_server():
    """在临时数据目录上启动服务，返回 (进程, 基础 URL)。"""
    data_dir = tempfile.TemporaryDirectory()
    proc = subprocess.Popen(
        [sys.executable, str(APP), "serve", "--host", "127.0.0.1",
         "--port", "0", "--data-dir", data_dir.name],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    line = proc.stdout.readline()
    match = re.search(r"http://[\d.]+:(\d+)", line)
    if not match:
        proc.kill()
        raise RuntimeError(f"服务未能启动：{line!r}")
    proc._data_dir = data_dir  # 让临时目录与进程同生命周期
    return proc, f"http://127.0.0.1:{match.group(1)}"


class EditReplacementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    # ---------- HTTP 辅助 ----------

    def request(self, method, path, payload=None):
        body = json.dumps(payload).encode("utf8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=body, method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf8"))

    def create(self, payload):
        status, data = self.request("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"创建问卷失败：{data}")
        return data

    def put(self, survey_id, payload):
        return self.request("PUT", f"/api/surveys/{survey_id}", payload)

    def detail(self, survey_id):
        status, data = self.request("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, f"读取详情失败：{data}")
        return data

    def list_ids(self):
        status, data = self.request("GET", "/api/surveys")
        self.assertEqual(status, 200)
        return [item["id"] for item in data["surveys"]]

    # ---------- 成功保存：整份替换而非局部追加 ----------

    def test_full_replacement_replaces_instead_of_appending(self):
        original = self.create({
            "title": "原始问卷",
            "description": "原始说明",
            "questions": [
                {"type": "text", "title": "将被删除的文本题", "required": True},
                {"type": "single_choice", "title": "保留下来的单选题",
                 "required": True, "options": ["旧选项甲", "旧选项乙"]},
                {"type": "text", "title": "也将被删除"},
            ],
        })
        survey_id = original["id"]

        # 删除两道旧题；保留的单选题调到后面并改题意，选项内容与次序全部更新
        replacement = {
            "title": "替换后的问卷",
            "description": "替换后的说明",
            "questions": [
                {"type": "text", "title": "全新的文本题", "required": False},
                {"type": "single_choice", "title": "保留下来的单选题",
                 "required": True, "options": ["新选项一", "新选项二", "新选项三"]},
            ],
        }
        status, saved = self.put(survey_id, replacement)

        self.assertEqual(status, 200, f"保存应成功：{saved}")
        self.assertEqual(saved["id"], survey_id, "保存后问卷编号必须保持不变")

        # 服务端会把文本题的 options 规范化为空数组
        expected = {
            "id": survey_id,
            "title": replacement["title"],
            "description": replacement["description"],
            "questions": [
                {"type": "text", "title": "全新的文本题",
                 "required": False, "options": []},
                {"type": "single_choice", "title": "保留下来的单选题",
                 "required": True, "options": ["新选项一", "新选项二", "新选项三"]},
            ],
        }
        self.assertEqual(saved, expected, "返回的完整问卷必须等于本次提交的内容")
        self.assertEqual(self.detail(survey_id), expected,
                         "随后读取的详情必须与保存结果一致")

        # 旧内容不能混入：题目数量、旧题目标题、旧选项都不应再出现
        detail = self.detail(survey_id)
        self.assertEqual(len(detail["questions"]), 2)
        titles = [q["title"] for q in detail["questions"]]
        self.assertNotIn("将被删除的文本题", titles)
        self.assertNotIn("也将被删除", titles)
        all_options = [opt for q in detail["questions"] for opt in q["options"]]
        self.assertNotIn("旧选项甲", all_options)
        self.assertNotIn("旧选项乙", all_options)

    def test_duplicate_titles_kept_separate_in_submission_order(self):
        survey_id = self.create({
            "title": "重名题问卷",
            "questions": [{"type": "text", "title": "占位题"}],
        })["id"]

        questions = [
            {"type": "text", "title": "同名题", "required": True},
            {"type": "single_choice", "title": "同名题", "required": False,
             "options": ["甲", "乙"]},
            {"type": "text", "title": "同名题", "required": False},
        ]
        status, saved = self.put(survey_id, {"title": "重名题问卷", "questions": questions})

        self.assertEqual(status, 200, f"同名题应允许保存：{saved}")
        expected = [
            {"type": "text", "title": "同名题", "required": True, "options": []},
            {"type": "single_choice", "title": "同名题", "required": False,
             "options": ["甲", "乙"]},
            {"type": "text", "title": "同名题", "required": False, "options": []},
        ]
        # 三道同名题各自保留自己的题型、必填设置与选项，不被合并
        self.assertEqual(saved["questions"], expected)
        self.assertEqual(self.detail(survey_id)["questions"], expected)

    def test_trimming_and_description_preservation(self):
        survey_id = self.create({
            "title": "空白处理问卷",
            "questions": [{"type": "text", "title": "占位题"}],
        })["id"]

        description = "第一行：中文说明，含“引号”与 English \"quotes\"\n第二行：换行后原样保留\n\n末尾空行上方"
        status, saved = self.put(survey_id, {
            "title": "  前后带空白的标题  ",
            "description": description,
            "questions": [
                {"type": "text", "title": "\t 带空白的文本题 \n"},
                {"type": "single_choice", "title": "  带空白的单选题 ",
                 "options": ["  选项甲  ", "\t选项乙\n"]},
            ],
        })

        self.assertEqual(status, 200, f"保存应成功：{saved}")
        # 标题、题目标题、选项按去掉首尾空白后的文字保存
        self.assertEqual(saved["title"], "前后带空白的标题")
        self.assertEqual(saved["questions"][0]["title"], "带空白的文本题")
        self.assertEqual(saved["questions"][1]["title"], "带空白的单选题")
        self.assertEqual(saved["questions"][1]["options"], ["选项甲", "选项乙"])
        # 说明中的中文、引号和换行原样保留（不去空白）
        self.assertEqual(saved["description"], description)

        detail = self.detail(survey_id)
        self.assertEqual(detail["title"], "前后带空白的标题")
        self.assertEqual(detail["description"], description)
        self.assertEqual(detail["questions"][1]["options"], ["选项甲", "选项乙"])

    def test_omitted_fields_reset_and_other_survey_untouched(self):
        survey_id = self.create({
            "title": "字段省略问卷",
            "description": "原有说明，应被清空",
            "questions": [
                {"type": "text", "title": "原本必填的题", "required": True},
            ],
        })["id"]
        other = self.create({
            "title": "另一份问卷",
            "description": "不应被波及",
            "questions": [
                {"type": "single_choice", "title": "他卷题目", "required": True,
                 "options": ["是", "否"]},
            ],
        })

        # 省略 description、省略某题的 required、文本题省略 options
        status, saved = self.put(survey_id, {
            "title": "字段省略问卷",
            "questions": [{"type": "text", "title": "原本必填的题"}],
        })

        self.assertEqual(status, 200, f"保存应成功：{saved}")
        expected = {
            "id": survey_id,
            "title": "字段省略问卷",
            "description": "",  # 省略说明 → 空字符串，不沿用旧值
            "questions": [
                {"type": "text", "title": "原本必填的题",
                 "required": False, "options": []},  # 省略 required → 非必填
            ],
        }
        self.assertEqual(saved, expected)
        self.assertEqual(self.detail(survey_id), expected,
                         "返回的完整问卷与之后读取的详情应一致")

        # 同一次保存不能改变另一份问卷
        self.assertEqual(self.detail(other["id"]), other)

    # ---------- 校验失败：原草稿完整保留 ----------

    def test_invalid_edit_returns_400_and_preserves_original(self):
        original = self.create({
            "title": "将被保护的问卷",
            "description": "原说明\n保持原样",
            "questions": [
                {"type": "text", "title": "第一题", "required": True},
                {"type": "single_choice", "title": "第二题", "required": False,
                 "options": ["旧甲", "旧乙", "旧丙"]},
            ],
        })
        survey_id = original["id"]

        # 前面的题目都是合法修改，最后的单选题含两个去空白后相同的选项
        status, error = self.put(survey_id, {
            "title": "不应生效的新标题",
            "description": "不应生效的新说明",
            "questions": [
                {"type": "text", "title": "合法的新题"},
                {"type": "single_choice", "title": "问题题",
                 "options": ["重复项", "  重复项  ", "另一个"]},
            ],
        })

        self.assertEqual(status, 400, f"非法提交必须返回 400：{error}")
        message = error.get("error", "")
        self.assertIn("第 2 题", message, "错误应指出具体题目")
        self.assertIn("重复项", message, "错误应指出具体选项问题")

        # 再次读取：原标题、说明、题目顺序、必填设置和全部旧选项保持原样，
        # 不能留下前面合法修改的部分结果
        self.assertEqual(self.detail(survey_id), original)

    def test_put_missing_survey_returns_404_and_creates_nothing(self):
        missing_id = max(self.list_ids(), default=0) + 1000
        ids_before = self.list_ids()

        status, error = self.put(missing_id, {
            "title": "完全合法的草稿",
            "description": "内容合法但目标不存在",
            "questions": [
                {"type": "single_choice", "title": "评分", "required": True,
                 "options": ["好", "一般", "差"]},
            ],
        })

        self.assertEqual(status, 404, f"不存在的编号必须返回 404：{error}")
        # 不能借编辑创建记录：详情仍 404，列表不增加条目
        get_status, _ = self.request("GET", f"/api/surveys/{missing_id}")
        self.assertEqual(get_status, 404)
        self.assertEqual(self.list_ids(), ids_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
