# OpenSurvey

问卷调查与反馈管理。

需要Python 3.10 或更新版本，包含标准库 sqlite3。

查看命令帮助：

```sh
python3 app.py --help
```

启动本地服务：

```sh
python3 app.py serve --host 127.0.0.1 --port 8080 --data-dir data
```

打开 http://127.0.0.1:8080 查看首页，可在首页新建问卷草稿、查看问卷列表并打开问卷详情。Ctrl+C 停止服务。`--data-dir` 指定本地业务数据目录，重启时继续使用同一目录。

接口：

- `GET /health` 返回服务状态和产品名称。
- `GET /api/surveys` 返回问卷列表（仅含编号与标题，按编号升序），首次启动时为空。
- `POST /api/surveys` 创建问卷草稿，接收 JSON 对象：
  - `title`（字符串，必填，去掉首尾空白后不能为空，按处理后的值保存）。
  - `description`（字符串，可省略，默认为空字符串；原样保留中文、引号与换行）。
  - `questions`（数组，必填，至少一道题）。每题：
    - `type`：仅接受 `text` 或 `single_choice`。
    - `title`：题目标题，去掉首尾空白后不能为空，按处理后的值保存；题目允许重名。
    - `required`：布尔值，可省略，默认 `false`。
    - `options`：单选题至少两个选项，各选项去掉首尾空白后非空且同题内不重复，按顺序保存；文本题省略或为空数组，携带非空选项会被拒绝。
  - 任意内容不合法返回 `400`，错误信息定位到具体字段/题目/选项，且不创建任何记录；成功返回 `201` 和含 `id` 的完整问卷对象。
- `GET /api/surveys/{id}` 返回问卷草稿详情（含说明与全部题目、选项）；不存在的编号返回 `404`。仅含编号和标题的旧记录详情为说明空字符串、问题空数组。
- `PUT /api/surveys/{id}` 完整替换问卷草稿，请求体结构与 `POST /api/surveys` 相同（整份替换，不是局部补充）：省略 `description` 时替换为空字符串，省略某题的 `required` 时按 `false` 保存（不沿用旧值），题目与选项按本次提交的数组顺序保存。成功返回 `200` 和更新后的完整问卷对象；编号保持不变。校验规则与创建一致（任何字段不合法返回 `400` 并指出对应字段/题目/选项，整份旧草稿保持原样，不会只保存前面合法的题目）；不存在的编号返回 `404`，不会因编辑请求创建记录。
- 未知路径返回 404，已知路径不支持的方法返回 405，响应头 `Allow` 反映该路径支持的方法。

数据保存在 `--data-dir` 下的 SQLite 中，使用同一目录重启后记录、题目与选项仍可读取。

```sh
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/api/surveys
curl -X POST http://127.0.0.1:8080/api/surveys \
  -H 'Content-Type: application/json' \
  -d '{"title":"示例问卷","questions":[{"type":"single_choice","title":"评分","required":true,"options":["好","一般","差"]}]}'
curl http://127.0.0.1:8080/api/surveys/1
curl -X PUT http://127.0.0.1:8080/api/surveys/1 \
  -H 'Content-Type: application/json' \
  -d '{"title":"修改后的问卷","questions":[{"type":"text","title":"新题目"}]}'
```
