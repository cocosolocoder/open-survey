#!/usr/bin/env python3
"""编辑页“等待保存期间又有新修改”的浏览器回归保障。

与 test_edit_save_stale_response.py（离开后旧结果必须忽略）互补：本文件覆盖
用户留在同一编辑页继续输入的场景——

- 保存成功返回时表单已被改动：停留在编辑页，完整保留当前内容与顺序，
  提示条明确区分“刚才提交的已保存”与“当前还有未保存的修改”；
  服务器上仍是实际提交的内容，不自动补交；用户可不退出页面直接再次保存。
- 等待期间改过又恢复成提交时的内容：按原功能进入详情。
- 保存失败（校验/网络）返回时：保留失败到达时的最新表单，不误称已保存。

运行方式：python3 -m unittest test_edit_save_during_wait -v
"""
import time
import unittest

from test_edit_save_stale_response import (
    ChromeBrowser, SurveyServer, ERROR_PHRASES,
)


class SaveWhileEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SurveyServer()
        cls.server.start()
        cls.browser = ChromeBrowser()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.server.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.stop_holding()
        self.page.close()

    def seed(self):
        status, data = self.server.request("POST", "/api/surveys", {
            "title": "旧标题", "description": "旧说明",
            "questions": [
                {"type": "text", "title": "保留文本题"},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ]})
        assert status == 201, data
        return data["id"]

    def open_edit(self, sid):
        self.page.open(f"{self.server.base_url}/#/surveys/{sid}/edit")
        self.page.wait_for(
            "!!document.querySelector('#draft-form .q-card') && "
            "document.getElementById('survey-title').value !== ''")

    def test_edit_during_wait_stays_with_notice_and_keeps_form(self):
        sid = self.seed()
        self.open_edit(sid)
        page = self.page
        page.hold_puts()
        page.t("setTitle", "第一次提交的标题")
        page.t("submit")
        paused = page.wait_held_put()

        # 等待期间继续修改：标题、说明、题目标题、必填、增删题目与选项
        page.t("setTitle", "等待期间又改的标题")
        page.t("setDesc", "等待期间的说明\n第二行")
        page.t("setQTitle", 0, "等待期间改的文本题")
        page.t("setRequired", 0, True)
        page.t("removeOption", 1, 2)
        page.t("addOption", 1, "选项丁")
        page.t("addText", "等待期间新增题", False)

        page.release_to_server(paused)
        page.wait_for("!document.getElementById('form-notice').hidden")
        page.settle(0.5)

        snap = page.snapshot()
        # 停留在编辑页，表单保留等待期间的完整内容与顺序
        self.assertEqual(snap["hash"], f"#/surveys/{sid}/edit")
        self.assertEqual(snap["title"], "等待期间又改的标题")
        self.assertEqual(snap["description"], "等待期间的说明\n第二行")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"]) for q in snap["questions"]],
            [("text", "等待期间改的文本题", True, []),
             ("single_choice", "原单选题", True, ["选项甲", "选项乙", "选项丁"]),
             ("text", "等待期间新增题", False, [])])
        # 提示区分“已保存”与“未保存”，且无失败措辞、无错误 banner
        notice = page.eval("document.getElementById('form-notice').textContent")
        self.assertIn("已保存", notice)
        self.assertIn("未保存", notice)
        for phrase in ERROR_PHRASES:
            self.assertNotIn(phrase, notice)
        self.assertFalse(snap["bannerVisible"])
        # 服务器上是第一次提交的内容，未被等待期间的修改污染
        status, saved = self.server.request("GET", f"/api/surveys/{sid}")
        self.assertEqual(saved["title"], "第一次提交的标题")
        self.assertEqual(saved["description"], "旧说明")
        self.assertEqual([q["title"] for q in saved["questions"]],
                         ["保留文本题", "原单选题"])

        # 不退出页面，直接再次保存当前内容：成功后进入详情，编号不变
        page.stop_holding()
        page.t("submit")
        page.wait_for(f"location.hash === '#/surveys/{sid}' && !!document.querySelector('.q-list')")
        detail = page.detail()
        self.assertEqual(detail["heading"], f"#{sid} 等待期间又改的标题")
        self.assertEqual(detail["description"], "等待期间的说明\n第二行")
        body = detail["bodyText"]
        for text in ("等待期间改的文本题", "等待期间新增题", "选项丁"):
            self.assertIn(text, body)
        self.assertNotIn("选项丙", body)
        status, saved = self.server.request("GET", f"/api/surveys/{sid}")
        self.assertEqual(saved["title"], "等待期间又改的标题")
        self.assertEqual([q["title"] for q in saved["questions"]],
                         ["等待期间改的文本题", "原单选题", "等待期间新增题"])
        self.assertEqual(saved["questions"][1]["options"], ["选项甲", "选项乙", "选项丁"])

    def test_revert_to_submitted_content_navigates_to_detail(self):
        sid = self.seed()
        self.open_edit(sid)
        page = self.page
        page.hold_puts()
        page.t("setTitle", "提交时的标题")
        page.t("submit")
        paused = page.wait_held_put()
        # 等待期间改了又改回提交时的内容
        page.t("setTitle", "等待期间临时改动")
        page.t("setDesc", "临时说明")
        page.t("setTitle", "提交时的标题")
        page.t("setDesc", "旧说明")
        page.release_to_server(paused)
        page.wait_for(f"location.hash === '#/surveys/{sid}' && !!document.querySelector('.q-list')")
        detail = page.detail()
        self.assertEqual(detail["heading"], f"#{sid} 提交时的标题")
        self.assertFalse(detail["anyVisibleBanner"])

    def test_failure_during_wait_keeps_latest_form(self):
        sid = self.seed()
        before = self.server.request("GET", f"/api/surveys/{sid}")[1]
        self.open_edit(sid)
        page = self.page
        page.hold_puts()
        page.t("setTitle", "提交时的标题")
        page.t("submit")
        paused = page.wait_held_put()
        # 等待期间继续修改
        page.t("setTitle", "失败时的最新标题")
        page.t("removeQuestion", 0)
        page.fulfill_json(paused, 400, {"error": "第 1 题：模拟校验错误"})
        page.wait_for("!document.getElementById('form-banner').hidden")
        snap = page.snapshot()
        self.assertEqual(snap["hash"], f"#/surveys/{sid}/edit")
        self.assertEqual(snap["title"], "失败时的最新标题")
        self.assertEqual([q["title"] for q in snap["questions"]], ["原单选题"])
        self.assertIn("模拟校验错误", snap["bannerText"])
        # 不出现“已保存”提示
        self.assertTrue(page.eval("document.getElementById('form-notice').hidden"))
        self.assertEqual(self.server.request("GET", f"/api/surveys/{sid}")[1], before)
        # 修正后可正常保存成功并进入详情
        page.stop_holding()
        page.t("submit")
        page.wait_for(f"location.hash === '#/surveys/{sid}' && !!document.querySelector('.q-list')")
        self.assertEqual(page.detail()["heading"], f"#{sid} 失败时的最新标题")


if __name__ == "__main__":
    unittest.main(verbosity=2)
