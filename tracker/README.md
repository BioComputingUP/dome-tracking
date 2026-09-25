# tracker/

The code behind the DOME tracker. It is adapted from the ECD uptime tracker
([gavinf97/ECD](https://github.com/gavinf97/ECD/tree/main/uptime)) and adds repository-activity
tracking.

## 1. Where the results are deposited

On the **`tracker-data` branch**, never on `main`. The branch is also the GitHub Pages source,
so every push to it republishes the dashboard.

```
branch tracker-data
├── index.html               the dashboard (copied from tracker/dashboard/ by the workflow)
├── STATUS.md                the status page: monitoring health, repo activity, uptime, recent events
├── summary.json             everything on the dashboard, machine-readable
├── history.json             per-resource daily uptime, last 90 days
├── state.json               current state per resource, plus timestamps of recent runs (48 h)
├── events.jsonl             append-only: every uptime state change
├── activity.json            latest push / head commit per repository
├── activity_history.jsonl   append-only: one line each time a repo's last push or head commit changes
├── badges/*.json            shields.io endpoints behind the README badges
└── daily/YYYY/YYYY-MM-DD.json   per resource: checks, up, challenged, down, latency histogram
```

Commits are squashed to one per UTC day and force-pushed. Never edit the branch by hand.

## 2. How it works

The [`tracker-check`](../.github/workflows/tracker-check.yml) workflow runs these steps in order:

1. [`scripts/check.py`](scripts/check.py) sends a `GET` to each URL in [`resources.yml`](resources.yml).
   It follows redirects, reads only the first 64 KB, and uses a 10 s connect and 20 s read timeout.
   Failures are checked again 30 s later.
2. [`scripts/activity.py`](scripts/activity.py) makes two GitHub API calls for each `repo:`.
   - `GET /repos/{repo}` gives `pushed_at` (a push to any branch) and the default branch.
   - `GET /repos/{repo}/commits/{branch}` gives the head commit.

   If an API call fails, that repo keeps its previous snapshot and is marked with an `error`.
   The run never fails because of it.
3. [`scripts/summarize.py`](scripts/summarize.py) rebuilds every report in §1.
4. The workflow copies the dashboard to the data branch and commits.

### Classification

| State | When | Counts as available |
|---|---|---|
| `up` | 2xx/3xx, or a 4xx other than 404/410 (the server is responding but restricting access) | yes |
| `challenged` | A bot-challenge page (Cloudflare, Anubis, DDoS-Guard) | yes, counted separately |
| `down` | 5xx, timeout, DNS or connection error, redirect loop, or a TLS error a browser would also reject | no |
| `down` + `url_review` | 404/410. The URL needs fixing | no |

If TLS fails only because the certificate chain is incomplete, the site is re-checked without
verification and recorded as `up` with a `tls_warning`. **DOME Copilot** is currently in this
state (shown as a "TLS chain" flag on the dashboard). The flag clears by itself once the server
sends the full chain.

### Metrics

- **Uptime** = (up + challenged) ÷ recorded checks, over today, 7, 30, 90 and 365 days.
- **Coverage** = recorded checks ÷ expected checks (one per hour since the resource was first
  seen), capped at 100%. Missed runs lower coverage and are never counted as up.
- **Runs in the last 24 h**: how many runs actually happened. The target is 24 or more. The
  dashboard shows it green at 20 or more, amber at 10–19, and red below 10.
- **Repository activity**: active if pushed within 7 days, recent within 30 days, quiet
  otherwise. The dashboard works this out against the viewer's clock.

### Check interval

The target interval is **1 hour** (`INTERVAL_MINUTES` in [`scripts/common.py`](scripts/common.py)).
The workflow schedules **four** crons an hour (:07, :23, :39, :51) because GitHub's scheduler is
best-effort. On the ECD tracker, one hourly cron delivered only 5–7 runs a day, with gaps of
2.5–6 hours. Every run succeeded and the repo is public, so this was scheduler throttling, not a
cost limit. Extra runs do no harm: coverage is capped, and the job takes about a minute.

**Escalation (not set up).** Use this if the "runs in the last 24 h" figure stays below about 18
for a week:

1. Create a fine-grained personal access token. Scope it to this repository only, with the
   permission *Actions: read and write*. The BioComputingUP organisation must allow fine-grained
   tokens.
2. Register a free external cron job, for example on cron-job.org, to run hourly:
   ```
   POST https://api.github.com/repos/BioComputingUP/dome-tracking/actions/workflows/tracker-check.yml/dispatches
   Authorization: Bearer <token>
   Accept: application/vnd.github+json
   {"ref": "main"}
   ```
3. Set a calendar reminder for when the token expires.

## 3. Common tasks

**Give the tracker access to private repos.** The workflow's built-in `GITHUB_TOKEN` can read
only this repository and public ones. `BioComputingUP/dome-ml-ui` and
`BioComputingUP/dome-ml-osai-ui` are private, so until this is set up they show as
**no access**. Their uptime checks are not affected.

1. Go to GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token.
   - Set *Resource owner* to **BioComputingUP**.
   - Under *Only select repositories*, choose `dome-ml-ui` and `dome-ml-osai-ui`.
   - Set *Repository permissions* to **Contents: Read-only**. Metadata is added automatically.

   An org owner may need to approve the token.
2. Save it as a secret named `ACTIVITY_TOKEN`:
   `gh secret set ACTIVITY_TOKEN -R BioComputingUP/dome-tracking`.
   The next run picks it up. Public repos keep working through the same token.
3. Set a reminder for when the token expires. After it expires, those two repos go back to
   **no access**, and nothing else breaks.

⚠️ This repository and its dashboard are **public**. Once the token is set, the latest commit
message, author and date from the two private repos are published here too. If that is not
acceptable, leave the secret unset.

**Add or change a resource.** Edit [`resources.yml`](resources.yml). Each entry needs `id`,
`name`, `url` and `repo`. Use `check.url` to check a different address than `url`, and
`enabled: false` to pause an entry while keeping its history.

**Run locally:**
```bash
pip install -r tracker/requirements.txt -r tracker/requirements-dev.txt
python tracker/scripts/check.py --dry-run
GITHUB_TOKEN=$(gh auth token) python tracker/scripts/activity.py --dry-run
python -m pytest tracker/tests -q
```

**Preview the dashboard with real data:**
```bash
D=/tmp/dome-data; mkdir -p $D
python tracker/scripts/check.py --data $D && python tracker/scripts/activity.py --data $D \
  && python tracker/scripts/summarize.py --data $D
cp tracker/dashboard/index.html $D/ && python -m http.server -d $D 8000
```

## 4. Limitations

- **One vantage point.** Checks run from GitHub-hosted runners, mostly in US Azure regions.
- **Homepage only.** Each resource gets one URL check. APIs and search are not tested.
- **Hourly sampling.** Outages shorter than an hour can be missed.
- **"Activity" means code pushes.** Issues, pull requests and releases are not counted.
  `pushed_at` covers every branch, and the head commit covers only the default branch.
