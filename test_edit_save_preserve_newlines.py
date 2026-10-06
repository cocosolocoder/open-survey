#!/usr/bin/env python3
"""编辑问卷草稿“保存多行文字时原稿换行不被悄悄改写”的浏览器回归保障。

接口（POST/PUT /api/surveys）原样保留文本内部的回车换行（CRLF，\\r\\n）、
单独回车（CR，\\r）与换行（LF，\\n）；问卷标题、说明、题目标题、单选选项都可能
是多行文本。但这些值一旦赋给编辑页的 textarea，浏览器会立即把三种换行统一成
LF——若保存时直接提交输入框内容，未改动字段里的 CRLF/CR 就会在用户毫不知情的
情况下全部变成 LF。本文件用真实 Chrome 驱动真实页面，保护既有的保存行为：

- 打开编辑页：完整文字、原来的行次、全部选项按原次序显示，内部换行不挤成一行，
  也不额外插入空行（textarea 里按 LF 分行显示，但原始字符串在保存前已被记录）。
- 不改文字直接保存，或仅调整某题的必填勾选后保存：问卷编号不变，PUT 请求体与
  再次读取的草稿中，所有未改文字字段逐字符等于原稿（CRLF/CR 不能全变 LF），
  题目与选项次序不变；详情与再次打开的编辑页显示同样的内容。
- 改过又恢复：字段先被改成别的内容，再把可见文字与分行恢复成打开时的样子后
  保存，该字段仍按原稿的换行形式提交。
- 问卷标题、题目标题、选项只增删首尾空白：沿用原稿内部换行，同时仍遵守既有的
  首尾裁剪规则（保存值不含首尾空白）。
- 说明不裁剪：只给说明增加首尾空白也算真实修改，必须原样保存，不能因为“正文
  没变”就被当成未改动而忽略这次输入。
- 真实修改：只改一个多行字段的文字或内部行次后保存，该字段保存编辑框当前内容
  （浏览器中只会是 LF），不能被旧原稿覆盖；其余未改字段仍保留各自原来的换行
  形式。保存成功后详情展示新文字，再次打开编辑页显示已保存内容，并以它作为
  新一轮“原稿”。

问卷一律先经接口创建，再从详情页点击“编辑草稿”进入编辑页。断言同时基于：
挂起的 PUT 请求体（经 Chrome DevTools Protocol Fetch 域截获）、再次读取接口
得到的落库结果，以及用户可见的 DOM/表单值。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_edit_save_preserve_newlines -v
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(os.environ.get("APP_UNDER_TEST",
                          Path(__file__).resolve().parent / "app.py"))
CHROME = os.environ.get("CHROME_BIN", "google-chrome")


# --------------------------------------------------------------------------
# 原稿：同一字段混用 CRLF / CR / LF，包含中文与引号。首尾均无空白——标题、
# 题目标题、选项在接口侧会 strip，内部换行一律保留。
# --------------------------------------------------------------------------

TITLE = '问卷标题“第一行” 带"引号"\r\n第二行（CRLF）\r第三行（单独 CR）\n第四行（LF）'
DESC = "说明首行（CRLF）\r\n说明第二行（单独 CR）\r说明第三行（LF）\n说明第四行"
Q1_TITLE = "文本题“请填写”\n第二行（LF）\r\n第三行（CRLF）"
Q2_TITLE = '单选题“请选择”\r第二行（单独 CR）\n第三行（LF）'
OPT_A = '选项“甲”\r\n甲的第二行（CRLF）'
OPT_B = '选项“乙” 带"引号"\r乙的第二行（CR）\n第三行（LF）'
OPT_C = "选项“丙”\n丙的第二行（LF）"


def norm_nl(value):
    """浏览器 textarea 中的形态：CRLF 与单独 CR 都被归一化成 LF。"""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def seed_payload():
    return {
        "title": TITLE,
        "description": DESC,
        "questions": [
            {"type": "text", "title": Q1_TITLE, "required": False},
            {"type": "single_choice", "title": Q2_TITLE, "required": True,
             "options": [OPT_A, OPT_B, OPT_C]},
        ],
    }


# --------------------------------------------------------------------------
# 真实 app.py 服务进程（与其它浏览器回归测试同款夹具）
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-newlines-")
        self.process = None
        self.base_url = None

    def start(self):
        # 自行选定空闲端口：app.py 每次请求都会向 stderr 写访问日志，若用
        # PIPE 承接输出又不持续读取，缓冲区写满后服务会阻塞在写日志上，因此
        # 把子进程输出直接丢弃。
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.process = subprocess.Popen(
            [sys.executable, str(APP), "serve",
             "--host", "127.0.0.1", "--port", str(port),
             "--data-dir", self.data_dir.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base_url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(
                        f"{self.base_url}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
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
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            status = error.code
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw


# --------------------------------------------------------------------------
# 极简 WebSocket 客户端（RFC 6455 客户端握手 + 文本帧，仅用于连接 CDP）
# --------------------------------------------------------------------------

class WebSocket:
    def __init__(self, url):
        host_port, path = url[5:].split("/", 1)
        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            port = int(port)
        else:
            host, port = host_port, 80
        self.sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败：连接已关闭")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket 握手被拒绝：{head[:120]!r}")
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._results = {}
        self._events = []
        self._next_id = 0
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _send_text(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        n = len(payload)
        prefix = bytearray([0x81])
        if n < 126:
            prefix.append(0x80 | n)
        elif n < 65536:
            prefix.append(0x80 | 126)
            prefix += struct.pack(">H", n)
        else:
            prefix.append(0x80 | 127)
            prefix += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(prefix) + mask + masked)

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("连接关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_message(self):
        chunks = []
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8))[0]
        data = self._read_exact(n) if n else b""
        if not fin:  # 分片帧（CDP 一般不发送），拼到 FIN 为止
            while True:
                c0, c1 = self._read_exact(2)
                cn = c1 & 0x7F
                if cn == 126:
                    cn = struct.unpack(">H", self._read_exact(2))[0]
                elif cn == 127:
                    cn = struct.unpack(">Q", self._read_exact(8))[0]
                data += self._read_exact(cn) if cn else b""
                if c0 & 0x80:
                    break
        if opcode == 0x9:  # ping -> pong
            with self._send_lock:
                self.sock.sendall(b"\x8A\x00")
            return None
        if opcode == 0x8:  # close
            raise OSError("收到 close 帧")
        if opcode != 0x1:
            return None
        return json.loads(data.decode("utf-8"))

    def _read_loop(self):
        while not self._closed:
            try:
                message = self._read_message()
            except OSError:
                break
            if message is None:
                continue
            with self._cond:
                if "id" in message:
                    self._results[message["id"]] = message
                else:
                    self._events.append(message)
                self._cond.notify_all()

    def call(self, method, params=None, *, session_id=None, timeout=15):
        with self._cond:
            self._next_id += 1
            msg_id = self._next_id
        message = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self._send_text(json.dumps(message))
        end = time.time() + timeout
        with self._cond:
            while msg_id not in self._results:
                remaining = end - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"CDP 调用超时：{method}")
                self._cond.wait(remaining)
            result = self._results.pop(msg_id)
        if "error" in result:
            raise RuntimeError(f"CDP 错误（{method}）：{result['error']}")
        return result.get("result", {})

    def drain_events(self, timeout=0.1):
        """等待并取出缓存的全部事件（命令响应不会混入）。"""
        end = time.time() + timeout
        with self._cond:
            while not self._events and time.time() < end:
                self._cond.wait(max(0.0, end - time.time()))
            events, self._events = self._events, []
            return events

    def push_events(self, events):
        """把不属于调用方的事件放回队列，供其它等待者继续观察。"""
        with self._cond:
            self._events.extend(events)
            self._cond.notify_all()

    def close(self):
        self._closed = True
        with self._send_lock:
            try:
                self.sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------
# 真实 Chrome 中的一个标签页（扁平化 CDP 会话）
# --------------------------------------------------------------------------

# 注入到每个页面文档的测试操作手柄：所有动作都走真实的 DOM 事件。
TEST_HELPERS = r"""
(() => {
  const T = window.__t = {};
  const fire = el => {
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  T.cards = () => [...document.querySelectorAll('.q-card')];
  T.setTitle = v => {
    const el = document.getElementById('survey-title');
    el.focus(); el.value = v; fire(el);
  };
  T.setDesc = v => {
    const el = document.getElementById('survey-desc');
    el.focus(); el.value = v; fire(el);
  };
  T.setQTitle = (i, v) => {
    const el = T.cards()[i].querySelector('.q-title');
    el.focus(); el.value = v; fire(el);
  };
  T.setRequired = (i, v) => {
    const el = T.cards()[i].querySelector('.q-required');
    el.checked = !!v;
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  T.setOption = (qi, oi, v) => {
    const el = T.cards()[qi].querySelectorAll('.opt-text')[oi];
    el.focus(); el.value = v; fire(el);
  };
  T.submit = () => document.querySelector('#draft-form button[type=submit]').click();
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
})();
"""

# 编辑页表单快照：取 textarea/勾选框的当前值（用户在编辑框里实际看到的内容）。
SNAPSHOT_JS = r"""
(() => ({
  hash: location.hash,
  title: document.getElementById('survey-title').value,
  description: document.getElementById('survey-desc').value,
  questions: [...document.querySelectorAll('.q-card')].map(card => ({
    type: card.dataset.type,
    title: card.querySelector('.q-title').value,
    required: card.querySelector('.q-required').checked,
    options: [...card.querySelectorAll('.opt-text')].map(o => o.value),
  })),
}))()
"""

# 详情页读取：
# - textContent 为 DOM 文本节点中的原字符串（接口给的 CRLF/CR 应逐字符在内）；
# - innerText 为按 pre-wrap 实际渲染出的可见文本，分行必须与原稿行次一致，
#   既不挤成一行也不多出空行。
DETAIL_JS = r"""
(() => ({
  hash: location.hash,
  heading: (() => {
    const el = document.querySelector('h2');
    return el ? {text: el.textContent, visible: el.innerText} : null;
  })(),
  description: (() => {
    const el = document.querySelector('.detail-desc');
    return el ? {text: el.textContent, visible: el.innerText} : null;
  })(),
  questions: [...document.querySelectorAll('.q-list > li')].map(li => ({
    titleVisible: li.querySelector('.q-line').innerText,
    options: [...li.querySelectorAll('.opt-text-display')].map(o => ({
      text: o.textContent, visible: o.innerText,
    })),
  })),
  bodyText: document.body.innerText,
}))()
"""


class ChromeBrowser:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="opensurvey-chrome-")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.process = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-first-run", "--no-default-browser-check",
             f"--remote-debugging-port={self.port}",
             f"--user-data-dir={self.profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=1) as resp:
                    info = json.loads(resp.read())
                break
            except OSError:
                time.sleep(0.1)
        if info is None:
            raise RuntimeError("Chrome 调试端口未就绪")
        self.ws = WebSocket(info["webSocketDebuggerUrl"])

    def new_page(self):
        result = self.ws.call("Target.createTarget", {"url": "about:blank"})
        target_id = result["targetId"]
        result = self.ws.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True})
        page = ChromePage(self.ws, target_id, result["sessionId"])
        page.prepare()
        return page

    def close(self):
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()
        self.ws.close()
        shutil.rmtree(self.profile, ignore_errors=True)


class ChromePage:
    def __init__(self, ws, target_id, session_id):
        self.ws = ws
        self.target_id = target_id
        self.session = session_id
        self.held = []          # 被挂起的 PUT 请求（按到达顺序）
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，非 PUT 请求（列表/详情/静态
        # 资源）必须立即放行，否则页面自己的 GET 会被挂起饿死；PUT 才按顺序
        # 收集起来交给测试核对请求体后再放行。
        self._pumping = True
        threading.Thread(target=self._event_loop, daemon=True).start()

    def _event_loop(self):
        while self._pumping:
            events = self.ws.drain_events(0.2)
            passthrough = []
            for event in events:
                if event.get("method") == "Fetch.requestPaused":
                    params = event["params"]
                    if params["request"]["method"] == "PUT":
                        with self._held_cond:
                            self.held.append(params)
                            self._held_cond.notify_all()
                        continue
                    try:
                        self.call("Fetch.continueRequest",
                                  {"requestId": params["requestId"]})
                    except Exception:
                        pass
                else:
                    passthrough.append(event)
            if passthrough:
                self.ws.push_events(passthrough)

    def call(self, method, params=None, timeout=15):
        return self.ws.call(method, params, session_id=self.session, timeout=timeout)

    def close(self):
        self._pumping = False
        try:
            self.ws.call("Target.closeTarget", {"targetId": self.target_id}, timeout=5)
        except Exception:
            pass

    def eval(self, expression, timeout=10):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise AssertionError(f"页面脚本异常：{json.dumps(details, ensure_ascii=False)[:500]}")
        return result.get("result", {}).get("value")

    def t(self, name, *args):
        arg_s = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        return self.eval(f"window.__t.{name}({arg_s})")

    # ---------- 导航 ----------

    def open(self, url):
        self.ws.drain_events(0)
        self.call("Page.navigate", {"url": url})
        end = time.time() + 10
        while time.time() < end:
            for event in self.ws.drain_events(0.2):
                if event.get("method") == "Page.loadEventFired":
                    return
        raise AssertionError(f"页面加载超时：{url}")

    def wait_for(self, expression, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            value = self.eval(expression)
            if value:
                return value
            time.sleep(0.15)
        raise AssertionError(f"等待页面条件超时：{expression}")

    def settle(self, seconds=0.5):
        """让后台拦截循环、微任务与异步回调有机会落地。"""
        time.sleep(seconds)

    # ---------- 保存请求拦截 ----------

    def hold_puts(self):
        """挂起所有发往 /api/surveys 的 PUT；GET 等由后台事件循环立即放行。"""
        with self._held_cond:
            self.held = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def pop_held(self, timeout=8):
        """取出（并移除）最早一个被挂起的 PUT。"""
        end = time.time() + timeout
        with self._held_cond:
            while not self.held and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held:
                return self.held.pop(0)
        raise AssertionError("保存请求未发出（PUT 未被挂起）")

    def release_to_server(self, paused):
        """让挂起的请求真正到达服务器并把响应原样带回页面。"""
        self.call("Fetch.continueRequest", {"requestId": paused["requestId"]})

    @staticmethod
    def put_body(paused):
        """读取挂起请求实际携带的 JSON 请求体（逐字符，含 CR）。"""
        raw = paused["request"].get("postData")
        if raw is None:
            raise AssertionError("挂起的 PUT 没有请求体")
        return json.loads(raw)

    def stop_holding(self):
        try:
            self.call("Fetch.disable", timeout=5)
        except Exception:
            pass

    # ---------- 页面状态读取 ----------

    def snapshot(self):
        return self.eval(SNAPSHOT_JS)

    def detail(self):
        return self.eval(DETAIL_JS)


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

class PreserveNewlinesTests(unittest.TestCase):
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

    def seed_survey(self):
        """先经接口创建混用三种换行的草稿，返回其编号与创建响应。"""
        status, data = self.api("POST", "/api/surveys", seed_payload())
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        # 前置条件：接口本身逐字符保留了内部 CRLF/CR，否则网页测试无意义。
        self.assertIn("\r", data["title"])
        self.assertIn("\r", data["description"])
        self.assertIn("\r", data["questions"][0]["title"])
        self.assertIn("\r", data["questions"][1]["title"])
        self.assertIn("\r", data["questions"][1]["options"][0])
        self.assertIn("\r", data["questions"][1]["options"][1])
        self.assertNotIn("\r", data["questions"][1]["options"][2],
                         "夹具本身应同时包含纯 LF 字段，用于区分三种换行")
        return data["id"], data

    def open_detail(self, survey_id):
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}' && "
            "document.querySelectorAll('.q-list > li').length === 2")
        return self.page.detail()

    def open_edit_from_detail(self, survey_id):
        """按真实用户路径：详情页点击“编辑草稿”进入编辑页。"""
        self.page.t("clickHref", f"#/surveys/{survey_id}/edit")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}/edit' && "
            "!!document.querySelector('#draft-form .q-card') && "
            "document.getElementById('survey-title').value.length > 0")

    def open_detail_then_edit(self, survey_id):
        self.open_detail(survey_id)
        self.open_edit_from_detail(survey_id)

    def listing_ids(self):
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, data)
        return [s["id"] for s in data["surveys"]]

    def save_and_capture_put(self, survey_id):
        """点击保存，截获 PUT 请求体后放行，等待页面跳回详情。

        返回 (请求体, 保存前列表编号)；同一套类夹具被多个用例共用，因此只
        断言保存前后编号集合不变，而不假设库里只有当前这一份问卷。
        """
        page = self.page
        before_ids = self.listing_ids()
        page.hold_puts()
        page.t("submit")
        paused = page.pop_held()
        body = page.put_body(paused)
        page.release_to_server(paused)
        page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        page.wait_for("!!document.querySelector('.q-list')")
        return body, before_ids

    def get_survey(self, survey_id):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        return data

    def assert_visible_lines(self, visible, expected_raw, where):
        """pre-wrap 渲染出的可见文本必须与原稿归一化后逐字符一致。

        既不把内部换行挤成一行（行数与每行内容一致），也不额外插入空行。
        """
        self.assertEqual(
            norm_nl(visible), norm_nl(expected_raw),
            f"{where}：可见分行与原稿不一致（挤成一行或多出空行）")

    def assert_snapshot_shows_seed(self, snapshot, where):
        """编辑页各字段显示完整文字、原来的行次、全部选项按原次序排列。"""
        self.assertEqual(snapshot["title"], norm_nl(TITLE), f"{where}：标题显示")
        self.assertEqual(snapshot["title"].split("\n"),
                         norm_nl(TITLE).split("\n"), f"{where}：标题行次")
        self.assertEqual(snapshot["description"], norm_nl(DESC),
                         f"{where}：说明显示")
        self.assertEqual(
            [(q["type"], q["title"], q["required"]) for q in snapshot["questions"]],
            [("text", norm_nl(Q1_TITLE), False),
             ("single_choice", norm_nl(Q2_TITLE), True)],
            f"{where}：题型/题目标题/必填/题序")
        self.assertEqual(
            snapshot["questions"][0]["options"], [],
            f"{where}：文本题不应出现选项")
        self.assertEqual(
            snapshot["questions"][1]["options"],
            [norm_nl(OPT_A), norm_nl(OPT_B), norm_nl(OPT_C)],
            f"{where}：选项内容或次序")
        # 浏览器在赋值瞬间就把换行归一化为 LF；没有任何 CR 残留才能证明
        # 后续测试验证的“原稿还原”确实是页面额外保障，而不是浏览器白送的。
        for field_name in ("title", "description"):
            self.assertNotIn("\r", snapshot[field_name],
                             f"{where}：{field_name} 输入框里不应残留 CR")
        for qi, question in enumerate(snapshot["questions"]):
            self.assertNotIn("\r", question["title"],
                             f"{where}：第 {qi + 1} 题标题输入框不应残留 CR")
            for oi, option in enumerate(question["options"]):
                self.assertNotIn("\r", option,
                                 f"{where}：第 {qi + 1} 题选项 {oi + 1} 不应残留 CR")

    def assert_stored_matches(self, survey_id, expected, where, *, before_ids=None):
        """再次读取接口：落库草稿逐字符等于期望，编号不变。"""
        stored = self.get_survey(survey_id)
        self.assertEqual(stored["id"], survey_id, f"{where}：问卷编号被改变")
        self.assertEqual(stored["title"], expected["title"], f"{where}：落库标题")
        self.assertEqual(stored["description"], expected["description"],
                         f"{where}：落库说明")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], list(q.get("options", [])))
             for q in stored["questions"]],
            [(q["type"], q["title"], q["required"], list(q.get("options", [])))
             for q in expected["questions"]],
            f"{where}：落库题目/选项/必填/次序")
        # 保存是整份替换而不是新建：编号集合保持不变，当前编号仍在其中。
        after_ids = self.listing_ids()
        self.assertEqual(after_ids, before_ids,
                         f"{where}：保存前后问卷编号集合变化（疑似新建记录）")
        self.assertIn(survey_id, after_ids, f"{where}：原编号消失")

    # ---------- 打开编辑页：完整显示，不挤行、不插空行 ----------

    def test_edit_page_shows_full_multiline_text_line_order_and_all_options(self):
        survey_id, _ = self.seed_survey()

        # 详情页：接口下发的原始字符串逐字符进入 DOM（CRLF/CR 都还在）。
        detail = self.open_detail(survey_id)
        self.assertEqual(detail["heading"]["text"], f"#{survey_id} {TITLE}",
                         "详情标题 DOM 文本被改写")
        self.assertEqual(detail["description"]["text"], DESC,
                         "详情说明 DOM 文本被改写")
        choice_options = detail["questions"][1]["options"]
        self.assertEqual(
            [o["text"] for o in choice_options], [OPT_A, OPT_B, OPT_C],
            "详情选项 DOM 文本或次序被改写")
        # 可见渲染（pre-wrap）：分行与原稿一致，不挤成一行，不插空行。
        self.assert_visible_lines(
            detail["heading"]["visible"], f"#{survey_id} {TITLE}", "详情标题")
        self.assert_visible_lines(detail["description"]["visible"], DESC, "详情说明")
        for option, raw in zip(choice_options, (OPT_A, OPT_B, OPT_C)):
            self.assert_visible_lines(option["visible"], raw, "详情选项")

        # 从详情进入编辑页：textarea 中看到完整文字、原行次与全部选项。
        self.open_edit_from_detail(survey_id)
        self.assert_snapshot_shows_seed(
            self.page.snapshot(), "打开编辑页")

    # ---------- 不改文字直接保存：原稿（含 CRLF/CR）逐字符保留 ----------

    def test_save_without_changes_resends_and_persists_raw_strings(self):
        survey_id, _ = self.seed_survey()
        self.open_detail_then_edit(survey_id)

        body, before_ids = self.save_and_capture_put(survey_id)
        self.assertEqual(body, seed_payload(),
                         "未改动直接保存时，PUT 请求体必须逐字符等于原稿")
        self.assert_stored_matches(
            survey_id, seed_payload(), "未改动保存后", before_ids=before_ids)

        # 详情页展示的仍是原稿分行。
        detail = self.page.detail()
        self.assert_visible_lines(
            detail["heading"]["visible"], f"#{survey_id} {TITLE}", "保存后详情标题")
        self.assert_visible_lines(
            detail["description"]["visible"], DESC, "保存后详情说明")

        # 再次打开编辑页仍显示同样内容；紧接着第二次原样保存，请求体依旧
        # 逐字符等于原稿——保留行为在多轮编辑间持续成立。
        self.open_edit_from_detail(survey_id)
        self.assert_snapshot_shows_seed(
            self.page.snapshot(), "再次打开编辑页")
        second_body, before_ids = self.save_and_capture_put(survey_id)
        self.assertEqual(second_body, seed_payload(),
                         "第二轮未改动保存时原稿换行仍被保留")
        self.assert_stored_matches(
            survey_id, seed_payload(), "第二轮保存后", before_ids=before_ids)

    # ---------- 只改必填勾选：文字字段原稿逐字符保留 ----------

    def test_save_after_toggling_only_required_keeps_all_raw_text(self):
        survey_id, _ = self.seed_survey()
        self.open_detail_then_edit(survey_id)

        # 只把文本题从选填改成必填，一个字都不动。
        self.page.t("setRequired", 0, True)
        body, before_ids = self.save_and_capture_put(survey_id)

        expected = seed_payload()
        expected["questions"][0]["required"] = True
        self.assertEqual(body, expected,
                         "只改必填时，请求体除该勾选外必须逐字符等于原稿")
        # 显式强调：所有文字字段里的 CR 一个都不能少。
        self.assertIn("\r", body["title"])
        self.assertIn("\r", body["description"])
        self.assertIn("\r", body["questions"][0]["title"])
        self.assertIn("\r", body["questions"][1]["title"])
        self.assertIn("\r", body["questions"][1]["options"][0])
        self.assertIn("\r", body["questions"][1]["options"][1])
        self.assert_stored_matches(
            survey_id, expected, "只改必填保存后", before_ids=before_ids)

    # ---------- 改过又恢复：可见文字与分行恢复即沿用原稿换行 ----------

    def test_change_then_restore_visible_text_keeps_original_newline_forms(self):
        survey_id, _ = self.seed_survey()
        self.open_detail_then_edit(survey_id)
        page = self.page

        # 先把每个字段都改成完全不同的内容（textarea 中只会有 LF）。
        page.t("setTitle", "临时标题\n另一行")
        page.t("setDesc", "临时说明\n临时第二行")
        page.t("setQTitle", 0, "临时文本题")
        page.t("setQTitle", 1, "临时单选题")
        page.t("setOption", 1, 0, "临时选项甲")
        page.t("setOption", 1, 1, "临时选项乙")
        page.t("setOption", 1, 2, "临时选项丙")
        page.settle()

        # 再把可见文字与分行恢复成打开编辑页时的样子（用 LF 形式恢复，
        # 用户无法在输入框里输入 CRLF/CR）。
        page.t("setTitle", norm_nl(TITLE))
        page.t("setDesc", norm_nl(DESC))
        page.t("setQTitle", 0, norm_nl(Q1_TITLE))
        page.t("setQTitle", 1, norm_nl(Q2_TITLE))
        page.t("setOption", 1, 0, norm_nl(OPT_A))
        page.t("setOption", 1, 1, norm_nl(OPT_B))
        page.t("setOption", 1, 2, norm_nl(OPT_C))

        body, before_ids = self.save_and_capture_put(survey_id)
        self.assertEqual(body, seed_payload(),
                         "改过又恢复后保存，必须沿用原稿的 CRLF/CR 形式")
        self.assert_stored_matches(
            survey_id, seed_payload(), "改过又恢复保存后", before_ids=before_ids)

    # ---------- 只增删首尾空白（标题/题目标题/选项）：原稿内部换行保留 ----------

    def test_only_surrounding_whitespace_edits_keep_internal_newlines(self):
        survey_id, _ = self.seed_survey()
        self.open_detail_then_edit(survey_id)
        page = self.page

        # 只在首尾加空白（含制表符、空格、全角空格、换行），正文与分行不动。
        page.t("setTitle", "\t " + norm_nl(TITLE) + " 　\n")
        page.t("setQTitle", 0, "\n" + norm_nl(Q1_TITLE) + "  ")
        page.t("setOption", 1, 0, "  " + norm_nl(OPT_A) + "\n\t")

        body, before_ids = self.save_and_capture_put(survey_id)
        # 首尾空白按既有规则裁掉，字段内部换行沿用原稿（仍是 CRLF/CR），
        # 因此最终请求体与原稿逐字符一致。
        self.assertEqual(body, seed_payload(),
                         "只增删首尾空白时应裁剪首尾并还原原稿内部换行")
        self.assert_stored_matches(
            survey_id, seed_payload(), "首尾空白编辑保存后", before_ids=before_ids)

    # ---------- 说明只加首尾空白：真实修改，必须原样保留（说明不裁剪） ----------

    def test_adding_surrounding_whitespace_to_description_is_a_real_change(self):
        survey_id, _ = self.seed_survey()
        self.open_detail_then_edit(survey_id)

        # 正文一字未动，只在说明首尾加空白；说明不裁剪，这属于真实修改。
        new_description = "　" + norm_nl(DESC) + "   "
        self.page.t("setDesc", new_description)

        body, before_ids = self.save_and_capture_put(survey_id)
        self.assertEqual(body["description"], new_description,
                         "说明首尾新增的空白必须随正文一起保存，不能被忽略")
        self.assertTrue(body["description"].startswith("　"))
        self.assertTrue(body["description"].endswith("   "))
        # 其余字段仍逐字符保留原稿换行。
        self.assertEqual(body["title"], TITLE)
        self.assertEqual(body["questions"], seed_payload()["questions"])

        expected = seed_payload()
        expected["description"] = new_description
        self.assert_stored_matches(
            survey_id, expected, "说明空白修改保存后", before_ids=before_ids)

        # 详情与再次打开的编辑页都必须显示带首尾空白的说明。
        detail = self.page.detail()
        self.assertEqual(detail["description"]["text"], new_description)
        self.open_edit_from_detail(survey_id)
        self.assertEqual(self.page.snapshot()["description"], new_description)

    # ---------- 真实修改一个多行字段：保存编辑框 LF 内容，其余字段保留原稿 ----------

    def test_real_edit_to_title_saves_lf_content_and_keeps_other_fields_raw(self):
        survey_id, _ = self.seed_survey()
        self.open_detail_then_edit(survey_id)

        # 只改问卷标题：改动文字并调整内部行次，编辑框里只会是 LF。
        edited_title = ('问卷标题“已修改” 带"引号"\n第四行（LF）\n'
                        "第二行（CRLF）\n新增的一行")
        self.page.t("setTitle", edited_title)

        body, before_ids = self.save_and_capture_put(survey_id)
        self.assertEqual(body["title"], edited_title,
                         "真实修改必须保存编辑框当前内容（LF），不能被旧原稿覆盖")
        self.assertNotIn("\r", body["title"],
                         "编辑框提交的真实修改里不应出现 CR")
        # 其余字段逐字符保留各自原来的换行形式。
        self.assertEqual(body["description"], DESC)
        self.assertEqual(body["questions"], seed_payload()["questions"])

        expected = seed_payload()
        expected["title"] = edited_title
        self.assert_stored_matches(
            survey_id, expected, "标题真实修改保存后", before_ids=before_ids)

        # 详情展示修改后的文字与行次。
        detail = self.page.detail()
        self.assertEqual(detail["heading"]["text"],
                         f"#{survey_id} {edited_title}")
        self.assert_visible_lines(
            detail["heading"]["visible"],
            f"#{survey_id} {edited_title}", "真实修改后的详情标题")

        # 再次打开编辑页显示已保存内容；此时不改文字再保存一次：新标题按
        # 已保存的 LF 原稿提交，未改字段依旧是各自原来的 CRLF/CR。
        self.open_edit_from_detail(survey_id)
        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["title"], edited_title)
        self.assertEqual(snapshot["description"], norm_nl(DESC))
        second_body, second_before_ids = self.save_and_capture_put(survey_id)
        self.assertEqual(second_body["title"], edited_title,
                         "重新进入后应以已保存内容为新原稿")
        self.assertEqual(second_body["description"], DESC)
        self.assertEqual(second_body["questions"], seed_payload()["questions"])
        self.assert_stored_matches(
            survey_id, expected, "重新进入后第二次保存",
            before_ids=second_before_ids)

    def test_real_edit_to_question_title_and_option_saves_lf_keeps_rest_raw(self):
        survey_id, _ = self.seed_survey()
        self.open_detail_then_edit(survey_id)

        # 只改单选题的题目标题（调整行次）与其中一个选项，全部用编辑框 LF。
        edited_q2_title = "单选题“请选择（已改）”\n第三行（LF）\n第二行（CR 变 LF）"
        edited_opt_b = '选项“乙”已改 带"引号"\n乙的新第二行\n第三行（LF）'
        self.page.t("setQTitle", 1, edited_q2_title)
        self.page.t("setOption", 1, 1, edited_opt_b)

        body, before_ids = self.save_and_capture_put(survey_id)
        self.assertEqual(body["title"], TITLE, "未改标题必须保留原稿换行")
        self.assertEqual(body["description"], DESC, "未改说明必须保留原稿换行")
        self.assertEqual(body["questions"][0], seed_payload()["questions"][0],
                         "未改文本题必须逐字符保留原稿")
        saved_q2 = body["questions"][1]
        self.assertEqual(saved_q2["title"], edited_q2_title,
                         "题目标题真实修改必须保存编辑框当前内容")
        self.assertNotIn("\r", saved_q2["title"])
        self.assertEqual(saved_q2["options"][0], OPT_A,
                         "未改选项甲必须保留原 CRLF")
        self.assertEqual(saved_q2["options"][1], edited_opt_b,
                         "选项真实修改必须保存编辑框当前内容")
        self.assertNotIn("\r", saved_q2["options"][1])
        self.assertEqual(saved_q2["options"][2], OPT_C,
                         "未改选项丙次序与内容必须不变")
        self.assertTrue(saved_q2["required"], "原有必填勾选保持不变")

        # 题目与选项次序不变。
        self.assertEqual([q["type"] for q in body["questions"]],
                         ["text", "single_choice"])

        # 再次读取接口核对落库结果：被改字段为编辑框 LF 内容，其余字段仍是原稿。
        expected = seed_payload()
        expected["questions"][1]["title"] = edited_q2_title
        expected["questions"][1]["options"][1] = edited_opt_b
        self.assert_stored_matches(
            survey_id, expected, "题目标题/选项真实修改保存后",
            before_ids=before_ids)

        # 详情按新行次显示被改字段，未改选项仍以原换行渲染。
        detail = self.page.detail()
        choice = detail["questions"][1]
        self.assertEqual(
            [o["text"] for o in choice["options"]],
            [OPT_A, edited_opt_b, OPT_C],
            "详情选项内容或次序错误")
        for option, raw in zip(choice["options"],
                               (OPT_A, edited_opt_b, OPT_C)):
            self.assert_visible_lines(option["visible"], raw, "详情选项分行")

        # 再次打开编辑页：被改字段显示已保存的 LF 内容，未改字段按原行次显示。
        self.open_edit_from_detail(survey_id)
        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["questions"][1]["title"], edited_q2_title)
        self.assertEqual(snapshot["questions"][1]["options"],
                         [norm_nl(OPT_A), edited_opt_b, norm_nl(OPT_C)])
        self.assertEqual(snapshot["questions"][0]["title"], norm_nl(Q1_TITLE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
