#!/usr/bin/env python3
"""问卷编辑页「保存修改」的页面级回归保障。

接口测试（test_survey_replace.py）确认整份替换在服务器上的保存结果；
本文件从用户实际看到的页面与表单内容出发，保护「点击保存修改后、结果
尚未返回时用户已经离开编辑页」这一时序：

- 旧保存随后成功：不能把页面跳回原问卷详情，不能改动新页面上的任何输入
  （首页新建表单、另一份问卷的编辑表单）；
- 旧保存随后返回校验错误或发生网络错误：不能在新页面显示保存失败提示，
  更不能清空、替换新页面的内容；
- 离开后重新进入同一份问卷的编辑页（编号与地址相同）：新打开的表单属于
  本次编辑，旧保存的成功或失败结果都必须被忽略；通过页面链接离开与使用
  浏览器前进/后退切换都遵守这一规则；
- 用户没有离开时行为保持不变：合法保存成功进入该问卷详情并显示保存后的
  内容；校验失败或网络失败停留在编辑页，显示相应原因并保留当前全部修改
  （含已新增/删除的题目与选项），可修改后再提交。

已送出的合法保存允许在服务器完成，本套测试只约束页面状态，不要求因用户
离开而撤销保存或恢复旧草稿。

运行前需要 playwright 与可用的 Chrome/Chromium：

    pip install playwright
    python3 -m unittest test_survey_save_page -v
"""
import json
import time
import unittest

from test_survey_replace import SurveyServer

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - 环境缺少 playwright 时整组跳过
    expect = None
    sync_playwright = None


def read_form_state(page):
    """读取当前问卷表单（新建/编辑共用）的全部用户可见状态。"""
    return page.evaluate(
        """() => ({
            title: document.getElementById("survey-title").value,
            description: document.getElementById("survey-desc").value,
            questions: [...document.querySelectorAll("#questions .q-card")].map(card => ({
                type: card.dataset.type,
                title: card.querySelector(".q-title").value,
                required: card.querySelector(".q-required").checked,
                options: [...card.querySelectorAll(".opt-text")].map(opt => opt.value),
            })),
        })"""
    )


class PendingSaves:
    """拦截 PUT /api/surveys/{id} 保存请求，由测试决定何时、以何种结果放行。"""

    def __init__(self, page):
        self.page = page
        self.pending = []
        page.route("**/api/surveys/*", self._handle)

    def _handle(self, route):
        if route.request.method == "PUT":
            self.pending.append(route)
        else:
            route.continue_()

    def wait_for(self, count=1):
        """等待第 count 个被拦截的保存请求出现，返回对应 route。"""
        deadline = time.time() + 5
        while len(self.pending) < count:
            if time.time() > deadline:
                raise AssertionError(
                    f"等待保存请求超时：期望 {count} 个，实际 {len(self.pending)} 个"
                )
            self.page.wait_for_timeout(25)
        return self.pending[count - 1]


def release_success(route):
    """放行到真实服务器：合法保存正常完成（HTTP 200）。"""
    route.continue_()


def release_invalid(route, message):
    """模拟服务端校验失败（HTTP 400）。"""
    route.fulfill(
        status=400,
        content_type="application/json; charset=utf-8",
        body=json.dumps({"error": message}, ensure_ascii=False),
    )


def release_network_error(route):
    """模拟网络错误：请求根本未能完成。"""
    route.abort("failed")


@unittest.skipUnless(sync_playwright, "需要安装 playwright 才能运行页面级测试")
class SavePageTests(unittest.TestCase):
    server = None
    playwright = None
    browser = None
    page = None

    @classmethod
    def setUpClass(cls):
        cls.server = SurveyServer()
        cls.server.start()
        cls.playwright = sync_playwright().start()
        try:
            try:
                cls.browser = cls.playwright.chromium.launch(
                    channel="chrome", headless=True, args=["--no-sandbox"]
                )
            except Exception:
                cls.browser = cls.playwright.chromium.launch(
                    headless=True, args=["--no-sandbox"]
                )
        except Exception as exc:
            cls.playwright.stop()
            cls.server.stop()
            raise unittest.SkipTest(f"无法启动 Chrome/Chromium：{exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(10_000)

    def tearDown(self):
        self.page.close()

    # ---------- 通用辅助 ----------

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

    def open_home(self):
        self.page.goto(self.server.base_url + "/")
        self.page.wait_for_selector("h2:has-text('新建问卷草稿')")

    def open_edit_via_links(self, survey_id):
        """首页 → 详情 → 编辑，全程通过页面链接进入编辑页。"""
        self.open_home()
        self.page.click(f'a[href="#/surveys/{survey_id}"]')
        self.page.wait_for_selector("h3:has-text('说明')")
        self.page.click(f'a[href="#/surveys/{survey_id}/edit"]')
        self.wait_edit_form(survey_id)

    def wait_edit_form(self, survey_id):
        self.page.wait_for_selector(f"h2:has-text('编辑问卷草稿 #{survey_id}')")

    def wait_detail(self):
        self.page.wait_for_selector("h3:has-text('说明')")

    def go_home_via_links(self, survey_id):
        """编辑页 → 详情 → 首页，全程通过页面链接离开。"""
        self.page.click(f'a[href="#/surveys/{survey_id}"]')
        self.wait_detail()
        self.page.click('a[href="#/"]')
        self.page.wait_for_selector("h2:has-text('新建问卷草稿')")

    def click_save_edit(self):
        self.page.click("button:has-text('保存修改')")

    def add_text_question(self, title, required=False):
        self.page.click("button:has-text('添加文本题')")
        card = self.page.locator(".q-card").last
        card.locator(".q-title").fill(title)
        card.locator(".q-required").set_checked(required)

    def add_choice_question(self, title, options, required=False):
        self.page.click("button:has-text('添加单选题')")
        card = self.page.locator(".q-card").last
        card.locator(".q-title").fill(title)
        card.locator(".q-required").set_checked(required)
        inputs = card.locator(".opt-text")
        for index, value in enumerate(options):
            if index >= inputs.count():
                card.locator(".add-opt").click()
            card.locator(".opt-text").nth(index).fill(value)

    def assert_banner_hidden(self):
        self.assertTrue(
            self.page.locator("#form-banner").is_hidden(),
            "旧保存结果不应在当前页面显示保存失败提示",
        )

    def assert_on_edit_page(self, survey_id):
        expect(
            self.page.locator("h2", has_text=f"编辑问卷草稿 #{survey_id}")
        ).to_be_visible()

    def settle(self):
        """给迟到的保存结果足够时间产生（本不应产生的）页面影响。"""
        self.page.wait_for_timeout(300)

    # ---------- 离开后旧保存结果必须被忽略 ----------

    def test_stale_success_after_leaving_to_home_keeps_create_form(self):
        """保存未返回时回到首页并填写新建表单：旧成功不跳转、不改写新输入。"""
        survey = self.create_survey({
            "title": "原标题",
            "description": "原说明",
            "questions": [{"type": "text", "title": "原题目"}],
        })
        sid = survey["id"]
        self.open_edit_via_links(sid)
        self.page.fill("#survey-title", "离开前改的新标题")

        saves = PendingSaves(self.page)
        self.click_save_edit()
        stale_save = saves.wait_for()

        # 结果尚未返回，用户经详情页回到首页，开始填写新建表单。
        self.go_home_via_links(sid)
        self.page.fill("#survey-title", "首页新问卷")
        self.page.fill("#survey-desc", "首页说明\n第二行")
        self.add_text_question("首页文本题", required=True)
        self.add_choice_question("首页单选题", ["首页甲", "首页乙", "首页丙"])
        expected = read_form_state(self.page)

        # 旧保存此刻才返回成功。
        release_success(stale_save)
        self.settle()

        # 页面必须停留在首页，新建表单内容分毫不动，也不出现失败提示。
        self.assertNotIn("/surveys/", self.page.url)
        expect(self.page.locator("h2", has_text="新建问卷草稿")).to_be_visible()
        self.assert_banner_hidden()
        self.assertEqual(read_form_state(self.page), expected)

        # 已送出的合法保存允许在服务器完成，只是不能影响当前页面。
        self.assertEqual(self.get_detail(sid)["title"], "离开前改的新标题")

    def test_stale_validation_error_after_leaving_keeps_other_edit_form(self):
        """去编辑另一份问卷：旧保存的 400 不提示、不清空、不替换新页面。"""
        survey_a = self.create_survey({
            "title": "问卷A",
            "questions": [{"type": "text", "title": "A的题目"}],
        })
        survey_b = self.create_survey({
            "title": "问卷B",
            "description": "B的说明",
            "questions": [
                {"type": "text", "title": "B文本题", "required": True},
                {"type": "single_choice", "title": "B单选题",
                 "options": ["甲", "乙"]},
            ],
        })
        aid, bid = survey_a["id"], survey_b["id"]

        self.open_edit_via_links(aid)
        self.page.fill("#survey-title", "A改后的标题")
        saves = PendingSaves(self.page)
        self.click_save_edit()
        stale_save = saves.wait_for()

        # 结果未返回，用户经首页进入另一份问卷的编辑页并大量修改：
        # 改标题说明、删题、加题、改必填、改选项、加选项。
        self.go_home_via_links(aid)
        self.page.click(f'a[href="#/surveys/{bid}"]')
        self.wait_detail()
        self.page.click(f'a[href="#/surveys/{bid}/edit"]')
        self.wait_edit_form(bid)

        self.page.fill("#survey-title", "B改后的标题")
        self.page.fill("#survey-desc", "B改后的说明")
        self.page.locator(".q-card").nth(0).get_by_text("删除本题").click()
        self.add_text_question("B新增的文本题", required=True)
        choice = self.page.locator(".q-card").nth(0)
        choice.locator(".q-required").set_checked(True)
        choice.locator(".opt-text").nth(0).fill("甲改")
        choice.locator(".add-opt").click()
        choice.locator(".opt-text").nth(2).fill("丙")
        expected = read_form_state(self.page)

        # 旧保存此刻才返回校验错误。
        release_invalid(stale_save, "第 1 题：题目标题不能为空（或全为空白字符）")
        self.settle()

        # 仍在 B 的编辑页，无失败提示，B 的全部修改保持当前状态。
        self.assert_on_edit_page(bid)
        self.assert_banner_hidden()
        self.assertEqual(read_form_state(self.page), expected)

    def test_stale_network_error_after_leaving_keeps_other_edit_form(self):
        """去编辑另一份问卷：旧保存的网络错误同样不得影响新页面。"""
        survey_a = self.create_survey({
            "title": "问卷A",
            "questions": [{"type": "text", "title": "A的题目"}],
        })
        survey_b = self.create_survey({
            "title": "问卷B",
            "questions": [
                {"type": "single_choice", "title": "B单选题",
                 "required": True, "options": ["红", "蓝"]},
            ],
        })
        aid, bid = survey_a["id"], survey_b["id"]

        self.open_edit_via_links(aid)
        self.page.fill("#survey-title", "A改后的标题")
        saves = PendingSaves(self.page)
        self.click_save_edit()
        stale_save = saves.wait_for()

        self.go_home_via_links(aid)
        self.page.click(f'a[href="#/surveys/{bid}"]')
        self.wait_detail()
        self.page.click(f'a[href="#/surveys/{bid}/edit"]')
        self.wait_edit_form(bid)

        self.page.fill("#survey-title", "B改后的标题")
        card = self.page.locator(".q-card").nth(0)
        card.locator(".q-required").set_checked(False)
        card.locator(".opt-text").nth(1).fill("蓝改")
        card.locator(".add-opt").click()
        card.locator(".opt-text").nth(2).fill("绿")
        expected = read_form_state(self.page)

        # 旧保存此刻才以网络错误告终。
        release_network_error(stale_save)
        self.settle()

        self.assert_on_edit_page(bid)
        self.assert_banner_hidden()
        self.assertEqual(read_form_state(self.page), expected)

    def test_stale_success_ignored_after_reentering_same_survey_edit(self):
        """重新进入同一份问卷的编辑页：旧成功不跳转、不改写本次输入。"""
        survey = self.create_survey({
            "title": "原标题",
            "questions": [{"type": "text", "title": "原题目"}],
        })
        sid = survey["id"]
        self.open_edit_via_links(sid)
        self.page.fill("#survey-title", "第一次输入的标题")

        saves = PendingSaves(self.page)
        self.click_save_edit()
        stale_save = saves.wait_for()

        # 离开后经详情页重新进入同一份问卷的编辑页（编号与地址相同），
        # 这次打开的表单属于本次编辑。
        self.page.click(f'a[href="#/surveys/{sid}"]')
        self.wait_detail()
        self.page.click(f'a[href="#/surveys/{sid}/edit"]')
        self.wait_edit_form(sid)

        self.page.fill("#survey-title", "第二次输入的标题")
        self.page.fill("#survey-desc", "本次编辑补的说明")
        self.add_text_question("本次新增的题", required=True)
        expected = read_form_state(self.page)

        # 上一次保存此刻才返回成功。
        release_success(stale_save)
        self.settle()

        # 不能跳去详情页，不能改写本次输入，也不能追加任何提示。
        self.assert_on_edit_page(sid)
        self.assert_banner_hidden()
        self.assertEqual(read_form_state(self.page), expected)

        # 旧保存本身合法，服务器上允许生效。
        self.assertEqual(self.get_detail(sid)["title"], "第一次输入的标题")

    def test_stale_failure_ignored_after_reentering_same_survey_edit(self):
        """重新进入同一份问卷的编辑页：旧失败不提示、不清空本次输入。"""
        survey = self.create_survey({
            "title": "原标题",
            "questions": [{"type": "single_choice", "title": "原单选",
                           "options": ["一", "二"]}],
        })
        sid = survey["id"]
        self.open_edit_via_links(sid)
        self.page.fill("#survey-title", "第一次输入的标题")

        saves = PendingSaves(self.page)
        self.click_save_edit()
        stale_save = saves.wait_for()

        self.page.click(f'a[href="#/surveys/{sid}"]')
        self.wait_detail()
        self.page.click(f'a[href="#/surveys/{sid}/edit"]')
        self.wait_edit_form(sid)

        self.page.fill("#survey-title", "第二次输入的标题")
        card = self.page.locator(".q-card").nth(0)
        card.locator(".q-required").set_checked(True)
        card.locator(".add-opt").click()
        card.locator(".opt-text").nth(2).fill("三")
        expected = read_form_state(self.page)

        # 上一次保存此刻才返回校验错误。
        release_invalid(stale_save, "第 1 题：选项“一”重复（同一题内选项不得重复）")
        self.settle()

        self.assert_on_edit_page(sid)
        self.assert_banner_hidden()
        self.assertEqual(read_form_state(self.page), expected)

    def test_stale_results_ignored_with_browser_back_and_forward(self):
        """浏览器前进/后退切换页面时，旧保存的成功与失败同样被忽略。"""
        survey = self.create_survey({
            "title": "原标题",
            "questions": [{"type": "text", "title": "原题目"}],
        })
        sid = survey["id"]
        self.open_edit_via_links(sid)
        self.page.fill("#survey-title", "历史切换前的标题")

        saves = PendingSaves(self.page)
        self.click_save_edit()
        stale_success = saves.wait_for(1)
        # 同一表单上再点一次保存，制造第二个迟到的结果。
        self.click_save_edit()
        stale_failure = saves.wait_for(2)

        # 后退两站到首页，再前进两站回到编辑页：每一站都是新一代页面。
        self.page.go_back()
        self.wait_detail()
        self.page.go_back()
        self.page.wait_for_selector("h2:has-text('新建问卷草稿')")
        self.page.go_forward()
        self.wait_detail()
        self.page.go_forward()
        self.wait_edit_form(sid)

        self.page.fill("#survey-title", "前进后退后的输入")
        self.add_choice_question("前进后退后加的题", ["左", "右"], required=True)
        expected = read_form_state(self.page)

        # 两个旧结果一成功一失败，先后返回。
        release_success(stale_success)
        release_invalid(stale_failure, "第 1 题：题目标题不能为空（或全为空白字符）")
        self.settle()

        self.assert_on_edit_page(sid)
        self.assert_banner_hidden()
        self.assertEqual(read_form_state(self.page), expected)
        # 成功的那次保存在服务器上仍然有效。
        self.assertEqual(self.get_detail(sid)["title"], "历史切换前的标题")

    # ---------- 未离开时正常等待保存的行为保持不变 ----------

    def test_save_success_while_staying_goes_to_detail_with_saved_content(self):
        """未离开：合法保存成功进入该问卷详情，并显示保存后的内容。"""
        survey = self.create_survey({
            "title": "留页原标题",
            "description": "留页原说明",
            "questions": [{"type": "text", "title": "留页原题"}],
        })
        sid = survey["id"]
        self.open_edit_via_links(sid)

        self.page.fill("#survey-title", "留页改后标题")
        self.page.fill("#survey-desc", "留页改后说明")
        self.add_choice_question("留页新增单选", ["好", "差"], required=True)
        self.click_save_edit()

        # 进入该问卷详情，地址与内容都是保存后的结果。
        self.wait_detail()
        self.assertTrue(self.page.url.endswith(f"#/surveys/{sid}"))
        expect(self.page.locator("h2", has_text="留页改后标题")).to_be_visible()
        expect(self.page.locator("text=留页改后说明")).to_be_visible()
        expect(self.page.locator("text=留页新增单选")).to_be_visible()
        self.assertEqual(self.get_detail(sid)["title"], "留页改后标题")

    def test_validation_error_while_staying_keeps_form_and_shows_reason(self):
        """未离开：校验失败停留编辑页，显示原因并保留当前全部修改。"""
        survey = self.create_survey({
            "title": "留页原标题",
            "questions": [
                {"type": "text", "title": "待删除的题"},
                {"type": "single_choice", "title": "留页单选",
                 "options": ["猫", "狗"]},
            ],
        })
        sid = survey["id"]
        self.open_edit_via_links(sid)

        # 增删题目、改必填、改选项，形成一批必须被保留的修改。
        self.page.fill("#survey-title", "留页改后标题")
        self.page.locator(".q-card").nth(0).get_by_text("删除本题").click()
        self.add_text_question("留页新增的题", required=True)
        # 删除第 1 题后，原来的单选题成为第 1 张卡片。
        choice = self.page.locator(".q-card").nth(0)
        choice.locator(".q-required").set_checked(True)
        choice.locator(".opt-text").nth(0).fill("猫改")
        choice.locator(".add-opt").click()
        choice.locator(".opt-text").nth(2).fill("鸟")

        saves = PendingSaves(self.page)
        self.click_save_edit()
        save = saves.wait_for()
        expected = read_form_state(self.page)

        release_invalid(save, "第 2 题：选项“猫改”重复（同一题内选项不得重复）")

        # 停留编辑页，banner 展示服务端原因，表单内容（含增删）原样保留。
        self.assert_on_edit_page(sid)
        banner = self.page.locator("#form-banner")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text("第 2 题")
        expect(banner).to_contain_text("猫改")
        self.assertEqual(read_form_state(self.page), expected)

    def test_network_error_while_staying_keeps_form_and_shows_reason(self):
        """未离开：网络失败停留编辑页，提示网络错误并保留当前修改。"""
        survey = self.create_survey({
            "title": "留页原标题",
            "questions": [{"type": "text", "title": "留页原题", "required": True}],
        })
        sid = survey["id"]
        self.open_edit_via_links(sid)

        self.page.fill("#survey-title", "网络失败前的修改")
        self.page.fill("#survey-desc", "网络失败前补的说明")
        self.add_choice_question("网络失败前加的题", ["上", "下"])

        saves = PendingSaves(self.page)
        self.click_save_edit()
        save = saves.wait_for()
        expected = read_form_state(self.page)

        release_network_error(save)

        self.assert_on_edit_page(sid)
        banner = self.page.locator("#form-banner")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text("网络错误")
        self.assertEqual(read_form_state(self.page), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
