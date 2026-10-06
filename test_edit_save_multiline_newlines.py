#!/usr/bin/env python3
"""问卷草稿编辑页“保存多行文字不得悄悄改写原稿换行”的浏览器回归保障。

接口（POST/PUT）会逐字符保留文本内部的三种换行：回车换行（CRLF，\\r\\n）、
单独回车（CR，\\r）与换行（LF，\\n）。但把这些值写进 textarea 的瞬间，浏览器
就会把它们统一成 LF（实测在给 .value 赋值时即发生），因此编辑页靠
“记住原稿 + 按可见内容比较”在保存时还原未改字段的原始字符串。本文件用真实
Chrome 驱动真实页面，围绕这项**既有保存行为**做端到端回归：

- 打开编辑页：标题、说明、题目标题、单选选项中混用的 CRLF/CR/LF 显示成相同的
  分行文字——行次、行数、中文与引号完整，不挤成一行，也不额外插入空行；题目
  与全部选项次序原样；打开编辑页本身不改数据。
- 不改任何文字直接保存，或只调整某题的必填勾选后保存：问卷编号不变、不新增
  记录，所有未改文字字段再次读取时逐字符等于原稿（CRLF 仍是 CRLF、单独 CR
  仍是 CR，不能全部变成 LF），题目与选项次序不变。
- 改过又恢复：先改动一个字段，再把可见文字与分行恢复成打开时的样子再保存，
  该字段仍按原稿换行形式保存（标题/题目标题/选项/说明四类字段都覆盖）。
- 标题、题目标题、选项只增删首尾空白：仍按既有裁剪规则保存，并沿用原稿内部的
  CRLF/CR；说明不裁剪，只给说明加首尾空白属于真实修改，保存后必须保留。
- 真实修改：只改一个多行字段的文字或内部行次后保存，该字段保存编辑框当前内容
  （换行只能是 LF），其余未改字段仍各自保留原换行形式，不被旧原稿覆盖，也不
  被同次保存牵连。保存成功后进入同一问卷详情展示修改后的文字，再次打开编辑页
  也显示已保存内容；在这一代编辑页上原样再存一次，结果保持不变。

断言全部基于真实 Chrome 中用户可见的 DOM、表单值，以及保存后再次读取接口得到
的实际落库字符串（逐字符比较，CRLF/CR 与 LF 不可互相冒充）。

仅依赖 Python 标准库与本机 Google Chrome，运行方式：

    python3 -m unittest test_edit_save_multiline_newlines -v
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

APP = Path(os.environ.get("APP_UNDER_TEST",
                          Path(__file__).resolve().parent / "app.py"))
CHROME = os.environ.get("CHROME_BIN", "google-chrome")

CRLF = "\r\n"
CR = "\r"
LF = "\n"

# -- 原稿：文本题 + 单选题，标题/说明/题目标题/选项都带多行内容，同一字段内混用
# CRLF、单独 CR 与 LF，并含中文与引号。标题/题目标题/选项首尾不放空白（接口会
# 裁剪），内部三种换行各自至少出现一次；说明不裁剪，首尾空白留给专门用例追加。
TITLE = f"问卷“标题甲”{CRLF}第二行“引号”{CR}第三行单独CR{LF}第四行普通LF"
DESCRIPTION = f"说明首行{CRLF}中文与“弯引号”、\"直引号\"{CR}CR行{LF}LF行"
Q0_TITLE = f"必填文本题{CRLF}标题第二行{CR}CR行{LF}LF行“引号”"
Q1_TITLE = f"选填单选题{CR}题标CR行{LF}题标LF行"
OPT0 = f"选项甲{CRLF}甲的第二行“引号”"
OPT1 = f"选项乙{CR}乙CR行"
OPT2 = f"选项丙{LF}丙LF行“弯引号”"
OPT3 = "选项丁"
Q2_TITLE = f"必填单选题{LF}题标仅LF换行"
Q2_O0 = f"一{CRLF}一二"
Q2_O1 = f"二{LF}二二"
Q3_TITLE = f"选填文本题{LF}题标LF行"


def nl(value):
    """把三种换行都归一成 LF：编辑框里可见内容只能是这个形态。"""
    return re.sub(r"\r\n|\r|\n", "\n", value)


def seed_payload():
    return {
        "title": TITLE,
        "description": DESCRIPTION,
        "questions": [
            {"type": "text", "title": Q0_TITLE, "required": True},
            {"type": "single_choice", "title": Q1_TITLE, "required": False,
             "options": [OPT0, OPT1, OPT2, OPT3]},
            {"type": "single_choice", "title": Q2_TITLE, "required": True,
             "options": [Q2_O0, Q2_O1]},
            {"type": "text", "title": Q3_TITLE, "required": False},
        ],
    }


# --------------------------------------------------------------------------
# 真实 app.py 服务进程（与其它浏览器回归测试同款夹具）
# --------------------------------------------------------------------------

class SurveyServer:
    def __init__(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="opensurvey-multiline-")
        self.process = None
        self.base_url = None

    def start(self):
        # app.py 每次请求都会向 stderr 写访问日志；PIPE 不读会写满导致服务阻塞，
        # 因此自行选定端口并把子进程输出直接丢弃。
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
# 注意：这里给 .value 赋的字符串与真实用户输入一样只能含 LF——浏览器在赋值
# 瞬间就会把任何 CR/CRLF 归一成 LF，页面脚本只能靠打开时记下的原稿还原。
TEST_HELPERS = r"""
(() => {
  const T = window.__t = {};
  const fire = el => {
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  T.cards = () => [...document.querySelectorAll('.q-card')];
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
  T.submit = () => document.querySelector('#draft-form button[type=submit]').click();
  T.clickHref = href => {
    const a = [...document.querySelectorAll('a[href]')]
      .find(x => x.getAttribute('href') === href);
    if (!a) throw new Error('找不到链接 ' + href);
    a.click();
  };
  // 标题 textarea 相对单行 textarea 的高度比：多行内容必须真实撑高输入框，
  // 而不能在视觉上被压成一行。
  T.titleHeightRatio = () => {
    const title = document.getElementById('survey-title');
    const one = document.createElement('textarea');
    one.className = 'grow'; one.rows = 1; one.value = 'x';
    document.body.appendChild(one);
    const ratio = title.getBoundingClientRect().height
      / Math.max(1, one.getBoundingClientRect().height);
    one.remove();
    return ratio;
  };
})();
"""

# 编辑表单状态快照——全部取表单当前值（浏览器中只可能含 LF）。
SNAPSHOT_JS = r"""
(() => ({
  hash: location.hash,
  title: document.getElementById('survey-title').value,
  description: document.getElementById('survey-desc').value,
  questions: [...document.querySelectorAll('.q-card')].map(card => ({
    type: card.dataset.type,
    title: card.querySelector('.q-title').value,
    required: card.querySelector('.q-required').checked,
    options: [...card.querySelectorAll('.opt-text')].map(o => o.value),
  })),
}))()
"""

# 详情页结构化读取：题目标题与选项取 textContent（white-space:pre-wrap）。
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

    def prepare(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": TEST_HELPERS})

    def call(self, method, params=None, timeout=15):
        return self.ws.call(method, params, session_id=self.session, timeout=timeout)

    def close(self):
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
                if event.get("method") == "Page.loadEventFired":
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
            time.sleep(0.15)
        raise AssertionError(f"等待页面条件超时：{expression}")

    def settle(self, seconds=0.4):
        """让异步保存、requestAnimationFrame 自适应高度等回调有机会落地。"""
        time.sleep(seconds)

    def snapshot(self):
        return self.eval(SNAPSHOT_JS)

    def detail(self):
        return self.eval(DETAIL_JS)


# --------------------------------------------------------------------------
# 测试本体
# --------------------------------------------------------------------------

class MultilineNewlineSaveTests(unittest.TestCase):
    server = None

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
        if cls.server is not None:
            cls.server.stop()

    def setUp(self):
        # 每个用例独立 Chrome，避免标签页累积拖垮浏览器（与同目录用例同款做法）。
        self.browser = ChromeBrowser()
        self.page = self.browser.new_page()
        # 服务进程在全部用例间共享：记下本用例开始前已存在的编号，保存后只核对
        # “没有额外新增记录”，而不是假设列表里只有本用例这一份。
        self.ids_start = self.survey_ids()

    def tearDown(self):
        self.page.close()
        self.browser.close()

    # ---------- 夹具与断言辅助 ----------

    def api(self, method, path, body=None):
        return self.server.request(method, path, body)

    def seed(self, payload=None):
        """通过接口创建草稿，返回 (编号, 接口回显的完整问卷对象)。"""
        status, data = self.api("POST", "/api/surveys",
                                payload if payload is not None else seed_payload())
        self.assertEqual(status, 201, f"准备草稿失败：{data}")
        return data["id"], data

    def get_survey(self, survey_id):
        status, data = self.api("GET", f"/api/surveys/{survey_id}")
        self.assertEqual(status, 200, data)
        return data

    def survey_ids(self):
        status, data = self.api("GET", "/api/surveys")
        self.assertEqual(status, 200, data)
        return {item["id"] for item in data["surveys"]}

    def open_edit_direct(self, survey_id):
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}/edit")
        self.wait_edit_form()

    def open_edit_from_detail(self, survey_id):
        """首页 → 详情 → “编辑草稿”，全程走真实链接（即题目描述的进入路径）。"""
        self.page.open(f"{self.server.base_url}/#/surveys/{survey_id}")
        self.page.wait_for(
            f"location.hash === '#/surveys/{survey_id}' && "
            "!!document.querySelector('.q-list')")
        self.page.t("clickHref", f"#/surveys/{survey_id}/edit")
        self.wait_edit_form()

    def wait_edit_form(self):
        self.page.wait_for(
            "!!document.querySelector('#draft-form .q-card') && "
            "document.getElementById('survey-title').value !== ''")
        self.page.settle(0.3)

    def save_and_enter_detail(self, survey_id):
        # 本文件不在网络层挂起保存请求：所有改动都发生在点击保存之前，属于这次
        # 提交快照，成功后按既有行为进入同一编号的详情页。
        self.page.t("submit")
        self.page.wait_for(f"location.hash === '#/surveys/{survey_id}'")
        self.page.wait_for("!!document.querySelector('.q-list')")

    def reopen_edit(self, survey_id):
        self.page.t("clickHref", f"#/surveys/{survey_id}/edit")
        self.wait_edit_form()

    def assert_text_fields_exact(self, data, expected, where):
        """逐字段、逐字符核对整份草稿（id、题目/选项次序与必填也核对）。"""
        self.assertEqual(data, expected, f"{where}：落库草稿与期望不完全一致")

    def assert_original_newline_forms(self, value, original, where):
        """逐字符之外再按换行形态计数核对：原稿里的每个 CRLF、单独 CR、单独 LF
        都必须原样存活，不能全部退化成 LF。各字段实际含哪种形态以原稿为准。"""
        def counts(s):
            return (
                len(re.findall(r"\r\n", s)),
                len(re.findall(r"\r(?!\n)", s)),
                len(re.findall(r"(?<!\r)\n", s)),
            )
        self.assertEqual(counts(value), counts(original),
                         f"{where}：CRLF/单独CR/单独LF 的数量与原稿不一致"
                         "（原稿换行形态可能被悄悄改写成 LF）")
        if "\r\n" in original:
            self.assertIn(CRLF, value, f"{where}：原稿的 CRLF 丢失")
        if re.search(r"\r(?!\n)", original):
            self.assertIsNotNone(re.search(r"\r(?!\n)", value),
                                 f"{where}：原稿的单独 CR 丢失")

    def assert_form_shows_original(self, snap, where):
        """编辑框里看到的必须是原稿归一化成 LF 后的完整分行文字。"""
        self.assertEqual(snap["title"], nl(TITLE), f"{where}：标题显示不符")
        self.assertEqual(snap["description"], nl(DESCRIPTION),
                         f"{where}：说明显示不符")
        self.assertEqual(
            [(q["type"], q["title"], q["required"], tuple(q["options"]))
             for q in snap["questions"]],
            [("text", nl(Q0_TITLE), True, ()),
             ("single_choice", nl(Q1_TITLE), False,
              (nl(OPT0), nl(OPT1), nl(OPT2), OPT3)),
             ("single_choice", nl(Q2_TITLE), True, (nl(Q2_O0), nl(Q2_O1))),
             ("text", nl(Q3_TITLE), False, ())],
            f"{where}：题目次序、题型、必填或选项次序/内容显示不符",
        )

    # ---------- 打开编辑页：完整分行文字、行次与全部选项 ----------

    def test_edit_form_shows_all_newline_forms_as_lines_without_collapse_or_blanks(self):
        survey_id, saved = self.seed()
        ids_before = self.survey_ids()

        self.open_edit_from_detail(survey_id)
        snap = self.page.snapshot()

        # 表单值逐字符等于“原稿归一化成 LF”后的内容：既没把内部换行挤成一行，
        # 也没多出空行或首尾换行。
        self.assert_form_shows_original(snap, "打开编辑页")

        # 行次与行数：每种换行都断成同样的行，顺序与原稿 splitlines 一致。
        self.assertEqual(snap["title"].split("\n"), TITLE.splitlines(),
                         "标题行次/行数必须与原稿一致")
        self.assertEqual(snap["description"].split("\n"), DESCRIPTION.splitlines(),
                         "说明行次/行数必须与原稿一致")
        self.assertEqual(snap["questions"][0]["title"].split("\n"),
                         Q0_TITLE.splitlines(), "文本题标题行次/行数必须与原稿一致")
        self.assertEqual(snap["questions"][1]["options"][0].split("\n"),
                         OPT0.splitlines(), "单选选项行次/行数必须与原稿一致")
        # 原稿没有空行：显示里也不能冒出连续换行或首尾空行。覆盖标题、说明、
        # 文本题标题以及含不同换行形式的多个单选选项。
        shown_fields = [
            snap["title"], snap["description"], snap["questions"][0]["title"],
            snap["questions"][1]["title"],
            *snap["questions"][1]["options"],
            snap["questions"][2]["title"], *snap["questions"][2]["options"],
            snap["questions"][3]["title"],
        ]
        for value in shown_fields:
            self.assertNotIn("\n\n", value, "不应额外插入空行")
            self.assertFalse(value.startswith("\n") or value.endswith("\n"),
                             "不应额外插入首尾空行")

        # 题型、必填勾选、题目数与每道单选题的全部选项次序都来自原稿。
        self.assertEqual([q["type"] for q in snap["questions"]],
                         ["text", "single_choice", "single_choice", "text"])
        self.assertEqual([q["required"] for q in snap["questions"]],
                         [True, False, True, False])
        self.assertEqual(
            [q["options"] for q in snap["questions"]],
            [[], [nl(OPT0), nl(OPT1), nl(OPT2), OPT3], [nl(Q2_O0), nl(Q2_O1)], []],
            "单选题必须显示全部选项且次序不变，文本题不带选项",
        )

        # 多行标题真实撑高了输入框（视觉上也不能是一行）。
        self.page.settle(0.3)
        self.assertGreater(self.page.t("titleHeightRatio"), 2.0,
                           "多行标题输入框必须随内容增高")

        # 打开编辑页本身不改数据，也不新增问卷。
        self.assertEqual(self.get_survey(survey_id), saved,
                         "打开编辑页不应改动服务器原稿")
        self.assertEqual(self.survey_ids(), ids_before, "打开编辑页不应新增问卷")

    # ---------- 不改直接保存：原稿逐字符保留，编号不变，可跨代重复保存 ----------

    def test_save_without_edits_keeps_original_crlf_cr_lf_char_for_char(self):
        survey_id, saved = self.seed()
        self.open_edit_direct(survey_id)

        # 第一次：一字不改直接保存，进入同一编号详情。
        self.save_and_enter_detail(survey_id)
        data = self.get_survey(survey_id)
        self.assertEqual(data["id"], survey_id, "保存后问卷编号发生了变化")
        self.assert_text_fields_exact(data, saved, "不改直接保存")
        # 不能只“看着一样”：CRLF 与单独 CR 必须真实存活。
        self.assert_original_newline_forms(data["title"], TITLE, "问卷标题")
        self.assert_original_newline_forms(data["description"], DESCRIPTION, "问卷说明")
        self.assert_original_newline_forms(
            data["questions"][0]["title"], Q0_TITLE, "文本题标题")
        self.assert_original_newline_forms(
            data["questions"][1]["title"], Q1_TITLE, "单选题标题")
        self.assert_original_newline_forms(
            data["questions"][1]["options"][0], OPT0, "单选选项")
        self.assertNotIn(CR, data["questions"][3]["title"],
                         "原本只有 LF 的字段不应凭空多出 CR")
        # 不新增记录：列表里只多出本用例这一份，编号不变。
        self.assertEqual(self.survey_ids(), self.ids_start | {survey_id})

        # 再进同一问卷详情，页面展示的分行文字与原稿各行一致。
        detail = self.page.detail()
        self.assertEqual(detail["hash"], f"#/surveys/{survey_id}")
        self.assertEqual(nl(detail["heading"]), f"#{survey_id} {nl(TITLE)}")
        self.assertEqual(nl(detail["description"]), nl(DESCRIPTION))

        # 从详情再次打开编辑页（全新一代表单），仍一字不改再存一次：原稿换行
        # 形式必须继续保留，证明“记住原稿”不是只在首次进入时有效。
        self.reopen_edit(survey_id)
        self.assert_form_shows_original(self.page.snapshot(), "第二次打开编辑页")
        self.save_and_enter_detail(survey_id)
        self.assert_text_fields_exact(self.get_survey(survey_id), saved,
                                      "第二代编辑页不改再保存")
        self.assertEqual(self.survey_ids(), self.ids_start | {survey_id})

    # ---------- 只改必填勾选：文字字段逐字符保留 ----------

    def test_save_after_only_toggling_required_keeps_all_text_and_order(self):
        survey_id, saved = self.seed()
        self.open_edit_direct(survey_id)

        # 只动必填：第 1 题 必填→选填，第 2 题 选填→必填；文字一律不碰。
        self.page.t("setRequired", 0, False)
        self.page.t("setRequired", 1, True)
        # 必填变化属于这次提交快照，保存成功后正常进入同一编号详情。
        self.save_and_enter_detail(survey_id)

        data = self.get_survey(survey_id)
        self.assertEqual(data["id"], survey_id)
        self.assertEqual(data["title"], saved["title"], "只改必填不得改写标题")
        self.assertEqual(data["description"], saved["description"],
                         "只改必填不得改写说明")
        self.assertEqual(
            [(q["type"], q["title"], tuple(q["options"])) for q in data["questions"]],
            [(q["type"], q["title"], tuple(q["options"]))
             for q in saved["questions"]],
            "只改必填不得改写题目标题/选项或其顺序",
        )
        self.assertEqual([q["required"] for q in data["questions"]],
                         [False, True, True, False], "必填修改必须落库")
        # 文字未动：CRLF/CR 逐字符保留。
        self.assert_original_newline_forms(data["title"], TITLE, "只改必填后的标题")
        self.assert_original_newline_forms(
            data["questions"][1]["options"][0], OPT0, "只改必填后的单选选项")
        self.assertEqual(self.survey_ids(), self.ids_start | {survey_id})

    # ---------- 改过又恢复：四类字段恢复可见文字后仍按原稿换行保存 ----------

    def test_change_then_restore_visible_text_keeps_original_newline_forms(self):
        survey_id, saved = self.seed()
        self.open_edit_direct(survey_id)
        page = self.page

        # 问卷标题、说明、文本题标题、单选选项：都先改成别的，再把可见文字与分行
        # 恢复成打开时的样子（恢复时输入的只能是 LF，与真实用户逐行重输一致）。
        page.t("setTitle", "临时标题\n另一行")
        page.t("setTitle", nl(TITLE))
        page.t("setDesc", "临时说明\n临时第二行")
        page.t("setDesc", nl(DESCRIPTION))
        page.t("setQTitle", 0, "临时文本题\n两行")
        page.t("setQTitle", 0, nl(Q0_TITLE))
        page.t("setOption", 1, 0, "临时选项\n两行")
        page.t("setOption", 1, 0, nl(OPT0))

        # 表单可见内容确已恢复（LF 形态）。
        self.assert_form_shows_original(page.snapshot(), "改过又恢复之后")

        self.save_and_enter_detail(survey_id)
        data = self.get_survey(survey_id)
        # 恢复可见文字不等于接受浏览器的 LF：原稿的 CRLF/CR 必须原样回来。
        self.assert_text_fields_exact(data, saved, "改过又恢复后保存")
        self.assert_original_newline_forms(data["title"], TITLE, "恢复后的标题")
        self.assert_original_newline_forms(data["description"], DESCRIPTION, "恢复后的说明")
        self.assert_original_newline_forms(
            data["questions"][0]["title"], Q0_TITLE, "恢复后的题目标题")
        self.assert_original_newline_forms(
            data["questions"][1]["options"][0], OPT0, "恢复后的选项")
        self.assertEqual(self.survey_ids(), self.ids_start | {survey_id})

    # ---------- 裁剪字段只增删首尾空白：沿用原稿内部换行 ----------

    def test_surrounding_whitespace_on_trim_fields_keeps_internal_original_newlines(self):
        survey_id, saved = self.seed()
        self.open_edit_direct(survey_id)
        page = self.page

        # 给标题、一道题目标题、一个选项只加首尾空白（含换行空白），内部文字与
        # 行次不动。这些字段按既有规则裁剪首尾，比较时视为未改 → 还原原稿。
        page.t("setTitle", "\t " + nl(TITLE) + " \n")
        page.t("setQTitle", 1, "  " + nl(Q1_TITLE) + "\n\t ")
        page.t("setOption", 1, 0, "\n " + nl(OPT0) + "  ")

        self.save_and_enter_detail(survey_id)
        data = self.get_survey(survey_id)
        # 整份逐字符不变：三个被加过首尾空白的字段连内部 CRLF/CR 都沿用原稿。
        self.assert_text_fields_exact(data, saved, "仅增删首尾空白后保存")
        self.assert_original_newline_forms(data["title"], TITLE, "加首尾空白后的标题")
        self.assert_original_newline_forms(
            data["questions"][1]["title"], Q1_TITLE, "加首尾空白后的题目标题")
        self.assert_original_newline_forms(
            data["questions"][1]["options"][0], OPT0, "加首尾空白后的选项")

    # ---------- 说明不裁剪：首尾空白是真实修改，必须保留（内部按 LF） ----------

    def test_surrounding_whitespace_on_description_is_real_change_and_preserved(self):
        survey_id, saved = self.seed()
        self.open_edit_direct(survey_id)

        # 只给说明加首尾空白：说明不裁剪，这是真实修改，保存后必须保留；内部文字
        # 与行次不变，但换行取编辑框里的 LF（不能被旧原稿的 CR 覆盖）。
        new_desc = "   " + nl(DESCRIPTION) + "  "
        self.page.t("setDesc", new_desc)
        self.save_and_enter_detail(survey_id)

        data = self.get_survey(survey_id)
        self.assertEqual(data["description"], new_desc,
                         "说明新增的首尾空白必须原样保留，正文不能盖住这次输入")
        self.assertTrue(data["description"].startswith("   ")
                        and data["description"].endswith("  "),
                         "说明首尾空白不得被裁剪")
        self.assertNotIn(CR, data["description"],
                         "真实修改后说明内部换行应取编辑框的 LF，不得残留旧 CR")
        # 其余字段没动：原稿换行形式逐字符保留。
        self.assertEqual(data["title"], saved["title"])
        self.assertEqual(data["questions"], saved["questions"])

        # 保存后停在同一问卷详情：说明连同首尾空白与各行一起展示。
        detail = self.page.detail()
        self.assertEqual(detail["description"], new_desc,
                         "详情页必须展示修改后的说明")

        # 再次打开编辑页：显示已保存的说明；这一代上不改再存，结果保持不变。
        self.reopen_edit(survey_id)
        self.assertEqual(self.page.snapshot()["description"], new_desc,
                         "再次打开编辑页必须显示已保存的说明")
        self.save_and_enter_detail(survey_id)
        self.assertEqual(self.get_survey(survey_id)["description"], new_desc)
        self.assertEqual(self.survey_ids(), self.ids_start | {survey_id})

    # ---------- 真实多行修改：改谁存谁（LF），未改字段保留各自原换行 ----------

    def test_real_multiline_edit_saves_lf_and_leaves_other_fields_untouched(self):
        survey_id, saved = self.seed()
        self.open_edit_direct(survey_id)

        # 只改两处多行文字：第 1 题题目标题重排为三行，第 2 题第 2 个选项改写。
        new_q0 = "必填文本题\n新第二行\n新增第三行"
        new_opt1 = "选项乙\n乙的新内容"
        self.page.t("setQTitle", 0, new_q0)
        self.page.t("setOption", 1, 1, new_opt1)
        self.save_and_enter_detail(survey_id)

        data = self.get_survey(survey_id)
        self.assertEqual(data["id"], survey_id)
        # 被改字段：保存编辑框当前内容，只能含 LF，且不能被旧原稿覆盖。
        self.assertEqual(data["questions"][0]["title"], new_q0,
                         "真实修改的题目标题必须按编辑框内容保存")
        self.assertNotIn(CR, data["questions"][0]["title"],
                         "编辑框内容的换行必须是 LF")
        self.assertEqual(data["questions"][1]["options"][1], new_opt1,
                         "真实修改的选项必须按编辑框内容保存")
        self.assertNotIn(CR, data["questions"][1]["options"][1])

        # 其余一切照旧：标题、说明、题目/选项次序、必填、同题其它选项与其余题目
        # 的内部 CRLF/CR 全部逐字符保留（仅把被改的两个位置替换成新值后整体比对）。
        expected = json.loads(json.dumps(saved))   # 深拷贝，避免改动原稿常量
        expected["questions"][0]["title"] = new_q0
        expected["questions"][1]["options"][1] = new_opt1
        self.assertEqual(data["title"], expected["title"])
        self.assertEqual(data["description"], expected["description"])
        self.assertEqual(data["questions"], expected["questions"],
                         "除被改的两处多行文字外，其余字段/次序/必填必须逐字符不变")
        self.assertEqual(data["questions"][1]["title"], Q1_TITLE)
        self.assert_original_newline_forms(
            data["questions"][1]["options"][0], OPT0, "同题未改的选项")
        self.assertEqual(data["questions"][1]["options"][2], OPT2)
        self.assertEqual(data["questions"][1]["options"][3], OPT3)

        # 保存成功后停在同一问卷详情：展示修改后的文字（被改字段按 LF 分行）。
        detail = self.page.detail()
        self.assertEqual(nl(detail["heading"]), f"#{survey_id} {nl(TITLE)}")
        self.assertIn(new_q0, detail["questions"][0]["line"],
                      "详情页必须展示修改后的题目标题分行")
        self.assertEqual(detail["questions"][1]["options"][1], new_opt1,
                         "详情页必须展示修改后的选项")
        self.assertEqual(nl(detail["questions"][1]["options"][0]), nl(OPT0),
                         "详情页同题未改选项仍按原稿分行展示")

        # 再次打开编辑页：被改字段显示已保存的 LF 内容，未改字段仍是原稿分行。
        self.reopen_edit(survey_id)
        snap = self.page.snapshot()
        self.assertEqual(snap["questions"][0]["title"], new_q0,
                         "再次打开编辑页必须显示已保存的题目标题")
        self.assertEqual(snap["questions"][1]["options"][1], new_opt1,
                         "再次打开编辑页必须显示已保存的选项")
        self.assertEqual(snap["title"], nl(TITLE))
        self.assertEqual(snap["questions"][1]["options"][0], nl(OPT0))

        # 在这一代编辑页上不再改动直接保存：结果应保持不变（被改字段维持 LF，
        # 未改字段仍保留各自的 CR/CRLF），证明三种情形可同时并存、互不串写。
        self.save_and_enter_detail(survey_id)
        again = self.get_survey(survey_id)
        self.assertEqual(again, data, "新一代编辑页原样再存后结果发生了漂移")
        self.assertEqual(self.survey_ids(), self.ids_start | {survey_id})


if __name__ == "__main__":
    unittest.main(verbosity=2)
