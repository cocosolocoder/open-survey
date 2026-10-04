#!/usr/bin/env python3
"""问卷草稿“编辑内容加载”的浏览器回归保障。

草稿保存是整份替换，进入编辑页时能否拿到正确的原稿直接决定后续修改是否建立
在正确的内容之上。本文件驱动真实 Chrome 中的真实页面，从问卷详情点击“编辑
草稿”进入，并覆盖加载失败后的“重试”，以用户实际看到的页面与服务端落库结果
为断言依据，保护以下既有行为：

- 进入编辑页、内容尚未返回时只显示加载提示，不能出现可填写/可保存的空白表单。
- 仅当读取成功才显示带原稿的编辑表单：草稿同时含文本题与单选题、必填与选填
  题目时，标题、说明、题目顺序、题型、必填勾选与单选题全部选项均按保存时的
  次序来自这份问卷，不因重载少题、少选项或套用默认值；说明里的中文、引号、
  换行以及题目标题和选项内部的换行在表单中原样保留。打开编辑页本身不保存、
  不新增问卷。
- 读取因网络错误或服务端失败（5xx）未完成时，页面明确说明内容加载失败，并
  提供“重试”和“返回详情”；此时不开放编辑表单，也不能把失败当成“问卷没有
  题目”，已有草稿的标题、说明、题目、选项保持原样。
- 对不存在的编号显示“该问卷不存在”，同样不进入空白表单、不因此创建记录。
- 失败后的重试仍读取同一份问卷；恢复成功后错误提示被完整编辑表单取代，显示
  已保存内容，编号与对应问卷不变；持续失败则可再次重试。
- 加载结果只作用于当前编辑页：等待内容期间返回首页并在新建表单输入后，迟到
  的原编辑页读取结果（无论成功还是失败）都不能把用户带回编辑页、覆盖当前
  输入或在首页插入旧的加载错误提示。

这里保护的是“编辑内容读取”；首页新建、整份保存以及取消未保存修改的行为由
test_create_duplicate_options.py、test_survey_replace.py 与
test_edit_save_stale_response.py 分别保障，本文件只在边界处顺带确认不被破坏。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把读取问卷
详情的 GET 挂起，再决定让它真实到达服务器、伪造 5xx/404 响应，还是直接按
网络错误失败。放行成功后会再读一次接口，仅作“服务器原稿未被打开/重试改动”
的旁证；页面行为本身只按 DOM 与可见文本断言。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_edit_draft_load -v
"""
import base64
import json
import os
import re
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

# 只挂起读取问卷详情的 GET（/api/surveys/{id}），不挂问卷列表 /api/surveys。
DETAIL_GET_RE = re.compile(r"/api/surveys/\d+$")


# --------------------------------------------------------------------------
# 真实 app.py 服务进程（app 绑内部端口，浏览器与测试都经过多线程网关访问）
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

# 注入到每个页面文档的测试操作手柄：所有动作都走真实的 DOM 事件与链接/按钮。
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
  T.submit = () => {
    const b = document.querySelector('#draft-form button[type=submit]');
    if (b) b.click();
  };
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
  // 加载失败页上的“重试”是 button（“返回详情”是 a），按文案精确点击。
  T.retry = () => T.clickBtn('重试');
})();
"""

# 页面状态快照：加载中 / 加载失败 / 编辑表单 / 首页新建表单四种形态都用这一份
# 快照区分，所有字段均来自用户可见的 DOM 与表单值。
SNAPSHOT_JS = r"""
(() => {
  const banner = document.getElementById('form-banner');
  const titleEl = document.getElementById('survey-title');
  const descEl = document.getElementById('survey-desc');
  const formHeading = document.querySelector('#draft-form h2');
  const loadingEl = document.getElementById('edit-status');
  const alertEl = document.querySelector('.banner[role=alert]');
  return {
    hash: location.hash,
    loadingVisible: loadingEl ? !loadingEl.hidden && loadingEl.offsetParent !== null : false,
    loadingText: loadingEl ? loadingEl.textContent : null,
    loadErrorVisible: !!alertEl,
    loadErrorText: alertEl ? alertEl.textContent : '',
    hasRetry: !![...document.querySelectorAll('.banner button')]
      .find(b => b.textContent.trim() === '重试'),
    backToDetailHref: (() => {
      const a = alertEl ? [...alertEl.querySelectorAll('a[href]')]
        .find(x => x.textContent.indexOf('返回详情') !== -1) : null;
      return a ? a.getAttribute('href') : null;
    })(),
    formPresent: !!document.querySelector('#draft-form'),
    formHeading: formHeading ? formHeading.textContent : null,
    saveButtonPresent: !!document.querySelector('#draft-form button[type=submit]'),
    bannerVisible: banner ? !banner.hidden : false,
    title: titleEl ? titleEl.value : null,
    description: descEl ? descEl.value : null,
    questions: [...document.querySelectorAll('.q-card')].map(card => ({
      type: card.dataset.type,
      qIndex: card.querySelector('.q-index').textContent,
      typeTag: card.querySelector('.q-type-tag').textContent,
      title: card.querySelector('.q-title').value,
      required: card.querySelector('.q-required').checked,
      requiredTag: (() => {
        const t = card.querySelector('.q-head .tag.required');
        return t ? t.textContent : null;
      })(),
      options: [...card.querySelectorAll('.opt-row')].map(row => ({
        label: row.querySelector('.opt-index').textContent,
        value: row.querySelector('.opt-text').value,
      })),
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
        # 共享主机上 Chrome 偶发整体卡住：新标签页迟迟创建不出来，且同一浏览器
        # 进程内不再恢复；重启整个浏览器即可恢复（每个用例都在全新标签页开始，
        # 重启不丢失任何用例状态）。
        self._shutdown()
        self._launch()

    def new_page(self):
        try:
            return self._new_page()
        except TimeoutError:
            self.restart()
            return self._new_page()

    def _new_page(self):
        result = self.ws.call("Target.createTarget", {"url": "about:blank"}, timeout=30)
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
        self.held_gets = []        # 被挂起的“读取问卷详情”GET
        self._held_cond = threading.Condition()
        self._pumping = False
        self._pump_thread = None

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件。所有标签页共用同一个浏览器级 WebSocket：泵
        # 线程只处理属于本标签页会话（sessionId 匹配）的事件，其它会话的事件
        # 一律原样放回，关闭标签页时还要等线程退出，避免上一页的线程排空
        # socket、把新一页的 Fetch.requestPaused 事件误吞掉。拦截开启期间，
        # 只有读取问卷详情的 GET（/api/surveys/{id}）会被挂起；问卷列表 GET、
        # 静态资源与保存请求（POST/PUT）必须立即放行，否则切到的首页/详情会
        # 因自己的请求被挂起而饿死，也不能影响整份保存的既有行为。
        self._pumping = True
        self._pump_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._pump_thread.start()

    def _event_loop(self):
        while self._pumping:
            events = self.ws.drain_events(0.2)
            passthrough = []
            for event in events:
                if event.get("sessionId") != self.session:
                    passthrough.append(event)
                    continue
                if event.get("method") == "Fetch.requestPaused":
                    params = event["params"]
                    request = params["request"]
                    path = urllib.request.urlsplit(request["url"]).path
                    if request["method"] == "GET" and DETAIL_GET_RE.fullmatch(path):
                        with self._held_cond:
                            self.held_gets.append(params)
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
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=3)
            self._pump_thread = None
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
        # 后台泵线程也在同一个 WebSocket 上取事件：这里拿到的非 load 事件
        # （尤其拦截开启后的 Fetch.requestPaused）必须原样放回队列交给泵，
        # 不能就地丢弃，否则挂起的读取会“丢事件”导致等待超时。
        pending = self.ws.drain_events(0)
        if pending:
            self.ws.push_events(pending)
        self.call("Page.navigate", {"url": url})
        end = time.time() + 10
        while time.time() < end:
            events = self.ws.drain_events(0.2)
            requeue = []
            loaded = False
            for event in events:
                if event.get("method") == "Page.loadEventFired":
                    loaded = True
                else:
                    requeue.append(event)
            if requeue:
                self.ws.push_events(requeue)
            if loaded:
                return
        raise AssertionError(f"页面加载超时：{url}")

    def reload(self):
        # hash 路由下对同一 URL 再调 Page.navigate 是同文档导航，不会触发
        # load 事件；整页重载要用 Page.reload 真正重建文档、重跑数据读取。
        self.call("Page.reload", {"ignoreCache": True})
        end = time.time() + 10
        while time.time() < end:
            events = self.ws.drain_events(0.2)
            requeue = []
            loaded = False
            for event in events:
                if event.get("method") == "Page.loadEventFired":
                    loaded = True
                else:
                    requeue.append(event)
            if requeue:
                self.ws.push_events(requeue)
            if loaded:
                return
        raise AssertionError("页面整页重载超时")

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

    # ---------- 详情读取请求拦截 ----------

    def hold_detail_gets(self):
        """挂起所有读取问卷详情的 GET；其它请求由后台事件循环立即放行。"""
        with self._held_cond:
            self.held_gets = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def wait_held_get(self, timeout=8):
        end = time.time() + timeout
        with self._held_cond:
            while not self.held_gets and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held_gets:
                # 弹出最早一个：重试会在同一标签页里产生多个被挂起的读取，
                # 不弹出会把已处置（失效）的旧 requestId 再次交给 CDP。
                return self.held_gets.pop(0)
        raise AssertionError("详情读取请求未发出（GET 未被挂起）")

    def release_to_server(self, paused):
        """让挂起的请求真正到达服务器并把响应原样带回页面。"""
        self.call("Fetch.continueRequest", {"requestId": paused["requestId"]})

    def fulfill_json(self, paused, status, payload=None):
        body = json.dumps(payload if payload is not None else {"error": "mock"},
                          ensure_ascii=False).encode("utf-8")
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
        with self._held_cond:
            self.held_gets = []

    # ---------- 页面状态读取 ----------

    def snapshot(self):
        return self.eval(SNAPSHOT_JS)

    def detail(self):
        return self.eval(DETAIL_JS)


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

class EditDraftLoadTests(unittest.TestCase):
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
        """准备一份同时含多选/必填形态、中文引号换行的完整草稿。"""
        payload = {
            "title": "加载回归问卷\n标题第二行",
            "description": "说明第一行\n说明第二行带\"引号\"与中文\n末尾换行前一行\n",
            "questions": [
                {"type": "text", "title": "必填文本题\n第二行", "required": True},
                {"type": "single_choice",
                 "title": "选填单选题\n题目第二行", "required": False,
                 "options": ["选项第一行\n选项第二行", "含\"引号\"选项",
                             "中文选项丙", "末尾选项丁"]},
                {"type": "text", "title": "选填文本题", "required": False},
                {"type": "single_choice", "title": "必填单选题", "required": True,
                 "options": ["是", "否"]},
            ],
        }
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data["id"], data

    def open_detail(self, survey_id):
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}' && "
            "!!document.querySelector('a[href$=\"/edit\"]')")

    def click_edit_from_detail(self):
        """从详情页点击真实的“编辑草稿”链接进入编辑页。"""
        self.page.t("clickHref", f"#/surveys/{self.survey_id}/edit")

    def start_edit_and_hold(self, survey_id=None):
        """挂起详情读取后进入编辑页，返回被挂起的 GET，页面停在加载中。"""
        sid = survey_id if survey_id is not None else self.survey_id
        self.page.hold_detail_gets()
        self.page.open(f"{self.server.base_url}/#/surveys/{sid}/edit")
        return self.page.wait_held_get()

    def wait_edit_form(self, survey_id=None):
        sid = survey_id if survey_id is not None else self.survey_id
        self.page.wait_for(
            f"location.hash === '#/surveys/{sid}/edit' && "
            "!!document.querySelector('#draft-form .q-card') && "
            "(document.getElementById('survey-title')||{}).value !== ''")

    def wait_load_error(self):
        self.page.wait_for("!!document.querySelector('.banner[role=alert]')")

    def assert_loading_only(self, snap, where):
        """内容未返回：只有加载提示，没有可填写/可保存的空白表单。"""
        self.assertTrue(snap["loadingVisible"], f"{where}：未显示加载提示")
        self.assertIn("加载中", snap["loadingText"] or "", f"{where}：加载提示文案不对")
        self.assertFalse(snap["formPresent"], f"{where}：加载期间出现了编辑表单")
        self.assertFalse(snap["saveButtonPresent"], f"{where}：加载期间出现了保存按钮")
        self.assertEqual(snap["questions"], [], f"{where}：加载期间出现了题目卡片")
        self.assertFalse(snap["loadErrorVisible"], f"{where}：加载期间出现了错误提示")

    def assert_error_page(self, snap, survey_id, phrase, where):
        """加载失败：明确说明原因，提供“重试”和“返回详情”，不开放表单。"""
        self.assertTrue(snap["loadErrorVisible"], f"{where}：未显示加载失败提示")
        self.assertIn("加载失败", snap["loadErrorText"], f"{where}：未说明加载失败")
        self.assertIn(phrase, snap["loadErrorText"],
                      f"{where}：失败原因缺少“{phrase}”")
        self.assertTrue(snap["hasRetry"], f"{where}：缺少“重试”按钮")
        self.assertEqual(snap["backToDetailHref"], f"#/surveys/{survey_id}",
                         f"{where}：缺少指向本问卷详情的“返回详情”")
        self.assertFalse(snap["formPresent"], f"{where}：失败页开放了编辑表单")
        self.assertFalse(snap["saveButtonPresent"], f"{where}：失败页出现了保存按钮")
        self.assertEqual(snap["questions"], [], f"{where}：失败页出现了题目卡片")

    def assert_form_matches_survey(self, snap, survey, where):
        """成功加载：编辑表单的标题/说明/题目顺序/题型/必填/选项全部来自原稿。"""
        self.assertTrue(snap["formPresent"], f"{where}：未显示编辑表单")
        self.assertEqual(snap["formHeading"], f"编辑问卷草稿 #{survey['id']}",
                         f"{where}：编辑标题/编号不对")
        self.assertFalse(snap["loadErrorVisible"], f"{where}：仍显示加载失败提示")
        self.assertFalse(snap["loadingVisible"], f"{where}：仍停留在加载提示")
        self.assertEqual(snap["title"], survey["title"], f"{where}：标题不是原稿")
        self.assertEqual(snap["description"], survey["description"],
                         f"{where}：说明不是原稿")
        self.assertEqual(len(snap["questions"]), len(survey["questions"]),
                         f"{where}：题目数量与原稿不符（少题/多题）")
        for i, (card, question) in enumerate(zip(snap["questions"], survey["questions"])):
            loc = f"{where}：第 {i + 1} 题"
            self.assertEqual(card["qIndex"], f"第 {i + 1} 题", f"{loc}：题号次序错误")
            self.assertEqual(card["type"], question["type"], f"{loc}：题型错误")
            self.assertEqual(card["typeTag"],
                             "单选题" if question["type"] == "single_choice" else "文本题",
                             f"{loc}：题型标签错误")
            self.assertEqual(card["title"], question["title"], f"{loc}：题目标题不是原稿")
            self.assertEqual(card["required"], question["required"],
                             f"{loc}：必填勾选不是原稿（套用默认值？）")
            if question["type"] == "single_choice":
                values = [opt["value"] for opt in card["options"]]
                self.assertEqual(values, question["options"],
                                 f"{loc}：选项内容/次序不是保存时的次序（少选项？）")
                labels = [opt["label"] for opt in card["options"]]
                self.assertEqual(labels, [f"选项 {j + 1}" for j in range(len(values))],
                                 f"{loc}：选项编号未按保存次序排列")
            else:
                self.assertEqual(card["options"], [], f"{loc}：文本题被加了选项")
        # 内部换行必须在表单控件值里真实保留，而不是被折叠或截断。
        self.assertIn("\n", snap["title"], f"{where}：标题内部换行丢失")
        self.assertIn("\n", snap["description"], f"{where}：说明换行丢失")
        self.assertIn('"', snap["description"], f"{where}：说明中的引号丢失")
        self.assertIn("中文", snap["description"], f"{where}：说明中的中文丢失")
        self.assertIn("\n", snap["questions"][0]["title"],
                      f"{where}：题目标题内部换行丢失")
        self.assertIn("\n", snap["questions"][1]["options"][0]["value"],
                      f"{where}：选项内部换行丢失")

    def assert_server_survey_intact(self, survey_id, before, where):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, f"{where}：原稿读取失败：{data}")
        self.assertEqual(data, before, f"{where}：加载/重试改动了已保存的问卷")

    def assert_listing_ids(self, expected_ids, where):
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, data)
        ids = [item["id"] for item in data["surveys"]]
        self.assertEqual(ids, sorted(expected_ids), f"{where}：问卷列表被改动（新增记录？）")

    # ---------- 加载中：只显示加载提示 ----------

    def test_loading_shows_hint_without_blank_editable_form(self):
        """从详情点“编辑草稿”、内容未返回时只有加载提示，没有空白可填写表单。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        ids_before = [item["id"] for item in self.api("GET", "/api/surveys")[1]["surveys"]]

        # 先在详情页确认入口，再开启拦截并经真实链接进入，覆盖“详情→编辑草稿”。
        self.open_detail(survey_id)
        self.page.hold_detail_gets()
        paused = self._click_edit_and_wait_hung(survey_id)

        # 多观察一拍：加载结果未返回期间始终不能冒出空表单。
        self.page.settle(0.5)
        self.assert_loading_only(self.page.snapshot(), "加载结果挂起期间")
        self.page.settle(0.5)
        self.assert_loading_only(self.page.snapshot(), "再次观察加载期间")

        # 放行成功：加载提示被带原稿的编辑表单取代。
        self.page.release_to_server(paused)
        self.wait_edit_form(survey_id)
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], f"#/surveys/{survey_id}/edit")
        self.assert_form_matches_survey(snap, survey, "加载成功后")
        # 打开编辑页本身不保存、不新增问卷。
        self.assert_server_survey_intact(survey_id, before, "打开编辑页后")
        self.assert_listing_ids(ids_before, "打开编辑页后")

    def _click_edit_and_wait_hung(self, survey_id):
        """拦截已开启时点击编辑入口并等待详情 GET 被挂起。"""
        self.page.t("clickHref", f"#/surveys/{survey_id}/edit")
        return self.page.wait_held_get()

    def test_opening_edit_does_not_save_or_create_and_reload_keeps_everything(self):
        """打开编辑页与整页重新加载都不发保存请求，原稿逐字呈现且可反复重载。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        ids_before = [item["id"] for item in self.api("GET", "/api/surveys")[1]["surveys"]]

        # 正常（不拦截）直接打开编辑地址：应自动加载原稿。
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}/edit")
        self.wait_edit_form(survey_id)
        self.assert_form_matches_survey(self.page.snapshot(), survey, "首次进入编辑页")

        # 再次整页加载同一编辑地址：仍按服务器原稿重建，不丢题、不丢选项、不套用默认值。
        self.page.reload()
        self.wait_edit_form(survey_id)
        self.assert_form_matches_survey(self.page.snapshot(), survey, "重新加载编辑页")

        # 没有任何写请求：编号与列表完全不变，内容逐字一致。
        self.assert_listing_ids(ids_before, "重复打开编辑页后")
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data, survey)

    # ---------- 加载失败：网络错误 ----------

    def test_network_failure_shows_error_retry_and_back_without_form(self):
        """读取因网络错误失败：提示加载失败，给重试/返回详情，不开放表单、不改原稿。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]
        ids_before = [item["id"] for item in self.api("GET", "/api/surveys")[1]["surveys"]]

        paused = self.start_edit_and_hold(survey_id)
        self.assert_loading_only(self.page.snapshot(), "网络失败返回前")
        self.page.fail_as_network_error(paused)
        self.wait_load_error()
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], f"#/surveys/{survey_id}/edit")
        self.assert_error_page(snap, survey_id, "网络错误", "网络失败后")

        # 失败不能被当成“没有题目”：已有草稿在服务端原样保留，也没有新建记录。
        self.assert_server_survey_intact(survey_id, before, "网络失败后")
        self.assert_listing_ids(ids_before, "网络失败后")

    def test_server_failure_shows_error_retry_and_back_without_form(self):
        """服务端返回 500：提示加载失败，给重试/返回详情，不开放表单、不改原稿。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]

        paused = self.start_edit_and_hold(survey_id)
        self.page.fulfill_json(paused, 500, {"error": "模拟服务端异常"})
        self.wait_load_error()
        snap = self.page.snapshot()
        # 500 属于“加载失败可重试”，不应误报成“问卷不存在”。
        self.assert_error_page(snap, survey_id, "HTTP 500", "服务端失败后")
        self.assertNotIn("不存在", snap["loadErrorText"], "5xx 不应提示问卷不存在")
        self.assert_server_survey_intact(survey_id, before, "服务端失败后")

    # ---------- 不存在的问卷 ----------

    def test_missing_survey_shows_not_found_without_blank_form_or_creation(self):
        """不存在的编号：显示问卷不存在，不进入空白表单，也不因此创建记录。"""
        # 先有一份真实问卷，再取一个明显不存在的编号。
        existing_id, _ = self.seed_survey()
        missing_id = existing_id + 999_999
        ids_before = [item["id"] for item in self.api("GET", "/api/surveys")[1]["surveys"]]
        self.assertNotIn(missing_id, ids_before)

        # 不拦截：真实服务器对未知编号返回 404，页面应显示“不存在”。
        self.page.open(f"{self.server.base_url}/#/surveys/{missing_id}/edit")
        self.page.wait_for("!!document.querySelector('.banner[role=alert]')")
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], f"#/surveys/{missing_id}/edit")
        self.assertTrue(snap["loadErrorVisible"])
        self.assertIn("不存在", snap["loadErrorText"], "应明确显示该问卷不存在")
        self.assertTrue(snap["hasRetry"], "不存在页也应提供重试")
        self.assertEqual(snap["backToDetailHref"], f"#/surveys/{missing_id}")
        self.assertFalse(snap["formPresent"], "404 后不能进入空白编辑表单")
        self.assertFalse(snap["saveButtonPresent"])
        self.assertEqual(snap["questions"], [])
        # 未因此创建记录；接口层对该编号依旧 404。
        self.assert_listing_ids(ids_before, "打开不存在编号后")
        status, _ = self.api("GET", f"/api/surveys/{missing_id}")
        self.assertEqual(status, 404)

    # ---------- 返回详情 ----------

    def test_back_to_detail_from_load_error_shows_original_survey(self):
        """加载失败页点“返回详情”回到原问卷详情，详情展示未被改动的原稿。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]

        paused = self.start_edit_and_hold(survey_id)
        self.page.fail_as_network_error(paused)
        self.wait_load_error()

        # 点击真实的“返回详情”链接；先解除 GET 挂起，让详情页自己的读取放行。
        self.page.stop_holding()
        self.page.t("clickHref", f"#/surveys/{survey_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}' && "
            "!!document.querySelector('.q-list')")
        detail = self.page.detail()
        # h2 的 textContent 逐字保留标题（含内部换行），前缀为“#编号 ”。
        self.assertEqual(detail["heading"], f"#{survey_id} {survey['title']}",
                         "详情标题应逐字展示原问卷标题（含内部换行）")
        # 详情正文应包含全部原稿题目与选项。
        body = detail["bodyText"]
        for question in survey["questions"]:
            self.assertIn(question["title"].splitlines()[0], body)
            for opt in question["options"]:
                self.assertIn(opt.splitlines()[0], body)
        self.assertEqual(len(detail["lines"]), len(survey["questions"]),
                         "详情题目数量应与原稿一致")
        self.assert_server_survey_intact(survey_id, before, "返回详情后")

    # ---------- 重试：失败后再读取同一份问卷 ----------

    def test_retry_after_network_error_loads_same_survey(self):
        """网络失败后点重试仍读同一份问卷；恢复成功后错误提示被完整表单取代。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]

        first = self.start_edit_and_hold(survey_id)
        self.page.fail_as_network_error(first)
        self.wait_load_error()
        self.assert_error_page(self.page.snapshot(), survey_id, "网络错误", "首次失败后")

        # 点“重试”：再次发起对同一编号的读取，先让它也挂起，确认请求确实重发。
        self.page.t("retry")
        second = self.page.wait_held_get()
        self.page.settle(0.3)
        # 重试请求在途时回到“加载中”，旧错误消失，但同样不会提前出现空表单。
        pending = self.page.snapshot()
        self.assertFalse(pending["loadErrorVisible"], "重试在途时不应仍停留在错误提示")
        self.assertTrue(pending["loadingVisible"], "重试在途时应重新显示加载提示")
        self.assertFalse(pending["formPresent"], "重试在途时不能提前出现编辑表单")

        # 读取恢复：放行到真实服务器，错误提示必须被带原稿的完整编辑表单取代。
        self.page.release_to_server(second)
        self.wait_edit_form(survey_id)
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], f"#/surveys/{survey_id}/edit",
                         "重试成功后编号/地址发生变化")
        self.assert_form_matches_survey(snap, survey, "重试成功后")
        self.assert_server_survey_intact(survey_id, before, "重试成功后")

    def test_retry_after_server_error_then_success_shows_saved_content(self):
        """500 后重试仍读原问卷：先继续失败可再次重试，恢复成功后显示已保存内容。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]

        first = self.start_edit_and_hold(survey_id)
        self.page.fulfill_json(first, 500, {"error": "模拟服务端异常"})
        self.wait_load_error()

        # 第一次重试仍失败（又一个 500）：继续停留在失败提示，可再次重试。
        self.page.t("retry")
        second = self.page.wait_held_get()
        self.page.fulfill_json(second, 500, {"error": "模拟服务端仍异常"})
        self.wait_load_error()
        self.assert_error_page(self.page.snapshot(), survey_id, "HTTP 500", "再次失败后")
        self.assertFalse(self.page.snapshot()["formPresent"])

        # 第二次重试放行成功：显示已保存内容，编号与问卷不变。
        self.page.t("retry")
        third = self.page.wait_held_get()
        self.page.release_to_server(third)
        self.wait_edit_form(survey_id)
        self.assert_form_matches_survey(self.page.snapshot(), survey, "二次重试成功后")
        self.assert_server_survey_intact(survey_id, before, "二次重试成功后")

    def test_retry_targets_same_original_survey(self):
        """重试请求必须仍是原来的问卷编号，不能换成别的问卷或新建。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id

        first = self.start_edit_and_hold(survey_id)
        self.page.fail_as_network_error(first)
        self.wait_load_error()

        self.page.t("retry")
        second = self.page.wait_held_get()
        path = urllib.request.urlsplit(second["request"]["url"]).path
        self.assertEqual(path, f"/api/surveys/{survey_id}",
                         "重试读取的不是原来的问卷编号")
        self.assertEqual(second["request"]["method"], "GET", "重试必须仍是读取请求")

        self.page.release_to_server(second)
        self.wait_edit_form(survey_id)
        self.assertEqual(self.page.snapshot()["formHeading"],
                         f"编辑问卷草稿 #{survey_id}")

    # ---------- 加载结果不得跨页面生效（迟到响应边界） ----------

    def test_late_success_after_returning_home_is_ignored(self):
        """等待加载期间回首页并填写新建表单，迟到的成功读取不能带回编辑页或覆盖输入。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        ids_before = [item["id"] for item in self.api("GET", "/api/surveys")[1]["surveys"]]

        paused = self.start_edit_and_hold(survey_id)
        self.assert_loading_only(self.page.snapshot(), "离开前的编辑页加载中")

        # 不经过失败页：加载在途直接返回首页（与点“← 返回问卷详情/返回首页”相同的路由）。
        self.page.eval("location.hash = '#/'")
        self.page.wait_for("location.hash === '#/' && !!document.querySelector('#draft-form')")

        # 在首页新建表单中输入标题与题目。
        self.page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent === '新建问卷草稿'")
        self.page.t("setTitle", "首页新建的问卷")
        self.page.t("setDesc", "首页新建说明")
        self.page.t("addText", "首页文本题", True)
        self.page.t("addChoice", "首页单选题", ["首页选项甲", "首页选项乙"], False)
        expected = {
            "formHeading": "新建问卷草稿",
            "title": "首页新建的问卷",
            "description": "首页新建说明",
            "questions": [
                ("text", "首页文本题", True, []),
                ("single_choice", "首页单选题", False, ["首页选项甲", "首页选项乙"]),
            ],
        }

        # 原编辑页的读取结果此刻才到达：成功返回原稿。
        self.page.release_to_server(paused)
        self.page.settle(0.8)

        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", "迟到的成功读取把页面劫持回了编辑页")
        self.assertEqual(snap["formHeading"], "新建问卷草稿")
        self.assertEqual(snap["title"], expected["title"], "首页输入的标题被覆盖")
        self.assertEqual(snap["description"], expected["description"], "首页输入的说明被覆盖")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], [o["value"] for o in q["options"]])
             for q in snap["questions"]],
            expected["questions"], "首页已输入的题目/选项被迟到结果覆盖")
        self.assertFalse(snap["loadErrorVisible"], "首页被插入了旧的加载错误提示")
        self.assertFalse(snap["bannerVisible"])
        self.assertNotIn("加载失败", snap["bodyText"], "首页可见文本出现旧的加载错误")
        # 再观察一拍，确认没有延迟跳转/改写；原稿未被动过，也没有新增问卷。
        self.page.settle(0.4)
        self.assertEqual(self.page.eval("location.hash"), "#/")
        self.assert_server_survey_intact(
            survey_id, self.api("GET", f"/api/surveys/{survey_id}")[1], "迟到成功后")
        self.assert_listing_ids(ids_before, "迟到成功后（首页输入尚未保存）")

    def test_late_failure_after_returning_home_is_ignored(self):
        """等待加载期间回首页填写新建表单，迟到的失败读取不能带回编辑页或插入错误提示。"""
        survey_id, survey = self.seed_survey()
        self.survey_id = survey_id
        before = self.api("GET", f"/api/surveys/{survey_id}")[1]

        paused = self.start_edit_and_hold(survey_id)
        self.page.eval("location.hash = '#/'")
        self.page.wait_for("location.hash === '#/' && !!document.querySelector('#draft-form')")
        self.page.t("setTitle", "首页新建的问卷二")
        self.page.t("addText", "首页文本题二", False)
        expected_title = "首页新建的问卷二"

        # 原编辑页的读取结果此刻才失败（网络错误）。
        self.page.fail_as_network_error(paused)
        self.page.settle(0.8)

        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", "迟到的失败读取把页面带回了编辑页")
        self.assertEqual(snap["formHeading"], "新建问卷草稿")
        self.assertEqual(snap["title"], expected_title, "首页输入被覆盖")
        self.assertEqual(len(snap["questions"]), 1)
        self.assertEqual(snap["questions"][0]["title"], "首页文本题二")
        self.assertFalse(snap["loadErrorVisible"], "首页被插入了旧编辑页的加载错误提示")
        self.assertNotIn("加载失败", snap["bodyText"], "首页出现旧的加载失败文案")
        self.page.settle(0.4)
        self.assertEqual(self.page.eval("location.hash"), "#/")
        # 原稿未被这次失败改变。
        self.assert_server_survey_intact(survey_id, before, "迟到失败后")

    def test_late_404_after_returning_home_is_ignored(self):
        """等待一个不存在编号期间回首页，迟到的 404 同样不能在首页插入“不存在”提示。"""
        existing_id, _ = self.seed_survey()
        missing_id = existing_id + 888_888

        paused = self.start_edit_and_hold(missing_id)
        self.assert_loading_only(self.page.snapshot(), "等待 404 期间")
        self.page.eval("location.hash = '#/'")
        self.page.wait_for("location.hash === '#/' && !!document.querySelector('#draft-form')")
        self.page.t("setTitle", "与缺失编号无关的新建")
        self.page.t("addText", "无关题目", True)

        self.page.fulfill_json(paused, 404, {"error": "not found"})
        self.page.settle(0.8)

        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", "迟到的 404 把页面带回了缺失编号的编辑页")
        self.assertEqual(snap["title"], "与缺失编号无关的新建")
        self.assertFalse(snap["loadErrorVisible"], "首页被插入了旧的“问卷不存在”提示")
        self.assertNotIn("不存在", snap["bodyText"], "首页出现旧的不存在提示")


if __name__ == "__main__":
    unittest.main(verbosity=2)
