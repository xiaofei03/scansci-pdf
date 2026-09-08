---
name: scansci-pdf
description: "Download scholarly PDFs in batches and attach verified files to a specified Zotero collection. Uses Zotero full-text lookup, lawful OA/publisher fallback, and optionally authorized AbleSci requests. Supports resumable jobs and duplicate/budget guards. Full desktop workflow currently requires macOS."
---

# Scholarly PDF acquisition

Use the included scripts, not code stored in a manuscript project. Resolve all paths
relative to this SKILL.md. Do not assume another computer has the same paths, login
state, collection, MCP server, or installed Python environment.

## Fast route

1. Read [references/local-batch-pipeline.md](references/local-batch-pipeline.md).
2. Confirm the DOI list and exact target Zotero collection. If the user only gives
   titles, first resolve/verify bibliographic identity using available scholarly
   tools; follow any project journal-screening rules before recommending papers.
3. On a new device run `scripts/doctor.py`; install declared dependencies into a
   dedicated environment if needed. Do not repeatedly rediscover a working setup.
4. Run `scripts/batch.py` once for the batch:
   **Zotero full lookup → OA/publisher fallback → AbleSci → validation → managed PDF**.
   Reuse the same work directory to resume and preserve the cumulative point ledger.
5. Return completed, waiting and needs-review counts. Only a real validated
   Zotero-managed attachment counts as success, not a metadata-only item.

## Authority and exception boundaries

- Keep real batches outside the skill/repository, preferably in the project's
  `_work/<batch>/`. Do not modify manuscripts or delete files.
- New AbleSci requests require explicit per-paper and cumulative batch budgets.
  Defaults authorize no spending; default actual reward is 10 points, distinct
  from the maximum. Do not infer permission to increase rewards.
- CAPTCHA, login, second-factor prompts, unclear PDF identity, existing closed
  requests, ambiguous duplicates and unresolved writes require review.
- Only lawful OA/authorized sources; no access-control bypass or cookie export.
- Browser profile logins and Zotero local data stay on the device. Skill installation
  neither installs Zotero nor authenticates accounts nor enables automatic monitoring.
- Full workflow uses macOS Apple Events and a bounded Zotero Run JavaScript launch.
  Windows/Linux are not supported by this desktop adapter. Current menu labels
  were tested with Chinese Zotero; other locales may need explicit adaptation.
- Existing native file-copy stalls are avoided with a scoped temporary loopback PDF
  stream. Never install a generic unauthenticated privileged JS bridge.
- Reusable runner does not automatically accept/reject AbleSci uploads. Human
  response time prevents a universal five-minute guarantee.

## Optional external tools

For an explicitly requested capability beyond the included runner (e.g. external MCP
search or citation export), discover available tools first. Only then consult
[references/external-mcp-catalog.md](references/external-mcp-catalog.md) as a historical
catalog, not proof that those tools or parameters are installed. Do not load it for
ordinary batch downloads.
