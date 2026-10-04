#!/usr/bin/env python3
"""编辑问卷草稿“内容加载”的浏览器回归保障。

test_survey_replace.py 只通过 HTTP 接口核对整份替换的落库结果；
test_edit_save_stale_response.py 保障的是“保存结果不得跨页面生效”。本文件改用
真实 Chrome 驱动真实页面，保护从问卷详情点击“编辑草稿”开始的**内容读取**：

- 进入编辑页、内容尚未返回时只显示加载提示，不出现可以填写或保存的空白表单；
  只有读取成功才显示带原稿的编辑表单。
- 加载成功后：标题、说明、题目顺序、题型、必填勾选、单选题全部选项次序均来自
  这份问卷；说明中的中文、引号、换行，题目标题与选项内部的换行原样保留；不会
  少题、少选项或套用默认空选项。打开编辑页本身不发起任何保存、不新增问卷。
- 读取因网络错误或服务端失败（非 2xx）未完成时：明确说明加载失败，提供
  “重试”和“返回详情”，不开放编辑表单，也不能把失败当成“没有题目”；服务器上
  已有草稿的标题、说明、题目、选项保持原样。
- 对不存在的编号显示“问卷不存在”，同样不进入空白表单、不创建记录。
- 失败后点击“重试”仍读取原来的问卷；读取恢复成功时，错误提示被完整编辑表单
  取代，显示已保存内容，编号与对应问卷不变。
- 加载结果只属于发起它的那一代表单：等待读取期间返回首页并在新建表单输入标题
- 与题目后，旧编辑页的读取结果才到达（无论成功还是失败），都不能把用户带回
  编辑页、覆盖当前输入或在首页插入旧的加载错误提示。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把编辑加载的
GET /api/surveys/{id} 挂起，等页面进入加载态（或用户已切换到首页继续输入）
后，再决定让它真实到达服务器（200/404）、伪造 500 响应，还是按网络错误失败。
断言全部基于用户可见的 DOM、表单值，以及再次读取接口得到的实际保存结果。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_edit_draft_load -v
"""
import base64
import json
import os
import re
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
from urllib.parse import urlsplit

APP = Path(os.environ.get("APP_UNDER_TEST",
                          Path(__file__).resolve().parent / "app.py"))
CHROME = os.environ.get("CHROME_BIN", "google-chrome")

# 旧编辑页的加载失败绝不能在切换后的首页上残留的提示语。
STALE_LOAD_ERROR_PHRASES = (
    "问卷内容加载失败", "未打开编辑表单", "重试", "返回详情", "不存在",
)


# --------------------------------------------------------------------------
# 真实 app.py 服务进程（与其它浏览器回归测试同款夹具）
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-load-")
        self.process = None
        self.base_url = None

    def start(self):
        # app.py 每次请求都会向 stderr 写访问日志；PIPE 不读会写满导致单线程
        # 服务阻塞，因此自行选定端口并把子进程输出直接丢弃。
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
      const el = row.querySelector('.opt-text');
      el.value = options[i]; fire(el);
    });
  };
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
})();
"""

# 编辑页加载态 / 失败态快照——页面上还没有表单，全部来自用户可见的 DOM。
LOAD_JS = r"""
(() => {
  const status = document.getElementById('edit-status');
  const form = document.getElementById('draft-form');
  const heading = document.querySelector('h2');
  const buttons = [...document.querySelectorAll('button')].map(b => b.textContent.trim());
  return {
    hash: location.hash,
    heading: heading ? heading.textContent : null,
    statusText: status ? status.textContent : null,
    hasForm: !!form,
    cardCount: document.querySelectorAll('.q-card').length,
    hasTitleInput: !!document.getElementById('survey-title'),
    submitPresent: !!document.querySelector('#draft-form button[type=submit]'),
    addQuestionButtons: buttons.filter(t => t.indexOf('添加') !== -1),
    saveButtons: buttons.filter(t => t.indexOf('保存') !== -1),
    anyVisibleBanner: !!document.querySelector('.banner:not([hidden])'),
    bannerText: [...document.querySelectorAll('.banner')].map(b => b.textContent).join('\n'),
    buttons,
    links: [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href')),
    bodyText: document.body.innerText,
  };
})()
"""

# 加载成功后表单状态快照——标题、说明、题目顺序、题型、必填、选项全部取表单值。
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
  status: (document.getElementById('detail-status') || {}).textContent || null,
  bodyText: document.body.innerText,
}))()
"""

DETAIL_GET_RE = re.compile(r"^/api/surveys/\d+$")


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
        self.held = []          # 被挂起的详情 GET
        self.observed = []      # 观察到的全部 /api/surveys 请求（method, path）
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，只有读取详情的 GET
        # （/api/surveys/{id}，编辑加载与重试都走它）会被挂起；列表 GET、静态
        # 资源以及任何 POST/PUT 都立即放行，否则切换到的首页会被自己的请求饿死。
        self._pumping = True
        threading.Thread(target=self._event_loop, daemon=True).start()

    def _event_loop(self):
        while self._pumping:
            events = self.ws.drain_events(0.2)
            passthrough = []
            for event in events:
                # 多个标签页的事件泵共用一条 WebSocket：只处理属于本会话的
                # 事件，否则旧标签页收尾时会用失效 session 错误放行新标签页
                # 挂起的请求，或把新页面的 requestPaused 吞掉导致永久挂起。
                if event.get("sessionId") != self.session:
                    passthrough.append(event)
                    continue
                if event.get("method") == "Fetch.requestPaused":
                    params = event["params"]
                    request = params["request"]
                    path = urlsplit(request["url"]).path
                    with self._held_cond:
                        self.observed.append((request["method"], path))
                    if request["method"] == "GET" and DETAIL_GET_RE.match(path):
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
        queued = self.ws.drain_events(0)
        if queued:
            self.ws.push_events(queued)
        self.call("Page.navigate", {"url": url})
        end = time.time() + 10
        while time.time() < end:
            # 只取本页的加载完成事件；其它事件（尤其 Fetch.requestPaused）
            # 必须放回队列交给后台事件泵处理，否则该请求会永久暂停。
            events = self.ws.drain_events(0.2)
            mine, others = [], []
            for event in events:
                if (event.get("sessionId") == self.session
                        and event.get("method") == "Page.loadEventFired"):
                    mine.append(event)
                else:
                    others.append(event)
            if others:
                self.ws.push_events(others)
            if mine:
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

    def settle(self, seconds=0.7):
        """让微任务/异步回调有机会落地。"""
        time.sleep(seconds)

    # ---------- 详情 GET 拦截 ----------

    def hold_gets(self):
        """挂起所有 GET /api/surveys/{id}；其它请求由后台事件循环立即放行。"""
        with self._held_cond:
            self.held = []
        self.observed = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def wait_held_get(self, timeout=8):
        end = time.time() + timeout
        with self._held_cond:
            while not self.held and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held:
                return self.held.pop(0)
        raise AssertionError("内容读取请求未发出（详情 GET 未被挂起）")

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

    def observed_requests(self):
        with self._held_cond:
            return list(self.observed)

    # ---------- 页面状态读取 ----------

    def load_state(self):
        return self.eval(LOAD_JS)

    def snapshot(self):
        return self.eval(SNAPSHOT_JS)

    def detail_state(self):
        return self.eval(DETAIL_JS)


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

class EditDraftLoadTests(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        if shutil.which(CHROME) is None and not Path(CHROME).exists():
            raise unittest.SkipTest(
                f"未找到浏览器（{CHROME}），跳过需要真实浏览器的回归测试；"
                "可用环境变量 CHROME_BIN 指定可执行文件路径。")
        cls.server = SurveyServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.stop()

    def setUp(self):
        # 每个用例使用独立 Chrome：长生命周期共享浏览器在标签页/挂起请求累积后
        # 会拖垮自身与单线程服务器（本环境实测会级联超时）。独立实例把用例
        # 完全隔离，代价只是每个用例多花约一秒启动浏览器。
        self.browser = ChromeBrowser()
        self.page = self.browser.new_page()

    def tearDown(self):
        # 先放行本用例仍挂起的详情 GET，释放被占用的服务器连接，再关页面/浏览器。
        try:
            for paused in list(self.page.held):
                try:
                    self.page.release_to_server(paused)
                except Exception:
                    pass
        finally:
            self.page.stop_holding()
            self.page.close()
            self.browser.close()

    # ---------- 夹具与断言辅助 ----------

    def api(self, method, path, body=None):
        return self.server.request(method, path, body)

    def survey_ids(self):
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, data)
        return {item["id"] for item in data["surveys"]}

    def seed_survey(self, payload):
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data["id"], data

    def standard_survey(self, title="旧标题", description="旧说明"):
        return self.seed_survey({
            "title": title,
            "description": description,
            "questions": [
                {"type": "text", "title": "保留文本题", "required": False},
                {"type": "single_choice", "title": "原单选题", "required": True,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        })

    def rich_survey(self):
        """同时含文本题/单选题、必填/选填、中文引号换行、题内换行的草稿。"""
        return self.seed_survey({
            "title": "  问卷标题\n第二行  ",
            "description": "  说明第一行\n中文与\"引号\"、引号“弯”\n第三行  ",
            "questions": [
                {"type": "text", "title": " 必填文本题\n标题第二行 ", "required": True},
                {"type": "single_choice", "title": "选填单选题", "required": False,
                 "options": ["选项甲\n第二行", "选项乙", " 选项丙 "]},
                {"type": "single_choice", "title": "必填单选题\n标题两行", "required": True,
                 "options": ["一", "二", "三\n换行", "四"]},
            ],
        })

    def open_home(self):
        self.page.open(f"{self.server.base_url}/#/")
        self.page.wait_for("!!document.querySelector('#draft-form')")

    def open_detail_through_links(self, survey_id):
        """首页 → 问卷详情，全部走真实链接。"""
        self.open_home()
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_id}\"]')")
        self.page.t("clickHref", f"#/surveys/{survey_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}' && "
            "!!document.querySelector('.q-list')")

    def open_edit_direct_held(self, survey_id):
        """直接打开编辑页并停在加载态，返回被挂起的详情 GET。"""
        self.page.hold_gets()
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}/edit")
        self.wait_loading()
        return self.page.wait_held_get()

    def click_edit_and_wait_loading(self, survey_id):
        """在详情页点真实的“编辑草稿”按钮并停在加载态。"""
        self.page.hold_gets()
        self.page.t("clickHref", f"#/surveys/{survey_id}/edit")
        self.wait_loading()
        return self.page.wait_held_get()

    def wait_loading(self):
        self.page.wait_for(
            "((document.getElementById('edit-status')||{}).textContent || '').indexOf('加载中') !== -1")

    def wait_form_loaded(self, survey_id):
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}/edit' && "
            "!!document.querySelector('#draft-form .q-card') && "
            "document.getElementById('survey-title').value !== ''")

    def assert_loading_not_blank_form(self, state):
        """内容未返回：只有加载提示，没有任何可填写或可保存的表单。"""
        self.assertIn("加载中", state["statusText"] or "",
                      "等待内容时必须显示加载提示")
        self.assertFalse(state["hasForm"], "加载未完成就出现了编辑表单")
        self.assertFalse(state["hasTitleInput"], "加载未完成就出现了标题输入框")
        self.assertEqual(state["cardCount"], 0, "加载未完成就出现了题目卡片")
        self.assertFalse(state["submitPresent"], "加载未完成就出现了保存按钮")
        self.assertEqual(state["saveButtons"], [], "加载未完成就出现了保存入口")
        self.assertEqual(state["addQuestionButtons"], [],
                         "加载未完成就出现了添加题目按钮")
        self.assertFalse(state["anyVisibleBanner"], "加载态不应出现错误提示条")

    def assert_error_state(self, state, survey_id, *, phrases):
        """加载失败：明确说明失败、提供重试与返回详情，绝不开放编辑表单。"""
        self.assertEqual(state["hash"], f"#/surveys/{survey_id}/edit",
                         "加载失败不应离开编辑地址")
        self.assertEqual(state["heading"], f"编辑问卷草稿 #{survey_id}")
        self.assertIsNone(state["statusText"], "进入失败态后加载提示应被替换")
        self.assertTrue(state["anyVisibleBanner"], "加载失败必须显示明确的错误提示")
        self.assertIn("问卷内容加载失败", state["bannerText"])
        for phrase in phrases:
            self.assertIn(phrase, state["bodyText"],
                          f"失败页缺少说明“{phrase}”")
        self.assertIn("重试", state["buttons"], "失败页必须提供“重试”按钮")
        self.assertIn(f"#/surveys/{survey_id}", state["links"],
                      "失败页必须提供“返回详情”链接")
        self.assertIn("#/", state["links"], "失败页必须保留返回首页入口")
        self.assertFalse(state["hasForm"], "加载失败不能打开编辑表单")
        self.assertFalse(state["hasTitleInput"], "加载失败不能出现标题输入框")
        self.assertEqual(state["cardCount"], 0, "加载失败不能显示题目卡片")
        self.assertEqual(state["saveButtons"], [], "加载失败不能出现保存入口")
        self.assertEqual(state["addQuestionButtons"], [], "加载失败不能添加题目")

    def assert_form_matches_survey(self, snapshot, survey):
        """表单中的原稿必须与已保存问卷逐字段一致。"""
        survey_id = survey["id"]
        self.assertEqual(snapshot["formHeading"], f"编辑问卷草稿 #{survey_id}")
        self.assertFalse(snapshot["bannerVisible"])
        self.assertEqual(snapshot["title"], survey["title"],
                         "标题必须来自已保存问卷（含内部换行）")
        self.assertEqual(snapshot["description"], survey["description"],
                         "说明必须原样来自已保存问卷（中文、引号、换行、首尾空白）")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], tuple(q["options"]))
             for q in snapshot["questions"]],
            [(q["type"], q["title"], q["required"], tuple(q["options"]))
             for q in survey["questions"]],
            "题目顺序、题型、必填勾选与选项次序必须来自已保存问卷",
        )
        for card, question in zip(snapshot["questions"], survey["questions"]):
            if question["type"] == "single_choice":
                self.assertEqual(card["options"], question["options"],
                                 "单选题必须显示全部已保存选项，不能套用默认空选项")
                self.assertTrue(all(opt != "" for opt in card["options"]),
                                 "已保存选项不能变成空白默认值")

    def assert_server_survey(self, survey_id, expected):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data, expected, "服务器上的草稿必须保持保存时的原样")

    def assert_only_gets_observed(self, survey_id, where):
        """打开/重试编辑页只能发出读取请求，绝不能保存或新增。"""
        requests = self.page.observed_requests()
        self.assertTrue(requests, f"{where}：没有观察到内容读取请求")
        for method, path in requests:
            self.assertEqual(method, "GET",
                             f"{where}：打开编辑页不应发出 {method} 保存请求（{path}）")
            self.assertEqual(path, f"/api/surveys/{survey_id}",
                             f"{where}：编辑加载读取了错误的地址（{path}）")

    # ---------- 加载中：提示而非空白表单，且打开编辑不保存 ----------

    def test_clicking_edit_from_detail_shows_loading_without_blank_form(self):
        """从详情点“编辑草稿”后只显示加载提示，不出现可填写/保存的空白表单。"""
        survey_id, saved = self.standard_survey()
        ids_before = self.survey_ids()
        self.open_detail_through_links(survey_id)

        paused = self.click_edit_and_wait_loading(survey_id)
        state = self.page.load_state()
        self.assertEqual(state["hash"], f"#/surveys/{survey_id}/edit")
        self.assert_loading_not_blank_form(state)
        # 加载期间服务器上没有任何新增或修改。
        self.assertEqual(self.survey_ids(), ids_before,
                         "等待加载期间不应新增问卷")
        self.assert_server_survey(survey_id, saved)

        self.page.release_to_server(paused)
        self.wait_form_loaded(survey_id)
        # 整个“进入编辑页”过程只读取了原问卷，没有任何 POST/PUT。
        self.assert_only_gets_observed(survey_id, "进入编辑页")
        self.assertEqual(self.survey_ids(), ids_before,
                         "打开编辑页本身不应新增问卷")
        self.assert_server_survey(survey_id, saved)

    # ---------- 加载成功：完整原稿进入表单 ----------

    def test_loaded_form_shows_saved_content_exactly(self):
        """文本题/单选题、必填/选填、顺序、全部选项、中文引号换行均按原稿显示。"""
        survey_id, saved = self.rich_survey()
        ids_before = self.survey_ids()

        paused = self.open_edit_direct_held(survey_id)
        self.page.release_to_server(paused)
        self.wait_form_loaded(survey_id)

        snapshot = self.page.snapshot()
        self.assert_form_matches_survey(snapshot, saved)

        # 题型标签按原稿呈现；必填状态以勾选框为准（已在上面逐题比对）。
        body = snapshot["bodyText"]
        self.assertIn("文本题", body)
        self.assertIn("单选题", body)
        required_checked = self.page.eval(
            "[...document.querySelectorAll('.q-card .q-required')].map(c => c.checked)")
        self.assertEqual(required_checked, [True, False, True],
                         "必填勾选必须逐题来自已保存问卷")
        # 三道题、两道单选题分别带 3、4 个选项，没有少题少项或套用默认空选项。
        self.assertEqual(len(snapshot["questions"]), 3)
        self.assertEqual(len(snapshot["questions"][1]["options"]), 3)
        self.assertEqual(len(snapshot["questions"][2]["options"]), 4)

        # 说明、题目标题、选项中的换行必须真实保留在表单值里。
        self.assertIn("\n", snapshot["description"])
        self.assertIn('"', snapshot["description"])
        self.assertIn("\n", snapshot["questions"][0]["title"])
        self.assertIn("\n", snapshot["questions"][1]["options"][0])
        self.assertIn("\n", snapshot["questions"][2]["options"][2])

        # 读取不改数据：服务器内容逐字段不变，列表不增加。
        self.assert_server_survey(survey_id, saved)
        self.assertEqual(self.survey_ids(), ids_before)
        self.assert_only_gets_observed(survey_id, "加载原稿")

    # ---------- 加载失败：网络错误 ----------

    def test_network_error_shows_retry_and_back_without_form_or_data_change(self):
        """网络错误：明确提示加载失败、提供重试/返回详情，不进表单，草稿原样保留。"""
        survey_id, saved = self.standard_survey()
        before_list = self.survey_ids()

        paused = self.open_edit_direct_held(survey_id)
        self.page.fail_as_network_error(paused)
        self.page.wait_for("!!document.querySelector('.banner:not([hidden])')")

        state = self.page.load_state()
        self.assert_error_state(
            state, survey_id, phrases=["网络错误", "重试", "返回详情"])
        # 失败不能被当成“问卷没有题目”：服务器草稿原样，列表不增不减。
        self.assert_server_survey(survey_id, saved)
        self.assertEqual(self.survey_ids(), before_list)

    # ---------- 加载失败：服务端返回失败 ----------

    def test_server_error_shows_retry_and_back_without_form_or_data_change(self):
        """服务端 500：明确提示加载失败、提供重试/返回详情，不进表单，草稿原样保留。"""
        survey_id, saved = self.standard_survey()
        before_list = self.survey_ids()

        paused = self.open_edit_direct_held(survey_id)
        self.page.fulfill_json(paused, 500, {"error": "模拟的服务端异常"})
        self.page.wait_for("!!document.querySelector('.banner:not([hidden])')")

        state = self.page.load_state()
        self.assert_error_state(
            state, survey_id, phrases=["HTTP 500", "重试", "返回详情"])
        self.assert_server_survey(survey_id, saved)
        self.assertEqual(self.survey_ids(), before_list)

    # ---------- 不存在的编号 ----------

    def test_missing_survey_shows_not_found_without_form_or_record(self):
        """不存在的编号：提示问卷不存在，可重试/返回详情，不进空白表单也不建记录。"""
        existing_ids = self.survey_ids()
        unknown_id = max(existing_ids, default=0) + 999_999
        before_list = self.survey_ids()

        paused = self.open_edit_direct_held(unknown_id)
        # 让请求真实到达服务器，由服务器给出真实的 404。
        self.page.release_to_server(paused)
        self.page.wait_for("!!document.querySelector('.banner:not([hidden])')")

        state = self.page.load_state()
        self.assert_error_state(
            state, unknown_id, phrases=[f"问卷 #{unknown_id} 不存在", "重试", "返回详情"])

        # 重试仍读取同一编号，服务器依然 404：不能借机创建记录。
        self.page.t("clickBtn", "重试")
        retry_paused = self.page.wait_held_get()
        self.assertEqual(
            urlsplit(retry_paused["request"]["url"]).path,
            f"/api/surveys/{unknown_id}", "重试必须读取原来的问卷编号")
        self.page.release_to_server(retry_paused)
        self.page.wait_for(
            "((document.querySelector('.banner') || {}).textContent || '').indexOf('不存在') !== -1")
        state_after = self.page.load_state()
        self.assertFalse(state_after["hasForm"], "404 重试后仍不能进入空白表单")
        self.assertIn(f"问卷 #{unknown_id} 不存在", state_after["bodyText"])

        # “返回详情”进入的详情页同样提示不存在，而不是空白内容。
        self.page.t("clickHref", f"#/surveys/{unknown_id}")
        detail_paused = self.page.wait_held_get()
        self.assertEqual(
            urlsplit(detail_paused["request"]["url"]).path,
            f"/api/surveys/{unknown_id}")
        self.page.release_to_server(detail_paused)
        self.page.wait_for(
            f"location.hash === '#/surveys/{unknown_id}' && "
            "((document.getElementById('detail-status')||{}).textContent || '').indexOf('不存在') !== -1")
        detail = self.page.detail_state()
        self.assertIn(f"问卷 #{unknown_id} 不存在", detail["bodyText"])

        status, _ = self.api("GET", f"/api/surveys/{unknown_id}")
        self.assertEqual(status, 404, "失败的编辑加载不能创建问卷记录")
        self.assertEqual(self.survey_ids(), before_list)

    # ---------- 失败后重试：读取恢复后错误被完整表单取代 ----------

    def test_retry_after_failure_reads_same_survey_and_replaces_error_with_form(self):
        """先网络失败，恢复后点重试：仍读原问卷，错误提示被带原稿的表单完整取代。"""
        survey_id, saved = self.standard_survey()

        paused = self.open_edit_direct_held(survey_id)
        self.page.fail_as_network_error(paused)
        self.page.wait_for("!!document.querySelector('.banner:not([hidden])')")
        self.assertFalse(self.page.load_state()["hasForm"])

        # 重试必须重新读取原来的问卷编号（同地址、同 id）。
        self.page.t("clickBtn", "重试")
        retry_paused = self.page.wait_held_get()
        self.assertEqual(
            urlsplit(retry_paused["request"]["url"]).path,
            f"/api/surveys/{survey_id}", "重试读取的问卷编号发生了变化")
        self.page.release_to_server(retry_paused)
        self.wait_form_loaded(survey_id)

        state = self.page.load_state()
        # 读取成功后：失败提示与加载提示都消失，编辑表单完整接管。
        self.assertFalse(state["anyVisibleBanner"], "恢复成功后旧错误提示必须消失")
        self.assertNotIn("重试", state["buttons"])
        self.assertIsNone(state["statusText"])
        self.assertTrue(state["hasForm"], "恢复成功后必须显示完整编辑表单")

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], f"#/surveys/{survey_id}/edit",
                         "重试成功不能改变编号或地址")
        self.assert_form_matches_survey(snapshot, saved)
        # 编号与对应问卷不变，内容从头到尾未被修改。
        self.assert_server_survey(survey_id, saved)

    # ---------- 边界：等待加载期间返回首页，旧结果不得跨页面生效 ----------

    def _leave_to_home_and_fill_create_form(self):
        """加载挂起时经 hash 路由返回首页（与点链接走同一路由）并填写新建表单。"""
        page = self.page
        page.eval("location.hash = '#/'")
        page.wait_for("location.hash === '#/' && !!document.querySelector('#draft-form')")
        page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent === '新建问卷草稿'")
        page.t("setTitle", "首页新建的问卷")
        page.t("setDesc", "首页新建说明\n带换行")
        page.t("addText", "首页必填文本题", True)
        page.t("addChoice", "首页单选题", ["首页选项甲", "首页选项乙"], False)
        return {
            "formHeading": "新建问卷草稿",
            "title": "首页新建的问卷",
            "description": "首页新建说明\n带换行",
            "questions": [
                {"type": "text", "title": "首页必填文本题", "required": True, "options": []},
                {"type": "single_choice", "title": "首页单选题", "required": False,
                 "options": ["首页选项甲", "首页选项乙"]},
            ],
        }

    def _assert_home_untouched(self, expected, where):
        self.page.settle(0.8)
        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], "#/",
                         f"{where}：旧编辑页的读取结果把用户带回了编辑页")
        self.assertEqual(snapshot["formHeading"], "新建问卷草稿",
                         f"{where}：首页新建表单被编辑页内容替换")
        self.assertEqual(snapshot["title"], expected["title"],
                         f"{where}：首页已输入的标题被覆盖")
        self.assertEqual(snapshot["description"], expected["description"],
                         f"{where}：首页已输入的说明被覆盖")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snapshot["questions"]],
            [(q["type"], q["title"], q["required"], q["options"])
             for q in expected["questions"]],
            f"{where}：首页已输入的题目/选项/必填被覆盖")
        self.assertFalse(snapshot["anyVisibleBanner"],
                         f"{where}：首页冒出了旧编辑页的错误提示条")
        for phrase in STALE_LOAD_ERROR_PHRASES:
            self.assertNotIn(
                phrase, snapshot["bodyText"],
                f"{where}：首页可见文本出现了旧编辑页的提示“{phrase}”")
        # 再观察一拍，确认没有延迟的跳转、改写或插入提示。
        self.page.settle(0.4)
        self.assertEqual(self.page.eval("location.hash"), "#/")
        snapshot2 = self.page.snapshot()
        self.assertEqual(snapshot2["formHeading"], "新建问卷草稿")
        self.assertEqual(snapshot2["title"], expected["title"])
        self.assertEqual(
            [q["title"] for q in snapshot2["questions"]],
            [q["title"] for q in expected["questions"]])
        self.assertFalse(snapshot2["anyVisibleBanner"])

    def test_successful_load_after_leaving_to_home_is_ignored(self):
        """等待加载时回首页填新建表单，旧读取成功不能带回编辑页或覆盖输入。"""
        survey_id, saved = self.standard_survey()
        ids_before = self.survey_ids()
        paused = self.open_edit_direct_held(survey_id)

        expected = self._leave_to_home_and_fill_create_form()
        self.page.release_to_server(paused)
        self._assert_home_untouched(expected, "旧读取成功后")

        # 旧读取只是 GET：被丢弃的响应不改变任何已保存内容。
        self.assert_server_survey(survey_id, saved)
        self.assertEqual(self.survey_ids(), ids_before)

    def test_network_failed_load_after_leaving_to_home_is_ignored(self):
        """等待加载时回首页填新建表单，旧读取网络失败不能在首页插入错误提示。"""
        survey_id, saved = self.standard_survey()
        ids_before = self.survey_ids()
        paused = self.open_edit_direct_held(survey_id)

        expected = self._leave_to_home_and_fill_create_form()
        self.page.fail_as_network_error(paused)
        self._assert_home_untouched(expected, "旧读取网络失败后")

        self.assert_server_survey(survey_id, saved)
        self.assertEqual(self.survey_ids(), ids_before)

    def test_server_failed_load_after_leaving_to_home_is_ignored(self):
        """等待加载时回首页填新建表单，旧读取返回 500 同样不能跨页面提示或改写。"""
        survey_id, saved = self.standard_survey()
        ids_before = self.survey_ids()
        paused = self.open_edit_direct_held(survey_id)

        expected = self._leave_to_home_and_fill_create_form()
        self.page.fulfill_json(paused, 500, {"error": "模拟的服务端异常"})
        self._assert_home_untouched(expected, "旧读取服务端失败后")

        self.assert_server_survey(survey_id, saved)
        self.assertEqual(self.survey_ids(), ids_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
