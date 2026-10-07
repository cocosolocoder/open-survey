#!/usr/bin/env python3
"""问卷草稿“字符内容校验”的自动化回归保障（POST 创建 / PUT 整份替换）。

只通过对外可见的 HTTP 行为观察结果，服务进程为真实 app.py：
- POST /api/surveys        创建问卷草稿
- PUT  /api/surveys/{id}   整份替换问卷草稿
- GET  /api/surveys/{id}   读取详情
- GET  /api/surveys        问卷列表

保护的既有规则：

- 问卷标题、说明、题目标题、单选选项可以保存中文、引号、内部换行与表情
  （含表情等补充平面字符，UTF-16 下是一对正确配对的代理码点）。
- 请求体本身是合法 JSON，但其中的 ``\\uXXXX`` 转义解码后出现“未配对的
  Unicode 代理码点”（孤立高代理 U+D800..U+DBFF 或孤立低代理
  U+DC00..U+DFFF）时，接口必须返回 400：响应正文仍是可以正常解析的 JSON，
  说明字符无法保存并给出 U+XXXX 码点；不能中断连接，也不能保存半份草稿。
  四类文字字段都要覆盖，且包括“前面的题目合法、后面的题目标题或选项才
  出错”的情形；标题/说明错误指名字段，题目标题错误指出第几题，选项错误
  还要指出第几个选项。
- 创建被拒绝：列表一条记录都不能多，提交中合法的部分也不能留成草稿。
- 编辑被拒绝：原问卷标题、说明、全部题目、必填设置、选项及次序原样保留，
  列表仍显示原标题；提交中先改好的标题与前面题目也不能局部落库。
- 合法边界：正确配对的代理转义解出的表情/补充平面字符可正常创建与编辑；
  同一表情以“直接文字（UTF-8）”和“配对 \\uXXXX 转义（ASCII）”两种方式
  提交，读到的内容必须一致；成功响应与随后读取的草稿都保留这些字符与内部
  换行；标题、题目标题、选项按既有规则裁掉首尾空白，说明原样保存。

仅依赖标准库，运行方式：

    python3 -m unittest test_survey_character_validation -v
"""
import http.client
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

# 孤立的高代理与孤立的低代理各取一个代表（均无法以 UTF-8 编码保存）。
LONELY_HIGH = 0xD800
LONELY_LOW = 0xDC00

# 表情 😀（U+1F600）在 UTF-16 下是一对正确配对的代理：
# 高代理 U+D83D + 低代理 U+DE00。以 ensure_ascii 发送时，json.dumps 会自动
# 把它写成连续的两个转义（高代理转义后紧跟低代理转义），服务端应把二者
# 解码成同一个表情，而不是当作孤立代理拒绝。
EMOJI = "\U0001F600"


class SurveyServer:
    """在临时数据目录上启动一个真实的 app.py 服务进程。"""

    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-chars-")
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
        self._wait_until_ready(port)

    def _wait_until_ready(self, port):
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
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

    def send(self, method, path, payload, *, ascii_escapes=False):
        """发送 JSON 请求，返回 (status, value)。

        ascii_escapes=False（默认）：以 UTF-8 直接发送文字（中文、表情原样
        上线）。ascii_escapes=True：json.dumps 的 ensure_ascii 把所有非 ASCII
        字符写成 \\uXXXX——含孤立代理码点的字符串只能这样合法地发出去
        （ensure_ascii=False 会在客户端编码时直接抛 UnicodeEncodeError），
        配对代理则会写成连续的 \\uD83D\\uDE00。服务端收到的都是合法 JSON，
        代理是否配对要由服务端解码后自行判定。
        """
        data = json.dumps(payload, ensure_ascii=ascii_escapes).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json"}, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as error:
            # 正常路径：400 也带着完整的 JSON 错误正文走到这里。
            raw = error.read().decode("utf-8")
            status = error.code
        except (urllib.error.URLError, http.client.HTTPException,
                ConnectionError, OSError) as error:
            # 关键回归点：服务端绝不能因为无法编码保存的字符而断开连接。
            # 一旦发生断连/重置，明确报成断言失败（而不是让底层异常带着
            # 堆栈冒出），直指“连接被中断、没有拿到 JSON 响应”。
            raise AssertionError(
                f"{method} {path} 的连接被服务端中断，未返回完整的 JSON 响应"
                f"（疑似无法编码的字符导致写出中途失败）：{type(error).__name__}: {error}"
            ) from error
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw


class ServerTestCase(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = SurveyServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def api(self, method, path, payload, *, ascii_escapes=False):
        return self.server.send(method, path, payload,
                                ascii_escapes=ascii_escapes)

    def create_ok(self, payload, *, ascii_escapes=False):
        status, data = self.api("POST", "/api/surveys", payload,
                                ascii_escapes=ascii_escapes)
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data

    def _raw_get(self, path):
        """发起无请求体的 GET，返回 (status, value)。"""
        req = urllib.request.Request(self.server.base_url + path, method="GET")
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

    def get_detail(self, survey_id):
        status, data = self._raw_get(f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, f"读取详情失败：{data}")
        return data

    def listing_titles(self):
        status, data = self._raw_get("/api/surveys")
        self.assertEqual(status, 200, f"读取列表失败：{data}")
        return {item["id"]: item["title"] for item in data["surveys"]}

    def listing(self):
        status, data = self._raw_get("/api/surveys")
        self.assertEqual(status, 200, f"读取列表失败：{data}")
        return data


def valid_payload():
    """一份各字段都合法、且四类字段都含多行内容的问卷。"""
    return {
        "title": "问卷标题\n第二行",
        "description": '说明 “引号” 与中文\n第二行',
        "questions": [
            {"type": "text", "title": "文本题一", "required": True},
            {"type": "single_choice", "title": "单选题二",
             "options": ["选项甲", "选项乙"]},
        ],
    }


def with_bad_char(target, codepoint):
    """在合法问卷的指定字段塞入一个孤立代理码点，返回 (payload, 定位断言词)。

    target 取值：
      ("title",) / ("description",)
      ("question", 题号)
      ("option", 题号, 选项号)
    题目标题/选项都放在多题问卷里，以便覆盖“前面的题目合法、后面才出错”。
    """
    payload = valid_payload()
    bad = f"前文{chr(codepoint)}后文"
    kind = target[0]
    if kind == "title":
        payload["title"] = bad
        return payload, ["问卷标题"]
    if kind == "description":
        payload["description"] = bad
        return payload, ["问卷说明"]
    if kind == "question":
        index = target[1]
        payload["questions"][index - 1]["title"] = bad
        return payload, [f"第 {index} 题", "题目标题"]
    if kind == "option":
        index, option_index = target[1], target[2]
        payload["questions"][index - 1]["options"][option_index - 1] = bad
        return payload, [f"第 {index} 题", f"第 {option_index} 个选项"]
    raise AssertionError(f"未知字段目标：{target!r}")


# 四类文字字段，题目/选项同时覆盖第 1 题与后面的第 2 题、以及第 2 个选项。
FIELD_TARGETS = [
    ("title", ("title",)),
    ("description", ("description",)),
    ("第 1 题题目标题", ("question", 1)),
    ("后面第 2 题题目标题", ("question", 2)),
    ("第 2 题第 1 个选项", ("option", 2, 1)),
    ("第 2 题第 2 个选项", ("option", 2, 2)),
]


class CreateRejectsUnpairedSurrogateTests(ServerTestCase):
    """创建：四类文字字段中的孤立高/低代理一律 400，且不留任何记录。"""

    def assert_surrogate_error(self, data, codepoint, location_words):
        # 错误正文必须仍是可正常解析的 JSON 对象（不是断连/半截响应/纯文本）。
        self.assertIsInstance(data, dict,
                              f"错误正文不是可解析的 JSON 对象：{data!r}")
        self.assertIn("error", data)
        message = data["error"]
        self.assertIsInstance(message, str)
        # 码点以 U+XXXX 形式给出（错误信息自身也必须能 UTF-8 编码）。
        self.assertIn(f"U+{codepoint:04X}", message)
        # 错误定位：字段名 / 第几题 / 第几个选项。
        for word in location_words:
            self.assertIn(word, message)

    def test_every_text_field_rejects_lone_high_and_low_surrogate(self):
        listing_before = self.listing()
        for label, target in FIELD_TARGETS:
            for codepoint in (LONELY_HIGH, LONELY_LOW):
                with self.subTest(field=label, codepoint=f"U+{codepoint:04X}"):
                    payload, words = with_bad_char(target, codepoint)
                    # 含孤立代理的正文以 ASCII \uXXXX 转义发送：HTTP 层与
                    # JSON 语法都合法，解码后才暴露未配对代理。
                    status, data = self.api(
                        "POST", "/api/surveys", payload, ascii_escapes=True)
                    self.assertEqual(status, 400)
                    self.assert_surrogate_error(data, codepoint, words)

                    # 创建被拒绝后列表一条记录都不能增加。
                    self.assertEqual(self.listing(), listing_before)

    def test_later_bad_question_does_not_save_its_valid_parts(self):
        """前题合法、第 2 题题目标题含孤立代理：整份都不能留成草稿。"""
        listing_before = self.listing()
        payload = valid_payload()
        payload["title"] = "本不该被创建的标题"
        payload["questions"][0]["title"] = "合法的第一题"
        payload["questions"][1]["title"] = f"坏的第二题{chr(LONELY_HIGH)}"

        status, data = self.api("POST", "/api/surveys", payload,
                                ascii_escapes=True)
        self.assertEqual(status, 400)
        self.assertIsInstance(data, dict)
        self.assertIn("U+D800", data["error"])
        self.assertIn("第 2 题", data["error"])
        self.assertIn("题目标题", data["error"])

        # 合法的标题、第一题也不能作为半份草稿留存：列表中找不到该标题，
        # 且记录总数不变。
        self.assertEqual(self.listing(), listing_before)
        titles = list(self.listing_titles().values())
        self.assertNotIn("本不该被创建的标题", titles)
        self.assertNotIn("坏的第二题", titles)

    def test_later_bad_option_does_not_save_its_valid_parts(self):
        """坏选项在第 2 题：无论落在第 1 还是第 2 个选项，整份都不留草稿，
        且错误定位准确到具体选项。"""
        listing_before = self.listing()
        payload = valid_payload()
        payload["questions"][1]["options"] = [
            f"选项甲{chr(LONELY_LOW)}", "选项乙"]  # 第 1 个选项先出错时定位到它
        status, data = self.api("POST", "/api/surveys", payload,
                                ascii_escapes=True)
        self.assertEqual(status, 400)
        self.assertIn("U+DC00", data["error"])
        self.assertIn("第 2 题", data["error"])
        self.assertIn("第 1 个选项", data["error"])
        self.assertEqual(self.listing(), listing_before)

        # 同类情形：坏字符落在第 2 个选项，定位必须随之改变。
        payload = valid_payload()
        payload["questions"][1]["options"] = [
            "合法选项甲", f"选项乙{chr(LONELY_LOW)}"]
        status, data = self.api("POST", "/api/surveys", payload,
                                ascii_escapes=True)
        self.assertEqual(status, 400)
        self.assertIn("U+DC00", data["error"])
        self.assertIn("第 2 题", data["error"])
        self.assertIn("第 2 个选项", data["error"])
        self.assertEqual(self.listing(), listing_before)

    def test_malformed_surrogate_pairs_are_rejected_cleanly(self):
        """畸形代理序列同样拒绝：顺序颠倒（低后高）与高代理后不跟低代理。

        合法 JSON 可以写出 ``\\uDE00\\uD83D``（低代理在前、高代理在后）或
        ``\\uD83DX``（高代理后不是低代理）；两者都解不出补充平面字符，只
        剩下未配对代理。接口必须返回 400、正文是可解析 JSON，且连接完整
        （不中断、不返回半截响应）。
        """
        # 低代理 U+DE00 在前、高代理 U+D83D 在后：顺序颠倒，两个都是孤立的，
        # 报错定位到先出现的低代理 U+DE00。
        reversed_pair = "颠倒" + chr(0xDE00) + chr(0xD83D) + "结尾"
        # 高代理 U+D83D 后面直接跟普通字符，没有低代理与之配对。
        dangling_high = "悬空" + chr(0xD83D) + "X"
        for bad, codepoint in ((reversed_pair, "U+DE00"),
                               (dangling_high, "U+D83D")):
            with self.subTest(codepoint=codepoint):
                payload = {
                    "title": bad,
                    "questions": [{"type": "text", "title": "合法题目"}],
                }
                status, data = self.api("POST", "/api/surveys", payload,
                                        ascii_escapes=True)
                self.assertEqual(status, 400)
                self.assertIsInstance(data, dict)
                self.assertIn("error", data)
                self.assertIn(codepoint, data["error"])
                self.assertIn("问卷标题", data["error"])


class EditRejectsUnpairedSurrogateTests(ServerTestCase):
    """编辑：拒绝替换时旧草稿（含必填、选项、次序）完整保留。"""

    def seed_draft(self):
        created = self.create_ok({
            "title": "不可变标题",
            "description": "原说明\n带换行与\"引号\"",
            "questions": [
                {"type": "text", "title": "必填文本题", "required": True},
                {"type": "single_choice", "title": "旧单选题", "required": False,
                 "options": ["旧选项甲", "旧选项乙", "旧选项丙"]},
            ],
        })
        return created["id"]

    def assert_draft_unchanged(self, survey_id, before):
        after_status, after = self._raw_get(f"/api/surveys/{survey_id}")
        self.assertEqual(after_status, 200)
        # 整份旧草稿逐项相等：标题、说明、题目、必填、选项、次序。
        self.assertEqual(after, before)
        self.assertEqual(after["title"], "不可变标题")
        self.assertEqual(after["description"], "原说明\n带换行与\"引号\"")
        self.assertEqual([q["title"] for q in after["questions"]],
                         ["必填文本题", "旧单选题"])
        self.assertEqual([q["required"] for q in after["questions"]], [True, False])
        self.assertEqual(after["questions"][0]["options"], [])
        self.assertEqual(after["questions"][1]["options"],
                         ["旧选项甲", "旧选项乙", "旧选项丙"])
        # 列表仍显示原来的标题。
        self.assertEqual(self.listing_titles()[survey_id], "不可变标题")

    def test_bad_later_question_title_after_valid_edits_leaves_no_trace(self):
        """先改标题和前题（合法），第 2 题题目标题含孤立高代理：全部回滚。"""
        survey_id = self.seed_draft()
        before_status, before = self._raw_get(f"/api/surveys/{survey_id}")
        self.assertEqual(before_status, 200)

        replacement = {
            "title": "试图改成的标题",
            "description": "试图改成的说明",
            "questions": [
                {"type": "text", "title": "前面合法的新题", "required": False},
                {"type": "single_choice", "title": f"后面坏题{chr(LONELY_HIGH)}",
                 "required": True, "options": ["新甲", "新乙"]},
            ],
        }
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", replacement,
                                ascii_escapes=True)
        self.assertEqual(status, 400)
        self.assertIsInstance(data, dict)
        self.assertIn("U+D800", data["error"])
        self.assertIn("第 2 题", data["error"])
        self.assertIn("题目标题", data["error"])

        self.assert_draft_unchanged(survey_id, before)
        # 提交中先改好的标题与前面题目也不能局部落库。
        after_titles = [q["title"] for q in before["questions"]]
        self.assertNotIn("前面合法的新题", after_titles)
        self.assertNotIn("后面坏题", after_titles)
        self.assertNotIn("试图改成的标题", self.listing_titles().values())

    def test_bad_later_option_leaves_entire_old_draft_untouched(self):
        """第 2 题第 2 个选项含孤立低代理：旧草稿与选项次序原样保留。"""
        survey_id = self.seed_draft()
        _, before = self._raw_get(f"/api/surveys/{survey_id}")

        replacement = {
            "title": "试图改成的标题",
            "questions": [
                {"type": "text", "title": "前面合法的新题", "required": True},
                {"type": "single_choice", "title": "也是合法标题",
                 "options": ["合法新甲", f"坏选项乙{chr(LONELY_LOW)}"]},
            ],
        }
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", replacement,
                                ascii_escapes=True)
        self.assertEqual(status, 400)
        self.assertIsInstance(data, dict)
        self.assertIn("U+DC00", data["error"])
        self.assertIn("第 2 题", data["error"])
        self.assertIn("第 2 个选项", data["error"])

        self.assert_draft_unchanged(survey_id, before)

    def test_bad_title_and_bad_description_are_named_on_edit(self):
        """编辑时标题/说明字段的孤立代理也要指名字段并原样保留旧草稿。"""
        survey_id = self.seed_draft()
        _, before = self._raw_get(f"/api/surveys/{survey_id}")

        for field, word, codepoint in (
            ("title", "问卷标题", LONELY_HIGH),
            ("description", "问卷说明", LONELY_LOW),
        ):
            with self.subTest(field=field):
                replacement = {
                    "title": f"坏标题{chr(codepoint)}" if field == "title"
                    else "合法标题",
                    "description": f"坏说明{chr(codepoint)}"
                    if field == "description" else "合法说明",
                    "questions": [
                        {"type": "text", "title": "合法题目"},
                        {"type": "single_choice", "title": "合法单选",
                         "options": ["甲", "乙"]},
                    ],
                }
                status, data = self.api(
                    "PUT", f"/api/surveys/{survey_id}", replacement,
                    ascii_escapes=True)
                self.assertEqual(status, 400)
                self.assertIsInstance(data, dict)
                self.assertIn(f"U+{codepoint:04X}", data["error"])
                self.assertIn(word, data["error"])
                _, after = self._raw_get(f"/api/surveys/{survey_id}")
                self.assertEqual(after, before)
                self.assertEqual(self.listing_titles()[survey_id], "不可变标题")


class ValidCharactersRoundTripTests(ServerTestCase):
    """合法边界：中文、引号、换行与配对代理（表情）正常保存，两种表达等价。"""

    def payload_with_emoji(self):
        """四类文字字段都含表情与内部换行；首尾空白用于验证裁剪规则。"""
        return {
            # 标题：首尾空白应被裁掉，内部换行与表情保留。
            "title": f"  问卷{EMOJI}标题\n第二行  ",
            # 说明：首尾空白、引号、换行、表情全部原样保留（不裁剪）。
            "description": f'  说明 “引号” 与{EMOJI}中文\n第二行  ',
            "questions": [
                {"type": "text", "title": f"  文本题{EMOJI}\n第二行  ",
                 "required": True},
                {"type": "single_choice", "title": f"单选题{EMOJI}",
                 "required": False,
                 "options": [f" 选项{EMOJI}甲 ", f"选项乙\n带换行{EMOJI}"]},
            ],
        }

    def expected_clean(self, survey_id, payload):
        return {
            "id": survey_id,
            "title": payload["title"].strip(),
            "description": payload["description"],
            "questions": [
                {"type": "text",
                 "title": payload["questions"][0]["title"].strip(),
                 "required": True, "options": []},
                {"type": "single_choice",
                 "title": payload["questions"][1]["title"].strip(),
                 "required": False,
                 "options": [opt.strip() for opt in
                             payload["questions"][1]["options"]]},
            ],
        }

    def test_direct_utf8_and_paired_escapes_create_identical_drafts(self):
        payload = self.payload_with_emoji()

        # 方式一：直接文字，UTF-8 上线。
        status_direct, direct = self.api(
            "POST", "/api/surveys", payload, ascii_escapes=False)
        self.assertEqual(status_direct, 201, direct)
        # 成功响应本身就保留表情与内部换行（不是 \u 转义回来的残缺形态）。
        self.assertIn(EMOJI, direct["title"])
        self.assertIn("\n", direct["title"])
        self.assertIn(EMOJI, direct["description"])
        self.assertIn(EMOJI, direct["questions"][0]["title"])
        self.assertIn(EMOJI, direct["questions"][1]["options"][0])

        # 方式二：同一表情用配对的两个 \uXXXX 转义（ASCII JSON）发送。
        status_escaped, escaped = self.api(
            "POST", "/api/surveys", payload, ascii_escapes=True)
        self.assertEqual(status_escaped, 201, escaped)

        expected_direct = self.expected_clean(direct["id"], payload)
        expected_escaped = self.expected_clean(escaped["id"], payload)
        self.assertEqual(direct, expected_direct)
        self.assertEqual(escaped, expected_escaped)

        # 两种表达读到的内容逐字符一致（编号各自不同，去掉编号后整份相等）。
        detail_direct = self.get_detail(direct["id"])
        detail_escaped = self.get_detail(escaped["id"])
        self.assertEqual(detail_direct, expected_direct)
        self.assertEqual(detail_escaped, expected_escaped)
        for key in ("title", "description"):
            self.assertEqual(detail_direct[key], detail_escaped[key])
        self.assertEqual(
            [q["title"] for q in detail_direct["questions"]],
            [q["title"] for q in detail_escaped["questions"]])
        self.assertEqual(
            [q["options"] for q in detail_direct["questions"]],
            [q["options"] for q in detail_escaped["questions"]])

        # 裁剪规则没有被字符校验改变：标题/题目标题/选项裁掉首尾空白，
        # 内部换行与表情仍在；说明原样保留（首尾空白还在）。
        self.assertEqual(detail_direct["title"], f"问卷{EMOJI}标题\n第二行")
        self.assertTrue(detail_direct["title"].startswith("问卷"))
        self.assertEqual(
            detail_direct["description"],
            f'  说明 “引号” 与{EMOJI}中文\n第二行  ')
        self.assertTrue(detail_direct["description"].startswith("  "))
        self.assertTrue(detail_direct["description"].endswith("  "))
        self.assertEqual(detail_direct["questions"][0]["title"],
                         f"文本题{EMOJI}\n第二行")
        self.assertEqual(detail_direct["questions"][1]["title"],
                         f"单选题{EMOJI}")
        self.assertEqual(detail_direct["questions"][1]["options"],
                         [f"选项{EMOJI}甲", f"选项乙\n带换行{EMOJI}"])

        # 列表里的标题同样保留表情与内部换行。
        self.assertEqual(self.listing_titles()[direct["id"]],
                         detail_direct["title"])

    def test_paired_escape_bytes_decode_to_one_emoji(self):
        """😀 必须被当成一个表情，而不是两个代理字符。"""
        payload = {
            "title": f"配对测试{EMOJI}结尾",
            "questions": [{"type": "text", "title": "题目"}],
        }
        _, created = self.api("POST", "/api/surveys", payload,
                              ascii_escapes=True)
        detail = self.get_detail(created["id"])
        title = detail["title"]
        self.assertIn(EMOJI, title)
        # 解出的字符串里不残留任何代理码点。
        self.assertFalse(any(0xD800 <= ord(ch) <= 0xDFFF for ch in title))
        self.assertEqual(title, "配对测试😀结尾")
        self.assertEqual(len(title), len("配对测试😀结尾"))

    def test_edit_keeps_emoji_newlines_and_trimming_rules(self):
        """编辑同样接受表情；直接文字与配对转义两种方式互相覆盖后内容一致。"""
        payload = self.payload_with_emoji()
        created = self.create_ok(payload)
        survey_id = created["id"]
        expected = self.expected_clean(survey_id, payload)
        self.assertEqual(self.get_detail(survey_id), expected)

        # 用配对转义方式 PUT 同一内容：编号不变，读回与直接文字版本一致。
        status, data = self.api("PUT", f"/api/surveys/{survey_id}", payload,
                                ascii_escapes=True)
        self.assertEqual(status, 200, data)
        self.assertEqual(data, expected)
        self.assertEqual(self.get_detail(survey_id), expected)

        # 再换成新内容（直接 UTF-8）：表情出现在说明与选项中，内部换行保留，
        # 同时整份替换的必填规则不被字符校验影响。
        new_payload = {
            "title": f"改过的标题{EMOJI}",
            "description": f"新说明\n第二行{EMOJI}\n第三行",
            "questions": [
                # 省略 required：仍按 false 保存（既有规则）。
                {"type": "single_choice", "title": f"新单选{EMOJI}",
                 "options": [f"新甲{EMOJI}", "新乙"]},
                {"type": "text", "title": f"新文本题\n换行{EMOJI}"},
            ],
        }
        status, data = self.api("PUT", f"/api/surveys/{survey_id}",
                                new_payload, ascii_escapes=False)
        self.assertEqual(status, 200, data)
        detail = self.get_detail(survey_id)
        self.assertEqual(detail["title"], f"改过的标题{EMOJI}")
        self.assertEqual(detail["description"], f"新说明\n第二行{EMOJI}\n第三行")
        self.assertEqual([q["required"] for q in detail["questions"]],
                         [False, False])
        self.assertEqual(detail["questions"][0]["options"],
                         [f"新甲{EMOJI}", "新乙"])
        self.assertEqual(detail["questions"][1]["title"],
                         f"新文本题\n换行{EMOJI}")
        # 成功响应与随后读取的草稿内容一致，且都不含代理码点。
        for source in (data, detail):
            joined = json.dumps(source, ensure_ascii=False)
            self.assertFalse(
                any(0xD800 <= ord(ch) <= 0xDFFF for ch in joined))
            self.assertIn(EMOJI, joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
