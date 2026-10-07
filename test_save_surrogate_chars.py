#!/usr/bin/env python3
"""问卷草稿保存时字符内容校验的自动化回归保障。

覆盖 POST /api/surveys（新建）与 PUT /api/surveys/{id}（整份替换）四类
文字字段——问卷标题、问卷说明、题目标题、单选题选项——的字符边界：

- 合法内容：中文、引号、内部换行，以及正确配对的代理对解码出的表情等
  补充平面字符，无论以直接 UTF-8 文字还是 ``\\uD83D\\uDE00`` 形式的 JSON
  转义提交，都能正常创建/编辑，且两种写法读回的内容必须逐字符一致；
  标题/题目标题/选项仍按既有规则裁掉首尾空白，说明原样保存。
- 非法内容：请求体本身是合法 JSON，但其中 ``\\uD800``/``\\uDC00`` 转义
  解码后出现未配对的 Unicode 代理码点（孤立高代理、孤立低代理都算）。
  接口必须返回 400，响应正文仍是可正常解析的 JSON（不能因服务端无法
  UTF-8 编码而写断连接、丢下半段响应），错误信息说明字符无法保存、给出
  U+XXXX 形式的码点，并精确定位：
    * 问卷标题/说明：指出对应字段；
    * 题目标题：指出第几题；
    * 选项：指出第几题的第几个选项。
- 原子性：
    * 新建被拒绝后，问卷列表不增加任何记录，提交里合法的前半部分也不会
      被留成新草稿；
    * 编辑被拒绝后，原问卷标题、说明、全部题目、必填设置、选项及次序
      完整保留，列表仍显示原标题；即使提交中先改了标题和前面的题目，
      这些局部修改也一点不能留下。

只通过对外可见的 HTTP 行为观察结果，仅依赖标准库，运行方式：

    python3 -m unittest test_save_surrogate_chars -v
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

# 一对互相配对的代理码点，解码后是 U+1F600（😀）。
EMOJI = "\U0001F600"
HIGH_SURROGATE = "\uD800"  # 孤立的高代理码点
LOW_SURROGATE = "\uDC00"   # 孤立的低代理码点


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
        """以直接 UTF-8 文字形式提交 JSON，返回 (status, payload_dict_or_raw)。"""
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return self.raw_request(method, path, data, headers)

    def request_ascii_escaped(self, method, path, body):
        """提交 JSON，但非 ASCII 字符一律写成 ``\\uXXXX`` 转义。

        Python 的 json.dumps 会把含孤立代理码点的字符串逐字输出成
        ``\\uD800`` 这样的合法 JSON 转义；服务端解码后才得到 Python 内部
        的未配对代理码点。这正是“请求是合法 JSON、解码后才有坏字符”的
        进入方式。配对的代理对（如 ``\\uD83D\\uDE00``）也经此路径提交，
        用于验证它必须被正常解码成表情而不是误判为非法。
        """
        data = json.dumps(body, ensure_ascii=True).encode("ascii")
        return self.raw_request(
            method, path, data, {"Content-Type": "application/json"})

    def raw_request(self, method, path, data=None, headers=None):
        """发起最底层的 HTTP 请求并完整读回响应。

        返回 (status, headers, payload)：payload 能解析为 JSON 时返回解析
        后的对象，否则返回原始字符串。关键的一点是：如果服务端在写错误
        响应中途因无法编码而中断连接，这里的 read() 会直接抛异常，测试
        当场失败，从而把“断开连接/半份响应”与“完整的 JSON 错误体”区分开。
        """
        headers = headers or {}
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
                resp_headers = resp.headers
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            status = error.code
            resp_headers = error.headers
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        return status, resp_headers, payload


class SurrogateValidationTests(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = SurveyServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    # -- 基础辅助 -------------------------------------------------------

    def api(self, method, path, body=None):
        status, _headers, data = self.server.request(method, path, body)
        return status, data

    def api_escaped(self, method, path, body):
        status, _headers, data = self.server.request_ascii_escaped(method, path, body)
        return status, data

    def create_survey(self, payload):
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data

    def get_detail(self, survey_id):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, f"读取详情失败：{data}")
        return data

    def list_ids_titles(self):
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, f"读取列表失败：{data}")
        return {item["id"]: item["title"] for item in data["surveys"]}

    def assert_complete_json_error(self, status, headers, data, code_point):
        """400 错误体必须是完整、可解析的 JSON，并给出 U+XXXX 码点。"""
        self.assertEqual(status, 400)
        # 响应头明确是 JSON；Content-Length 由服务端按完整错误体给出。
        self.assertIn("application/json", headers.get("Content-Type", ""))
        # 能解析成对象且含 error 字符串：若连接中途断开，这里拿到的会是
        # JSONDecodeError 或被 urllib 当作连接错误抛出，而不是走到此处。
        self.assertIsInstance(data, dict)
        message = data.get("error")
        self.assertIsInstance(message, str)
        self.assertIn("无法编码保存", message)
        self.assertIn(f"U+{code_point:04X}", message)
        return message

    def valid_survey(self, **overrides):
        """一份各字段都合法、可随意改坏某一处的基准草稿。"""
        payload = {
            "title": "字符校验问卷",
            "description": "说明含中文与\"引号\"，\n还有内部换行。",
            "questions": [
                {"type": "text", "title": "第一道文本题", "required": True},
                {"type": "single_choice", "title": "第二道单选题",
                 "required": False, "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }
        payload.update(overrides)
        return payload

    # -- 新建：四类字段上的孤立高/低代理码点都必须被拒绝 -----------------

    def test_create_rejects_lone_high_surrogate_in_title(self):
        before = self.list_ids_titles()
        body = self.valid_survey()
        body["title"] = f"标题里有坏字符{HIGH_SURROGATE}结尾"
        status, headers, data = self.server.request_ascii_escaped(
            "POST", "/api/surveys", body)

        message = self.assert_complete_json_error(status, headers, data, 0xD800)
        self.assertIn("问卷标题", message)

        after = self.list_ids_titles()
        self.assertEqual(after, before)

    def test_create_rejects_lone_low_surrogate_in_description(self):
        before = self.list_ids_titles()
        body = self.valid_survey()
        body["description"] = f"说明中间{LOW_SURROGATE}混入坏字符"
        status, headers, data = self.server.request_ascii_escaped(
            "POST", "/api/surveys", body)

        message = self.assert_complete_json_error(status, headers, data, 0xDC00)
        self.assertIn("问卷说明", message)

        self.assertEqual(self.list_ids_titles(), before)

    def test_create_rejects_lone_high_surrogate_in_question_title(self):
        before = self.list_ids_titles()
        body = self.valid_survey()
        # 第一题完全合法，坏字符出现在后一题的标题上。
        body["questions"][1]["title"] = f"第二题标题{HIGH_SURROGATE}"
        status, headers, data = self.server.request_ascii_escaped(
            "POST", "/api/surveys", body)

        message = self.assert_complete_json_error(status, headers, data, 0xD800)
        # 必须指出是第几题，而不能只笼统地说题目标题有问题。
        self.assertIn("第 2 题", message)
        self.assertIn("题目标题", message)
        self.assertNotIn("选项", message)

        self.assertEqual(self.list_ids_titles(), before)

    def test_create_rejects_lone_surrogate_in_first_question_title(self):
        """坏字符出现在第一道题（定位不能依赖“后面的题”这一前提）。"""
        before = self.list_ids_titles()
        body = self.valid_survey()
        body["questions"][0]["title"] = f"{LOW_SURROGATE}开头就是坏的"
        status, headers, data = self.server.request_ascii_escaped(
            "POST", "/api/surveys", body)

        message = self.assert_complete_json_error(status, headers, data, 0xDC00)
        self.assertIn("第 1 题", message)
        self.assertIn("题目标题", message)

        self.assertEqual(self.list_ids_titles(), before)

    def test_create_rejects_lone_low_surrogate_in_option(self):
        before = self.list_ids_titles()
        body = self.valid_survey()
        # 第一题合法；第二题的第 1 个选项合法，第 2 个选项含孤立低代理。
        body["questions"][1]["options"] = ["合法选项", f"选项带{LOW_SURROGATE}", "选项丙"]
        status, headers, data = self.server.request_ascii_escaped(
            "POST", "/api/surveys", body)

        message = self.assert_complete_json_error(status, headers, data, 0xDC00)
        # 必须同时指出第几题、第几个选项。
        self.assertIn("第 2 题", message)
        self.assertIn("第 2 个选项", message)

        self.assertEqual(self.list_ids_titles(), before)

    def test_create_rejects_lone_high_surrogate_in_later_option(self):
        """前面的题目与同题前两个选项都合法，最后一个选项才出错。"""
        before = self.list_ids_titles()
        body = self.valid_survey()
        body["questions"][1]["options"] = ["选项甲", "选项乙", f"坏{HIGH_SURROGATE}"]
        status, headers, data = self.server.request_ascii_escaped(
            "POST", "/api/surveys", body)

        message = self.assert_complete_json_error(status, headers, data, 0xD800)
        self.assertIn("第 2 题", message)
        self.assertIn("第 3 个选项", message)

        self.assertEqual(self.list_ids_titles(), before)

    def test_rejected_create_keeps_no_partial_draft(self):
        """被拒绝的新建：列表不增加，任何 id 上都读不到提交中的合法部分。"""
        before = self.list_ids_titles()
        body = self.valid_survey()
        # 标题、说明、第一题全部合法，只有第二题标题出错。
        body["title"] = "本不该存下的标题"
        body["questions"][0] = {"type": "text", "title": "本不该存下的前题"}
        body["questions"][1]["title"] = f"坏题{HIGH_SURROGATE}"

        status, _headers, data = self.server.request_ascii_escaped(
            "POST", "/api/surveys", body)
        self.assertEqual(status, 400)
        self.assertIsInstance(data, dict)

        after = self.list_ids_titles()
        self.assertEqual(after, before)
        # 列表标题中绝不能出现提交里的合法标题。
        self.assertNotIn("本不该存下的标题", after.values())
        # 下一个编号也必须尚不存在：没有半份草稿被悄悄创建。
        next_id = max(before, default=0) + 1
        status, _ = self.api("GET", f"/api/surveys/{next_id}")
        self.assertEqual(status, 404)

    # -- 编辑：被拒绝时旧草稿必须逐字段完整保留 --------------------------

    def test_edit_rejects_surrogate_without_touching_old_draft(self):
        """编辑时后一题标题含孤立代理：400，旧草稿（含前面的改动企图）原样。"""
        created = self.create_survey({
            "title": "不可变的原标题",
            "description": "原说明\n第二行带\"引号\"",
            "questions": [
                {"type": "text", "title": "原第一题", "required": True},
                {"type": "single_choice", "title": "原第二题", "required": True,
                 "options": ["原选项甲", "原选项乙", "原选项丙"]},
            ],
        })
        survey_id = created["id"]
        before = self.get_detail(survey_id)

        replacement = {
            # 先改标题、说明与第一道题（这些内容本身合法），坏字符只出现在
            # 后一道题的标题上：任何局部修改都不能落库。
            "title": "试图改成的新标题",
            "description": "试图改成的新说明",
            "questions": [
                {"type": "text", "title": "试图改成的前题", "required": False},
                {"type": "single_choice", "title": f"后题带{LOW_SURROGATE}",
                 "options": ["新甲", "新乙"]},
            ],
        }
        status, headers, data = self.server.request_ascii_escaped(
            "PUT", f"/api/surveys/{survey_id}", replacement)

        message = self.assert_complete_json_error(status, headers, data, 0xDC00)
        self.assertIn("第 2 题", message)
        self.assertIn("题目标题", message)

        # 详情逐字段保持：标题、说明、全部题目、必填、选项与次序。
        after = self.get_detail(survey_id)
        self.assertEqual(after, before)
        self.assertEqual(after["title"], "不可变的原标题")
        self.assertEqual(after["description"], "原说明\n第二行带\"引号\"")
        self.assertEqual([q["title"] for q in after["questions"]],
                         ["原第一题", "原第二题"])
        self.assertEqual([q["required"] for q in after["questions"]], [True, True])
        self.assertEqual(after["questions"][1]["options"],
                         ["原选项甲", "原选项乙", "原选项丙"])
        # 提交中的合法局部修改绝不能留下。
        titles = [q["title"] for q in after["questions"]]
        self.assertNotIn("试图改成的新标题", [after["title"]])
        self.assertNotIn("试图改成的前题", titles)

        # 列表仍显示原标题，编号不变。
        listing = self.list_ids_titles()
        self.assertEqual(listing[survey_id], "不可变的原标题")

    def test_edit_rejects_bad_option_without_touching_old_draft(self):
        """编辑时后一个选项含孤立高代理：400，旧选项与次序完整保留。"""
        created = self.create_survey(self.valid_survey())
        survey_id = created["id"]
        before = self.get_detail(survey_id)

        replacement = self.valid_survey()
        replacement["title"] = "换不掉的标题"
        replacement["questions"][1]["options"] = ["新甲", f"新乙{HIGH_SURROGATE}"]
        status, headers, data = self.server.request_ascii_escaped(
            "PUT", f"/api/surveys/{survey_id}", replacement)

        message = self.assert_complete_json_error(status, headers, data, 0xD800)
        self.assertIn("第 2 题", message)
        self.assertIn("第 2 个选项", message)

        self.assertEqual(self.get_detail(survey_id), before)
        self.assertEqual(self.list_ids_titles()[survey_id], "字符校验问卷")

    def test_edit_rejects_bad_title_and_description(self):
        """编辑时坏字符落在标题/说明上：400 并指明字段，旧草稿不变。"""
        created = self.create_survey(self.valid_survey())
        survey_id = created["id"]
        before = self.get_detail(survey_id)

        cases = (
            ("title", "问卷标题", HIGH_SURROGATE, 0xD800),
            ("description", "问卷说明", LOW_SURROGATE, 0xDC00),
        )
        for field, label, bad_char, code in cases:
            replacement = self.valid_survey()
            replacement[field] = f"带坏字符{bad_char}"
            status, headers, data = self.server.request_ascii_escaped(
                "PUT", f"/api/surveys/{survey_id}", replacement)
            message = self.assert_complete_json_error(status, headers, data, code)
            self.assertIn(label, message)
            # 每次拒绝后旧草稿都必须原样可读。
            self.assertEqual(self.get_detail(survey_id), before)

        self.assertEqual(self.list_ids_titles()[survey_id], "字符校验问卷")

    # -- 合法边界：配对代理对（表情）在两种写法下都能保存且读回一致 --------

    def _emoji_payload(self):
        return {
            # 标题含表情与内部换行，首尾空白应被裁掉。
            "title": f"  表情问卷{EMOJI}\n第二行  ",
            # 说明原样保存：首尾空白、引号、中文、换行、表情都保留。
            "description": f"  说明{EMOJI}首行\n第二行\"引号\"收尾  ",
            "questions": [
                {"type": "text", "title": f"\t文本题{EMOJI}换行\n在内 ", "required": True},
                {"type": "single_choice", "title": f" 选择题{EMOJI} ",
                 "options": [f" 选项{EMOJI}甲 ", f"乙\n换行{EMOJI}"]},
            ],
        }

    def test_create_accepts_direct_emoji_and_preserves_content(self):
        """直接 UTF-8 文字提交表情：成功创建，响应与读取详情都保留字符。"""
        payload = self._emoji_payload()
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"含表情的创建被拒绝：{data}")
        survey_id = data["id"]

        self.assertEqual(data["title"], payload["title"].strip())
        self.assertIn(EMOJI, data["title"])
        self.assertIn("\n", data["title"])
        # 说明逐字保留，包括首尾空白。
        self.assertEqual(data["description"], payload["description"])
        self.assertTrue(data["description"].startswith("  "))
        self.assertTrue(data["description"].endswith("  "))
        self.assertEqual(data["questions"][0]["title"],
                         payload["questions"][0]["title"].strip())
        self.assertEqual(data["questions"][1]["options"],
                         [opt.strip() for opt in payload["questions"][1]["options"]])
        for field in (data["title"], data["description"],
                      data["questions"][0]["title"],
                      *data["questions"][1]["options"]):
            self.assertIn(EMOJI, field)

        # 随后读取的草稿与成功响应完全一致，字符与内部换行都还在。
        self.assertEqual(self.get_detail(survey_id), data)

    def test_paired_surrogate_escape_decodes_to_same_emoji_on_create(self):
        r"""😀 形式的配对转义与直接文字表达同一表情，读回一致。"""
        # 直接文字写法。
        direct_status, direct = self.api(
            "POST", "/api/surveys", self._emoji_payload())
        self.assertEqual(direct_status, 201, f"直接表情创建失败：{direct}")

        # 同一份内容以 \uXXXX 转义（含配对代理对）写法提交。
        escaped_status, escaped = self.api_escaped(
            "POST", "/api/surveys", self._emoji_payload())
        self.assertEqual(escaped_status, 201, f"配对转义表情创建失败：{escaped}")

        # 请求体确实走了 ASCII 转义路径：抽查服务端收到的应是解码后的表情。
        direct_detail = self.get_detail(direct["id"])
        escaped_detail = self.get_detail(escaped["id"])
        # 编号不同，去掉编号后两份草稿必须逐字段、逐字符相同。
        for detail in (direct_detail, escaped_detail):
            del detail["id"]
        self.assertEqual(escaped_detail, direct_detail)
        # 明确断言：读回的是一个完整的补充平面字符，而不是两个代理码点。
        self.assertIn(EMOJI, escaped_detail["title"])
        self.assertNotIn(HIGH_SURROGATE, json.dumps(escaped_detail, ensure_ascii=False))

    def test_edit_accepts_emoji_in_both_forms_and_preserves(self):
        """编辑时直接文字与配对转义两种表情写法都能保存，读回一致。"""
        created = self.create_survey(self.valid_survey())
        survey_id = created["id"]

        # 第一次整份替换：直接 UTF-8 表情。
        payload = self._emoji_payload()
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", payload)
        self.assertEqual(status, 200, f"含表情的编辑被拒绝：{data}")
        self.assertEqual(data["id"], survey_id)
        direct_expected = self.get_detail(survey_id)
        self.assertEqual(direct_expected, data)

        # 第二次整份替换：同样内容改用 😀 配对转义提交。
        status, escaped = self.api_escaped(
            "PUT", f"/api/surveys/{survey_id}", payload)
        self.assertEqual(status, 200, f"配对转义表情编辑被拒绝：{escaped}")
        self.assertEqual(escaped["id"], survey_id)

        after = self.get_detail(survey_id)
        # 两种写法保存后的可见内容必须完全一致。
        self.assertEqual(after, direct_expected)
        self.assertIn(EMOJI, after["title"])
        self.assertIn(EMOJI, after["description"])
        self.assertIn(EMOJI, after["questions"][0]["title"])
        self.assertTrue(any(EMOJI in opt for opt in after["questions"][1]["options"]))
        # 内部换行仍在，说明首尾空白仍原样保留。
        self.assertIn("\n", after["title"])
        self.assertEqual(after["description"], payload["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
