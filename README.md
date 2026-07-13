# pr-watcher

Polls ~40 feeds (company newsrooms, SEC EDGAR 8-K/6-K, Google News queries,
X/Twitter accounts) every 5 minutes and emails a digest of anything new.
Items are only alerted on if published today (UTC) — older entries that
resurface due to feed reordering are recorded as seen but not sent.

## Setup

1. Push these files to a **public** GitHub repo (public = unlimited free
   Actions minutes; a 15-min cron on a private repo blows past the 2,000
   free min/month). Nothing sensitive lives in the repo.
2. Edit `USER_AGENT` in `watcher.py` — SEC asks for a contact email in the UA.
3. Validate feeds and prune dead candidates:
   ```
   pip install feedparser pyyaml
   python watcher.py --validate
   ```
4. Pick a notification mode (no Gmail connection needed for any of them):
   - **GitHub issue mode (default, zero setup):** set no secrets. When
     there are new items the workflow opens an issue in the repo and
     GitHub emails it to you within seconds. Make sure you're watching
     the repo (Watch → All activity) and GitHub notification emails are on.
   - **Resend:** add secrets `RESEND_API_KEY` and `MAIL_TO` (free tier
     ~100 emails/day; sender defaults to onboarding@resend.dev, or set
     `MAIL_FROM` with a verified domain).
   - **SMTP:** add `SMTP_USER`, `SMTP_PASS`, `MAIL_TO` (e.g. a Gmail app
     password) — only if you prefer this over the two options above.
5. Run once locally (`python watcher.py`) or trigger the workflow manually.
   The first run baselines everything without notifying; alerts start
   on run two.

## Latency expectations

GitHub Actions cron is best-effort, and in practice it's worse than the
docs imply: on a low-activity public repo, a `*/5` schedule has been
observed firing only every 2-3 **hours** instead of every 5 minutes —
GitHub silently deprioritizes frequent schedules on repos without much
other Actions traffic. `git log` is the way to check this: look at the
timestamps on `update seen state` commits.

### Fixing it: trigger from outside GitHub's scheduler

The workflow already accepts `workflow_dispatch` (manual/API trigger), so
the fix is to have an external cron service call that endpoint instead of
relying on GitHub's `schedule:` trigger:

1. Create a GitHub **fine-grained personal access token**
   (Settings → Developer settings → Fine-grained tokens) scoped to just
   this repo, with **Actions: Read and write** permission.
2. Sign up at a free external cron service, e.g. https://cron-job.org.
3. Create a job that runs every 5 minutes and sends:
   - **URL:** `https://api.github.com/repos/synapticlee/news-agg/actions/workflows/watch.yml/dispatches`
   - **Method:** POST
   - **Headers:** `Authorization: Bearer <YOUR_TOKEN>`,
     `Accept: application/vnd.github+json`
   - **Body:** `{"ref":"main"}`
4. Leave the `schedule:` trigger in `watch.yml` as a backup — the
   `concurrency` group (`cancel-in-progress: false`) already prevents
   overlapping runs if both fire close together.

You can test the request by hand first:
```
curl -X POST -H "Authorization: Bearer <YOUR_TOKEN>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/synapticlee/news-agg/actions/workflows/watch.yml/dispatches \
  -d '{"ref":"main"}'
```
A 204 response means it queued a run — check the Actions tab.

If you ever need true near-instant (2–5 min) and don't want to depend on
GitHub Actions at all, run this same script in a loop on a small VPS or
Fly.io machine — no code changes needed:
`while true; do python watcher.py; sleep 180; done`

## Future ideas (not yet implemented)

- **Scrape newsletters in the inbox.** Would need read access to a mailbox
  (Gmail API or IMAP) to pull in newsletter content that never appears on
  the web (paid Substacks, member-only digests). Bigger lift than the RSS
  sources here: needs auth/credentials as GitHub secrets, a parser per
  newsletter format, and dedup logic separate from `seen.json`'s
  entry-ID scheme.

## Notes / caveats

- **EDGAR feeds are the reliable backbone.** Any US-listed company's
  material news (earnings, M&A, exec changes) hits its 8-K feed fast, and
  the press release is usually Exhibit 99.1 inside the filing. Foreign
  issuers (TSMC, ASML) file 6-K instead — listed separately in the config.
- **EDGAR only covers public companies.** Anthropic, xAI, Mistral, etc.
  are covered via Google News query feeds, which are noisier (media
  coverage, not press releases). Tighten queries with extra terms if needed.
- sec.gov rate limit: 10 req/s; the script sleeps between fetches.
- **X/Twitter feeds are the most fragile source.** The `xcancel.com`
  bridge can go down or start blocking without warning — check it first
  if the digest goes quiet on those entries specifically.
- **Newsroom RSS ≠ IR press releases** for some companies (e.g., NVIDIA's
  investor site has a separate Q4-hosted press-release feed). If earnings
  specifically matter, EDGAR catches them regardless.
- Feeds die and move. Re-run `--validate` occasionally.
