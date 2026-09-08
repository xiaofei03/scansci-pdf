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
   For an explicitly requested unattended batch, use `--until-complete` and a
   suitable `--max-run-seconds`; observe the live process, not repeated fresh runs.
5. Return completed, waiting and needs-review counts. Only a real validated
   Zotero-managed attachment counts as success, not a metadata-only item.

## Authority and exception boundaries

- Keep real batches outside the skill/repository, preferably in the project's
  `_work/<batch>/`. Do not modify manuscripts or delete files.
- New AbleSci requests require explicit per-paper and cumulative batch budgets.
  Defaults authorize no spending; default actual reward is 10 points, distinct
  from the maximum. Do not infer permission to increase rewards.
- Prefer the site's high-speed download by default once batch point caps are
  authorized. Its currently supported 2-point fee counts inside both caps; never
  silently accept a changed price. `--no-fast-download` chooses free routes.
- User-approved `--approved-accept-verified` permits acceptance only after exact
  PDF validation. It is needed for unattended continuation when the website blocks
  new requests until earlier uploaded files are handled. Do not accept unverified
  files or interact with requests outside the current batch.
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
- Without acceptance authorization, pause at that gate; never auto-reject uploads.
  Chrome's blocked/multiple-download permission also requires user action. A
  completed web transfer with no local file is not success and must not be re-billed.
  Human response time and network throughput prevent a universal five-minute guarantee.
- A slow active transfer is progress, not failure. Do not navigate from its page.
  Read [references/ablesci-state-machine.md](references/ablesci-state-machine.md)
  when diagnosing transfer, browser-dialog, budget or acceptance exceptions.

## Optional external tools

For an explicitly requested capability beyond the included runner (e.g. external MCP
search or citation export), discover available tools first. Only then consult
[references/external-mcp-catalog.md](references/external-mcp-catalog.md) as a historical
catalog, not proof that those tools or parameters are installed. Do not load it for
ordinary batch downloads.
