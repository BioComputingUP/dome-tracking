# DOME tracker

Uptime and code-activity tracking for the five web resources of the DOME ecosystem, run on
free GitHub Actions every 20 minutes, with the full history kept in this repository. When a
site goes down, an issue is opened to alert you, and it closes itself when the site is back.

[![monitoring](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/BioComputingUP/dome-tracking/tracker-data/badges/monitoring.json)](https://github.com/BioComputingUP/dome-tracking/actions/workflows/tracker-check.yml)
[![responding](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/BioComputingUP/dome-tracking/tracker-data/badges/up.json)](https://biocomputingup.github.io/dome-tracking/)
[![down](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/BioComputingUP/dome-tracking/tracker-data/badges/down.json)](https://github.com/BioComputingUP/dome-tracking/blob/tracker-data/STATUS.md#needs-attention)
[![runs in last 24h](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/BioComputingUP/dome-tracking/tracker-data/badges/runs-24h.json)](https://github.com/BioComputingUP/dome-tracking/actions/workflows/tracker-check.yml)
[![repos pushed](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/BioComputingUP/dome-tracking/tracker-data/badges/activity.json)](https://github.com/BioComputingUP/dome-tracking/blob/tracker-data/STATUS.md#repository-activity)

**🖥️ Dashboard: https://biocomputingup.github.io/dome-tracking/** ·
**📄 [STATUS.md](https://github.com/BioComputingUP/dome-tracking/blob/tracker-data/STATUS.md)**

## What is tracked

| Resource | Uptime check | Code activity |
|---|---|---|
| DOME Registry | https://registry.dome-ml.org | [BioComputingUP/dome-registry](https://github.com/BioComputingUP/dome-registry) |
| DOME-ML website | https://dome-ml.org | [BioComputingUP/dome-ml-ui](https://github.com/BioComputingUP/dome-ml-ui) |
| DOME Copilot | https://dome-copilot.ifca.es/ | [IFCA-Advanced-Computing/dome-copilot](https://github.com/IFCA-Advanced-Computing/dome-copilot) |
| OSAI | https://osai.dome-ml.org | [BioComputingUP/dome-ml-osai-ui](https://github.com/BioComputingUP/dome-ml-osai-ui) |
| DOME Observatory | https://observatory.dome-ml.org | [BioComputingUP/dome-ml-observatory](https://github.com/BioComputingUP/dome-ml-observatory) |

Each run records:

- **Uptime:** an HTTP check of each site, classified as up, challenged (a bot-check page) or down.
  Failures are re-checked 30 s later before they count as down.
- **Code activity:** each repository's last push to *any* branch, and the latest commit on its
  default branch (sha, author, date, message). A repo counts as **active** if it was pushed in
  the last 7 days, **recent** if within 30 days, and **quiet** otherwise.

The results are committed to the [`tracker-data`](https://github.com/BioComputingUP/dome-tracking/tree/tracker-data)
branch, which is also the GitHub Pages source for the dashboard. `main` holds only the code. See
[tracker/README.md](tracker/README.md) for how it works, the metric definitions and common tasks.

## Outage alerts

When a site fails **two runs in a row** (each run already re-checks a failure after 30 s), the
tracker opens an issue titled **🔴 &lt;site&gt; is down**. The issue @mentions and is assigned to
@gavinf97, so you get a GitHub notification and an email. When the site responds again, the
tracker comments **🟢 back up** with how long it was down, and closes the issue, which sends a
second email. A site that stays down gets no further messages.

All past outages: [issues labelled `outage`](https://github.com/BioComputingUP/dome-tracking/issues?q=label%3Aoutage).
To test the alerts, see [tracker/README.md → Outage alerts](tracker/README.md#outage-alerts).

## GitHub Actions limits on the free plan, and how this repo handles them

| Limit | What it means here | Mitigation |
|---|---|---|
| **Scheduled runs are best-effort** | GitHub delays and drops `schedule` ticks under load. On the sister ECD tracker, a single hourly cron delivered only 5–7 runs a day. This is throttling, not a billing limit. | Runs are triggered every 20 minutes from outside GitHub: a free cron-job.org job calls `workflow_dispatch`, which is not throttled. The 4 GitHub crons an hour stay as a backup. The dashboard shows **runs in the last 24 h** against the target of 72, so under-delivery is visible. |
| **Pages builds** | Sites published from a branch have a soft limit of 10 builds an hour. Every run pushes once. | At most 7 runs an hour: 3 external plus 4 backup crons. |
| **Minutes** | Public repositories get free, unmetered Actions minutes on standard runners. A run takes about 1 minute. | Keep the repo **public**. A private repo would use up the 2,000 free minutes a month in about 3 weeks. |
| **60-day inactivity** | GitHub disables scheduled workflows in a repo with no activity for 60 days. Bot commits to `tracker-data` may not count. GitHub emails a warning first. | This affects only the backup crons: the external `workflow_dispatch` trigger keeps running. If all runs stop for 3 hours, the dashboard shows **STALE** and a banner. |
| **API rate limit** | The workflow's `GITHUB_TOKEN` allows 1,000 requests an hour. | Each run makes about 13 requests: 10 for repo activity and 1–3 for outage issues. That is under 100 an hour. |
| **Token scope** | The built-in `GITHUB_TOKEN` cannot read other private repos. `dome-ml-ui` and `dome-ml-osai-ui` are private, so they show as **no access**. | Add a read-only fine-grained token as the `ACTIVITY_TOKEN` secret. See [tracker/README.md → Common tasks](tracker/README.md#3-common-tasks) for the steps and the privacy trade-off. |

**Setting up the 20-minute trigger** takes about 10 minutes, once: a fine-grained token plus a
free cron-job.org job. Until it is set up, only the backup crons run. See
[tracker/README.md → Check interval](tracker/README.md#check-interval).
