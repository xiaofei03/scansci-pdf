# AbleSci execution states and verified lessons

The site is controlled through the user's logged-in Chrome tab and visible DOM.
No cookies, private site APIs, encrypted-file tricks, CAPTCHA bypass or permanent
privileged bridge are needed. Keep one batch process; the work-directory lock
prevents another runner from posting the same requests concurrently.

## Progression

Metadata and target identity → permitted lookup exhausted → request prepared →
points reserved → submitted → waiting for uploader → file offered → transferring →
local PDF validated → Zotero-managed attachment validated → authorized acceptance.

Resume the existing state. Never classify metadata-only or offered files as success.
Uncertain submission counts against the budget and must be reconciled, not reposted.
Downloaded PDFs can be picked up by identity after restart; don't require the whole
Downloads directory to contain exactly one new PDF.
Request-detail pages need a real refresh on each bounded waiting poll; their old
DOM can still say “waiting” after an upload has arrived. Refresh only request pages,
never the active download page. Do not infer uploader delay from a stale snapshot.

Uploader waiting is capped at 300 seconds per request per runner invocation, shared
across all short polling rounds. Expiry checkpoints the existing request and points,
skips further polls for that item, and exits partial when no other work remains.
It never cancels a request, restarts its reward, or interrupts an active transfer.
A later explicit resume may check the same request with a fresh bounded window.

## Transfer behavior

- `/assist/download` is a transfer page, not necessarily a direct PDF response.
  Ordinary transfers observed at around 20 KB/s can take several minutes. Preserve
  the tab and watch progress; the old 45-second assumption was incorrect.
- Inspect the transfer panel, excluding FAQ text. A FAQ heading mentioning
  “download failure” does not mean the current transfer has failed.
- Prefer high-speed within authorized caps; reserve its cost before clicking and
  wait for the asynchronously displayed confirmation. A button click does not prove
  a file has arrived. A changed fee requires renewed authority.
- A disabled high-speed button during initialization is not an attempted route.
  Wait for readiness without reserving points or marking the route as tried.
  Empty read-only snapshots may be retried briefly; never replay a write merely
  because its response was empty.
- A failed high-speed CDN is not proof the file is unavailable. Use the page's
  ordinary route buttons. No same-node infinite retries; persist tried routes.
- Do not navigate while transferring. The browser's beforeunload warning prevents
  accidental cancellation. Only after validating the exact job's PDF may an owned
  obsolete transfer be left via its normal native confirmation.
- An observation timeout does not kill the browser transfer. Return a pending
  state and continue observing the same job; do not start another request over it.
- Unknown dialogs, authentication gates, changed file identity and unresolved
  spending are review states, not permission to click through.
- Chrome can block multiple downloads after the site has received 100% of a PDF.
  Collect and validate existing local files first. Otherwise `save_received_blob.py`
  exports the unique visible “手动保存文件” blob on the exact recorded completed page.
  It fetches only already delivered same-site blob bytes, not a publisher/CDN URL;
  a bounded owned temporary cache transfers them to a hash-named local PDF.
  Validate identity before attachment/acceptance. No Save As, permission changes,
  extra fee or transfer restart. A missing/ambiguous/non-blob link or invalid PDF
  is a review state, not permission for GUI rescue. Pending PDF reviews are not
  retried by exporting the same bytes again.

## Acceptance and budget

Observed account policy: below 500 points, uploaded files may need handling before
new requests are allowed. This is a website rule, not an HTTP/JSON error. When the
user authorizes acceptance for the batch, validate first, accept the exact matching
request, verify its accepted/completed status, then continue. Never use acceptance
as a way to skip file verification. Without that authority, report the specific gate.
This is not evidence that accounts below 500 cannot process a batch, or that higher
balances permit unlimited simultaneous requests. Follow the actual create-page gate;
do not hard-code eligibility from a guessed balance or assume the exact 500 boundary.

Reward and high-speed costs share one cumulative budget. Default reward remains
10, not the per-paper cap. Network failures are not presumed refunded. Resume the
same ledger; moving to another directory must not reset spending authority.

Check the actual visible confirmation price, not only the page's advertised price.
Unknown payment results retain their reservation and require reconciliation; never
confirm again while counting only one charge. Insufficient fast-route budget may
fall back to the free route, but does not authorize a higher cap. A mandatory site
minimum needs explicit authorization; a per-DOI exception leaves the batch cap intact.

Treat “下载已完成 / 浏览器已发起保存” as completion too. If the saved PDF fails checks,
report the validation issue rather than restarting a completed transfer. Author
versions with different pagination require an explicit, hash-bound review described
in local-batch-pipeline.md; don't weaken the general page-count or DOI gates.

Zotero script termination is not attachment success. Check the per-job managed PDF
state before acceptance; persist success when a resumed attachment already exists.
Resume pending Zotero report handles instead of launching another write. Prioritize
the currently owned transfer ahead of earlier requests still waiting for uploads.
When acceptance was requested, include its confirmed completion in the final gate.
Keep a pending import's scoped local stream alive until the same report terminates.
Metadata writes are also resumable: an uncertain saveItems must be reconciled by
identity, not sent again under a new session. Do not touch the browser at all when
the requested batch and any authorized acceptance are already complete.

## Desktop handling

Zotero can be open while its library window is hidden. Restore the library before
opening Tools → Developer → Run JavaScript. Close the exact UUID-matched owned
script window through its close button, then verify disappearance. Command-W alone
can target another window or fail to close the intended one.

## Performance claims

Report original vs assisted runs, new vs reused PDFs, metadata vs managed attachments,
and lookup vs uploader-wait vs transfer time. A nine-paper assisted recovery is not
proof of an arbitrary future batch completing unattended. Verify the improved runner
on real remaining papers and retain regression tests for the observed failure states.
