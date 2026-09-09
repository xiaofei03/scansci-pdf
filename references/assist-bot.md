# Scripted AbleSci helping mode — experimental

## Current evidence and limits (2026-09-09)

`scripts/assist_bot.py` is a standalone Python 3.10+ Unix runner (macOS/Linux;
uses `fcntl` for single-process ownership). It needs the existing pypdf dependency,
but its default HTTP transport needs no Chrome, Apple Events, Zotero, GUI, or model loop. Windows is not
supported by its current lock implementation. Installing the skill does not start it.

Optional `--browser-session` reuses an existing logged-in Chrome tab on macOS using
bounded same-origin fetches via Apple Events. It does not click, focus, navigate,
read/export cookies, or change browser settings. Prefer it for agent-operated local
tests when the user does not want repeated terminal logins. This mode needs Chrome
running and existing Apple Events JavaScript permission; it is not headless/cloud mode.

Verified so far:

- User-provided terminal output confirms two successful pure-HTTP `login-check`
  runs with `logged_in: true`, `session_persisted: false`, `uploaded: 0`.
  This verifies login, not an active session available to another process or upload.
- Agent-operated Chrome-session login and `/my/home` public account-ID verification
  succeeded. A later user run's bare `TimeoutError` cannot be localized retrospectively:
  the ledger was empty. Current direct HTTP login-page GET also succeeded, so an
  enduring login-page outage was not established.
- A bounded 6-minute Chrome-session run exited normally: 3 cycles, 9 candidates,
  5 without sufficient sharing-license evidence and 4 ineligible. No timeout,
  upload or points spending occurred. The 134-test regression suite passed.
- A further 90-second run using automatic tab selection also exited normally.
  Across both runs: 28 candidates, zero uploads. One PDF lookup ended at the run
  deadline, not a site timeout. This now checkpoints `run_deadline` for immediate
  next-run resumption instead of misclassifying it as a six-hour-backoff failure.
- Real public waiting-list extraction, excluding pinned notices and already-uploaded
  list entries; real detail DOI/title/owner parsing.
- Real AlphaFold PDF acquisition through Unpaywall/publisher, 12 pages, DOI/title/
  first author/readability checks, Crossref VOR CC BY evidence and matching license
  URL in the actual PDF. This was a download-only test, not an upload.
- Unit tests for identity, own-request exclusion, notes/supplement review, changed
  bytes, MD5 fast-upload acknowledgement, CAPTCHA stops and uncertain-write dedupe.
- Saved ready/retryable jobs resume even after leaving the newest list page. Real
  subprocess tests verify stop/deadline termination and reaping, not just mocked flags.
- The upload handshake was read from the site's logged-in form using a one-time
  read-only inspection of the existing local browser session, without cookie export.

NOT yet verified: real storage upload/callback, per-upload acceptance/points reconciliation, cloud-network behavior,
or a 5–24-hour soak test. Thus it is **not yet a proven production points bot**.
No live upload or recurring deployment was performed during this implementation.

## Rights and site boundaries

Finding/downloading a PDF does not establish redistribution rights. Helping mode
requires already-effective Crossref version-of-record CC BY/BY-SA/CC0 evidence,
matching license text in the PDF, exact DOI/title/first-author checks and unchanged
bytes. Attribution/copyright/license notices remain in the original file.
Unknown rights, subscription-only access, bare OA flags and ambiguous manuscript
versions are skipped. No paid relay via new AbleSci requests; no own-request helping,
multiple-account points loops, CAPTCHA solving, proxy rotation or access bypass.

The site's copyright declaration requires the helper to have appropriate sharing
rights: https://www.ablesci.com/post/detail?id=07ya7J . Public upload code does not
establish official permission for unattended bots. Confirm site acceptance of the
intended scale before long-running deployment; respect account/server limits.

## Commands

Use a private work directory **outside the skill repository**. In this manuscript
project use `_work/<run>/`. Keep that directory stable for all resumes. Do not share
it between cloud and local processes concurrently; the local lock is not distributed.

`login-check` uses only the Python standard library. Its success does not prove that
the selected Python has pypdf for real downloading. `run`/`resolve`/`reconcile` now
check the declared pypdf version before authentication/network work; use the skill's
dedicated environment rather than assuming the system `python3` has dependencies.
Once login-check has succeeded, proceed to a bounded `run`; do not ask the user to
repeat login-only checks unnecessarily. Pure HTTP runs need in-memory authentication;
local Chrome-session runs use the browser's existing authenticated session.

```sh
# Read-only list; creates a local ledger but does not log in/upload.
python3 scripts/assist_bot.py scan --work-dir /private/path/assist-job

# Check script login only: no downloads or uploads. Credentials stay in memory.
python3 scripts/assist_bot.py login-check --work-dir /private/path/assist-job --prompt-login

# Download and verify one DOI, without helping or importing into Zotero.
python3 scripts/assist_bot.py resolve --work-dir /private/path/assist-job/papers \
  --doi 10.1038/s41586-021-03819-2

# Observe and prepare candidates for a limited run, WITHOUT uploads.
python3 scripts/assist_bot.py run --work-dir /private/path/assist-job \
  --own-user-id YOUR_PUBLIC_USER_ID --hours 0.1 --max-items-per-cycle 3

# Explicit live helping: enter credentials in your own terminal, never in chat.
# Run a short supervised test first. No successful live test has yet been recorded.
python3 scripts/assist_bot.py run --work-dir /private/path/assist-job \
  --own-user-id YOUR_PUBLIC_USER_ID --hours 0.1 --max-daily-uploads 1 \
  --allow-upload --prompt-login

# Local agent-operated test: no repeated password entry, no screen clicking.
python3 scripts/assist_bot.py run --work-dir /private/path/assist-job \
  --own-user-id YOUR_PUBLIC_USER_ID --hours 0.1 --max-daily-uploads 1 \
  --max-items-per-cycle 3 --allow-upload --browser-session

python3 scripts/assist_bot.py status --work-dir /private/path/assist-job

# Public read-only observations for existing uploads/uncertain writes; never reposts.
python3 scripts/assist_bot.py reconcile --work-dir /private/path/assist-job
```

`YOUR_PUBLIC_USER_ID` is the `id` in your own public profile URL, not your nickname
or email. Before any live run the runner now requires the unique own-profile link
on authenticated `/my/home` to match that ID. Wrong-account/ambiguous pages stop.
Chrome mode pins one existing AbleSci tab by window/tab IDs; multiple homepage tabs
are allowed. `--browser-tab-url` optionally selects a unique exact existing URL.
Closing/navigating the pinned tab can stop the run; it never silently switches tabs
mid-request. Browser mode and `--prompt-login` are mutually exclusive.

Credentials: local `--prompt-login` keeps them in memory only. Cloud deployments
may inject `ABLESCI_USERNAME`/`ABLESCI_PASSWORD` into the process from a secret
manager; the password is removed from the process environment before lookup child
processes launch. Do not put credentials in command arguments, repository files,
job JSON, shell history, or plaintext cookie files. Cookies exist in memory only;
after restart, authenticate again. Do not bypass a login challenge.
The login-only check also ends its in-memory session when it exits; `run` must log
in again. Hidden prompts require a real private terminal and never fall back to
echoed pipe input. Positive authenticated navigation evidence is required after
login; a success code followed by a maintenance page is not successful login.

Existing OpenAlex/Unpaywall/Elsevier settings are reused. No credential is migrated
with this repository. OpenAlex/Unpaywall are discovery indexes, not universal
full-text subscriptions. Institutional access does not by itself authorize sharing.

## Runtime, checkpoints and upload contract

- `--hours 5` or `--hours 8` bounds one invocation. For a 24-hour invocation use
  `--hours 24`; this does not configure a scheduler or claim continuous uptime.
- Default scan interval 120 s (minimum 30); site calls at least 3 s apart; default
  10 candidates/cycle and 10 attempted uploads/UTC day. These are conservative local
  controls, **not verified site quotas**. A stop signal or elapsed run deadline stops
  new work. Active site network calls can take their bounded 25/60 s to return.
- Safe request-phase/method/route/timing logs are emitted and appended to private
  `runtime.jsonl` in the work directory; request bodies, cookies, CSRF and upload
  tickets are excluded. Read timeouts retry once; write timeouts never retry.
  Browser fetches have a 30 s abort timer and a bounded polling window. An uncertain
  upload remains reserved in the ledger even if the browser context disappears.
- Per-paper discovery/validation runs in a child process with a 120 s ceiling.
  Cached downloaded files remain for revalidation. Failed PDF lookups may retry
  after six hours; rights/identity/schema review states are not blindly retried.
  Stop/deadline termination reaps the child; a non-responsive child is killed after
  a three-second graceful termination window. No new lookup begins after stopping.
  With less than five seconds of run time left, defer the candidate without starting
  a lookup. `run_deadline` jobs resume next invocation, without the failure cooldown.
- Saved ready candidates are served before new list entries in live mode. They are
  rechecked against the current request and freshly resolved/revalidated using the
  existing file cache. Dry runs do not repeatedly process already-ready entries.
- SQLite ledger plus OS process lock prevents same-work-directory overlap.
  Do not create a new work directory to evade an uncertain write.
- Recheck current request status/DOI/title immediately before upload. Requests with
  arbitrary extra notes are conservatively skipped rather than assuming a standard
  article satisfies a special request. Uploaded/completed/own requests are skipped.
- `/assist/upload-request` accepts CSRF, request ID, filename, MD5 and byte size.
  Its code `10` means MD5-based upload already completed. Record `posting_uncertain`
  **before this request**, not just before the subsequent file transfer.
- Code `0` returns a short-lived signed storage ticket; multipart fields follow
  the observed SimpleUpload form. Credentials/CSRF/cookies are never sent to storage.
  HTTPS, public-address checks, no redirects and response-size limits apply.
  `--upload-host EXACT_HOST` optionally pins storage hostnames after live observation;
  otherwise the HTTPS host supplied by the authenticated site ticket is used.
- Code `2`, malformed responses, login/403/429 challenges or ambiguous writes stop
  the process with exit 2. Signed tickets are not logged/persisted. A transport
  failure is not evidence that no upload occurred; **never auto-repost it**.
- `uploaded` means a successful server acknowledgement, not acceptance or points
  earned. `reconcile` and live loop observations save remote request state and DOI
  agreement without changing a local uncertain/acknowledged state. A request being
  closed alone does not identify the accepted uploader. Exact file-level acceptance
  and points tracking remain incomplete; do not manufacture earnings metrics.

Before cloud deployment: validate one real upload, verify account exclusion and
storage hostname, then run a short bounded batch and 5-hour soak. Use persistent
private storage, network egress rules blocking private/metadata networks, a non-root
service user, secret injection and controlled log retention. Existing free-source
transport does not pin DNS across all redirects; cloud egress controls matter.
Do not install an automatic restart-on-error loop: login, verification or uncertain
writes require review. A service supervisor must not treat exit 2 as retryable.

## Open-source inspiration

- https://github.com/LucasLin21/ablesci-assist-guard (MIT): list filtering,
  status recheck and human-in-loop helping. Its documented workflow is not automatic
  uploading. Our server runner is an independent implementation, not this extension.
- https://github.com/hai178912522/ablesci-cli (MIT): CLI/session/CSRF organization;
  primarily a **requester** client. We do not adopt its persistent-cookie design.
- https://github.com/Shawp1n/literature-helper (MIT): resumable state concepts;
  not evidence of an existing production helping daemon.

No third-party source code was vendored/copied. Protocol fields came from the site's
observed upload form. No unlicensed project code is incorporated.
