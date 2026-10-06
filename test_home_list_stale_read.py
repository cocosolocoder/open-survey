#!/usr/bin/env python3
"""首页问卷列表“较早发起的读取不能覆盖较晚发起的读取”的浏览器回归保障。

test_survey_replace.py 只核对 HTTP 接口的落库结果；本文件改用真实 Chrome 驱动
真实页面，保护首页列表在“进入首页的首次读取”与“创建草稿成功后的刷新读取”
返回顺序与发起顺序相反时的现有行为：

- 首次列表读取尚未返回时，用户填写包含文本题与单选题的有效草稿并保存；等待
  保存结果期间继续修改标题与说明（创建成功后留在当前表单）。随后那次刷新
  成功：列表显示服务器返回的问卷列表，新草稿使用创建成功时的编号与已保存
  标题（不是表单里尚未保存的新标题），按编号升序排列，点击条目可打开详情。
- 此后较早发起的首次读取才返回——无论返回创建前的旧列表、空列表，还是网络
  错误——都必须丢弃：新草稿不能消失、不能多出一条，列表不能被换回“还没有
  问卷记录”或“问卷列表加载失败”。以读取的发起顺序定胜负，与返回先后无关。
- 当前有效读取的正常提示保留：首次打开尚无问卷的首页显示空列表提示；创建后
  的最新刷新发生网络错误时显示列表加载失败提示，且较早读取随后成功也不能
  把失败提示换成旧列表。
- 列表区域的任何变化都不能清空、恢复或改写等待期间用户在表单里继续输入的
  标题、说明、题目、选项与必填设置。
- 用户离开首页后旧首页读取才返回时，不能把当前详情页或编辑页替换成列表。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把首页的列表
读取（GET /api/surveys）与创建请求（POST /api/surveys）挂起，由测试决定每次
读取何时、以何种结果返回——较晚发起的刷新先放行到真实服务器，较早发起的
首次读取再用伪造的旧响应/空列表/网络错误返回，以此核对“发起顺序优先”。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_home_list_stale_read -v
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

EMPTY_HINT = "还没有问卷记录。"
FAILURE_HINT = "问卷列表加载失败。"


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
        # PIPE 承接输出又不持续读取，缓冲区写满后服务会阻塞在写日志上，
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
  const status = document.getElementById('save-status');
  return {
    hash: location.hash,
    formHeading: formHeading ? formHeading.textContent : null,
    bannerVisible: banner ? !banner.hidden : false,
    bannerText: banner ? banner.textContent : '',
    saveStatusText: status && !status.hidden ? status.innerText : '',
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

# 列表区域快照：条目可见文本与链接目标，逐条对应。
LIST_JS = r"""
(() => {
  const list = document.getElementById('survey-list');
  return {
    present: !!list,
    text: list ? list.innerText : null,
    items: list ? [...list.querySelectorAll('li')].map(li => li.innerText) : [],
    hrefs: list ? [...list.querySelectorAll('a[href]')]
        .map(a => a.getAttribute('href')) : [],
  };
})()
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
        self.held_lists = []    # 被挂起的列表读取（GET /api/surveys，按到达顺序）
        self.held_posts = []    # 被挂起的创建请求（POST /api/surveys，按到达顺序）
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，除“列表读取”和“创建请求”外的
        # 请求（详情 GET、PUT、静态资源等）必须立即放行，否则页面自己的其它
        # 请求会被挂起饿死；被挂起的两类请求按到达顺序收集，交给测试决定
        # 每一次何时、以何种结果返回。
        self._pumping = True
        self._pump_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._pump_thread.start()

    def _event_loop(self):
        while self._pumping:
            try:
                events = self.ws.drain_events(0.2)
            except OSError:
                break
            passthrough = []
            for event in events:
                # 同一根 WebSocket 承载着浏览器里所有标签页的事件：只处理属于
                # 本会话的，其它会话的事件原样放回，交给对应页面的循环处理，
                # 绝不能按本页会话误放行/误收集（否则被挂起的请求会永远卡住）。
                if event.get("sessionId") not in (None, self.session):
                    passthrough.append(event)
                    continue
                if event.get("method") == "Fetch.requestPaused":
                    params = event["params"]
                    request = params["request"]
                    path = urlsplit(request["url"]).path
                    if request["method"] == "GET" and path == "/api/surveys":
                        with self._held_cond:
                            self.held_lists.append(params)
                            self._held_cond.notify_all()
                        continue
                    if request["method"] == "POST" and path == "/api/surveys":
                        with self._held_cond:
                            self.held_posts.append(params)
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
        # 先停掉并等尽本页的后台事件循环：它与下一页的循环共用同一根
        # WebSocket，若不及时退出会把新页被挂起的请求抢走（按旧会话处理），
        # 造成后续测试莫名超时。
        self._pumping = False
        self._pump_thread.join(timeout=5)
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
            batch = self.ws.drain_events(0.2)
            for i, event in enumerate(batch):
                if event.get("method") == "Page.loadEventFired":
                    # 拦截已开启时，导航期间就可能有请求被挂起（首页的首次列表
                    # 读取）：不属于本方法的事件——包括同一批里排在加载事件
                    # 之后的——都必须放回队列，交给后台事件循环处理，否则被
                    # 挂起的请求会永远卡住。
                    self.ws.push_events(leftover + batch[i + 1:])
                    return
                leftover.append(event)
            self.ws.push_events(leftover)
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
        """让后台拦截循环、微任务与异步回调有机会落地。"""
        time.sleep(seconds)

    # ---------- 列表读取 / 创建请求拦截 ----------

    def hold_reads(self):
        """挂起 GET/POST /api/surveys；详情 GET、PUT 等由后台事件循环立即放行。"""
        with self._held_cond:
            self.held_lists = []
            self.held_posts = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def held_list(self, index, timeout=8):
        """取出（不移除）按到达顺序第 index 个被挂起的列表读取。"""
        end = time.time() + timeout
        with self._held_cond:
            while len(self.held_lists) <= index and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if len(self.held_lists) > index:
                return self.held_lists[index]
        raise AssertionError(f"第 {index + 1} 次列表读取未发出（GET 未被挂起）")

    def list_read_count(self):
        with self._held_cond:
            return len(self.held_lists)

    def pop_held_post(self, timeout=8):
        """取出（并移除）最早一个被挂起的创建请求。"""
        end = time.time() + timeout
        with self._held_cond:
            while not self.held_posts and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held_posts:
                return self.held_posts.pop(0)
        raise AssertionError("创建请求未发出（POST 未被挂起）")

    def release_to_server(self, paused):
        """让挂起的请求真正到达服务器并把响应原样带回页面。"""
        self.call("Fetch.continueRequest", {"requestId": paused["requestId"]})

    def fulfill_json(self, paused, status, payload):
        """让挂起的请求直接以伪造的 JSON 响应返回（不接触服务器）。"""
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

    @staticmethod
    def held_body(paused):
        """读取挂起请求实际携带的 JSON 请求体。"""
        raw = paused["request"].get("postData")
        if raw is None:
            raise AssertionError("挂起的请求没有请求体")
        return json.loads(raw)

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


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

class HomeListStaleReadTests(unittest.TestCase):
    browser = None

    @classmethod
    def setUpClass(cls):
        if shutil.which(CHROME) is None and not Path(CHROME).exists():
            raise unittest.SkipTest(
                f"未找到浏览器（{CHROME}），跳过需要真实浏览器的回归测试；"
                "可用环境变量 CHROME_BIN 指定可执行文件路径。")
        cls.browser = ChromeBrowser()

    @classmethod
    def tearDownClass(cls):
        if cls.browser is not None:
            cls.browser.close()

    def setUp(self):
        # 每个用例独立的服务与数据目录：列表内容、编号都从空库开始，
        # “创建前的旧列表”与“按编号升序”的断言不依赖用例执行顺序。
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

    def seed_survey(self, title="已有问卷"):
        status, data = self.api("POST", "/api/surveys", {
            "title": title,
            "description": "已有说明",
            "questions": [{"type": "text", "title": "已有题目"}],
        })
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data["id"]

    def create_draft_with_held_initial_read(self):
        """制造“首次读取未返回、创建成功后的刷新已发出”的现场。

        打开首页（首次列表读取被挂起）→ 填写含文本题与单选题的有效草稿并
        提交（POST 被挂起）→ 等待保存结果期间继续修改标题与说明 → 放行
        POST 到真实服务器（201，表单就地转为编辑刚创建的草稿，并发出刷新
        读取，同样被挂起）。

        返回 (initial_read, refresh_read, new_id)：两次列表读取按发起顺序
        排列，都尚未返回；new_id 是创建成功时服务器分配的编号。
        """
        page = self.page
        page.hold_reads()
        page.open(f"{self.srv.base_url}/#/")
        page.wait_for("!!document.querySelector('#draft-form')")
        initial_read = page.held_list(0)
        # 首次读取未返回：列表停留在加载中，表单可正常填写。
        self.assertIn("加载中", page.listing()["text"])

        page.t("setTitle", "创建时保存的标题")
        page.t("setDesc", "创建时保存的说明")
        page.t("addText", "文本题一", True)
        page.t("addChoice", "单选题一", ["选项甲", "选项乙"], False)
        page.t("submit")
        post = page.pop_held_post()
        # 提交的就是点击保存那一刻的内容。
        self.assertEqual(page.held_body(post)["title"], "创建时保存的标题")

        # 等待保存结果期间继续修改标题与说明：创建成功后应留在当前表单。
        page.t("setTitle", "等待期间改的新标题")
        page.t("setDesc", "等待期间改的新说明")

        page.release_to_server(post)
        # 创建成功触发刷新读取（第二次列表读取，发起更晚）。
        refresh_read = page.held_list(1)
        page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent.startsWith('编辑问卷草稿 #')")

        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        new_id = data["surveys"][-1]["id"]
        self.assertEqual(data["surveys"][-1]["title"], "创建时保存的标题")
        return initial_read, refresh_read, new_id

    def release_refresh_and_assert_list(self, refresh_read, expected_items):
        """放行较晚发起的刷新读取到真实服务器，并核对列表按预期渲染。"""
        self.page.release_to_server(refresh_read)
        self.page.wait_for(
            f"document.querySelectorAll('#survey-list a[href]').length === "
            f"{len(expected_items)}")
        listing = self.page.listing()
        self.assertEqual(listing["items"], expected_items,
                         "刷新成功后列表应显示服务器返回的问卷列表")
        return listing

    def assert_list_exact(self, expected_items, where):
        listing = self.page.listing()
        self.assertTrue(listing["present"], f"{where}：列表区域不见了")
        self.assertEqual(listing["items"], expected_items,
                         f"{where}：列表被较早返回的读取改写")
        self.assertNotIn(EMPTY_HINT, listing["items"],
                         f"{where}：列表被换回了空列表提示")
        self.assertNotIn(FAILURE_HINT, listing["items"],
                         f"{where}：列表被换回了加载失败提示")

    def assert_form_kept(self, new_id, where):
        """列表读取的任何返回都不得触碰表单区域与保存状态条。"""
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", f"{where}：页面离开了首页")
        self.assertEqual(snap["formHeading"], f"编辑问卷草稿 #{new_id}",
                         f"{where}：表单标题（编号）被改写")
        self.assertEqual(snap["title"], "等待期间改的新标题",
                         f"{where}：等待期间输入的标题被清空或恢复")
        self.assertEqual(snap["description"], "等待期间改的新说明",
                         f"{where}：等待期间输入的说明被清空或恢复")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snap["questions"]],
            [("text", "文本题一", True, []),
             ("single_choice", "单选题一", False, ["选项甲", "选项乙"])],
            f"{where}：题目/选项/必填设置被改写")
        self.assertFalse(snap["bannerVisible"], f"{where}：表单冒出了错误提示条")
        self.assertIn("已保存", snap["saveStatusText"],
                      f"{where}：保存成功提示被列表读取抹掉")
        self.assertIn("未保存的修改", snap["saveStatusText"],
                      f"{where}：未保存提示被列表读取抹掉")

    # ---------- 较晚发起的刷新先返回：较早读取随后返回旧列表 ----------

    def test_stale_old_list_read_does_not_override_refresh(self):
        """刷新先成功展示新列表后，首次读取才返回创建前的旧列表：必须丢弃。"""
        seed_id = self.seed_survey("已有问卷")
        initial_read, refresh_read, new_id = self.create_draft_with_held_initial_read()

        # 较晚发起的刷新先返回：服务器列表含已有问卷与新草稿，按编号升序。
        expected_items = [f"#{seed_id} 已有问卷", f"#{new_id} 创建时保存的标题"]
        listing = self.release_refresh_and_assert_list(refresh_read, expected_items)
        self.assertEqual(listing["hrefs"],
                         [f"#/surveys/{seed_id}", f"#/surveys/{new_id}"])
        # 列表用的是已保存标题，不是表单里尚未保存的新标题。
        self.assertNotIn("等待期间改的新标题", listing["text"])
        self.assert_form_kept(new_id, "刷新成功后")

        # 较早发起的首次读取现在才返回创建前的旧列表（还没有新草稿）。
        self.page.fulfill_json(initial_read, 200,
                               {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)

        # 新草稿不能消失，已有问卷不能多出一条，顺序不变。
        self.assert_list_exact(expected_items, "旧列表返回后")
        self.assert_form_kept(new_id, "旧列表返回后")
        # 再观察一拍，确认没有延迟的改写。
        self.page.settle(0.4)
        self.assert_list_exact(expected_items, "旧列表返回后再次观察")
        self.assertEqual(self.page.list_read_count(), 2,
                         "首页停留期间应只有首次读取与创建后的刷新两次列表读取")

        # 点击新草稿条目可以打开对应详情，详情展示已保存的标题与内容。
        self.page.t("clickHref", f"#/surveys/{new_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{new_id}' && "
            "document.querySelector('h2') && "
            f"document.querySelector('h2').textContent === '#{new_id} 创建时保存的标题'")
        body = self.page.eval("document.body.innerText")
        self.assertIn("创建时保存的说明", body)
        self.assertIn("文本题一", body)
        self.assertIn("单选题一", body)
        self.assertIn("选项甲", body)

    def test_stale_empty_list_read_does_not_clear_refresh(self):
        """首次读取随后返回空列表：不能把已有新草稿的列表换回空列表提示。"""
        initial_read, refresh_read, new_id = self.create_draft_with_held_initial_read()

        expected_items = [f"#{new_id} 创建时保存的标题"]
        self.release_refresh_and_assert_list(refresh_read, expected_items)

        # 较早发起的首次读取返回空列表（创建前一份问卷都没有）。
        self.page.fulfill_json(initial_read, 200, {"surveys": []})
        self.page.settle(0.8)

        self.assert_list_exact(expected_items, "空列表返回后")
        self.assert_form_kept(new_id, "空列表返回后")

    def test_stale_network_error_read_does_not_replace_refresh(self):
        """首次读取随后网络失败：不能把已有新草稿的列表换成加载失败提示。"""
        initial_read, refresh_read, new_id = self.create_draft_with_held_initial_read()

        expected_items = [f"#{new_id} 创建时保存的标题"]
        self.release_refresh_and_assert_list(refresh_read, expected_items)

        self.page.fail_as_network_error(initial_read)
        self.page.settle(0.8)

        self.assert_list_exact(expected_items, "旧读取网络失败后")
        self.assert_form_kept(new_id, "旧读取网络失败后")

    # ---------- 当前有效读取的正常提示必须保留 ----------

    def test_refresh_network_error_shows_failure_and_stale_read_cannot_replace(self):
        """最新刷新网络失败显示加载失败提示；较早读取随后成功也不能换成旧列表。"""
        seed_id = self.seed_survey("已有问卷")
        initial_read, refresh_read, new_id = self.create_draft_with_held_initial_read()

        # 较晚发起的刷新读取网络失败：这是当前有效读取，显示加载失败提示。
        self.page.fail_as_network_error(refresh_read)
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes('{FAILURE_HINT}')")
        listing = self.page.listing()
        self.assertEqual(listing["items"], [FAILURE_HINT])
        self.assert_form_kept(new_id, "刷新网络失败后")

        # 较早发起的首次读取随后成功返回旧列表：不能把失败提示换成旧列表。
        self.page.fulfill_json(initial_read, 200,
                               {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)

        listing = self.page.listing()
        self.assertEqual(listing["items"], [FAILURE_HINT],
                         "较早读取随后成功不得把加载失败提示换成旧列表")
        self.assertNotIn("已有问卷", listing["text"])
        self.assert_form_kept(new_id, "旧列表返回后")

    def test_first_open_without_surveys_shows_empty_hint(self):
        """首次打开尚无问卷的首页：唯一一次读取返回空列表，显示空列表提示。"""
        self.page.open(f"{self.srv.base_url}/#/")
        self.page.wait_for(
            "document.getElementById('survey-list') && "
            f"document.getElementById('survey-list').innerText.includes('{EMPTY_HINT}')")
        listing = self.page.listing()
        self.assertEqual(listing["items"], [EMPTY_HINT])
        self.assertNotIn("加载中", listing["text"])

    # ---------- 离开首页后：旧首页读取不能替换当前页面 ----------

    def test_stale_read_arriving_on_detail_page_is_ignored(self):
        """已进入问卷详情页后，旧首页读取才返回：不能把详情页替换成列表。"""
        seed_id = self.seed_survey("已有问卷")
        initial_read, refresh_read, new_id = self.create_draft_with_held_initial_read()
        expected_items = [f"#{seed_id} 已有问卷", f"#{new_id} 创建时保存的标题"]
        self.release_refresh_and_assert_list(refresh_read, expected_items)

        # 用户点击新草稿条目离开首页进入详情。
        self.page.t("clickHref", f"#/surveys/{new_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{new_id}' && "
            "document.querySelector('h2') && "
            f"document.querySelector('h2').textContent === '#{new_id} 创建时保存的标题'")

        # 旧首页的首次读取此刻才返回：必须静默丢弃。
        self.page.fulfill_json(initial_read, 200,
                               {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)

        self.assertEqual(self.page.eval("location.hash"), f"#/surveys/{new_id}",
                         "旧首页读取返回后详情页被替换")
        self.assertIsNone(self.page.eval("document.getElementById('survey-list')"),
                          "详情页被旧首页读取换成了列表")
        self.assertEqual(
            self.page.eval("document.querySelector('h2').textContent"),
            f"#{new_id} 创建时保存的标题")

    def test_stale_read_arriving_on_edit_page_is_ignored(self):
        """已进入编辑页后，旧首页读取才返回：不能把编辑页替换成列表或清空输入。"""
        initial_read, refresh_read, new_id = self.create_draft_with_held_initial_read()
        expected_items = [f"#{new_id} 创建时保存的标题"]
        self.release_refresh_and_assert_list(refresh_read, expected_items)

        # 用户离开首页进入刚创建草稿的编辑页，并继续输入。
        self.page.eval(f"location.hash = '#/surveys/{new_id}/edit'")
        self.page.wait_for(
            f"location.hash === '#/surveys/{new_id}/edit' && "
            "(document.getElementById('survey-title')||{}).value === '创建时保存的标题'")
        self.page.t("setTitle", "编辑页里的新标题")

        # 旧首页的首次读取此刻才网络失败返回：必须静默丢弃。
        self.page.fail_as_network_error(initial_read)
        self.page.settle(0.8)

        self.assertEqual(self.page.eval("location.hash"),
                         f"#/surveys/{new_id}/edit",
                         "旧首页读取返回后编辑页被替换")
        self.assertIsNone(self.page.eval("document.getElementById('survey-list')"),
                          "编辑页被旧首页读取换成了列表")
        snap = self.page.snapshot()
        self.assertEqual(snap["title"], "编辑页里的新标题",
                         "旧首页读取返回后编辑页输入被改写")
        self.assertFalse(snap["bannerVisible"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
