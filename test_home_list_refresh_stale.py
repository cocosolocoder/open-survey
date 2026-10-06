#!/usr/bin/env python3
"""首页问卷列表“创建草稿后刷新”的浏览器回归保障。

test_create_save_continue_editing.py 保障首次创建等待期间继续编辑的表单行为；
本文件保护的是同一过程中**首页列表区域**的既有行为，核心是“较早发起的读取
不能覆盖较晚发起的读取”：首页打开时发起一次列表读取，新建草稿创建成功后又会
刷新列表；两次读取的返回先后可能与发起先后相反，只有最新发起的那次读取允许
更新列表。以防以后把实现改回“谁最后返回就用谁”。

保护的行为（全部以真实 Chrome 中用户可见的 DOM、表单值与接口结果为断言依据）：

- 初次列表读取尚未返回时，可以填写包含文本题与单选题的有效草稿并保存；等待
  保存结果期间继续修改标题或说明的，创建成功后仍留在当前表单。随后的刷新
  成功时，列表显示服务器返回的问卷列表：新草稿使用创建成功时的编号与已保存
  标题（不能拿表单里尚未保存的新标题替换），列表按编号升序，点击新草稿条目
  可以打开对应详情。
- 初次读取再返回创建前的旧列表（或空列表、或网络错误）时，页面保留最新刷新
  得到的列表：新草稿不能消失、不能多出一条，也不能被改成“还没有问卷记录”
  或“问卷列表加载失败”。以读取的发起顺序确定哪次结果有效，与返回先后无关。
- 当前有效读取的正常提示保留：首次打开尚无问卷时显示空列表提示；创建后的
  最新刷新网络错误时显示列表加载失败提示，且不能因较早的读取随后成功而把
  失败提示换成旧列表。
- 列表变化只影响列表区域：用户等待期间继续输入的标题、说明、题目、选项与
  必填设置不被清空、恢复或保存；用户离开首页后旧读取才返回的，不把当前
  详情或编辑页替换成列表；创建成功后的详情跳转与草稿内容保存规则保持不变。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把发往
/api/surveys 的 GET（列表读取）与 POST（首次创建）按发起顺序挂起，由测试
决定每一次以何种结果、何种顺序返回（放行到真实服务器、伪造旧列表 JSON、
或按网络错误失败）；详情等其它请求一律立即放行。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_home_list_refresh_stale -v
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
from urllib.parse import urlsplit

APP = Path(os.environ.get("APP_UNDER_TEST",
                          Path(__file__).resolve().parent / "app.py"))
CHROME = os.environ.get("CHROME_BIN", "google-chrome")


# --------------------------------------------------------------------------
# 真实 app.py 服务进程（与其它浏览器回归测试同款夹具）
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-home-list-")
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
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
})();
"""

# 表单区域快照——全部来自用户可见的 DOM 与表单值。
SNAPSHOT_JS = r"""
(() => {
  const banner = document.getElementById('form-banner');
  const titleEl = document.getElementById('survey-title');
  const descEl = document.getElementById('survey-desc');
  const formHeading = document.querySelector('#draft-form h2');
  const statusBar = document.getElementById('save-status');
  return {
    hash: location.hash,
    formHeading: formHeading ? formHeading.textContent : null,
    bannerVisible: banner ? !banner.hidden : false,
    bannerText: banner ? banner.textContent : '',
    saveStatusVisible: statusBar ? !statusBar.hidden : false,
    savedNote: !!document.getElementById('save-note-saved'),
    dirtyNote: !!document.getElementById('save-note-dirty'),
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

# 列表区域快照：条目文本与链接目标，都来自用户可见的 DOM。
LIST_JS = r"""
(() => {
  const list = document.getElementById('survey-list');
  return {
    hash: location.hash,
    hasList: !!list,
    items: list ? [...list.querySelectorAll('li')].map(li => li.innerText) : null,
    links: list ? [...list.querySelectorAll('a[href]')]
        .map(a => a.getAttribute('href')) : null,
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
  hasList: !!document.getElementById('survey-list'),
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
        self.held = []          # 被挂起的列表 GET / 创建 POST，按发起顺序排列
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        # 关闭 HTTP 缓存：同一浏览器配置跨用例共享，若不关闭，后跑的用例打开
        # 首页时的初次列表读取可能直接命中缓存，不产生可拦截的网络请求。
        self.call("Network.enable")
        self.call("Network.setCacheDisabled", {"cacheDisabled": True})
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，非列表/创建请求（详情、静态资源
        # 等）必须立即放行，否则详情页会因自己的 GET 被挂起而饿死；列表读取与
        # 创建请求才收集起来交给测试决定何时、以何种结果返回。
        self._pumping = True
        threading.Thread(target=self._event_loop, daemon=True).start()

    @staticmethod
    def _is_list_or_create(request):
        return (urlsplit(request["url"]).path == "/api/surveys"
                and request["method"] in ("GET", "POST"))

    def _event_loop(self):
        while self._pumping:
            events = self.ws.drain_events(0.2)
            passthrough = []
            for event in events:
                if event.get("method") == "Fetch.requestPaused":
                    params = event["params"]
                    if self._is_list_or_create(params["request"]):
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
            leftover = []
            loaded = False
            for event in self.ws.drain_events(0.2):
                if event.get("method") == "Page.loadEventFired":
                    loaded = True
                else:
                    # 导航期间首页就会发起初次列表读取，其 Fetch.requestPaused
                    # 事件不能在这里丢弃，放回队列交给后台事件循环挂起。
                    leftover.append(event)
            if leftover:
                self.ws.push_events(leftover)
            if loaded:
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

    # ---------- 列表读取 / 创建请求拦截 ----------

    def hold_reads(self):
        """挂起发往 /api/surveys 的 GET（列表读取）与 POST（首次创建）；
        详情等其它请求由后台事件循环立即放行。"""
        with self._held_cond:
            self.held = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def pop_held(self, kind, timeout=8):
        """按发起顺序取出被挂起的请求：kind 为 "list"（GET）或 "create"（POST）。"""
        method = {"list": "GET", "create": "POST"}[kind]
        end = time.time() + timeout
        with self._held_cond:
            while time.time() < end:
                for index, paused in enumerate(self.held):
                    if paused["request"]["method"] == method:
                        return self.held.pop(index)
                self._held_cond.wait(max(0.0, end - time.time()))
        raise AssertionError(f"请求未发出（{kind} 未被挂起）")

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

    def listing(self):
        return self.eval(LIST_JS)

    def detail(self):
        return self.eval(DETAIL_JS)


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

# 等待创建结果期间继续输入后的表单内容（列表如何变化都不允许动它）。
PENDING_TITLE = "等待期间改的新标题"
PENDING_DESC = "等待期间改的新说明"
PENDING_QUESTIONS = [
    ("text", "文本题一", True, []),
    ("single_choice", "单选题一", False, ["选项甲", "选项乙"]),
]


class HomeListRefreshTests(unittest.TestCase):
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

    def seed_survey(self, title="已有问卷"):
        status, data = self.api("POST", "/api/surveys", {
            "title": title,
            "description": "已有说明",
            "questions": [
                {"type": "text", "title": "已有文本题", "required": False},
            ],
        })
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data["id"]

    def open_home_holding_initial_read(self):
        """打开首页并挂起初次列表读取；返回被挂起的初次读取。"""
        self.page.hold_reads()
        self.page.open(f"{self.server.base_url}/#/")
        self.page.wait_for("!!document.querySelector('#draft-form')")
        return self.page.pop_held("list")

    def fill_and_submit_draft(self, title="创建时的标题", description="创建时的说明"):
        """在首页新建表单填写文本题 + 单选题的有效草稿并提交，返回被挂起的 POST。"""
        self.page.t("setTitle", title)
        self.page.t("setDesc", description)
        self.page.t("addText", "文本题一", True)
        self.page.t("addChoice", "单选题一", ["选项甲", "选项乙"], False)
        self.page.t("submit")
        return self.page.pop_held("create")

    def begin_create_while_initial_read_held(self):
        """首页初次读取挂起期间提交草稿、等待期间改标题/说明、创建成功。

        返回 (创建前的旧列表, 新编号, 初次读取, 刷新读取)。
        """
        # 创建前服务器上的真实列表，稍后作为“初次读取返回的旧列表”原样回放。
        status, before = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, before)
        old_list = before["surveys"]

        initial = self.open_home_holding_initial_read()
        create = self.fill_and_submit_draft()
        # 等待保存结果期间继续修改标题与说明：创建成功后应留在当前表单。
        self.page.t("setTitle", PENDING_TITLE)
        self.page.t("setDesc", PENDING_DESC)

        self.page.release_to_server(create)
        # 创建成功触发刷新读取；表单就地转为编辑刚创建的那份草稿。
        refresh = self.page.pop_held("list")
        self.page.wait_for("!!document.getElementById('save-note-saved')")
        heading = self.page.eval(
            "document.querySelector('#draft-form h2').textContent")
        new_id = int(heading.rsplit("#", 1)[1])
        return old_list, new_id, initial, refresh

    def release_refresh_and_assert_list(self, refresh, old_list, new_id):
        """放行刷新读取并核对列表：按编号升序、新草稿用已保存标题。"""
        self.page.release_to_server(refresh)
        self.page.wait_for(
            "!!document.querySelector("
            f"'#survey-list a[href=\"#/surveys/{new_id}\"]')")
        expected = old_list + [{"id": new_id, "title": "创建时的标题"}]
        listing = self.page.listing()
        self.assertEqual(
            listing["items"],
            [f"#{survey['id']} {survey['title']}" for survey in expected],
            "刷新后的列表必须就是服务器返回的列表（按编号升序）")
        self.assertEqual(
            listing["links"],
            [f"#/surveys/{survey['id']}" for survey in expected],
            "列表条目的链接必须指向对应编号的详情")
        for item in listing["items"]:
            self.assertNotIn(
                PENDING_TITLE, item,
                "列表不能拿表单里尚未保存的新标题替换已保存的标题")
        return listing

    def assert_pending_edits_preserved(self, where):
        """列表区域的任何变化都不得动用户等待期间继续输入的内容。"""
        snap = self.page.snapshot()
        self.assertEqual(snap["title"], PENDING_TITLE, f"{where}：标题被改写")
        self.assertEqual(snap["description"], PENDING_DESC, f"{where}：说明被改写")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snap["questions"]],
            PENDING_QUESTIONS,
            f"{where}：题目/选项/必填设置被列表变化改写")
        self.assertTrue(snap["saveStatusVisible"], f"{where}：保存状态条被隐藏")
        self.assertTrue(snap["savedNote"], f"{where}：丢了“已保存”提示")
        self.assertTrue(snap["dirtyNote"], f"{where}：丢了“未保存修改”提示")
        self.assertFalse(snap["bannerVisible"], f"{where}：表单冒出了错误提示条")

    # ---------- 当前有效读取的正常提示 ----------

    def test_initial_empty_list_shows_empty_hint(self):
        """首次打开尚无问卷的首页：列表读取成功后显示现有的空列表提示。"""
        # 空列表提示要求数据库确实为空；本类共享的服务器已被其它用例写入，
        # 因此这里单独起一个全新服务。
        server = SurveyServer()
        server.start()
        try:
            self.page.hold_reads()
            self.page.open(f"{server.base_url}/#/")
            self.page.wait_for("!!document.querySelector('#draft-form')")
            initial = self.page.pop_held("list")
            self.page.release_to_server(initial)
            self.page.wait_for(
                "document.getElementById('survey-list').innerText"
                ".indexOf('还没有问卷记录') !== -1")
            listing = self.page.listing()
            self.assertEqual(listing["items"], ["还没有问卷记录。"])
            self.assertEqual(listing["links"], [])
        finally:
            server.stop()

    # ---------- 创建后刷新成功：较早的初次读取不得覆盖 ----------

    def test_refresh_shows_saved_title_and_stale_old_list_is_ignored(self):
        """刷新成功显示服务器列表；初次读取再返回旧列表不能覆盖它。"""
        self.seed_survey(title="已有问卷")
        (old_list, new_id, initial, refresh) = \
            self.begin_create_while_initial_read_held()

        # 较晚发起的刷新先返回：列表显示服务器返回的内容。
        listing = self.release_refresh_and_assert_list(refresh, old_list, new_id)
        expected_items = listing["items"]
        self.assert_pending_edits_preserved("刷新成功后")

        # 较早发起的初次读取再返回创建前的旧列表：必须被丢弃。
        self.page.fulfill_json(initial, 200, {"surveys": old_list})
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], expected_items,
                         "较早发起的初次读取覆盖了最新刷新得到的列表")
        self.assert_pending_edits_preserved("旧列表返回后")
        self.page.settle(0.4)
        self.assertEqual(self.page.listing()["items"], expected_items,
                         "旧列表返回后再次观察：列表被改写")

        # 服务器旁证：新草稿以创建成功时的编号与已保存标题保存。
        status, saved = self.api("GET", f"/api/surveys/{new_id}")
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["title"], "创建时的标题")
        self.assertEqual(saved["description"], "创建时的说明")

        # 点击新草稿的条目可以打开对应详情，详情展示已保存内容。
        self.page.t("clickHref", f"#/surveys/{new_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{new_id}' && "
            "!!document.querySelector('.q-list')")
        detail = self.page.detail()
        self.assertEqual(detail["heading"], f"#{new_id} 创建时的标题")
        self.assertEqual(detail["description"], "创建时的说明")
        self.assertIn("文本题一", detail["bodyText"])
        self.assertIn("单选题一", detail["bodyText"])
        self.assertIn("选项甲", detail["bodyText"])
        self.assertIn("选项乙", detail["bodyText"])

    def test_stale_empty_list_does_not_clear_refreshed_list(self):
        """初次读取再返回空列表：新草稿不能消失，也不能变成空列表提示。"""
        self.seed_survey(title="已有问卷")
        (old_list, new_id, initial, refresh) = \
            self.begin_create_while_initial_read_held()
        expected_items = self.release_refresh_and_assert_list(
            refresh, old_list, new_id)["items"]

        self.page.fulfill_json(initial, 200, {"surveys": []})
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], expected_items,
                         "较早读取返回的空列表清掉了最新刷新得到的列表")
        self.assertNotIn("还没有问卷记录。", listing["items"])
        self.assert_pending_edits_preserved("空列表返回后")

    def test_stale_network_error_does_not_replace_refreshed_list(self):
        """初次读取网络错误：不能把已显示的刷新结果改成加载失败提示。"""
        self.seed_survey(title="已有问卷")
        (old_list, new_id, initial, refresh) = \
            self.begin_create_while_initial_read_held()
        expected_items = self.release_refresh_and_assert_list(
            refresh, old_list, new_id)["items"]

        self.page.fail_as_network_error(initial)
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], expected_items,
                         "较早读取的网络错误覆盖了最新刷新得到的列表")
        self.assertNotIn("问卷列表加载失败。", listing["items"])
        self.assert_pending_edits_preserved("旧读取网络错误后")

    # ---------- 最新刷新失败：较早读取随后成功也不得覆盖 ----------

    def test_refresh_network_error_shows_failure_and_stale_success_keeps_it(self):
        """最新刷新网络错误显示加载失败；较早的初次读取随后成功不能换回旧列表。"""
        self.seed_survey(title="已有问卷")
        (old_list, new_id, initial, refresh) = \
            self.begin_create_while_initial_read_held()

        # 较晚发起的刷新先返回：网络错误，显示现有的列表加载失败提示。
        self.page.fail_as_network_error(refresh)
        self.page.wait_for(
            "document.getElementById('survey-list').innerText"
            ".indexOf('问卷列表加载失败') !== -1")
        self.assertEqual(self.page.listing()["items"], ["问卷列表加载失败。"])
        self.assert_pending_edits_preserved("刷新网络错误后")

        # 较早发起的初次读取随后成功返回旧列表：失败提示必须保留。
        self.page.fulfill_json(initial, 200, {"surveys": old_list})
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], ["问卷列表加载失败。"],
                         "较早读取的成功结果把加载失败提示换成了旧列表")
        self.assert_pending_edits_preserved("旧列表随后成功返回后")

    # ---------- 离开首页后：旧读取不得替换当前页面 ----------

    def test_stale_read_after_leaving_home_does_not_replace_detail(self):
        """用户已进入详情页后，旧首页读取才返回也不能把详情替换成列表。"""
        seed_id = self.seed_survey(title="已有问卷")
        initial = self.open_home_holding_initial_read()

        self.page.eval(f"location.hash = '#/surveys/{seed_id}'")
        self.page.wait_for(
            f"location.hash === '#/surveys/{seed_id}' && "
            "!!document.querySelector('.q-list')")

        self.page.fulfill_json(initial, 200,
                               {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)
        detail = self.page.detail()
        self.assertEqual(detail["hash"], f"#/surveys/{seed_id}",
                         "旧首页读取返回后页面被带离详情")
        self.assertEqual(detail["heading"], f"#{seed_id} 已有问卷")
        self.assertFalse(detail["hasList"], "详情页被旧首页读取替换成了列表")

    def test_stale_read_after_leaving_home_does_not_replace_edit_page(self):
        """用户已进入编辑页后，旧首页读取才返回也不能把编辑页替换成列表。"""
        seed_id = self.seed_survey(title="已有问卷")
        initial = self.open_home_holding_initial_read()

        self.page.eval(f"location.hash = '#/surveys/{seed_id}/edit'")
        self.page.wait_for(
            f"location.hash === '#/surveys/{seed_id}/edit' && "
            "(document.getElementById('survey-title')||{}).value === '已有问卷'")

        self.page.fulfill_json(initial, 200,
                               {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], f"#/surveys/{seed_id}/edit",
                         "旧首页读取返回后页面被带离编辑页")
        self.assertEqual(snap["formHeading"], f"编辑问卷草稿 #{seed_id}")
        self.assertEqual(snap["title"], "已有问卷")
        self.assertFalse(self.page.listing()["hasList"],
                         "编辑页被旧首页读取替换成了列表")

    # ---------- 既有行为保留：无新修改时创建成功直接进入详情 ----------

    def test_create_success_without_new_edits_navigates_to_detail(self):
        """等待期间没有新修改：创建成功仍进入详情；旧首页读取随后返回不得替换。"""
        initial = self.open_home_holding_initial_read()
        create = self.fill_and_submit_draft(
            title="直达详情的问卷", description="直达详情的说明")

        self.page.release_to_server(create)
        # 创建成功同样会发起刷新读取；等待期间没有新修改，随后进入详情。
        refresh = self.page.pop_held("list")
        self.page.wait_for("/^#\\/surveys\\/\\d+$/.test(location.hash)")
        new_id = int(self.page.eval("location.hash").rsplit("/", 1)[1])

        # 刷新读取在页面进入详情后才返回：必须被丢弃，不能影响详情页。
        self.page.release_to_server(refresh)
        self.page.settle(0.6)
        # 初次读取再返回创建前的旧列表：同样不能替换详情。
        self.page.fulfill_json(initial, 200, {"surveys": []})
        self.page.settle(0.8)

        detail = self.page.detail()
        self.assertEqual(detail["hash"], f"#/surveys/{new_id}",
                         "旧首页读取把详情页带回了列表")
        self.assertEqual(detail["heading"], f"#{new_id} 直达详情的问卷")
        self.assertEqual(detail["description"], "直达详情的说明")
        self.assertIn("文本题一", detail["bodyText"])
        self.assertIn("单选题一", detail["bodyText"])
        self.assertIn("选项甲", detail["bodyText"])
        self.assertFalse(detail["hasList"], "详情页被旧首页读取替换成了列表")

        # 草稿内容保存规则不变：服务器上的内容与点击保存时一致。
        status, saved = self.api("GET", f"/api/surveys/{new_id}")
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["title"], "直达详情的问卷")
        self.assertEqual(saved["description"], "直达详情的说明")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in saved["questions"]],
            PENDING_QUESTIONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
