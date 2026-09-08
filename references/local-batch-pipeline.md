# 批量下载与跨设备安装

所有脚本均在本 skill 的 `scripts/`，不依赖任何论文项目。
完整桌面流程当前支持 macOS；测试环境为 Zotero 9.0.6（中文菜单）和 Chrome。
Python 3.10+；运行依赖见 requirements.txt，测试依赖见 requirements-dev.txt。

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

上限不等于悬赏额：总入口仍默认每篇悬赏 10 积分，不自动加价；用户明确要求
其他悬赏额时，可用 `ablesci.py prepare --points N` 后单独 submit，不能假称
总入口已提供自动加价。无预算仍可走前两层并列出缺失清单。

## 续跑和质量门槛

- 重用同一工作目录；成功项跳过，未完结科研通项续查同一求助，PDF 已下载则补挂。
- 新求助预先记账，结果不确定则不重发。历史已关闭且无人上传/积分退回的求助，
  只有获得明确许可后才使用 `--approved-closed-request-url` 重发。
- 默认对每篇科研通求助限时等待 45 秒，每 30 秒检查；到期保留 waiting 状态，
  不承诺自动持续监控。重新执行同批次继续；长期定时任务需单独配置产品调度器。
- 不自动采纳或驳回文件。验证码、登录、多个候选附件、存疑文献、未结束的写操作
  需要介入，不得把异常当作下载成功。
- 成功需：题名、DOI、第一作者、已知页数下限、可读性核验，以及实际 Zotero
  管理文件和哈希验证。不是仅有元数据，也不是对出版商原件逐页完整性认证。
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

2026-09-08 在原测试环境跑通五篇管理附件，其中一篇经过真实科研通人工补缺；
两篇延迟附件通过本机流成功补挂；重复运行不产生重复条目/附件。另有隔离单元测试。
这些结果不代表任意网站、任意系统或任意批量都能成功，也不能保证人工应助五分钟内完成。
