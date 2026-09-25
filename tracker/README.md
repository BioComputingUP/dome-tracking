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
5. [`scripts/notify.py`](scripts/notify.py) opens or closes outage issues (see
   [Outage alerts](#outage-alerts)). It runs after the commit, so the data is saved even if the
   GitHub API fails at this step.

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
- **Coverage** = recorded checks ÷ expected checks (one per 20 minutes since the resource was
  first seen), capped at 100%. Missed runs lower coverage and are never counted as up. Days up
  to 2026-09-25 are measured against the hourly interval in force then, because each daily file
  records its own interval.
- **Runs in the last 24 h**: how many runs actually happened, against a target of 72. It shows
  green at 75% of the target or more (54+), amber at 33% or more (24+), and red below that.
- **STALE**: no run for 3 hours. This is the "monitoring has stopped" signal. A late run or two
  does not trigger it.
- **Repository activity**: active if pushed within 7 days, recent within 30 days, quiet
  otherwise. The dashboard works this out against the viewer's clock.

### Check interval

The target interval is **20 minutes** (`INTERVAL_MINUTES` in [`scripts/common.py`](scripts/common.py)).

GitHub's own scheduler can't deliver that. It is best-effort and drops ticks under load: on the
ECD tracker, an hourly cron fired only 5–7 times a day. So runs are triggered in two ways:

- **Primary:** a free [cron-job.org](https://cron-job.org) job calls the `workflow_dispatch` API
  at :05, :25 and :45. Dispatch events run straight away and are not throttled.
- **Backup:** four GitHub crons an hour (:07, :23, :39, :51), best-effort. They keep monitoring
  going, roughly hourly, if the external trigger stops. For example, when its token expires.

Extra runs do no harm. Coverage is capped at 100%, the concurrency group stops runs from
overlapping, and the worst case of 7 runs an hour stays under the GitHub Pages limit of 10
builds an hour.

**One-time setup of the external trigger (about 10 minutes)**

1. **Create a token.** Go to https://github.com/settings/personal-access-tokens/new and make a
   fine-grained token.
   - Resource owner: **BioComputingUP**.
   - Repository access: *Only select repositories*, choosing `dome-tracking`.
   - Repository permissions: **Actions: Read and write**. Nothing else.
   - Expiration: 1 year, or the longest the org allows.
   - An org owner may need to approve the token before it works.
2. **Create the job.** Sign up at [cron-job.org](https://cron-job.org) (free) and create a cronjob.
   - **URL:** `https://api.github.com/repos/BioComputingUP/dome-tracking/actions/workflows/tracker-check.yml/dispatches`
   - **Schedule:** Custom. Every hour, every day, at minutes **5, 25 and 45**.
   - **Advanced settings:**
     - Request method: **POST**.
     - Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json` and
       `X-GitHub-Api-Version: 2022-11-28`.
     - Request body: `{"ref":"main"}`.
   - **Notifications:** turn on "notify me when execution fails". An expired or revoked token
     then emails you.
3. **Check it.** Press **Test run**; it should answer with a 2xx status. A new run then appears
   under `gh run list -R BioComputingUP/dome-tracking --event workflow_dispatch`.
4. **Set a reminder** for the token's expiry date. When it expires, runs fall back to the
   backup crons, and "runs in the last 24 h" turns amber or red.

The token can only trigger and manage Actions runs in this one repository. It lives only in
cron-job.org, not in this repo.

### Outage alerts

[`scripts/notify.py`](scripts/notify.py) compares the current state with the open issues
labelled `outage`, on every run:

| Situation | Action |
|---|---|
| Down for `min_consecutive_down` runs in a row (2), with no open issue | Open **🔴 &lt;site&gt; is down**, @mention and assign the users in `notify:` in [`resources.yml`](resources.yml) |
| Up or challenged again, with an open issue | Comment **🟢 back up** with the outage duration, and close the issue |
| Still down, with an issue already open | Nothing, so there is no spam |

Each issue carries a hidden marker naming the resource, so the script never opens a duplicate,
and it catches up by itself if a run fails.

Issues are opened by **github-actions[bot]** through the workflow's built-in token. That is
deliberate: GitHub does not notify you about actions taken with your own token. Being mentioned
and assigned makes you a participant, so you are emailed about the opening, the "back up" comment
and the close, whatever your watch settings are. The only setting needed is email for
"Participating" notifications at https://github.com/settings/notifications, which is on by
default.

**Test the alerts.** Run
`gh workflow run tracker-check.yml -R BioComputingUP/dome-tracking -f simulate_down=dome-registry`.
- This opens a **[test] 🔴 DOME Registry is down** issue. Recorded data is not changed.
- The next run sees the site up, comments "back up" and closes the issue.

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
- **20-minute sampling.** Outages shorter than about 20 minutes can be missed. Because an alert
  needs two failed runs in a row, it arrives 20–40 minutes after a site goes down.
- **"Activity" means code pushes.** Issues, pull requests and releases are not counted.
  `pushed_at` covers every branch, and the head commit covers only the default branch.
