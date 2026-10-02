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

打开 http://127.0.0.1:8080 查看首页。Ctrl+C 停止服务。`--data-dir` 指定本地业务数据目录，重启时继续使用同一目录，问卷与题目记录仍可读取。

接口：

- `GET /health` 返回服务状态和产品名称。
- `GET /api/surveys` 返回问卷列表，按编号升序，首次启动时为空。
- `POST /api/surveys` 新建问卷草稿。请求体为 JSON 对象：
  - `title`（字符串，必填）：问卷标题，去掉首尾空白后不能为空，按处理后的值保存。
  - `description`（字符串，可省略）：问卷说明，省略时为空字符串，保留换行与引号。
  - `questions`（数组，必填）：至少一道题。每题包含：
    - `type`：`text`（文本题）或 `single_choice`（单选题）。
    - `title`（字符串，必填）：题目，去掉首尾空白后不能为空，按处理后的值保存。
    - `required`（布尔值，可省略）：是否必填，省略时为 false。
    - `options`（字符串数组）：单选题必填，至少两个选项；文本题可省略或为空数组，包含选项时拒绝保存。每个选项去掉首尾空白后不能为空、同一题内不得重复，按处理后的值与提供顺序保存。
  - 保存成功返回 201，响应为包含生成 `id` 的完整问卷对象。
  - JSON 无法解析、字段类型不符、缺少必要字段、题型不支持或内容不合法时返回 400，错误信息通过 `errors` 数组定位到具体字段（如 `questions[1].options[0]`），且不创建任何记录。
- `GET /api/surveys/{id}` 返回问卷详情（含说明与全部题目），不存在的编号返回 404。
- 未知路径返回 404；已知路径不支持的方法返回 405，`Allow` 反映该路径支持的方法。

```sh
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/api/surveys
curl -X POST http://127.0.0.1:8080/api/surveys \
  -H "Content-Type: application/json" \
  -d '{"title":"客户满意度调查","description":"感谢参与。","questions":[{"type":"single_choice","title":"总体评价","required":true,"options":["满意","一般","不满意"]},{"type":"text","title":"改进建议"}]}'
curl http://127.0.0.1:8080/api/surveys/1
```
