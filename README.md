# 招投标信息采集项目

使用 Python、Playwright、Pydantic 和本地 OCR，从公开网页采集公告正文，提取字段并导出 JSON。目前实现三个站点：

| `SITES` 配置值 | 网站 | 日期处理 |
| --- | --- | --- |
| `shandong_ggzy` | 山东省公共资源交易中心 | 搜索时传入配置的起止日期，并复核列表及详情日期 |
| `ccgp` | 中国政府采购网 | 搜索时传入配置的起止日期，并复核列表及详情日期 |
| `zycg` | 中央政府采购网 | 搜索最近一年公告，再按发布日期在程序中筛选；不使用配置的起止日期 |

## 项目结构

```text
tender_practise.py          运行入口
tender_app/
  config.py                站点、关键词、日期、数量和访问控制配置
  app.py                   多站点调度、浏览器运行、统计与导出
  crawlers/                三个站点的搜索和详情适配器
  pagination.py            山东站及中国政府采购网的翻页、重试和断点
  access.py                请求间隔、访问控制、SQLite 缓存和进度存储
  extraction.py            公告字段提取
  field_values.py          金额及日期字段处理
  models.py                记录模型
  collection.py            规范化、去重和导出入口
  exporters.py             按 output_structure.json 生成 JSON
  ocr.py                   图片正文的本地 OCR
tests/                     单元测试
cache/                     SQLite 缓存及断点，运行时使用
results/                   每次运行的结果和日志
output_structure.json      输出结构参考
```

## 安装与运行

在项目根目录打开 PowerShell。需要 Python 3.10 或更新版本，并安装 Playwright 的 Chromium。已有 `.venv` 时可跳过创建虚拟环境：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe tender_practise.py
```

程序会打开可见的 Chromium 浏览器。请保持浏览器运行；遇到验证码或访问拒绝时查看终端及访问控制日志。三个站点均按公开页面自动采集，无终端登录步骤。

## 配置

运行前编辑 `tender_app/config.py`。当前副本的实际配置是：

```python
SITES = ["zycg"]
MAX_SITE_PROCESSES = 2
KEYWORDS = ["招标"]
PER_KEYWORD = 3
START_DATE = "2026-10-13"
END_DATE = "2026-10-13"
SAVE_TEXT_FILES = False
```

因此直接运行时只采中央政府采购网，每个关键词最多保存 3 条；该站使用最近一年的日期范围，不使用上面写死的 `START_DATE` 和 `END_DATE`。要采山东站或中国政府采购网，可将 `SITES` 改为相应站点，或配置为 `["shandong_ggzy", "zycg", "ccgp"]` 同时运行。站点不能重复；多站点使用独立进程，最多同时运行 `MAX_SITE_PROCESSES` 个，单站点内部顺序采集。

`KEYWORDS` 是搜索词列表；`PER_KEYWORD` 是**每个站点、每个关键词、每次运行中新保存记录的上限**，不是网站结果总量，也不能保证达到该数量。命中跨次缓存的公告会跳过详情，不会计入本次新导出数量。不同关键词可能命中同一公告，导出时按来源链接去重。

山东站和中国政府采购网的 `START_DATE`、`END_DATE` 可填 `YYYY-MM-DD`、`today` 或 `yesterday`，起始日期不能晚于结束日期。相对日期在运行时按北京时间解析；补采某个历史日期时应填写具体日期，确保再次运行使用同一个断点键。

两个站点不再使用固定的 15 页或 65 页截断。搜索会继续到数量上限、网站报告的末页、空列表或访问控制停止；网站返回重复列表、页面超时或详情失败时会记录待补采状态。中央政府采购网通过页面“下一页”按钮翻页，至按钮不可用、列表不变或达到数量上限为止。

访问预算在 `config.py` 中配置：每个站点进程最多 `MAX_HTTP_REQUESTS = 7000` 次 HTTP 请求、`MAX_DETAIL_ATTEMPTS = 1500` 次详情尝试。列表页超时最多额外重试 `PAGE_TIMEOUT_RETRIES = 2` 次，重试间隔由 `PAGE_RETRY_BACKOFF_SECONDS = 30` 控制。站点还分别设置了请求间隔和随机抖动；这些限制可能使采集在达到 `PER_KEYWORD` 之前结束。

## 结果、日志与续采

每个站点每次运行写入 `results/时间戳_站点名/`：

- `招投标信息.json`：顶层键为站点配置值；每个关键词对应一个 `samples` 项，公告在 `result.Data` 中。完整正文在记录的 `content_text` 字段；无记录时 `Data` 为空、`VerifyResult` 为 0。JSON 原子替换写入。
- `采集日志.json`：搜索页及公告处理记录。山东站和中国政府采购网逐条记录关键词、页码、页内序号及处理状态；“候选数”不等于成功保存数。
- `采集统计.json`：网站报告总量（无法读取时为 `null`）、发现的链接数、缓存命中数、成功数、失败数、详情尝试数及逐页统计。山东站和中国政府采购网还包含分页进度、待补采页和待补详情；只有 `分页完整` 为 `true` 才表示当前断点没有待补项目。
- `访问控制日志.json`：请求数、详情尝试数、停止原因和访问事件。异常时还可能保存启动诊断或 OCR 图片。

`SAVE_TEXT_FILES = False` 时不生成逐条正文 TXT；设为 `True` 可额外保存。程序不输出 XLSX。

成功记录、七天内的公告缓存、分页进度和待补项目保存在 `cache/access.sqlite3`。山东站与中国政府采购网按**站点、关键词、实际起止日期**保存断点；重新运行相同日期范围时会继续未完成的页面，并尝试补采失败页面及详情。连续三页超时会保留断点并结束本次尝试。若上次运行在导出前意外退出，下次启动同一站点会先从数据库恢复未导出的 JSON。历史结果和缓存不会因修改 `SITES` 自动删除。

## 测试与已知限制

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

单元测试检验本地逻辑，不代表实时网站页面、日期筛选或访问状态始终可用。页面结构变化可能导致候选列表或正文解析失败；请结合 `采集日志.json` 和 `采集统计.json` 判断本次是否采全，不能仅凭导出条数判断。
