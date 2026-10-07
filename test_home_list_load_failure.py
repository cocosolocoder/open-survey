#!/usr/bin/env python3
"""首页问卷列表“加载失败提示”的浏览器回归保障。

页面已经区分了“列表读取异常”与“真实空列表”（见 app.py 首页 loadSurveys），
本文件用真实 Chrome 驱动真实页面，保护这项现有行为，覆盖两个读取时机：

1. 用户打开首页时的首次列表读取；
2. 首页新建草稿成功、用户等待保存结果期间继续修改而留在首页（表单就地转为
   “编辑刚创建的草稿”）时，创建成功触发的较新列表读取。

保护的行为（全部以用户可见的 DOM、表单值与重新读取的接口结果为断言依据）：

- 列表读取返回非成功 HTTP 状态（500/404 等）：列表区域显示加载失败并注明
  实际状态码（提示里能看到 500）。正文是错误对象、普通文字或空内容都不影响；
  即使失败响应碰巧带有合法的 surveys 数组，也不展示其中问卷，更不会因数组
  为空而显示“还没有问卷记录”。
- 请求因网络中断没有得到响应：保留现有网络失败提示，不编造状态码。
- 成功响应仍需可用正文：正文无法解析为 JSON、解析后缺少 surveys、surveys
  不是数组时，列表区域说明“返回内容无法作为问卷列表使用”；不出现空白区域、
  不停留“加载中”，也不保留旧条目冒充成功。只有合法空数组才显示“还没有问卷
  记录”；非空数组按返回顺序展示编号、标题与详情链接，并替换此前的失败提示。
- 列表读取结果只影响列表区域：等待期间已输入的标题、说明、题目、选项与必填
  勾选都保留；草稿已保存但用户继续修改而留在首页时，列表刷新失败也不能抹掉
  “已保存”与“还有未保存修改”的提示，新建、详情、编辑功能继续可用。
- 同一次首页停留中，较晚发起的读取已显示 HTTP 失败或正文不可用提示后，较早
  发起的读取随后成功返回列表，也不能把提示覆盖成旧条目或空列表。

测试在网络层制造结果与时序：通过 Chrome DevTools Protocol 的 Fetch 域挂起
GET/POST /api/surveys，由测试决定每次读取何时、以何种状态码、正文（任意字节
与 Content-Type）返回，或直接制造网络失败。

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
NETWORK_FAILURE_HINT = "问卷列表加载失败。"
BODY_UNUSABLE_HINT = "问卷列表加载失败：返回内容无法作为问卷列表使用。"


def http_failure_hint(status):
    """非成功 HTTP 状态对应的失败提示：必须注明实际状态码。"""
    return f"问卷列表加载失败（HTTP {status}）。"


# --------------------------------------------------------------------------
# 真实 app.py 服务进程（与其它浏览器回归测试同款夹具）
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

# 表单区域快照——全部来自用户可见的 DOM 与表单值。列表读取结果不得改动其中
# 任何一项（本文件不涉及离开首页，因此页面上总有新建/编辑表单）。
SNAPSHOT_JS = r"""
(() => {
  const banner = document.getElementById('form-banner');
  const titleEl = document.getElementById('survey-title');
  const descEl = document.getElementById('survey-desc');
  const formHeading = document.querySelector('#draft-form h2');
  const status = document.getElementById('save-status');
  const submit = document.querySelector('#draft-form button[type=submit]');
  return {
    hash: location.hash,
    formHeading: formHeading ? formHeading.textContent : null,
    bannerVisible: banner ? !banner.hidden : false,
    bannerText: banner ? banner.textContent : '',
    saveStatusText: status && !status.hidden ? status.innerText : '',
    submitLabel: submit ? submit.textContent.trim() : null,
    submitDisabled: submit ? submit.disabled : null,
    title: titleEl ? titleEl.value : null,
    description: descEl ? descEl.value : null,
    questions: [...document.querySelectorAll('.q-card')].map(card => ({
      type: card.dataset.type,
      title: card.querySelector('.q-title').value,
      required: card.querySelector('.q-required').checked,
      options: [...card.querySelectorAll('.opt-text')].map(o => o.value),
    })),
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
        self.fulfill_raw(paused, status,
                         json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    def fulfill_raw(self, paused, status, body, content_type):
        """以任意状态码、任意字节正文与 Content-Type 返回挂起的请求。

        用于覆盖“正文是普通文字/空内容/不是合法 JSON”等失败与异常场景。
        """
        if isinstance(body, str):
            body = body.encode("utf-8")
        params = {
            "requestId": paused["requestId"],
            "responseCode": status,
            "responseHeaders": [{"name": "Content-Type", "value": content_type}],
            "body": base64.b64encode(body).decode("ascii"),
        }
        self.call("Fetch.fulfillRequest", params)

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
        # 每个用例独立的服务与数据目录：列表内容、编号都从空库开始。
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
        """开启拦截并打开首页，返回被挂起的首次列表读取。

        首次读取未返回期间，列表应停在“加载中…”，新建表单可正常填写。
        """
        page = self.page
        page.hold_reads()
        page.open(f"{self.srv.base_url}/#/")
        page.wait_for("!!document.querySelector('#draft-form')")
        read = page.held_list(0)
        self.assertIn("加载中", page.listing()["text"],
                      "首次读取未返回时列表应显示加载中")
        return read

    def wait_list_item(self, expected):
        expected_js = json.dumps(expected, ensure_ascii=False)
        self.page.wait_for(
            "(function () { var l = document.getElementById('survey-list'); "
            "var items = l ? [].slice.call(l.querySelectorAll('li')).map("
            "function (x) { return x.innerText; }) : []; "
            "return items.length === 1 && items[0] === " + expected_js + "; })()")

    def assert_single_list_item(self, expected, where):
        listing = self.page.listing()
        self.assertTrue(listing["present"], f"{where}：列表区域不见了（不能是空白）")
        self.assertEqual(listing["items"], [expected], f"{where}：列表提示不符合预期")
        self.assertEqual(listing["hrefs"], [], f"{where}：失败提示里不应出现问卷链接")
        self.assertNotIn("加载中", listing["text"], f"{where}：列表停在了加载中")
        return listing

    def assert_form_inputs_kept(self, where):
        """首次读取失败前后，用户已填写的表单内容一项都不能丢。"""
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", f"{where}：页面离开了首页")
        self.assertEqual(snap["formHeading"], "新建问卷草稿",
                         f"{where}：仍应停留在新建表单")
        self.assertEqual(snap["title"], "等待期间填写的标题",
                         f"{where}：已填写的问卷标题被清空或改写")
        self.assertEqual(snap["description"], "等待期间填写的说明",
                         f"{where}：已填写的问卷说明被清空或改写")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snap["questions"]],
            [("text", "文本题甲", True, []),
             ("single_choice", "单选题乙", False, ["选项子", "选项丑"])],
            f"{where}：题目/选项/必填设置被列表读取改写")
        self.assertFalse(snap["bannerVisible"], f"{where}：列表读取不应在表单上产生错误提示条")
        self.assertEqual(snap["submitLabel"], "保存整份问卷",
                         f"{where}：新建表单的保存按钮不应被列表读取改变")
        self.assertFalse(snap["submitDisabled"],
                         f"{where}：列表读取不应锁住新建表单的保存按钮")

    def fill_draft_while_read_pending(self):
        """首次列表读取挂起期间，在新建表单里填入标题/说明/题目/选项/必填。"""
        page = self.page
        page.t("setTitle", "等待期间填写的标题")
        page.t("setDesc", "等待期间填写的说明")
        page.t("addText", "文本题甲", True)
        page.t("addChoice", "单选题乙", ["选项子", "选项丑"], False)

    # ==================================================================
    # 一、打开首页：非成功 HTTP 状态一律显示“加载失败 + 实际状态码”
    # ==================================================================

    def test_http_500_json_error_shows_failure_with_status(self):
        """500 且正文是错误对象：显示加载失败，提示中能看到 500。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_json(read, 500, {"error": "服务器内部错误"})
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        self.assert_single_list_item(hint, "500 错误对象返回后")
        self.assertNotIn(EMPTY_HINT, self.page.listing()["text"])

    def test_http_500_plain_text_shows_failure_with_status(self):
        """500 且正文是普通文字：判断只取决于状态码，仍显示带 500 的失败提示。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_raw(read, 500, "服务器开小差了，请稍后再试",
                              "text/plain; charset=utf-8")
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        self.assert_single_list_item(hint, "500 普通文字返回后")

    def test_http_500_empty_body_shows_failure_with_status(self):
        """500 且正文为空内容：同样显示带 500 的失败提示。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_raw(read, 500, b"", "application/json; charset=utf-8")
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        self.assert_single_list_item(hint, "500 空正文返回后")

    def test_http_404_status_is_shown_verbatim(self):
        """其它非成功状态（404）：提示注明的必须是实际状态码 404，不套用 500。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_json(read, 404, {"error": "not found"})
        hint = http_failure_hint(404)
        self.wait_list_item(hint)
        listing = self.assert_single_list_item(hint, "404 返回后")
        self.assertNotIn("500", listing["text"], "提示中不得编造其它状态码")

    def test_http_error_with_surveys_array_does_not_render_surveys(self):
        """失败响应碰巧带合法非空 surveys 数组：也不能展示其中问卷。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_json(read, 500, {"surveys": [
            {"id": 999, "title": "失败正文里夹带的问卷"},
        ]})
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        listing = self.assert_single_list_item(hint, "500 夹带 surveys 返回后")
        self.assertNotIn("失败正文里夹带的问卷", listing["text"])
        self.assertNotIn("#999", listing["text"])

    def test_http_error_with_empty_surveys_does_not_show_empty_hint(self):
        """失败响应碰巧带空 surveys 数组：不能因此显示“还没有问卷记录”。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_json(read, 500, {"surveys": []})
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        self.assert_single_list_item(hint, "500 夹带空 surveys 返回后")

    def test_network_failure_keeps_existing_message_without_status(self):
        """网络中断没有响应：保留网络失败提示，不编造状态码。"""
        read = self.open_home_with_held_read()
        self.page.fail_as_network_error(read)
        self.wait_list_item(NETWORK_FAILURE_HINT)
        listing = self.assert_single_list_item(
            NETWORK_FAILURE_HINT, "网络失败返回后")
        self.assertNotIn("HTTP", listing["text"], "网络失败提示中不得编造状态码")
        self.assertNotIn("500", listing["text"])
        self.assertNotIn(EMPTY_HINT, listing["text"])

    # ==================================================================
    # 二、打开首页：读取失败不得影响用户已填写的表单内容
    # ==================================================================

    def test_failed_read_preserves_form_inputs(self):
        """等待列表期间已输入的标题/说明/题目/选项/必填，在读取失败后保留。"""
        read = self.open_home_with_held_read()
        self.fill_draft_while_read_pending()
        self.assert_form_inputs_kept("首次读取失败前")

        self.page.fulfill_json(read, 500, {"error": "boom"})
        self.wait_list_item(http_failure_hint(500))
        self.assert_form_inputs_kept("500 返回后")

        # 再观察一拍：失败提示稳定，表单内容仍在，新建功能可继续使用。
        self.page.settle(0.4)
        self.assert_form_inputs_kept("500 返回后再次观察")

    def test_network_failure_preserves_form_inputs(self):
        """网络失败同样不得触碰等待期间已填写的表单内容。"""
        read = self.open_home_with_held_read()
        self.fill_draft_while_read_pending()
        self.page.fail_as_network_error(read)
        self.wait_list_item(NETWORK_FAILURE_HINT)
        self.assert_form_inputs_kept("网络失败返回后")

    # ==================================================================
    # 三、打开首页：成功 HTTP 但正文无法作为问卷列表使用
    # ==================================================================

    def test_success_invalid_json_shows_unusable_body_failure(self):
        """200 但正文不是合法 JSON：说明返回内容无法作为问卷列表使用。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_raw(read, 200, "这不是JSON，也不是数组",
                              "application/json; charset=utf-8")
        self.wait_list_item(BODY_UNUSABLE_HINT)
        self.assert_single_list_item(BODY_UNUSABLE_HINT, "200 非法 JSON 返回后")
        self.assertNotIn(EMPTY_HINT, self.page.listing()["text"])

    def test_success_plain_text_shows_unusable_body_failure(self):
        """200 但正文是普通文字：同样无法作为问卷列表使用。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_raw(read, 200, "Internal maintenance",
                              "text/plain; charset=utf-8")
        self.wait_list_item(BODY_UNUSABLE_HINT)
        self.assert_single_list_item(BODY_UNUSABLE_HINT, "200 普通文字返回后")

    def test_success_missing_surveys_shows_unusable_body_failure(self):
        """200 且为合法 JSON，但缺少 surveys 字段：按加载失败处理。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_json(read, 200, {"items": []})
        self.wait_list_item(BODY_UNUSABLE_HINT)
        self.assert_single_list_item(BODY_UNUSABLE_HINT, "200 缺少 surveys 返回后")

    def test_success_surveys_not_array_shows_unusable_body_failure(self):
        """200 且有 surveys 字段但不是数组：按加载失败处理。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_json(read, 200, {"surveys": {"id": 1, "title": "不是数组"}})
        self.wait_list_item(BODY_UNUSABLE_HINT)
        self.assert_single_list_item(BODY_UNUSABLE_HINT, "surveys 非数组返回后")
        self.assertNotIn("不是数组", self.page.listing()["text"],
                         "surveys 不是数组时不能渲染其中的标题")

    def test_failure_then_unusable_refresh_after_create_replaces_hint(self):
        """首次读取已显示 HTTP 失败；创建成功留在首页触发的较新读取正文不可用：

        新读取的“正文无法作为问卷列表使用”提示应替换此前的 HTTP 失败提示，
        已保存结果与未保存修改提示、表单内容继续保留。
        """
        page = self.page
        page.hold_reads()
        page.open(f"{self.srv.base_url}/#/")
        page.wait_for("!!document.querySelector('#draft-form')")
        initial_read = page.held_list(0)

        # 填写有效草稿并提交（POST 挂起）。
        page.t("setTitle", "创建时保存的标题")
        page.t("setDesc", "创建时保存的说明")
        page.t("addText", "文本题一", True)
        page.t("addChoice", "单选题一", ["选项甲", "选项乙"], False)
        page.t("submit")
        post = page.pop_held_post()

        # 创建请求尚未返回、刷新读取尚未发起时，首次读取是当前最新读取：
        # 先让它显示 HTTP 500 失败提示。
        page.fulfill_json(initial_read, 500, {"error": "boom"})
        self.wait_list_item(http_failure_hint(500))

        # 等待保存结果期间继续修改：创建成功后留在当前表单，并发起较新读取。
        page.t("setTitle", "保存后继续改的标题")
        page.t("setDesc", "保存后继续改的说明")
        page.release_to_server(post)
        refresh_read = page.held_list(1)
        page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent.startsWith('编辑问卷草稿 #')")
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        new_id = data["surveys"][-1]["id"]

        # 较新读取返回 200 但 surveys 不是数组：提示换成“正文无法作为问卷列表
        # 使用”，旧的 HTTP 失败提示不应残留，保存结果与表单继续保留。
        page.fulfill_json(refresh_read, 200, {"surveys": {"id": new_id}})
        self.wait_list_item(BODY_UNUSABLE_HINT)
        self.assert_single_list_item(BODY_UNUSABLE_HINT, "创建后刷新正文不可用后")
        self.assertNotIn("500", page.listing()["text"],
                         "旧的 HTTP 失败提示不应残留")
        self.assert_stay_on_home_form_kept(new_id, "创建后刷新正文不可用后")

    # ==================================================================
    # 四、打开首页：只有合法正文才算成功（空提示 / 非空列表替换失败提示）
    # ==================================================================

    def test_valid_empty_array_shows_empty_hint(self):
        """合法空数组：才显示“还没有问卷记录”，且不再停留加载中。"""
        read = self.open_home_with_held_read()
        self.page.fulfill_json(read, 200, {"surveys": []})
        self.wait_list_item(EMPTY_HINT)
        listing = self.assert_single_list_item(EMPTY_HINT, "合法空数组返回后")
        self.assertNotIn("加载中", listing["text"])
        self.assertNotIn("加载失败", listing["text"])

    def test_nonempty_array_renders_entries_in_order_with_links(self):
        """合法非空数组：按返回顺序展示编号、标题与详情链接。"""
        read = self.open_home_with_held_read()
        surveys = [
            {"id": 3, "title": "第三份问卷"},
            {"id": 1, "title": "第一份问卷"},
            {"id": 2, "title": "第二份问卷"},
        ]
        self.page.fulfill_json(read, 200, {"surveys": surveys})
        self.page.wait_for(
            "document.querySelectorAll('#survey-list a[href]').length === 3")
        listing = self.page.listing()
        # 必须按返回顺序展示（这里返回顺序故意不是编号升序）。
        self.assertEqual(listing["items"],
                         ["#3 第三份问卷", "#1 第一份问卷", "#2 第二份问卷"])
        self.assertEqual(listing["hrefs"],
                         ["#/surveys/3", "#/surveys/1", "#/surveys/2"])

    def test_successful_list_replaces_previous_failure_hint(self):
        """列表先失败，随后一次成功读取：非空列表替换掉失败提示，不留旧提示。"""
        first = self.open_home_with_held_read()
        self.page.fulfill_json(first, 500, {"error": "boom"})
        self.wait_list_item(http_failure_hint(500))

        # 通过真实路由离开首页再返回，触发新的一次首页停留与首次读取（回首页
        # 的读取走真实服务器；先在库里准备一条问卷，使新读取为非空列表）。
        seed_id = self.seed_survey("恢复后的问卷")
        self.page.stop_holding()
        self.page.eval("location.hash = '#/surveys/1'")
        self.page.wait_for("location.hash === '#/surveys/1'")
        self.page.settle(0.3)
        self.page.eval("location.hash = '#/'")
        self.page.wait_for(
            "document.querySelectorAll('#survey-list a[href]').length === 1")
        listing = self.page.listing()
        self.assertEqual(listing["items"], [f"#{seed_id} 恢复后的问卷"])
        self.assertEqual(listing["hrefs"], [f"#/surveys/{seed_id}"])
        self.assertNotIn("加载失败", listing["text"])
        self.assertNotIn("加载中", listing["text"])

    # ==================================================================
    # 五、新建草稿成功后留在首页：刷新读取失败不得抹掉保存结果
    # ==================================================================

    def create_draft_and_stay_on_home(self):
        """制造“创建成功、等待期间继续修改而留在首页、刷新读取已挂起”的现场。

        打开首页（首次读取挂起并以伪造结果由调用方处理）→ 填写有效草稿并
        提交（POST 挂起）→ 等待期间继续修改标题与说明 → 放行 POST 到真实
        服务器（201，表单就地转为编辑刚创建的草稿，并发出刷新读取）。

        返回 (initial_read, refresh_read, new_id)。
        """
        page = self.page
        page.hold_reads()
        page.open(f"{self.srv.base_url}/#/")
        page.wait_for("!!document.querySelector('#draft-form')")
        initial_read = page.held_list(0)

        page.t("setTitle", "创建时保存的标题")
        page.t("setDesc", "创建时保存的说明")
        page.t("addText", "文本题一", True)
        page.t("addChoice", "单选题一", ["选项甲", "选项乙"], False)
        page.t("submit")
        post = page.pop_held_post()
        self.assertEqual(page.held_body(post)["title"], "创建时保存的标题")

        # 等待保存结果期间继续修改：创建成功后应留在当前表单。
        page.t("setTitle", "保存后继续改的标题")
        page.t("setDesc", "保存后继续改的说明")

        page.release_to_server(post)
        refresh_read = page.held_list(1)
        page.wait_for(
            "document.querySelector('#draft-form h2') && "
            "document.querySelector('#draft-form h2').textContent.startsWith('编辑问卷草稿 #')")

        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        new_id = data["surveys"][-1]["id"]
        self.assertEqual(data["surveys"][-1]["title"], "创建时保存的标题")
        return initial_read, refresh_read, new_id

    def assert_stay_on_home_form_kept(self, new_id, where):
        """刷新读取失败后：已保存结果与未保存修改提示、表单内容全部保留。"""
        snap = self.page.snapshot()
        self.assertEqual(snap["hash"], "#/", f"{where}：页面离开了首页")
        self.assertEqual(snap["formHeading"], f"编辑问卷草稿 #{new_id}",
                         f"{where}：表单应仍绑定刚创建并保存成功的编号 {new_id}")
        self.assertEqual(snap["title"], "保存后继续改的标题",
                         f"{where}：等待期间继续修改的标题被抹掉")
        self.assertEqual(snap["description"], "保存后继续改的说明",
                         f"{where}：等待期间继续修改的说明被抹掉")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in snap["questions"]],
            [("text", "文本题一", True, []),
             ("single_choice", "单选题一", False, ["选项甲", "选项乙"])],
            f"{where}：题目/选项/必填设置被列表刷新改写")
        self.assertFalse(snap["bannerVisible"], f"{where}：列表刷新不应在表单上产生错误提示条")
        self.assertIn("已保存", snap["saveStatusText"],
                      f"{where}：‘刚才提交的内容已保存’提示被列表刷新抹掉")
        self.assertIn("未保存的修改", snap["saveStatusText"],
                      f"{where}：‘当前还有未保存的修改’提示被列表刷新抹掉")
        self.assertEqual(snap["submitLabel"], "保存修改",
                         f"{where}：表单应仍是编辑刚创建草稿的状态（保存修改）")
        self.assertFalse(snap["submitDisabled"], f"{where}：保存按钮不应被列表刷新锁住")

    def test_refresh_http_failure_after_create_keeps_save_result(self):
        """创建成功留在首页后，刷新读取 500：列表失败，但保存结果与提示保留。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()

        # 较早发起的首次读取先按网络失败丢弃（它本就不该生效），聚焦刷新失败。
        self.page.fail_as_network_error(initial_read)
        self.page.settle(0.2)

        # 较晚发起、当前有效的刷新读取返回 500（夹带 surveys 也不许渲染）。
        self.page.fulfill_json(refresh_read, 500, {"surveys": [
            {"id": new_id, "title": "不应显示的标题"},
        ]})
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        listing = self.assert_single_list_item(hint, "创建后刷新 500 返回后")
        self.assertNotIn("不应显示的标题", listing["text"])
        self.assertNotIn(EMPTY_HINT, listing["text"])
        self.assert_stay_on_home_form_kept(new_id, "创建后刷新 500 返回后")

        # 保存确实落库：直接读接口确认刚创建的草稿就是点击保存那一刻的内容。
        status, detail = self.api("GET", f"/api/surveys/{new_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["title"], "创建时保存的标题")
        self.assertEqual(detail["description"], "创建时保存的说明")

    def test_refresh_unusable_body_after_create_keeps_save_result(self):
        """创建成功留在首页后，刷新读取 200 但正文不可用：提示明确，表单保留。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_json(initial_read, 200, {"surveys": []})
        self.page.settle(0.2)

        self.page.fulfill_json(refresh_read, 200, {"surveys": "not-an-array"})
        self.wait_list_item(BODY_UNUSABLE_HINT)
        self.assert_single_list_item(BODY_UNUSABLE_HINT, "创建后刷新正文不可用后")
        self.assert_stay_on_home_form_kept(new_id, "创建后刷新正文不可用后")

    def test_refresh_network_failure_after_create_keeps_save_result(self):
        """创建成功留在首页后，刷新读取网络失败：网络失败提示，不编造状态码。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fulfill_json(initial_read, 200, {"surveys": []})
        self.page.settle(0.2)

        self.page.fail_as_network_error(refresh_read)
        self.wait_list_item(NETWORK_FAILURE_HINT)
        listing = self.assert_single_list_item(
            NETWORK_FAILURE_HINT, "创建后刷新网络失败后")
        self.assertNotIn("HTTP", listing["text"])
        self.assert_stay_on_home_form_kept(new_id, "创建后刷新网络失败后")

    def test_after_failed_refresh_create_detail_and_edit_still_work(self):
        """刷新失败后：已保存草稿的详情与编辑仍可打开，且可再次保存（PUT 同号）。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()
        self.page.fail_as_network_error(initial_read)
        self.page.fulfill_json(refresh_read, 500, {"error": "boom"})
        self.wait_list_item(http_failure_hint(500))
        self.assert_stay_on_home_form_kept(new_id, "刷新失败后")

        # 列表读取失败不影响通过链接打开已保存草稿的详情：先停止拦截，避免
        # 后续详情 GET / PUT 被本用例的拦截策略影响（后台循环本会放行它们，
        # 这里直接关闭更稳妥）。
        self.page.stop_holding()
        # 用表单上的“取消”链接区不适用（会放弃修改）；通过注入一个指向详情的
        # 真实点击走页面既有链接。先直接导航到详情（与点击链接同一路由）。
        self.page.eval(f"location.hash = '#/surveys/{new_id}'")
        self.page.wait_for(
            f"location.hash === '#/surveys/{new_id}' && document.querySelector('h2') && "
            f"document.querySelector('h2').textContent === '#{new_id} 创建时保存的标题'")
        body = self.page.eval("document.body.innerText")
        self.assertIn("创建时保存的说明", body)
        self.assertIn("文本题一", body)
        self.assertIn("单选题一", body)

        # 详情页提供编辑入口；进入编辑应加载已保存内容。
        self.page.eval(f"location.hash = '#/surveys/{new_id}/edit'")
        self.page.wait_for(
            f"location.hash === '#/surveys/{new_id}/edit' && "
            "(document.getElementById('survey-title')||{}).value === '创建时保存的标题'")
        self.assertEqual(
            self.page.eval("document.getElementById('survey-desc').value"),
            "创建时保存的说明")

    def test_after_failed_refresh_new_create_still_works(self):
        """刷新失败后，新建功能继续可用：另建一份新草稿成功并出现第二个编号。"""
        initial_read, refresh_read, first_id = self.create_draft_and_stay_on_home()
        self.page.fail_as_network_error(initial_read)
        self.page.fulfill_json(refresh_read, 500, {"error": "boom"})
        self.wait_list_item(http_failure_hint(500))

        # 当前停在“编辑刚创建草稿”的表单；再保存属于 PUT 同一编号。这里验证
        # “新建功能继续可用”：经返回首页的全新停留来新建第二份。先停止拦截，
        # 让首页读取与 POST 走真实服务器。
        self.page.stop_holding()
        # 当前 hash 已是 '#/'，直接再赋同值不会触发路由；先进入已保存草稿的
        # 详情（走真实服务器），再返回首页，得到一次全新的首页停留与读取。
        self.page.eval(f"location.hash = '#/surveys/{first_id}'")
        self.page.wait_for(f"location.hash === '#/surveys/{first_id}'")
        self.page.settle(0.3)
        self.page.eval("location.hash = '#/'")
        self.page.wait_for(
            f"[...document.querySelectorAll('#survey-list a')].some(a => "
            f"a.getAttribute('href') === '#/surveys/{first_id}')")
        # 全新新建表单：填第二份并提交，走真实 POST，成功后进入详情。
        self.page.wait_for("!!document.querySelector('#draft-form')")
        self.page.t("setTitle", "第二份新问卷")
        self.page.t("addText", "第二份的题目", False)
        self.page.t("submit")
        self.page.wait_for(
            "location.hash.startsWith('#/surveys/') && "
            "document.querySelector('h2') && "
            "document.querySelector('h2').textContent.includes('第二份新问卷')")
        # 库里应有两份，且第一份未被列表失败/后续操作影响。
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200)
        ids = [s["id"] for s in data["surveys"]]
        self.assertIn(first_id, ids)
        self.assertEqual(len(ids), 2)

    # ==================================================================
    # 六、同一次首页停留：较晚读取的失败提示不能被较早成功读取覆盖
    # ==================================================================

    def test_later_http_failure_not_overridden_by_earlier_success(self):
        """刷新（较晚）已显示 HTTP 500 后，首次读取（较早）才成功：不得覆盖。"""
        seed_id = self.seed_survey("已有问卷")
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()

        # 较晚发起的刷新读取先返回 500：这是当前有效读取，显示带状态码的失败。
        self.page.fulfill_json(refresh_read, 500, {"error": "boom"})
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        self.assert_single_list_item(hint, "刷新 500 后")
        self.assert_stay_on_home_form_kept(new_id, "刷新 500 后")

        # 较早发起的首次读取随后成功返回“创建前的旧列表”：必须丢弃。
        self.page.fulfill_json(initial_read, 200,
                               {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], [hint],
                         "较早读取成功不得把 HTTP 失败提示换成旧条目")
        self.assertNotIn("已有问卷", listing["text"])
        self.assertNotIn(EMPTY_HINT, listing["text"])
        self.assert_stay_on_home_form_kept(new_id, "旧列表晚到后")

    def test_later_http_failure_not_overridden_by_earlier_empty_list(self):
        """刷新（较晚）显示 HTTP 500 后，首次读取（较早）成功为空列表：不得覆盖。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()

        self.page.fulfill_json(refresh_read, 500, {"surveys": []})
        hint = http_failure_hint(500)
        self.wait_list_item(hint)
        self.assert_single_list_item(hint, "刷新 500 后")

        # 较早读取随后成功为空列表：不能把失败提示换成“还没有问卷记录”。
        self.page.fulfill_json(initial_read, 200, {"surveys": []})
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], [hint],
                         "较早读取的空列表不得覆盖当前失败提示")
        self.assertNotIn(EMPTY_HINT, listing["text"])
        self.assert_stay_on_home_form_kept(new_id, "旧空列表晚到后")

    def test_later_unusable_body_not_overridden_by_earlier_success(self):
        """刷新（较晚）显示正文不可用提示后，首次读取（较早）成功：不得覆盖。"""
        seed_id = self.seed_survey("已有问卷")
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()

        self.page.fulfill_json(refresh_read, 200, {"surveys": {"id": new_id}})
        self.wait_list_item(BODY_UNUSABLE_HINT)
        self.assert_single_list_item(BODY_UNUSABLE_HINT, "刷新正文不可用后")

        # 较早读取随后成功返回非空旧列表：不能把“正文不可用”提示换成旧条目。
        self.page.fulfill_json(initial_read, 200,
                               {"surveys": [{"id": seed_id, "title": "已有问卷"}]})
        self.page.settle(0.8)
        listing = self.page.listing()
        self.assertEqual(listing["items"], [BODY_UNUSABLE_HINT],
                         "较早读取成功不得覆盖正文不可用提示")
        self.assertNotIn("已有问卷", listing["text"])
        self.assert_stay_on_home_form_kept(new_id, "旧列表晚到后")

    def test_later_failure_stays_even_when_earlier_read_settles_last(self):
        """刷新先失败、首次读取再网络失败后又补成功：失败提示始终稳定。"""
        initial_read, refresh_read, new_id = self.create_draft_and_stay_on_home()

        # 较晚读取 HTTP 失败先落地。
        self.page.fulfill_json(refresh_read, 503, {"error": "unavailable"})
        hint = http_failure_hint(503)
        self.wait_list_item(hint)

        # 较早读取先网络失败（本就丢弃），再不会重试；额外观察一拍确认提示稳定。
        self.page.fail_as_network_error(initial_read)
        self.page.settle(0.8)
        self.assert_single_list_item(hint, "较早读取网络失败后")
        self.page.settle(0.4)
        self.assert_single_list_item(hint, "再次观察")
        self.assert_stay_on_home_form_kept(new_id, "提示稳定后")
        # 同一次首页停留只有首次读取与创建后刷新两次读取，没有多余请求。
        self.assertEqual(self.page.list_read_count(), 2,
                         "首页停留期间应只有首次读取与创建后的刷新两次列表读取")


if __name__ == "__main__":
    unittest.main(verbosity=2)
