#!/usr/bin/env python3
"""首页新建问卷时“单选题选项重复校验”的浏览器回归保障。

test_survey_replace.py 只通过 HTTP 接口核对服务端校验与落库结果；本文件改用真实
Chrome 驱动真实首页，以用户实际看到的提示、保留下来的输入与最终保存结果为断言
依据，保护新建表单里已经存在的下列行为（本次只补回归保障，不改产品规则）：

- 同一道单选题中，去掉首尾空白（空格 / 制表符 / 换行）后相同的选项判为重复：
  - “满意”与“ 满意 ”不得保存；首尾的空格、换行不能绕过校验；
  - 两项之间隔着其他选项仍然算重复，重复判定只按“后出现的那一项”报错；
  - 内容内部换行有实际意义：“满意\n一般”与“满意”“一般”是三个不同选项，
    但两个完全相同的多行选项仍应判重。
- 提示必须说明当前第几题以及重复的选项内容；后出现的重复选项有明确错误标记；
  点击顶部提示项把焦点移到那个选项。
- 校验失败时停留在新建表单：不发送创建请求，问卷列表不增加记录。
- 校验失败后标题、说明、全部题目、必填勾选与每个选项的原始输入全部保留，
  包括首尾空白与内部换行，不能为展示错误替用户清理或删改内容。
- 用户可修正重复选项后按当前输入重新判断并保存；也可删除重复选项，只要仍有
  至少两个非空且不重复的选项便允许保存；删到只剩一个时提示该题选项不足。
- 保存前删除前面的题目或选项，题号与选项编号按页面现有顺序更新，再次保存的
  错误提示与跳转焦点对应当前输入位置。
- 重复判断只限同一道题：不同题目使用相同选项、不同题目使用相同标题均可保存。
- 合法保存后进入新问卷详情：题目按原顺序、必填设置正确、选项去掉首尾空白，
  说明中的中文、引号与换行原样保留；返回首页后列表出现新编号与标题。

测试通过 Chrome DevTools Protocol 的 Fetch 域挂起发往 /api/surveys 的 POST：
客户端校验失败时一个 POST 都不应到达；合法保存放行后再读接口与页面详情核对。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_create_duplicate_options -v
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

# 测试专用启动器：app.py 本身用的是单线程 HTTPServer。真实 Chrome 关闭标签页时，
# 偶尔会把一条仍处于 keep-alive 的空闲连接留在浏览器进程的连接池里（迟迟不关），
# 单线程服务器会阻塞在这条连接的“下一次请求”读取上，导致后续所有请求排队超时。
# 这里不改产品代码，只在测试夹具里把服务器类换成多线程版本，对外 HTTP 行为完全
# 一致；sqlite 连接因此需要允许跨线程使用（请求量很小，写入为短促的串行事务）。
THREADED_LAUNCHER = r"""
import importlib.util
import http.server
import sqlite3
import sys

app_path, rest = sys.argv[1], sys.argv[2:]
sys.argv = [app_path] + rest

_real_connect = sqlite3.connect
sqlite3.connect = lambda *a, **k: _real_connect(*a, **{**k, "check_same_thread": False})

spec = importlib.util.spec_from_file_location("opensurvey_app_under_test", app_path)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class _ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

app.HTTPServer = _ThreadedServer
app.main()
"""


# --------------------------------------------------------------------------
# 真实 app.py 服务进程
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-dup-")
        self.process = None
        self.base_url = None

    def start(self):
        # 显式选定空闲端口：app.py 每个请求都向 stderr 写访问日志，若用 PIPE
        # 承接而不持续读取，单线程服务会在缓冲区写满后阻塞，故直接丢弃输出。
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.process = subprocess.Popen(
            [sys.executable, "-c", THREADED_LAUNCHER, str(APP), "serve",
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

    def list_ids(self):
        status, data = self.request("GET", "/api/surveys")
        assert status == 200, data
        return {item["id"]: item["title"] for item in data["surveys"]}


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
        """把不属于拦截器的事件放回队列，供其它等待者继续观察。"""
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
# 注入到页面文档的测试操作手柄：所有动作都走真实的 DOM 事件与按钮
# --------------------------------------------------------------------------

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
  // 经“添加选项”按钮追加，走真实的 renumber 与自动聚焦逻辑。
  T.addOption = (qi, v) => {
    const card = T.cards()[qi];
    card.querySelector('.add-opt').click();
    const rows = card.querySelectorAll('.opt-row');
    const el = rows[rows.length - 1].querySelector('.opt-text');
    el.focus(); el.value = v; fire(el);
  };
  // 必须点击行内“删除”按钮：直接移除 DOM 节点会绕过应用自己的 renumber。
  T.deleteOption = (qi, oi) => {
    T.cards()[qi].querySelectorAll('.opt-row')[oi]
      .querySelector('button.link.danger').click();
  };
  T.deleteQuestion = i => {
    const b = [...T.cards()[i].querySelectorAll('button')]
      .find(x => x.textContent.trim() === '删除本题');
    if (!b) throw new Error('找不到删除本题按钮');
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
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
  // 顶部错误提示条里的每一条都是按钮，返回其文案并支持按内容点击。
  T.bannerItems = () =>
      [...document.querySelectorAll('#form-banner li button')].map(b => b.textContent);
  T.clickBannerItem = needle => {
    const b = [...document.querySelectorAll('#form-banner li button')]
      .find(x => x.textContent.indexOf(needle) !== -1);
    if (!b) throw new Error('提示条中找不到包含“' + needle + '”的条目');
    b.click();
  };
  // 当前焦点落在哪个选项上：返回题卡序号、选项序号与页面上显示的编号文案。
  T.focusedOption = () => {
    const el = document.activeElement;
    if (!el || !el.classList.contains('opt-text')) return null;
    const card = el.closest('.q-card');
    const cards = T.cards();
    const qi = cards.indexOf(card);
    const rows = [...card.querySelectorAll('.opt-row')];
    const oi = rows.findIndex(r => r.contains(el));
    return {
      qi, oi,
      value: el.value,
      qIndex: card.querySelector('.q-index').textContent,
      optIndex: rows[oi].querySelector('.opt-index').textContent,
    };
  };
})();
"""

# 新建表单的完整状态快照：编号文案、原始输入值、错误标记全部来自真实 DOM。
SNAPSHOT_JS = r"""
(() => {
  const banner = document.getElementById('form-banner');
  const titleEl = document.getElementById('survey-title');
  const descEl = document.getElementById('survey-desc');
  const heading = document.querySelector('#draft-form h2');
  return {
    hash: location.hash,
    heading: heading ? heading.textContent : null,
    bannerVisible: banner ? !banner.hidden : false,
    bannerText: banner ? banner.textContent : '',
    bannerItems: [...document.querySelectorAll('#form-banner li button')]
      .map(b => b.textContent),
    title: titleEl ? titleEl.value : null,
    description: descEl ? descEl.value : null,
    questions: [...document.querySelectorAll('.q-card')].map(card => ({
      type: card.dataset.type,
      qIndex: card.querySelector('.q-index').textContent,
      title: card.querySelector('.q-title').value,
      required: card.querySelector('.q-required').checked,
      optErr: card.querySelector('.opt-err') ? card.querySelector('.opt-err').textContent : null,
      options: [...card.querySelectorAll('.opt-row')].map(row => ({
        indexLabel: row.querySelector('.opt-index').textContent,
        value: row.querySelector('.opt-text').value,
        invalid: row.querySelector('.opt-text').classList.contains('invalid'),
      })),
    })),
    listItems: [...document.querySelectorAll('#survey-list a')]
      .map(a => ({href: a.getAttribute('href'), text: a.textContent})),
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
    line: li.querySelector('.q-line') ? li.querySelector('.q-line').innerText : li.innerText,
    options: [...li.querySelectorAll('ol > li')].map(o => o.textContent),
  })),
  anyVisibleBanner: !!document.querySelector('.banner:not([hidden])'),
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
        self.held = []                # 被挂起的创建请求（POST）
        self._held_cond = threading.Condition()
        self._pumping = False

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})
        # 后台持续运转 CDP 事件：拦截开启期间，GET（列表/详情）必须立即放行，
        # 否则页面切换会被自己的 GET 饿死；POST 才收集起来交给测试断言/放行。
        # 所有标签页共用一条 WebSocket，事件队列是全局的，因此这里必须只处理
        # 属于本会话（sessionId）的事件，其余一律放回队列交给对应标签页，
        # 避免上一个标签页即将退出的泵线程误吞新标签页挂起的创建请求。
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
                    if params["request"]["method"] == "POST":
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
        self._pump_thread.join(timeout=2)
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

    def open_home(self, base_url):
        self.ws.drain_events(0)
        self.call("Page.navigate", {"url": f"{base_url}/#/"})
        self.wait_for("!!document.querySelector('#draft-form') && "
                      "document.querySelector('#draft-form h2') && "
                      "document.querySelector('#draft-form h2').textContent === '新建问卷草稿'")

    def wait_for(self, expression, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            value = self.eval(expression)
            if value:
                return value
            time.sleep(0.15)
        raise AssertionError(f"等待页面条件超时：{expression}")

    def settle(self, seconds=0.5):
        """让微任务/异步回调有机会落地。"""
        time.sleep(seconds)

    # ---------- 创建请求（POST）拦截 ----------

    def hold_creates(self):
        """挂起所有发往 /api/surveys 的 POST；GET 由后台事件循环立即放行。"""
        with self._held_cond:
            self.held = []
        self.call("Fetch.enable", {
            "patterns": [{"urlPattern": "*api/surveys*", "requestStage": "Request"}]})

    def held_requests(self):
        with self._held_cond:
            return list(self.held)

    def assert_no_create_request(self, wait=0.8):
        """客户端校验失败时：在足够长的观察窗口内一个创建请求都不应发出。"""
        time.sleep(wait)
        held = self.held_requests()
        assert not held, f"校验失败仍发出了 {len(held)} 个创建请求"

    def wait_held_create(self, timeout=8):
        end = time.time() + timeout
        with self._held_cond:
            while not self.held and time.time() < end:
                self._held_cond.wait(max(0.0, end - time.time()))
            if self.held:
                return self.held[0]
        raise AssertionError("合法表单保存时创建请求未发出（POST 未被挂起）")

    def release_create(self, paused):
        """让挂起的创建请求真正到达服务器并把响应原样带回页面。"""
        self.call("Fetch.continueRequest", {"requestId": paused["requestId"]})

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

    def wait_detail(self):
        """等待合法保存后进入新问卷详情，返回新问卷编号。"""
        self.wait_for(
            "/#\\/surveys\\/\\d+$/.test(location.hash) && "
            "!!document.querySelector('.q-list')")
        return int(self.eval("location.hash").rsplit("/", 1)[1])


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
        self.page.open_home(self.server.base_url)
        self.page.hold_creates()

    def tearDown(self):
        self.page.stop_holding()
        self.page.close()

    # ---------- 断言辅助 ----------

    def assert_still_on_create_form(self, snap):
        self.assertEqual(snap["hash"], "#/", "校验失败后离开了新建表单")
        self.assertEqual(snap["heading"], "新建问卷草稿",
                         "校验失败后页面不再是新建表单")
        self.assertTrue(snap["bannerVisible"], "应显示顶部错误提示条")

    def assert_duplicate_banner(self, snap, q_label, content):
        """提示条必须指出当前题号与重复选项内容，且只有这一条重复错误。"""
        self.assertIn(q_label, snap["bannerText"],
                      f"提示未说明出错题号：{snap['bannerText']}")
        self.assertIn(content, snap["bannerText"],
                      f"提示未说明重复选项内容：{snap['bannerText']}")
        self.assertIn("重复", snap["bannerText"])
        dup_items = [m for m in snap["bannerItems"]
                     if q_label in m and content in m and "重复" in m]
        self.assertEqual(len(dup_items), 1,
                         f"顶部应恰好一条“{q_label}选项{content}重复”的提示，"
                         f"实际为：{snap['bannerItems']}")

    def assert_invalid_later_only(self, snap, qi, bad_oi):
        """只有后出现的重复选项带错误标记。"""
        flags = [o["invalid"] for o in snap["questions"][qi]["options"]]
        expected = [j == bad_oi for j in range(len(flags))]
        self.assertEqual(flags, expected,
                         f"第 {qi + 1} 题各选项错误标记不符：{flags}")

    def assert_focus_on_option(self, qi, oi, value=None,
                               q_label=None, opt_label=None):
        focus = self.page.t("focusedOption")
        self.assertIsNotNone(focus, "焦点没有落在选项输入框上")
        self.assertEqual(focus["qi"], qi, "焦点所在题卡不对")
        self.assertEqual(focus["oi"], oi, "焦点所在选项不对")
        if value is not None:
            self.assertEqual(focus["value"], value, "焦点选项保留的原始输入不对")
        if q_label is not None:
            self.assertEqual(focus["qIndex"], q_label, "焦点所在题号文案不对")
        if opt_label is not None:
            self.assertEqual(focus["optIndex"], opt_label, "焦点所在选项编号文案不对")

    def option_values(self, snap, qi):
        return [o["value"] for o in snap["questions"][qi]["options"]]

    def option_labels(self, snap, qi):
        return [o["indexLabel"] for o in snap["questions"][qi]["options"]]

    def save_valid_and_open_detail(self):
        """放行当前挂起的合法创建请求，等待详情，返回 (新编号, 详情快照)。"""
        page = self.page
        paused = page.wait_held_create()
        page.release_create(paused)
        new_id = page.wait_detail()
        return new_id, page.detail()

    def assert_saved_survey(self, new_id, expected):
        status, data = self.server.request("GET", f"/api/surveys/{new_id}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["id"], new_id)
        self.assertEqual(data["title"], expected["title"])
        self.assertEqual(data["description"], expected["description"])
        self.assertEqual(
            [(q["type"], q["title"], q["required"], q["options"])
             for q in data["questions"]],
            [(q["type"], q["title"], q["required"], q["options"])
             for q in expected["questions"]],
            "服务端保存的题目顺序/必填/选项与预期不符")
        return data

    # ---------- 一、重复检测、提示、错误标记与焦点 ----------

    def test_spaces_cannot_bypass_duplicate_check_with_focus_and_no_request(self):
        """“满意”与“ 满意 ”：提示第 2 题重复，标记后者，点提示聚焦，且不发创建请求。"""
        page = self.page
        ids_before = self.server.list_ids()

        page.t("setTitle", "重复校验问卷")
        page.t("setDesc", "一份说明\n带换行与\"引号\"")
        page.t("addText", "第一道文本题", True)
        page.t("addChoice", "您的满意度", ["满意", " 满意 "], False)
        page.t("submit")
        page.assert_no_create_request()

        snap = page.snapshot()
        self.assert_still_on_create_form(snap)
        # 重复题是第 2 题，提示必须写出题号与重复内容。
        self.assert_duplicate_banner(snap, "第 2 题", "满意")
        # 题目下方的行内错误同样定位到该题与重复内容。
        self.assertIn("第 2 题", snap["questions"][1]["optErr"])
        self.assertIn("满意", snap["questions"][1]["optErr"])
        self.assertIn("重复", snap["questions"][1]["optErr"])
        # 后出现的那一项（选项 2）有错误标记，第一项没有。
        self.assert_invalid_later_only(snap, qi=1, bad_oi=1)

        # 点击顶部提示把焦点移到后出现的重复选项上，原始首尾空格仍在输入框里。
        page.t("clickBannerItem", "满意")
        self.assert_focus_on_option(
            qi=1, oi=1, value=" 满意 ",
            q_label="第 2 题", opt_label="选项 2")

        # 没有创建记录：接口层面问卷列表不增加。
        self.assertEqual(self.server.list_ids(), ids_before,
                         "校验失败不应在问卷列表中增加记录")

    def test_surrounding_newlines_and_tabs_cannot_bypass_check(self):
        """首尾是换行/制表符的两个选项去掉首尾空白后仍判重。"""
        page = self.page
        ids_before = self.server.list_ids()

        page.t("setTitle", "换行重复问卷")
        page.t("addChoice", "满意度", ["满意\n", "\n满意\t"], False)
        page.t("submit")
        page.assert_no_create_request()

        snap = page.snapshot()
        self.assert_still_on_create_form(snap)
        self.assert_duplicate_banner(snap, "第 1 题", "满意")
        self.assert_invalid_later_only(snap, qi=0, bad_oi=1)
        # 首尾换行/制表符属于原始输入，必须原样留在输入框里。
        self.assertEqual(self.option_values(snap, 0), ["满意\n", "\n满意\t"])
        self.assertEqual(self.server.list_ids(), ids_before)

    def test_duplicate_separated_by_other_option_still_flagged(self):
        """重复项之间隔着其他选项时，仍然只标记后出现的重复项。"""
        page = self.page
        page.t("setTitle", "隔项重复问卷")
        page.t("addChoice", "满意度", ["满意", "一般", " 满意 "], False)
        page.t("submit")
        page.assert_no_create_request()

        snap = page.snapshot()
        self.assert_still_on_create_form(snap)
        self.assert_duplicate_banner(snap, "第 1 题", "满意")
        self.assert_invalid_later_only(snap, qi=0, bad_oi=2)
        page.t("clickBannerItem", "满意")
        self.assert_focus_on_option(
            qi=0, oi=2, value=" 满意 ",
            q_label="第 1 题", opt_label="选项 3")

    # ---------- 二、校验失败后原始输入全部保留 ----------

    def test_failed_validation_keeps_all_raw_inputs_including_whitespace(self):
        """失败后标题/说明/题目/必填/每个选项的原始输入（含首尾空白、内部换行）全部保留。"""
        page = self.page
        page.t("setTitle", "  保留标题  ")
        page.t("setDesc", "第一行\n  缩 进 与\"引号\"\n第三行  ")
        page.t("addChoice", " 满意度题 ", [" 满意 ", "满意", "一般\n换行\n"], True)
        page.t("addText", " 文本题标题 ", True)
        page.t("submit")
        page.assert_no_create_request()

        snap = page.snapshot()
        self.assert_still_on_create_form(snap)
        self.assertEqual(snap["title"], "  保留标题  ", "标题的首尾空白被清理")
        self.assertEqual(snap["description"],
                         "第一行\n  缩 进 与\"引号\"\n第三行  ",
                         "说明的中文、引号、缩进或换行被改写")
        self.assertEqual(len(snap["questions"]), 2, "题目数量被改动")
        q1, q2 = snap["questions"]
        self.assertEqual(q1["type"], "single_choice")
        self.assertEqual(q1["title"], " 满意度题 ", "题目标题的首尾空白被清理")
        self.assertTrue(q1["required"], "必填勾选丢失")
        self.assertEqual(self.option_values(snap, 0),
                         [" 满意 ", "满意", "一般\n换行\n"],
                         "选项原始输入（含首尾空白、内部换行）被清理或删改")
        # 后出现的重复项是第 2 个选项；其余两项不被误标。
        self.assert_invalid_later_only(snap, qi=0, bad_oi=1)
        self.assertEqual(q2["type"], "text")
        self.assertEqual(q2["title"], " 文本题标题 ")
        self.assertTrue(q2["required"], "文本题必填勾选丢失")

        # 再点一次保存：输入原样未动，仍应被同样拦下（按当前输入重新判断）。
        page.t("submit")
        page.assert_no_create_request()
        snap2 = page.snapshot()
        self.assertTrue(snap2["bannerVisible"])
        self.assertEqual(snap2["title"], "  保留标题  ")
        self.assertEqual(self.option_values(snap2, 0),
                         [" 满意 ", "满意", "一般\n换行\n"])

    # ---------- 三、修正重复内容后按当前输入重新判断并保存 ----------

    def test_fix_duplicate_then_save_enters_detail_and_list(self):
        """把重复项改成不同内容后再次保存：按当前输入放行，详情与首页列表正确。"""
        page = self.page
        page.t("setTitle", " 修正后保存问卷 ")
        page.t("setDesc", "说明保留\"引号\"\n与换行")
        page.t("addChoice", "满意度", ["满意", " 满意 "], True)
        page.t("submit")
        page.assert_no_create_request()
        self.assertTrue(page.snapshot()["bannerVisible"])

        # 把后一项改成不同内容（首尾仍带空白，保存时按 trim 后的值判断与存储）。
        page.t("setOption", 0, 1, " 不满意 ")
        page.t("submit")

        new_id, detail = self.save_valid_and_open_detail()
        self.assertEqual(detail["heading"], f"#{new_id} 修正后保存问卷")
        self.assertEqual(detail["description"], "说明保留\"引号\"\n与换行",
                         "说明中的引号与换行必须原样保留")
        self.assertFalse(detail["anyVisibleBanner"])
        self.assertEqual(len(detail["questions"]), 1)
        self.assertIn("满意度", detail["questions"][0]["line"])
        self.assertIn("必填", detail["questions"][0]["line"])
        # 选项按原顺序保存，且首尾空白已去除。
        self.assertEqual(detail["questions"][0]["options"], ["满意", "不满意"])

        saved = self.assert_saved_survey(new_id, {
            "title": "修正后保存问卷",
            "description": "说明保留\"引号\"\n与换行",
            "questions": [
                {"type": "single_choice", "title": "满意度", "required": True,
                 "options": ["满意", "不满意"]},
            ],
        })

        # 返回首页：列表出现新编号与标题。
        page.t("clickHref", "#/")
        page.wait_for(
            f"!!document.querySelector('#survey-list a[href=\"#/surveys/{new_id}\"]')")
        snap = page.snapshot()
        items = {item["href"]: item["text"] for item in snap["listItems"]}
        self.assertEqual(items[f"#/surveys/{new_id}"],
                         f"#{new_id} 修正后保存问卷")
        self.assertEqual(self.server.list_ids().get(new_id), "修正后保存问卷")
        self.assertEqual(saved["id"], new_id)

    # ---------- 四、删除重复选项后保存 / 删到只剩一个 ----------

    def test_delete_duplicate_option_then_save_with_remaining_two(self):
        """删除重复项后仍有两个非空不重复选项：允许保存，剩余选项按顺序落库。"""
        page = self.page
        page.t("setTitle", "删除重复项问卷")
        page.t("addChoice", "满意度", ["满意", "一般", " 满意 "], False)
        page.t("submit")
        page.assert_no_create_request()
        self.assert_invalid_later_only(page.snapshot(), qi=0, bad_oi=2)

        # 删掉重复的第 3 项，剩“满意 / 一般”。应用只在再次保存时清空旧提示，
        # 这里不要求编辑中途提示消失；关键是被标记的那一行已删除、剩余输入无误标，
        # 下一次保存必须按当前输入放行。
        page.t("deleteOption", 0, 2)
        snap = page.snapshot()
        self.assertEqual(self.option_values(snap, 0), ["满意", "一般"])
        self.assertEqual(self.option_labels(snap, 0), ["选项 1", "选项 2"])
        self.assertFalse(any(o["invalid"] for o in snap["questions"][0]["options"]),
                         "删除被标记的重复项后，剩余选项不应带着错误标记")

        page.t("submit")
        new_id, detail = self.save_valid_and_open_detail()
        self.assertEqual(detail["questions"][0]["options"], ["满意", "一般"])
        self.assert_saved_survey(new_id, {
            "title": "删除重复项问卷",
            "description": "",
            "questions": [
                {"type": "single_choice", "title": "满意度", "required": False,
                 "options": ["满意", "一般"]},
            ],
        })

    def test_delete_down_to_one_option_still_reports_not_enough_options(self):
        """重复提示后一路删到只剩一个选项：再次保存提示该题选项不足，不发请求；补齐后可保存。"""
        page = self.page
        page.t("setTitle", "选项不足问卷")
        page.t("addChoice", "满意度", ["满意", "一般", " 满意 "], False)
        page.t("submit")
        page.assert_no_create_request()

        # 删掉重复项（第 3 项），再删掉“一般”，只剩“满意”一个。
        page.t("deleteOption", 0, 2)
        page.t("deleteOption", 0, 1)
        snap = page.snapshot()
        self.assertEqual(self.option_values(snap, 0), ["满意"])
        self.assertEqual(self.option_labels(snap, 0), ["选项 1"])

        page.t("submit")
        page.assert_no_create_request()
        snap = page.snapshot()
        self.assert_still_on_create_form(snap)
        self.assertIn("第 1 题", snap["bannerText"])
        self.assertIn("至少需要两个选项", snap["bannerText"],
                      f"应提示选项不足：{snap['bannerItems']}")
        # 选项不足不是内容重复：不应留下任何重复错误标记。
        self.assertFalse(any(o["invalid"] for o in snap["questions"][0]["options"]),
                         "提示选项不足时不应残留重复错误标记")
        self.assertEqual(self.option_values(snap, 0), ["满意"],
                         "提示错误不应删改仅剩的输入")

        # 补回一个不同选项后即可保存。
        page.t("addOption", 0, "一般")
        snap = page.snapshot()
        self.assertEqual(self.option_values(snap, 0), ["满意", "一般"])
        page.t("submit")
        new_id, detail = self.save_valid_and_open_detail()
        self.assertEqual(detail["questions"][0]["options"], ["满意", "一般"])

    # ---------- 五、删除前面的题目/选项后，编号与错误位置按当前顺序更新 ----------

    def test_renumber_after_deleting_preceding_question_and_option(self):
        """删掉前面的题与选项后：题号/选项编号重排，重复提示与焦点对应当前位置。"""
        page = self.page
        page.t("setTitle", "重排编号问卷")
        page.t("addText", "将被删除的题", False)                          # 原第 1 题
        page.t("addChoice", "保留单选题", ["将删选项", "保留甲", "保留乙"], False)  # 原第 2 题
        page.t("addChoice", "满意度", ["满意", "一般", " 满意 ", "末尾"], False)   # 原第 3 题

        # 删掉原第 1 题后题卡整体前移：原第 2 题变第 1 题、原第 3 题变第 2 题。
        page.t("deleteQuestion", 0)
        # 在重排后的顺序上分别删掉前一题的首选项、后一题夹在中间的“一般”。
        page.t("deleteOption", 0, 0)
        page.t("deleteOption", 1, 1)

        snap = page.snapshot()
        self.assertEqual([q["qIndex"] for q in snap["questions"]],
                         ["第 1 题", "第 2 题"], "删除前面的题后题号未重排")
        self.assertEqual(snap["questions"][0]["title"], "保留单选题")
        self.assertEqual(self.option_values(snap, 0), ["保留甲", "保留乙"])
        self.assertEqual(self.option_labels(snap, 0), ["选项 1", "选项 2"],
                         "删除前面的选项后选项编号未重排")
        # 重复题现在是第 2 题，重复项删掉“一般”后位于选项 2。
        self.assertEqual(snap["questions"][1]["qIndex"], "第 2 题")
        self.assertEqual(self.option_values(snap, 1), ["满意", " 满意 ", "末尾"])
        self.assertEqual(self.option_labels(snap, 1),
                         ["选项 1", "选项 2", "选项 3"])

        page.t("submit")
        page.assert_no_create_request()
        snap = page.snapshot()
        self.assert_still_on_create_form(snap)
        # 提示只能按重排后的题号（第 2 题），不能再出现已删除的“第 3 题”。
        self.assert_duplicate_banner(snap, "第 2 题", "满意")
        self.assertNotIn("第 3 题", snap["bannerText"])
        self.assert_invalid_later_only(snap, qi=1, bad_oi=1)

        # 点击提示：焦点落到当前第 2 题的选项 2（即保留着首尾空格的“ 满意 ”）。
        page.t("clickBannerItem", "满意")
        self.assert_focus_on_option(
            qi=1, oi=1, value=" 满意 ",
            q_label="第 2 题", opt_label="选项 2")

    # ---------- 六、重复只限同题；跨题相同选项、相同题目标题允许保存 ----------

    def test_same_options_and_titles_across_questions_save_fine(self):
        """两道同名单选题使用相同选项、再加一道同名文本题：正常保存且必填各自保留。"""
        page = self.page
        page.t("setTitle", "跨题重复问卷")
        page.t("setDesc", "跨题说明")
        page.t("addChoice", "重名题", ["满意", "一般"], True)
        page.t("addChoice", "重名题", ["满意", "一般"], False)
        page.t("addText", "重名题", False)
        page.t("submit")

        new_id, detail = self.save_valid_and_open_detail()
        self.assertEqual(len(detail["questions"]), 3)
        self.assertIn("必填", detail["questions"][0]["line"])
        self.assertIn("选填", detail["questions"][1]["line"])
        self.assertEqual(detail["questions"][0]["options"], ["满意", "一般"])
        self.assertEqual(detail["questions"][1]["options"], ["满意", "一般"])
        self.assertEqual(detail["questions"][2]["options"], [])

        self.assert_saved_survey(new_id, {
            "title": "跨题重复问卷",
            "description": "跨题说明",
            "questions": [
                {"type": "single_choice", "title": "重名题", "required": True,
                 "options": ["满意", "一般"]},
                {"type": "single_choice", "title": "重名题", "required": False,
                 "options": ["满意", "一般"]},
                {"type": "text", "title": "重名题", "required": False, "options": []},
            ],
        })

    # ---------- 七、选项内部换行有实际意义 ----------

    def test_internal_newlines_are_significant(self):
        """“满意\\n一般”与“满意”“一般”互不相同可保存；两个相同的多行选项仍判重。"""
        page = self.page

        # 三个互不相同的选项（一个内部带换行）必须放行。
        page.t("setTitle", "多行选项问卷")
        page.t("addChoice", "满意度", ["满意\n一般", "满意", "一般"], False)
        page.t("submit")
        new_id, detail = self.save_valid_and_open_detail()
        self.assertEqual(detail["questions"][0]["options"],
                         ["满意\n一般", "满意", "一般"])
        status, saved = self.server.request("GET", f"/api/surveys/{new_id}")
        self.assertEqual(status, 200)
        self.assertEqual(saved["questions"][0]["options"],
                         ["满意\n一般", "满意", "一般"])

        # 回到首页再建一份：两个完全相同的多行选项仍然判重，
        # 证明内部换行既不会被压平合并，也不会让逐字相同的内容蒙混过关。
        page.t("clickHref", "#/")
        page.wait_for("!!document.querySelector('#draft-form')")
        page.hold_creates()
        ids_before = self.server.list_ids()
        page.t("setTitle", "相同多行选项问卷")
        page.t("addChoice", "满意度", ["第一行\n第二行", "第一行\n第二行"], False)
        page.t("submit")
        page.assert_no_create_request()
        snap = page.snapshot()
        self.assert_still_on_create_form(snap)
        self.assert_duplicate_banner(snap, "第 1 题", "第一行\n第二行")
        self.assert_invalid_later_only(snap, qi=0, bad_oi=1)
        self.assertEqual(self.option_values(snap, 0),
                         ["第一行\n第二行", "第一行\n第二行"])
        self.assertEqual(self.server.list_ids(), ids_before)

    # ---------- 八、合法保存：顺序、必填、trim、说明原样、首页列表 ----------

    def test_valid_save_detail_shows_order_required_trimmed_options_and_raw_desc(self):
        """完整合法保存：详情按原顺序展示题目/必填/去首尾空白选项，说明原样，首页出现新记录。"""
        page = self.page
        title_raw = "  年度满意度问卷  "
        desc_raw = "这是一份\"匿名\"问卷\n请如实填写\n  谢谢参与  "
        ids_before = self.server.list_ids()
        page.t("setTitle", title_raw)
        page.t("setDesc", desc_raw)
        page.t("addChoice", "您的满意度？", [" 满意 ", "一般\n换行", "不满意"], True)
        page.t("addText", "还有什么建议", False)
        page.t("submit")

        new_id, detail = self.save_valid_and_open_detail()

        # 详情页用户可见内容。
        self.assertEqual(detail["heading"], f"#{new_id} 年度满意度问卷")
        self.assertEqual(detail["description"], desc_raw,
                         "说明中的中文、引号、换行与缩进必须原样保留")
        self.assertFalse(detail["anyVisibleBanner"])
        self.assertEqual(len(detail["questions"]), 2)
        q1_line, q2_line = detail["questions"][0]["line"], detail["questions"][1]["line"]
        self.assertIn("您的满意度？", q1_line)
        self.assertIn("单选题", q1_line)
        self.assertIn("必填", q1_line)
        self.assertIn("还有什么建议", q2_line)
        self.assertIn("文本题", q2_line)
        self.assertIn("选填", q2_line)
        # 选项按原顺序，去掉首尾空白，内部换行保留为同一个选项。
        self.assertEqual(detail["questions"][0]["options"],
                         ["满意", "一般\n换行", "不满意"])
        self.assertEqual(detail["questions"][1]["options"], [])

        # 接口落库结果与详情一致。
        self.assert_saved_survey(new_id, {
            "title": "年度满意度问卷",
            "description": desc_raw,
            "questions": [
                {"type": "single_choice", "title": "您的满意度？", "required": True,
                 "options": ["满意", "一般\n换行", "不满意"]},
                {"type": "text", "title": "还有什么建议", "required": False,
                 "options": []},
            ],
        })

        # 返回首页：列表出现对应新编号与标题。
        page.t("clickHref", "#/")
        page.wait_for(
            f"!!document.querySelector('#survey-list a[href=\"#/surveys/{new_id}\"]')")
        items = {item["href"]: item["text"]
                 for item in page.snapshot()["listItems"]}
        self.assertEqual(items[f"#/surveys/{new_id}"],
                         f"#{new_id} 年度满意度问卷")
        ids_after = self.server.list_ids()
        # 与保存前相比只多出这一条记录，既有记录标题不变。
        self.assertEqual(set(ids_after) - set(ids_before), {new_id})
        self.assertEqual(ids_after[new_id], "年度满意度问卷")
        for old_id, old_title in ids_before.items():
            self.assertEqual(ids_after[old_id], old_title)


if __name__ == "__main__":
    unittest.main(verbosity=2)
