#!/usr/bin/env python3
"""问卷详情“读取失败 / 正文不可用”处理的浏览器回归保障。

打开详情（无论从首页列表点链接还是直接访问 #/surveys/{id}）只读取数据：
只有读到属于当前地址编号、能够完整展示的单份问卷对象，才显示标题、说明、
题目与“编辑草稿”入口。本文件用真实 Chrome 驱动真实页面，保护：

- 等待响应期间停在“加载中…”，但不会先渲染半份内容。
- 只有真实收到 404 才显示“问卷不存在”；404 响应即使夹带完整问卷正文也一样。
- 其它非成功状态（500/502/503，正文为错误 JSON、普通文字、空内容或碰巧是
  合法问卷）沿用现有“详情加载失败（HTTP 状态码）”提示。
- 网络中断沿用现有“详情加载失败：…”提示，不编造状态码，也不说成不存在。
- 200 但正文无法解析为 JSON，或能解析却不是可完整展示的问卷（顶层非对象、
  编号缺失/不一致、标题或说明不是字符串、题目不是数组、题目结构不完整、题型
  不受支持、required 非布尔、选项不是字符串数组、单选题少于两个选项、文本题
  带选项、以及一半题目正常一半异常）时，整份按加载失败处理：留在当前详情
  地址，明确提示“详情加载失败，返回内容无法用于展示”，保留返回首页入口，
  不显示任何残缺说明、题目与编辑链接，也不能把不支持的题型标成文本题。
- 合法内容照常展示：标题、说明、题目次序、题型、必填/选填、全部选项按原稿；
  说明空字符串显示“（无说明）”；题目空数组的合法旧记录显示零道题与编辑入口。
- 等待返回期间用户已离开详情页（回首页）时，晚到的成功或失败结果一律忽略，
  不跳回详情、不在首页插入失败提示。

时序全部在网络层制造：通过 Chrome DevTools Protocol 的 Fetch 域挂起
GET /api/surveys/{id}，由测试决定每次返回的状态码与正文（fulfillRequest
伪造任意正文、failRequest 模拟网络中断，或放行到真实服务器得到真实 404）。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_detail_load_failure -v
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

BODY_UNUSABLE = "详情加载失败，返回内容无法用于展示。"
DETAIL_GET_RE = re.compile(r"^/api/surveys/\d+$")


# --------------------------------------------------------------------------
# 真实 app.py 服务进程
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-detail-")
        self.process = None
        self.base_url = None

    def start(self):
        # 自行选定空闲端口并把子进程访问日志直接丢弃，避免 PIPE 写满后阻塞。
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
# 极简 WebSocket 客户端（仅用于连接 CDP）
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
        threading.Thread(target=self._read_loop, daemon=True).start()

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
# 真实 Chrome 标签页（扁平化 CDP 会话）
# --------------------------------------------------------------------------

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
        self.held = []
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        # 拦截开启期间只有详情 GET（/api/surveys/{id}）被挂起；首页列表 GET、
        # 静态资源及任何 POST/PUT 立即放行，避免切到首页时其请求被饿死。
        self._pumping = True
        threading.Thread(target=self._event_loop, daemon=True).start()

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
                    path = urlsplit(request["url"]).path
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

    def open(self, url):
        queued = self.ws.drain_events(0)
        if queued:
            self.ws.push_events(queued)
        self.call("Page.navigate", {"url": url})
        end = time.time() + 10
        while time.time() < end:
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
            time.sleep(0.12)
        raise AssertionError(f"等待页面条件超时：{expression}")

    def settle(self, seconds=0.6):
        time.sleep(seconds)

    def hold_gets(self):
        with self._held_cond:
            self.held = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def wait_held_get(self, timeout=8):
        end = time.time() + timeout
        with self._held_cond:
            while not self.held and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held:
                return self.held.pop(0)
        raise AssertionError("详情读取请求未发出（GET 未被挂起）")

    def release_to_server(self, paused):
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

    def fulfill_text(self, paused, status, text,
                     content_type="text/plain; charset=utf-8"):
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

    def stop_holding(self):
        try:
            self.call("Fetch.disable", timeout=5)
        except Exception:
            pass


# 详情页快照：加载/失败态与成功态共用，全部来自用户可见的 DOM。
DETAIL_JS = r"""
(() => {
  const statusEl = document.getElementById('detail-status');
  const heading = document.querySelector('h2');
  const descEl = document.querySelector('.detail-desc');
  const noDesc = [...document.querySelectorAll('p.muted')]
      .some(p => p.textContent === '（无说明）');
  return {
    hash: location.hash,
    statusText: statusEl ? statusEl.textContent : null,
    heading: heading ? heading.textContent : null,
    editHrefs: [...document.querySelectorAll('a[href]')]
        .map(a => a.getAttribute('href')).filter(h => h.indexOf('/edit') !== -1),
    editTexts: [...document.querySelectorAll('a[href]')]
        .filter(a => a.getAttribute('href').indexOf('/edit') !== -1)
        .map(a => a.textContent),
    links: [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href')),
    hasQList: !!document.querySelector('.q-list'),
    questionCountText: [...document.querySelectorAll('h3')]
        .map(x => x.textContent).find(t => t.indexOf('题目（共') === 0) || null,
    noDescription: noDesc,
    description: descEl ? descEl.textContent : null,
    questions: [...document.querySelectorAll('.q-list > li')].map(li => {
      const line = li.querySelector('.q-line');
      return {
        lead: line.childNodes[0].textContent.trim(),
        tags: [...line.querySelectorAll('.tag')].map(t => t.textContent),
        options: [...li.querySelectorAll('.opt-text-display')].map(o => o.textContent),
      };
    }),
    bodyText: document.body.innerText,
  };
})()
"""

HOME_JS = r"""
(() => ({
  hash: location.hash,
  hasCreateForm: !!document.querySelector('#draft-form'),
  formHeading: (document.querySelector('#draft-form h2') || {}).textContent || null,
  bodyText: document.body.innerText,
}))()
"""


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

class DetailLoadFailureTests(unittest.TestCase):
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
        cls.server.stop()

    def setUp(self):
        # 每例独立 Chrome，避免挂起请求跨用例累积相互干扰。
        self.browser = ChromeBrowser()
        self.page = self.browser.new_page()

    def tearDown(self):
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

    def seed_survey(self, payload=None):
        payload = payload or {
            "title": "已有问卷",
            "description": "已有说明",
            "questions": [
                {"type": "text", "title": "文本题甲", "required": True},
                {"type": "single_choice", "title": "单选题乙", "required": False,
                 "options": ["选项一", "选项二", "选项三"]},
            ],
        }
        status, data = self.api("POST", "/api/surveys", payload)
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data["id"], data

    def open_detail_direct_held(self, survey_id):
        """直接访问详情地址并停在加载态，返回被挂起的详情 GET。"""
        self.page.hold_gets()
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}")
        self.wait_loading(survey_id)
        return self.page.wait_held_get()

    def open_detail_from_home_held(self, survey_id):
        """从首页列表点真实链接进入详情并停在加载态，返回被挂起的详情 GET。"""
        self.page.hold_gets()
        self.page.open(f"{self.server.base_url}/#/")
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_id}\"]')")
        self.page.eval(
            f"[...document.querySelectorAll('a[href]')].find(a => "
            f"a.getAttribute('href') === '#/surveys/{survey_id}').click()")
        self.wait_loading(survey_id)
        return self.page.wait_held_get()

    def wait_loading(self, survey_id):
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}' && "
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('加载中') !== -1")

    def detail(self):
        return self.page.eval(DETAIL_JS)

    def trigger_detail_reread(self):
        """停留同址重新派发 hashchange，产生新一代详情读取（被挂起）。"""
        before = len(self.page.held)
        self.page.eval("window.dispatchEvent(new Event('hashchange'))")
        end = time.time() + 8
        while time.time() < end:
            if len(self.page.held) >= before + 1:
                return self.page.held[before]
            time.sleep(0.1)
        raise AssertionError("重新路由后新的详情读取未发出")

    def assert_loading_only(self, state, survey_id, where):
        """等待期间：加载提示 + 返回首页，先不渲染任何标题、说明、题目、编辑入口。"""
        self.assertEqual(state["hash"], f"#/surveys/{survey_id}",
                         f"{where}：加载等待期离开了详情地址")
        self.assertIn("加载中", state["statusText"] or "",
                      f"{where}：等待时必须显示加载中")
        self.assertIsNone(state["heading"], f"{where}：等待时就渲染了标题")
        self.assertFalse(state["hasQList"], f"{where}：等待时就渲染了题目")
        self.assertEqual(state["editHrefs"], [], f"{where}：等待时出现了编辑入口")
        self.assertIn("#/", state["links"], f"{where}：等待时也应保留返回首页入口")

    def assert_failure_shell(self, state, survey_id, where):
        """失败态公共要求：留在详情地址，只有返回首页，无任何残缺内容与编辑链接。"""
        self.assertEqual(state["hash"], f"#/surveys/{survey_id}",
                         f"{where}：失败后不应跳转地址")
        self.assertIsNone(state["heading"], f"{where}：失败不能渲染问卷标题")
        self.assertFalse(state["hasQList"], f"{where}：失败不能渲染任何题目")
        self.assertEqual(state["editHrefs"], [],
                         f"{where}：失败不能保留编辑入口：{state['editHrefs']}")
        self.assertEqual(state["editTexts"], [], f"{where}：失败不能出现编辑链接文字")
        self.assertIn("#/", state["links"], f"{where}：失败必须保留返回首页入口")
        self.assertNotIn("加载中", state["bodyText"], f"{where}：不能一直停在加载中")
        self.assertNotIn("（无说明）", state["bodyText"],
                         f"{where}：失败不能渲染残缺说明")
        self.assertNotIn("题目（共", state["bodyText"] or "",
                         f"{where}：失败不能渲染残缺题数")

    def assert_body_unusable(self, survey_id, where):
        state = self.detail()
        self.assertEqual(state["statusText"], BODY_UNUSABLE,
                         f"{where}：失败提示不对：{state['statusText']!r}")
        self.assert_failure_shell(state, survey_id, where)
        self.assertNotIn("不存在", state["bodyText"],
                         f"{where}：正文不可用不能说成问卷不存在")
        self.assertNotIn("HTTP", state["bodyText"],
                         f"{where}：200 正文不可用不应编造状态码")
        return state

    def assert_http_failure(self, survey_id, code, where):
        state = self.detail()
        self.assertEqual(
            state["statusText"],
            f"详情加载失败（HTTP {code}），请稍后重试。",
            f"{where}：HTTP 失败提示不对：{state['statusText']!r}")
        self.assert_failure_shell(state, survey_id, where)
        self.assertNotIn("不存在", state["bodyText"],
                         f"{where}：{code} 不能说成问卷不存在")
        return state

    def assert_not_found(self, survey_id, where):
        state = self.detail()
        self.assertEqual(state["statusText"], f"问卷 #{survey_id} 不存在。",
                         f"{where}：404 提示不对：{state['statusText']!r}")
        self.assert_failure_shell(state, survey_id, where)
        return state

    def valid_survey_body(self, survey_id, **over):
        body = {
            "id": survey_id,
            "title": "正常问卷",
            "description": "正常说明",
            "questions": [
                {"type": "text", "title": "文本题甲", "required": True, "options": []},
                {"type": "single_choice", "title": "单选题乙", "required": False,
                 "options": ["选项一", "选项二"]},
            ],
        }
        body.update(over)
        return body

    # ---------- 加载中：只显示加载提示与返回首页 ----------

    def test_loading_state_before_response_renders_nothing_else(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self.assert_loading_only(self.detail(), survey_id, "直接访问等待返回时")
        # 再等一拍，确认挂起期间不会提前渲染半份内容。
        self.page.settle(0.5)
        self.assert_loading_only(self.detail(), survey_id, "挂起一拍后")
        self.page.release_to_server(paused)
        self.page.wait_for("!!document.querySelector('.q-list')")

    # ---------- 404：只有真实 404 才显示不存在 ----------

    def test_real_server_404_shows_not_found(self):
        existing = {s["id"] for s in self.api("GET", "/api/surveys")[1]["surveys"]}
        unknown_id = max(existing, default=0) + 999_999
        paused = self.open_detail_direct_held(unknown_id)
        self.page.release_to_server(paused)   # 真实服务器给出真实 404
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('不存在') !== -1")
        self.assert_not_found(unknown_id, "真实 404 后")

    def test_404_even_with_full_survey_body_still_says_not_found(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        # 404 响应碰巧夹带了这份编号的完整正文：仍只能按不存在处理。
        self.page.fulfill_json(paused, 404, self.valid_survey_body(survey_id))
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('不存在') !== -1")
        state = self.assert_not_found(survey_id, "404 夹带完整正文后")
        self.assertNotIn("正常问卷", state["bodyText"], "404 夹带的正文绝不能展示")
        self.assertNotIn("文本题甲", state["bodyText"])

    # ---------- 其它非成功状态 ----------

    def test_http_500_variants_show_code_without_partial_content(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)

        cases = [
            ("错误 JSON 对象", 500, "json", {"error": "database is locked"}),
            ("普通文字", 503, "text", "服务暂不可用"),
            ("空正文", 500, "text", ""),
        ]
        for i, (label, code, kind, payload) in enumerate(cases):
            with self.subTest(label):
                if i > 0:
                    paused = self.trigger_detail_reread()
                if kind == "json":
                    self.page.fulfill_json(paused, code, payload)
                else:
                    self.page.fulfill_text(paused, code, payload)
                self.page.wait_for(
                    "((document.getElementById('detail-status')||{}).textContent || '')"
                    f".indexOf('HTTP {code}') !== -1")
                self.assert_http_failure(survey_id, code, f"{label}后")

    def test_http_502_with_valid_survey_body_must_not_render(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self.page.fulfill_json(
            paused, 502, {"error": "bad gateway", **self.valid_survey_body(survey_id)})
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('HTTP 502') !== -1")
        state = self.assert_http_failure(survey_id, 502, "502 夹带完整问卷后")
        self.assertNotIn("正常问卷", state["bodyText"], "失败响应夹带的问卷不能展示")
        self.assertNotIn("文本题甲", state["bodyText"])

    # ---------- 网络中断 ----------

    def test_network_failure_keeps_plain_message(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self.page.fail_as_network_error(paused)
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('详情加载失败：') !== -1")
        state = self.detail()
        self.assertTrue((state["statusText"] or "").startswith("详情加载失败："),
                        f"网络失败提示不对：{state['statusText']!r}")
        self.assert_failure_shell(state, survey_id, "网络失败后")
        self.assertNotIn("HTTP", state["bodyText"], "网络失败不能编造状态码")
        self.assertNotIn("不存在", state["bodyText"], "网络失败不能说成不存在")

    # ---------- 200 但正文无法解析 ----------

    def test_200_invalid_json_is_body_failure(self):
        survey_id, _ = self.seed_survey()
        for i, raw in enumerate(["这不是JSON{,,", "", "{"]):
            with self.subTest(raw=raw):
                if i == 0:
                    paused = self.open_detail_direct_held(survey_id)
                else:
                    paused = self.trigger_detail_reread()
                self.page.fulfill_text(
                    paused, 200, raw, content_type="application/json; charset=utf-8")
                self.page.wait_for(
                    "((document.getElementById('detail-status')||{}).textContent || '')"
                    ".indexOf('无法用于展示') !== -1")
                self.assert_body_unusable(survey_id, f"200 非法 JSON {raw!r} 后")

    # ---------- 200 但结构不可用：整份失败，不跳过、不补默认、不标错题型 ----------

    def test_200_top_level_not_object_is_body_failure(self):
        survey_id, _ = self.seed_survey()
        cases = [
            ("null", "text", "null"),
            ("数组", "json", []),
            ("数字", "text", "42"),
            ("字符串", "text", '"一串文字"'),
        ]
        paused = self.open_detail_direct_held(survey_id)
        for i, (label, kind, payload) in enumerate(cases):
            with self.subTest(label):
                if i > 0:
                    paused = self.trigger_detail_reread()
                if kind == "json":
                    self.page.fulfill_json(paused, 200, payload)
                else:
                    self.page.fulfill_text(
                        paused, 200, payload,
                        content_type="application/json; charset=utf-8")
                self.page.wait_for(
                    "((document.getElementById('detail-status')||{}).textContent || '')"
                    ".indexOf('无法用于展示') !== -1")
                self.assert_body_unusable(survey_id, f"顶层为{label}后")

    def test_200_wrong_or_missing_id_is_body_failure(self):
        survey_id, _ = self.seed_survey()
        other = survey_id + 1000
        cases = [
            ("编号属于别的问卷", self.valid_survey_body(other)),
            ("缺少编号", {k: v for k, v in self.valid_survey_body(survey_id).items()
                          if k != "id"}),
            ("编号为字符串", self.valid_survey_body(survey_id, id=str(survey_id))),
        ]
        paused = self.open_detail_direct_held(survey_id)
        for i, (label, body) in enumerate(cases):
            with self.subTest(label):
                if i > 0:
                    paused = self.trigger_detail_reread()
                self.page.fulfill_json(paused, 200, body)
                self.page.wait_for(
                    "((document.getElementById('detail-status')||{}).textContent || '')"
                    ".indexOf('无法用于展示') !== -1")
                state = self.assert_body_unusable(survey_id, f"{label}后")
                self.assertNotIn("正常问卷", state["bodyText"],
                                 f"{label}：不能展示别的编号/无编号问卷的内容")

    def test_200_bad_survey_fields_is_body_failure(self):
        survey_id, _ = self.seed_survey()
        good = self.valid_survey_body(survey_id)
        cases = [
            ("缺标题", {k: v for k, v in good.items() if k != "title"}),
            ("标题非字符串", self.valid_survey_body(survey_id, title=123)),
            ("标题为 null", self.valid_survey_body(survey_id, title=None)),
            ("说明非字符串", self.valid_survey_body(survey_id, description=False)),
            ("缺题目数组", {k: v for k, v in good.items() if k != "questions"}),
            ("题目不是数组", self.valid_survey_body(survey_id, questions={"0": 1})),
            ("题目为 null", self.valid_survey_body(survey_id, questions=None)),
            ("题目不是对象", self.valid_survey_body(survey_id, questions=["题目"])),
            ("题目缺标题", self.valid_survey_body(
                survey_id, questions=[{"type": "text", "required": True, "options": []}])),
            ("题目标题非字符串", self.valid_survey_body(
                survey_id, questions=[{"type": "text", "title": 7,
                                       "required": True, "options": []}])),
            ("题型不受支持", self.valid_survey_body(
                survey_id, questions=[{"type": "rating", "title": "评分题",
                                       "required": False, "options": []}])),
            ("required 为数字", self.valid_survey_body(
                survey_id, questions=[{"type": "text", "title": "必填误用数字",
                                       "required": 1, "options": []}])),
            ("required 为字符串", self.valid_survey_body(
                survey_id, questions=[{"type": "text", "title": "必填误用字符串",
                                       "required": "true", "options": []}])),
            ("options 不是数组", self.valid_survey_body(
                survey_id, questions=[{"type": "single_choice", "title": "选项畸形",
                                       "required": False, "options": "甲乙"}])),
            ("选项不是字符串", self.valid_survey_body(
                survey_id, questions=[{"type": "single_choice", "title": "选项畸形",
                                       "required": False, "options": ["甲", 2]}])),
            ("单选题只有一个选项", self.valid_survey_body(
                survey_id, questions=[{"type": "single_choice", "title": "只有一项",
                                       "required": False, "options": ["仅一项"]}])),
            ("文本题带有选项", self.valid_survey_body(
                survey_id, questions=[{"type": "text", "title": "文本题带选项",
                                       "required": False, "options": ["多余选项"]}])),
        ]
        paused = self.open_detail_direct_held(survey_id)
        for i, (label, body) in enumerate(cases):
            with self.subTest(label):
                if i > 0:
                    paused = self.trigger_detail_reread()
                self.page.fulfill_json(paused, 200, body)
                self.page.wait_for(
                    "((document.getElementById('detail-status')||{}).textContent || '')"
                    ".indexOf('无法用于展示') !== -1")
                state = self.assert_body_unusable(survey_id, f"{label}后")
                # 题型不受支持时绝不能被标成文本题；任何畸形题目都不能上屏。
                self.assertNotIn("评分题", state["bodyText"],
                                 f"{label}：畸形题目不能上屏")
                self.assertFalse(
                    state["questions"], f"{label}：不能渲染任何题目：{state['questions']}")

    def test_200_half_valid_questions_is_entire_failure(self):
        """前一道题正常、后一道题畸形：整份失败，不能只渲染前半份。"""
        survey_id, _ = self.seed_survey()
        body = self.valid_survey_body(survey_id, questions=[
            {"type": "text", "title": "第一题完全正常", "required": True, "options": []},
            {"type": "weird", "title": "第二题题型不受支持",
             "required": False, "options": []},
        ])
        paused = self.open_detail_direct_held(survey_id)
        self.page.fulfill_json(paused, 200, body)
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('无法用于展示') !== -1")
        state = self.assert_body_unusable(survey_id, "一半题目正常一半畸形后")
        self.assertNotIn("第一题完全正常", state["bodyText"],
                         "不能只展示正常的那一半题目")
        self.assertNotIn("第二题题型不受支持", state["bodyText"])

    def test_200_unsupported_type_must_not_be_tagged_text(self):
        """不支持题型不能借用“其它题型=文本题”的分支被标成文本题并展示。"""
        survey_id, _ = self.seed_survey()
        body = self.valid_survey_body(survey_id, questions=[
            {"type": "single_choice", "title": "正常单选题",
             "required": False, "options": ["甲", "乙"]},
            {"type": "matrix", "title": "矩阵题不受支持",
             "required": True, "options": []},
        ])
        paused = self.open_detail_direct_held(survey_id)
        self.page.fulfill_json(paused, 200, body)
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('无法用于展示') !== -1")
        state = self.assert_body_unusable(survey_id, "出现不支持题型后")
        self.assertNotIn("矩阵题", state["bodyText"])
        self.assertNotIn("正常单选题", state["bodyText"], "整份失败，正常题也不能显示")

    # ---------- 合法正文：正常问卷完整展示 ----------

    def test_valid_survey_from_real_server_renders_exactly(self):
        payload = {
            "title": "  满意度问卷\n第二行  ",
            "description": "  说明第一行\n中文与\"引号\"、弯引号“”\n第三行  ",
            "questions": [
                {"type": "text", "title": " 必填文本题\n标题第二行 ", "required": True},
                {"type": "single_choice", "title": "选填单选题", "required": False,
                 "options": ["选项甲\n第二行", "选项乙", " 选项丙 "]},
                {"type": "single_choice", "title": "必填单选题", "required": True,
                 "options": ["一", "二", "三", "四"]},
            ],
        }
        survey_id, saved = self.seed_survey(payload)
        paused = self.open_detail_direct_held(survey_id)
        self.page.release_to_server(paused)
        self.page.wait_for("!!document.querySelector('.q-list')")
        state = self.detail()

        self.assertEqual(state["hash"], f"#/surveys/{survey_id}")
        self.assertIsNone(state["statusText"])
        self.assertEqual(state["heading"], f"#{survey_id} {saved['title']}",
                         "标题必须按已保存内容（含内部换行、首尾裁剪后）显示")
        self.assertEqual(state["description"], saved["description"],
                         "说明必须原样显示（中文、引号、换行、首尾空白）")
        self.assertEqual(state["questionCountText"], "题目（共 3 道）")
        self.assertEqual(len(state["questions"]), 3)
        self.assertEqual(state["questions"][0]["lead"], "1. " + saved["questions"][0]["title"])
        self.assertEqual(state["questions"][0]["tags"], ["文本题", "必填"])
        self.assertEqual(state["questions"][0]["options"], [])
        self.assertEqual(state["questions"][1]["lead"], "2. 选填单选题")
        self.assertEqual(state["questions"][1]["tags"], ["单选题", "选填"])
        self.assertEqual(state["questions"][1]["options"],
                         saved["questions"][1]["options"])
        self.assertEqual(state["questions"][2]["tags"], ["单选题", "必填"])
        self.assertEqual(state["questions"][2]["options"], ["一", "二", "三", "四"])
        # 编辑入口指向当前编号。
        self.assertEqual(state["editHrefs"], [f"#/surveys/{survey_id}/edit"])

    def test_empty_description_shows_none_hint(self):
        survey_id, saved = self.seed_survey({
            "title": "无说明问卷",
            "description": "",
            "questions": [{"type": "text", "title": "唯一题目", "required": False}],
        })
        paused = self.open_detail_direct_held(survey_id)
        self.page.release_to_server(paused)
        self.page.wait_for("!!document.querySelector('.q-list')")
        state = self.detail()
        self.assertTrue(state["noDescription"], "说明空字符串应显示（无说明）")
        self.assertIsNone(state["description"])
        self.assertEqual(state["questions"][0]["tags"], ["文本题", "选填"])

    def test_empty_questions_legacy_record_shows_zero_and_edit_entry(self):
        """题目为空数组的合法旧记录：零道题 + 进入编辑补齐入口，不算加载失败。"""
        # 该形态无法通过保存接口构造（保存要求至少一题），用拦截伪造同形态正文。
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self.page.fulfill_json(paused, 200, {
            "id": survey_id,
            "title": "只有标题的旧记录",
            "description": "",
            "questions": [],
        })
        self.page.wait_for(
            f"document.querySelector('h2') && "
            f"document.querySelector('h2').textContent === "
            f"'#{survey_id} 只有标题的旧记录'")
        state = self.detail()
        self.assertIsNone(state["statusText"], "合法空题目不应有失败/加载提示")
        self.assertEqual(state["questionCountText"], "题目（共 0 道）")
        self.assertFalse(state["hasQList"])
        self.assertTrue(state["noDescription"], "说明空字符串仍显示（无说明）")
        self.assertIn("该问卷草稿还没有题目，可进入编辑补齐。", state["bodyText"])
        # 头部“编辑草稿”与补齐题目的 CTA 两个入口都指向当前编号。
        self.assertEqual(set(state["editHrefs"]), {f"#/surveys/{survey_id}/edit"})
        self.assertIn("编辑草稿", state["editTexts"])
        self.assertIn("编辑草稿并添加题目", state["editTexts"])

    # ---------- 从首页列表打开同样适用 ----------

    def test_bad_body_when_opened_from_home_list_link(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_from_home_held(survey_id)
        self.page.fulfill_json(paused, 200, {"id": survey_id, "title": "缺说明与题目"})
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('无法用于展示') !== -1")
        self.assert_body_unusable(survey_id, "从首页列表打开且正文畸形后")

    def test_valid_when_opened_from_home_list_link(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_from_home_held(survey_id)
        self.page.release_to_server(paused)
        self.page.wait_for("!!document.querySelector('.q-list')")
        state = self.detail()
        self.assertEqual(state["heading"], f"#{survey_id} 已有问卷")
        self.assertEqual(len(state["questions"]), 2)
        self.assertEqual(state["editHrefs"], [f"#/surveys/{survey_id}/edit"])

    # ---------- 失败页的返回首页入口可用 ----------

    def test_back_home_link_works_after_body_failure(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self.page.fulfill_text(paused, 200, "not json",
                               content_type="application/json; charset=utf-8")
        self.page.wait_for(
            "((document.getElementById('detail-status')||{}).textContent || '')"
            ".indexOf('无法用于展示') !== -1")
        self.page.eval(
            "[...document.querySelectorAll('a[href]')].find(a => "
            "a.getAttribute('href') === '#/').click()")
        self.page.wait_for("location.hash === '#/' && !!document.querySelector('#draft-form')")
        home = self.page.eval(HOME_JS)
        self.assertEqual(home["hash"], "#/")
        self.assertTrue(home["hasCreateForm"])
        self.assertNotIn("无法用于展示", home["bodyText"],
                         "返回首页后不能残留详情失败提示")

    # ---------- 晚到结果：用户已离开详情则一律忽略 ----------

    def _leave_to_home_while_loading(self):
        self.page.eval("location.hash = '#/'")
        self.page.wait_for(
            "location.hash === '#/' && !!document.querySelector('#draft-form')")

    def _assert_home_untouched(self, where):
        self.page.settle(0.8)
        home = self.page.eval(HOME_JS)
        self.assertEqual(home["hash"], "#/", f"{where}：晚到结果把用户带回了详情")
        self.assertTrue(home["hasCreateForm"], f"{where}：首页新建表单被覆盖")
        self.assertNotIn("详情加载失败", home["bodyText"],
                         f"{where}：首页冒出了旧详情的失败提示")
        self.assertNotIn("无法用于展示", home["bodyText"])
        self.assertNotIn("问卷 #", home["bodyText"])
        self.assertIsNone(
            self.page.eval("document.querySelector('.q-list') ? 1 : null"),
            f"{where}：首页冒出了详情题目列表")

    def test_late_success_after_leaving_detail_is_ignored(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self._leave_to_home_while_loading()
        self.page.fulfill_json(paused, 200, self.valid_survey_body(survey_id))
        self._assert_home_untouched("晚到成功结果后")
        self.page.settle(0.4)
        self.assertEqual(self.page.eval("location.hash"), "#/",
                         "再观察一拍后仍不能被晚到结果跳走")

    def test_late_body_failure_after_leaving_detail_is_ignored(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self._leave_to_home_while_loading()
        self.page.fulfill_json(paused, 200, {"id": survey_id, "title": "残缺"})
        self._assert_home_untouched("晚到正文失败后")

    def test_late_http_failure_after_leaving_detail_is_ignored(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self._leave_to_home_while_loading()
        self.page.fulfill_json(paused, 500, {"error": "boom"})
        self._assert_home_untouched("晚到 HTTP 失败后")

    def test_late_not_found_after_leaving_detail_is_ignored(self):
        survey_id, _ = self.seed_survey()
        paused = self.open_detail_direct_held(survey_id)
        self._leave_to_home_while_loading()
        self.page.fulfill_json(paused, 404, {"error": "not found"})
        self._assert_home_untouched("晚到 404 后")


if __name__ == "__main__":
    unittest.main(verbosity=2)
