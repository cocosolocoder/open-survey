#!/usr/bin/env python3
"""OpenSurvey HTTP service."""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import signal
import sqlite3
from urllib.parse import urlsplit

PRODUCT = "OpenSurvey"
RESOURCE = "surveys"

INDEX_PAGE = '''<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenSurvey · 问卷调查与反馈管理</title>
<style>
body{font-family:system-ui,sans-serif;max-width:52rem;margin:3rem auto;padding:0 1rem;line-height:1.7;color:#222}
a{color:#175b9c}
h2{margin-top:2rem;border-bottom:1px solid #eee;padding-bottom:.3rem}
ul{padding-left:1.4rem}
form{border:1px solid #ddd;border-radius:8px;padding:1rem 1.25rem;margin:1rem 0;background:#fdfdfd}
label{display:block;margin:.6rem 0 .2rem;font-weight:600}
input[type=text],textarea{width:100%;padding:.45rem .55rem;border:1px solid #ccc;border-radius:4px;box-sizing:border-box;font:inherit;background:#fff}
textarea{min-height:5rem;resize:vertical}
.q{border:1px solid #e2e2e2;border-radius:6px;padding:.6rem .75rem;margin:.75rem 0;background:#fafafa}
.qhead{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.qhead input[type=text]{flex:1;min-width:12rem}
.qhead label{margin:0;font-weight:400;white-space:nowrap}
.opts{margin:.5rem 0 0 1.25rem}
.opts>div{display:flex;gap:.5rem;align-items:center;margin:.25rem 0}
.opts input[type=text]{flex:1}
button{font:inherit;padding:.35rem .8rem;border:1px solid #175b9c;background:#175b9c;color:#fff;border-radius:4px;cursor:pointer}
button.secondary{background:#fff;color:#175b9c}
button.danger{background:#fff;color:#b00020;border-color:#b00020;padding:.2rem .55rem}
.errors{background:#fdecea;border:1px solid #f5c6c6;border-radius:6px;padding:.6rem .9rem;margin:1rem 0;color:#b00020}
.success{background:#e8f5e9;border:1px solid #c8e6c9;border-radius:6px;padding:.75rem 1rem;margin:1rem 0}
.req{color:#b00020;font-size:.85em}
ol.questions{padding-left:1.5rem}
.desc{white-space:pre-wrap;color:#555}
.hint{color:#777;font-size:.9em}
</style>
<main>
<h1>OpenSurvey</h1>
<p>问卷调查与反馈管理</p>

<h2>问卷列表</h2>
<ul id="list"><li>加载中…</li></ul>

<h2>新建问卷草稿</h2>
<form id="survey-form" onsubmit="return false">
  <label for="title">标题</label>
  <input type="text" id="title" placeholder="问卷标题，必填">
  <label for="description">说明</label>
  <textarea id="description" placeholder="问卷说明，可留空"></textarea>
  <div id="questions"></div>
  <p>
    <button type="button" class="secondary" onclick="addQuestion('text')">添加文本题</button>
    <button type="button" class="secondary" onclick="addQuestion('single_choice')">添加单选题</button>
  </p>
  <div id="errors"></div>
  <p><button type="button" onclick="save()">保存问卷</button></p>
</form>
<div id="success"></div>

<p><a href="/api/surveys">查看问卷列表接口</a> · <a href="/health">服务状态</a></p>
</main>
<script>
let questions = [];

function esc(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}

async function loadList(){
  const ul = document.getElementById("list");
  try{
    const r = await fetch("/api/surveys");
    const data = await r.json();
    ul.innerHTML = "";
    if(!data.surveys || data.surveys.length === 0){
      ul.innerHTML = "<li>还没有问卷记录。</li>";
      return;
    }
    for(const s of data.surveys){
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "/surveys/" + s.id;
      a.textContent = "#" + s.id + " " + s.title;
      li.appendChild(a);
      ul.appendChild(li);
    }
  }catch(e){
    ul.innerHTML = "<li>加载失败，请稍后重试。</li>";
  }
}

function addQuestion(type){
  questions.push({type: type, title: "", required: false, options: type === "single_choice" ? ["", ""] : null});
  renderQuestions();
}

function removeQuestion(i){
  questions.splice(i, 1);
  renderQuestions();
}

function addOption(i){
  questions[i].options.push("");
  renderQuestions();
}

function removeOption(i, j){
  questions[i].options.splice(j, 1);
  renderQuestions();
}

function renderQuestions(){
  const box = document.getElementById("questions");
  box.innerHTML = "";
  if(questions.length === 0){
    box.innerHTML = '<p class="hint">还没有题目，点击下方按钮添加。</p>';
    return;
  }
  questions.forEach(function(q, i){
    const div = document.createElement("div");
    div.className = "q";
    const typeName = q.type === "text" ? "文本题" : "单选题";
    let html = '<div class="qhead"><strong>' + (i + 1) + '. ' + typeName + '</strong>';
    html += '<input type="text" placeholder="题目，必填" value="' + esc(q.title) + '" oninput="questions[' + i + '].title=this.value">';
    html += '<label><input type="checkbox" ' + (q.required ? "checked" : "") + ' onchange="questions[' + i + '].required=this.checked"> 必填</label>';
    html += '<button type="button" class="danger" onclick="removeQuestion(' + i + ')">删除</button></div>';
    if(q.type === "single_choice"){
      html += '<div class="opts">';
      q.options.forEach(function(o, j){
        html += '<div><input type="text" placeholder="选项 ' + (j + 1) + '" value="' + esc(o) + '" oninput="questions[' + i + '].options[' + j + ']=this.value">';
        html += '<button type="button" class="danger" onclick="removeOption(' + i + ',' + j + ')">删除</button></div>';
      });
      html += '<div><button type="button" class="secondary" onclick="addOption(' + i + ')">添加选项</button></div>';
      html += '</div>';
    }
    div.innerHTML = html;
    box.appendChild(div);
  });
}

function showErrors(errs){
  document.getElementById("errors").innerHTML =
    '<div class="errors"><strong>保存失败，请修正后重新保存：</strong><ul>' +
    errs.map(function(e){ return '<li>' + esc(e) + '</li>'; }).join("") +
    '</ul></div>';
}

function renderSurvey(s){
  let html = '<h3>#' + s.id + ' ' + esc(s.title) + '</h3>';
  if(s.description){
    html += '<p class="desc">' + esc(s.description) + '</p>';
  }
  html += '<ol class="questions">';
  for(const q of s.questions){
    html += '<li><div>' + esc(q.title) + (q.required ? ' <span class="req">（必填）</span>' : '') + '</div>';
    if(q.type === "single_choice"){
      html += '<ul>' + q.options.map(function(o){ return '<li>' + esc(o) + '</li>'; }).join("") + '</ul>';
    }
    html += '</li>';
  }
  html += '</ol>';
  return html;
}

async function save(){
  document.getElementById("errors").innerHTML = "";
  document.getElementById("success").innerHTML = "";
  const payload = {
    title: document.getElementById("title").value,
    description: document.getElementById("description").value,
    questions: questions.map(function(q){
      const o = {type: q.type, title: q.title, required: !!q.required};
      if(q.type === "single_choice"){
        o.options = q.options;
      }
      return o;
    })
  };
  let r, data;
  try{
    r = await fetch("/api/surveys", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    data = await r.json();
  }catch(e){
    showErrors(["网络错误，保存失败，请稍后重试。"]);
    return;
  }
  if(r.status === 201){
    document.getElementById("success").innerHTML =
      '<div class="success"><strong>保存成功。</strong>' + renderSurvey(data) +
      '<p><a href="/surveys/' + data.id + '">打开详情页</a></p></div>';
    document.getElementById("survey-form").reset();
    questions = [];
    renderQuestions();
    loadList();
  }else{
    const errs = (data.errors && data.errors.length) ? data.errors : [data.error || "保存失败"];
    showErrors(errs);
  }
}

loadList();
</script>
</main></html>'''

DETAIL_PAGE = '''<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>问卷详情 · OpenSurvey</title>
<style>
body{font-family:system-ui,sans-serif;max-width:52rem;margin:3rem auto;padding:0 1rem;line-height:1.7;color:#222}
a{color:#175b9c}
.req{color:#b00020;font-size:.85em}
.desc{white-space:pre-wrap;color:#555}
</style>
<main>
<p><a href="/">← 返回首页</a></p>
<div id="app">加载中…</div>
</main>
<script>
function esc(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function renderSurvey(s){
  let html = '<h1>#' + s.id + ' ' + esc(s.title) + '</h1>';
  if(s.description){
    html += '<p class="desc">' + esc(s.description) + '</p>';
  }
  if(!s.questions || s.questions.length === 0){
    html += '<p>该问卷还没有题目。</p>';
    return html;
  }
  html += '<ol>';
  for(const q of s.questions){
    html += '<li><div>' + esc(q.title) + (q.required ? ' <span class="req">（必填）</span>' : '') + '</div>';
    if(q.type === "single_choice"){
      html += '<ul>' + q.options.map(function(o){ return '<li>' + esc(o) + '</li>'; }).join("") + '</ul>';
    }
    html += '</li>';
  }
  html += '</ol>';
  return html;
}
const parts = location.pathname.split("/").filter(Boolean);
const id = parts[parts.length - 1];
fetch("/api/surveys/" + encodeURIComponent(id))
  .then(function(r){ return r.json().then(function(data){ return {status: r.status, data: data}; }); })
  .then(function(res){
    if(res.status === 404){
      document.getElementById("app").innerHTML = "<p>问卷不存在或已被删除。</p>";
      document.title = "问卷不存在 · OpenSurvey";
      return;
    }
    document.getElementById("app").innerHTML = renderSurvey(res.data);
    document.title = "#" + res.data.id + " " + res.data.title + " · OpenSurvey";
  })
  .catch(function(){
    document.getElementById("app").innerHTML = "<p>加载失败，请稍后重试。</p>";
  });
</script>
</html>'''

NOT_FOUND_PAGE = '''<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>页面不存在 · OpenSurvey</title>
<style>body{font-family:system-ui,sans-serif;max-width:52rem;margin:3rem auto;padding:0 1rem;line-height:1.7}a{color:#175b9c}</style>
<main><h1>404</h1><p>页面不存在。</p><p><a href="/">返回首页</a></p></main>'''

METHOD_ALLOWED = {
    "/": "GET",
    "/health": "GET",
    "/api/surveys": "GET, POST",
}


def validate_survey(payload):
    """Return a list of validation error messages (with field paths)."""
    errors = []
    if not isinstance(payload, dict):
        return ["请求体必须是 JSON 对象"]
    title = payload.get("title")
    if not isinstance(title, str):
        errors.append("title 必须是字符串")
    elif not title.strip():
        errors.append("title 去掉首尾空白后不能为空")
    if "description" in payload and not isinstance(payload.get("description"), str):
        errors.append("description 必须是字符串")
    questions = payload.get("questions")
    if not isinstance(questions, list):
        errors.append("questions 必须是数组")
        return errors
    if len(questions) == 0:
        errors.append("questions 至少需要一道题")
    for i, question in enumerate(questions):
        path = "questions[%d]" % i
        if not isinstance(question, dict):
            errors.append("%s 必须是对象" % path)
            continue
        qtype = question.get("type")
        if qtype not in ("text", "single_choice"):
            errors.append("%s.type 必须是 text 或 single_choice" % path)
        qtitle = question.get("title")
        if not isinstance(qtitle, str):
            errors.append("%s.title 必须是字符串" % path)
        elif not qtitle.strip():
            errors.append("%s.title 去掉首尾空白后不能为空" % path)
        if "required" in question and not isinstance(question.get("required"), bool):
            errors.append("%s.required 必须是布尔值" % path)
        if qtype == "text":
            options = question.get("options")
            if options:
                errors.append("%s 是文本题，不能包含选项" % path)
        elif qtype == "single_choice":
            options = question.get("options")
            if not isinstance(options, list):
                errors.append("%s.options 必须是字符串数组" % path)
                continue
            if len(options) < 2:
                errors.append("%s.options 至少需要两个选项" % path)
            seen = set()
            for j, option in enumerate(options):
                opath = "%s.options[%d]" % (path, j)
                if not isinstance(option, str):
                    errors.append("%s 必须是字符串" % opath)
                    continue
                stripped = option.strip()
                if not stripped:
                    errors.append("%s 去掉首尾空白后不能为空" % opath)
                elif stripped in seen:
                    errors.append("%s 与同一题内的其他选项重复" % opath)
                else:
                    seen.add(stripped)
    return errors


def main():
    parser = argparse.ArgumentParser(description="OpenSurvey - 问卷调查与反馈管理")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the HTTP service")
    serve.add_argument("--host", default="127.0.0.1", help="Address to bind (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8080, help="Port to bind; 0 selects an available port")
    serve.add_argument("--data-dir", type=Path, default=Path("data"), help="Directory for the local SQLite database")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(args.data_dir / "open-survey.sqlite")
    database.execute("CREATE TABLE IF NOT EXISTS surveys (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    columns = [row[1] for row in database.execute("PRAGMA table_info(surveys)")]
    if "description" not in columns:
        database.execute("ALTER TABLE surveys ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    database.execute(
        "CREATE TABLE IF NOT EXISTS questions ("
        "id INTEGER PRIMARY KEY, survey_id INTEGER NOT NULL, position INTEGER NOT NULL, "
        "type TEXT NOT NULL, title TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 0)")
    database.execute(
        "CREATE TABLE IF NOT EXISTS options ("
        "id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, position INTEGER NOT NULL, value TEXT NOT NULL)")
    database.commit()

    def get_survey(survey_id):
        row = database.execute(
            "SELECT id, title, description FROM surveys WHERE id = ?", (survey_id,)).fetchone()
        if row is None:
            return None
        question_rows = database.execute(
            "SELECT id, type, title, required FROM questions WHERE survey_id = ? ORDER BY position",
            (survey_id,)).fetchall()
        questions = []
        for question_id, qtype, qtitle, required in question_rows:
            item = {"type": qtype, "title": qtitle, "required": bool(required)}
            if qtype == "single_choice":
                item["options"] = [
                    row[0] for row in database.execute(
                        "SELECT value FROM options WHERE question_id = ? ORDER BY position",
                        (question_id,))]
            questions.append(item)
        return {"id": row[0], "title": row[1], "description": row[2], "questions": questions}

    def create_survey(payload):
        title = payload["title"].strip()
        description = payload.get("description", "")
        cursor = database.execute(
            "INSERT INTO surveys (title, description) VALUES (?, ?)", (title, description))
        survey_id = cursor.lastrowid
        for position, question in enumerate(payload["questions"]):
            qtype = question["type"]
            qtitle = question["title"].strip()
            required = 1 if question.get("required", False) else 0
            cursor = database.execute(
                "INSERT INTO questions (survey_id, position, type, title, required) VALUES (?, ?, ?, ?, ?)",
                (survey_id, position, qtype, qtitle, required))
            question_id = cursor.lastrowid
            if qtype == "single_choice":
                for option_position, option in enumerate(question["options"]):
                    database.execute(
                        "INSERT INTO options (question_id, position, value) VALUES (?, ?, ?)",
                        (question_id, option_position, option.strip()))
        database.commit()
        return get_survey(survey_id)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, value, *, html=False, allow=None):
            payload = value.encode("utf8") if html else json.dumps(value, ensure_ascii=False).encode("utf8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if status == 405:
                self.send_header("Allow", allow or "GET")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def respond_405(self, allow):
            self.respond(405, {"error": "method not allowed"}, allow=allow)

        def handle_create(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                self.respond(400, {"error": "请求体不是合法的 JSON"})
                return
            if not isinstance(payload, dict):
                self.respond(400, {"error": "请求体必须是 JSON 对象"})
                return
            errors = validate_survey(payload)
            if errors:
                self.respond(400, {"error": "问卷内容不合法", "errors": errors})
                return
            try:
                survey = create_survey(payload)
            except sqlite3.Error:
                database.rollback()
                self.respond(500, {"error": "保存失败，请稍后重试"})
                return
            self.respond(201, survey)

        def route(self):
            location = urlsplit(self.path).path
            if location == "/":
                if self.command != "GET":
                    self.respond_405(METHOD_ALLOWED["/"])
                    return
                self.respond(200, INDEX_PAGE, html=True)
            elif location == "/health":
                if self.command != "GET":
                    self.respond_405(METHOD_ALLOWED["/health"])
                    return
                self.respond(200, {"status": "ok", "product": PRODUCT})
            elif location == "/api/surveys":
                if self.command == "GET":
                    records = [
                        {"id": row[0], "title": row[1]}
                        for row in database.execute("SELECT id, title FROM surveys ORDER BY id")]
                    self.respond(200, {RESOURCE: records})
                elif self.command == "POST":
                    self.handle_create()
                else:
                    self.respond_405(METHOD_ALLOWED["/api/surveys"])
            elif re.fullmatch(r"/api/surveys/\d+", location):
                if self.command != "GET":
                    self.respond_405("GET")
                    return
                survey_id = int(location.rsplit("/", 1)[1])
                survey = get_survey(survey_id)
                if survey is None:
                    self.respond(404, {"error": "问卷不存在"})
                else:
                    self.respond(200, survey)
            elif re.fullmatch(r"/surveys/\d+", location):
                if self.command != "GET":
                    self.respond_405("GET")
                    return
                self.respond(200, DETAIL_PAGE, html=True)
            else:
                self.respond(404, {"error": "not found"})

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = route

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    server = None
    try:
        server = HTTPServer((args.host, args.port), Handler)
        host, port = server.server_address[:2]
        address = "[%s]" % host if ":" in host else host
        print("%s listening on http://%s:%s" % (PRODUCT, address, port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        database.close()


if __name__ == "__main__":
    main()
