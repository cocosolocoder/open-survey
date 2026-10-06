#!/usr/bin/env python3
"""问卷草稿保存对不完整 Unicode 字符（未配对代理码点）的处理：接口回归保障。

请求体可以是语法合法的 JSON，却在标题、说明、题目标题或选项中含有未配对
的 Unicode 代理转义（如单独的 \uD800 或 \uDC00 转义）。这类内容必须作为具体字段
的内容错误被拒绝：返回 400 与可正常解析的 JSON 错误信息，定位到具体字段/
题目/选项；不能断开连接、不能返回保存成功、也不能用替换字符悄悄改写内容。
合法内容（中文、引号、内部换行、表情符号、成对的代理转义、以及字面的
“\uD800”文本）仍照常保存。

只通过对外可见的 HTTP 行为观察结果，仅依赖标准库，运行方式：

    python3 -m unittest test_survey_unpaired_surrogate -v
"""
import json
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent / "app.py"

# 未配对的代理码点（高半 / 低半），用于构造合法 JSON 中的 \uD800 / \uDC00。
LONE_HIGH = "\ud800"
LONE_LOW = "\udc00"
# 一个表情的正确代理对转义（JSON 文本写法）与直接字符。
EMOJI = "\U0001F600"
EMOJI_ESCAPED = "\\uD83D\\uDE00"


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
                with socket.create_connection(
                        ("127.0.0.1", int(self.base_url.rsplit(":", 1)[1])),
                        timeout=0.2):
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

    def request(self, method, path, body=None, raw_body=None):
        """发起一次 JSON 请求，返回 (status, payload_dict_or_raw)。

        body 走 json.dumps（默认 ensure_ascii，未配对代理会以 \uD800
        转义形式出现在合法 JSON 文本中）；raw_body 用于逐字节控制请求体。
        """
        data = None
        headers = {}
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
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


class UnpairedSurrogateTests(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = SurveyServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def api(self, method, path, body=None, raw_body=None):
        return self.server.request(method, path, body, raw_body)

    def listing(self):
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, data)
        return data["surveys"]

    def get_detail(self, survey_id):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, f"读取详情失败：{data}")
        return data

    def create_survey(self, payload):
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data

    def valid_payload(self):
        return {
            "title": "正常问卷",
            "description": "正常说明",
            "questions": [
                {"type": "text", "title": "第一题", "required": True},
                {"type": "single_choice", "title": "第二题",
                 "options": ["甲", "乙", "丙"]},
            ],
        }

    def assert_rejected_with_location(self, status, data, *needles):
        """400、可解析的 JSON 错误信息，且定位到提交者能找到的原字段。"""
        self.assertEqual(status, 400, f"应返回 400 而非断开或成功：{data!r}")
        self.assertIsInstance(data, dict, f"错误响应必须是可解析的 JSON：{data!r}")
        self.assertIn("error", data)
        for needle in needles:
            self.assertIn(needle, data["error"])

    # ---------- 创建（POST）----------

    def test_create_with_lone_surrogate_in_title_rejected_and_nothing_saved(self):
        before = self.listing()
        payload = self.valid_payload()
        payload["title"] = f"标题{LONE_HIGH}坏字符"
        status, data = self.api("POST", "/api/surveys", payload)
        self.assert_rejected_with_location(status, data, "title")
        self.assertEqual(self.listing(), before, "新建失败时列表不能出现新问卷")

    def test_create_with_lone_surrogate_in_description_rejected(self):
        before = self.listing()
        payload = self.valid_payload()
        payload["description"] = f"说明里的{LONE_LOW}坏字符"
        status, data = self.api("POST", "/api/surveys", payload)
        # 标题与说明必须明确区分：错误指向 description 而不是 title。
        self.assert_rejected_with_location(status, data, "description")
        self.assertNotIn("title 包含", data["error"])
        self.assertEqual(self.listing(), before)

    def test_create_with_lone_surrogate_in_later_question_title_rejected(self):
        before = self.listing()
        payload = self.valid_payload()
        # 坏字符在第二题：第一题完全合法也不能被先保存。
        payload["questions"][1]["title"] = f"第二题{LONE_HIGH}"
        status, data = self.api("POST", "/api/surveys", payload)
        self.assert_rejected_with_location(status, data, "第 2 题")
        self.assertEqual(self.listing(), before)

    def test_create_with_lone_surrogate_in_option_rejected_with_option_number(self):
        before = self.listing()
        payload = self.valid_payload()
        # 坏字符在第 2 题的第 3 个选项。
        payload["questions"][1]["options"][2] = f"丙{LONE_LOW}"
        status, data = self.api("POST", "/api/surveys", payload)
        self.assert_rejected_with_location(status, data, "第 2 题", "第 3 个选项")
        self.assertEqual(self.listing(), before)

    def test_create_with_lone_surrogate_in_unsupported_type_name_rejected(self):
        before = self.listing()
        payload = self.valid_payload()
        # 题型名称本身不受支持且含坏字符：仍要返回可读的 400，
        # 不能在输出错误文字时再次中断请求。
        payload["questions"][0]["type"] = f"mystery{LONE_HIGH}"
        status, data = self.api("POST", "/api/surveys", payload)
        self.assert_rejected_with_location(status, data, "第 1 题", "不支持的题型")
        self.assertEqual(self.listing(), before)

    # ---------- 整份替换（PUT）----------

    def test_replace_with_lone_surrogate_keeps_entire_old_draft(self):
        created = self.create_survey(self.valid_payload())
        survey_id = created["id"]
        before = self.get_detail(survey_id)

        replacement = self.valid_payload()
        replacement["title"] = "试图改成的标题"
        replacement["description"] = "试图改成的说明"
        replacement["questions"][0]["title"] = "前面合法的新题"
        # 坏字符出现在后面的题目里。
        replacement["questions"][1]["options"][1] = f"乙{LONE_HIGH}"
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", replacement)

        self.assert_rejected_with_location(status, data, "第 2 题", "第 2 个选项")

        # 原问卷的编号、标题、说明、题目顺序、必填设置及选项全部保持原样。
        after = self.get_detail(survey_id)
        self.assertEqual(after, before)
        self.assertEqual(after["title"], "正常问卷")
        self.assertEqual(after["description"], "正常说明")
        self.assertEqual([q["title"] for q in after["questions"]],
                         ["第一题", "第二题"])
        self.assertEqual([q["required"] for q in after["questions"]], [True, False])
        self.assertEqual(after["questions"][1]["options"], ["甲", "乙", "丙"])

        titles = {item["id"]: item["title"] for item in self.listing()}
        self.assertEqual(titles[survey_id], "正常问卷")

    def test_replace_unknown_id_with_lone_surrogate_still_404(self):
        unknown_id = max((item["id"] for item in self.listing()), default=0) + 999_999
        payload = self.valid_payload()
        payload["title"] = f"标题{LONE_HIGH}"
        status, data = self.api("PUT", f"/api/surveys/{unknown_id}", payload)
        self.assertEqual(status, 404)
        status, _ = self.api("GET", f"/api/surveys/{unknown_id}")
        self.assertEqual(status, 404)

    # ---------- 合法内容照常保存 ----------

    def test_valid_content_still_saves(self):
        """中文、引号、内部换行与表情符号不能被误拒绝。"""
        payload = {
            "title": f"  满意度调查{EMOJI}  ",
            "description": "  说明带\"引号\"、中文\n与内部换行  ",
            "questions": [
                {"type": "text", "title": f"  感受如何{EMOJI}  ", "required": True},
                {"type": "single_choice", "title": "评分",
                 "options": [f" 好{EMOJI} ", "一般\n还行", "差"]},
            ],
        }
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, data)
        self.assertEqual(data["title"], f"满意度调查{EMOJI}")
        self.assertEqual(data["description"], payload["description"])
        self.assertEqual(data["questions"][0]["title"], f"感受如何{EMOJI}")
        self.assertEqual(data["questions"][1]["options"],
                         [f"好{EMOJI}", "一般\n还行", "差"])
        # 随后读取详情能看到同样的内容。
        self.assertEqual(self.get_detail(data["id"]), data)

    def test_paired_surrogate_escape_equals_literal_character(self):
        """JSON 中正确成对的代理转义与直接发送该表情得到相同的文字。"""
        base = self.valid_payload()
        base["title"] = f"转义对比{EMOJI}"
        base["questions"][0]["title"] = f"题目{EMOJI}"
        base["questions"][1]["options"][0] = f"甲{EMOJI}"
        base["description"] = f"说明{EMOJI}"
        literal = self.create_survey(base)

        # 同一份内容，但表情全部写成 \uD83D\uDE00 形式的转义代理对。
        escaped_body = json.dumps(base, ensure_ascii=False).encode("utf-8")
        escaped_body = escaped_body.replace(
            EMOJI.encode("utf-8"), EMOJI_ESCAPED.encode("ascii"))
        status, escaped = self.api("POST", "/api/surveys", raw_body=escaped_body)
        self.assertEqual(status, 201, escaped)

        # 两种写法落库后的文字完全相同（响应与再次读取都一致）。
        self.assertEqual(escaped["title"], literal["title"])
        self.assertEqual(escaped["description"], literal["description"])
        self.assertEqual(escaped["questions"], literal["questions"])
        self.assertEqual(self.get_detail(escaped["id"])["title"], literal["title"])

    def test_literal_backslash_u_text_is_kept_as_is(self):
        """用户实际输入的反斜杠文本“\uD800”只是普通字符，原样保留。"""
        literal_text = "\\uD800"  # 反斜杠 + 字母数字，共 6 个普通字符
        payload = self.valid_payload()
        payload["title"] = f"标题{literal_text}"
        payload["questions"][1]["options"][0] = f"甲{literal_text}"
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, data)
        self.assertEqual(data["title"], f"标题{literal_text}")
        self.assertEqual(data["questions"][1]["options"][0], f"甲{literal_text}")
        saved = self.get_detail(data["id"])
        self.assertEqual(saved["title"], f"标题{literal_text}")
        self.assertEqual(saved["questions"][1]["options"][0], f"甲{literal_text}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
