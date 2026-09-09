# Free-source setup and diagnostics

## What is implemented

The main `batch.py` retains one native Zotero full lookup. Missing papers then use
`free_sources.py`: up to 3 concurrent papers; each discovers Unpaywall and OpenAlex
concurrently, with at most 2 requests per host in this process. Publisher HTML is a
lazy fallback, so slow landing pages do not hold up already discovered PDF links.
Public Crossref PDF links and an optional Elsevier Article Retrieval PDF request
complete the candidates. Only OA-flagged OpenAlex locations are followed. Publisher
pages must match DOI and normalized title before following citation_pdf_url.
Repository PDFs exposed by these indexes are supported, but there is no independent
CORE/Semantic Scholar/search-engine adapter or unrestricted website crawler.

OA locations pointing to PMC are resolved through NLM's public Cloud Service even
when the index has no PDF URL. `pmc_cloud.py` lists only that PMCID's version prefixes
(at most 10 entries; at most three versions), checks DOI/title, OA license, retraction
and manuscript flags, then downloads the declared PDF from the official S3 bucket.
PDF MD5 must match the metadata before the usual identity/completeness checks.
Only unambiguous published CC BY/BY-SA/CC0 records qualify in this adapter.
Files use two 512 KiB range-download workers with If-Match and strict Content-Range
checks. Complete segments retain local SHA-256 checksums and resume under the same
object ETag; the assembled file must match the official whole-file MD5. Partial
segments never become PDFs or Zotero attachments. No corpus-wide bucket scan occurs.
Cached PMC copies are rechecked against current repository metadata/checksum before
reuse. Source attribution is retained in the cache and result. No AWS account/key
is required. No PMC website scraping or retired OA Web Service calls are used.
Explicit Crossref URLs ending `/pdf` or `.pdf` are candidates even if their MIME
type is `unspecified`; URLs are not invented, and returned bytes still require PDF
and identity validation.

At most 6 primary candidate URLs are downloaded per paper, preferring reported published versions.
If none succeeds, the license-gated supplementary source below may try one more PDF.
Default primary lookup budget: 75 seconds per paper including discovery; discovery has an
18-second cooperative budget and sockets a maximum 12-second blocking timeout.
These are cooperative bounds, NOT a hard process timeout: DNS, one blocking read or
PDF parsing may overrun the budget. No infinite retries or paywall/verification bypass.
429/503 responses cause bounded host cooldowns; no repeated sleep on each failed URL.
Logs retain provider, HTTP status, phase, bytes and elapsed time, never credentials
or arbitrary response/error bodies. Elsevier's structured error code and a bounded,
key/email/URL-redacted status message aid request debugging.
Candidate order is deterministic; parallelism
does not make Zotero/SQLite writes concurrent. The owning thread alone imports.

Verified bytes cache under the batch's `free_sources/`; reuse revalidates identity.
Unresolved results are not proof no OA copy exists: inspect authentication, access,
rate-limit, timeout and missing-config events. Routine resume skips previous misses.
After fixing credentials/network, explicitly pass `batch.py --retry-free`; this does
not reset budgets or retry any DOI with an existing AbleSci request record.
Source version labels are recorded. Known accepted/submitted versions get an Author
Manuscript attachment title. A shorter-than-published copy still needs the existing
hash-bound user-approved version review; new sources do not loosen validation.
Native Zotero lookup reports per-item start/end timestamps for new runs. A batch
lookup remains serial; distinguish its elapsed time from the concurrent free-source
phase. A native title/author-only `probably_correct` match must not be reported as a
DOI-confirmed publisher version. Inspect version evidence or probe an entitled source
when the user needs the final published version; do not silently replace attachments.

## TLS and HTTP 403 recovery

- A reproducible `[SSL] record layer failure` at an OA repository can trigger one
  fresh TLS 1.2 request. It retains certificate verification, hostname checking,
  system trust roots, redirect protections, byte limits and the original deadline.
  Partial bytes are discarded. This is per-request compatibility, not a global
  downgrade; there is no TLS 1.0/1.1, custom insecure CA, proxy change or `verify=False`.
- Certificate verification failures never trigger this fallback. Logs distinguish
  `tls_certificate_error` (with verification code), `tls_record_error` and other
  `tls_handshake_error` outcomes. Unresolved trust errors require diagnosis, not
  silently trusting an intercepted certificate. Other TLS errors are not retried.
- HTTP 403 plus explicit `cf-mitigated: challenge` is
  `browser_verification_required`. The engine skips further requests to that host
  for its lifetime and continues independent sources. It does not fake browser
  fingerprints, rotate IPs, extract cookies or solve the challenge. Ordinary 403
  remains `access_denied`; it is not proof of either a paywall or a bad API key.
- Live regression on 2026-09-09: SMJ DOI `10.1002/smj.70041` failed with the
  default TLS path, but its indexed DAU repository copy downloaded via the secure
  TLS 1.2 retry (28 pages; DOI/title/first-author/readability checks passed).
  This demonstrates an effective compatibility fix on the tested network, not
  whether the underlying defect is in the server, proxy or TLS implementation.
  Wiley/OUP Cloudflare challenges remain unresolved. Three fixed samples retested
  without cached PDFs yielded 2 verified files in 10.252 s for the free-source
  phase, excluding metadata preparation and Zotero import. No universal rate or
  timing guarantee follows from this small test.

## License-gated Sci-Hub OA-copy supplement

`scripts/scihub_oa.py` is a bounded HTML/PDF adapter, not an official API or the
previously reviewed insecure third-party client. It runs after ordinary free-source
failure, before any authorized AbleSci request. No extra package or key is needed.

- Enabled by default, but only metadata obtained from Crossref with an already
  effective **version-of-record** CC BY, CC BY-SA or CC0 license qualifies. A bare OA
  flag, TDM/author-manuscript-only license, subscription entitlement or unknown rights
  does not qualify. Missing/malformed license records fail closed, without a request.
  This narrow gate does not add access to arbitrary subscription-only papers.
- The tested host is `sci-hub.ru`. Initial URLs, PDF links and redirects must stay
  HTTPS on that same host; no mirror search/rotation, credentials, cookies, browser
  fingerprinting, CAPTCHA solving or certificate-check bypass. Changed page structure,
  multiple different PDF links or access challenges stop this source.
- At most one landing page and one unique linked PDF, with its own 60-second cooperative
  phase budget starting when this source begins, even if the primary 75-second
  budget has expired. `SCANSCI_SCIHUB_SECONDS=120` changes this to 120 seconds;
  accept only positive finite values, not infinite waits. The existing narrow TLS-record
  compatibility retry may repeat a request once; it retains verified TLS. Network
  reads, DNS and validation can overrun a cooperative deadline.
  Total free-phase time may now include both budgets. `seconds` includes both phases;
  `primary_deadline_reached` (and compatibility field `deadline_reached`) refers to
  the primary budget only and does not mean the supplementary source failed.
- Require the existing DOI/title/author/readability/page-count checks to return
  `verified`, not merely a title-only probable match. Keep source and license evidence
  in the result/job. Do not assume a manuscript version from the host or license record.
  Separate cache under `free_sources/scihub_oa/` rechecks license, identity and hash.
- Set `SCANSCI_SCIHUB_OA=0` to disable in all batch entrypoints, or use
  `free_sources.py --no-scihub-oa` for a free-source probe. Existing saved jobs without
  Crossref license metadata skip this route; `--retry-free` alone does not refresh
  old metadata. Do not discard their batch state or point ledger to force a retry.

Direct adapter test (no Zotero write, browser control or point spending):

```sh
python3 scripts/free_sources.py --doi 10.1038/s41586-021-03819-2 \
  --scihub-oa-only --work-dir /project/_work/scihub_oa_probe
```

This switch still enforces the license gate. One successful sample is not evidence of
general coverage, an official API, or reliable end-to-end batch performance.
On 2026-09-09 the earlier AlphaFold diagnostic succeeded, but two integrated live
probes returned a PDF-transfer timeout and no supported PDF link, respectively.
Treat the route as best-effort; do not describe it as a stable replacement for OA
repositories or authorized subscription access.

## One-time configuration

No new email account is necessary. An existing real research email can be used.

| Setting | Need / how to obtain |
| --- | --- |
| `email` | Needed for Unpaywall; existing email, no separate Unpaywall API key. |
| `openalex_api_key` | Recommended for larger batches; create a free OpenAlex account and copy Settings → API key. Basic keyless queries remain supported by current docs. |
| `elsevier_api_key` | Optional; create a key in Elsevier Developer Portal. Used only for Elsevier-associated records, not Wiley/OUP/etc. |
| `elsevier_insttoken` | Optional institutional credential, requested through Elsevier support/library when IP-based institutional authentication is unavailable. Not a self-issued arbitrary token. |

Run with Python 3.10+ (configuration uses the standard library):

```sh
python3 /absolute/path/to/scansci-pdf/scripts/free_sources.py --configure
python3 /absolute/path/to/scansci-pdf/scripts/free_sources.py --status
```

The interactive prompt hides keys; blank keeps an existing value, `-` clears it.
It saves `~/.config/scansci-pdf/config.json` with mode 0600, outside the skill/project.
This is a restricted-permission local file, not encrypted storage; protect the account
and device. Do not paste keys into chat, CLI arguments, job files or public GitHub.
`SCANSCI_CONFIG` overrides the private path. Environment variables override fields:
`SCANSCI_EMAIL`, `OPENALEX_API_KEY`, `ELSEVIER_API_KEY`, `ELSEVIER_INSTTOKEN`.
`--status` reports configured/unconfigured only, not validity or entitlement.
Installing the skill on another device does not migrate credentials.

Do not purchase plans, request institutional credentials or silently raise quotas.
An Elsevier key alone does not grant subscribed full text. Institutional IP/token,
API permissions, license and response-format rights still apply. A 200 XML response
is not a PDF; only %PDF bytes that pass the existing validation gate are accepted.
The adapter requests application/pdf without a view parameter, with no author-manuscript redirect
override; it sends keys only to the Elsevier API and strips credentials on cross-origin
redirects. OpenAlex uses the supported bearer header, avoiding keys in query URLs.
Live testing found `view=FULL` invalid for the PDF representation (HTTP 400); omit
it, not the PDF validation. A valid key can still return a one-page PDF, which fails
the unchanged completeness gate. Do not label that response as successful full text.

## Small live test without library changes or point spending

```sh
python3 scripts/free_sources.py --doi DOI --doi DOI --work-dir /project/_work/free_probe
```

This retrieves Crossref metadata, downloads public/entitled files and prints validation
plus timings. It does not import Zotero, launch Chrome or contact AbleSci. Install
requirements.txt for PDF validation. Source-lookup elapsed excludes Crossref metadata
and queue time; use an outer wall clock when reporting end-to-end throughput.
After credentials are present, validate each adapter with a real source-specific DOI;
mock tests alone do not prove actual key validity, PDF entitlement or improved hit rate.

## Official references checked 2026-09-09

- PMC current cloud access: https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/
- PMC dataset schema: https://pmc-oa-opendata.s3.amazonaws.com/README.txt
- Retired OA Web Service notice: https://pmc.ncbi.nlm.nih.gov/tools/oa-service/

- Unpaywall API: https://unpaywall.org/api ; schema https://unpaywall.org/data-format
- OpenAlex authentication and free-key setup: https://help.openalex.org/api/authentication/
  (account key: https://openalex.org/settings/api)
- Elsevier registration: https://dev.elsevier.com/
- Elsevier authentication: https://dev.elsevier.com/tecdoc_api_authentication.html
- PDF-specific entitlement: https://dev.elsevier.com/tecdoc_article_access.html
- Article Retrieval schema: https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl

Old OpenAlex docs URLs now redirect; do not assume older claims that keys are mandatory
remain current. Follow current docs and actual status codes rather than guessing why a
keyless request failed. No paid full-text storage API has been enabled by this change.
