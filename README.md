# scansci-pdf — Zotero PDF 批量下载 skill

**Zotero 查找全文 → OA/出版商补缺 → 科研通补缺 → PDF 核验 → 指定 Zotero 分类。**

这是可安装的 Codex skill 与自带脚本，不是外部同名 MCP 服务器的源码或完整发行版。
不需要下载任何论文项目。完整桌面流程当前只支持 **macOS + Zotero + Chrome**；
Zotero 菜单自动化在中文界面测试过，Windows/Linux 适配尚未实现。

## 安装

在新设备的 Codex 中请求：

> 用 skill-installer 从 https://github.com/xiaofei03/scansci-pdf 安装根目录的 skill，名称 scansci-pdf。

如果同名 skill 已存在，先确认替换/升级方案，不要强制覆盖。也可以 git clone 此仓库，
再将完整目录安装到当前 Codex 支持的个人 skills 目录。保留 SKILL.md、scripts/、
references/ 和依赖清单，不能只下载一个 SKILL.md。

安装后按 [首次配置与使用](references/local-batch-pipeline.md) 创建环境，运行：

```sh
python3 scripts/doctor.py
```

随后可以说：

> 下载这份 DOI 清单，导入 Zotero 的「我的研究」分类。允许科研通每篇最多 50 积分，本批最多 300 积分。

注意：50/300 是授权上限的示例，默认悬赏仍是每篇 10 积分，不自动提高。
登录、验证码、权限、存疑 PDF、人工应助等待仍可能需要介入。不会自动持续监控，
也不自动采纳/驳回科研通文件，不能保证所有文献在五分钟内获取。

## 验证与隐私

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s scripts -p test_pipeline.py
```

测试 PDF 为本地生成的虚构样本；测试不发真实求助、不扣积分。真实数据请保存在
仓库外的项目 _work 目录。gitignore 排除了常见数据文件，但上传前仍应检查清单。
不要上传 PDF、论文、Zotero 数据库、登录状态、API 密钥或批次账本。

支持断点续跑与查重；跨设备安装不等于自动迁移已运行批次。迁移同一批时须安全保留
账本并重新核对路径与 Zotero 标识，避免重复发布或重置累计积分。

旧的外部 MCP 工具说明保存在 references/external-mcp-catalog.md，仅供按需核对，
不代表已安装或已测试这些外部服务。正常下载以本仓库的 batch.py 为准。
