# 批量下载与跨设备安装

所有脚本均在本 skill 的 `scripts/`，不依赖任何论文项目。
完整桌面流程当前支持 macOS；测试环境为 Zotero 9.0.6（中文菜单）和 Chrome。
Python 3.10+；运行依赖见 requirements.txt，测试依赖见 requirements-dev.txt。
免费来源的邮箱/API 设置、超时与重试说明见 [free-sources.md](free-sources.md)。
设置一次后各批次自动读取；无须每次给模型传递密钥。
常规免费来源失败后，默认尝试经 Crossref 开放许可验证的 Sci-Hub OA 副本补缺；
不适用于许可不明或仅订阅可读的论文，不需要 API key，不操作浏览器。
全批禁用可设置 `SCANSCI_SCIHUB_OA=0`；许可、超时及失败边界见 free-sources.md。

## 首次配置

在安装目录创建专用环境并检查（无需改动系统 Python）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/doctor.py
```

doctor 只检查系统、依赖、Zotero 本地接口并列出候选 prefs.js 路径，不更改配置、
不读取或输出密码/cookie、不自动点击。用户需要安装并启动 Zotero、开启本地 API，
在个人库中选择明确的目标分类。开启科研通补缺时，还需打开唯一一个已登录的
Chrome 科研通标签，允许 Chrome 接受 Apple Events JavaScript，并授予必要的
macOS 自动化/辅助功能权限；桌面应保持解锁。

## 执行

将 DOI 清单、输出和批次状态保存在 skill 外部。一个 UTF-8 文本文件每行一个 DOI。

```sh
.venv/bin/python scripts/batch.py \
  --collection COLLECTION_KEY \
  --prefs '/absolute/path/to/prefs.js' \
  --work-dir '/absolute/project/_work/pdf_batch' \
  --doi-file '/absolute/path/to/dois.txt'
```

启用科研通时追加以下参数（数字仅示例，必须有本批授权）：

```sh
--ablesci --downloads-dir '/absolute/browser/download/directory' \
--approved-per-paper-points 50 --approved-total-points 300
```

预算授权后默认优先高速下载，当前支持的每文件 2 积分也计入上述上限；
只使用免费线路时追加 `--no-fast-download`。用户明确授权验证后采纳时，追加：

```sh
--approved-accept-verified --until-complete --max-run-seconds 3600
```

默认预检只要求目标目录存在且可写；加 `--preflight-only` 可在任何导入/扣费前检查。
网站传输完成但没有本地 PDF 时，`save_received_blob.py` 自动保存当前页面唯一可见
“手动保存文件”链接中已交付的 blob，再走相同的身份核验。无需开启多文件下载许可、
无需另存为对话框、不重新请求 CDN、不增加扣费，也不更改浏览器安全配置。
只有需要诊断浏览器原生保存功能时才额外指定 `--chrome-preferences`；它启用严格的
只读站点许可/另存为/目录检查，不是默认导出路径的必要配置。有多个 profile 时不得猜测。
常规批量禁止临时调用 CUA/截图点选救场；明确返回阻塞原因，保留账本后原目录续跑。
脚本仍有桌面依赖，不等于无浏览器、无窗口的纯 API 运行。
批次退出码：0 = 全部完成（或 preflight-only 检查通过），2 = 配置/人工复核阻塞，
3 = 未完成但可续查，1 = 未捕获运行错误。不要把进程退出或单元测试通过当作下载成功。

这会在同一个受锁保护的进程内续查，不创建定时任务。下载阶段默认可等待 900 秒
（`--download-wait-seconds`），有传输进展不切页，明确失败才限次换普通线路。

网站强制最低悬赏超过原约定时，先确认该篇实际悬赏与总费用。明确允许后，可用
`--paper-budget DOI=52 --approved-site-minimum` 为指定论文设置例外上限；
这不增加整批预算，也不代表可以把其他论文的悬赏统一提高。

上限不等于悬赏额：总入口仍默认每篇悬赏 10 积分，不自动加价；用户明确要求
其他悬赏额时，可用 `ablesci.py prepare --points N` 后单独 submit，不能假称
总入口已提供自动加价。无预算仍可走前两层并列出缺失清单。

## 续跑和质量门槛

- 重用同一工作目录；成功项跳过，未完结科研通项续查同一求助，PDF 已下载则补挂。
- 若剩余项全部是跨分类/重复条目/文件复核等需用户决策的状态，直接以待复核退出，
  不继续每 30 秒空轮询；仍有下载、应助或写入进行时不套用这个停止条件。
  跨分类条目保留原分类，只有用户授权后才加入目标分类，不创建副本。
- 新求助预先记账，结果不确定则不重发。历史已关闭且无人上传/积分退回的求助，
  只有获得明确许可后才使用 `--approved-closed-request-url` 重发。
- 默认对每篇科研通求助一轮等待 45 秒，每 30 秒检查；`--until-complete` 继续后续轮次，
  到运行时限保留状态。观察工具超时不等于进程退出，不可重复启动。跨会话定时监控
  仍需产品调度器，不能声称退出后自动持续运行。
- 仅 `--approved-accept-verified` 允许验证后采纳；不自动驳回。验证码、登录、多个候选附件、存疑文献、未结束的写操作
  需要介入，不得把异常当作下载成功。
- 成功需：题名、DOI、第一作者、已知页数下限、可读性核验，以及实际 Zotero
  管理文件和哈希验证。不是仅有元数据，也不是对出版商原件逐页完整性认证。
- 页数少于期刊版不能自动当作作者稿放行。用户明确接受作者版本后，核对全体作者、
  版本日期、连续页码、结论/参考文献结尾并抽查渲染；将检查结果保存在该任务
  metadata.reviewed_author_version（精确 SHA-256、pages、user_approved、
  pagination_checked、ending_checked、review_notes）。只对这个哈希生效，
  仍标为 probably_correct / author_manuscript，附件名称明确写 Author Manuscript。
  首页冲突 DOI、不可读文件或身份不符不得通过版本例外绕过。
- 直接 importFromFile 曾在测试机卡住，现用临时 127.0.0.1 随机路径 PDF 流和
  Zotero importFromURL。启动一次内置 Run JavaScript，结束只关闭 UUID 匹配的
  自有窗口；会覆盖剪贴板为生成代码，但不读取或保存原剪贴板。

## 搬到另一台电脑

安装 skill 只迁移程序。Zotero/浏览器/依赖/授权需在新机配置，先运行 doctor。
首次只用小测试集合验收，不直接启动大批积分求助。Windows/Linux 不支持完整桌面流程。

不要通过公开 GitHub 同步真实批次状态。要接着处理同一批，须私下迁移 jobs.sqlite
和所需文件、保留积分账本，并人工核对旧绝对路径、Zotero 条目/分类标识与当前设备
是否一致。当前版本没有自动跨设备状态迁移器；不能把新目录当作原批次续跑。
不可两台电脑同时操作同一批求助。

## 验证范围

2026-09-09 九篇测试最终全部在指定 Zotero 分类下有已验证的管理附件：复用一篇，
新增八篇；其中七篇经科研通补缺，两篇为明确标注的作者稿。最后一篇真实使用了
已交付 blob 的脚本导出，并成功导入/采纳。这是经过调试协助完成的批次，不是全批
无人干预测速；独立导出路径已经接入常规总入口。不能据此保证下一批零异常。

2026-09-08 在原测试环境跑通五篇管理附件，其中一篇经过真实科研通人工补缺；
两篇延迟附件通过本机流成功补挂；重复运行不产生重复条目/附件。另有隔离单元测试。
这些结果不代表任意网站、任意系统或任意批量都能成功，也不能保证人工应助五分钟内完成。
