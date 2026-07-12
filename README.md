# pr-watcher

Polls ~40 feeds (company newsrooms, SEC EDGAR 8-K/6-K, Google News queries)
every 20 minutes and emails a digest of anything new.

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

GitHub Actions cron is best-effort: a `*/15` schedule typically fires
5–15 minutes late and can occasionally skip. Real-world latency is
~15–30 min, which is fine for press releases. If you ever need true
near-instant (2–5 min), run this same script in a loop on a small VPS
or Fly.io machine — no code changes needed:
`while true; do python watcher.py; sleep 180; done`

## Notes / caveats

- **EDGAR feeds are the reliable backbone.** Any US-listed company's
  material news (earnings, M&A, exec changes) hits its 8-K feed fast, and
  the press release is usually Exhibit 99.1 inside the filing. Foreign
  issuers (TSMC, ASML) file 6-K instead — listed separately in the config.
- **EDGAR only covers public companies.** Anthropic, xAI, Mistral, etc.
  are covered via Google News query feeds, which are noisier (media
  coverage, not press releases). Tighten queries with extra terms if needed.
- **GitHub Actions cron is best-effort** — "*/20" can slip to 25–35 min
  under load. If you need true instant, run the script on a small VPS or
  fly.io machine on a 5-min loop instead. sec.gov rate limit: 10 req/s;
  the script sleeps between fetches.
- **Newsroom RSS ≠ IR press releases** for some companies (e.g., NVIDIA's
  investor site has a separate Q4-hosted press-release feed). If earnings
  specifically matter, EDGAR catches them regardless.
- Feeds die and move. Re-run `--validate` occasionally.
