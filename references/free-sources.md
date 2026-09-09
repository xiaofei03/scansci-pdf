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

At most 6 unique URLs are downloaded per paper, preferring reported published versions.
Default lookup budget: 75 seconds per paper including discovery; discovery has an
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
