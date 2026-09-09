# Headless AbleSci helping mode — experimental

## Current evidence and limits (2026-09-09)

`scripts/assist_bot.py` is a standalone Python 3.10+ Unix runner (macOS/Linux;
uses `fcntl` for single-process ownership). It needs the existing pypdf dependency,
but no Chrome, Apple Events, Zotero, GUI, or model loop at runtime. Windows is not
supported by its current lock implementation. Installing the skill does not start it.

Verified so far:

- Real public waiting-list extraction, excluding pinned notices and already-uploaded
  list entries; real detail DOI/title/owner parsing.
- Real AlphaFold PDF acquisition through Unpaywall/publisher, 12 pages, DOI/title/
  first author/readability checks, Crossref VOR CC BY evidence and matching license
  URL in the actual PDF. This was a download-only test, not an upload.
- Unit tests for identity, own-request exclusion, notes/supplement review, changed
  bytes, MD5 fast-upload acknowledgement, CAPTCHA stops and uncertain-write dedupe.
- The upload handshake was read from the site's logged-in form using a one-time
  read-only inspection of the existing local browser session, without cookie export.

NOT yet verified: pure-HTTP authenticated login with this account, real storage
upload/callback, per-upload acceptance/points reconciliation, cloud-network behavior,
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

```sh
# Read-only list; creates a local ledger but does not log in/upload.
python3 scripts/assist_bot.py scan --work-dir /private/path/assist-job

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

python3 scripts/assist_bot.py status --work-dir /private/path/assist-job
```

`YOUR_PUBLIC_USER_ID` is the `id` in your own public profile URL, not your nickname
or email. It must be correct to exclude your own requests. Runtime login currently
does not independently resolve that ID: verify it before a live run.

Credentials: local `--prompt-login` keeps them in memory only. Cloud deployments
may inject `ABLESCI_USERNAME`/`ABLESCI_PASSWORD` into the process from a secret
manager; the password is removed from the process environment before lookup child
processes launch. Do not put credentials in command arguments, repository files,
job JSON, shell history, or plaintext cookie files. Cookies exist in memory only;
after restart, authenticate again. Do not bypass a login challenge.

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
- Per-paper discovery/validation runs in a child process with a 120 s ceiling.
  Cached downloaded files remain for revalidation. Failed PDF lookups may retry
  after six hours; rights/identity/schema review states are not blindly retried.
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
  earned. Review the site for acceptance. Automatic acceptance tracking remains
  a subsequent implementation/validation step; do not manufacture earnings metrics.

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
