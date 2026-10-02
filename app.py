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
MAX_BODY_BYTES = 10 * 1024 * 1024
QUESTION_TYPES = ("text", "single_choice")

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenSurvey · 问卷调查与反馈管理</title>
<style>
:root{--blue:#175b9c;--red:#c0392b;--grey:#666;--line:#d8dce3}
*{box-sizing:border-box}
body{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;max-width:52rem;margin:2.5rem auto;padding:0 1rem;line-height:1.7;color:#1f2430}
a{color:var(--blue)}
h1{margin-bottom:.2rem}
h2{margin-top:2.2rem;border-bottom:1px solid var(--line);padding-bottom:.3rem}
.muted{color:var(--grey);font-size:.92rem}
ul.plain{list-style:none;padding-left:0}
ul.plain li{padding:.25rem 0;border-bottom:1px dashed var(--line)}
form label{display:block;margin:1rem 0 .25rem;font-weight:600}
input[type=text],textarea,input.opt-text,input.q-title{width:100%;padding:.5rem .6rem;border:1px solid var(--line);border-radius:.35rem;font:inherit}
textarea{resize:vertical}
.card{border:1px solid var(--line);border-radius:.5rem;padding:1rem 1.2rem;margin:1rem 0;background:#fafbfd}
.q-head{display:flex;align-items:center;gap:.7rem;flex-wrap:wrap;margin-bottom:.5rem}
.q-index{font-weight:700}
.tag{font-size:.8rem;background:#e8f0fa;color:var(--blue);border-radius:1rem;padding:.05rem .6rem}
.tag.required{background:#fdecea;color:var(--red)}
.req{margin:0;font-weight:400;display:flex;align-items:center;gap:.3rem}
.spacer{flex:1}
button{font:inherit;cursor:pointer}
.btn{border:1px solid var(--blue);background:var(--blue);color:#fff;border-radius:.35rem;padding:.45rem 1.1rem;text-decoration:none;display:inline-block}
.btn.secondary{background:#fff;color:var(--blue)}
.link{border:none;background:none;color:var(--blue);padding:0;font-size:.92rem}
.link.danger{color:var(--red)}
.row-actions{display:flex;gap:.8rem;flex-wrap:wrap;margin-top:1.2rem}
.opt-row{display:flex;align-items:center;gap:.6rem;margin:.4rem 0}
.opt-index{color:var(--grey);font-size:.9rem;min-width:3.2rem}
.banner{border:1px solid var(--red);background:#fdf3f2;color:var(--red);border-radius:.5rem;padding:.7rem 1rem;margin:1rem 0;white-space:normal}
.banner ul{margin:.4rem 0 0;padding-left:1.2rem}
.banner button{color:var(--red);text-decoration:underline;background:none;border:none;padding:0;text-align:left}
.field-err{color:var(--red);font-size:.85rem;margin:.25rem 0 0;min-height:1px}
.invalid{border-color:var(--red)!important;background:#fdf6f5}
.detail-meta{color:var(--grey);font-size:.9rem}
.detail-desc,.opt-text-display{white-space:pre-wrap}
.q-list>li{margin:1rem 0}
.q-list .q-line{font-weight:600}
.q-list ol{margin:.4rem 0 0 1.4rem}
</style>
<main>
<h1>OpenSurvey</h1>
<p>问卷调查与反馈管理</p>
<div id="app"></div>
<p class="muted"><a href="/api/surveys">问卷列表接口</a> · <a href="/health">服务状态</a></p>
</main>
<script>
"use strict";
const app = document.getElementById("app");

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

/* ---------- 问卷表单（新建 / 编辑共用） ---------- */

function buildSurveyForm(existing, hooks) {
  const mode = existing ? "edit" : "create";

  const banner = h("div", {class: "banner", id: "form-banner"});
  banner.hidden = true;

  const titleInput = h("input", {type: "text", id: "survey-title", maxlength: "200", placeholder: "请输入问卷标题"});
  const titleErr = h("p", {class: "field-err", id: "title-err"});
  const descArea = h("textarea", {id: "survey-desc", rows: "4", placeholder: "问卷说明（可留空，支持中文、引号与换行）"});
  const qBox = h("div", {id: "questions"});
  const qErr = h("p", {class: "field-err", id: "questions-err"});

  function renumber() {
    const cards = qBox.querySelectorAll(".q-card");
    cards.forEach((card, i) => {
      card.querySelector(".q-index").textContent = `第 ${i + 1} 题`;
      card.querySelectorAll(".opt-row").forEach((row, j) => {
        row.querySelector(".opt-index").textContent = `选项 ${j + 1}`;
      });
    });
  }

  function optionRow(value) {
    const text = h("input", {type: "text", class: "opt-text", placeholder: "选项内容"});
    if (value !== undefined && value !== null) text.value = value;
    const row = h("div", {class: "opt-row"},
      h("span", {class: "opt-index"}, "选项"),
      text,
      h("button", {type: "button", class: "link danger", onclick: () => { row.remove(); renumber(); }}, "删除")
    );
    return row;
  }

  function addQuestion(type, prefill) {
    const isChoice = type === "single_choice";
    const options = h("div", {class: "options"});
    const card = h("div", {class: "card q-card", "data-type": type},
      h("div", {class: "q-head"},
        h("span", {class: "q-index"}, "第 N 题"),
        h("span", {class: "tag q-type-tag"}, isChoice ? "单选题" : "文本题"),
        h("label", {class: "req"},
          h("input", {type: "checkbox", class: "q-required"}), "必填（默认非必填）"),
        h("span", {class: "spacer"}),
        h("button", {type: "button", class: "link danger", onclick: () => { card.remove(); renumber(); }}, "删除本题")
      ),
      h("input", {type: "text", class: "q-title", placeholder: "题目标题"}),
      h("p", {class: "field-err q-title-err"})
    );
    if (isChoice) {
      const saved = prefill && Array.isArray(prefill.options) ? prefill.options : null;
      if (saved && saved.length) saved.forEach(value => options.append(optionRow(value)));
      else options.append(optionRow(), optionRow());
      card.append(
        options,
        h("p", {class: "field-err opt-err"}),
        h("button", {type: "button", class: "link add-opt"}, "添加选项")
      );
      card.querySelector(".add-opt").addEventListener("click", () => {
        options.append(optionRow());
        renumber();
        options.querySelector(".opt-row:last-child .opt-text").focus();
      });
    }
    if (prefill) {
      card.querySelector(".q-title").value = prefill.title != null ? prefill.title : "";
      card.querySelector(".q-required").checked = !!prefill.required;
    }
    qBox.append(card);
    renumber();
    if (!prefill) card.querySelector(".q-title").focus();
    return card;
  }

  function clearErrors() {
    banner.hidden = true;
    banner.replaceChildren();
    titleErr.textContent = "";
    qErr.textContent = "";
    qBox.querySelectorAll(".invalid").forEach(el => el.classList.remove("invalid"));
    qBox.querySelectorAll(".field-err").forEach(el => { el.textContent = ""; });
  }

  function showBanner(items) {
    banner.replaceChildren(h("strong", null, "问卷未能保存，请修改后重试："), h("ul", null,
      items.map(it => h("li", null, h("button", {type: "button", onclick: () => it.el && it.el.focus()}, it.msg)))));
    banner.hidden = false;
    banner.scrollIntoView({behavior: "smooth", block: "nearest"});
  }

  const actionButtons = [
    h("button", {type: "button", class: "btn secondary", onclick: () => addQuestion("text")}, "添加文本题"),
    h("button", {type: "button", class: "btn secondary", onclick: () => addQuestion("single_choice")}, "添加单选题"),
    h("span", {class: "spacer"})
  ];
  if (mode === "edit") {
    // 离开编辑页即丢弃全部未保存的增删与输入
    actionButtons.push(h("a", {class: "btn secondary", href: `#/surveys/${existing.id}`}, "取消"));
  }
  actionButtons.push(h("button", {type: "submit", class: "btn"},
    mode === "edit" ? "保存修改" : "保存整份问卷"));

  const form = h("form", {id: "draft-form", onsubmit: submitForm},
    banner,
    h("section", null,
      h("h2", null, mode === "edit" ? `编辑问卷草稿 #${existing.id}` : "新建问卷草稿"),
      mode === "edit"
        ? h("p", {class: "muted"}, "保存将整份替换当前草稿；取消则放弃本次修改，问卷编号和地址不变。")
        : null,
      h("label", {for: "survey-title"}, "标题"),
      titleInput, titleErr,
      h("label", {for: "survey-desc"}, "说明"),
      descArea,
      h("label", null, "题目（按下方顺序保存，至少保留一道题）"),
      qBox, qErr,
      h("div", {class: "row-actions"}, actionButtons)
    )
  );

  if (mode === "edit") {
    titleInput.value = existing.title != null ? existing.title : "";
    descArea.value = existing.description || "";
    existing.questions.forEach(question => addQuestion(question.type, question));
    renumber();
  }

  async function submitForm(event) {
    event.preventDefault();
    clearErrors();
    const problems = [];

    const title = titleInput.value.trim();
    if (!title) {
      titleInput.classList.add("invalid");
      titleErr.textContent = "标题不能为空。";
      problems.push({el: titleInput, msg: "问卷标题不能为空。"});
    }

    const cards = [...qBox.querySelectorAll(".q-card")];
    if (cards.length === 0) {
      qErr.textContent = "问卷至少需要保留一道题。";
      problems.push({el: qBox, msg: "问卷至少需要保留一道题，请先添加题目。"});
    }

    const payloadQuestions = [];
    cards.forEach((card, i) => {
      const loc = `第 ${i + 1} 题`;
      const titleEl = card.querySelector(".q-title");
      const qTitle = titleEl.value.trim();
      if (!qTitle) {
        titleEl.classList.add("invalid");
        card.querySelector(".q-title-err").textContent = `${loc}：题目标题不能为空。`;
        problems.push({el: titleEl, msg: `${loc}：题目标题不能为空。`});
      }
      const q = {
        type: card.dataset.type,
        title: qTitle,
        required: card.querySelector(".q-required").checked
      };
      if (card.dataset.type === "single_choice") {
        const optInputs = [...card.querySelectorAll(".opt-text")];
        const values = optInputs.map(el => el.value.trim());
        const optErr = card.querySelector(".opt-err");
        const seen = new Set();
        values.forEach((val, j) => {
          if (!val) {
            optInputs[j].classList.add("invalid");
            optErr.textContent = `${loc}：第 ${j + 1} 个选项不能为空。`;
            problems.push({el: optInputs[j], msg: `${loc}：第 ${j + 1} 个选项不能为空。`});
          } else if (seen.has(val)) {
            optInputs[j].classList.add("invalid");
            optErr.textContent = `${loc}：选项“${val}”与同题其他选项重复。`;
            problems.push({el: optInputs[j], msg: `${loc}：选项“${val}”重复。`});
          }
          seen.add(val);
        });
        if (values.length < 2) {
          optErr.textContent = `${loc}：单选题至少需要两个选项。`;
          problems.push({el: card.querySelector(".add-opt"), msg: `${loc}：单选题至少需要两个选项。`});
        }
        q.options = values;
      }
      payloadQuestions.push(q);
    });

    if (problems.length) {
      showBanner(problems);
      return;
    }

    const payload = {title, description: descArea.value, questions: payloadQuestions};
    const endpoint = mode === "edit" ? `/api/surveys/${existing.id}` : "/api/surveys";
    let resp;
    try {
      resp = await fetch(endpoint, {
        method: mode === "edit" ? "PUT" : "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
    } catch (err) {
      // 请求未成功：保留当前全部输入与增删结果
      showBanner([{el: null, msg: `网络错误，问卷尚未保存：${err}`}]);
      return;
    }
    let data = {};
    try { data = await resp.json(); } catch (_) { /* 保留已输入内容 */ }
    const saved = mode === "edit" ? resp.status === 200 : resp.status === 201;
    if (saved && data.id !== undefined) {
      if (mode === "create") {
        form.reset();
        qBox.replaceChildren();
        if (hooks && hooks.onCreated) hooks.onCreated();
      }
      location.hash = `#/surveys/${data.id}`;
      return;
    }
    // 服务端未确认保存成功：不清空任何输入，展示具体问题
    showBanner([{el: null, msg: data.error || `保存失败（HTTP ${resp.status}），问卷未保存，请检查后重试。`}]);
  }

  return form;
}

/* ---------- 首页 ---------- */

function renderHome() {
  app.replaceChildren();

  const listSection = h("section", null,
    h("h2", null, "问卷列表"),
    h("ul", {class: "plain", id: "survey-list"}, h("li", {class: "muted"}, "加载中…"))
  );
  const listEl = listSection.querySelector("#survey-list");

  async function loadSurveys() {
    let data;
    try {
      const resp = await fetch("/api/surveys");
      data = await resp.json();
    } catch (err) {
      listEl.replaceChildren(h("li", {class: "muted"}, "问卷列表加载失败。"));
      return;
    }
    listEl.replaceChildren();
    if (!data.surveys.length) {
      listEl.append(h("li", {class: "muted"}, "还没有问卷记录。"));
      return;
    }
    for (const survey of data.surveys) {
      listEl.append(h("li", null,
        h("a", {href: `#/surveys/${survey.id}`}, `#${survey.id} ${survey.title}`)));
    }
  }

  app.append(
    listSection,
    buildSurveyForm(null, {onCreated: loadSurveys})
  );
  loadSurveys();
}

/* ---------- 问卷详情 ---------- */

async function renderDetail(id) {
  app.replaceChildren(
    h("p", null, h("a", {href: "#/"}, "← 返回首页")),
    h("p", {class: "muted", id: "detail-status"}, "加载中…")
  );
  const status = document.getElementById("detail-status");

  let survey;
  try {
    const resp = await fetch(`/api/surveys/${id}`);
    if (resp.status === 404) {
      status.textContent = `问卷 #${id} 不存在。`;
      return;
    }
    if (!resp.ok) {
      status.textContent = `详情加载失败（HTTP ${resp.status}），请稍后重试。`;
      return;
    }
    survey = await resp.json();
  } catch (err) {
    status.textContent = `详情加载失败：${err}`;
    return;
  }

  app.replaceChildren(
    h("p", null, h("a", {href: "#/"}, "← 返回首页")),
    h("h2", null, `#${survey.id} ${survey.title}`),
    h("div", {class: "row-actions"},
      h("a", {class: "btn", href: `#/surveys/${survey.id}/edit`}, "编辑草稿"),
      h("a", {class: "btn secondary", href: "#/"}, "返回首页")
    ),
    h("h3", null, "说明"),
    survey.description
      ? h("p", {class: "detail-desc"}, survey.description)
      : h("p", {class: "muted"}, "（无说明）"),
    h("h3", null, `题目（共 ${survey.questions.length} 道）`)
  );
  if (!survey.questions.length) {
    app.append(
      h("p", {class: "muted"}, "该问卷草稿还没有题目，可进入编辑补齐。"),
      h("p", null, h("a", {class: "btn", href: `#/surveys/${survey.id}/edit`}, "编辑草稿并添加题目"))
    );
    return;
  }
  app.append(h("ol", {class: "q-list"},
    survey.questions.map((q, i) => h("li", null,
      h("div", {class: "q-line"},
        `${i + 1}. ${q.title} `,
        h("span", {class: "tag"}, q.type === "single_choice" ? "单选题" : "文本题"),
        " ",
        h("span", {class: "tag" + (q.required ? " required" : "")}, q.required ? "必填" : "选填")
      ),
      q.options && q.options.length
        ? h("ol", {start: "1"}, q.options.map(opt => h("li", {class: "opt-text-display"}, opt)))
        : null
    )))
  );
}

/* ---------- 编辑草稿 ---------- */

function showEditLoadError(id, message) {
  // 加载失败时明确提示，绝不能用空白编辑表单覆盖已有内容
  app.replaceChildren(
    h("p", null, h("a", {href: "#/"}, "← 返回首页")),
    h("h2", null, `编辑问卷草稿 #${id}`),
    h("div", {class: "banner", role: "alert"},
      h("strong", null, "问卷内容加载失败，未打开编辑表单："),
      h("p", {class: "detail-meta", style: "margin:.4rem 0 0"}, message),
      h("p", {style: "margin:.6rem 0 0"},
        h("button", {type: "button", class: "btn secondary", onclick: () => renderEdit(id)}, "重试"), " ",
        h("a", {class: "btn secondary", href: `#/surveys/${id}`}, "返回详情"))
    )
  );
}

async function renderEdit(id) {
  app.replaceChildren(
    h("p", null, h("a", {href: `#/surveys/${id}`}, "← 返回问卷详情")),
    h("p", {class: "muted", id: "edit-status"}, "加载中…")
  );

  let survey;
  try {
    const resp = await fetch(`/api/surveys/${id}`);
    if (resp.status === 404) {
      showEditLoadError(id, `问卷 #${id} 不存在。`);
      return;
    }
    if (!resp.ok) {
      showEditLoadError(id, `服务端返回异常（HTTP ${resp.status}），请稍后重试。`);
      return;
    }
    survey = await resp.json();
  } catch (err) {
    showEditLoadError(id, `网络错误：${err}。已保存的问卷内容未受影响，可重试加载。`);
    return;
  }

  app.replaceChildren(
    h("p", null, h("a", {href: `#/surveys/${id}`}, "← 返回问卷详情")),
    buildSurveyForm(survey, {})
  );
}

function route() {
  const editMatch = location.hash.match(/^#\/surveys\/(\d+)\/edit$/);
  const detailMatch = location.hash.match(/^#\/surveys\/(\d+)$/);
  if (editMatch) renderEdit(Number(editMatch[1]));
  else if (detailMatch) renderDetail(Number(detailMatch[1]));
  else renderHome();
}
window.addEventListener("hashchange", route);
route();
</script>
</html>
"""


def validate_survey(obj):
    """Validate a decoded survey payload.

    Returns (clean_survey, None) on success or (None, error_message) on failure.
    Strings are stored trimmed of surrounding whitespace; description keeps its
    raw value so Chinese, quotes and newlines are preserved.
    """
    if not isinstance(obj, dict):
        return None, "请求体必须是 JSON 对象"
    if "title" not in obj:
        return None, "缺少字段 title"
    title = obj["title"]
    if not isinstance(title, str):
        return None, "title 必须是字符串"
    title = title.strip()
    if not title:
        return None, "title 不能为空（或全为空白字符）"

    description = obj.get("description", "")
    if not isinstance(description, str):
        return None, "description 必须是字符串"

    if "questions" not in obj:
        return None, "缺少字段 questions"
    raw_questions = obj["questions"]
    if not isinstance(raw_questions, list):
        return None, "questions 必须是数组"
    if len(raw_questions) == 0:
        return None, "questions 至少需要一道题"

    questions = []
    for index, raw_question in enumerate(raw_questions, start=1):
        location = f"第 {index} 题"
        if not isinstance(raw_question, dict):
            return None, f"{location}（questions[{index - 1}]）必须是对象"

        if "type" not in raw_question:
            return None, f"{location}：缺少字段 type"
        question_type = raw_question["type"]
        if not isinstance(question_type, str):
            return None, f"{location}：type 必须是字符串"
        if question_type not in QUESTION_TYPES:
            shown = json.dumps(question_type, ensure_ascii=False)
            return None, f"{location}：不支持的题型 {shown}（仅支持 text 或 single_choice）"

        if "title" not in raw_question:
            return None, f"{location}：缺少字段 title"
        question_title = raw_question["title"]
        if not isinstance(question_title, str):
            return None, f"{location}：title 必须是字符串"
        question_title = question_title.strip()
        if not question_title:
            return None, f"{location}：题目标题不能为空（或全为空白字符）"

        required = raw_question.get("required", False)
        if not isinstance(required, bool):
            return None, f"{location}：required 必须是布尔值"

        raw_options = raw_question.get("options", [])
        if not isinstance(raw_options, list):
            return None, f"{location}：options 必须是数组"

        options = []
        if question_type == "text":
            if len(raw_options) > 0:
                return None, f"{location}：文本题不能包含选项（options 必须省略或为空数组）"
        else:
            if len(raw_options) < 2:
                return None, f"{location}：单选题至少需要两个选项"
            seen = set()
            for option_index, raw_option in enumerate(raw_options, start=1):
                if not isinstance(raw_option, str):
                    return None, f"{location}：第 {option_index} 个选项必须是字符串"
                option = raw_option.strip()
                if not option:
                    return None, f"{location}：第 {option_index} 个选项不能为空（或全为空白字符）"
                if option in seen:
                    shown = json.dumps(option, ensure_ascii=False)
                    return None, f"{location}：选项 {shown} 重复（同一题内选项不得重复）"
                seen.add(option)
                options.append(option)

        questions.append({
            "type": question_type,
            "title": question_title,
            "required": required,
            "options": options,
        })

    return {
        "title": title,
        "description": description,
        "questions": questions,
    }, None


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
    columns = {row[1] for row in database.execute("PRAGMA table_info(surveys)")}
    if "description" not in columns:
        database.execute("ALTER TABLE surveys ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    database.execute(
        "CREATE TABLE IF NOT EXISTS questions ("
        "id INTEGER PRIMARY KEY, survey_id INTEGER NOT NULL, position INTEGER NOT NULL, "
        "type TEXT NOT NULL, title TEXT NOT NULL, required INTEGER NOT NULL)"
    )
    database.execute(
        "CREATE TABLE IF NOT EXISTS question_options ("
        "id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL)"
    )
    database.commit()

    def survey_detail(survey_id):
        row = database.execute(
            "SELECT title, COALESCE(description, '') FROM surveys WHERE id = ?",
            (survey_id,),
        ).fetchone()
        if row is None:
            return None
        title, description = row
        questions = []
        question_rows = database.execute(
            "SELECT id, type, title, required FROM questions WHERE survey_id = ? ORDER BY position",
            (survey_id,),
        ).fetchall()
        for question_id, question_type, question_title, required in question_rows:
            options = [option_row[0] for option_row in database.execute(
                "SELECT text FROM question_options WHERE question_id = ? ORDER BY position",
                (question_id,),
            )]
            questions.append({
                "type": question_type,
                "title": question_title,
                "required": bool(required),
                "options": options,
            })
        return {
            "id": survey_id,
            "title": title,
            "description": description,
            "questions": questions,
        }

    def save_survey(clean):
        """Insert the whole survey atomically; returns the new id."""
        try:
            cursor = database.execute(
                "INSERT INTO surveys (title, description) VALUES (?, ?)",
                (clean["title"], clean["description"]),
            )
            survey_id = cursor.lastrowid
            write_questions(survey_id, clean["questions"])
            database.commit()
            return survey_id
        except Exception:
            database.rollback()
            raise

    def write_questions(survey_id, questions):
        """Replace all questions/options of a survey with the given ordered list."""
        old_question_ids = [row[0] for row in database.execute(
            "SELECT id FROM questions WHERE survey_id = ?", (survey_id,)
        )]
        if old_question_ids:
            placeholders = ",".join("?" for _ in old_question_ids)
            database.execute(
                f"DELETE FROM question_options WHERE question_id IN ({placeholders})",
                old_question_ids,
            )
        database.execute("DELETE FROM questions WHERE survey_id = ?", (survey_id,))
        for position, question in enumerate(questions):
            question_cursor = database.execute(
                "INSERT INTO questions (survey_id, position, type, title, required) "
                "VALUES (?, ?, ?, ?, ?)",
                (survey_id, position, question["type"], question["title"],
                 1 if question["required"] else 0),
            )
            question_id = question_cursor.lastrowid
            for option_position, option in enumerate(question["options"]):
                database.execute(
                    "INSERT INTO question_options (question_id, position, text) VALUES (?, ?, ?)",
                    (question_id, option_position, option),
                )

    def replace_survey(survey_id, clean):
        """Replace a whole survey atomically; keeps the same id.

        Returns False when the survey does not exist (no record is created).
        """
        row = database.execute("SELECT 1 FROM surveys WHERE id = ?", (survey_id,)).fetchone()
        if row is None:
            return False
        try:
            database.execute(
                "UPDATE surveys SET title = ?, description = ? WHERE id = ?",
                (clean["title"], clean["description"], survey_id),
            )
            write_questions(survey_id, clean["questions"])
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, value, *, html=False, allow=None):
            payload = value.encode("utf8") if html else json.dumps(value, ensure_ascii=False).encode("utf8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if status == 405 and allow is not None:
                self.send_header("Allow", ", ".join(allow))
            self.end_headers()
            self.wfile.write(payload)

        def list_surveys(self):
            records = [
                {"id": row[0], "title": row[1]}
                for row in database.execute("SELECT id, title FROM surveys ORDER BY id")
            ]
            self.respond(200, {RESOURCE: records})

        def read_payload(self):
            """Read and decode a JSON request body.

            Returns (payload, None) or (None, error_message).
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                return None, "Content-Length 无效"
            if length > MAX_BODY_BYTES:
                return None, "请求体过大"
            body = self.rfile.read(length) if length else b""
            try:
                return json.loads(body.decode("utf8")), None
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return None, f"请求体不是合法 JSON：{error}"

        def create_survey(self):
            payload, error = self.read_payload()
            if error is not None:
                self.respond(400, {"error": error})
                return

            clean, error = validate_survey(payload)
            if error is not None:
                self.respond(400, {"error": error})
                return
            try:
                survey_id = save_survey(clean)
            except sqlite3.DatabaseError as error:
                self.respond(500, {"error": f"保存失败：{error}"})
                return
            result = {"id": survey_id, **clean}
            self.respond(201, result)

        def update_survey(self, survey_id):
            payload, read_error = self.read_payload()
            if survey_detail(survey_id) is None:
                # Editing a missing id is always 404 and must never create one.
                self.respond(404, {"error": "not found"})
                return
            if read_error is not None:
                self.respond(400, {"error": read_error})
                return

            # Full replacement: validate before touching the stored draft so a
            # rejected request leaves the old survey exactly as it was.
            clean, error = validate_survey(payload)
            if error is not None:
                self.respond(400, {"error": error})
                return
            try:
                replace_survey(survey_id, clean)
            except sqlite3.DatabaseError as error:
                self.respond(500, {"error": f"保存失败：{error}"})
                return
            self.respond(200, {"id": survey_id, **clean})

        def route(self):
            location = urlsplit(self.path).path
            detail_match = re.fullmatch(r"/api/surveys/(\d+)", location)

            if location == "/api/surveys":
                allowed = ["GET", "POST"]
            elif location in ("/", "/health"):
                allowed = ["GET"]
            elif detail_match:
                allowed = ["GET", "PUT"]
            else:
                self.respond(404, {"error": "not found"})
                return

            if self.command not in allowed:
                self.respond(405, {"error": "method not allowed"}, allow=allowed)
                return

            if location == "/":
                self.respond(200, PAGE, html=True)
            elif location == "/health":
                self.respond(200, {"status": "ok", "product": PRODUCT})
            elif location == "/api/surveys":
                if self.command == "POST":
                    self.create_survey()
                else:
                    self.list_surveys()
            else:
                survey_id = int(detail_match.group(1))
                if self.command == "PUT":
                    self.update_survey(survey_id)
                    return
                detail = survey_detail(survey_id)
                if detail is None:
                    self.respond(404, {"error": "not found"})
                else:
                    self.respond(200, detail)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = route

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    server = None
    try:
        server = HTTPServer((args.host, args.port), Handler)
        host, port = server.server_address[:2]
        address = f"[{host}]" if ":" in host else host
        print(f"{PRODUCT} listening on http://{address}:{port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        database.close()


if __name__ == "__main__":
    main()
