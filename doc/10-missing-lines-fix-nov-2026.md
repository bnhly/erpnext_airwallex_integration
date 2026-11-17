# Missing bank lines and the log flood: fix and runbook (17 Nov 2026)

## Why lines went missing (all in the code, both feeds)

1. A line that failed to insert was logged and dropped. The run still said
   Completed and the next run started after it, so it was never asked for
   again.
2. Each run asked from the moment the last run finished. Lines made while a
   run was going, or listed late by the bank, fell in the gap.
3. Airwallex: only the first page of results was read.
4. "In Progress" left by a run that died stopped every later run, for good
   (the scheduler checked it before the run could look at its age).
5. Airwallex: the AUD and USD rows share one login. Each row fetched the same
   lines; the USD row tried every AUD line with no bank account. Once Bank
   Account became mandatory on Bank Transaction that was an Error Log row per
   line per run, which buried the real failures.
6. Starting "Sync old transactions" set the scheduled start point to now,
   skipping everything since the last scheduled run.
7. Skript: one cursor for every account, moved to the latest posting time on
   any account. An account that posts late (Judo) lost every line older than
   CBA's latest. A failed line was skipped past the same way.

Still to read on the live site (the feed check below answers it): why the AUD
row itself made nothing after 3 Nov, and who made Bank Account mandatory.

## Why the log grew to 65,000+ rows

Every HTTP call was written to Bank Integration Log whatever the Enable Log
tick said, with the request headers unmasked (login rows hold the long-life
x-api-key, the rest a bearer token) and the whole reply. After every hourly
run a reference backfill re-reads 60 days of lines and looks up each card
line's merchant; each lookup that fails was another Error row. Hourly, every
card line in 60 days.

## What changed

- Every run asks from the last good run less 14 days. Lines already in
  ERPNext are skipped by transaction_id (cancelled ones too).
- Each line goes in on its own savepoint. Failures are kept in the run report
  (Last Run Report on the settings page), the run ends Completed with Errors,
  and the next window is held at the earliest failure (at most 45 days).
- Every page is read. One fetch per Airwallex login; each line goes to the
  row whose Bank Account's ledger currency matches. Currencies with no row
  (GBP, EUR, SGD) are counted, not inserted, not errors.
- A run In Progress for over 3 hours is taken over. The 23:00 Skript job that
  forced "Completed" every night is off the schedule.
- A manual range or catch-up never moves the scheduled start point.
- Line dates are the Melbourne date, not the UTC date (the statement ran a
  day behind the feed for lines made before 10 or 11 am).
- Logging follows Enable Log. Errors are still logged, with headers masked
  and no login reply. Expected lookup misses (403 / 404) are not errors.
  Log Settings clears rows older than 30 days, daily.
- The Airwallex token expiry is read from expires_at (it lasts 30 minutes,
  not the hour the code assumed).

## Runbook after deploy (each step asks first on live)

All are GET calls in the browser while signed in. Read only unless it says.

1. Feed check (read only):
   `/api/method/bank_integration.feed.tools.feed_check?txn=dbe76514-de25-4d00-81db-1d851987a43b&ask_airwallex=1`
   Look at `airwallex_problems`, `airwallex_routes`, `bank_account_mandatory`,
   `lines_by_bank_account`, `feed_error_log_since`, and where the 3 Nov line
   would go.
2. Log count (read only):
   `/api/method/bank_integration.feed.maintenance.purge_logs?error_logs=1`
3. Rotate the Airwallex API key in Airwallex, paste the new one into both
   client rows, Save. (The old key sits in log rows readable by any System
   Manager.)
4. Clear the logs (deletes, background, batches of 2,000):
   `...purge_logs?error_logs=1&dry_run=0&confirm=YES_DELETE`
5. Catch-up dry run (reads Airwallex, writes nothing):
   `/api/method/bank_integration.feed.tools.catch_up?provider=airwallex&from_date=2025-07-01&to_date=2026-11-17&currencies=AUD`
   Compare with the 63 lines from the statement check.
6. Catch-up (adds lines, background): the same with `&dry_run=0&confirm=YES_ADD`.
7. Skript the same way with `provider=skript` (Judo since 8 Jul 2026).

## Checks

`dev/check_feed_bench.py` runs on a real Frappe v15 + ERPNext v15 site with
Airwallex and Skript stubbed (50 checks: paging, routing, a failure mid-run,
the window held and the line coming in next run, a late line, stale and live
locks, manual ranges, logging and masking, the purge, catch-up, Skript late
posting and unmapped accounts).
