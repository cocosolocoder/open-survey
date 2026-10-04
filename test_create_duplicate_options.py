#!/usr/bin/env python3
"""首页新建问卷时“单选题选项重复校验”的浏览器回归保障。

test_survey_replace.py 保障接口层的落库规则；本文件驱动真实 Chrome 中的真实
首页新建表单，以用户实际看到的提示、保留下来的输入与最终保存结果为断言依据，
保护以下既有行为：

- 同一道单选题中，去掉首尾空白（空格、换行）后相同的选项判为重复；两项之间
  隔着其他选项仍然算重复。点击“保存整份问卷”时：
  - 停留在新建表单，不发送 POST /api/surveys，问卷列表不增加记录；
  - 顶部提示说明出错的是当前第几题与重复的选项内容，后出现的重复选项带
    .invalid 错误标记；点击提示把焦点移到那个选项。
- 校验失败后标题、说明、全部题目、必填勾选与每个选项的原始输入（含首尾空白
  与换行）原样保留，页面不替用户清理。
- 把重复选项改成不同内容后可按当前输入重新判断并保存；删除重复选项后只要仍
  有至少两个非空且互不相同的选项即可保存；删到只剩一个时提示该题选项不足。
- 保存前删除前面的题目或选项后，题号与选项编号按页面当前顺序更新，再次保存
  的错误提示与焦点对应当前位置。
- 重复判断只限于同一道题：另一道题使用相同选项、不同题目使用相同标题都可
  正常保存；选项内部换行有实际意义，不同的多行选项不会被合并。
- 修正后的合法保存进入新问卷详情，按原顺序展示题目、必填设置与去掉首尾空白
  后的选项；说明中的中文、引号、换行与首尾空白原样保留；返回首页后列表出现
  新编号与标题。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_create_duplicate_options -v
"""
import base64
import json
import os
import select
import shutil
import socketserver
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
# 真实 app.py 服务进程
# --------------------------------------------------------------------------

def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _IdleConnectionFilter(socketserver.ThreadingTCPServer):
    """浏览器与单线程 app 之间的透明 TCP 网关。

    Chrome 的网络服务会对源站建立“备用预连接”：TCP 已连通却迟迟不发送任何
    字节。app.py 的 BaseHTTPRequestHandler 在单线程内 accept 后会阻塞在读取
    请求行上，一个这样的空闲连接就能饿死全部后续请求。网关要求连接先收到
    字节才向上游建连：空连接只占用网关自己的守护线程，永远碰不到单线程 app。
    """

    daemon_threads = True
    allow_reuse_address = True
    IDLE_TIMEOUT = 30

    def __init__(self, port, upstream_port):
        self.upstream = ("127.0.0.1", upstream_port)
        super().__init__(("127.0.0.1", port), _FilterHandler)


class _FilterHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(_IdleConnectionFilter.IDLE_TIMEOUT)
        try:
            first = client.recv(65536)
        except (socket.timeout, OSError):
            return
        if not first:
            return
        try:
            upstream = socket.create_connection(self.server.upstream, timeout=10)
        except OSError:
            return
        try:
            upstream.sendall(first)
            client.settimeout(None)
            upstream.settimeout(None)
            sockets = [client, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                if not readable:
                    break
                closing = False
                for src in readable:
                    data = src.recv(65536)
                    if not data:
                        closing = True
                        break
                    (upstream if src is client else client).sendall(data)
                if closing:
                    break
        except OSError:
            pass
        finally:
            upstream.close()
            client.close()


class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-browser-")
        self.process = None
        self.base_url = None
        self._proxy = None

    def start(self):
        # app 绑定在不对外暴露的内部端口；所有访问（浏览器与测试的接口断言）
        # 都经过多线程网关，Chrome 的空闲预连接会被挡在网关，饿死不了单线程
        # app。显式选端口并丢弃子进程输出：单线程服务每个请求都写访问日志，
        # PIPE 无人读取会把服务阻塞在写日志上。
        app_port = _free_port()
        proxy_port = _free_port()
        self.process = subprocess.Popen(
            [sys.executable, str(APP), "serve",
             "--host", "127.0.0.1", "--port", str(app_port),
             "--data-dir", self.data_dir.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{app_port}/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("服务端口未就绪")
        self._proxy = _IdleConnectionFilter(proxy_port, app_port)
        threading.Thread(target=self._proxy.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{proxy_port}"

    def stop(self):
        if self._proxy is not None:
            self._proxy.shutdown()
            self._proxy.server_close()
            self._proxy = None
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
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            status = error.code
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw

    def listing(self):
        status, data = self.request("GET", "/api/surveys")
        assert status == 200, data
        return data["surveys"]


# --------------------------------------------------------------------------
# 极简 WebSocket 客户端（RFC 6455 握手 + 文本帧，仅用于连接 CDP）
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
        if not fin:
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
        if opcode == 0x9:
            with self._send_lock:
                self.sock.sendall(b"\x8A\x00")
            return None
        if opcode == 0x8:
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
        end = time.time() + timeout
        with self._cond:
            while not self._events and time.time() < end:
                self._cond.wait(max(0.0, end - time.time()))
            events, self._events = self._events, []
            return events

    def push_events(self, events):
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
# 真实 Chrome 中的一个标签页
# --------------------------------------------------------------------------

# 注入到每个页面文档的测试操作手柄：所有动作都走真实 DOM 事件与按钮/链接。
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
  T.removeOption = (qi, oi) => {
    // 走该行真实的“删除”按钮（内部会 row.remove() 后重新编号），
    // 不能直接调用 DOM 的 .remove() 绕过页面自己的重排逻辑。
    const row = T.cards()[qi].querySelectorAll('.opt-row')[oi];
    row.querySelector('button.link.danger').click();
  };
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
  T.bannerButtons = () => [...document.querySelectorAll('#form-banner li button')];
  T.clickBannerItem = needle => {
    const b = T.bannerButtons().find(x => x.textContent.indexOf(needle) !== -1);
    if (!b) throw new Error('顶部提示中找不到包含“' + needle + '”的条目');
    b.click();
  };
  T.activeIsOption = (qi, oi) =>
    document.activeElement === T.cards()[qi].querySelectorAll('.opt-text')[oi];
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
})();
"""

# 新建表单状态快照：题号/选项编号标签、原始输入值、错误标记与顶部提示全部取自
# 用户可见的 DOM。
SNAPSHOT_JS = r"""
(() => {
  const banner = document.getElementById('form-banner');
  const heading = document.querySelector('#draft-form h2');
  return {
    hash: location.hash,
    formHeading: heading ? heading.textContent : null,
    bannerVisible: banner ? !banner.hidden : false,
    bannerItems: banner
      ? [...banner.querySelectorAll('li button')].map(b => b.textContent)
      : [],
    title: (document.getElementById('survey-title') || {}).value,
    description: (document.getElementById('survey-desc') || {}).value,
    questions: [...document.querySelectorAll('.q-card')].map(card => ({
      type: card.dataset.type,
      qIndex: card.querySelector('.q-index').textContent,
      title: card.querySelector('.q-title').value,
      required: card.querySelector('.q-required').checked,
      optErr: card.querySelector('.opt-err')
        ? card.querySelector('.opt-err').textContent : null,
      options: [...card.querySelectorAll('.opt-row')].map(row => ({
        label: row.querySelector('.opt-index').textContent,
        value: row.querySelector('.opt-text').value,
        invalid: row.querySelector('.opt-text').classList.contains('invalid'),
      })),
    })),
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
  questions: [...document.querySelectorAll('.q-list > li')].map(li => ({
    text: li.querySelector('.q-line').innerText,
    options: [...li.querySelectorAll('.opt-text-display')].map(o => o.textContent),
  })),
  bodyText: document.body.innerText,
}))()
"""


class ChromeBrowser:
    def __init__(self):
        self.profile = None
        self.port = None
        self.process = None
        self.ws = None
        self._launch()

    def _launch(self):
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

    def _shutdown(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.process = None
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        if self.profile is not None:
            shutil.rmtree(self.profile, ignore_errors=True)
            self.profile = None

    def restart(self):
        # 共享主机上 Chrome 偶发整体卡住：新标签页/渲染进程迟迟创建不出来，
        # 且同一浏览器进程内不再恢复；重启整个浏览器即可恢复（每个用例都在
        # 全新标签页从首页开始，重启不会丢失任何用例状态）。
        self._shutdown()
        self._launch()

    def new_page(self):
        try:
            return self._new_page()
        except TimeoutError:
            self.restart()
            return self._new_page()

    def _new_page(self):
        result = self.ws.call("Target.createTarget", {"url": "about:blank"},
                              timeout=30)
        target_id = result["targetId"]
        result = self.ws.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True},
            timeout=30)
        page = ChromePage(self.ws, target_id, result["sessionId"])
        page.prepare()
        return page

    def close(self):
        self._shutdown()


class ChromePage:
    def __init__(self, ws, target_id, session_id):
        self.ws = ws
        self.target_id = target_id
        self.session = session_id
        self.held_posts = []
        self._held_lock = threading.Lock()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 拦截开启期间只挂起 POST（新建保存）；首页列表的 GET 必须立即放行，
        # 否则页面会饿死在加载列表上。
        self._pumping = True
        threading.Thread(target=self._event_loop, daemon=True).start()

    def _event_loop(self):
        while self._pumping:
            events = self.ws.drain_events(0.2)
            passthrough = []
            for event in events:
                if event.get("method") == "Fetch.requestPaused":
                    params = event["params"]
                    if params["request"]["method"] == "POST":
                        with self._held_lock:
                            self.held_posts.append(params)
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
            raise AssertionError(
                f"页面脚本异常：{json.dumps(details, ensure_ascii=False)[:500]}")
        return result.get("result", {}).get("value")

    def t(self, name, *args):
        arg_s = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        return self.eval(f"window.__t.{name}({arg_s})")

    def open_home(self):
        self.open(f"{self.server_url}/#/")
        self.wait_for("!!document.querySelector('#draft-form')")

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
        time.sleep(seconds)

    # ---------- 新建请求拦截 ----------

    def hold_posts(self):
        with self._held_lock:
            self.held_posts = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def held_post_count(self):
        with self._held_lock:
            return len(self.held_posts)

    def stop_holding(self):
        try:
            self.call("Fetch.disable", timeout=5)
        except Exception:
            pass
        with self._held_lock:
            # 解除拦截后这些请求已不会再到达页面，清掉计数即可。
            self.held_posts = []

    # ---------- 页面状态读取 ----------

    def snapshot(self):
        return self.eval(SNAPSHOT_JS)

    def detail(self):
        return self.eval(DETAIL_JS)


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

class CreateDuplicateOptionTests(unittest.TestCase):
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
        self.page.server_url = self.server.base_url
        self.page.open_home()

    def tearDown(self):
        self.page.stop_holding()
        self.page.close()

    # ---------- 辅助 ----------

    def api(self, method, path, body=None):
        return self.server.request(method, path, body)

    def submit_expect_form_errors(self):
        """点击保存并等待顶部错误提示出现（客户端校验失败、不离开表单）。"""
        page = self.page
        page.t("submit")
        page.wait_for("!document.getElementById('form-banner').hidden")
        return page.snapshot()

    def submit_expect_detail(self):
        """点击保存并等待进入新问卷详情，返回新编号。"""
        page = self.page
        page.t("submit")
        page.wait_for(r"/^#\/surveys\/\d+$/.test(location.hash)")
        page.wait_for("!!document.querySelector('.q-list')")
        survey_id = int(page.eval("location.hash").rsplit("/", 1)[1])
        return survey_id, page.detail()

    def assert_listing_unchanged(self, before):
        self.assertEqual(self.server.listing(), before,
                         "校验失败不应在问卷列表中增加任何记录")

    # ---------- 用例 ----------

    def test_duplicate_with_whitespace_blocked_marked_focused_and_input_kept(self):
        """首尾空格/换行不能绕过同题重复校验：提示、错误标记、焦点、输入保留与不发请求。"""
        page = self.page
        before = self.server.listing()

        # 标题与说明都带首尾空白；说明含中文、引号与换行，校验失败后必须原样保留。
        page.t("setTitle", "  满意度调查  ")
        page.t("setDesc", "  说明开头有空白\n第二行带\"引号\"和中文\n末尾也有空白  ")
        # 第 1 题：必填单选题；重复的两项之间隔着“一般”，第 3 项首尾是空格与
        # 换行，去掉首尾空白后与第 1 项相同。题目标题也带首尾空白。
        page.t("addChoice", " 你满意吗 ", ["满意", "一般", "\n 满意 \n"], True)
        # 第 2 题：非必填文本题，用于核对全部题目与勾选状态都会保留。
        page.t("addText", "其他建议", False)

        # 开启请求挂起：客户端校验失败时根本不应发出 POST。
        page.hold_posts()
        snap = self.submit_expect_form_errors()

        # 仍停留在首页新建表单。
        self.assertEqual(snap["hash"], "#/")
        self.assertEqual(snap["formHeading"], "新建问卷草稿")

        # 顶部提示只报告这一处重复，且说明当前题号与重复内容。
        self.assertEqual(len(snap["bannerItems"]), 1, snap["bannerItems"])
        message = snap["bannerItems"][0]
        self.assertIn("第 1 题", message)
        self.assertIn("满意", message)
        self.assertIn("重复", message)

        # 题卡内联错误文案同样定位到题号与选项内容。
        q1 = snap["questions"][0]
        self.assertIn("第 1 题", q1["optErr"])
        self.assertIn("满意", q1["optErr"])
        self.assertIn("重复", q1["optErr"])

        # 后出现的重复选项（第 3 个）有错误标记，先出现的第 1 个没有。
        flags = [opt["invalid"] for opt in q1["options"]]
        self.assertEqual(flags, [False, False, True])

        # 点击顶部提示把焦点移到后出现的那个重复选项上。
        page.t("clickBannerItem", "满意")
        self.assertTrue(page.t("activeIsOption", 0, 2),
                        "点击顶部提示后焦点未移到重复选项")

        # 没有任何创建请求发出，列表也不增加记录。
        page.settle(0.4)
        self.assertEqual(page.held_post_count(), 0,
                         "客户端校验失败时不应发送 POST /api/surveys")
        self.assert_listing_unchanged(before)

        # 全部原始输入原样保留，包括首尾空白与内部换行，页面不替用户清理。
        self.assertEqual(snap["title"], "  满意度调查  ")
        self.assertEqual(
            snap["description"],
            "  说明开头有空白\n第二行带\"引号\"和中文\n末尾也有空白  ")
        self.assertEqual(len(snap["questions"]), 2)
        self.assertEqual(q1["type"], "single_choice")
        self.assertEqual(q1["title"], " 你满意吗 ")
        self.assertTrue(q1["required"])
        self.assertEqual([opt["value"] for opt in q1["options"]],
                         ["满意", "一般", "\n 满意 \n"])
        q2 = snap["questions"][1]
        self.assertEqual(q2["type"], "text")
        self.assertEqual(q2["title"], "其他建议")
        self.assertFalse(q2["required"])

    def test_fix_duplicate_content_then_save_shows_detail_and_home_listing(self):
        """把重复选项改成不同内容后再次保存：按当前输入重新判断，保存成功并可在列表看到。"""
        page = self.page
        page.t("setTitle", "  满意度调查  ")
        page.t("setDesc", "  说明开头有空白\n第二行带\"引号\"和中文\n末尾也有空白  ")
        page.t("addChoice", "你满意吗", ["满意", "一般", "  满意  "], True)

        page.hold_posts()
        snap = self.submit_expect_form_errors()
        self.assertEqual([o["invalid"] for o in snap["questions"][0]["options"]],
                         [False, False, True])

        # 修正后先解除挂起，再按当前输入重新保存。
        page.t("setOption", 0, 2, "不满意")
        self.assertEqual(
            [o["value"] for o in page.snapshot()["questions"][0]["options"]],
            ["满意", "一般", "不满意"])
        page.stop_holding()
        survey_id, detail = self.submit_expect_detail()

        # 详情：标题与选项去掉首尾空白后按原顺序展示，必填设置保留。
        self.assertEqual(detail["heading"], f"#{survey_id} 满意度调查")
        self.assertEqual(len(detail["questions"]), 1)
        self.assertIn("你满意吗", detail["questions"][0]["text"])
        self.assertIn("必填", detail["questions"][0]["text"])
        self.assertEqual(detail["questions"][0]["options"],
                         ["满意", "一般", "不满意"])
        # 说明中的中文、引号、换行与首尾空白原样保留。
        self.assertEqual(
            detail["description"],
            "  说明开头有空白\n第二行带\"引号\"和中文\n末尾也有空白  ")

        # 服务端落库结果与详情一致。
        status, saved = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["title"], "满意度调查")
        self.assertEqual(
            saved["description"],
            "  说明开头有空白\n第二行带\"引号\"和中文\n末尾也有空白  ")
        self.assertEqual(saved["questions"][0]["options"],
                         ["满意", "一般", "不满意"])
        self.assertTrue(saved["questions"][0]["required"])

        # 返回首页：列表出现新编号与标题。
        page.t("clickHref", "#/")
        page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_id}\"]')")
        link_text = page.eval(
            f"document.querySelector('a[href=\"#/surveys/{survey_id}\"]').textContent")
        self.assertEqual(link_text, f"#{survey_id} 满意度调查")

    def test_delete_duplicate_option_allows_save_with_remaining_two(self):
        """删除后出现的重复选项后，剩余两个非空且不重复的选项允许保存。"""
        page = self.page
        page.t("setTitle", "删除重复项问卷")
        page.t("addChoice", "你满意吗", ["满意", "一般", " 满意 "], False)

        page.hold_posts()
        snap = self.submit_expect_form_errors()
        self.assertTrue(snap["questions"][0]["options"][2]["invalid"])

        # 删掉第 3 个（重复）选项，剩下“满意/一般”，解除挂起后保存成功。
        page.t("removeOption", 0, 2)
        snap = page.snapshot()
        self.assertEqual([o["value"] for o in snap["questions"][0]["options"]],
                         ["满意", "一般"])
        page.stop_holding()
        survey_id, detail = self.submit_expect_detail()

        self.assertEqual(detail["questions"][0]["options"], ["满意", "一般"])
        status, saved = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["questions"][0]["options"], ["满意", "一般"])

    def test_delete_down_to_one_option_reports_not_enough_then_recover(self):
        """删到只剩一个选项时提示该题选项不足、不发请求；补回不同选项后可保存。"""
        page = self.page
        before = self.server.listing()
        page.t("setTitle", "选项不足问卷")
        page.t("addChoice", "你满意吗", ["满意", "一般", " 满意 "], True)

        page.hold_posts()
        snap = self.submit_expect_form_errors()
        self.assertIn("重复", snap["bannerItems"][0])

        # 连续删除前两行，仅剩一个选项（仍保留其原始首尾空白）。
        page.t("removeOption", 0, 0)
        page.t("removeOption", 0, 0)
        snap = page.snapshot()
        self.assertEqual(len(snap["questions"][0]["options"]), 1)
        self.assertEqual(snap["questions"][0]["options"][0]["value"], " 满意 ")

        # 再次保存：按当前输入重新判断，提示变为“至少需要两个选项”。
        snap = self.submit_expect_form_errors()
        self.assertEqual(len(snap["bannerItems"]), 1, snap["bannerItems"])
        self.assertIn("第 1 题", snap["bannerItems"][0])
        self.assertIn("至少需要两个选项", snap["bannerItems"][0])
        self.assertNotIn("重复", snap["bannerItems"][0])
        self.assertFalse(
            snap["questions"][0]["options"][0]["invalid"],
            "仅剩的非空选项本身不应被标红")
        # 唯一的原始输入依旧保留。
        self.assertEqual(snap["questions"][0]["options"][0]["value"], " 满意 ")
        self.assertEqual(page.held_post_count(), 0)
        self.assert_listing_unchanged(before)

        # 补回一个不同选项后恢复为可保存状态。
        page.t("addOption", 0, "一般")
        page.stop_holding()
        survey_id, detail = self.submit_expect_detail()
        self.assertEqual(detail["questions"][0]["options"], ["满意", "一般"])

    def test_delete_preceding_question_and_option_renumbers_error_and_focus(self):
        """删除前面的题目/选项后，题号与选项编号更新，错误提示与焦点对应当前位置。"""
        page = self.page
        before = self.server.listing()
        # 第 1 题随后会被整题删除；第 2 题是真正出错的单选题：
        # 三个选项中后两项去掉首尾空白后相同，但先删掉第 1 个选项，
        # 使重复发生在删除后编号的“选项 2”上。
        page.t("setTitle", "重新编号问卷")
        page.t("addText", "将要删除的题", True)
        page.t("addChoice", "你满意吗", ["无关项", "满意", "\n满意\n"], False)

        page.hold_posts()

        # 删除第 1 题；再删除单选题当前的第 1 个选项“无关项”。
        page.t("removeQuestion", 0)
        page.t("removeOption", 0, 0)

        snap = page.snapshot()
        self.assertEqual(len(snap["questions"]), 1)
        q1 = snap["questions"][0]
        # 题号与选项编号已按页面当前顺序更新。
        self.assertEqual(q1["qIndex"], "第 1 题")
        self.assertEqual([o["label"] for o in q1["options"]], ["选项 1", "选项 2"])
        self.assertEqual([o["value"] for o in q1["options"]],
                         ["满意", "\n满意\n"])

        # 再次保存：错误按当前编号报告“第 1 题”，标记当前第 2 个选项。
        snap = self.submit_expect_form_errors()
        self.assertEqual(snap["formHeading"], "新建问卷草稿")
        self.assertEqual(len(snap["bannerItems"]), 1, snap["bannerItems"])
        message = snap["bannerItems"][0]
        self.assertIn("第 1 题", message)
        self.assertIn("满意", message)
        q1 = snap["questions"][0]
        self.assertEqual([o["invalid"] for o in q1["options"]], [False, True])
        self.assertEqual([o["label"] for o in q1["options"]], ["选项 1", "选项 2"])

        # 顶部提示聚焦的是当前第 2 个选项（删除前的位置已失效）。
        page.t("clickBannerItem", "满意")
        self.assertTrue(page.t("activeIsOption", 0, 1))
        self.assertEqual(page.held_post_count(), 0)
        self.assert_listing_unchanged(before)

        # 按当前输入修正后保存，落库顺序与当前页面一致。
        page.t("setOption", 0, 1, "一般")
        page.stop_holding()
        survey_id, detail = self.submit_expect_detail()
        self.assertEqual(detail["questions"][0]["options"], ["满意", "一般"])

    def test_same_options_other_question_and_same_titles_save_normally(self):
        """重复判断只限同题：两道题使用相同选项、标题相同都应正常保存。"""
        page = self.page
        page.t("setTitle", "跨题重复问卷")
        # 两道题标题相同、选项完全相同；必填设置不同，保存后都要保留。
        page.t("addChoice", "满意度", ["满意", "一般"], True)
        page.t("addChoice", "满意度", ["满意", "一般"], False)

        survey_id, detail = self.submit_expect_detail()
        self.assertEqual(len(detail["questions"]), 2)
        self.assertIn("必填", detail["questions"][0]["text"])
        self.assertIn("选填", detail["questions"][1]["text"])
        for question in detail["questions"]:
            self.assertEqual(question["options"], ["满意", "一般"])

        status, saved = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, saved)
        self.assertEqual([q["title"] for q in saved["questions"]], ["满意度", "满意度"])
        self.assertEqual([q["required"] for q in saved["questions"]], [True, False])
        self.assertEqual([q["options"] for q in saved["questions"]],
                         [["满意", "一般"], ["满意", "一般"]])

    def test_internal_newlines_distinguish_options_and_are_preserved(self):
        """选项内部换行有实际意义：不同多行选项不合并；首尾空白版仍按重复拦截。"""
        page = self.page

        # 先确认首尾空白相同的多行选项仍被判重（不被内部换行扰乱校验）。
        page.t("setTitle", "多行选项问卷")
        page.t("addChoice", "你满意吗", ["满意\n甲", " 满意\n甲 "], False)
        page.hold_posts()
        snap = self.submit_expect_form_errors()
        self.assertEqual([o["invalid"] for o in snap["questions"][0]["options"]],
                         [False, True])
        self.assertIn("第 1 题", snap["bannerItems"][0])

        # 改成两个仅内部换行不同的选项后可正常保存。
        page.t("setOption", 0, 1, "满意\n乙")
        page.stop_holding()
        survey_id, detail = self.submit_expect_detail()
        self.assertEqual(detail["questions"][0]["options"],
                         ["满意\n甲", "满意\n乙"])

        status, saved = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["questions"][0]["options"],
                         ["满意\n甲", "满意\n乙"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
