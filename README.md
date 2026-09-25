# DOME tracker

Uptime and code-activity tracking for the five web resources of the DOME ecosystem, run on
free GitHub Actions about once an hour, with the full history kept in this repository.

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

## GitHub Actions limits on the free plan, and how this repo handles them

| Limit | What it means here | Mitigation |
|---|---|---|
| **Scheduled runs are best-effort** | GitHub delays and drops `schedule` ticks under load. On the sister ECD tracker, a single hourly cron delivered only 5–7 runs a day. This is throttling, not a billing limit. | The workflow has **4 crons an hour** (:07, :23, :39, :51), so hourly coverage survives even if about 75% of ticks are dropped. Extra runs are harmless because coverage is capped at 100%. The dashboard shows **runs in the last 24 h**, so under-delivery is visible. |
| **Minutes** | Public repositories get free, unmetered Actions minutes on standard runners. A run takes about 1 minute. | Keep the repo **public**. A private repo would use up the 2,000 free minutes a month in about 3 weeks. |
| **60-day inactivity** | GitHub disables scheduled workflows in a repo with no activity for 60 days. Bot commits to `tracker-data` may not count. GitHub emails a warning first. | The dashboard checks the age of the last run against the viewer's clock, so it shows **STALE** and a banner if runs stop. To fix, re-enable the workflow in the Actions tab, or push any commit to `main` every few weeks. |
| **API rate limit** | The workflow's `GITHUB_TOKEN` allows 1,000 requests an hour. | Each run makes 10 requests (2 per repo), and a failed call keeps the last known values. |

**If runs are still irregular.** If scheduled delivery stays below about 18 runs a day for a
week, trigger the workflow from outside GitHub's scheduler instead. See
[tracker/README.md → Check interval](tracker/README.md#check-interval).
