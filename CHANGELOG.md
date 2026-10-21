# Changelog

## v1 (2026-10-21)

First build, tested end to end against mock monday.com, Slack and Resend APIs (36 scenarios, 60 checks).

### Added

- A weekly schedule (`REPORT_CRON`) and a Run Now webhook protected by the `X-Run-Secret` header. Both run the same steps.
- A settings check before any API call. A broken `BOARDS_JSON` or a missing key stops the run with a list of what to fix.
- Full reads of every board: `items_page` for the first page, then `next_items_page` with the cursor, until the cursor is empty. The `API-Version` header is sent on every request (default `2026-07`).
- A check of every monday.com answer, because most errors come back with HTTP 200. Rate limits and the complexity budget wait for `retry_in_seconds` (or `Retry-After`) and retry the same page; 5xx and 423 (resource locked) answers retry with a pause; a low complexity budget pauses before the next page. A bad token or the daily call limit stops at once.
- Per board problems (board deleted, not shared or refused, a configured column no longer on the board, a read that fails part way) are named in the report while the other boards still run. A board that was only partly read is never scored.
- Five checks per board: duplicates by email or phone (including chains), missing required fields, stale items, items without an owner, and invalid email or phone formats.
- A 0 to 100 health score (share of items with no issues), a grade, and the worst items with links.
- One Slack message (Block Kit, within Slack's limits) and an optional HTML email through Resend, with an idempotency key so a retried request never sends a second email. CRM names are escaped in both.
- A CRM Health Log board with one row per board per day. Rows are found by a key column and updated on a re-run, so trends never count a day twice. All rows are saved in one GraphQL request; each row's result is checked on its own.
- An error alert workflow, sticky notes on the canvas, and an end to end mock test whose monday.com mock validates every request against monday.com's published GraphQL schema.

### Found and fixed during testing

- **Re-runs could not save the health log.** When every row was an update, the save mutation still declared the `$group` variable, which only new rows use. GraphQL rejects a declared variable that nothing uses, so the whole save failed. The schema check in the mock caught it on the first re-run; `$group` is now declared only when a row is created.
- **Mirror columns looked empty.** monday.com returns `null` as the text of mirror, connect boards and dependency columns. Used as a required field, every item would have counted as missing it. These columns are now read from `display_value`.
- **Failure alerts never arrived.** n8n 2 only calls an error workflow that is active, and skips an inactive one with a note in the server log only. The setup steps now say to activate it, and the test does the same.
- **Unreadable error text in Slack.** When an HTTP request fails, n8n puts the raw JSON body in the error message. Slack and the alert now say, for example, "HTTP 500: Internal server error".
- **A locked resource stopped a board.** monday.com answers "Resource is currently locked, please try again later" with HTTP 423. The first version treated that as a permanent failure and left the board out of the report. It now waits and retries, like a 5xx.
- **The mock was kinder than the real API.** monday.com answers `UserUnauthorizedException` with HTTP 403, not 200, and the mock used to send 200. It now answers each error with the status monday.com documents for it, and new scenarios cover a temporary GraphQL error with HTTP 200, an expired cursor on page 3 and a missing key column on the log board.
- **The test could pick up the wrong alert.** Error workflow alerts arrive after the failed run ends, sometimes after the next scenario has started. The test now matches each alert to its run through the execution link inside it.

### Found and fixed in review

- **Placeholders made false duplicates.** Any non empty email was used to match duplicates, so on a board where the email is a text column, every item holding `n/a` or `none` became one big "duplicate" group. Only a valid email or phone number can match now; placeholders are counted as invalid. The test data now has the same `n/a` on two items.
- **Formula columns gave a misleading score.** monday.com returns no text for formula columns, so a formula column in `required_columns` made every item look incomplete. The board is now named in the report with the column ID instead of being scored.
- **A re-run could keep an old score.** If a board was emptied after an earlier run the same day, the re-run updated the log row to 0 items but left the old score in place. It now clears the score with `null`. An empty string would not work: monday.com turns it into 0 for a numbers column, and the mock now does the same.
- **Slack's notification text was not escaped.** Board names were escaped in the message blocks but not in the top level `text`, which Slack also reads as markup. Every section is also kept within Slack's 3,000 character limit, so a long list of log errors cannot make Slack refuse the whole report.
- **The Run Now answer promised Slack and email** even when only one of them is set up. It now says the report arrives in Slack or email.
- **The mocks were missing real rules.** Resend refuses requests without a `User-Agent` header (n8n sends `User-Agent: n8n`, so the workflow is fine), and monday.com writes an empty string as 0 and `null` as cleared for a numbers column, and returns an empty text for formula columns. The mocks now do all three.
- **Gaps in the test.** New scenarios: a board emptied since the last run, a formula column, an email refused by Resend, and an email only setup that hits `MONDAY_MAX_PAGES`. Failure scenarios now also check that no email went out and that a failed log save leaves every row untouched. Nine broken copies of the workflow (five from the build, four for the fixes above) each fail the test.

### Found and fixed in a second review

- **The health log save was never retried.** The first version sent it once only, because `create_item` alone is not idempotent, so one 5xx or a dropped connection meant no trend row that day. monday.com documents an `Idempotency-Key` header for mutations: a retry with the same key within 30 minutes gets the first answer back instead of running again. The save now sends a key that is fixed for the run and is retried up to 3 times. A new scenario loses the answer after monday.com has created the rows, and the retry creates nothing new.
- **Run Now said "started" when the settings were broken.** It answered 202 before the settings were checked, so the setup check in step 5 could not show a typo in `BOARDS_JSON`; only the alert did. Run Now now answers after the check: 202 when the settings are fine, 500 with the list of problems when they are not.
- **Errors that hit every board were reported board by board.** monday.com answers a retired API version with HTTP 200 and `InvalidVersionException`, and a user who may not use the API with 403 and `USER_ACCESS_DENIED`. Both produced a report where every board was "not checked". The run now stops at once and says what to change.
- **A page over the per-query complexity limit had no clear outcome.** `REQUEST_MAX_COMPLEXITY_EXCEEDED` (`maxComplexityExceeded` in older versions) never goes away by waiting. Answered with HTTP 429, it was retried until the whole run failed; answered with 200, the board was flagged without saying what to do. Now the board is named with a hint to lower `MONDAY_PAGE_SIZE`, and nothing is retried.
- **The mock could share its port.** On Windows, Python's test server binds with `SO_REUSEADDR`, which lets a second server take the same port without an error and receive some of the requests. The mock now binds the port exclusively, so a clash stops the test instead of making checks fail at random.
- **The mock now follows two more monday.com rules:** an unknown API version gets HTTP 200 with `InvalidVersionException`, and a mutation with an `Idempotency-Key` it has seen gets the first answer back with `Idempotency-Replayed: true`.
- **Idempotency keys could repeat across n8n instances.** The email key was `crm-health-report/<date>/<execution id>`, and execution IDs are only unique within one n8n. Two instances that share a Resend key (or, for the new log save key, a monday.com token) could send the same key on the same day, and the second request would be refused or answered from the first. Both keys now also hold the workflow ID.
- **Email only setups get no failure alerts.** The error alert workflow posts to Slack. This is now said in the README and the setup guide.
- New scenarios for each fix above, plus a scheduled run with good settings: the schedule now has its own path past the settings check, so it gets its own test. Sixteen broken copies of the workflow (the nine above and seven for these fixes) each fail the test.

### Found and fixed in a third review

- **A big Slack report could point to an email that does not exist.** When there are more boards than fit in Slack's 50 blocks, the message ended with "N more boards are in the email report", also in a Slack only setup. The boards it left out are now named in that line with their score, and the email is only mentioned when one is sent.
- **Worst items cut from Slack were cut silently.** A board section that would pass Slack's 3,000 character limit dropped worst items from the end without saying so. It now ends with "and N more". Neither limit had a test; a new scenario sends a Slack only report over 50 boards with `TOP_OFFENDERS=25` and 200 character item names, and the mock Slack refuses anything over the limits, as Slack does.
- **"Without RUN_NOW_SECRET, Run Now refuses every call" had no test.** It holds, but only because of the "secret is set" condition: the IF node compares loosely, so with the secret empty, a missing or empty `X-Run-Secret` header equals it, and a broken copy without that condition started a full report for both. A new scenario calls a copy with the secret unset, once without the header and once with an empty one, and both get 401.
- **Long board names could break the log save.** monday.com item names are 1 to 255 characters, and a new log row is named "<board name> <date>". The board name part is now capped at 240 characters.
- **The test report did not show the final run.** Its results block still held the 44 check output from the build, and the broken copy table stopped at nine. Both now come from the final run. Three more broken copies (no "secret is set" condition, no section trim, the old note) each fail the test, nineteen in all.
- The README now also says what happens when the log save outlasts its timeout: monday.com answers the retries with 409 `IDEMPOTENCY_CONFLICT`, nothing is duplicated, but the report can call saved rows unsaved.
