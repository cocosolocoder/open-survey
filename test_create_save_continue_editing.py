#!/usr/bin/env python3
"""首页新建问卷“首次创建结果未返回时继续编辑”的浏览器回归保障。

test_edit_save_continue_editing.py 保障的是**编辑已有草稿**时等待保存结果期间
继续编辑的既有行为；本文件从**首页尚未取得问卷编号的新建表单**开始，保障首次
创建转为编辑的现有行为，防止以后把它改成“创建期间锁定表单”“成功响应覆盖
后来输入”或“创建成功后又 POST 出第二份问卷”。

保护的行为（全部以真实 Chrome 中用户可见的 DOM、表单值与重新读取的接口结果
为断言依据）：

- 填写包含文本题与单选题的有效问卷，点击“保存整份问卷”后：页面明确显示正在
  保存（按钮禁用并显示“正在保存…”，状态条显示等待提示），但标题、说明、题目
  输入、必填勾选与题目、选项的增删仍然全部可用；重复点击不会追加第二笔请求。
- 首次创建只保存点击那一刻的完整内容（逐字段核对 POST 请求体）：等待期间修改
  说明与单选题选项后，创建成功读到的问卷仍与点击时一致，后改的文字、选项及
  顺序不混入这次保存。
- 成功返回时表单还有未保存变化：页面保留全部当前内容，继续留在首页表单，不跳
  详情、不清空也不恢复旧输入；表单就地切换为编辑刚创建的那份草稿——明确显示
  问卷编号，提供“取消”和“保存修改”；绿色提示“刚才提交的内容已保存”，黄色
  提示“当前还有未保存的修改”；首页列表出现刚创建的那一份记录；等待期间的
  改动不会被自动补交。
- 用户主动点击“保存修改”：完整替换刚创建的这份草稿（PUT 同一编号，请求体逐
  字段核对），编号不变，列表只增加最初创建的一条记录；第二次保存期间没有新
  变化时，成功后进入这份问卷的详情，标题、说明、题目、选项与本次点击时的
  内容一致。
- 易误判边界：等待第一次创建结果时改过内容、又恢复到点击保存时的内容，按没有
  新修改处理，成功后直接进入详情，不残留未保存提示。
- 比较规则遵循现有保存规则：问卷标题、题目标题、选项仅首尾空白不同不算新修改；
  说明的首尾空白有意义；中文、引号与内部换行按已有规则保留。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把新建表单
发出的 POST（首次创建）与随后的 PUT（保存修改）挂起，等用户在等待期间完成
编辑后再放行到真实服务器；挂起时直接读取请求体，核对“提交的就是点击那一刻
的页面内容”。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_create_save_continue_editing -v
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
# 真实 app.py 服务进程（与其它浏览器回归测试同款夹具）
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-create-")
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

# 注入到每个页面文档的测试操作手柄：所有动作都走真实的 DOM 事件与按钮。
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
    T.btn('添加文本题').click();
    const card = T.cards()[T.cards().length - 1];
    const el = card.querySelector('.q-title');
    el.value = title; fire(el);
    card.querySelector('.q-required').checked = !!required;
  };
  T.addChoice = (title, options, required) => {
    T.btn('添加单选题').click();
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
  T.submit = () => document.querySelector('#draft-form button[type=submit]').click();
})();
"""

# 当前首页表单状态快照——全部来自用户可见的 DOM 与表单值。
SNAPSHOT_JS = r"""
(() => {
  const titleEl = document.getElementById('survey-title');
  const descEl = document.getElementById('survey-desc');
  const submitEl = document.querySelector('#draft-form button[type=submit]');
  const bar = document.getElementById('save-status');
  const dirtyEl = document.getElementById('save-note-dirty');
  const headingEl = document.querySelector('#draft-form h2');
  const cancelEl = [...document.querySelectorAll('#draft-form a')]
      .find(a => a.textContent.trim() === '取消');
  return {
    hash: location.hash,
    heading: headingEl ? headingEl.textContent : null,
    cancelHref: cancelEl ? cancelEl.getAttribute('href') : null,
    title: titleEl ? titleEl.value : null,
    description: descEl ? descEl.value : null,
    questions: [...document.querySelectorAll('.q-card')].map(card => ({
      type: card.dataset.type,
      title: card.querySelector('.q-title').value,
      required: card.querySelector('.q-required').checked,
      options: [...card.querySelectorAll('.opt-text')].map(o => o.value),
    })),
    submit: submitEl
      ? {disabled: submitEl.disabled, text: submitEl.textContent.trim()}
      : null,
    savingVisible: !!document.getElementById('save-note-saving'),
    savedVisible: !!document.getElementById('save-note-saved'),
    dirtyVisible: dirtyEl ? !dirtyEl.hidden : false,
    statusHidden: bar ? bar.hidden : true,
    statusText: bar ? bar.textContent : '',
    // 等待保存结果期间只有提交按钮可以禁用；标题/说明/题目/选项输入、必填勾选、
    // 增删按钮都必须保持可用，不能把整份表单锁成只读。
    controlsEnabled: (() => {
      const fields = [
        ...document.querySelectorAll('#survey-title, #survey-desc, '
          + '#questions input, #questions textarea, #questions button'),
      ];
      return fields.every(el => !el.disabled);
    })(),
  };
})()
"""

# 详情页结构化读取：题目标题与选项都取 textContent，内部换行不能被折叠。
DETAIL_JS = r"""
(() => ({
  hash: location.hash,
  heading: document.querySelector('h2') ? document.querySelector('h2').textContent : null,
  description: (() => {
    const el = document.querySelector('.detail-desc');
    return el ? el.textContent : null;
  })(),
  questions: [...document.querySelectorAll('.q-list > li')].map(li => ({
    line: li.querySelector('.q-line').textContent,
    options: [...li.querySelectorAll('.opt-text-display')].map(o => o.textContent),
  })),
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
        self.held = []          # 被挂起的保存请求（POST/PUT，按到达顺序）
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，非保存请求（列表/详情/静态
        # 资源）必须立即放行，否则页面自己的 GET 会被挂起饿死；POST/PUT 才
        # 按顺序收集起来交给测试决定何时放行。
        self._pumping = True
        threading.Thread(target=self._event_loop, daemon=True).start()

    def _event_loop(self):
        while self._pumping:
            events = self.ws.drain_events(0.2)
            passthrough = []
            for event in events:
                if event.get("method") == "Fetch.requestPaused":
                    params = event["params"]
                    if params["request"]["method"] in ("POST", "PUT"):
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
        """让后台拦截循环、微任务与异步回调有机会落地。"""
        time.sleep(seconds)

    # ---------- 保存请求拦截 ----------

    def hold_saves(self):
        """挂起所有发往 /api/surveys 的 POST/PUT；GET 等由后台事件循环立即放行。"""
        with self._held_cond:
            self.held = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def pop_held(self, timeout=8):
        """取出（并移除）最早一个被挂起的保存请求。"""
        end = time.time() + timeout
        with self._held_cond:
            while not self.held and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held:
                return self.held.pop(0)
        raise AssertionError("保存请求未发出（POST/PUT 未被挂起）")

    def held_count(self):
        with self._held_cond:
            return len(self.held)

    def release_to_server(self, paused):
        """让挂起的请求真正到达服务器并把响应原样带回页面。"""
        self.call("Fetch.continueRequest", {"requestId": paused["requestId"]})

    @staticmethod
    def held_body(paused):
        """读取挂起请求实际携带的 JSON 请求体。"""
        raw = paused["request"].get("postData")
        if raw is None:
            raise AssertionError("挂起的保存请求没有请求体")
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

class CreateThenEditTests(unittest.TestCase):
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

    def known_ids(self):
        """当前列表接口里的全部问卷编号（各用例共用一个服务进程，按差集认新增）。"""
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, data)
        return {survey["id"] for survey in data["surveys"]}

    def created_id(self, before_ids, where):
        new_ids = self.known_ids() - before_ids
        self.assertEqual(
            len(new_ids), 1,
            f"{where}：列表应恰好新增一份记录，实际新增编号 {sorted(new_ids)}")
        return new_ids.pop()

    def open_home(self):
        self.page.open(f"{self.server.base_url}/")
        self.page.wait_for(
            "!!document.getElementById('draft-form') && "
            "!!document.getElementById('survey-title') && "
            "document.querySelector('#draft-form h2').textContent.includes('新建问卷草稿')")

    def submit_and_catch_save(self):
        self.page.t("submit")
        return self.page.pop_held()

    def assert_saving_ui(self, snapshot, where):
        """等待保存结果期间：按钮禁用并显示“正在保存…”，状态条明确提示。"""
        self.assertTrue(snapshot["savingVisible"], f"{where}：没有显示正在保存提示")
        self.assertIn("正在保存", snapshot["statusText"], f"{where}：等待提示文案不对")
        self.assertIn("仍可继续编辑", snapshot["statusText"],
                      f"{where}：等待提示没有说明等待期间仍可编辑")
        self.assertTrue(snapshot["submit"]["disabled"],
                        f"{where}：等待保存结果时保存按钮没有禁用")
        self.assertEqual(snapshot["submit"]["text"], "正在保存…",
                         f"{where}：保存按钮没有显示正在保存")
        self.assertTrue(snapshot["controlsEnabled"],
                        f"{where}：等待保存结果期间表单控件被锁定，无法继续编辑")

    def assert_saved_and_dirty(self, snapshot, where):
        self.assertTrue(snapshot["savedVisible"], f"{where}：缺少“已保存”提示")
        self.assertTrue(snapshot["dirtyVisible"], f"{where}：缺少“未保存修改”提示")
        self.assertIn("已保存", snapshot["statusText"], f"{where}：已保存提示文案不对")
        self.assertIn("未保存", snapshot["statusText"], f"{where}：未保存提示文案不对")
        self.assertIn("不会自动补交", snapshot["statusText"],
                      f"{where}：未保存提示没有说明改动不会自动补交")
        self.assertFalse(snapshot["savingVisible"], f"{where}：保存结束后仍停在正在保存")
        self.assertFalse(snapshot["submit"]["disabled"],
                         f"{where}：保存结束后按钮没有恢复可用")
        self.assertEqual(snapshot["submit"]["text"], "保存修改",
                         f"{where}：创建成功后按钮文案没有切换为“保存修改”")

    def assert_form_questions(self, snapshot, expected, where):
        actual = [(q["type"], q["title"], q["required"], tuple(q["options"]))
                  for q in snapshot["questions"]]
        wanted = [(q[0], q[1], q[2], tuple(q[3])) for q in expected]
        self.assertEqual(actual, wanted, f"{where}：表单题目/选项/必填/顺序与当前内容不符")

    def get_survey(self, survey_id):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        return data

    def assert_server_content(self, survey_id, expected, where):
        """逐字段核对服务器草稿（id 不参与比较）。

        期望题目用 (type, title, required, options) 元组给出。
        """
        data = self.get_survey(survey_id)
        self.assertEqual(data["title"], expected["title"], f"{where}：服务器标题不符")
        self.assertEqual(data["description"], expected["description"],
                         f"{where}：服务器说明不符（必须原样保留）")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], tuple(q["options"]))
             for q in data["questions"]],
            [(q[0], q[1], q[2], tuple(q[3])) for q in expected["questions"]],
            f"{where}：服务器题目/选项/必填/顺序与提交内容不符")

    def wait_detail(self, survey_id):
        self.page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        self.page.wait_for("!!document.querySelector('.q-list')")

    def wait_new_detail(self, before_ids):
        """等待首次创建成功后进入详情，返回新问卷编号。"""
        self.page.wait_for("/^#\\/surveys\\/\\d+$/.test(location.hash)")
        self.page.wait_for("!!document.querySelector('.q-list')")
        return self.created_id(before_ids, "进入详情后")

    # ---------- 主场景：等待期间继续编辑，首笔只保存快照，表单就地转为编辑 ----------

    def test_first_create_keeps_clicked_snapshot_and_adopts_edit_mode_when_dirty(self):
        page = self.page
        before_ids = self.known_ids()
        self.open_home()
        page.hold_saves()

        # 点击“保存整份问卷”时的完整内容：说明含中文、引号与换行；一道文本题
        # （必填）加一道三个选项的单选题。
        first_desc = '首存说明：含「中文引号」与“弯引号”、"直引号"\n第二行换行'
        page.t("setTitle", "首存问卷")
        page.t("setDesc", first_desc)
        page.t("addText", "首存文本题", True)
        page.t("addChoice", "首存单选题", ["选项甲", "选项乙", "选项丙"], False)
        first_expected = {
            "title": "首存问卷",
            "description": first_desc,
            "questions": [
                ("text", "首存文本题", True, []),
                ("single_choice", "首存单选题", False, ["选项甲", "选项乙", "选项丙"]),
            ],
        }

        paused = self.submit_and_catch_save()
        # 首次保存必须走创建接口，且请求体就是点击那一刻的完整内容。
        self.assertEqual(paused["request"]["method"], "POST",
                         "尚未取得编号时首次保存必须走创建接口")
        self.assertRegex(paused["request"]["url"], r"/api/surveys$")
        self.assertEqual(ChromePage.held_body(paused), {
            "title": "首存问卷",
            "description": first_desc,
            "questions": [
                {"type": "text", "title": "首存文本题", "required": True},
                {"type": "single_choice", "title": "首存单选题", "required": False,
                 "options": ["选项甲", "选项乙", "选项丙"]},
            ],
        }, "首次创建提交的不是点击那一刻的完整内容")

        # 等待结果期间：明确显示正在保存，保存按钮禁用，其余编辑全部可用。
        self.assert_saving_ui(page.snapshot(), "首次创建等待期间")

        # 等待期间修改说明与单选题选项：改文字、新增选项、删除首项（顺序改变）。
        page.t("setDesc", '等待期说明：改过的内容\n带"引号"')
        page.t("setOption", 1, 1, "选项乙改")
        page.t("addOption", 1, "选项丁")
        page.t("removeOption", 1, 0)                 # 删掉“选项甲”
        current_questions = [
            ("text", "首存文本题", True, []),
            ("single_choice", "首存单选题", False, ["选项乙改", "选项丙", "选项丁"]),
        ]
        # 等待期间的编辑立即体现在表单上，等待提示不被清掉。
        snapshot = page.snapshot()
        self.assertEqual(snapshot["description"], '等待期说明：改过的内容\n带"引号"',
                         "首次创建等待期间：说明的修改没有留在表单上")
        self.assert_form_questions(snapshot, current_questions, "首次创建等待期间编辑后")
        self.assert_saving_ui(snapshot, "完成等待期编辑后")

        # 等待期间重复点击保存不能追加第二笔请求（第一笔已在上面被取出挂起）。
        page.t("submit")
        page.settle(0.6)
        self.assertEqual(page.held_count(), 0, "等待创建结果时重复点击追加了新的保存请求")

        # 首次创建成功返回。
        page.release_to_server(paused)
        page.wait_for("!!document.getElementById('save-note-saved')")
        page.settle(0.3)

        # 首页列表出现刚创建的那一份记录（借此拿到编号）。
        survey_id = self.created_id(before_ids, "首次创建成功后")
        page.wait_for(
            "!!document.querySelector('#survey-list a[href=\"#/surveys/"
            + str(survey_id) + "\"]')")
        list_texts = page.eval(
            "[...document.querySelectorAll('#survey-list a')].map(a => a.textContent)")
        self.assertTrue(any(f"#{survey_id} 首存问卷" in text for text in list_texts),
                        f"首页列表没有显示刚创建的记录：{list_texts}")

        # 创建成功读到的问卷仍与点击时一致：后改的文字、选项及顺序不能混入。
        self.assert_server_content(survey_id, first_expected, "首次创建成功后")

        snapshot = page.snapshot()
        # 继续留在首页表单，不跳到详情。
        self.assertNotIn("/surveys/", snapshot["hash"],
                         "等待期间有新修改时，创建成功不应跳离表单进入详情")
        # 表单明确显示已创建的问卷编号，切换为编辑草稿：提供“取消”和“保存修改”。
        self.assertEqual(snapshot["heading"], f"编辑问卷草稿 #{survey_id}",
                         "创建成功后表单没有显示已创建的问卷编号")
        self.assertEqual(snapshot["cancelHref"], f"#/surveys/{survey_id}",
                         "创建成功后表单没有提供指向该问卷的“取消”")
        # 保留全部当前内容：不清空、不恢复旧输入，等待期间的修改都在。
        self.assertEqual(snapshot["title"], "首存问卷")
        self.assertEqual(snapshot["description"], '等待期说明：改过的内容\n带"引号"',
                         "创建成功后说明被旧响应覆盖")
        self.assert_form_questions(snapshot, current_questions, "首次创建成功后的页面")
        # 提示分别说明：刚才提交的内容已保存、当前还有未保存的修改。
        self.assert_saved_and_dirty(snapshot, "首次创建成功后的状态条")

        # 再隔一拍：不能自动补交等待期间的改动，服务器停留在首次创建结果。
        page.settle(0.6)
        self.assertEqual(page.held_count(), 0, "首次创建成功后自动补交了等待期间的改动")
        self.assert_server_content(survey_id, first_expected, "再次观察时的服务器")
        self.assertEqual(self.known_ids() - before_ids, {survey_id},
                         "首次创建成功后列表多出了额外记录")

    # ---------- 主动第二次保存：PUT 同一编号完整替换，无新改动则进详情 ----------

    def test_second_manual_save_replaces_created_draft_and_enters_detail(self):
        page = self.page
        before_ids = self.known_ids()
        self.open_home()
        page.hold_saves()

        # 首次创建的内容。
        page.t("setTitle", "二存流程问卷")
        page.t("setDesc", "首笔说明")
        page.t("addText", "题一", False)
        page.t("addChoice", "题二", ["A", "B"], True)
        first_expected = {
            "title": "二存流程问卷",
            "description": "首笔说明",
            "questions": [
                ("text", "题一", False, []),
                ("single_choice", "题二", True, ["A", "B"]),
            ],
        }
        first_post = self.submit_and_catch_save()
        # 等待期间先有一处改动，使首次创建成功后按既有行为留在表单并提示
        # “已保存 + 仍有未保存修改”，而不是直接进入详情。
        page.t("setTitle", "等待期临时标题")
        page.t("setOption", 1, 0, "A改")
        page.release_to_server(first_post)
        page.wait_for("!!document.getElementById('save-note-saved')")
        page.settle(0.4)

        survey_id = self.created_id(before_ids, "首次创建成功后")
        self.assert_saved_and_dirty(page.snapshot(), "首次创建成功后留在表单")
        # 成功后没有自动补交第二笔；服务器停留在首次创建结果（未被撤销）。
        self.assertEqual(page.held_count(), 0, "首次创建成功后自动补交了第二笔保存")
        self.assert_server_content(survey_id, first_expected, "第二笔保存之前")

        # 用户把当前内容改成第二笔要提交的完整内容：说明含首尾空白、中文、引号、
        # 换行，题目标题含内部换行；选项增一个；必填变化；新增题目。
        second_desc = '  第二笔说明：中文「引号」与 "直引号"\n换行第二行\n  '
        page.t("setTitle", "  第二笔标题  ")          # 首尾空白按规则裁剪
        page.t("setDesc", second_desc)                # 说明原样保存，含首尾空白
        page.t("setQTitle", 0, "题一\n内部换行")
        page.t("setRequired", 0, True)
        page.t("addOption", 1, "C")
        page.t("setRequired", 1, False)
        page.t("addText", "题三", False)

        # 用户主动点击“保存修改”：必须 PUT 刚创建的同一编号，请求体就是当前内容。
        second_put = self.submit_and_catch_save()
        self.assertEqual(second_put["request"]["method"], "PUT",
                         "创建成功后再保存必须走整份替换接口，不能再 POST 出新问卷")
        self.assertTrue(
            second_put["request"]["url"].endswith(f"/api/surveys/{survey_id}"),
            "第二次保存必须落在刚创建的同一编号上")
        self.assertEqual(ChromePage.held_body(second_put), {
            "title": "第二笔标题",
            "description": second_desc,
            "questions": [
                {"type": "text", "title": "题一\n内部换行", "required": True},
                {"type": "single_choice", "title": "题二", "required": False,
                 "options": ["A改", "B", "C"]},
                {"type": "text", "title": "题三", "required": False},
            ],
        }, "第二次保存提交的不是点击时的当前页面内容")
        self.assert_saving_ui(page.snapshot(), "第二次保存等待期间")

        # 第二次保存期间没有新变化：成功后进入这份问卷的详情。
        page.release_to_server(second_put)
        self.wait_detail(survey_id)

        # 重新读取服务器草稿：编号不变，内容与本次点击时一致。
        server = self.get_survey(survey_id)
        self.assertEqual(server["id"], survey_id, "保存后问卷编号发生了变化")
        self.assertEqual(server["title"], "第二笔标题")
        self.assertEqual(server["description"], second_desc,
                         "说明必须原样保留（首尾空白、中文、引号、换行）")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], tuple(q["options"]))
             for q in server["questions"]],
            [("text", "题一\n内部换行", True, ()),
             ("single_choice", "题二", False, ("A改", "B", "C")),
             ("text", "题三", False, ())],
            "服务器草稿与第二笔提交不一致")

        # 详情页显示与本次点击时的内容一致。
        detail = page.detail()
        self.assertEqual(detail["hash"], f"#/surveys/{survey_id}")
        self.assertEqual(detail["heading"], f"#{survey_id} 第二笔标题")
        self.assertEqual(detail["description"], second_desc,
                         "详情页没有原样显示说明")
        self.assertEqual(len(detail["questions"]), 3)
        self.assertIn("题一\n内部换行", detail["questions"][0]["line"],
                      "题目标题内部换行必须保留显示")
        self.assertIn("必填", detail["questions"][0]["line"])
        self.assertEqual(detail["questions"][1]["options"], ["A改", "B", "C"],
                         "详情页选项内容或顺序不对")
        self.assertIn("选填", detail["questions"][1]["line"])

        # 列表只增加最初创建的一条记录：第二次保存没有新增问卷。
        self.assertEqual(self.known_ids() - before_ids, {survey_id},
                         "第二次保存后列表多出了额外记录")

    # ---------- 边界：等待期间改过又全部恢复成提交内容 → 直接进详情 ----------

    def test_edits_reverted_before_create_response_go_straight_to_detail(self):
        page = self.page
        before_ids = self.known_ids()
        self.open_home()
        page.hold_saves()

        page.t("setTitle", "恢复测试问卷")
        page.t("setDesc", "恢复测试说明")
        page.t("addText", "文本题", False)
        page.t("addChoice", "单选题", ["甲", "乙"], True)
        first_expected = {
            "title": "恢复测试问卷",
            "description": "恢复测试说明",
            "questions": [
                ("text", "文本题", False, []),
                ("single_choice", "单选题", True, ["甲", "乙"]),
            ],
        }
        paused = self.submit_and_catch_save()

        # 等待期间做过一批真实编辑（标题、说明、必填、选项文字、选项增删、
        # 新增题目），但在响应返回前全部恢复成点击保存时的内容。
        page.t("setTitle", "临时标题")
        page.t("setTitle", "恢复测试问卷")
        page.t("setDesc", "临时说明")
        page.t("setDesc", "恢复测试说明")
        page.t("setRequired", 0, True)
        page.t("setRequired", 0, False)
        page.t("setOption", 1, 0, "临时选项")
        page.t("setOption", 1, 0, "甲")
        page.t("addOption", 1, "临时选项")            # 末尾新增再删除，顺序天然恢复
        page.t("removeOption", 1, 2)
        page.t("addText", "临时新增题", True)
        page.t("removeQuestion", 2)
        # 恢复后表单与点击保存时的快照逐项一致，等待提示仍在。
        snapshot = page.snapshot()
        self.assertEqual(snapshot["title"], "恢复测试问卷")
        self.assertEqual(snapshot["description"], "恢复测试说明")
        self.assert_form_questions(
            snapshot,
            [("text", "文本题", False, []),
             ("single_choice", "单选题", True, ["甲", "乙"])],
            "响应返回前恢复完成时")
        self.assert_saving_ui(snapshot, "恢复完成后仍在等待结果")

        page.release_to_server(paused)
        # 按没有新修改处理：直接进入这份问卷的详情，不残留未保存提示。
        survey_id = self.wait_new_detail(before_ids)
        self.assertFalse(
            page.eval("!!document.getElementById('save-note-dirty')"),
            "恢复成提交内容后不应残留未保存提示")
        self.assert_server_content(survey_id, first_expected, "恢复原值进入详情后")
        detail = page.detail()
        self.assertEqual(detail["heading"], f"#{survey_id} 恢复测试问卷")
        self.assertEqual(detail["description"], "恢复测试说明")
        self.assertEqual(self.known_ids() - before_ids, {survey_id},
                         "恢复场景下列表多出了额外记录")

    # ---------- 比较规则：标题/题目标题/选项的首尾空白不算新修改 ----------

    def test_surrounding_whitespace_only_is_not_a_new_change(self):
        page = self.page
        before_ids = self.known_ids()
        self.open_home()
        page.hold_saves()

        page.t("setTitle", "空白测试问卷")
        page.t("setDesc", "说明")
        page.t("addText", "文本题", False)
        page.t("addChoice", "单选题", ["甲", "乙"], False)
        paused = self.submit_and_catch_save()

        # 等待期间只给问卷标题、题目标题、选项加上首尾空白（裁剪后与提交值相同）。
        page.t("setTitle", "  空白测试问卷 \t")
        page.t("setQTitle", 0, "\t 文本题 \n ")
        page.t("setQTitle", 1, " 单选题  ")
        page.t("setOption", 1, 0, "   甲  ")

        page.release_to_server(paused)
        # 不算新修改：直接进入详情，落库为裁剪后的值。
        survey_id = self.wait_new_detail(before_ids)
        self.assert_server_content(survey_id, {
            "title": "空白测试问卷",
            "description": "说明",
            "questions": [
                ("text", "文本题", False, []),
                ("single_choice", "单选题", False, ["甲", "乙"]),
            ],
        }, "仅首尾空白不同时")

    # ---------- 比较规则：说明的首尾空白有意义 ----------

    def test_description_whitespace_change_during_first_create_is_a_real_change(self):
        page = self.page
        before_ids = self.known_ids()
        self.open_home()
        page.hold_saves()

        page.t("setTitle", "说明空白问卷")
        page.t("setDesc", "说明文字")
        page.t("addText", "文本题", False)
        paused = self.submit_and_catch_save()
        # 等待期间只改说明的首尾空白：说明原样保存，这也算真实修改。
        page.t("setDesc", "  说明文字  ")

        page.release_to_server(paused)
        page.wait_for("!!document.getElementById('save-note-saved')")
        page.settle(0.3)

        survey_id = self.created_id(before_ids, "说明加空白后")
        snapshot = page.snapshot()
        self.assertNotIn("/surveys/", snapshot["hash"],
                         "说明的空白变化也是未保存修改，不能直接进详情")
        self.assertEqual(snapshot["heading"], f"编辑问卷草稿 #{survey_id}")
        self.assert_saved_and_dirty(snapshot, "只改说明空白后")
        self.assertEqual(snapshot["description"], "  说明文字  ",
                         "页面必须原样保留说明里的空白")
        # 首次创建落库的说明仍是点击时的值，不含等待期间的空白改动。
        self.assertEqual(self.get_survey(survey_id)["description"], "说明文字",
                         "首次创建的说明必须保持点击时的原样")


if __name__ == "__main__":
    unittest.main(verbosity=2)
