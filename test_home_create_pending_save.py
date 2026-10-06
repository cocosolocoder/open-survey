#!/usr/bin/env python3
"""首页新建问卷“首次创建等待期间继续编辑”的浏览器回归保障。

test_edit_save_continue_editing.py 保障的是**已有草稿编辑页**等待保存期间继续编辑
的既有行为；本文件从**首页尚未取得问卷编号的新建表单**开始，保障“第一次创建
尚未返回结果时用户继续编辑，创建成功后仍能把后续内容保存到同一份问卷”的现有
行为，防止以后把它改成“创建期间锁定表单”“成功后丢弃/还原等待期间的输入”或
“再次保存又 POST 出第二份问卷”。

保护的行为（全部以真实 Chrome 中用户可见的 DOM、表单值与接口结果为断言依据）：

- 点击“保存整份问卷”后页面明确显示正在保存（按钮禁用并显示“正在保存…”，状态条
  显示等待提示），但标题、说明、题目输入、必填勾选与题目/选项的增删仍然可用；
  等待期间重复点击保存不会追加第二笔创建请求。
- 第一次创建只保存点击那一刻的完整内容：挂起的 POST 请求体与服务器落库内容都
  只含点击时的快照，等待期间改过的说明、选项与顺序不混入（中文、引号、内部换行
  按既有规则原样保留，标题/题目标题/选项按首尾空白裁剪后的值保存）。
- 成功返回时表单还有未保存修改：页面保留全部当前内容、留在首页表单（不跳详情、
  不清空、不恢复旧输入），表单就地切换为“编辑问卷草稿 #编号”，出现“取消”链接、
  按钮变为“保存修改”；状态条分别提示“刚才提交的内容已保存”（绿）与“当前还有
  未保存的修改”（黄）；首页列表出现刚创建的那一份记录；等待期间的改动不会被
  自动补交。
- 用户主动点击“保存修改”：PUT 完整替换同一份草稿（编号不变，列表仍只有最初
  创建的那一条记录），请求体逐字段等于第二次点击时的页面内容；第二次保存期间
  没有新变化时，成功后进入这份问卷的详情，详情与本次提交一致。
- 易误判边界：等待期间改过、但在创建成功返回前恢复成点击时内容的，按没有新
  修改处理，直接进入详情，不残留未保存提示。
- 比较规则：问卷标题、题目标题、选项仅首尾空白不同不算新修改；说明的首尾空白
  有意义（改了就算未保存修改）。

测试在网络层制造时序：通过 Chrome DevTools Protocol 的 Fetch 域把新建表单发出的
POST（以及转为编辑后的 PUT）挂起，等等待期间的编辑完成后再放行到真实服务器；
其余请求（列表、详情）由后台事件循环立即放行。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_home_create_pending_save -v
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
      if (i < options.length) {
        const el = row.querySelector('.opt-text');
        el.value = options[i]; fire(el);
      } else {
        row.remove();
      }
    });
  };
  T.submit = () => document.querySelector('#draft-form button[type=submit]').click();
})();
"""

# 当前首页/表单状态快照——全部来自用户可见的 DOM 与表单值。
SNAPSHOT_JS = r"""
(() => {
  const titleEl = document.getElementById('survey-title');
  const descEl = document.getElementById('survey-desc');
  const submitEl = document.querySelector('#draft-form button[type=submit]');
  const bar = document.getElementById('save-status');
  const dirtyEl = document.getElementById('save-note-dirty');
  const heading = document.querySelector('#draft-form h2');
  const cancel = [...document.querySelectorAll('#draft-form a')]
      .find(a => a.textContent.trim() === '取消');
  return {
    hash: location.hash,
    formHeading: heading ? heading.textContent : null,
    cancelHref: cancel ? cancel.getAttribute('href') : null,
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
    listLinks: [...document.querySelectorAll('#survey-list a')]
      .map(a => ({href: a.getAttribute('href'), text: a.textContent})),
    // 等待保存结果期间只有提交按钮可以禁用；标题/说明/题目/选项输入、必填勾选、
    // 增删按钮都必须保持可用，不能把整份表单锁成只读。
    controlsEnabled: (() => {
      const fields = [
        ...document.querySelectorAll('#survey-title, #survey-desc, '
          + '#questions input, #questions textarea, #questions button, '
          + '#draft-form .row-actions button:not([type=submit])'),
      ];
      return fields.length > 0 && fields.every(el => !el.disabled);
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
        self.held = []          # 被挂起的 POST/PUT 请求（按到达顺序）
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，非保存请求（列表/详情等 GET）必须
        # 立即放行，否则页面自己的读取会被挂起饿死；POST 与 PUT 才按顺序收集起来
        # 交给测试决定何时放行。
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

# 点击“保存整份问卷”那一刻填写的内容：标题含中文引号，说明含中文、引号与内部
# 换行（说明从不裁剪、原样保存），用来核对保存与比较规则不破坏这些字符。
CLICK_TITLE = "年度「满意度」调查"
CLICK_DESC = "说明第一行\n第二行：“引号”与中文"


class HomeCreatePendingSaveTests(unittest.TestCase):
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

    def existing_ids(self):
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, data)
        return {row["id"] for row in data["surveys"]}

    def open_home(self):
        self.page.open(f"{self.server.base_url}/#/")
        self.page.wait_for(
            "!!document.querySelector('#draft-form') && "
            "document.querySelector('#draft-form h2').textContent === '新建问卷草稿'")

    def fill_create_form(self):
        """填写一份含文本题与单选题的有效问卷（点击保存那一刻的内容）。"""
        page = self.page
        page.t("setTitle", CLICK_TITLE)
        page.t("setDesc", CLICK_DESC)
        page.t("addText", "开放建议", True)
        page.t("addChoice", "整体评分", ["好", "一般", "差"], False)

    def click_payload(self):
        """点击保存那一刻应提交的请求体（文本题不带 options）。"""
        return {
            "title": CLICK_TITLE,
            "description": CLICK_DESC,
            "questions": [
                {"type": "text", "title": "开放建议", "required": True},
                {"type": "single_choice", "title": "整体评分", "required": False,
                 "options": ["好", "一般", "差"]},
            ],
        }

    def click_state(self):
        """点击保存那一刻的整份内容（服务器落库后逐字段核对的期望）。"""
        return {
            "title": CLICK_TITLE,
            "description": CLICK_DESC,
            "questions": [
                ("text", "开放建议", True, ()),
                ("single_choice", "整体评分", False, ("好", "一般", "差")),
            ],
        }

    def edit_during_pending(self):
        """等待首次创建结果期间做的一批修改：说明、题目标题、选项改/删/增、
        必填勾选、新增题目——覆盖所有输入方式，证明等待期间表单全部可用。"""
        page = self.page
        page.t("setDesc", "等待期间改过的说明\n追加一行")
        page.t("setQTitle", 1, "整体评分（改）")
        page.t("setOption", 1, 0, "很好")
        page.t("removeOption", 1, 2)        # 删掉“差”
        page.t("addOption", 1, "极差")
        page.t("setRequired", 1, True)
        page.t("addText", "等待期间新增题", False)

    def current_questions(self):
        """等待期间编辑完成后页面里的题目/选项/必填/顺序。"""
        return [
            ("text", "开放建议", True, ()),
            ("single_choice", "整体评分（改）", True, ("很好", "一般", "极差")),
            ("text", "等待期间新增题", False, ()),
        ]

    def second_payload(self):
        """转为编辑后再次点击保存应提交的请求体（即等待期间编辑后的当前内容）。"""
        return {
            "title": CLICK_TITLE,
            "description": "等待期间改过的说明\n追加一行",
            "questions": [
                {"type": "text", "title": "开放建议", "required": True},
                {"type": "single_choice", "title": "整体评分（改）", "required": True,
                 "options": ["很好", "一般", "极差"]},
                {"type": "text", "title": "等待期间新增题", "required": False},
            ],
        }

    def get_survey(self, survey_id):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        return data

    def assert_server_content(self, survey_id, expected, where):
        """逐字段核对服务器草稿（id 不参与比较）。"""
        data = self.get_survey(survey_id)
        self.assertEqual(data["title"], expected["title"], f"{where}：服务器标题不符")
        self.assertEqual(data["description"], expected["description"],
                         f"{where}：服务器说明不符（必须原样保留中文、引号与换行）")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], tuple(q["options"]))
             for q in data["questions"]],
            list(expected["questions"]),
            f"{where}：服务器题目/选项/必填/顺序与提交内容不符")

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

    def assert_form_questions(self, snapshot, expected, where):
        actual = [(q["type"], q["title"], q["required"], tuple(q["options"]))
                  for q in snapshot["questions"]]
        self.assertEqual(actual, list(expected),
                         f"{where}：表单题目/选项/必填/顺序与当前内容不符")

    def start_pending_create(self):
        """打开首页、填写表单、点击保存并挂起首次创建的 POST。

        返回 (before_ids, paused)：创建前的编号集合与被挂起的 POST。
        """
        before_ids = self.existing_ids()
        self.open_home()
        self.fill_create_form()
        self.page.hold_saves()
        self.page.t("submit")
        paused = self.page.pop_held()
        # 首次创建必须走 POST /api/surveys，且只携带点击那一刻的完整内容。
        self.assertEqual(paused["request"]["method"], "POST",
                         "首次创建必须发 POST，而不是其它方法")
        self.assertTrue(paused["request"]["url"].rstrip("/").endswith("/api/surveys"),
                        f"首次创建地址不对：{paused['request']['url']}")
        self.assertEqual(self.page.held_body(paused), self.click_payload(),
                         "首次创建的请求体不等于点击保存那一刻的完整内容")
        # 请求被挂起、尚未到达服务器：此时不应产生任何记录。
        self.assertEqual(self.existing_ids(), before_ids,
                         "创建请求尚未放行，服务器上不应出现新记录")
        return before_ids, paused

    def release_create_and_get_id(self, before_ids, paused):
        """放行首次创建，等页面确认已保存，返回新问卷编号。"""
        self.page.release_to_server(paused)
        self.page.wait_for("!!document.getElementById('save-note-saved')")
        new_ids = self.existing_ids() - before_ids
        self.assertEqual(len(new_ids), 1, "首次创建应只新增一份问卷记录")
        return new_ids.pop()

    def run_pending_create_with_edits(self):
        """完整走完“填写→挂起创建→等待期间编辑→放行成功→转为编辑”的前半段，
        返回新问卷编号。各用例在此之上做自己的断言或继续第二次保存。"""
        before_ids, paused = self.start_pending_create()
        self.edit_during_pending()
        survey_id = self.release_create_and_get_id(before_ids, paused)
        return survey_id

    # ---------- 主场景：等待期间继续编辑，首笔只保存快照，成功后转为编辑同一份 ----------

    def test_pending_create_saves_click_snapshot_and_adopts_edit_mode(self):
        before_ids, paused = self.start_pending_create()

        # 等待期间：明确显示正在保存、按钮禁用，但表单控件全部可用。
        saving_snap = self.page.snapshot()
        self.assertEqual(saving_snap["formHeading"], "新建问卷草稿",
                         "创建结果未返回时不应提前切换表单形态")
        self.assert_saving_ui(saving_snap, "首次创建等待期间")

        # 等待期间继续编辑（修改说明/题目标题/选项、增删选项与题目、切换必填），
        # 这些动作本身就是“表单仍然可用”的直接证据。
        self.edit_during_pending()

        # 等待期间重复点击保存（按钮已禁用）：不得追加第二笔请求。
        self.page.t("submit")
        self.page.settle(0.4)
        self.assertEqual(self.page.held_count(), 0,
                         "等待首次创建结果期间重复点击保存又发出了一笔请求")

        survey_id = self.release_create_and_get_id(before_ids, paused)
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_id}\"]')")

        snapshot = self.page.snapshot()
        # 不跳转、不清空：留在首页表单，保留等待期间的全部当前内容。
        self.assertEqual(snapshot["hash"], "#/",
                         "创建成功但还有未保存修改时不应跳到详情页")
        self.assertEqual(snapshot["title"], CLICK_TITLE, "标题被旧输入还原或清空")
        self.assertEqual(snapshot["description"], "等待期间改过的说明\n追加一行",
                         "说明被还原成提交时的旧值")
        self.assert_form_questions(snapshot, self.current_questions(),
                                   "创建成功返回后")
        # 表单就地切换为编辑刚创建的那份草稿：编号可见、有“取消”、按钮变“保存修改”。
        self.assertEqual(snapshot["formHeading"], f"编辑问卷草稿 #{survey_id}",
                         "表单没有切换为编辑刚创建的草稿（编号未显示）")
        self.assertEqual(snapshot["cancelHref"], f"#/surveys/{survey_id}",
                         "转为编辑后缺少指向该问卷详情的“取消”链接")
        self.assertEqual(snapshot["submit"]["text"], "保存修改",
                         "转为编辑后保存按钮文案应为“保存修改”")
        # 状态条分别说明：刚才提交的已保存（绿）、当前还有未保存修改（黄）。
        self.assert_saved_and_dirty(snapshot, "首次创建成功且有未保存修改时")
        # 首页列表出现刚创建的那一份记录（标题为点击时保存的标题）。
        links = [link for link in snapshot["listLinks"]
                 if link["href"] == f"#/surveys/{survey_id}"]
        self.assertEqual(len(links), 1, "首页列表没有出现刚创建的问卷记录")
        self.assertIn(CLICK_TITLE, links[0]["text"],
                      "列表中的新记录标题应是点击保存时的标题")

        # 第一次创建只保存点击时的快照：等待期间改过的说明、选项与顺序不混入。
        self.assert_server_content(survey_id, self.click_state(),
                                   "首次创建落库内容")

        # 页面不能自动补交等待期间的改动：再观察一拍，既没有新请求发出，
        # 服务器内容也保持点击时的快照。
        self.page.settle(0.8)
        self.assertEqual(self.page.held_count(), 0,
                         "创建成功后页面自动补交了等待期间的修改")
        self.assert_server_content(survey_id, self.click_state(),
                                   "自动补交检查")
        self.assertEqual(self.existing_ids() - before_ids, {survey_id},
                         "首次创建不应产生第二份问卷记录")

    # ---------- 随后主动“保存修改”：整份替换同一编号，成功后进详情 ----------

    def test_second_save_replaces_same_survey_and_enters_detail(self):
        survey_id = self.run_pending_create_with_edits()
        before_ids = self.existing_ids() - {survey_id}

        # 确认仍停留在转为编辑的表单上，再主动点击“保存修改”。
        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], "#/")
        self.assertEqual(snapshot["submit"]["text"], "保存修改")

        self.page.t("submit")
        paused_put = self.page.pop_held()
        # 第二次保存必须 PUT 同一编号（完整替换），请求体等于当前页面内容。
        self.assertEqual(paused_put["request"]["method"], "PUT",
                         "创建成功后的再次保存必须走 PUT，不能再 POST 出新问卷")
        self.assertTrue(
            paused_put["request"]["url"].rstrip("/").endswith(f"/api/surveys/{survey_id}"),
            f"第二次保存的编号变了：{paused_put['request']['url']}")
        self.assertEqual(self.page.held_body(paused_put), self.second_payload(),
                         "第二次保存提交的应是点击那一刻页面里的完整内容")

        # 第二次保存期间没有新变化：成功后进入这份问卷的详情。
        self.page.release_to_server(paused_put)
        self.page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        self.page.wait_for("!!document.querySelector('.q-list')")

        detail = self.page.detail()
        self.assertEqual(detail["heading"], f"#{survey_id} {CLICK_TITLE}")
        self.assertEqual(detail["description"], "等待期间改过的说明\n追加一行",
                         "详情说明应与第二次提交一致（中文、引号、换行原样保留）")
        self.assertEqual(len(detail["questions"]), 3, "详情题目数不对")
        lines = [q["line"] for q in detail["questions"]]
        self.assertTrue(lines[0].startswith("1. 开放建议"), lines[0])
        self.assertIn("文本题", lines[0])
        self.assertIn("必填", lines[0])
        self.assertTrue(lines[1].startswith("2. 整体评分（改）"), lines[1])
        self.assertIn("单选题", lines[1])
        self.assertTrue(lines[2].startswith("3. 等待期间新增题"), lines[2])
        self.assertIn("选填", lines[2])
        self.assertEqual([q["options"] for q in detail["questions"]],
                         [[], ["很好", "一般", "极差"], []],
                         "详情选项内容与顺序应与第二次提交一致")

        # 编号不变、整份替换：服务器内容等于第二次提交，列表仍只有最初创建的一条。
        self.assert_server_content(survey_id, {
            "title": self.second_payload()["title"],
            "description": self.second_payload()["description"],
            "questions": [
                ("text", "开放建议", True, ()),
                ("single_choice", "整体评分（改）", True, ("很好", "一般", "极差")),
                ("text", "等待期间新增题", False, ()),
            ],
        }, "第二次保存落库内容")
        self.assertEqual(self.existing_ids() - before_ids, {survey_id},
                         "第二次保存不应新增记录，列表只能有最初创建的那一条")

    # ---------- 边界：等待期间改过又恢复成点击时内容，按没有新修改处理 ----------

    def test_edits_reverted_before_create_response_go_straight_to_detail(self):
        before_ids, paused = self.start_pending_create()

        # 等待期间改说明、题目标题、选项，又在结果返回前全部恢复成点击时的内容。
        self.page.t("setDesc", "临时改过的说明")
        self.page.t("setQTitle", 0, "临时题目标题")
        self.page.t("setOption", 1, 1, "临时选项")
        self.page.t("setDesc", CLICK_DESC)
        self.page.t("setQTitle", 0, "开放建议")
        self.page.t("setOption", 1, 1, "一般")

        self.page.release_to_server(paused)
        # 按没有新修改处理：直接进入这份问卷的详情，不残留未保存提示。
        self.page.wait_for("location.hash.match(/^#\\/surveys\\/\\d+$/) !== null")
        survey_id = int(self.page.eval("location.hash").split("/")[-1])
        self.page.wait_for("!!document.querySelector('.q-list')")

        self.assertEqual(self.existing_ids() - before_ids, {survey_id},
                         "恢复成点击时内容后仍应只创建一份记录")
        self.assertIsNone(self.page.eval("document.getElementById('save-note-dirty')"),
                          "进入详情后不应残留“未保存修改”提示")
        detail = self.page.detail()
        self.assertEqual(detail["heading"], f"#{survey_id} {CLICK_TITLE}")
        self.assertEqual(detail["description"], CLICK_DESC)
        self.assertEqual([q["options"] for q in detail["questions"]],
                         [[], ["好", "一般", "差"]])
        self.assert_server_content(survey_id, self.click_state(),
                                   "改过又恢复场景落库内容")

    # ---------- 比较规则：标题/题目标题/选项仅首尾空白不同不算新修改 ----------

    def test_surrounding_whitespace_only_changes_are_not_new_changes(self):
        before_ids, paused = self.start_pending_create()

        # 等待期间只改动标题、题目标题、选项的首尾空白（裁剪后值不变）。
        self.page.t("setTitle", "  年度「满意度」调查\t")
        self.page.t("setQTitle", 0, " 开放建议 ")
        self.page.t("setQTitle", 1, "\t整体评分 ")
        self.page.t("setOption", 1, 2, " 差  ")

        self.page.release_to_server(paused)
        # 不算新修改：直接进入详情，不停留在表单、不出现未保存提示。
        self.page.wait_for("location.hash.match(/^#\\/surveys\\/\\d+$/) !== null")
        survey_id = int(self.page.eval("location.hash").split("/")[-1])
        self.page.wait_for("!!document.querySelector('.q-list')")

        self.assertEqual(self.existing_ids() - before_ids, {survey_id})
        detail = self.page.detail()
        self.assertEqual(detail["heading"], f"#{survey_id} {CLICK_TITLE}",
                         "标题按裁剪后的值保存，首尾空白不应进入详情")
        self.assert_server_content(survey_id, self.click_state(),
                                   "仅首尾空白差异场景落库内容")

    # ---------- 比较规则：说明的首尾空白有意义 ----------

    def test_description_surrounding_whitespace_is_a_real_change(self):
        before_ids, paused = self.start_pending_create()

        # 等待期间只给说明补上首尾空白：说明不裁剪，这就是真实修改。
        dirty_desc = CLICK_DESC + "\n"
        self.page.t("setDesc", dirty_desc)

        survey_id = self.release_create_and_get_id(before_ids, paused)
        self.page.wait_for(
            f"!!document.querySelector('a[href=\"#/surveys/{survey_id}\"]')")

        snapshot = self.page.snapshot()
        self.assertEqual(snapshot["hash"], "#/",
                         "说明首尾空白是真实修改，不应直接进入详情")
        self.assertEqual(snapshot["formHeading"], f"编辑问卷草稿 #{survey_id}")
        self.assertEqual(snapshot["description"], dirty_desc,
                         "说明的当前输入（含尾部换行）必须保留")
        self.assert_saved_and_dirty(snapshot, "说明首尾空白变化")
        # 服务器上仍是点击时的说明（不带尾部换行），等待期间的改动未被补交。
        self.assert_server_content(survey_id, self.click_state(),
                                   "说明留白场景首次落库内容")


if __name__ == "__main__":
    unittest.main(verbosity=2)
