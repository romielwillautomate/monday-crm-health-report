# Setup: monday.com, Slack, Resend and n8n

This takes about 30 minutes. You need a monday.com account that can see the boards you want to check, n8n 2.x that you host yourself, and at least one place for the report: a Slack incoming webhook, an email address through Resend, or both.

## 1. monday.com API token

1. In monday.com, click your profile picture in the top right corner.
2. Select **Developers**. The Developer Center opens in a new tab.
3. Click **API token**, then **Show**, and copy it into `MONDAY_API_TOKEN`.

Account admins can also find it under **Administration → Connections → Personal API token**.

A personal token sees exactly what its user sees in monday.com. If a board is private or the user is not a member, the report says "board not found, archived, or the API token has no access to it". For a team setup, create the token from a user who is a member of every board on the list.

## 2. Board IDs

Open each board in a web browser. The ID is the number in the address bar:

```
https://yourcompany.monday.com/boards/1234567890
                                      ^^^^^^^^^^
```

The desktop and mobile apps do not show it.

## 3. Column IDs

Column IDs are not the column titles. A column titled "Email" might have the ID `email`, `email_1` or `email_mkq1a2b3`.

**Option A, in the browser:** click your profile picture, open **monday.labs**, turn on **Developer mode**. Each column's menu (the arrow next to its title) now shows its ID; clicking the ID copies it.

**Option B, in the API playground** (Developer Center → API playground):

```graphql
query {
  boards(ids: [1234567890]) {
    name
    columns { id title type }
  }
}
```

For each board, write down:

| What | Column type | Used for |
|---|---|---|
| The contact's email | Email or Text | Duplicates and format check |
| The contact's phone | Phone or Text | Duplicates and format check |
| Fields that must be filled in | Any type except Formula | Missing fields check |
| The deal stage or status | Status | Done items are never stale |
| The owner | People | No owner check |

Then the exact labels that mean "finished" on the status column, for example `Closed Won` and `Closed Lost`. Put it all into `BOARDS_JSON`, on one line:

```
BOARDS_JSON=[{"id":"1234567890","name":"Sales Pipeline","required_columns":["email","phone","text_company"],"email_column":"email","phone_column":"phone","status_column":"status","owner_column":"person","done_labels":["Closed Won","Closed Lost"]}]
```

The workflow checks the column IDs against each board on every run. A wrong ID does not produce a misleading score: the board is reported as "column ID(s) not on this board" with the ID to fix. The same goes for a Formula column, which monday.com returns without text.

For a text column used as the email or phone column, placeholders such as `n/a` or `none` are counted as invalid and never matched as duplicates, so they do not turn unrelated contacts into one group.

## 4. The CRM Health Log board (optional)

This board collects one row per checked board per day, so you can see whether the data is getting better.

1. Create a board called **CRM Health Log**.
2. Add these columns. The names are up to you; the types matter.

   | Column | Type | Key in HEALTH_LOG_COLUMNS |
   |---|---|---|
   | Key | Text | `key` (required) |
   | Snapshot date | Date | `date` |
   | Board | Text | `board` |
   | Score | Numbers | `score` |
   | Items | Numbers | `items` |
   | With issues | Numbers | `issues` |
   | Duplicates | Numbers | `duplicates` |
   | Missing fields | Numbers | `missing` |
   | Stale | Numbers | `stale` |
   | No owner | Numbers | `no_owner` |
   | Invalid | Numbers | `invalid` |

3. Copy the board ID into `HEALTH_LOG_BOARD_ID`, look up the column IDs as in step 3, and map them in `HEALTH_LOG_COLUMNS`:

   ```
   HEALTH_LOG_COLUMNS={"key":"text_key","date":"date_snapshot","board":"text_board","score":"numbers_score","items":"numbers_items","issues":"numbers_issues","duplicates":"numbers_dupes","missing":"numbers_missing","stale":"numbers_stale","no_owner":"numbers_owner","invalid":"numbers_invalid"}
   ```

4. Optional: to put the rows into a specific group, set `HEALTH_LOG_GROUP_ID` to the group's ID (the API playground shows it with `boards(ids: [...]) { groups { id title } }`).

The Key column holds `<date>|<board id>`, for example `2026-10-20|1234567890`. That is how a second run on the same day finds and updates the row instead of adding another. Do not edit it by hand. An empty board gets a row with 0 items and no score; if a board was emptied after an earlier run that day, the re-run clears the score it wrote then.

The save request carries an `Idempotency-Key` header. monday.com keeps the answer to a keyed mutation for 30 minutes, so when n8n retries the save after an error or a lost answer, monday.com does not create the rows a second time.

## 5. Slack (optional)

1. Go to [api.slack.com/apps](https://api.slack.com/apps), click **Create New App → From scratch**, name it (for example "CRM Health") and pick your workspace.
2. Open **Incoming Webhooks**, turn them on, click **Add New Webhook to Workspace** and choose the channel.
3. Copy the webhook URL into `SLACK_WEBHOOK_URL`. The error alert workflow posts to the same URL.

Without Slack (email only), the report still goes out by email, but the error alert workflow has nowhere to post. A run that fails before the report, for example because monday.com is down, then only shows in n8n's Executions list. If you need alerts without Slack, change the last node of `error-alert-slack.json` to send them somewhere else.

## 6. Resend for email (optional)

1. Create an account at [resend.com](https://resend.com).
2. Under **Domains**, add your domain and create the DNS records Resend shows you. Wait until the domain shows as verified.
3. Under **API Keys**, create a key with **Sending access** and copy it into `RESEND_API_KEY`.
4. Set `REPORT_EMAIL_FROM` to an address on the verified domain, for example `CRM Reports <reports@example.com>`, and `REPORT_EMAIL_TO` to the people who should get it, separated by commas.

Every email is sent with an `Idempotency-Key` header, so if n8n retries after a lost answer, Resend does not send it twice.

## 7. n8n

1. Add the variables from `.env.example` to your n8n environment (or its `.env` file) and restart n8n. Keep `BOARDS_JSON` and `HEALTH_LOG_COLUMNS` on one line each.
2. Import `workflow/monday-crm-health-report.json` and `workflow/error-alert-slack.json`.
3. **Activate the error alert workflow.** n8n 2.x only runs error workflows that are active. An inactive one is skipped and the failure only shows in the n8n server log.
4. Open the report workflow's settings and choose the error alert workflow as its **Error Workflow**.
5. Activate the report workflow. The schedule now uses `REPORT_CRON` in n8n's time zone (`GENERIC_TIMEZONE`, or the workflow's own time zone setting). n8n reads `REPORT_CRON` when the workflow is activated, so after changing it, restart n8n and activate the workflow again.

**About the API version.** Requests carry `API-Version: 2026-07` (or whatever `MONDAY_API_VERSION` says). In October 2026 that is the version monday.com serves by default, although its release schedule already lists `2026-10` as current. Every request this workflow sends was checked against the published `2026-04`, `2026-07`, `2026-10` and `2027-01` schemas and is valid in all four, so moving to a newer version is a change of `MONDAY_API_VERSION` and a test run. monday.com announces each version's end of support at least six months ahead.

## 8. First run

Start a report on demand:

```
curl -X POST https://YOUR-N8N-DOMAIN/webhook/monday-crm-health-report -H "X-Run-Secret: YOUR_RUN_NOW_SECRET"
```

When the settings are fine, you get `202 {"status":"started", ...}` right away, and the report arrives in Slack, email or both (whatever you set up) when all boards are read, usually within a minute.

When a setting is wrong, nothing is read and you get 500 with the list of what to fix, for example:

```
{"status":"not started","message":"The CRM health report did not start. Fix these settings first.","problems":["MONDAY_API_TOKEN is empty","BOARDS_JSON is not valid JSON: Expected ',' or '}' after property value in JSON at position 42 (line 1 column 43)"]}
```

## Troubleshooting

If nothing arrives, open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| 401 from the curl command | The `X-Run-Secret` header does not match `RUN_NOW_SECRET`, or `RUN_NOW_SECRET` is empty |
| 500 from the curl command, or a run stopped at "Stop: Bad Config" | The `problems` list (and the error message) names every setting to fix, for example invalid JSON in `BOARDS_JSON` |
| Run stopped at "Stop: Monday Failed" with HTTP 401 | `MONDAY_API_TOKEN` is wrong or was regenerated, or your monday.com account only allows API calls from certain IP addresses and n8n's is not one of them |
| Run stopped at "Stop: Monday Failed" with "does not accept API version" | monday.com no longer serves the version in `MONDAY_API_VERSION`. Set a supported one (see "About the API version" above) |
| Run stopped at "Stop: Monday Failed" with `USER_ACCESS_DENIED` | The token's user may not use the API. monday.com requires an active user who is not a viewer and has a confirmed email address |
| A board shows `REQUEST_MAX_COMPLEXITY_EXCEEDED` | One page asks for more than monday.com allows in a single query. Lower `MONDAY_PAGE_SIZE` |
| A board shows "HTTP 403: UserUnauthorizedException" | monday.com does not allow the token's user to read that board. Check that user's permissions on the board, or use a token from a user who is a member of it |
| A board shows "board not found, archived, or the API token has no access to it" | Wrong board ID, or the token's user is not a member of that board |
| A board shows "column ID(s) not on this board" | A column ID in `BOARDS_JSON` is wrong or the column was deleted |
| A board shows "formula column(s) cannot be checked" | A column in `BOARDS_JSON` is a Formula column. monday.com returns no text for those, so remove it or point to a regular column |
| A board shows "stopped after 200 pages (MONDAY_MAX_PAGES)" | The board has more items than `MONDAY_PAGE_SIZE` × `MONDAY_MAX_PAGES`. Raise one of them |
| Slack says "Health log NOT saved" with `ColumnValueException` | A column ID in `HEALTH_LOG_COLUMNS` is wrong, or its column has the wrong type (see step 4) |
| Slack says "Health log NOT saved" with `InvalidColumnIdException` | The `key` column in `HEALTH_LOG_COLUMNS` is not on the log board, for example because it was deleted and made again with a new ID |
| The report arrives but no alert ever does when a run fails | The error alert workflow is not active, or not chosen as the Error Workflow |
