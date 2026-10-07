#!/usr/bin/env python3
"""首页问卷列表“读取失败 / 正文不可用”提示的浏览器自动化回归保障。

页面已经区分“读取异常”与“真实空列表”，本文件用真实 Chrome 驱动真实页面，
保护以下现有行为，覆盖两个触发时机：**用户打开首页时的首次读取**，以及
**新建草稿成功、用户继续修改而留在首页时触发的列表刷新读取**。

非成功 HTTP 状态：
- 列表区域显示加载失败，并注明实际状态码（如 500、503、502 必须出现在提示中）。
- 失败响应正文是错误 JSON 对象、普通文字还是空内容，判断都不受影响。
- 即使失败响应中碰巧带有合法的 surveys 数组（无论非空还是空数组），也不能展示
  其中问卷，更不能显示“还没有问卷记录”，失败提示中不应残留任何问卷链接。

网络中断（请求没有得到响应）：
- 继续沿用现有的网络失败提示“问卷列表加载失败。”，不编造任何 HTTP 状态码。

成功状态但正文不可用：
- 正文无法解析为 JSON、解析后缺少 surveys 字段、surveys 不是数组（含顶层不是
  对象）时，列表区域提示“返回内容无法作为问卷列表使用”。
- 用户不应看到空白区域、一直停留的“加载中…”，或被继续保留的旧问卷条目误导。
- 只有成功取得合法空数组才显示“还没有问卷记录。”；非空数组按返回顺序展示
  问卷编号、标题与详情链接，并能替换之前的失败提示。

列表读取结果只影响列表区域：
- 等待期间（首次读取未返回时）已输入的标题、说明、题目、选项、必填勾选保留。
- 创建已保存且用户留在首页继续修改时，刷新失败不能抹掉“已保存”与“还有未保存
  的修改”两条提示，也不能改写表单；原有的新建、详情、编辑功能继续可用。

新旧读取竞争（同一次首页停留）：
- 创建成功触发的较新读取已经显示 HTTP 失败或正文不可用提示后，较早发起的读取
  随后成功（旧条目或空列表）也不能把提示覆盖掉。

时序全部在网络层制造：通过 Chrome DevTools Protocol 的 Fetch 域挂起
GET/POST /api/surveys，由测试决定每次读取何时返回、返回什么状态码与正文
（可用 Fetch.fulfillRequest 伪造任意正文，或 failRequest 模拟网络中断）。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_home_list_load_failure -v
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

    def wait_for_listing_text(self, substring, timeout=8):
        """轮询列表区域，直到其文本包含 substring，返回列表快照。"""
        end = time.time() + timeout
        last = None
        while time.time() < end:
            listing = self.listing()
            last = listing
            if listing["present"] and substring in (listing["text"] or ""):
                return listing
            time.sleep(0.12)
        raise AssertionError(
            f"列表区域未出现期望文本 {substring!r}，最后状态：{last}")

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

    def fulfill_text(self, paused, status, text,
                     content_type="text/plain; charset=utf-8"):
        """让挂起的请求以任意原始正文（普通文字/空内容/伪造非法 JSON）返回。"""
        body = text.encode("utf-8")
        self.call("Fetch.fulfillRequest", {
            "requestId": paused["requestId"],
            "responseCode": status,
            "responseHeaders": [{"name": "Content-Type", "value": content_type}],
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

HTTP_FAIL_PREFIX = "问卷列表加载失败（HTTP "
BODY_FAIL_HINT = "问卷列表加载失败：返回内容无法作为问卷列表使用。"


class HomeListLoadFailureTests(unittest.TestCase):
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
        # 每个用例独立服务与数据目录，列表内容/编号都从空库开始，避免相互干扰。
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

    def open_home_with_held_read(self):
        """打开首页并挂起列表读取；返回被挂起的首次读取（尚未返回）。"""
        page = self.page
        page.hold_reads()
        page.open(f"{self.srv.base_url}/#/")
        page.wait_for("!!document.querySelector('#draft-form')")
        return page.held_list(0)

    def trigger_home_reread(self):
        """在不离开“首页”的前提下重新路由一次，产生新一代首页与新的挂起读取。

        首页列表本身没有“重试”按钮，只有进入首页与创建成功会发起读取；这里通过
        重新派发 hashchange 让路由重新渲染首页（activeView 与读取序号都递增），
        从而在同一停留会话中得到一次新的、被挂起的列表读取。
        """
        before = self.page.list_read_count()
        self.page.eval("window.dispatchEvent(new Event('hashchange'))")
        end = time.time() + 8
        while time.time() < end:
            if self.page.list_read_count() >= before + 1:
                return self.page.held_list(before)
            time.sleep(0.1)
        raise AssertionError("重新路由后新的列表读取未发出")

    def assert_loading_shown(self, where):
        """读取尚未返回时列表停在“加载中…”，不能空白。"""
        listing = self.page.listing()
        self.assertTrue(listing["present"], f"{where}：列表区域不见了")
        self.assertIn("加载中", listing["text"], f"{where}：没有显示加载中")

    def assert_http_failure(self, status_code, where):
        """列表区域显示加载失败并注明实际状态码，且无任何问卷链接。"""
        listing = self.page.wait_for_listing_text(HTTP_FAIL_PREFIX)
        self.assertTrue(listing["present"], f"{where}：列表区域不见了")
        self.assertEqual(len(listing["items"]), 1,
                         f"{where}：失败提示应只有一条，实际 {listing['items']}")
        message = listing["items"][0]
        self.assertTrue(message.startswith(HTTP_FAIL_PREFIX),
                        f"{where}：失败提示格式不对：{message!r}")
        self.assertIn(str(status_code), message,
                      f"{where}：提示未注明实际状态码 {status_code}：{message!r}")
        self.assertIn(f"（HTTP {status_code}）", message,
                      f"{where}：状态码未以（HTTP {status_code}）形式出现：{message!r}")
        self.assertEqual(listing["hrefs"], [],
                         f"{where}：失败时列表里不应残留任何问卷链接：{listing['hrefs']}")
        self.assertNotIn(EMPTY_HINT, listing["text"],
                         f"{where}：失败不能显示空列表提示")
        self.assertNotIn("加载中", listing["text"], f"{where}：一直停在加载中")
        return listing

    def assert_body_failure(self, where):
        listing = self.page.wait_for_listing_text(BODY_FAIL_HINT)
        self.assertEqual(listing["items"], [BODY_FAIL_HINT],
                         f"{where}：应只显示“正文不可用”失败提示：{listing['items']}")
        self.assertEqual(listing["hrefs"], [],
                         f"{where}：正文不可用时不应展示任何问卷链接")
        self.assertNotIn(EMPTY_HINT, listing["text"],
                         f"{where}：正文不可用不能退化为空列表提示")
        self.assertNotIn("加载中", listing["text"], f"{where}：一直停在加载中")
        return listing

    def assert_network_failure(self, where):
        """网络失败沿用现有提示，不编造状态码。"""
        listing = self.page.wait_for_listing_text(FAILURE_HINT)
        self.assertEqual(listing["items"], [FAILURE_HINT],
                         f"{where}：网络失败提示应只有一条：{listing['items']}")
        self.assertNotIn("HTTP", listing["text"],
                         f"{where}：网络失败不应编造状态码：{listing['text']}")
        self.assertNotIn(str(500), listing["text"],
                         f"{where}：网络失败不应出现状态码：{listing['text']}")
        self.assertEqual(listing["hrefs"], [], f"{where}：网络失败不应展示问卷链接")
        return listing

    def assert_empty_hint(self, where):
        listing = self.page.wait_for_listing_text(EMPTY_HINT)
        self.assertEqual(listing["items"], [EMPTY_HINT],
                         f"{where}：合法空数组应显示空列表提示：{listing['items']}")
        self.assertEqual(listing["hrefs"], [])
        self.assertNotIn("加载中", listing["text"])
        return listing

    def assert_items(self, expected_items, where):
        """非空数组：按返回顺序展示编号+标题与详情链接。"""
        text = "#" + str(expected_items[0][0])
        self.page.wait_for(
            f"[...document.querySelectorAll('#survey-list a')].some(a => "
            f"a.textContent.includes({json.dumps(text, ensure_ascii=False)}))")
        listing = self.page.listing()
        wanted = [f"#{i} {t}" for (i, t) in expected_items]
        self.assertEqual(listing["items"], wanted,
                         f"{where}：列表条目或顺序不对：{listing['items']}")
        self.assertEqual(listing["hrefs"], [f"#/surveys/{i}" for (i, _t) in expected_items],
                         f"{where}：详情链接不对：{listing['hrefs']}")
        return listing

    # ---------- 首页首次读取：非成功 HTTP 状态 ----------

    def test_first_open_http_500_error_object_shows_code(self):
        """首次读取返回 500 且正文是错误 JSON 对象：提示含 500，不展示任何记录。"""
        held = self.open_home_with_held_read()
        self.assert_loading_shown("首次读取返回前")
        self.page.fulfill_json(held, 500, {"error": "服务器内部错误：database is locked"})
        self.assert_http_failure(500, "首次读取 500（错误对象）后")

    def test_first_open_http_503_plain_text_shows_code(self):
        """首次读取返回 503 且正文是普通文字：提示仍含 503。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_text(held, 503, "服务暂不可用，稍后再试")
        self.assert_http_failure(503, "首次读取 503（普通文字）后")

    def test_first_open_http_500_empty_body_shows_code(self):
        """首次读取返回 500 且正文为空：提示仍含 500，不能空白。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_text(held, 500, "")
        self.assert_http_failure(500, "首次读取 500（空正文）后")

    def test_first_open_http_502_with_surveys_array_must_not_render(self):
        """502 失败响应碰巧带合法非空 surveys 数组：不能展示其中问卷。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_json(held, 502, {
            "error": "bad gateway",
            "surveys": [
                {"id": 1, "title": "错误响应里夹带的问卷甲"},
                {"id": 2, "title": "错误响应里夹带的问卷乙"},
            ],
        })
        listing = self.assert_http_failure(502, "首次读取 502（夹带 surveys）后")
        self.assertNotIn("夹带的问卷", listing["text"],
                         "失败响应中的 surveys 内容绝不能被展示")

    def test_first_open_http_500_with_empty_surveys_must_not_show_empty_hint(self):
        """500 失败响应碰巧带空 surveys 数组：不能显示“还没有问卷记录”。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_json(held, 500, {"error": "boom", "surveys": []})
        listing = self.assert_http_failure(500, "首次读取 500（夹带空 surveys）后")
        self.assertNotIn(EMPTY_HINT, listing["text"])

    # ---------- 首页首次读取：网络中断 ----------

    def test_first_open_network_failure_keeps_plain_message(self):
        """首次读取网络中断：显示现有网络失败提示，不编造状态码。"""
        held = self.open_home_with_held_read()
        self.page.fail_as_network_error(held)
        self.assert_network_failure("首次读取网络失败后")

    # ---------- 首页首次读取：200 但正文不可用 ----------

    def test_first_open_200_invalid_json_is_body_failure(self):
        """200 正文无法解析为 JSON：提示正文不可用，不空白、不停在加载中。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_text(held, 200, "这不是JSON{,,",
                               content_type="application/json; charset=utf-8")
        self.assert_body_failure("首次读取 200（非法 JSON）后")

    def test_first_open_200_missing_surveys_is_body_failure(self):
        """200 JSON 缺少 surveys 字段：提示正文不可用，不能当成空列表。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_json(held, 200, {"items": [], "ok": True})
        self.assert_body_failure("首次读取 200（缺 surveys）后")

    def test_first_open_200_surveys_not_array_is_body_failure(self):
        """200 surveys 不是数组（对象/字符串/null）：提示正文不可用。"""
        cases = ({"surveys": {"id": 1}}, {"surveys": "[]"}, {"surveys": None})
        held = self.open_home_with_held_read()
        for i, bad in enumerate(cases):
            with self.subTest(bad=bad):
                if i > 0:
                    held = self.trigger_home_reread()
                self.page.fulfill_json(held, 200, bad)
                self.assert_body_failure(f"首次读取 200（surveys={bad!r}）后")

    def test_first_open_200_top_level_not_object_is_body_failure(self):
        """200 顶层不是 JSON 对象（数组/数字/字符串/null）：提示正文不可用。"""
        cases = ("[1,2,3]", "42", '"一串文字"', "null")
        held = self.open_home_with_held_read()
        for i, bad_text in enumerate(cases):
            with self.subTest(bad=bad_text):
                if i > 0:
                    held = self.trigger_home_reread()
                self.page.fulfill_text(held, 200, bad_text,
                                       content_type="application/json; charset=utf-8")
                self.assert_body_failure(f"首次读取 200（顶层 {bad_text}）后")

    # ---------- 首页首次读取：成功正文替换失败/加载提示 ----------

    def test_first_open_success_empty_array_shows_empty_hint(self):
        """200 合法空数组：显示空列表提示。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_json(held, 200, {"surveys": []})
        self.assert_empty_hint("首次读取 200（空数组）后")

    def test_first_open_success_nonempty_renders_ordered_links(self):
        """200 非空数组：按返回顺序展示编号、标题与详情链接，替换加载中。"""
        seed_id = self.seed_survey("已有问卷")
        held = self.open_home_with_held_read()
        # 注意：顺序故意不按编号升序，页面必须按返回顺序展示，不自行重排。
        payload = [
            {"id": seed_id, "title": "已有问卷"},
            {"id": 999, "title": "接口顺序里的第二份"},
        ]
        self.page.fulfill_json(held, 200, {"surveys": payload})
        listing = self.assert_items(
            [(seed_id, "已有问卷"), (999, "接口顺序里的第二份")],
            "首次读取 200（非空数组）后")
        self.assertNotIn("加载中", listing["text"])

    def test_first_open_failure_then_success_replaces_failure_hint(self):
        """同一停留会话中先失败、随后一次成功读取：成功列表必须替换失败提示。"""
        held = self.open_home_with_held_read()
        self.page.fulfill_json(held, 500, {"error": "boom"})
        self.assert_http_failure(500, "第一次读取 500 后")

        # 重新路由制造一次新的首页读取；它成功返回后失败提示必须被整体替换。
        second = self.trigger_home_reread()
        self.page.fulfill_json(second, 200,
                               {"surveys": [{"id": 7, "title": "重试成功的问卷"}]})
        listing = self.assert_items([(7, "重试成功的问卷")], "第二次读取成功后")
        self.assertNotIn("加载失败", listing["text"],
                         "成功列表必须替换之前的失败提示")
        self.assertNotIn("加载中", listing["text"])

    # ---------- 列表结果只影响列表区域：首次读取挂起期间的输入全部保留 ----------

    def test_pending_read_does_not_block_form_and_inputs_survive_failure(self):
        """首次读取未返回时可正常填写；读取失败后已填写内容一项都不能丢。"""
        held = self.open_home_with_held_read()
        self.page.t("setTitle", "等待读取时填的标题")
        self.page.t("setDesc", "等待读取时填的说明\n第二行")
        self.page.t("addText", "文本题甲", True)
        self.page.t("addChoice", "单选题乙", ["选项一", "选项二"], False)
        self.assert_loading_shown("填写表单期间")

        self.page.fulfill_json(held, 500, {"error": "boom"})
        self.assert_http_failure(500, "读取失败后")

        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", "读取失败不应导致跳页")
        self.assertEqual(snap["title"], "等待读取时填的标题", "标题被列表失败抹掉")
        self.assertEqual(snap["description"], "等待读取时填的说明\n第二行",
                         "说明被列表失败抹掉")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"]) for q in snap["questions"]],
            [("text", "文本题甲", True, []),
             ("single_choice", "单选题乙", False, ["选项一", "选项二"])],
            "题目/选项/必填被列表失败改写")
        # 新建功能仍可用：这份填写好的草稿可以真正提交保存。保持拦截开启（不先
        # Fetch.disable，避免 POST 在取消拦截的瞬间被孤儿化而永远收不到响应），
        # 像创建用例一样取出挂起的 POST 再显式放行到真实服务器。
        before_ids = {s["id"] for s in self.api("GET", "/api/surveys")[1]["surveys"]}
        self.page.t("submit")
        post = self.page.pop_held_post()
        self.assertEqual(post["request"]["method"], "POST")
        self.assertEqual(self.page.held_body(post)["title"], "等待读取时填的标题")
        self.page.release_to_server(post)
        # 创建成功且无新改动时进入详情；创建还会触发一次新的列表读取（同样被挂
        # 起，但不影响这次断言）。
        self.page.wait_for(
            "/^#\\/surveys\\/\\d+$/.test(location.hash) && "
            "!!document.querySelector('h2')")
        new_ids = {s["id"] for s in self.api("GET", "/api/surveys")[1]["surveys"]} - before_ids
        self.assertEqual(len(new_ids), 1, "读取失败后新建未能保存一条草稿")
        new_id = new_ids.pop()
        self.assertEqual(self.page.eval("document.querySelector('h2').textContent"),
                         f"#{new_id} 等待读取时填的标题",
                         "读取失败后新建的草稿未能正常保存/进入详情")

    # ---------- 创建成功后留在首页：刷新读取的各类失败 ----------

    def create_draft_and_stay_on_home(self, title="刷新流程问卷"):
        """制造“首次读取已挂起、创建成功、用户留在首页继续改”的现场。

        与 stale 用例同款现场，但首次读取保持挂起（用于竞争场景）或随后单独
        处理；返回 (initial_read, refresh_read, new_id)，两次读取均尚未返回。
        """
        page = self.page
        initial_read = self.open_home_with_held_read()
        page.t("setTitle", title)
        page.t("setDesc", "创建时保存的说明")
        page.t("addText", "文本题一", True)
        page.t("addChoice", "单选题一", ["选项甲", "选项乙"], False)
        page.t("submit")
        post = page.pop_held_post()
        # 等待保存结果期间继续修改，使创建成功后留在首页表单。
        page.t("setTitle", "等待期间改的新标题")
        page.t("setDesc", "等待期间改的新说明")
        page.release_to_server(post)
        refresh_read = page.held_list(1)
        page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent.startsWith('编辑问卷草稿 #')")
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        new_id = data["surveys"][-1]["id"]
        return initial_read, refresh_read, new_id

    def assert_form_kept_after_refresh_failure(self, new_id, where):
        """刷新失败：已保存结果、未保存提示、表单内容与编辑功能都不能受影响。"""
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", f"{where}：页面离开了首页")
        self.assertEqual(snap["formHeading"], f"编辑问卷草稿 #{new_id}",
                         f"{where}：表单编号被改写")
        self.assertEqual(snap["title"], "等待期间改的新标题",
                         f"{where}：等待期间的标题被清空或恢复")
        self.assertEqual(snap["description"], "等待期间改的新说明",
                         f"{where}：等待期间的说明被清空或恢复")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"]) for q in snap["questions"]],
            [("text", "文本题一", True, []),
             ("single_choice", "单选题一", False, ["选项甲", "选项乙"])],
            f"{where}：题目/选项/必填被刷新失败改写")
        self.assertFalse(snap["bannerVisible"], f"{where}：表单冒出错误提示条")
        self.assertIn("已保存", snap["saveStatusText"],
                      f"{where}：已保存提示被刷新失败抹掉")
        self.assertIn("未保存的修改", snap["saveStatusText"],
                      f"{where}：未保存提示被刷新失败抹掉")

    def test_refresh_http_500_after_create_stays_failure_and_preserves_save(self):
        """创建后刷新读取返回 500：列表显示含 500 的失败提示，保存结果不受影响。"""
        _initial, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_json(refresh_read, 500, {"error": "boom"})
        self.assert_http_failure(500, "创建后刷新 500 后")
        self.assert_form_kept_after_refresh_failure(new_id, "创建后刷新 500 后")

    def test_refresh_http_503_empty_body_after_create_preserves_save(self):
        """创建后刷新读取返回 503 空正文：提示含 503，保存与未保存提示保留。"""
        _initial, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_text(refresh_read, 503, "")
        self.assert_http_failure(503, "创建后刷新 503 空正文后")
        self.assert_form_kept_after_refresh_failure(new_id, "创建后刷新 503 空正文后")

    def test_refresh_http_error_with_surveys_after_create_does_not_render(self):
        """创建后刷新 500 且夹带 surveys（含新草稿编号）：仍只显示失败提示。"""
        _initial, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_json(refresh_read, 500, {
            "error": "boom",
            "surveys": [{"id": new_id, "title": "夹带的新草稿"}],
        })
        listing = self.assert_http_failure(500, "创建后刷新 500（夹带 surveys）后")
        self.assertNotIn("夹带的新草稿", listing["text"])
        self.assert_form_kept_after_refresh_failure(
            new_id, "创建后刷新 500（夹带 surveys）后")

    def test_refresh_network_failure_after_create_preserves_save(self):
        """创建后刷新网络中断：网络失败提示无状态码，保存结果与未保存提示保留。"""
        _initial, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fail_as_network_error(refresh_read)
        self.assert_network_failure("创建后刷新网络失败后")
        self.assert_form_kept_after_refresh_failure(new_id, "创建后刷新网络失败后")

    def test_refresh_invalid_json_after_create_preserves_save(self):
        """创建后刷新 200 但正文非法 JSON：正文不可用提示，保存结果保留。"""
        _initial, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_text(refresh_read, 200, "not json {",
                               content_type="application/json; charset=utf-8")
        self.assert_body_failure("创建后刷新 200（非法 JSON）后")
        self.assert_form_kept_after_refresh_failure(new_id, "创建后刷新 200（非法 JSON）后")

    def test_refresh_missing_surveys_after_create_preserves_save(self):
        """创建后刷新 200 但缺 surveys：正文不可用提示，保存结果保留。"""
        _initial, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_json(refresh_read, 200, {"nope": []})
        self.assert_body_failure("创建后刷新 200（缺 surveys）后")
        self.assert_form_kept_after_refresh_failure(new_id, "创建后刷新 200（缺 surveys）后")

    def test_refresh_success_empty_after_create_replaces_and_detail_works(self):
        """创建后刷新成功返回（含新草稿）：替换失败前态，详情链接可打开。"""
        _initial, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_json(
            refresh_read, 200,
            {"surveys": [{"id": new_id, "title": "刷新流程问卷"}]})
        self.assert_items([(new_id, "刷新流程问卷")], "创建后刷新成功后")
        self.assert_form_kept_after_refresh_failure(new_id, "创建后刷新成功后")
        # 详情功能继续可用。
        self.page.t("clickHref", f"#/surveys/{new_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{new_id}' && "
            f"document.querySelector('h2') && "
            f"document.querySelector('h2').textContent === '#{new_id} 刷新流程问卷'")

    # ---------- 较新刷新已显示失败：较早读取随后成功也不能覆盖 ----------

    def test_stale_success_cannot_replace_refresh_http_failure(self):
        """较新刷新已显示 500 失败：较早读取随后成功返回旧条目也不能覆盖。"""
        seed_id = self.seed_survey("已有问卷")
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()

        # 较新的刷新读取先返回 HTTP 500（夹带 surveys 也不能展示）。
        self.page.fulfill_json(refresh_read, 500, {
            "error": "boom",
            "surveys": [{"id": new_id, "title": "不应出现的新草稿"}],
        })
        self.assert_http_failure(500, "刷新 500 后")

        # 较早发起的首次读取随后成功返回创建前的旧条目。
        self.page.fulfill_json(
            initial_read, 200,
            {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)

        listing = self.page.listing()
        self.assertEqual(len(listing["items"]), 1)
        message = listing["items"][0]
        self.assertIn("500", message, "较早读取成功后失败提示被旧条目覆盖")
        self.assertNotIn("已有问卷", listing["text"], "旧条目被渲染了出来")
        self.assertNotIn("不应出现的新草稿", listing["text"])
        self.assert_form_kept_after_refresh_failure(new_id, "旧读取成功后")
        self.page.settle(0.4)
        self.assertEqual(len(self.page.listing()["items"]), 1, "延迟一拍后失败提示被覆盖")

    def test_stale_empty_cannot_replace_refresh_http_failure(self):
        """较新刷新 503 已显示失败：较早读取随后返回空列表也不能换成空提示。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_json(refresh_read, 503, {"error": "unavailable"})
        self.assert_http_failure(503, "刷新 503 后")

        self.page.fulfill_json(initial_read, 200, {"surveys": []})
        self.page.settle(0.8)

        listing = self.page.listing()
        self.assertEqual(len(listing["items"]), 1)
        self.assertIn("503", listing["items"][0], "较早读取空列表把失败提示换掉了")
        self.assertNotIn(EMPTY_HINT, listing["text"], "失败提示被换成了空列表提示")
        self.assert_form_kept_after_refresh_failure(new_id, "旧读取空列表后")

    def test_stale_success_cannot_replace_refresh_body_failure(self):
        """较新刷新 200 正文不可用已提示：较早读取随后成功（旧条目/空）不能覆盖。"""
        seed_id = self.seed_survey("已有问卷")
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()

        self.page.fulfill_json(refresh_read, 200, {"surveys": "not-an-array"})
        self.assert_body_failure("刷新正文不可用后")

        # 较早读取随后返回创建前的旧条目（非空）。
        self.page.fulfill_json(
            initial_read, 200,
            {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], [BODY_FAIL_HINT],
                         "较早读取的旧条目覆盖了正文不可用提示")
        self.assertNotIn("已有问卷", listing["text"])
        self.assert_form_kept_after_refresh_failure(new_id, "旧读取旧条目后")

    def test_stale_empty_cannot_replace_refresh_body_failure(self):
        """较新刷新正文不可用已提示：较早读取随后返回合法空数组也不能换空提示。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_text(refresh_read, 200, "{",
                               content_type="application/json; charset=utf-8")
        self.assert_body_failure("刷新非法 JSON 后")

        self.page.fulfill_json(initial_read, 200, {"surveys": []})
        self.page.settle(0.8)
        self.assertEqual(self.page.listing()["items"], [BODY_FAIL_HINT],
                         "较早读取的空数组把正文不可用提示换成了空列表提示")
        self.assert_form_kept_after_refresh_failure(new_id, "旧读取空数组后")


if __name__ == "__main__":
    unittest.main(verbosity=2)
