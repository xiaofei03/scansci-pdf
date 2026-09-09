---
name: scansci-pdf
description: "Download scholarly PDFs in batches and attach verified files to a specified Zotero collection. Uses Zotero full-text lookup, lawful OA/publisher fallback, and optionally authorized AbleSci requests. Supports resumable jobs and duplicate/budget guards. Full desktop workflow currently requires macOS."
---

# Scholarly PDF acquisition

## Separate helping mode (experimental)

When explicitly asked to **answer other users' AbleSci requests**, read
[references/assist-bot.md](references/assist-bot.md) and use `scripts/assist_bot.py`.
This is separate from requesting PDFs for Zotero: it does not import, spend request
points, use browser control, or automatically start a service. The Unix headless
runner is implemented, but authenticated upload and extended unattended operation
have not yet been verified live. Do not present a scan/download test as proof of
successful helping. Only verified redistributable, unmodified files qualify.

Use the included scripts, not code stored in a manuscript project. Resolve all paths
relative to this SKILL.md. Do not assume another computer has the same paths, login
state, collection, MCP server, or installed Python environment.

## Script-only operating contract

For routine batches, use the packaged CLI and its machine-readable results. Do not
fall back to CUA, screenshots, ad-hoc AppleScript clicks or per-paper model-guided
browser manipulation. Known exceptions belong in tested reusable scripts. A blocked
result means checkpoint and ask for the exact setup/authority needed, not GUI rescue.
The current CLI still automates Chrome and Zotero through Apple Events; it is not
a headless API-only implementation. Do not claim zero failures or universal unattended
completion. One-time permissions, login and ambiguous manuscript versions can require
user action. Do not weaken identity checks or change security settings to meet a timer.

## Fast route

1. Read [references/local-batch-pipeline.md](references/local-batch-pipeline.md).
2. Confirm the DOI list and exact target Zotero collection. If the user only gives
   titles, first resolve/verify bibliographic identity using available scholarly
   tools; follow any project journal-screening rules before recommending papers.
3. On a new device run `scripts/doctor.py`; install declared dependencies into a
   dedicated environment if needed. Do not repeatedly rediscover a working setup.
   For free-source setup, authentication failures or efficiency work, read
   [references/free-sources.md](references/free-sources.md). `free_sources.py --status`
   reports presence only; `--configure` securely prompts for local settings.
   TLS record errors have one verified TLS 1.2 compatibility retry; certificate
   failures are not bypassed. Explicit 403 browser challenges skip the host and
   continue other sources; they are not reported as missing bibliographic records.
4. Run `scripts/batch.py` once for the batch:
   **Zotero full lookup → OA/publisher fallback → AbleSci → validation → managed PDF**.
   Reuse the same work directory to resume and preserve the cumulative point ledger.
   Free fallback now uses bounded network workers, Unpaywall/OpenAlex discovery,
   public publisher links and optional entitled Elsevier PDF retrieval.
   After those sources fail, a license-gated Sci-Hub OA-copy adapter is enabled by
   default; unknown/subscription-only rights are skipped. No API key is needed.
   It receives its own 60-second budget, independent of primary lookup elapsed time;
   `SCANSCI_SCIHUB_SECONDS` sets a different positive duration.
   See free-sources.md for the narrow license gate and opt-out; this is not a
   subscription-access route.
   Credentials are optional and never inherited from a Git repository. After changing credentials,
   `--retry-free` explicitly retries missing jobs without existing AbleSci requests.
   For an explicitly requested unattended batch, use `--until-complete` and a
   suitable `--max-run-seconds`; observe the live process, not repeated fresh runs.
   Each request's uploader wait defaults to 300 seconds across polling rounds
   (`--ablesci-wait-seconds`). On expiry, preserve the request/budget and stop waiting
   for that item in this invocation; no automatic repost, cancellation or new timer.
   A later explicit resume checks the same request with a new bounded window.
   This limit does not cancel an active PDF transfer or its validation/import.
   Preflight checks the explicit writable `--downloads-dir`. Completed transfers
   without a local file export the already delivered visible blob link automatically;
   no native Save As/multiple-download permission is needed for that export.
   Use `--preflight-only` to check setup without running.
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
  If the completed page has no unique supported delivered blob link, checkpoint
  for review; do not improvise clicks or security changes. A completed web transfer
  with no validated local file is not success and must not be re-billed.
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
