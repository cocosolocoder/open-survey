#!/usr/bin/env python3
"""整份替换问卷草稿（PUT /api/surveys/{id}）的自动化回归保障。

只通过对外可见的 HTTP 行为观察结果：
- POST /api/surveys        准备草稿
- PUT  /api/surveys/{id}   整份替换
- GET  /api/surveys/{id}   读取详情
- GET  /api/surveys        问卷列表

保存成功后不仅校验当次响应的状态与内容，还再次读取详情核对；
提交不合法时同样再次读取，确认旧草稿完整保留、没有部分写入。

仅依赖标准库，运行方式：

    python3 -m unittest test_survey_replace -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent / "app.py"


class SurveyServer:
    """在临时数据目录上启动一个真实的 app.py 服务进程。"""

    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-test-")
        self.process = None
        self.base_url = None

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, str(APP), "serve",
             "--host", "127.0.0.1", "--port", "0",
             "--data-dir", self.data_dir.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # 端口 0 时实际端口在启动行中：
        # "OpenSurvey listening on http://127.0.0.1:12345"
        line = self.process.stdout.readline()
        while "listening on" not in line:
            if self.process.poll() is not None:
                output = self.process.stdout.read()
                raise RuntimeError(f"服务启动失败：\n{output}")
            line = self.process.stdout.readline()
        port = int(line.rsplit(":", 1)[1].strip())
        self.base_url = f"http://127.0.0.1:{port}"
        self._wait_until_ready()

    def _wait_until_ready(self):
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", int(self.base_url.rsplit(":", 1)[1])), timeout=0.2):
                    return
            except OSError:
                pass
        raise RuntimeError("服务端口未就绪")

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.data_dir.cleanup()

    def request(self, method, path, body=None):
        """发起一次 JSON 请求，返回 (status, payload_dict_or_raw)。"""
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            status = error.code
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw


class ReplaceSurveyTests(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = SurveyServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def api(self, method, path, body=None):
        return self.server.request(method, path, body)

    def create_survey(self, payload):
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data

    def get_detail(self, survey_id):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, f"读取详情失败：{data}")
        return data

    def expected_survey(self, survey_id, payload):
        """按产品规则从提交体构造期望的完整问卷对象。"""
        return {
            "id": survey_id,
            "title": payload["title"].strip(),
            "description": payload.get("description", ""),
            "questions": [
                {
                    "type": q["type"],
                    "title": q["title"].strip(),
                    "required": q.get("required", False),
                    "options": [opt.strip() for opt in q.get("options", [])],
                }
                for q in payload["questions"]
            ],
        }

    def test_full_replacement_replaces_everything_in_submitted_order(self):
        """删题、调序、改选项后：旧内容全部消失，本次提交完整生效。"""
        created = self.create_survey({
            "title": "  原标题  ",
            "description": "原始说明\n第二行",
            "questions": [
                {"type": "text", "title": "文本题A", "required": True},
                {"type": "single_choice", "title": "单选题B",
                 "options": ["选项一", "选项二", "选项三"]},
                {"type": "text", "title": "文本题C", "required": True},
            ],
        })
        survey_id = created["id"]

        # 删除“文本题A”；保留题调换顺序（C 提到 B 前）；
        # 单选题选项改内容、改次序并增删；再追加三道同名题。
        replacement = {
            "title": "  新标题  ",
            # 说明中的首尾空白、中文、引号、制表符与换行必须原样保留，
            # 与标题/题目标题/选项的 trim 规则明确不同。
            "description": "  说明保留原样\n第二行\t\"引号\"与中文  ",
            "questions": [
                {"type": "text", "title": "  文本题C  "},
                {"type": "single_choice", "title": "单选题B", "required": True,
                 "options": [" 选项三 ", " 改后的选项二 ", " 全新选项 "]},
                # 同名题不合并：各自按提交顺序保留题型、必填与选项。
                {"type": "text", "title": "  重名题  ", "required": True},
                {"type": "single_choice", "title": "重名题 ",
                 "options": [" 甲 ", " 乙 "]},
                {"type": "text", "title": "重名题", "required": False},
            ],
        }
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", replacement)

        self.assertEqual(status, 200)
        expected = self.expected_survey(survey_id, replacement)
        # 编号保持不变，响应体即本次提交的完整结果。
        self.assertEqual(data, expected)

        # 不能只凭一次 200 响应认定保存正确：随后读取详情必须一致。
        self.assertEqual(self.get_detail(survey_id), expected)

        titles = [q["title"] for q in data["questions"]]
        self.assertEqual(titles, ["文本题C", "单选题B", "重名题", "重名题", "重名题"])

        # 旧题被删除而非追加保留。
        self.assertNotIn("文本题A", titles)
        # 旧选项不混入新内容：选项一被删除、选项二被改写、顺序也以新提交为准。
        choice_options = data["questions"][1]["options"]
        self.assertEqual(choice_options, ["选项三", "改后的选项二", "全新选项"])
        self.assertNotIn("选项一", choice_options)
        self.assertNotIn("选项二", choice_options)

        # 三道同名题各自保留自己的类型、必填设置与选项。
        dupes = [q for q in data["questions"] if q["title"] == "重名题"]
        self.assertEqual(
            [(q["type"], q["required"], q["options"]) for q in dupes],
            [("text", True, []),
             ("single_choice", False, ["甲", "乙"]),
             ("text", False, [])],
        )

        # trim 与原样保留两种处理在读取结果中可以明确区分。
        self.assertEqual(data["title"], "新标题")
        self.assertEqual(data["description"], replacement["description"])
        self.assertTrue(data["description"].startswith("  "))
        self.assertTrue(data["description"].endswith("  "))
        self.assertIn("\n", data["description"])
        self.assertIn("\t", data["description"])

    def test_omitted_fields_reset_to_defaults_and_text_question_has_no_options(self):
        """省略 description / required 按空字符串与 false 落库，不沿用旧值。"""
        created = self.create_survey({
            "title": "省略字段用问卷",
            "description": "旧说明，不应被沿用",
            "questions": [
                {"type": "text", "title": "旧的必填文本题", "required": True},
                {"type": "single_choice", "title": "旧的必填单选题", "required": True,
                 "options": ["旧甲", "旧乙"]},
            ],
        })
        survey_id = created["id"]

        # 另一份问卷用于确认编辑不会波及其它记录。
        other = self.create_survey({
            "title": "另一份问卷",
            "description": "旁人说明",
            "questions": [{"type": "single_choice", "title": "旁人题目",
                           "required": True, "options": ["X", "Y"]}],
        })
        other_before = self.get_detail(other["id"])

        replacement = {
            "title": "省略字段用问卷",
            # 故意省略 description。
            "questions": [
                # 文本题不带选项；required 省略。
                {"type": "text", "title": "新文本题"},
                # 单选题 required 同样省略。
                {"type": "single_choice", "title": "新单选题",
                 "options": ["新甲", "新乙"]},
            ],
        }
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", replacement)
        self.assertEqual(status, 200)

        expected = self.expected_survey(survey_id, replacement)
        self.assertEqual(data, expected)
        self.assertEqual(data["description"], "")
        self.assertEqual([q["required"] for q in data["questions"]], [False, False])
        self.assertEqual(data["questions"][0]["options"], [])

        # 响应体与随后读取到的详情一致。
        self.assertEqual(self.get_detail(survey_id), expected)
        # 同一次保存不改变另一份问卷。
        self.assertEqual(self.get_detail(other["id"]), other_before)

    def test_invalid_later_question_keeps_entire_old_draft_untouched(self):
        """后面的单选题选项重复：400 且定位题目/选项，旧草稿原样保留。"""
        created = self.create_survey({
            "title": "不可变标题",
            "description": "原说明\n带换行与\"引号\"",
            "questions": [
                {"type": "text", "title": "必填文本题", "required": True},
                {"type": "single_choice", "title": "旧单选题", "required": False,
                 "options": ["旧选项甲", "旧选项乙", "旧选项丙"]},
            ],
        })
        survey_id = created["id"]
        before = self.get_detail(survey_id)

        bad_payload = {
            "title": "试图改成的标题",
            "description": "试图改成的说明",
            "questions": [
                # 前面的题目本身都是合法修改。
                {"type": "text", "title": "前面合法的新题", "required": True},
                # 后面的单选题：去掉首尾空白后第 1、3 个选项相同。
                {"type": "single_choice", "title": "后面坏题",
                 "options": ["坏选项", "另一个", "  坏选项  "]},
            ],
        }
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", bad_payload)

        self.assertEqual(status, 400)
        self.assertIn("error", data)
        message = data["error"]
        # 错误信息必须定位到具体题目（第 2 题）和具体选项。
        self.assertIn("第 2 题", message)
        self.assertIn("坏选项", message)

        # 旧草稿完整保留：标题、说明、题目顺序、必填设置、全部旧选项。
        after = self.get_detail(survey_id)
        self.assertEqual(after, before)
        self.assertEqual(after["title"], "不可变标题")
        self.assertEqual(after["description"], "原说明\n带换行与\"引号\"")
        self.assertEqual([q["title"] for q in after["questions"]],
                         ["必填文本题", "旧单选题"])
        self.assertEqual([q["required"] for q in after["questions"]], [True, False])
        self.assertEqual(after["questions"][1]["options"],
                         ["旧选项甲", "旧选项乙", "旧选项丙"])
        # 前面“合法”的修改也不能留下部分结果。
        all_titles = [q["title"] for q in after["questions"]]
        self.assertNotIn("前面合法的新题", all_titles)
        self.assertNotIn("后面坏题", all_titles)

        # 列表里也仍是旧标题。
        status, listing = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        titles = {item["id"]: item["title"] for item in listing["surveys"]}
        self.assertEqual(titles[survey_id], "不可变标题")

    def test_put_unknown_id_returns_404_and_never_creates_record(self):
        """对不存在的编号提交有效草稿：404，不借编辑创建，列表不增加。"""
        status, listing_before = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        ids_before = {item["id"] for item in listing_before["surveys"]}

        unknown_id = max(ids_before, default=0) + 999_999
        payload = {
            "title": "不应被创建的问卷",
            "questions": [{"type": "text", "title": "题目"}],
        }
        status, data = self.api("PUT", f"/api/surveys/{unknown_id}", payload)
        self.assertEqual(status, 404)

        # 仍然读不到，说明没有被编辑请求顺手创建。
        status, _ = self.api("GET", f"/api/surveys/{unknown_id}")
        self.assertEqual(status, 404)

        status, listing_after = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        self.assertEqual(listing_after, listing_before)
        ids_after = {item["id"] for item in listing_after["surveys"]}
        self.assertEqual(ids_after, ids_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
