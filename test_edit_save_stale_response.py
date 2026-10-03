#!/usr/bin/env python3
"""编辑问卷草稿“保存结果不得跨页面生效”的浏览器回归保障。

test_survey_replace.py 只通过 HTTP 接口核对整份替换的落库结果；本文件改用真实
Chrome 驱动真实页面，以用户看到的页面与表单内容为断言依据，保护以下行为：

- 在编辑页点击“保存修改”后、结果尚未返回时离开（首页链接、取消链接、浏览器
  前进/后退），旧请求：
  - 即使保存成功，也不能把当前页面带回原问卷详情；
  - 返回校验错误或网络错误时，不能在新页面提示保存失败，更不能清空或覆盖
    用户在首页新建表单 / 另一份问卷编辑页 / 重新进入的同一问卷编辑页中
    已经输入的标题、说明、题目、选项增删与必填设置。
- 重新进入同一份问卷（编号与地址相同）属于一次全新的编辑，先前保存的成功或
  失败结果一律忽略。
- 用户没有离开时，既有行为保持不变：成功跳转详情并展示保存后的内容；
  校验失败 / 网络失败停留在编辑页、显示原因并保留全部修改。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把编辑保存的
PUT 挂起，等用户完成页面切换与新输入后，再决定让它真实到达服务器（200）、
伪造 400 校验响应，还是直接按网络错误失败。合法保存放行后会再读一次接口，
仅作“服务器确实完成了保存”的旁证；页面行为本身只按 DOM 与可见文本断言。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_edit_save_stale_response -v
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

ERROR_PHRASES = ("未能保存", "尚未保存", "保存失败", "网络错误", "检查后重试")


# --------------------------------------------------------------------------
# 真实 app.py 服务进程（与 test_survey_replace.py 中的同款夹具）
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-browser-")
        self.process = None
        self.base_url = None

    def start(self):
        # 自行选定空闲端口：app.py 每次请求都会向 stderr 写访问日志，若用
        # PIPE 承接输出又不持续读取，缓冲区写满后单线程服务会阻塞在写日志上，
        # 因此这里显式指定端口并把子进程输出直接丢弃。
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

# 注入到每个页面文档的测试操作手柄：所有动作都走真实的 DOM 事件与链接。
TEST_HELPERS = r"""
(() => {
  const T = window.__t = {};
  const fire = el => {
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  T.cards = () => [...document.querySelectorAll('.q-card')];
  T.btn = text => [...document.querySelectorAll('button')]
      .find(b => b.textContent.trim() === text);
  T.clickBtn = text => {
    const b = T.btn(text);
    if (!b) throw new Error('找不到按钮：' + text);
    b.click();
  };
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
  T.setRequired = (i, v) => { T.cards()[i].querySelector('.q-required').checked = !!v; };
  T.setOption = (qi, oi, v) => {
    const el = T.cards()[qi].querySelectorAll('.opt-text')[oi];
    el.focus(); el.value = v; fire(el);
  };
  T.addOption = (qi, v) => {
    const card = T.cards()[qi];
    card.querySelector('.add-opt').click();
    const rows = card.querySelectorAll('.opt-row');
    const el = rows[rows.length - 1].querySelector('.opt-text');
    el.focus(); el.value = v; fire(el);
  };
  T.removeOption = (qi, oi) => { T.cards()[qi].querySelectorAll('.opt-row')[oi].remove(); };
  T.removeQuestion = i => {
    const b = [...T.cards()[i].querySelectorAll('button')]
      .find(x => x.textContent.trim() === '删除本题');
    b.click();
  };
  T.addText = (title, required) => {
    T.clickBtn('添加文本题');
    const card = T.cards()[T.cards().length - 1];
    const el = card.querySelector('.q-title');
    el.value = title; fire(el);
    card.querySelector('.q-required').checked = !!required;
  };
  T.addChoice = (title, options, required) => {
    T.clickBtn('添加单选题');
    const card = T.cards()[T.cards().length - 1];
    const titleEl = card.querySelector('.q-title');
    titleEl.value = title; fire(titleEl);
    card.querySelector('.q-required').checked = !!required;
    while (card.querySelectorAll('.opt-row').length < options.length) {
      card.querySelector('.add-opt').click();
    }
    [...card.querySelectorAll('.opt-row')].forEach((row, i) => {
      if (i < options.length) {
        const el = row.querySelector('.opt-text');
        el.value = options[i]; fire(el);
      } else {
        row.remove();
      }
    });
  };
  T.submit = () => document.querySelector('#draft-form button[type=submit]').click();
  T.clickLink = needle => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href').indexOf(needle) !== -1);
    if (!a) throw new Error('找不到包含 ' + needle + ' 的链接');
    a.click();
  };
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
  T.back = () => history.back();
  T.forward = () => history.forward();
})();
"""

# 当前页面状态快照——全部来自用户可见的 DOM 与表单值。
SNAPSHOT_JS = r"""
(() => {
  const banner = document.getElementById('form-banner');
  const titleEl = document.getElementById('survey-title');
  const descEl = document.getElementById('survey-desc');
  const formHeading = document.querySelector('#draft-form h2');
  return {
    hash: location.hash,
    formHeading: formHeading ? formHeading.textContent : null,
    bannerVisible: banner ? !banner.hidden : false,
    bannerText: banner ? banner.textContent : '',
    anyVisibleBanner: !!document.querySelector('.banner:not([hidden])'),
    title: titleEl ? titleEl.value : null,
    description: descEl ? descEl.value : null,
    questions: [...document.querySelectorAll('.q-card')].map(card => ({
      type: card.dataset.type,
      title: card.querySelector('.q-title').value,
      required: card.querySelector('.q-required').checked,
      options: [...card.querySelectorAll('.opt-text')].map(o => o.value),
    })),
    bodyText: document.body.innerText,
  };
})()
"""

DETAIL_JS = r"""
(() => ({
  hash: location.hash,
  heading: document.querySelector('h2') ? document.querySelector('h2').textContent : null,
  description: (() => {
    const el = document.querySelector('.detail-desc');
    return el ? el.textContent : null;
  })(),
  lines: [...document.querySelectorAll('.q-list > li')].map(li => li.innerText),
  anyVisibleBanner: !!document.querySelector('.banner:not([hidden])'),
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
        self.held = []          # 被挂起的 PUT 请求
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，非 PUT 请求（列表/详情/静态
        # 资源）必须立即放行，否则切换到的新页面会因自己的 GET 被挂起而饿死；
        # PUT 才收集起来交给测试决定何时、以何种结果放行。
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

    def settle(self, seconds=0.6):
        """让微任务/异步回调有机会落地。"""
        time.sleep(seconds)

    # ---------- 保存请求拦截 ----------

    def hold_puts(self):
        """挂起所有发往 /api/surveys 的 PUT；GET 等由后台事件循环立即放行。"""
        with self._held_cond:
            self.held = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def wait_held_put(self, timeout=8):
        end = time.time() + timeout
        with self._held_cond:
            while not self.held and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held:
                return self.held[0]
        raise AssertionError("保存请求未发出（PUT 未被挂起）")

    def release_to_server(self, paused):
        """让挂起的请求真正到达服务器并把响应原样带回页面。"""
        self.call("Fetch.continueRequest", {"requestId": paused["requestId"]})

    def fulfill_json(self, paused, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.call("Fetch.fulfillRequest", {
            "requestId": paused["requestId"],
            "responseCode": status,
            "responseHeaders": [{"name": "Content-Type",
                                 "value": "application/json; charset=utf-8"}],
            "body": base64.b64encode(body).decode("ascii"),
        })

    def fail_as_network_error(self, paused):
        self.call("Fetch.failRequest",
                  {"requestId": paused["requestId"], "errorReason": "Failed"})

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

class StaleSaveResponseTests(unittest.TestCase):
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

    def seed_survey(self, title="旧标题", description="旧说明"):
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

    def open_edit(self, survey_id):
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}/edit")
        self.page.wait_for(
            "!!document.querySelector('#draft-form .q-card') && "
            "document.getElementById('survey-title').value !== ''")

    def open_home(self):
        self.page.open(f"{self.server.base_url}/#/")
        self.page.wait_for("!!document.querySelector('#draft-form')")

    def edit_and_hold(self):
        """在编辑页做出一批覆盖标题/说明/题目/选项/必填的修改并挂起保存。"""
        page = self.page
        page.hold_puts()
        page.t("setTitle", "第一次保存的标题")
        page.t("setDesc", "第一次保存的说明\n带换行")
        # 原单选题：删除一个选项、改写一个、新增一个；必填从 true 改为 false。
        page.t("setQTitle", 1, "改过的单选题")
        page.t("removeOption", 1, 2)          # 删掉“选项丙”，剩 甲/乙
        page.t("setOption", 1, 0, "选项甲改")
        page.t("addOption", 1, "选项丁")
        page.t("setRequired", 1, False)
        # 新增一道必填文本题。
        page.t("addText", "第一次新增文本题", True)
        page.t("submit")
        return page.wait_held_put()

    def expected_first_save(self):
        return {
            "title": "第一次保存的标题",
            "description": "第一次保存的说明\n带换行",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False, "options": []},
                {"type": "single_choice", "title": "改过的单选题", "required": False,
                 "options": ["选项甲改", "选项乙", "选项丁"]},
                {"type": "text", "title": "第一次新增文本题", "required": True, "options": []},
            ],
        }

    def fill_home_create_form(self):
        """离开后用户在首页新建表单里开始填写的一批新内容。"""
        page = self.page
        page.wait_for("document.querySelector('#draft-form h2') && "
                      "document.querySelector('#draft-form h2').textContent === '新建问卷草稿'")
        page.t("setTitle", "首页新建的问卷")
        page.t("setDesc", "首页新建说明")
        page.t("addText", "首页文本题", True)
        page.t("addChoice", "首页单选题", ["首页选项甲", "首页选项乙"], False)
        return {
            "formHeading": "新建问卷草稿",
            "title": "首页新建的问卷",
            "description": "首页新建说明",
            "questions": [
                {"type": "text", "title": "首页文本题", "required": True, "options": []},
                {"type": "single_choice", "title": "首页单选题", "required": False,
                 "options": ["首页选项甲", "首页选项乙"]},
            ],
        }

    def assert_no_save_feedback(self, snapshot, where):
        """旧保存的失败绝不能在当前页面留下任何提示。"""
        self.assertFalse(
            snapshot["anyVisibleBanner"],
            f"{where}：当前页面出现了错误提示条：{snapshot.get('bannerText')}")
        for phrase in ERROR_PHRASES:
            self.assertNotIn(
                phrase, snapshot["bodyText"],
                f"{where}：当前页面可见文本出现了保存失败提示“{phrase}”")

    def assert_form_state(self, snapshot, expected, where):
        self.assertEqual(snapshot["title"], expected["title"], f"{where}：标题被改写")
        self.assertEqual(snapshot["description"], expected["description"],
                         f"{where}：说明被改写")
        self.assertEqual(snapshot["formHeading"], expected["formHeading"],
                         f"{where}：离开了当前表单")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snapshot["questions"]],
            [(q["type"], q["title"], q["required"], q["options"])
             for q in expected["questions"]],
            f"{where}：题目/选项/必填状态被旧保存结果改写")
        self.assertFalse(snapshot["bannerVisible"], f"{where}：表单冒出了错误提示条")

    def assert_server_survey(self, survey_id, expected_fields):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        for key, value in expected_fields.items():
            self.assertEqual(data[key], value, f"服务端保存结果与放行的请求不一致：{key}")

    def assert_server_unchanged(self, survey_id, before):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data, before, "请求失败不应改动服务器上的旧草稿")

    # ---------- 离开后：旧保存成功 ----------

    def test_success_after_leaving_via_links_to_home_is_silently_ignored(self):
        """通过页面链接离开到首页并填写新建表单后，旧保存成功不能带回详情或覆盖表单。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        paused = self.edit_and_hold()

        # 通过真实页面链接离开：编辑页“取消”→ 问卷详情 →“返回首页”。
        self.page.t("clickLink", f"#/surveys/{survey_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}' && "
            "!!document.querySelector('.q-list')")
        self.page.t("clickHref", "#/")
        self.page.wait_for("location.hash === '#/' && !!document.querySelector('#draft-form')")

        expected = self.fill_home_create_form()
        snapshot_before = self.page.snapshot()
        self.assert_form_state(snapshot_before, expected, "旧保存返回前的首页新建表单")

        # 旧保存成功返回：放行到真实服务器（合法保存允许在服务器完成）。
        self.page.release_to_server(paused)
        self.page.settle(0.8)

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], "#/",
                         "旧保存成功把页面劫持回了原问卷详情")
        self.assert_form_state(snapshot, expected, "旧保存成功后")
        self.assert_no_save_feedback(snapshot, "旧保存成功后的首页")
        # 再观察一拍，确认没有延迟的跳转/改写。
        self.page.settle(0.4)
        self.assertEqual(self.page.eval("location.hash"), "#/")
        self.assert_form_state(self.page.snapshot(), expected, "旧保存成功后再次观察")

        # 旁证：合法保存确实已在服务器完成，不需要因离开而撤销。
        self.assert_server_survey(survey_id, self.expected_first_save())

    # ---------- 离开后：旧保存返回校验错误 / 网络错误 ----------

    def test_validation_error_after_leaving_to_home_is_silently_ignored(self):
        """首页填写新建表单期间，旧保存的 400 校验响应不能提示或清空任何内容。"""
        survey_id = self.seed_survey()
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        self.open_edit(survey_id)
        paused = self.edit_and_hold()

        self.page.eval(f"location.hash = '#/surveys/{survey_id}'")
        self.page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        self.page.eval("location.hash = '#/'")
        self.page.wait_for("location.hash === '#/'")
        expected = self.fill_home_create_form()

        # 等价于服务器对该次提交返回 400 校验错误。
        self.page.fulfill_json(paused, 400,
                               {"error": "第 2 题：模拟的服务端校验错误"})
        self.page.settle(0.8)

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], "#/", "旧的 400 响应导致页面跳转")
        self.assert_form_state(snapshot, expected, "旧 400 返回后的首页新建表单")
        self.assert_no_save_feedback(snapshot, "旧 400 返回后的首页")
        self.page.settle(0.4)
        self.assert_form_state(self.page.snapshot(), expected, "旧 400 返回后再次观察")
        self.assert_server_unchanged(survey_id, before)

    def test_network_error_after_leaving_to_home_is_silently_ignored(self):
        """首页填写新建表单期间，旧保存网络失败不能提示或清空任何内容。"""
        survey_id = self.seed_survey()
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        self.open_edit(survey_id)
        paused = self.edit_and_hold()

        # 编辑页没有指向首页的链接（“返回问卷详情/取消”都指向详情），
        # 这里直接改 hash 离开，hashchange 走的仍是与点链接相同的路由。
        self.page.eval("location.hash = '#/'")
        self.page.wait_for("location.hash === '#/' && !!document.querySelector('#draft-form')")
        expected = self.fill_home_create_form()

        self.page.fail_as_network_error(paused)
        self.page.settle(0.8)

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], "#/", "旧保存的网络失败导致页面跳转")
        self.assert_form_state(snapshot, expected, "旧网络失败后的首页新建表单")
        self.assert_no_save_feedback(snapshot, "旧网络失败后的首页")
        self.assert_server_unchanged(survey_id, before)

    # ---------- 离开后去编辑另一份问卷 ----------

    def test_stale_save_while_editing_another_survey_is_ignored(self):
        """在另一份问卷的编辑页继续输入时，旧保存结果不能跳转、提示或改写该表单。"""
        survey_a = self.seed_survey(title="问卷甲", description="甲的说明")
        survey_b = self.seed_survey(title="问卷乙", description="乙的说明")
        before_b = self.api("GET", f"/api/surveys/{survey_b}")[1]

        self.open_edit(survey_a)
        paused = self.edit_and_hold()

        # 经首页进入另一份问卷的编辑页。
        self.page.eval("location.hash = '#/'")
        self.page.wait_for("location.hash === '#/'")
        # 等首页列表渲染出问卷乙的真实链接后再点击。
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_b}\"]')")
        self.page.t("clickHref", f"#/surveys/{survey_b}")
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_b}/edit\"]')")
        self.page.t("clickHref", f"#/surveys/{survey_b}/edit")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_b}/edit' && "
            "(document.getElementById('survey-title')||{}).value === '问卷乙'")

        # 在新的编辑会话里修改标题、说明，删除选项、调整必填、新增题目。
        self.page.t("setTitle", "问卷乙的新标题")
        self.page.t("setDesc", "乙的新说明")
        self.page.t("setRequired", 0, True)
        self.page.t("removeOption", 1, 2)   # 删掉问卷乙原单选题的“选项丙”
        self.page.t("addChoice", "乙新增单选题", ["乙选项一", "乙选项二"], True)
        expected_b = {
            "formHeading": f"编辑问卷草稿 #{survey_b}",
            "title": "问卷乙的新标题",
            "description": "乙的新说明",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": True, "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙"]},
                {"type": "single_choice", "title": "乙新增单选题", "required": True,
                 "options": ["乙选项一", "乙选项二"]},
            ],
        }
        self.assert_form_state(self.page.snapshot(), expected_b, "旧保存返回前的问卷乙表单")

        self.page.release_to_server(paused)
        self.page.settle(0.8)

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_b}/edit",
                         "旧保存成功把页面从问卷乙带回了问卷甲详情")
        self.assert_form_state(snapshot, expected_b, "旧保存成功后的问卷乙编辑页")
        self.assert_no_save_feedback(snapshot, "问卷乙编辑页")
        # 问卷乙从未提交保存，服务器内容保持原样。
        self.assert_server_unchanged(survey_b, before_b)
        # 问卷甲的合法保存照常完成。
        self.assert_server_survey(survey_a, self.expected_first_save())

    # ---------- 离开后重新进入同一份问卷的编辑页 ----------

    def _leave_and_reenter_same_edit(self, survey_id):
        # 经“取消”到详情，再点“编辑草稿”重新进入：编号与地址相同，但是全新一代表单。
        self.page.t("clickHref", f"#/surveys/{survey_id}")
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_id}/edit\"]')")
        self.page.t("clickHref", f"#/surveys/{survey_id}/edit")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}/edit' && "
            "(document.getElementById('survey-title')||{}).value === '旧标题'")

    def _fill_second_edit(self):
        self.page.t("setTitle", "第二次编辑的标题")
        self.page.t("setDesc", "第二次编辑的说明")
        self.page.t("removeOption", 1, 2)       # 原单选题删掉“选项丙”
        self.page.t("addChoice", "第二次新增单选题", ["二选一", "二选二"], True)
        return {
            "formHeading": "编辑问卷草稿 #",  # 编号在断言处补全
            "title": "第二次编辑的标题",
            "description": "第二次编辑的说明",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False, "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙"]},
                {"type": "single_choice", "title": "第二次新增单选题", "required": True,
                 "options": ["二选一", "二选二"]},
            ],
        }

    def test_reenter_same_survey_ignores_prior_success(self):
        """重新进入同一问卷后继续输入：先前那次保存成功不能跳转或改写本次输入。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        paused = self.edit_and_hold()

        self._leave_and_reenter_same_edit(survey_id)
        expected = self._fill_second_edit()
        expected["formHeading"] = f"编辑问卷草稿 #{survey_id}"

        self.page.release_to_server(paused)
        self.page.settle(0.8)

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit",
                         "旧保存成功把重新进入的编辑页带去了详情")
        self.assert_form_state(snapshot, expected, "旧保存成功后的第二次编辑")
        self.assert_no_save_feedback(snapshot, "重新进入的编辑页")
        # 第一次的合法保存仍在服务器完成。
        self.assert_server_survey(survey_id, self.expected_first_save())

    def test_reenter_same_survey_ignores_prior_validation_error(self):
        """重新进入同一问卷后：先前那次保存的 400 不能追加提示或改写本次输入。"""
        survey_id = self.seed_survey()
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        self.open_edit(survey_id)
        paused = self.edit_and_hold()

        self._leave_and_reenter_same_edit(survey_id)
        expected = self._fill_second_edit()
        expected["formHeading"] = f"编辑问卷草稿 #{survey_id}"

        self.page.fulfill_json(paused, 400, {"error": "第 1 题：模拟的旧校验错误"})
        self.page.settle(0.8)

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit")
        self.assert_form_state(snapshot, expected, "旧 400 返回后的第二次编辑")
        self.assert_no_save_feedback(snapshot, "旧 400 返回后的第二次编辑")
        self.assert_server_unchanged(survey_id, before)

    def test_reenter_same_survey_ignores_prior_network_error(self):
        """重新进入同一问卷后：先前那次保存的网络失败不能追加提示或改写本次输入。"""
        survey_id = self.seed_survey()
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        self.open_edit(survey_id)
        paused = self.edit_and_hold()

        self._leave_and_reenter_same_edit(survey_id)
        expected = self._fill_second_edit()
        expected["formHeading"] = f"编辑问卷草稿 #{survey_id}"

        self.page.fail_as_network_error(paused)
        self.page.settle(0.8)

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit")
        self.assert_form_state(snapshot, expected, "旧网络失败后的第二次编辑")
        self.assert_no_save_feedback(snapshot, "旧网络失败后的第二次编辑")
        self.assert_server_unchanged(survey_id, before)

    # ---------- 浏览器前进 / 后退切换 ----------

    def test_browser_back_and_forward_switching_ignores_stale_success(self):
        """用浏览器后退、前进切换页面时，旧保存成功在切换后的页面上同样被忽略。"""
        survey_id = self.seed_survey(title="切换问卷")
        # 从首页经真实链接进入：首页 → 详情 → 编辑，形成可前进/后退的历史。
        self.open_home()
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_id}\"]')")
        self.page.t("clickHref", f"#/surveys/{survey_id}")
        self.page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        self.page.t("clickHref", f"#/surveys/{survey_id}/edit")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}/edit' && "
            "(document.getElementById('survey-title')||{}).value === '切换问卷'")

        # 第一代编辑：修改并挂起保存。
        page = self.page
        page.hold_puts()
        page.t("setTitle", "后退前的编辑标题")
        page.t("addText", "后退前新增题", True)
        page.t("submit")
        paused = page.wait_held_put()

        # 浏览器后退到详情，再前进回编辑（同地址，但这是新一代表单，按服务器内容重绘）。
        page.t("back")
        page.wait_for(f"location.hash === '#/surveys/{survey_id}' && !!document.querySelector('.q-list')")
        page.t("forward")
        page.wait_for(
            f"location.hash === '#/surveys/{survey_id}/edit' && "
            "(document.getElementById('survey-title')||{}).value === '切换问卷'")

        # 在前进后打开的新表单中继续输入。
        page.t("setTitle", "前进后的编辑标题")
        page.t("addChoice", "前进后的单选题", ["前进甲", "前进乙"], False)
        expected = {
            "formHeading": f"编辑问卷草稿 #{survey_id}",
            "title": "前进后的编辑标题",
            "description": "旧说明",
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False, "options": []},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
                {"type": "single_choice", "title": "前进后的单选题", "required": False,
                 "options": ["前进甲", "前进乙"]},
            ],
        }

        page.release_to_server(paused)
        page.settle(0.8)

        snapshot = page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit",
                         "前进/后退切换后，旧保存成功仍把页面带去了详情")
        self.assert_form_state(snapshot, expected, "前进后的编辑表单")
        self.assert_no_save_feedback(snapshot, "前进后的编辑表单")

    # ---------- 未离开时：既有正常行为必须保留 ----------

    def test_normal_wait_success_navigates_to_detail_with_saved_content(self):
        """没有离开时：保存成功进入详情页，并显示保存后的标题、说明、题目、选项、必填。"""
        survey_id = self.seed_survey()
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("setTitle", "正常保存后的标题")
        page.t("setDesc", "正常保存后的说明")
        page.t("removeOption", 1, 2)
        self.page.t("setRequired", 0, True)
        page.t("addText", "正常新增文本题", False)
        page.t("submit")
        paused = page.wait_held_put()  # 先确认请求确实发出
        page.release_to_server(paused)

        page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        page.wait_for("!!document.querySelector('.q-list')")
        detail = page.detail()
        self.assertEqual(detail["heading"], f"#{survey_id} 正常保存后的标题")
        self.assertEqual(detail["description"], "正常保存后的说明")
        body = detail["bodyText"]
        self.assertIn("保留文本题", body)
        self.assertIn("必填", body)
        self.assertIn("原单选题", body)
        self.assertIn("正常新增文本题", body)
        self.assertIn("选项甲", body)
        self.assertIn("选项乙", body)
        self.assertNotIn("选项丙", body)
        self.assertFalse(detail["anyVisibleBanner"])

        status, saved = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200)
        self.assertEqual(saved["title"], "正常保存后的标题")
        self.assertEqual([q["title"] for q in saved["questions"]],
                         ["保留文本题", "原单选题", "正常新增文本题"])
        self.assertEqual([q["required"] for q in saved["questions"]], [True, True, False])
        self.assertEqual(saved["questions"][1]["options"], ["选项甲", "选项乙"])

    def test_normal_wait_validation_error_stays_and_keeps_all_edits(self):
        """没有离开时：400 校验失败停在编辑页、显示原因，新增/删除的题目与选项全部保留。"""
        survey_id = self.seed_survey()
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("setTitle", "校验失败时的标题")
        page.t("removeOption", 1, 2)        # 三选删成两选
        page.t("addOption", 1, "选项戊")
        page.t("setRequired", 0, True)
        page.t("addChoice", "待修正的新题", ["新选项一", "新选项二"], True)
        page.t("submit")
        paused = page.wait_held_put()

        page.fulfill_json(paused, 400, {"error": "第 3 题：服务端指出的具体校验问题"})
        page.wait_for("!document.getElementById('form-banner').hidden")

        snapshot = page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit",
                         "校验失败不应离开编辑页")
        self.assertTrue(snapshot["bannerVisible"])
        self.assertIn("第 3 题", snapshot["bannerText"])
        self.assertIn("服务端指出的具体校验问题", snapshot["bannerText"])
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snapshot["questions"]],
            [("text", "保留文本题", True, []),
             ("single_choice", "原单选题", True, ["选项甲", "选项乙", "选项戊"]),
             ("single_choice", "待修正的新题", True, ["新选项一", "新选项二"])],
            "校验失败后必须保留当前全部修改，包括新增题目与选项增删")
        self.assertEqual(snapshot["title"], "校验失败时的标题")
        self.assert_server_unchanged(survey_id, before)

    def test_normal_wait_network_error_stays_and_keeps_all_edits(self):
        """没有离开时：网络失败停在编辑页、显示网络错误，新增/删除的题目与选项全部保留。"""
        survey_id = self.seed_survey()
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        self.open_edit(survey_id)
        page = self.page
        page.hold_puts()
        page.t("setTitle", "网络失败时的标题")
        page.t("setDesc", "网络失败时的说明")
        page.t("removeQuestion", 0)        # 删掉第一道文本题
        page.t("addText", "网络失败期间新增题", True)
        page.t("submit")
        paused = page.wait_held_put()

        page.fail_as_network_error(paused)
        page.wait_for("!document.getElementById('form-banner').hidden")

        snapshot = page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit",
                         "网络失败不应离开编辑页")
        self.assertTrue(snapshot["bannerVisible"])
        self.assertIn("网络错误", snapshot["bannerText"])
        self.assertEqual(snapshot["title"], "网络失败时的标题")
        self.assertEqual(snapshot["description"], "网络失败时的说明")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snapshot["questions"]],
            [("single_choice", "原单选题", True, ["选项甲", "选项乙", "选项丙"]),
             ("text", "网络失败期间新增题", True, [])],
            "网络失败后必须保留删题与新增题目的当前状态")
        self.assert_server_unchanged(survey_id, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
