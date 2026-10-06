#!/usr/bin/env python3
"""OpenSurvey HTTP service."""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import sqlite3
import threading
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
input[type=text],textarea{width:100%;padding:.5rem .6rem;border:1px solid var(--line);border-radius:.35rem;font:inherit}
textarea{resize:vertical}
/* 标题与选项允许内部换行：用随内容增高的 textarea 展示，不能压成单行输入框 */
textarea.grow{resize:none;overflow:hidden;min-height:2.4rem;line-height:1.5}
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
/* 保存状态条：与错误提示条区分开，明确“刚才那次提交已保存”与“还有未保存修改” */
.save-status{margin:1rem 0;display:flex;flex-direction:column;gap:.5rem}
.save-status[hidden]{display:none}
.save-note{border-radius:.5rem;padding:.7rem 1rem;white-space:normal}
.save-note.saved{border:1px solid #1e7e45;background:#ecf7f0;color:#155c33}
.save-note.dirty{border:1px solid #9a6d12;background:#fdf6e3;color:#7a5207}
/* 等待保存结果：蓝色中性提示，与成功（绿）、未保存（黄）、错误（红）区分 */
.save-note.saving{border:1px solid var(--blue);background:#eef4fb;color:#144a80}
button:disabled{opacity:.55;cursor:not-allowed}
.field-err{color:var(--red);font-size:.85rem;margin:.25rem 0 0;min-height:1px}
.invalid{border-color:var(--red)!important;background:#fdf6f5}
.detail-meta{color:var(--grey);font-size:.9rem}
.detail-desc,.opt-text-display{white-space:pre-wrap}
/* 标题中被接口保留的内部换行在列表与详情中也要原样显示 */
h2,.q-list .q-line,ul.plain a{white-space:pre-wrap}
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

// 每次进入一个页面都开启新一代视图：切换首页/详情/编辑、重新进入同一份问卷、
// 重试加载以及浏览器前进/后退都会使代数递增。较早一代页面发起的请求再晚
// 返回也必须丢弃，不能覆盖用户当前所在页面。
let activeView = 0;

// 首尾裁剪必须与接口（Python str.strip()）逐字符一致，网页校验、提交内容与
// “是否还有未保存修改”的比较才能表达同一份文本含义。JS 原生 trim() 的字符集
// 恰好不同：它会裁掉 U+FEFF（接口保留该字符），却不裁 U+0085 与 U+001C–
// U+001F（接口会把它们当空白裁掉）。这里显式列出接口会裁的 29 个字符，
// U+FEFF 刻意不在其中；内部字符（含内部换行）一律不动。说明字段从不裁剪。
const API_TRIM_CLASS =
  "\\t\\n\\v\\f\\r\\x1c-\\x1f\\x20\\u0085\\u00a0\\u1680" +
  "\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const apiTrimRe = new RegExp(
  "^[" + API_TRIM_CLASS + "]+|[" + API_TRIM_CLASS + "]+$", "g");
function apiTrim(value) { return value.replace(apiTrimRe, ""); }

// 换行归一化：接口会原样保留文本内部的回车换行（CRLF，\r\n）、单独回车
// （CR，\r）与换行（LF，\n），但这些值赋给 textarea 后会被浏览器立即统一
// 成 LF（实测在赋值给 .value 的那一刻即发生）。因此编辑页必须在把原稿送进
// 输入框之前记录原始字符串，保存时再按“可见内容是否改变”决定还原原稿还是
// 采用当前输入；比较一律基于归一化为 LF 的文本，使三种换行在编辑框里的同
// 一份可见内容被视为“没有改变”。
function normalizeNL(value) { return value.replace(/\r\n|\r|\n/g, "\n"); }

// 仅编辑模式使用：输入框元素 -> 打开编辑页时该字段从接口读到的原始字符串。
// 用 WeakMap 是为了让随题目/选项删除而移除的元素连同记录一起被回收，绝不
// 需要手工清理，也不会把已删除行的原文错配到后来顶上来的另一行。新建模式
// 下不写入任何记录。
const origMap = new WeakMap();
function rememberOriginal(el, raw) { origMap.set(el, String(raw)); }

// 计算一个文本字段“实际要保存”的字符串：
// - 编辑模式且该字段可见内容与打开时一致：还原为原稿，保留原始 CRLF/CR/LF；
// - 其余情况（真正修改过，或新建模式）：采用输入框当前文本（浏览器统一为 LF）。
// 比较规则与“判断文字是否改变”的既有规则一致：trimFields 为真时（问卷标题、
// 题目标题、选项）首尾空白不参与比较，因此只增删首尾空白也算未改变、仍还原
// 原稿（原稿内部的 CRLF/CR 不会因此被 LF 覆盖）；说明字段不裁剪，逐字符比较。
function resolveFieldValue(el, {trimFields = false} = {}) {
  const current = el.value;
  const original = origMap.get(el);
  if (original !== undefined) {
    const a = trimFields ? apiTrim(normalizeNL(current)) : normalizeNL(current);
    const b = trimFields ? apiTrim(normalizeNL(original)) : normalizeNL(original);
    if (a === b) return original;
  }
  return current;
}

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

function buildSurveyForm(existing, hooks, view) {
  // 新建表单首次创建成功且等待期间仍有未保存修改时，会就地转为“编辑刚创建
  // 的那份草稿”，因此 mode 允许从 create 切换为 edit。
  let mode = existing ? "edit" : "create";

  // 表单当前正在编辑的草稿编号：编辑模式一开始就有；新建模式只在首次创建
  // 成功后才拿到。拿到编号之后，这份表单继续保存时一律 PUT 同一编号，不
  // 能再 POST 出第二份问卷。
  let surveyId = existing ? existing.id : null;

  const banner = h("div", {class: "banner", id: "form-banner"});
  banner.hidden = true;

  // 保存结果的状态条：成功保存与“仍有未保存修改”必须分开展示，不能只给
  // 一条笼统的“保存成功”。新建模式首次保存成功且等待期间没有新改动时直接
  // 进入详情（状态条不可见）；等待期间又有改动时则与编辑模式共用本状态条。
  const statusBar = h("div", {class: "save-status", id: "save-status", role: "status"});
  statusBar.hidden = true;

  const titleInput = h("textarea", {id: "survey-title", rows: "1", class: "grow",
    maxlength: "200", placeholder: "请输入问卷标题（可含换行）"});
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

  // textarea 的高度随内容变化：单行内容外观与原输入框一致，出现内部换行时
  // 自动增高，让换行真实可见、可编辑，而不是被折叠或截断。
  function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }
  // 预填值时元素可能还没挂到文档上（scrollHeight 为 0），延到下一帧布局后再量。
  function scheduleGrow(el) { requestAnimationFrame(() => autoGrow(el)); }

  function optionRow(value) {
    const text = h("textarea", {rows: "1", class: "opt-text grow", placeholder: "选项内容（可含换行）"});
    text.addEventListener("input", () => autoGrow(text));
    if (value !== undefined && value !== null) {
      // 必须在写入 .value 之前记录原稿：赋值一旦发生，CRLF/CR 就已被浏览器
      // 归一化成 LF，之后再读 text.value 也拿不回原始换行形式。
      if (mode === "edit") rememberOriginal(text, value);
      text.value = value; scheduleGrow(text);
    }
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
      h("textarea", {rows: "1", class: "q-title grow", placeholder: "题目标题（可含换行）"}),
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
    const qTitleEl = card.querySelector(".q-title");
    qTitleEl.addEventListener("input", () => autoGrow(qTitleEl));
    if (prefill) {
      // 与选项同理：在 .value 赋值（会把 CRLF/CR 归一化成 LF）之前先记住原稿。
      if (mode === "edit" && prefill.title != null) rememberOriginal(qTitleEl, prefill.title);
      qTitleEl.value = prefill.title != null ? prefill.title : "";
      card.querySelector(".q-required").checked = !!prefill.required;
    }
    scheduleGrow(qTitleEl);
    qBox.append(card);
    renumber();
    if (!prefill) qTitleEl.focus();
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

  function hideSaveStatus() {
    statusBar.hidden = true;
    statusBar.replaceChildren();
  }

  // 保存成功后：绿色块只陈述“刚才提交的内容已保存”；若等待期间又有修改，
  // 再追加一块黄色“当前还有未保存的修改”。两者同时出现、含义互不混淆。
  function renderSaveStatus(dirty) {
    statusBar.replaceChildren(
      h("div", {class: "save-note saved", id: "save-note-saved"},
        h("strong", null, "已保存：刚才提交的内容已保存到服务器。")),
      dirty
        ? h("div", {class: "save-note dirty", id: "save-note-dirty"},
            h("strong", null,
              "当前还有未保存的修改：页面内容在点击保存后又被改动（等待结果期间的修改不会自动补交）。可继续编辑，并再次点击“保存修改”。"))
        : null
    );
    statusBar.hidden = false;
  }

  // 一次保存进行中：保存按钮禁用并明确显示“正在保存…”，状态条显示等待提示。
  // 在请求有明确结果前，重复点击按钮或用键盘再次提交都不会追加请求，也不会
  // 清掉等待提示。等待期间标题、说明、题目、选项、必填与增删操作全部可用。
  let saving = false;
  function setSavingUI() {
    submitButton.disabled = true;
    submitButton.textContent = "正在保存…";
    statusBar.replaceChildren(
      h("div", {class: "save-note saving", id: "save-note-saving", role: "status"},
        h("strong", null, "正在保存：正在等待本次保存的结果，请稍候。等待期间仍可继续编辑，这些改动不会自动追加到本次提交。"))
    );
    statusBar.hidden = false;
  }
  function resetSaveButton() {
    submitButton.disabled = false;
    submitButton.textContent = submitButton.getAttribute("data-label");
  }

  // 整份草稿的唯一整理入口：保存时提交的内容、保存前的校验、编辑页判断
  // “等待保存期间是否又有修改”都从这里取数，规则只维护这一份。标题/题目
  // 标题/选项只按接口规则（apiTrim，与 Python strip 一致）去掉首尾空白，
  // 说明与内部换行原样保留；题目与选项按页面当前顺序记录，必填勾选原样。
  // 编辑模式下未改动的字段（含“改过又恢复”）由 resolveFieldValue 还原为
  // 打开时从接口读到的原稿，使内部 CRLF/CR/LF 不被浏览器的换行归一化改写；
  // 真正改动的字段与新建模式一律采用当前输入（浏览器中只会是 LF）。
  // 每次调用都返回新建的独立对象（后续输入不会改变已返回的快照），可直接
  // 做整体相等比较：改过又恢复原值时结构相同，不会被当成有未保存修改。
  function readDraft() {
    return {
      title: apiTrim(resolveFieldValue(titleInput, {trimFields: true})),
      description: resolveFieldValue(descArea),
      questions: [...qBox.querySelectorAll(".q-card")].map(card => ({
        type: card.dataset.type,
        title: apiTrim(resolveFieldValue(card.querySelector(".q-title"), {trimFields: true})),
        required: card.querySelector(".q-required").checked,
        options: [...card.querySelectorAll(".opt-text")]
          .map(el => apiTrim(resolveFieldValue(el, {trimFields: true})))
      }))
    };
  }

  function sameState(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

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
  const submitLabel = mode === "edit" ? "保存修改" : "保存整份问卷";
  const submitButton = h("button", {type: "submit", class: "btn", "data-label": submitLabel}, submitLabel);
  actionButtons.push(submitButton);

  const form = h("form", {id: "draft-form", onsubmit: submitForm},
    banner,
    statusBar,
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

  titleInput.addEventListener("input", () => autoGrow(titleInput));
  if (mode === "edit") {
    // 同样在赋值（会归一化换行）之前记下原稿；说明字段不裁剪、逐字符比较。
    rememberOriginal(titleInput, existing.title != null ? existing.title : "");
    rememberOriginal(descArea, existing.description || "");
    titleInput.value = existing.title != null ? existing.title : "";
    descArea.value = existing.description || "";
    existing.questions.forEach(question => addQuestion(question.type, question));
    renumber();
    scheduleGrow(titleInput);
  }

  // 最近一次服务器确认保存的表单内容（点击保存时的快照）。新建模式首次保存
  // 成功前为 null（尚无已保存内容可比）；成功后改记那次提交的快照。它只用
  // 于成功返回后判断当前表单相对那次提交有没有新改动；等待期间的输入始终留
  // 在表单里，不会被它覆盖，也不会被自动补交。
  let savedState = mode === "edit" ? readDraft() : null;

  function refreshDirty() {
    const dirtyNote = statusBar.querySelector("#save-note-dirty");
    // 尚未成功保存过（状态条隐藏）时无需提示。
    if (statusBar.hidden || !dirtyNote) return;
    // 与点击保存时的整份内容比较：改过又恢复原值即视为无未保存修改。
    dirtyNote.hidden = sameState(readDraft(), savedState);
  }

  // 监听表单上全部真实编辑动作（标题/说明/题目标题/选项输入、必填勾选、
  // 题目与选项增删）。题目卡片是动态增删的，因此在表单上做事件委托。
  form.addEventListener("input", refreshDirty);
  form.addEventListener("change", event => {
    if (event.target.closest && event.target.closest(".q-required")) refreshDirty();
  });
  form.addEventListener("click", event => {
    const actionable = event.target.closest
      && event.target.closest("button.add-opt, button.link.danger, button.btn.secondary");
    if (actionable) {
      // 新增/删除在本次点击处理中完成，延到冒泡后再读取最终 DOM。
      queueMicrotask(refreshDirty);
    }
  });

  hooks = hooks || {};
  hooks.markSaved = state => { savedState = state; };
  hooks.isCurrent = state => sameState(readDraft(), state);

  // 首次创建成功、但等待期间表单又有未保存修改时：不跳转、不清空，把这份新建
  // 表单就地切换成“编辑刚创建的那份草稿”。只改标题、说明文字与操作按钮，
  // 绝不动任何当前输入与题目/选项增删结果及次序。此后 surveyId 已绑定编号，
  // 再保存走 PUT 同一编号，首页列表与详情也对应这同一个编号。
  function adoptCreatedDraft(id) {
    mode = "edit";
    const heading = form.querySelector("h2");
    if (heading) {
      heading.textContent = `编辑问卷草稿 #${id}`;
      heading.after(h("p", {class: "muted"},
        "保存将整份替换当前草稿；取消则放弃本次修改，问卷编号和地址不变。"));
    }
    submitButton.before(
      h("a", {class: "btn secondary", href: `#/surveys/${id}`}, "取消"));
    submitButton.setAttribute("data-label", "保存修改");
    submitButton.textContent = "保存修改";
  }

  async function submitForm(event) {
    event.preventDefault();
    // 已有一次保存尚未得到明确结果：忽略后续点击/键盘提交，不追加请求，也不
    // 清除正在显示的等待状态。本次保存只代表首次点击时的那份表单快照。
    if (saving) return;
    clearErrors();
    hideSaveStatus();
    const problems = [];

    // 点击保存这一刻的整份草稿：整理规则只有 readDraft 一处，校验、提交内容
    // 与成功后的“是否有新修改”比较表达的都是同一份草稿。
    const draft = readDraft();

    if (!draft.title) {
      titleInput.classList.add("invalid");
      titleErr.textContent = "标题不能为空。";
      problems.push({el: titleInput, msg: "问卷标题不能为空。"});
    }

    const cards = [...qBox.querySelectorAll(".q-card")];
    if (cards.length === 0) {
      qErr.textContent = "问卷至少需要保留一道题。";
      problems.push({el: qBox, msg: "问卷至少需要保留一道题，请先添加题目。"});
    }

    // 校验针对整理后的草稿内容（空标题、空选项、同题重复选项都按裁剪后的值
    // 判断），错误标记仍落在对应的页面输入框上；输入框原文保持不动。
    cards.forEach((card, i) => {
      const loc = `第 ${i + 1} 题`;
      const question = draft.questions[i];
      const titleEl = card.querySelector(".q-title");
      if (!question.title) {
        titleEl.classList.add("invalid");
        card.querySelector(".q-title-err").textContent = `${loc}：题目标题不能为空。`;
        problems.push({el: titleEl, msg: `${loc}：题目标题不能为空。`});
      }
      if (question.type === "single_choice") {
        const optInputs = [...card.querySelectorAll(".opt-text")];
        const optErr = card.querySelector(".opt-err");
        const seen = new Set();
        question.options.forEach((val, j) => {
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
        if (question.options.length < 2) {
          optErr.textContent = `${loc}：单选题至少需要两个选项。`;
          problems.push({el: card.querySelector(".add-opt"), msg: `${loc}：单选题至少需要两个选项。`});
        }
      }
    });

    if (problems.length) {
      showBanner(problems);
      return;
    }
    // 校验通过、请求即将发出：此后到拿到明确结果前只能有这一次保存进行中。
    // 纯客户端校验拒绝不会走到这里，因此不会留下等待状态。
    saving = true;
    setSavingUI();

    // 提交内容就是这份草稿本身：文本题不带 options（接口接受省略或空数组），
    // 单选题按整理后的选项内容与顺序提交。
    const payload = {
      title: draft.title,
      description: draft.description,
      questions: draft.questions.map(q => q.type === "single_choice"
        ? {type: q.type, title: q.title, required: q.required, options: q.options}
        : {type: q.type, title: q.title, required: q.required})
    };
    // 点击保存这一刻的整份表单快照：这次请求只代表这份内容。readDraft 返回的
    // 是独立对象，等待期间的后续输入不会改变它；成功返回后拿它与重新读取的
    // 当前表单比较——等待期间的任何输入/增删都不属于本次保存。
    const submittedState = draft;
    // 新建表单在首次创建成功之前没有编号，走 POST；一旦创建成功，这份表单就
    // 绑定到新编号，此后再保存一律 PUT 同一编号，绝不新增第二份问卷。
    const creating = surveyId === null;
    const endpoint = creating ? "/api/surveys" : `/api/surveys/${surveyId}`;
    // 这次保存只属于发起它的那一代表单；响应再晚返回，只要用户已经离开
    // （首页/详情/编辑切换、重新进入同一问卷、浏览器前进后退），其成功或
    // 失败结果都必须静默丢弃，不能跳转、清空或提示到当前页面上。
    const submitView = view;
    let resp;
    try {
      resp = await fetch(endpoint, {
        method: creating ? "POST" : "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
    } catch (err) {
      if (submitView !== activeView) return;
      // 请求未成功：结束等待，保留当前全部输入与增删结果。新建模式下编号仍未
      // 拿到（surveyId 保持 null），修改后再次保存会重新 POST 创建，绝不会
      // 显示成功或跳进某份问卷详情。
      saving = false;
      resetSaveButton();
      hideSaveStatus();
      showBanner([{el: null, msg: creating
        ? `网络错误，本次创建尚未确认保存：${err}。已填写的内容仍保留在页面上，修改后可重新创建。`
        : `网络错误，问卷尚未保存：${err}`}]);
      return;
    }
    if (submitView !== activeView) return;
    let data = {};
    try { data = await resp.json(); } catch (_) { /* 保留已输入内容 */ }
    if (submitView !== activeView) return;
    const saved = creating ? resp.status === 201 : resp.status === 200;
    if (saved && data.id !== undefined) {
      if (creating) {
        // 首次创建成功：这份表单从此绑定到刚创建的编号。刷新首页列表，让新
        // 草稿出现在列表中；列表与详情使用同一个编号。
        surveyId = data.id;
        if (hooks && hooks.onCreated) hooks.onCreated();
      }
      // 先把“这次提交的内容”记为已保存（服务器上的草稿正是它）。
      if (hooks && hooks.markSaved) hooks.markSaved(submittedState);
      // 比较点击保存前后的整份表单：等待期间改过又恢复原值时结构相同，按
      // 无新改动处理，仍进入详情。
      const stillSame = hooks && hooks.isCurrent ? hooks.isCurrent(submittedState) : true;
      if (stillSame) {
        if (creating) {
          // 新建且等待期间没有新改动：沿用原有行为，清空新建表单并进入新问卷
          // 详情，详情展示的就是服务器实际保存的内容。
          form.reset();
          qBox.replaceChildren();
          autoGrow(titleInput);
        }
        location.hash = `#/surveys/${data.id}`;
        return;
      }
      // 等待期间出现了新的修改：不跳转，保留当前完整内容与顺序（题目与选项
      // 维持页面当前次序，不恢复成提交时的结构）。绿色块只确认“刚才提交的
      // 内容已保存”，黄色块提示“当前还有未保存的修改”，不自动补交，也不
      // 撤销已成功的保存。新建表单就此转为编辑刚创建的那份草稿：标题改为
      // 编辑态、补上“取消”链接、按钮文案改为“保存修改”；再次点击保存时
      // surveyId 已存在，走 PUT 同一编号，提交的才是当前内容。
      if (creating) adoptCreatedDraft(data.id);
      saving = false;
      resetSaveButton();
      renderSaveStatus(true);
      return;
    }
    // 服务端未确认保存成功：结束等待，不清空任何输入，展示具体问题；用户修改
    // 后可再次保存（不会自行重发，也不会一直停在“正在保存”）。新建模式下
    // 本次没有创建任何记录（surveyId 仍为 null），再次保存仍是重新创建。
    saving = false;
    resetSaveButton();
    hideSaveStatus();
    showBanner([{el: null, msg: data.error
      || (creating
        ? `保存失败（HTTP ${resp.status}），本次创建未确认保存，请检查后重试。`
        : `保存失败（HTTP ${resp.status}），问卷未保存，请检查后重试。`)}]);
  }

  return form;
}

/* ---------- 首页 ---------- */

function renderHome() {
  const view = ++activeView;
  app.replaceChildren();

  const listSection = h("section", null,
    h("h2", null, "问卷列表"),
    h("ul", {class: "plain", id: "survey-list"}, h("li", {class: "muted"}, "加载中…"))
  );
  const listEl = listSection.querySelector("#survey-list");

  // 同一次首页停留期间可能先后发起多次列表读取：进入首页时读一次，新建草稿
  // 成功后再刷新一次。列表只认“开始得最晚”的那次读取——每开始一次新读取
  // 就递增序号，较早发起但尚未完成的读取再晚返回（成功且有数据、成功但为
  // 空列表、响应无法解析或网络失败）都一律丢弃，不能把新读取已展示的问卷
  // 换成旧列表、旧的“还没有问卷记录”空状态或“加载失败”提示。以读取开始
  // 的先后为准，与响应返回的先后无关；离开本次首页后再由 view 代数检查拦
  // 截，迟到结果不会更新其他页面或下一次进入的首页。
  let listRequestSeq = 0;

  async function loadSurveys() {
    const requestView = view;
    const requestSeq = ++listRequestSeq;
    let data;
    try {
      const resp = await fetch("/api/surveys");
      data = await resp.json();
    } catch (err) {
      if (requestView !== activeView || requestSeq !== listRequestSeq) return;
      listEl.replaceChildren(h("li", {class: "muted"}, "问卷列表加载失败。"));
      return;
    }
    if (requestView !== activeView || requestSeq !== listRequestSeq) return;
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
    buildSurveyForm(null, {onCreated: loadSurveys}, view)
  );
  loadSurveys();
}

/* ---------- 问卷详情 ---------- */

async function renderDetail(id) {
  const view = ++activeView;
  app.replaceChildren(
    h("p", null, h("a", {href: "#/"}, "← 返回首页")),
    h("p", {class: "muted", id: "detail-status"}, "加载中…")
  );
  const status = document.getElementById("detail-status");

  let survey;
  try {
    const resp = await fetch(`/api/surveys/${id}`);
    if (view !== activeView) return;
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
    if (view !== activeView) return;
    status.textContent = `详情加载失败：${err}`;
    return;
  }
  if (view !== activeView) return;

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

function showEditLoadError(id, message, view) {
  // 加载失败时明确提示，绝不能用空白编辑表单覆盖已有内容
  if (view !== activeView) return;
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
  const view = ++activeView;
  app.replaceChildren(
    h("p", null, h("a", {href: `#/surveys/${id}`}, "← 返回问卷详情")),
    h("p", {class: "muted", id: "edit-status"}, "加载中…")
  );

  let survey;
  try {
    const resp = await fetch(`/api/surveys/${id}`);
    if (view !== activeView) return;
    if (resp.status === 404) {
      showEditLoadError(id, `问卷 #${id} 不存在。`, view);
      return;
    }
    if (!resp.ok) {
      showEditLoadError(id, `服务端返回异常（HTTP ${resp.status}），请稍后重试。`, view);
      return;
    }
    survey = await resp.json();
  } catch (err) {
    showEditLoadError(id, `网络错误：${err}。已保存的问卷内容未受影响，可重试加载。`, view);
    return;
  }

  if (view !== activeView) return;
  app.replaceChildren(
    h("p", null, h("a", {href: `#/surveys/${id}`}, "← 返回问卷详情")),
    buildSurveyForm(survey, {}, view)
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
    # 每个连接由独立线程处理（见下方 ThreadingHTTPServer），因此数据库连接
    # 会被多个线程共享：check_same_thread=False 允许跨线程使用，所有读写再
    # 经 db_lock 串行化，保证一次保存/替换内部的多条语句仍然原子生效。
    database = sqlite3.connect(args.data_dir / "open-survey.sqlite", check_same_thread=False)
    db_lock = threading.Lock()
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
        with db_lock:
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
        with db_lock:
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
        with db_lock:
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
            with db_lock:
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
        # 每个连接在独立线程中处理：浏览器预先建立但暂不发送请求的备用连接、
        # 或只发了部分请求头/正文就暂停的连接，只会占用自己的线程，不会拖住
        # 其他连接上的完整请求。daemon_threads 保证退出时不被这些空闲连接卡住。
        server = ThreadingHTTPServer((args.host, args.port), Handler)
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
