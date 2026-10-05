#!/usr/bin/env python3
"""编辑问卷草稿“点击保存之后仍继续编辑”的浏览器回归保障。

test_edit_save_stale_response.py 保障的是保存结果不得**跨页面**生效（用户在等待
期间离开了编辑页）；本文件保护用户**一直停留在同一份问卷编辑页**的情况：

- 点击“保存修改”后、结果尚未返回时：页面明确显示正在保存、保存按钮不可重复
  提交，但标题、说明、题目标题、选项、必填勾选与题目/选项增删全部仍可编辑。
- 第一次保存成功返回后：服务器上的草稿只有第一次点击那一刻提交的内容与顺序；
  页面停留在编辑页，保留等待期间的完整当前内容（新增/删除的题目、选项不被
  旧响应还原，当前文字不被先前提交的文字覆盖），并同时清楚显示“刚才提交的
  内容已保存”与“当前还有未保存的修改”。第一笔成功不会自动追加第二笔保存，
  也不会为了消除未保存提示而撤销第一笔结果。
- 保存恢复可用后用户主动再次保存：提交的是此时页面里的内容；成功且等待期间
  没有再改动时进入同一问卷的详情，详情与重新读取的草稿一致，问卷编号不变。
- 边界：等待期间改过内容、但在响应返回前全部恢复成该次提交的内容，按没有新
  修改处理，正常进入详情。比较遵循现有保存规则：标题、题目标题、选项的首尾
  空白不算修改，内部换行算修改；说明原样保存，说明的空白变化也算修改；只改
  一个必填勾选、只增删一个选项都属于真实修改。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把编辑保存的
PUT 挂起，等用户在等待期间完成第二批编辑后，再放行请求真实到达服务器。断言
全部基于用户可见的 DOM、表单值，以及再次读取接口得到的实际保存结果。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_edit_save_pending_edits -v
"""
import json
import shutil
import time
import unittest
from pathlib import Path

# 复用跨页面迟到响应保障中的同款夹具：真实 app.py 服务进程、真实 Chrome、
# CDP 请求挂起/放行与页面操作手柄（window.__t.*）。
from test_edit_save_stale_response import (
    CHROME,
    ChromeBrowser,
    SurveyServer,
)


# 保存状态条与保存按钮的可见状态——全部来自用户可见的 DOM。
STATUS_JS = r"""
(() => {
  const bar = document.getElementById('save-status');
  const visible = id => {
    const el = document.getElementById(id);
    return !!el && !el.hidden && !!bar && !bar.hidden;
  };
  const submit = document.querySelector('#draft-form button[type=submit]');
  return {
    barVisible: !!bar && !bar.hidden,
    saving: visible('save-note-saving'),
    saved: visible('save-note-saved'),
    dirty: visible('save-note-dirty'),
    submitDisabled: submit ? submit.disabled : null,
    submitLabel: submit ? submit.textContent.trim() : null,
  };
})()
"""


class PendingEditSaveTests(unittest.TestCase):
    server = None
    browser = None

    @classmethod
    def setUpClass(cls):
        if shutil.which(CHROME) is None and not Path(CHROME).exists():
            raise unittest.SkipTest(
                f"未找到浏览器（{CHROME}），跳过需要真实浏览器的回归测试；"
                "可用环境变量 CHROME_BIN 指定可执行文件路径。")
        cls.server = SurveyServer()
        cls.server.start()
        cls.browser = ChromeBrowser()

    @classmethod
    def tearDownClass(cls):
        if cls.browser is not None:
            cls.browser.close()
        if cls.server is not None:
            cls.server.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.stop_holding()
        self.page.close()

    # ---------- 夹具与断言辅助 ----------

    def api(self, method, path, body=None):
        return self.server.request(method, path, body)

    def seed_survey(self, title="旧标题", description="旧说明第一行\n第二行：\"引号\" 与中文"):
        """准备一份已有草稿：说明含中文、引号与换行；一道文本题、一道三选项单选题。"""
        status, data = self.api("POST", "/api/surveys", {
            "title": title,
            "description": description,
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        })
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data["id"]

    def open_edit(self, survey_id, expected_title="旧标题"):
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}/edit")
        self.page.wait_for(
            "!!document.querySelector('#draft-form .q-card') && "
            f"document.getElementById('survey-title').value === {json.dumps(expected_title)}")

    def wait_held_puts(self, count, timeout=8):
        """等待第 count 笔被挂起的 PUT（第一笔由 wait_held_put 取出后仍在队列中）。"""
        end = time.time() + timeout
        with self.page._held_cond:
            while len(self.page.held) < count and time.time() < end:
                self.page._held_cond.wait(max(0.0, end - time.time()))
            if len(self.page.held) >= count:
                return self.page.held[count - 1]
        raise AssertionError(f"第 {count} 笔保存请求未发出（PUT 未被挂起）")

    def save_status(self):
        return self.page.eval(STATUS_JS)

    def assert_form_state(self, snapshot, expected, where):
        self.assertEqual(snapshot["title"], expected["title"], f"{where}：标题被改写")
        self.assertEqual(snapshot["description"], expected["description"],
                         f"{where}：说明被改写")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snapshot["questions"]],
            [(q["type"], q["title"], q["required"], q["options"])
             for q in expected["questions"]],
            f"{where}：题目/选项/必填状态被旧保存结果改写")
        self.assertFalse(snapshot["bannerVisible"], f"{where}：表单冒出了错误提示条")

    def assert_server_state(self, survey_id, expected):
        """重新读取接口，核对服务器上的草稿与期望完全一致（含顺序与编号）。"""
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {"id": survey_id, **expected},
                         "服务器上的草稿与期望不一致")

    def assert_stayed_dirty(self, survey_id, expected_page, expected_server, where):
        """第一次保存成功但等待期间有新修改后的共同断言：

        停留在编辑页；页面保留当前完整内容；同时显示“已保存”与“未保存修改”；
        保存按钮恢复可用；没有自动追加第二笔保存；服务器只有第一次提交的内容。
        """
        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit",
                         f"{where}：等待期间有新修改时不应离开编辑页")
        self.assert_form_state(snapshot, expected_page, where)
        status = self.save_status()
        self.assertTrue(status["saved"], f"{where}：缺少“刚才提交的内容已保存”提示")
        self.assertTrue(status["dirty"], f"{where}：缺少“当前还有未保存的修改”提示")
        self.assertFalse(status["saving"], f"{where}：保存已结束，不应仍显示正在保存")
        self.assertFalse(status["submitDisabled"], f"{where}：保存按钮未恢复可用")
        self.assertEqual(status["submitLabel"], "保存修改",
                         f"{where}：保存按钮未恢复原有文案")
        self.assertEqual(len(self.page.held), 1,
                         f"{where}：第一笔保存的成功自动追加了第二笔保存")
        self.assert_server_state(survey_id, expected_server)

    # ---------- 等待期间继续编辑，第一次保存成功后 ----------

    def test_edits_made_while_save_pending_survive_success(self):
        """等待保存结果期间的全部编辑保留在页面上；服务器只有第一次提交的内容。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()

        # 第一批修改：点击“保存修改”那一刻提交的内容。
        page.t("setTitle", "第一次提交的标题")
        page.t("setDesc", "第一次说明\n含 \"引号\" 与中文")
        page.t("setQTitle", 0, "第一次改文本题")
        page.t("setOption", 1, 0, "选项甲改")
        page.t("submit")
        paused = page.wait_held_put()

        # 等待期间：明确显示正在保存、保存按钮不可重复提交，但表单仍可编辑。
        status = self.save_status()
        self.assertTrue(status["saving"], "等待保存结果期间缺少“正在保存”提示")
        self.assertTrue(status["submitDisabled"], "等待期间保存按钮仍可重复提交")
        self.assertEqual(status["submitLabel"], "正在保存…")

        # 第二批修改（等待期间）：改标题/说明/题目，增删题目与选项，调整必填。
        page.t("setTitle", "等待期间改的标题")
        page.t("setDesc", "等待期间的说明\n第二行")
        page.t("setQTitle", 0, "等待期改文本题")
        page.t("setRequired", 0, True)
        page.t("removeOption", 1, 2)      # 删掉“选项丙”
        page.t("addOption", 1, "选项丁")
        page.t("addText", "等待期新增文本题", False)
        page.t("addChoice", "等待期新增单选题", ["新选项一", "新选项二"], True)

        expected_page = {
            "title": "等待期间改的标题",
            "description": "等待期间的说明\n第二行",
            "questions": [
                {"type": "text", "title": "等待期改文本题", "required": True,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲改", "选项乙", "选项丁"]},
                {"type": "text", "title": "等待期新增文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "等待期新增单选题", "required": True,
                 "options": ["新选项一", "新选项二"]},
            ],
        }
        self.assert_form_state(page.snapshot(), expected_page, "响应返回前")

        # 第一次保存成功返回。
        page.release_to_server(paused)
        page.settle(0.8)

        self.assert_stayed_dirty(survey_id, expected_page, {
            "title": "第一次提交的标题",
            "description": "第一次说明\n含 \"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "第一次改文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲改", "选项乙", "选项丙"]},
            ],
        }, "第一次保存成功后")
        # 再观察一拍：新增/删除的题目与选项不被旧响应还原，当前文字不被覆盖。
        page.settle(0.4)
        self.assert_form_state(page.snapshot(), expected_page, "第一次保存成功后再次观察")

    def test_second_save_submits_current_content_and_enters_detail(self):
        """再次保存提交的是当前页面内容；无新改动时进入详情，详情与草稿一致。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()

        page.t("setTitle", "第一笔标题")
        page.t("submit")
        paused = page.wait_held_put()

        # 等待期间的修改：属于第二笔内容，不会被第一笔成功自动补交。
        page.t("setTitle", "第二笔标题")
        page.t("setDesc", "第二笔说明\n第二行 \"引号\"")
        page.t("addText", "第二笔新增题", True)
        page.release_to_server(paused)
        page.settle(0.8)
        self.assertEqual(len(page.held), 1, "第一笔保存的成功自动追加了第二笔保存")

        # 保存恢复可用后，用户主动再次保存。
        page.t("submit")
        paused_second = self.wait_held_puts(2)

        # 第二笔请求体必须是此时页面里的内容，而不是沿用第一笔的旧内容。
        post_data = paused_second["request"].get("postData")
        if post_data is not None:
            self.assertEqual(json.loads(post_data), {
                "title": "第二笔标题",
                "description": "第二笔说明\n第二行 \"引号\"",
                "questions": [
                    {"type": "text", "title": "保留文本题", "required": False},
                    {"type": "single_choice", "title": "原单选题", "required": True,
                     "options": ["选项甲", "选项乙", "选项丙"]},
                    {"type": "text", "title": "第二笔新增题", "required": True},
                ],
            }, "第二笔保存提交的应是当前页面内容")

        # 第二笔等待期间没有再改动；放行前服务器仍只有第一笔的内容。
        self.assert_server_state(survey_id, {
            "title": "第一笔标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        })

        page.release_to_server(paused_second)
        page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        page.wait_for("!!document.querySelector('.q-list')")

        # 进入同一问卷的详情：编号不变，详情与重新读取的草稿一致。
        detail = page.detail()
        self.assertEqual(detail["heading"], f"#{survey_id} 第二笔标题")
        self.assertEqual(detail["description"], "第二笔说明\n第二行 \"引号\"")
        self.assertEqual(len(detail["lines"]), 3)
        self.assertIn("保留文本题", detail["lines"][0])
        self.assertIn("原单选题", detail["lines"][1])
        self.assertIn("选项丙", detail["lines"][1])
        self.assertIn("第二笔新增题", detail["lines"][2])
        self.assertIn("必填", detail["lines"][2])
        self.assertFalse(detail["anyVisibleBanner"])
        self.assert_server_state(survey_id, {
            "title": "第二笔标题",
            "description": "第二笔说明\n第二行 \"引号\"",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
                {"type": "text", "title": "第二笔新增题", "required": True,
                 "options": []},
            ],
        })

    # ---------- 边界：等待期间改过又全部恢复 ----------

    def test_edits_reverted_before_response_are_not_new_changes(self):
        """等待期间改过内容、响应返回前全部恢复成提交内容：按无新修改进入详情。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()

        page.t("setTitle", "还原边界标题")
        page.t("setDesc", "还原边界说明")
        page.t("submit")
        paused = page.wait_held_put()

        # 等待期间的临时改动，在响应返回前逐一恢复成该次提交的内容。
        page.t("setTitle", "等待期临时标题")
        page.t("setTitle", "还原边界标题")
        page.t("setDesc", "临时说明")
        page.t("setDesc", "还原边界说明")
        page.t("setRequired", 0, True)
        page.t("setRequired", 0, False)
        page.t("addOption", 1, "临时选项")
        page.t("removeOption", 1, 3)        # 删掉刚加的“临时选项”
        page.t("addText", "临时题", True)
        page.t("removeQuestion", 2)         # 删掉刚加的临时题

        page.release_to_server(paused)
        page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        page.wait_for("!!document.querySelector('.q-list')")

        detail = page.detail()
        self.assertEqual(detail["heading"], f"#{survey_id} 还原边界标题")
        self.assertEqual(detail["description"], "还原边界说明")
        self.assertEqual(len(detail["lines"]), 2)
        self.assert_server_state(survey_id, {
            "title": "还原边界标题",
            "description": "还原边界说明",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        })

    # ---------- 修改比较规则 ----------

    def test_surrounding_whitespace_changes_are_not_modifications(self):
        """标题、题目标题、选项的首尾空白变化不算修改：保存成功正常进入详情。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("submit")                    # 原样保存当前内容
        paused = page.wait_held_put()

        page.t("setTitle", "  旧标题  ")
        page.t("setQTitle", 0, " 保留文本题 ")
        page.t("setOption", 1, 0, "  选项甲 ")

        page.release_to_server(paused)
        page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        page.wait_for("!!document.querySelector('.q-list')")
        self.assertEqual(page.detail()["heading"], f"#{survey_id} 旧标题")
        self.assert_server_state(survey_id, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        })

    def test_internal_newline_in_title_is_a_real_modification(self):
        """标题内部的换行有意义：等待期间插入内部换行属于真实修改。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("submit")
        paused = page.wait_held_put()

        page.t("setTitle", "旧\n标题")
        page.release_to_server(paused)
        page.settle(0.8)

        self.assert_stayed_dirty(survey_id, {
            "title": "旧\n标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, "标题内部换行")

    def test_description_whitespace_change_is_a_real_modification(self):
        """说明原样保存：等待期间说明的空白变化（尾部换行）属于真实修改。"""
        survey_id = self.seed_survey(description="说明原文")
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("submit")
        paused = page.wait_held_put()

        page.t("setDesc", "说明原文\n")
        page.release_to_server(paused)
        page.settle(0.8)

        self.assert_stayed_dirty(survey_id, {
            "title": "旧标题",
            "description": "说明原文\n",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, {
            "title": "旧标题",
            "description": "说明原文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, "说明空白变化")

    def test_required_toggle_alone_is_a_real_modification(self):
        """只改一个必填勾选也属于真实修改，不能只比较问卷标题。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("submit")
        paused = page.wait_held_put()

        page.t("setRequired", 0, True)      # 文本题由选填改为必填
        page.release_to_server(paused)
        page.settle(0.8)

        self.assert_stayed_dirty(survey_id, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": True,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, "只改必填勾选")

    def test_option_added_alone_is_a_real_modification(self):
        """只新增一个选项也属于真实修改。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("submit")
        paused = page.wait_held_put()

        page.t("addOption", 1, "选项丁")
        page.release_to_server(paused)
        page.settle(0.8)

        self.assert_stayed_dirty(survey_id, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙", "选项丁"]},
            ],
        }, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, "只新增一个选项")

    def test_option_removed_alone_is_a_real_modification(self):
        """只删除一个选项也属于真实修改。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("submit")
        paused = page.wait_held_put()

        page.t("removeOption", 1, 2)        # 删掉“选项丙”
        page.release_to_server(paused)
        page.settle(0.8)

        self.assert_stayed_dirty(survey_id, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙"]},
            ],
        }, {
            "title": "旧标题",
            "description": "旧说明第一行\n第二行：\"引号\" 与中文",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False,
                 "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, "只删除一个选项")


if __name__ == "__main__":
    unittest.main(verbosity=2)
