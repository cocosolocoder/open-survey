#!/usr/bin/env python3
"""首页问卷列表“加载失败与空列表必须区分”的浏览器回归保障。

test_home_list_stale_read.py 保护的是多次列表读取之间的先后规则；本文件改用
同一套真实 Chrome 夹具，保护“只有成功取得符合格式的问卷列表才能展示记录或
空列表提示”这一判定本身：

- 服务端返回非成功状态时，无论正文是错误 JSON、普通文字、空内容，还是恰好
  带着 surveys 数组，列表区域都必须结束加载、显示“问卷列表加载失败”并注明
  实际 HTTP 状态码，绝不能当成成功结果展示记录或“还没有问卷记录”。
- 成功状态下正文无法解析为 JSON，或 surveys 缺失、不是数组时，同样显示加载
  失败并说明返回内容无法作为问卷列表使用。
- 失败时列表区域不能只剩空白、一直显示“加载中”，或留下可被误认为本次读取
  成功的旧记录；网络中断继续保留原有的失败提示。
- 列表失败只影响列表区域：首页表单的标题、说明、题目、选项、必填设置留在
  原处；草稿已创建成功且等待期间又有修改留在当前表单时，随后的列表刷新
  失败不能把创建改判为失败、不能解除已取得的问卷编号、不能清除“已保存/
  仍有未保存修改”的提示，后续保存仍修改同一份草稿。
- 失败之后通过已有操作重新读取且成功时，正常列表应取代失败提示。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_home_list_load_failure -v
"""
import base64
import json
import unittest

from test_home_list_stale_read import (
    ChromeBrowser,
    SurveyServer,
    EMPTY_HINT,
    FAILURE_HINT,
)

HTTP_FAIL_PREFIX = "问卷列表加载失败（HTTP "
FORMAT_FAIL_HINT = "问卷列表加载失败：返回内容无法作为问卷列表使用。"


def fulfill_raw(page, paused, status, body, content_type="text/plain; charset=utf-8"):
    """让挂起的请求以任意正文与内容类型返回（不接触服务器）。"""
    page.call("Fetch.fulfillRequest", {
        "requestId": paused["requestId"],
        "responseCode": status,
        "responseHeaders": [{"name": "Content-Type", "value": content_type}],
        "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
    })


class HomeListLoadFailureTests(unittest.TestCase):
    browser = None

    @classmethod
    def setUpClass(cls):
        cls.browser = ChromeBrowser()

    @classmethod
    def tearDownClass(cls):
        if cls.browser is not None:
            cls.browser.close()

    def setUp(self):
        self.srv = SurveyServer()
        self.srv.start()
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.stop_holding()
        self.page.close()
        self.srv.stop()

    # ---------- 夹具与断言辅助 ----------

    def api(self, method, path, body=None):
        return self.srv.request(method, path, body)

    def open_home_held(self):
        """打开首页并挂起首次列表读取，返回被挂起的读取。"""
        self.page.hold_reads()
        self.page.open(f"{self.srv.base_url}/#/")
        self.page.wait_for("!!document.querySelector('#draft-form')")
        initial = self.page.held_list(0)
        self.assertIn("加载中", self.page.listing()["text"])
        return initial

    def assert_list_failure(self, expected, where):
        """列表区域只显示失败提示：不空白、不加载中、不留旧记录、无空列表提示。"""
        listing = self.page.listing()
        self.assertTrue(listing["present"], f"{where}：列表区域不见了")
        self.assertEqual(listing["items"], [expected],
                         f"{where}：列表区域不是唯一的失败提示")
        self.assertNotIn("加载中", listing["text"], f"{where}：列表一直显示加载中")
        self.assertNotIn(EMPTY_HINT, listing["text"], f"{where}：失败被当成空列表")
        self.assertEqual(listing["hrefs"], [], f"{where}：列表里留下了旧记录链接")

    def assert_form_untouched(self, where, title, description,
                              expected_questions, heading=None):
        """列表读取的任何结果都不得触碰表单区域。"""
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", f"{where}：页面离开了首页")
        if heading is not None:
            self.assertEqual(snap["formHeading"], heading,
                             f"{where}：表单标题（编号）被改写")
        self.assertEqual(snap["title"], title, f"{where}：标题被清空或改写")
        self.assertEqual(snap["description"], description,
                         f"{where}：说明被清空或改写")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snap["questions"]],
            expected_questions,
            f"{where}：题目/选项/必填设置被改写")
        self.assertFalse(snap["bannerVisible"], f"{where}：表单冒出了错误提示条")
        return snap

    # ---------- 非成功状态：无论正文是什么都按失败处理 ----------

    def test_http_error_with_error_json_shows_status_code(self):
        """首次读取返回 500 与错误 JSON：显示失败与状态码，不是空列表提示。"""
        initial = self.open_home_held()
        self.page.fulfill_json(initial, 500, {"error": "保存失败：disk full"})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{HTTP_FAIL_PREFIX}500')")
        self.assert_list_failure("问卷列表加载失败（HTTP 500）。", "500 错误 JSON 后")

    def test_http_error_with_surveys_array_is_not_success(self):
        """错误状态但正文恰好带 surveys 数组：不能当成成功结果展示记录。"""
        initial = self.open_home_held()
        self.page.fulfill_json(initial, 503,
                               {"surveys": [{"id": 1, "title": "伪造的问卷"}]})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{HTTP_FAIL_PREFIX}503')")
        listing = self.page.listing()
        self.assert_list_failure("问卷列表加载失败（HTTP 503）。", "503 带 surveys 后")
        self.assertNotIn("伪造的问卷", listing["text"],
                         "错误状态里的 surveys 数组被当成真实记录展示")

    def test_http_error_with_plain_text_body(self):
        """错误状态配普通文字正文：同样按状态码提示失败。"""
        initial = self.open_home_held()
        fulfill_raw(self.page, initial, 502, "Bad Gateway")
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{HTTP_FAIL_PREFIX}502')")
        self.assert_list_failure("问卷列表加载失败（HTTP 502）。", "502 普通文字后")

    def test_http_error_with_empty_body(self):
        """错误状态配空正文：同样按状态码提示失败，不卡在加载中。"""
        initial = self.open_home_held()
        fulfill_raw(self.page, initial, 500, "")
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{HTTP_FAIL_PREFIX}500')")
        self.assert_list_failure("问卷列表加载失败（HTTP 500）。", "500 空正文后")

    # ---------- 成功状态但正文无法作为问卷列表 ----------

    def test_ok_with_unparseable_body_shows_format_failure(self):
        """200 但正文不是合法 JSON：说明返回内容无法作为问卷列表使用。"""
        initial = self.open_home_held()
        fulfill_raw(self.page, initial, 200, "这不是 JSON",
                    content_type="application/json; charset=utf-8")
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{FORMAT_FAIL_HINT}')")
        self.assert_list_failure(FORMAT_FAIL_HINT, "200 非 JSON 正文后")

    def test_ok_with_missing_surveys_shows_format_failure(self):
        """200 且是 JSON 但缺少 surveys：按加载失败处理，不是空列表。"""
        initial = self.open_home_held()
        self.page.fulfill_json(initial, 200, {"status": "ok"})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{FORMAT_FAIL_HINT}')")
        self.assert_list_failure(FORMAT_FAIL_HINT, "200 缺 surveys 后")

    def test_ok_with_non_array_surveys_shows_format_failure(self):
        """200 且 surveys 不是数组：按加载失败处理，不展示任何记录。"""
        initial = self.open_home_held()
        self.page.fulfill_json(initial, 200, {"surveys": {"id": 1, "title": "不是数组"}})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{FORMAT_FAIL_HINT}')")
        listing = self.page.listing()
        self.assert_list_failure(FORMAT_FAIL_HINT, "200 surveys 非数组后")
        self.assertNotIn("不是数组", listing["text"])

    # ---------- 失败时不能留下旧记录；成功后正常列表取代失败提示 ----------

    def test_refresh_failure_clears_previous_records(self):
        """已有记录的列表在刷新失败时只显示失败提示，旧记录不得残留。"""
        status, seed = self.api("POST", "/api/surveys", {
            "title": "已有问卷",
            "questions": [{"type": "text", "title": "已有题目"}],
        })
        self.assertEqual(status, 201)
        seed_id = seed["id"]

        initial = self.open_home_held()
        self.page.release_to_server(initial)
        self.page.wait_for(
            "document.querySelectorAll('#survey-list a[href]').length === 1")
        self.assertEqual(self.page.listing()["items"], [f"#{seed_id} 已有问卷"])

        # 创建草稿成功触发刷新读取；等待期间改一下标题，使创建成功后留在首页，
        # 再让这次刷新返回 500。
        self.page.t("setTitle", "新草稿")
        self.page.t("addText", "文本题一", False)
        self.page.t("submit")
        post = self.page.pop_held_post()
        self.page.t("setTitle", "等待期间改的新标题")
        self.page.release_to_server(post)
        refresh = self.page.held_list(1)
        self.page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent.startsWith('编辑问卷草稿 #')")
        self.page.fulfill_json(refresh, 500, {"error": "列表读取失败"})

        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{HTTP_FAIL_PREFIX}500')")
        listing = self.page.listing()
        self.assert_list_failure("问卷列表加载失败（HTTP 500）。", "刷新 500 后")
        self.assertNotIn("已有问卷", listing["text"],
                         "刷新失败后旧记录仍留在列表里，会被误认为本次读取成功")

    def test_successful_reload_replaces_failure_hint(self):
        """列表失败之后，创建成功触发的重新读取成功时正常列表取代失败提示。"""
        initial = self.open_home_held()
        self.page.fulfill_json(initial, 500, {"error": "首次读取失败"})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{HTTP_FAIL_PREFIX}500')")
        self.assert_list_failure("问卷列表加载失败（HTTP 500）。", "首次 500 后")

        # 列表失败不影响表单：照常填写并创建草稿。等待期间改一下标题，使创建
        # 成功后留在首页，才能观察到刷新成功取代失败提示。
        self.page.t("setTitle", "失败后创建的问卷")
        self.page.t("addText", "文本题一", True)
        self.page.t("submit")
        post = self.page.pop_held_post()
        self.page.t("setTitle", "等待期间改的新标题")
        self.page.release_to_server(post)
        refresh = self.page.held_list(1)
        self.page.release_to_server(refresh)

        self.page.wait_for(
            "document.querySelectorAll('#survey-list a[href]').length === 1")
        listing = self.page.listing()
        self.assertEqual(listing["items"], ["#1 失败后创建的问卷"],
                         "重新读取成功后应显示真实列表")
        self.assertNotIn("问卷列表加载失败", listing["text"],
                         "失败提示没有被正常列表取代")

    # ---------- 创建成功后的刷新失败：创建不得被改判为失败 ----------

    def test_refresh_failure_after_create_keeps_draft_and_save_state(self):
        """草稿已创建成功且等待期间有修改：刷新失败只影响列表区域。

        列表刷新返回 500 后：表单保持“编辑刚创建的草稿”、编号不变、已保存与
        未保存提示都在、全部输入保留；随后再次保存仍 PUT 同一编号并进入详情。
        """
        initial = self.open_home_held()
        self.page.fulfill_json(initial, 200, {"surveys": []})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes('{EMPTY_HINT}')")

        # 填写并提交草稿；等待保存结果期间继续修改，使创建成功后留在当前表单。
        self.page.t("setTitle", "创建时保存的标题")
        self.page.t("setDesc", "创建时保存的说明")
        self.page.t("addText", "文本题一", True)
        self.page.t("addChoice", "单选题一", ["选项甲", "选项乙"], False)
        self.page.t("submit")
        post = self.page.pop_held_post()
        self.page.t("setTitle", "等待期间改的新标题")
        self.page.t("setDesc", "等待期间改的新说明")
        self.page.release_to_server(post)

        refresh = self.page.held_list(1)
        self.page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent === '编辑问卷草稿 #1'")
        snap = self.page.snapshot()
        self.assertIn("已保存", snap["saveStatusText"])
        self.assertIn("未保存的修改", snap["saveStatusText"])

        # 创建成功后的列表刷新返回 500：只影响列表区域。
        self.page.fulfill_json(refresh, 500, {"error": "刷新失败"})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{HTTP_FAIL_PREFIX}500')")
        self.assert_list_failure("问卷列表加载失败（HTTP 500）。", "刷新 500 后")

        # 创建不能被改判为失败：编号、输入与保存状态提示全部保留。
        snap = self.assert_form_untouched(
            "刷新 500 后",
            title="等待期间改的新标题",
            description="等待期间改的新说明",
            expected_questions=[
                ("text", "文本题一", True, []),
                ("single_choice", "单选题一", False, ["选项甲", "选项乙"]),
            ],
            heading="编辑问卷草稿 #1")
        self.assertIn("已保存", snap["saveStatusText"],
                      "刷新失败后“已保存”提示被清除")
        self.assertIn("未保存的修改", snap["saveStatusText"],
                      "刷新失败后“未保存的修改”提示被清除")

        # 后续保存仍修改同一份草稿（PUT 已取得的编号），不是重新创建。
        self.page.t("submit")
        self.page.wait_for("location.hash === '#/surveys/1'")
        status, listing = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in listing["surveys"]], [1],
                         "再次保存创建出了第二份问卷，说明编号被解除")
        status, detail = self.api("GET", "/api/surveys/1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["title"], "等待期间改的新标题",
                         "再次保存没有落到已创建的草稿上")

    def test_refresh_network_error_keeps_existing_hint(self):
        """创建后的刷新网络中断：保留现有失败提示（不带状态码），表单不受影响。"""
        initial = self.open_home_held()
        self.page.fulfill_json(initial, 200, {"surveys": []})
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes('{EMPTY_HINT}')")

        # 等待保存结果期间修改标题，使创建成功后留在当前表单继续观察列表。
        self.page.t("setTitle", "网络错误场景的草稿")
        self.page.t("addText", "文本题一", False)
        self.page.t("submit")
        post = self.page.pop_held_post()
        self.page.t("setTitle", "等待期间改的新标题")
        self.page.release_to_server(post)
        refresh = self.page.held_list(1)
        self.page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent === '编辑问卷草稿 #1'")

        self.page.fail_as_network_error(refresh)
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes("
            f"'{FAILURE_HINT}')")
        # 网络中断保留现有提示文案：没有状态码，也不是“无法作为问卷列表”。
        self.assert_list_failure(FAILURE_HINT, "刷新网络错误后")
        self.assert_form_untouched(
            "刷新网络错误后",
            title="等待期间改的新标题",
            description="",
            expected_questions=[("text", "文本题一", False, [])],
            heading="编辑问卷草稿 #1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
