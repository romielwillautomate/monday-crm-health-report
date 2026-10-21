# Test report

**Date:** 2026-10-21
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 60 of 60 checks pass across 36 scenarios

## How it was tested

No real monday.com account, Slack workspace or email account was used. The test script:

1. starts mock monday.com, Slack and Resend APIs on the test machine (the mock binds its port exclusively, so a port already in use stops the test instead of sharing requests with another server),
2. imports a temporary copy of the workflow with every API host pointed at the mocks and fake keys, plus a temporary copy of the error alert workflow (activated, and set as the copy's Error Workflow); seven more short lived copies cover a Slack only setup, a Slack only report over 50 boards, Run Now with `RUN_NOW_SECRET` left empty, an email only setup, broken settings on Run Now, and the schedule with good and with broken settings,
3. starts each run through the Run Now webhook (and two through the schedule), with mock faults switched on per scenario,
4. waits for each n8n run to finish, then checks every request the workflow made, what each node produced and the node each run ended on,
5. deletes the temporary copies.

### How strict the mocks are

- **monday.com:** every request is parsed and validated against [monday-api-subset.graphql](../tests/monday-api-subset.graphql), the parts of monday.com's published 2026-07 schema this workflow touches, copied unchanged. A misspelled field, a wrong argument or variable type, or an unused variable gets the same GraphQL error the real API returns. The mock also requires the token in `Authorization` (no `Bearer`), an `API-Version` monday.com serves (anything else gets HTTP 200 with `InvalidVersionException`, as the real API answers), a JSON body with `query` and `variables` as an object, a page limit from 1 to 500, and cursors it actually issued. Like monday.com, it honours `Idempotency-Key` on mutations only: a mutation sent again with a key it has seen gets the first answer back with `Idempotency-Replayed: true` and runs nothing, and one sent while the first is still running gets 409 `IDEMPOTENCY_CONFLICT`. Answers have the real shapes: `data` plus `errors` with `extensions.code`, partial data on errors, `complexity { query after reset_in_x_seconds }`, and column values typed per column (an email column's plain text also holds the label, a mirror column's text is `null`, a formula column's text is empty), so the workflow only passes if it reads the right fields. Writes follow monday.com's rules too: a numbers column takes a string, an empty string is stored as 0 and `null` clears it. Each error comes back with the HTTP status monday.com documents for it: most with 200, `UserUnauthorizedException` with 403, rate limits with 429.
- **Slack:** requires JSON with a `text` field and rejects blocks over Slack's limits (50 blocks, 3,000 characters per section, 150 per header) with `invalid_blocks`.
- **Resend:** requires a `User-Agent` header (403 without one, as Resend does), `Authorization: Bearer`, `from`, `to`, `subject` and `html` or `text`, valid addresses, and honours `Idempotency-Key` (same key and body returns the first email's ID without sending; same key with a different body is a 409).

The first three checks send requests straight at the mocks to prove they reject malformed ones and replay a repeated idempotent mutation.

### Check against the real schema

The mock uses a trimmed schema, so as a separate check all 224 monday.com requests the workflow sent in the final run (158 from the main copy, 66 from the Slack only, 50 board, email only and scheduled copies; the broken settings and empty secret copies stop before any request) were validated against monday.com's full published schemas, downloaded on 2026-10-21, including the variable values. Two more requests were refused on purpose before the schema check (the wrong token and the retired API version scenarios), so they are not in this count.

| Schema version | Status on 2026-10-21 | Requests | Distinct queries | Invalid |
|---|---|---|---|---|
| 2026-04 | maintenance | 224 | 8 | 0 |
| 2026-07 | served by default (the workflow's default) | 224 | 8 | 0 |
| 2026-10 | current in monday.com's release schedule | 224 | 8 | 0 |
| 2027-01 | release candidate | 224 | 8 | 0 |

### Do the checks catch real mistakes?

Nineteen deliberately broken copies of the workflow were run through the final test. Each one failed it:

| Broken copy | Checks failed |
|---|---|
| Reads `text` for every column (mirror columns look empty) | 10 of 60 (Partner Leads scores 0) |
| Compares emails with case (misses `Jordan.Lee@Example.COM`) | 11 of 60 (3 duplicates instead of 5, score 87) |
| Ignores the done labels (closed deals become stale) | 5 of 60 (7 stale instead of 5, wrong worst items) |
| Treats HTTP 423 "resource is currently locked" as a permanent failure | 1 of 60 (Partner Leads left out of the report) |
| Ignores the `errors` list and trusts the HTTP status only | 11 of 60 (a 200 error looks like a missing board, the daily limit is retried) |
| Matches any email, valid or not, as a duplicate key | 4 of 60 (the two `n/a` items become a 4th duplicate group) |
| Scores a board that lists a formula column | 1 of 60 (Partner Leads scored instead of flagged) |
| Leaves the score alone when a board is now empty | 1 of 60 (the earlier score stays on today's row) |
| Clears the score with an empty string instead of `null` | 1 of 60 (monday.com stores 0) |
| Saves the health log without an `Idempotency-Key` | 4 of 60 (after the lost answer, the retry creates the rows again) |
| Sends the health log save once, without retries | 2 of 60 (one 500 or dropped connection and no row is saved) |
| Answers Run Now before the settings check | 1 of 60 (broken settings get 202 "started" instead of 500 and the list) |
| Treats `InvalidVersionException` as a problem of one board | 2 of 60 (a report where no board was checked, instead of a stop with the fix) |
| Treats `USER_ACCESS_DENIED` as a problem of one board | 1 of 60 (a report where no board was checked, instead of a stop) |
| Treats `REQUEST_MAX_COMPLEXITY_EXCEEDED` like any other GraphQL error | 1 of 60 (the board is flagged without the hint to lower `MONDAY_PAGE_SIZE`) |
| Leaves the "ok, schedule" output of Config Valid? unconnected | 1 of 60 (a scheduled run with good settings reads nothing and sends nothing) |
| Drops the "RUN_NOW_SECRET is set" condition from Run Now Authorized? | 2 of 60 (with the secret unset, a call with no header or an empty one starts a full report) |
| Never shortens a Slack board section | 1 of 60 (the long section is cut in the middle of an item with "(shortened)", and nobody learns how many items were left out) |
| Says "N more boards are in the email report" for the boards Slack has no room for | 1 of 60 (a Slack only setup is pointed at an email that does not exist, and the boards are not named) |

The first five come from the build, the next four are the bugs fixed in the first review, the next seven cover the fixes of the second review, and the last three cover the third review (see the changelog).

### Repeat runs

The final workflow was run through the test five times in a row: 60 of 60 every time, about 70 seconds per run. The last run, shown below, used the final test script; the four before it differ from it only in a comment. Earlier rounds saw three one off failures, each traced to the test rather than the workflow and fixed there: a mock port another program could share on Windows (the mock now binds it exclusively), item #110 planted exactly 60 days old so a clock a few seconds off made it "59 days" (it is now 60 and a half days old), and two timing gaps in the script (it now matches every alert to its run through the execution link inside it, and waits for an execution's final status before it checks it).

## The test data

Three boards, built so every number can be worked out by hand:

- **Sales Pipeline:** 130 items, read in pages of 50 (`MONDAY_PAGE_SIZE=50`), so 3 pages. Required columns Email, Phone and Company; a Stage status column with done labels Closed Won and Closed Lost; an Owner people column. `STALE_DAYS=30`.
- **Partner Leads:** 12 clean items. Email is a text column, Phone a real phone column, and a required "Account tier" mirror column. No owner column, so that check is skipped.
- **Old Imports:** empty.

The planted problems on Sales Pipeline:

| Items | What is wrong | Counts as |
|---|---|---|
| #10, #60, #110 | Same email, on pages 1, 2 and 3, in different case and with a space | 1 group, 2 duplicates (#10 is oldest) |
| #20, #120 | Same phone as `+1 (555) 010-2001` and `555.010.2001` | 1 group, 1 duplicate |
| #30, #80, #125 | #30 and #80 share a phone, #80 and #125 an email | 1 group of 3, 2 duplicates |
| #40, #41, #42, #43, #110 | Empty Company, Email, Phone and Company, Email and Phone, Company | 5 items missing fields |
| #50, #51, #52 | Open, no update for 45 days | stale |
| #53 | No status at all, no update for 31 days | stale |
| #110 | Open, no update for 60 days | stale |
| #54, #55 | Closed Won and Closed Lost, 90 and 120 days old | not stale (done) |
| #56 | Open, 29 days old | not stale yet |
| #54, #65, #66, #110 | No owner | 4 without owner |
| #70, #71, #72 | `n/a`; `555-01`; `N/A` and `call me` | 3 invalid (2 emails, 2 phones). #70 and #72 hold the same placeholder but are not duplicates |

Items with at least one issue: 5 duplicates + 4 more with missing fields + 4 more stale + 3 more without an owner + 3 invalid = 19. #110 has four issues and counts once. Score: round(100 × 111 ÷ 130) = 85.

## Results

```
Mock self-test
  PASS  mock monday.com rejects what the real API rejects  (typo 200, wrong variable type 200, string variables 400, no token 401, unknown API version 200 InvalidVersionException)
  PASS  mock monday.com replays a mutation sent again with the same Idempotency-Key  (3 requests, 2 keys, 2 items created)
  PASS  mock Slack and Resend reject malformed payloads  (Slack 400 invalid_blocks, Resend without subject 422, without User-Agent 403)

Run Now, wrong secret
  PASS  refused with 401, settings not even read, nothing sent, alert raised  (HTTP 401, ended at Stop: Bad Run Now Secret)

Run Now, no secret header
  PASS  refused with 401, settings not even read, nothing sent, alert raised  (HTTP 401, ended at Stop: Bad Run Now Secret)

Full report
  PASS  Run Now answers 202 once the settings check passes, and the run succeeds  (HTTP 202, success, ended at Any Problems?)
  PASS  snapshot date is today in REPORT_TIMEZONE  (2026-10-21)
  PASS  Sales Pipeline read in 3 pages, each cursor from the page before  (FirstPage(50 items) then NextPage(50 items) then NextPage(30 items) then FirstPage(12 items) then FirstPage(0 items))
  PASS  every monday.com request: token, API-Version 2026-07, valid against the real schema  (7 requests: FirstPage, NextPage, NextPage, FirstPage, FirstPage, FindLogRows, SaveHealthLog)
  PASS  page size and column IDs sent as GraphQL variables  ({"boardIds": ["1001001001"], "columnIds": ["email_mkq1a2b3", "text_mkq1c4d5", "text_mkq1e6f7", "status", "person"], "limit": 50})
  PASS  Sales Pipeline: exact counts  ({"items": 130, "issueItems": 19, "duplicates": 5, "duplicateGroups": 3, "missingRequired": 5, "missingByField": {"Company": 3, "Email": 2, "Phone": 2}, "stale": 5, "noOwner": 4, "invalid": 3, "invalidEmail": 2, "invalidPhone": 2})
  PASS  Sales Pipeline: score 85 (111 clean of 130), grade Fair  (score 85, Fair)
  PASS  Sales Pipeline: worst 5 items in the right order with the right issues  (#110, #042, #043, #072, #054 (#110: duplicate of Jordan Lee, missing Company, no update in 60 days, no owner))
  PASS  Partner Leads: 12 items, no issues (mirror column read from display_value), score 100, no owner check  (12 items, score 100)
  PASS  Old Imports: empty board, no score  (empty)
  PASS  one Slack message with a summary per board and the worst items (names escaped)  (1 message(s), 9 blocks)
  PASS  one HTML email to both recipients, names escaped, idempotency key set  (CRM health report 2026-10-21: lowest score 85/100 (Sales Pipeline))
  PASS  health log: one lookup by key, then 3 rows created in one request that carries an Idempotency-Key  (1 lookup, 1 save, key crm-health-log/t4HsNeNwhmob3zit/2026-10-21/15543)
  PASS  health log rows hold the exact numbers  (3 rows, Sales Pipeline score 85)

Re-run on the same day
  PASS  no new log rows: the 3 existing rows are updated  (3 rows, 3 updates)
  PASS  same numbers, a fresh Slack report and a new email key  (score 85, 1 Slack message, 1 email)

Board emptied since the last run today
  PASS  its log row is updated to 0 items and the old score is cleared with null (not kept, not 0)  (score sent None, row score cleared, items 0)

Complexity budget exhausted (429)
  PASS  waits the retry_in_seconds asked for (capped by MONDAY_MAX_WAIT_SECONDS), retries the same cursor, counts unchanged  (6 page reads, asked 30s (retry_in_seconds), waited 1s for: HTTP 429, COMPLEXITY_BUDGET_EXHAUSTED: Complexity budget exhausted)

Complexity budget low
  PASS  keeps the page, then pauses for the budget reset before the next page  (complexity budget low (5 left, one page costs 3700))

monday.com 500, then 423 locked
  PASS  pauses each time (10s back off, then the 7s Retry-After), then the report is complete  (5 first-page requests, waits [(10, 'backoff'), (7, 'Retry-After')], Partner Leads score 100)

GraphQL error with HTTP 200, temporary
  PASS  API_TEMPORARILY_BLOCKED is not read as an empty board: pause, retry, report complete  (4 first-page requests, Partner Leads 12 items, score 100, no error)

Board refused (HTTP 403) or deleted
  PASS  both boards reported as not checked, Sales Pipeline still scored  (HTTP 403: UserUnauthorizedException: User unauthorized to perform action / board not found, archived, or the API token has no access to it)
  PASS  log row only for the board that was checked, run flagged and alert sent  (ended at Stop: Report Problems)

GraphQL error with HTTP 200, permanent (cursor expired on page 3)
  PASS  board named as not checked; the 100 items already read are not scored; no retry  (CursorException: The cursor is invalid or has expired)
  PASS  the board's log row keeps its last good numbers, run flagged and alert sent  (log rows saved for 2 boards, ended at Stop: Report Problems)

Column ID no longer on the board
  PASS  board flagged with the missing column ID, nothing paged, others still run  (column ID(s) not on this board: text_mkq1e6f7. Fix BOARDS_JSON)

Formula column as a required field
  PASS  board flagged with the column ID instead of a misleading score, others still run  (formula column(s) cannot be checked: text_mkq2e5f6. Remove them from BOARDS_JSON)

One page over the 5M complexity limit (REQUEST_MAX_COMPLEXITY_EXCEEDED)
  PASS  not retried, the board is named with the fix, the others still run  (REQUEST_MAX_COMPLEXITY_EXCEEDED: Query has complexity of 5600000, which exceeds max complexity of 5000000. One page is too expensive, lower MONDAY_PAGE_SIZE (now 50))

monday.com down (500 every time)
  PASS  3 tries, then the run stops with an alert and no half report  (3 requests, ended at Stop: Monday Failed)

API token rejected (401)
  PASS  stops at once without retrying, no report  (1 request, ended at Stop: Monday Failed)

Daily call limit reached
  PASS  stops at once (waiting would not help), no report  (1 request, ended at Stop: Monday Failed)

API version no longer served (InvalidVersionException, HTTP 200)
  PASS  stops at once with the version to fix, no report  (1 request, ended at Stop: Monday Failed)

API token user may not use the API (USER_ACCESS_DENIED, HTTP 403)
  PASS  stops at once instead of reporting every board as not checked  (1 request, ended at Stop: Monday Failed)

monday.com unreachable (connection dropped)
  PASS  the HTTP node retries 3 times, then the run stops with an alert  (3 requests, ended at Stop: Monday Failed)

Health log lookup fails (500)
  PASS  retried 3 times, nothing written, the report still goes out and says so  (:warning: Health log NOT saved: monday.com did not accept the request (HTTP 500: Internal server error). Run t)

Health log key column missing (GraphQL error with HTTP 200)
  PASS  lookup not retried, nothing written, the report still goes out and names the column  (:warning: Health log NOT saved: could not look up existing rows (InvalidColumnIdException: Column text_key not found). R)

Health log save fails (500)
  PASS  tried 3 times with one Idempotency-Key, rows untouched, report and email still go out and say so  (3 save requests, 1 key, ended at Stop: Report Problems)

Health log save fails for one row
  PASS  the other rows are saved, Slack says so, run flagged  (save request as expected, 2 rows, ended at Stop: Report Problems, alert received; Slack: :warning: Health log NOT fully saved: Partner Leads: ColumnValueException: This column ID doesn't ex)
  PASS  the next run creates only the missing row, no duplicates  (3 rows, keys unique: True)

Health log save answer lost
  PASS  the retry carries the same key and body, monday.com replays the first answer, 3 rows and no duplicates  (2 save requests, replayed True, 3 rows, run success)

Slack down
  PASS  retried 3 times, the email still goes out, run flagged  (3 Slack tries, ended at Stop: Report Problems)

Email answer lost after sending
  PASS  the retry reuses the idempotency key, so only one email goes out  (2 requests, same key True, replayed True, 1 email delivered, run success)

Email refused (sending domain not verified)
  PASS  tried 3 times with one idempotency key, nothing sent, the Slack report still goes out, run flagged and alert sent  (3 tries, 1 key, 0 emails sent, ended at Stop: Report Problems)

After every run
  PASS  the health log still has exactly one row per board for 2026-10-21  (3 rows after 29 runs)
  PASS  no monday.com request was ever rejected as malformed (schema, variables, headers)  (160 requests, 0 malformed, 2 refused on purpose (wrong token, retired API version))

Slack only (no log board, no email)
  PASS  one Slack report, no log calls, no email, run succeeds  (1 Slack message, 0 emails, footer: e for more than 30 days. Run by Run Now.)

Big report, Slack only (50 boards)
  PASS  Slack accepts it: exactly 50 blocks, 46 boards shown, the other 4 named with their score  (1 message, 50 blocks, 46 boards shown, note: 4 more boards are not shown here, because Slack allows 50 bl)
  PASS  a section over the limit drops worst items and says how many: no section over 3,000 characters  (19 worst items found, 7 listed, section 2576 characters, 52 page reads)

Run Now, RUN_NOW_SECRET not set, empty X-Run-Secret header
  PASS  refused with 401, settings not even read, nothing sent  (header n8n received '', HTTP 401, ended at Stop: Bad Run Now Secret)

Run Now, RUN_NOW_SECRET not set, no header
  PASS  refused with 401, settings not even read, nothing sent  (header n8n received None, HTTP 401, ended at Stop: Bad Run Now Secret)

Email only, page limit reached
  PASS  Sales Pipeline stops at MONDAY_MAX_PAGES and is named as not checked, never half scored  (stopped after 2 pages (MONDAY_MAX_PAGES); the board has more items)
  PASS  no Slack or log calls, one email that names the board, run flagged so the error workflow fires  (CRM health report 2026-10-21: lowest score 100/100 (Partner Leads), 1 board not checked)

Run Now, broken settings
  PASS  answers 500 with every setting to fix, reads nothing, run stops with an alert  (HTTP 500, problems: ['MONDAY_API_TOKEN is empty', "BOARDS_JSON is not valid JSON: Expected ',' or '}' after property value in JSON at position 42 (line 1 column 43)"])

Scheduled run, good settings
  PASS  the schedule fires from REPORT_CRON and runs the whole report, with no Run Now answer involved  (mode trigger, ended at Any Problems?, 1 Slack report, footer: 30 days. Run by schedule.)

Scheduled run, broken BOARDS_JSON
  PASS  the schedule fires from REPORT_CRON, stops before any API call, alert sent  (mode trigger, ended at Stop: Bad Config)

60/60 checks passed
```

The main copy made 29 runs. Its "160 requests" check counts every monday.com request it sent, including the two refused on purpose; the schema table above counts the 158 the mock parsed, plus 66 from the other copies.

## What the workflow sent (full report scenario)

**monday.com: first page of a board** (`POST /v2`, headers `Authorization: <token>` and `API-Version: 2026-07`)

```graphql
query FirstPage($boardIds: [ID!], $columnIds: [String!], $limit: Int!) {
  complexity { query after reset_in_x_seconds }
  boards(ids: $boardIds) {
    id name url
    columns(ids: $columnIds) { id title type }
    items_page(limit: $limit) {
      cursor
      items {
        id name url created_at updated_at
        column_values(ids: $columnIds) {
          id type text
          ... on EmailValue { email }
          ... on PhoneValue { phone }
          ... on MirrorValue { display_value }
          ... on BoardRelationValue { display_value }
          ... on DependencyValue { display_value }
        }
      }
    }
  }
}
```

```json
{"boardIds": ["1001001001"], "columnIds": ["email_mkq1a2b3", "text_mkq1c4d5", "text_mkq1e6f7", "status", "person"], "limit": 50}
```

**monday.com: every later page** uses `next_items_page(cursor: $cursor, limit: $limit)` with the same item fields and the cursor from the page before:

```json
{"cursor": "MSwxMDAxMDAxMDAxLDUwLDgzMDM0NTQz", "columnIds": ["email_mkq1a2b3", "text_mkq1c4d5", "text_mkq1e6f7", "status", "person"], "limit": 50}
```

**monday.com: find today's log rows**

```graphql
query FindLogRows($board: ID!, $columns: [ItemsPageByColumnValuesQuery!], $keyColumn: [String!], $limit: Int!) {
  items_page_by_column_values(board_id: $board, columns: $columns, limit: $limit) {
    cursor
    items { id name column_values(ids: $keyColumn) { id text } }
  }
}
```

```json
{"board": "2000000001", "columns": [{"column_id": "text_key", "column_values": ["2026-10-21|1001001001", "2026-10-21|1002002002", "2026-10-21|1003003003"]}], "keyColumn": ["text_key"], "limit": 500}
```

**monday.com: save the log rows** (first run of the day, so all three are created; on a re-run the same aliases use `change_multiple_column_values` with the item IDs found above). This request also carries `Idempotency-Key: crm-health-log/<workflow id>/2026-10-21/<execution id>`, the same on every n8n retry of the run, so a retry after a lost answer gets monday.com's first answer back instead of creating the rows again.

```graphql
mutation SaveHealthLog($board: ID!, $name0: String!, $values0: JSON!, $group: String, $name1: String!, $values1: JSON!, $name2: String!, $values2: JSON!) {
  row0: create_item(board_id: $board, group_id: $group, item_name: $name0, column_values: $values0) { id }
  row1: create_item(board_id: $board, group_id: $group, item_name: $name1, column_values: $values1) { id }
  row2: create_item(board_id: $board, group_id: $group, item_name: $name2, column_values: $values2) { id }
}
```

The Sales Pipeline row (`values0`, sent as a JSON string as monday.com expects):

```json
{"text_key": "2026-10-21|1001001001", "date_snap": {"date": "2026-10-21"}, "text_board": "Sales Pipeline", "numbers_score": "85", "numbers_items": "130", "numbers_issues": "19", "numbers_dupes": "5", "numbers_missing": "5", "numbers_stale": "5", "numbers_owner": "4", "numbers_invalid": "3"}
```

When a re-run updates the row of a board that is now empty, it sends `"numbers_score": null`, which clears the score written earlier that day. An empty string would not: monday.com stores it as 0.

**Slack: the report** (`POST` to the incoming webhook)

```json
{
 "text": "CRM health report 2026-10-21: Sales Pipeline 85/100, Partner Leads 100/100, Old Imports empty",
 "blocks": [
  {"type": "header", "text": {"type": "plain_text", "text": "CRM health report, 2026-10-21"}},
  {"type": "section", "text": {"type": "mrkdwn", "text": "*3 of 3 boards checked*, 142 items, 19 with at least one issue."}},
  {"type": "divider"},
  {"type": "section", "text": {"type": "mrkdwn", "text": ":large_yellow_circle: *<https://example-crm.monday.com/boards/1001001001|Sales Pipeline>*: score *85/100* (Fair)\n130 items, 19 with issues: 5 duplicates, 5 missing required fields, 5 stale, 4 without an owner, 3 with an invalid email or phone.\n*Worst items*\n• <https://example-crm.monday.com/boards/1001001001/pulses/5000000110|Morgan Diaz &amp; Co &lt;VIP&gt;>: duplicate of Jordan Lee, missing Company, no update in 60 days, no owner\n• <https://example-crm.monday.com/boards/1001001001/pulses/5000000042|Blake Carter>: missing Phone, Company\n• <https://example-crm.monday.com/boards/1001001001/pulses/5000000043|Cameron Dalton>: missing Email, Phone\n• <https://example-crm.monday.com/boards/1001001001/pulses/5000000072|Noel Grant>: invalid email, invalid phone\n• <https://example-crm.monday.com/boards/1001001001/pulses/5000000054|Peyton Baker>: no owner"}},
  {"type": "divider"},
  {"type": "section", "text": {"type": "mrkdwn", "text": ":large_green_circle: *<https://example-crm.monday.com/boards/1002002002|Partner Leads>*: score *100/100* (Healthy)\n12 items, no issues found."}},
  {"type": "divider"},
  {"type": "section", "text": {"type": "mrkdwn", "text": ":white_circle: *<https://example-crm.monday.com/boards/1003003003|Old Imports>*: empty board, nothing to check."}},
  {"type": "context", "elements": [{"type": "mrkdwn", "text": "Score = share of items with no issues. Stale = not done and no update for more than 30 days. Health log: 3 new, 0 updated. Run by Run Now."}]}
 ]
}
```

**Resend: the email** (`POST /emails`, header `Idempotency-Key: crm-health-report/<workflow id>/2026-10-21/<execution id>`)

```
From: CRM Reports <reports@example.com>
To: ops@example.com, sales.lead@example.com
Subject: CRM health report 2026-10-21: lowest score 85/100 (Sales Pipeline)
```

The HTML body has a table with one row per board (score, items, with issues, duplicates, missing fields, stale, no owner, invalid) and the worst items per board as links. The plain text version:

```
CRM health report, 2026-10-21
3 of 3 boards checked, 142 items, 19 with at least one issue.

Sales Pipeline: score 85/100 (Fair). 130 items, 19 with issues: 5 duplicates, 5 missing required fields, 5 stale, 4 without an owner, 3 with an invalid email or phone.
  Morgan Diaz & Co <VIP>: duplicate of Jordan Lee, missing Company, no update in 60 days, no owner (https://example-crm.monday.com/boards/1001001001/pulses/5000000110)
  Blake Carter: missing Phone, Company (https://example-crm.monday.com/boards/1001001001/pulses/5000000042)
  Cameron Dalton: missing Email, Phone (https://example-crm.monday.com/boards/1001001001/pulses/5000000043)
  Noel Grant: invalid email, invalid phone (https://example-crm.monday.com/boards/1001001001/pulses/5000000072)
  Peyton Baker: no owner (https://example-crm.monday.com/boards/1001001001/pulses/5000000054)

Partner Leads: score 100/100 (Healthy). 12 items, no issues found.

Old Imports: empty board, nothing to check.

Score = share of items with no issues. Stale = not done and no update for more than 30 days. Health log: 3 new, 0 updated.
```

All names, companies, emails, phone numbers and board IDs above are made up.
