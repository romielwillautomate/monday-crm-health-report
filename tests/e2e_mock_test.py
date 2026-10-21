"""End-to-end test for the Monday.com CRM Health Report against MOCK APIs.

No real monday.com account, Slack workspace or email account is used. The script:
  1. starts mock monday.com, Slack and Resend APIs on this machine,
  2. imports temporary copies of the workflow and the error alert workflow into your n8n
     (API hosts pointed at the mocks, fake keys),
  3. plays every scenario: Run Now security, a full report over three boards (one of them read in three pages),
     a re-run on the same day, a board emptied since the last run, rate limits, the complexity budget, 500 and
     423 answers, outages and dropped connections, GraphQL errors returned with HTTP 200 (temporary and
     permanent), a board refused with 403, a deleted board, a wrong column ID, a formula column, a page over the
     complexity limit, a retired API version, a user without API access, health log lookup and save failures
     (including a save whose answer is lost, retried with monday.com's Idempotency-Key), Slack and email problems,
     a Slack only setup, a Slack only report over 50 boards with long item names (Slack's block and section limits),
     Run Now with RUN_NOW_SECRET left empty, an email only setup that hits MONDAY_MAX_PAGES, broken settings on Run Now,
     and the schedule with good and with broken settings,
  4. checks every request the workflow made, prints PASS or FAIL per check, then deletes the temporary copies.

The mock monday.com API validates every request against monday.com's real GraphQL schema (the subset in
monday-api-subset.graphql, copied unchanged from the published 2026-07 schema). A misspelled field, a wrong
argument type or an unused variable is rejected exactly like the real API rejects it.

Requirements: Python 3.9+, `pip install graphql-core`, a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
  E2E_DUMP     optional: a file path to save every monday.com request the workflow made (JSON)
Usage: python tests/e2e_mock_test.py
"""
import base64, http.client, json, os, re, socket, sys, threading, time, urllib.error, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from graphql import GraphQLError, build_schema, execute_sync, parse, validate
    from graphql.language import OperationDefinitionNode
except ImportError:
    raise SystemExit('This test needs graphql-core: pip install graphql-core')

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8788
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
HERE = Path(__file__).resolve().parent
WORKFLOW = HERE.parent / 'workflow' / 'monday-crm-health-report.json'
ERROR_WORKFLOW = HERE.parent / 'workflow' / 'error-alert-slack.json'
SCHEMA = build_schema((HERE / 'monday-api-subset.graphql').read_text(encoding='utf-8'))
NAME = 'TEST monday-crm-health-report (temporary)'
PATH = 'monday-crm-health-report-e2e'
SECRET = 'test-run-secret'
TOKEN = 'mock-monday-token'
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DATE = NOW.strftime('%Y-%m-%d')

A, B, C = '1001001001', '1002002002', '1003003003'      # Sales Pipeline, Partner Leads, Old Imports
LOG_BOARD = '2000000001'
BOARDS_CONFIG = [
    {'id': A, 'name': 'Sales Pipeline', 'required_columns': ['email_mkq1a2b3', 'text_mkq1c4d5', 'text_mkq1e6f7'],
     'email_column': 'email_mkq1a2b3', 'phone_column': 'text_mkq1c4d5', 'status_column': 'status', 'owner_column': 'person',
     'done_labels': ['Closed Won', 'Closed Lost']},
    {'id': B, 'name': 'Partner Leads', 'required_columns': ['text_mkq2a1b2', 'text_mkq2e5f6', 'mirror_mkq2g7h8'],
     'email_column': 'text_mkq2a1b2', 'phone_column': 'phone_mkq2c3d4', 'status_column': 'status', 'owner_column': None,
     'done_labels': ['Done']},
    {'id': C, 'name': 'Old Imports', 'required_columns': ['email'], 'email_column': 'email', 'status_column': 'status',
     'owner_column': 'person', 'done_labels': ['Done']},
]
LOG_COLUMNS = {'key': 'text_key', 'date': 'date_snap', 'board': 'text_board', 'score': 'numbers_score', 'items': 'numbers_items',
               'issues': 'numbers_issues', 'duplicates': 'numbers_dupes', 'missing': 'numbers_missing', 'stale': 'numbers_stale',
               'no_owner': 'numbers_owner', 'invalid': 'numbers_invalid'}
ENV = {
    'MONDAY_API_TOKEN': TOKEN, 'MONDAY_API_VERSION': '2026-07', 'BOARDS_JSON': json.dumps(BOARDS_CONFIG),
    'STALE_DAYS': '30', 'MONDAY_PAGE_SIZE': '50', 'MONDAY_MAX_PAGES': '200', 'MONDAY_MAX_TRIES': '3', 'MONDAY_MAX_WAIT_SECONDS': '1',
    'TOP_OFFENDERS': '5', 'PHONE_DEFAULT_COUNTRY_CODE': '1', 'REPORT_TIMEZONE': 'UTC', 'REPORT_CRON': '0 0 1 1 *',
    'RUN_NOW_SECRET': SECRET, 'SLACK_WEBHOOK_URL': MOCK + '/slack/report',
    'RESEND_API_KEY': 're_mock_key', 'REPORT_EMAIL_FROM': 'CRM Reports <reports@example.com>',
    'REPORT_EMAIL_TO': 'ops@example.com, sales.lead@example.com',
    'HEALTH_LOG_BOARD_ID': LOG_BOARD, 'HEALTH_LOG_COLUMNS': json.dumps(LOG_COLUMNS), 'HEALTH_LOG_GROUP_ID': 'topics',
}
HOSTS = {'https://api.monday.com': MOCK + '/monday', 'https://api.resend.com': MOCK + '/resend'}


# ------------------------------------------------------------------ fixtures
def ts(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


FIRST = ['Avery', 'Blake', 'Cameron', 'Dakota', 'Emerson', 'Finley', 'Harper', 'Jules', 'Kendall', 'Logan', 'Marley', 'Noel', 'Oakley',
         'Peyton', 'Reese', 'Rowan', 'Sage', 'Tatum', 'Wren', 'Ellis']
LAST = ['Adams', 'Baker', 'Carter', 'Dalton', 'Ellison', 'Fischer', 'Grant', 'Hale', 'Irwin', 'Jarvis', 'Keller', 'Lawson', 'Mercer']
COMPANIES = ['Brightline Dental', 'Cedar Plumbing', 'Summit Roofing', 'Harbor Fitness', 'Maple Tutoring', 'Northwind Legal',
             'Oak Street Bakery', 'Pinecrest Realty', 'Riverbend HVAC', 'Bluebird Cleaning', 'Ironwood Auto', 'Lakeside Vets']
OWNERS = ['Alex Morgan', 'Sam Lee', 'Chris Patel']


def sales_contact(i):
    first, last = FIRST[(i - 1) % len(FIRST)], LAST[(i - 1) % len(LAST)]
    c = dict(name=f'{first} {last}', email=f'{first}.{last}{i}@example.com'.lower(), phone=f'+1 555 {i:03d} {i:04d}',
             company=COMPANIES[i % len(COMPANIES)], status=['New', 'Working on it'][i % 2], owner=OWNERS[i % 3],
             updated=NOW - timedelta(days=3), created=NOW - timedelta(days=400) + timedelta(hours=i))
    over = {
        # duplicates: the same person three times by email (pages 1, 2 and 3), case and spaces differ
        10: dict(name='Jordan Lee', email='jordan.lee@example.com'),
        60: dict(name='Jordan Lee', email=' Jordan.Lee@Example.COM'),
        # 110 is also a duplicate and has three more issues: the worst item on the board (name tests escaping)
        # (60 and a half days, so a clock a few seconds off between this machine and n8n cannot make it "59 days")
        110: dict(name='Morgan Diaz & Co <VIP>', email='jordan.lee@example.com', company='', owner='', status='New',
                  updated=NOW - timedelta(days=60, hours=12)),
        # the same phone in two formats
        20: dict(name='Riley Patel', phone='+1 (555) 010-2001'),
        120: dict(name='Riley Patel', phone='555.010.2001'),
        # a chain: 30 and 80 share a phone, 80 and 125 share an email, so all three are one contact
        30: dict(name='Casey Nguyen', phone='+1 555 777 0303', email='c.nguyen@example.com'),
        80: dict(name='Casey Nguyen', phone='+15557770303', email='casey.nguyen@example.com'),
        125: dict(name='C. Nguyen', email='CASEY.NGUYEN@example.com'),
        # missing required fields
        40: dict(company=''), 41: dict(email=''), 42: dict(phone='', company=''), 43: dict(email='', phone=''),
        # stale and not stale
        50: dict(updated=NOW - timedelta(days=45), status='Working on it'),
        51: dict(updated=NOW - timedelta(days=45), status='Working on it'),
        52: dict(updated=NOW - timedelta(days=45), status='New'),
        53: dict(updated=NOW - timedelta(days=31), status=''),                       # no status at all: stale
        54: dict(updated=NOW - timedelta(days=90), status='Closed Won', owner=''),   # done, so not stale; no owner
        55: dict(updated=NOW - timedelta(days=120), status='Closed Lost'),          # done, so not stale
        56: dict(updated=NOW - timedelta(days=29), status='New'),                   # 29 days: not stale yet
        # no owner
        65: dict(owner=''), 66: dict(owner=''),
        # invalid formats. 70 and 72 hold the same placeholder: invalid, but never a duplicate of each other
        70: dict(email='n/a'), 71: dict(phone='555-01'), 72: dict(email='N/A', phone='call me'),
    }
    c.update(over.get(i, {}))
    return c


COLS_A = [('email_mkq1a2b3', 'Email', 'email'), ('text_mkq1c4d5', 'Phone', 'text'), ('text_mkq1e6f7', 'Company', 'text'),
          ('status', 'Stage', 'status'), ('person', 'Owner', 'people'), ('date4', 'Next call', 'date')]
COLS_B = [('text_mkq2a1b2', 'Email', 'text'), ('phone_mkq2c3d4', 'Phone', 'phone'), ('text_mkq2e5f6', 'Partner company', 'text'),
          ('status', 'Status', 'status'), ('mirror_mkq2g7h8', 'Account tier', 'mirror')]
COLS_C = [('email', 'Email', 'email'), ('status', 'Status', 'status'), ('person', 'Owner', 'people')]
COLS_LOG = [('text_key', 'Key', 'text'), ('date_snap', 'Snapshot date', 'date'), ('text_board', 'Board', 'text')] + [
    (c, c.replace('numbers_', '').title(), 'numbers') for c in
    ('numbers_score', 'numbers_items', 'numbers_issues', 'numbers_dupes', 'numbers_missing', 'numbers_stale', 'numbers_owner', 'numbers_invalid')]


def board(bid, name, cols, items, groups=('topics',)):
    return {'id': bid, 'name': name, 'url': f'https://example-crm.monday.com/boards/{bid}', 'state': 'active',
            'columns': [{'id': i, 'title': t, 'type': ty} for i, t, ty in cols], 'groups': list(groups), 'items': items}


def item(bid, iid, name, created, updated, cells):
    return {'id': str(iid), 'name': name, 'url': f'https://example-crm.monday.com/boards/{bid}/pulses/{iid}', 'state': 'active',
            'created_at': ts(created), 'updated_at': ts(updated), 'cells': cells}


def fresh_boards():
    sales = []
    for i in range(1, 131):
        c = sales_contact(i)
        sales.append(item(A, 5000000000 + i, c['name'], c['created'], c['updated'], {
            'email_mkq1a2b3': c['email'], 'text_mkq1c4d5': c['phone'], 'text_mkq1e6f7': c['company'], 'status': c['status'],
            'person': c['owner'], 'date4': ''}))
    partners = [item(B, 6000000000 + i, f'Partner {i:02d}', NOW - timedelta(days=200 - i), NOW - timedelta(days=2), {
        'text_mkq2a1b2': f'partner{i:02d}@example.com', 'phone_mkq2c3d4': f'+1555888{i:04d}',
        'text_mkq2e5f6': COMPANIES[i - 1], 'status': 'Active', 'mirror_mkq2g7h8': ['Gold', 'Silver'][i % 2]}) for i in range(1, 13)]
    return {A: board(A, 'Sales Pipeline', COLS_A, sales), B: board(B, 'Partner Leads', COLS_B, partners),
            C: board(C, 'Old Imports', COLS_C, []), LOG_BOARD: board(LOG_BOARD, 'CRM Health Log', COLS_LOG, [])}


# ------------------------------------------------------------------ mock state
BOARDS = fresh_boards()
LOCK = threading.RLock()
LOG = []                                 # every request any mock received
CURSORS = {}                             # cursor -> (board id, offset)
EMAILS = []                              # emails Resend accepted (delivered)
IDEMPOTENCY = {}                         # Resend: Idempotency-Key -> first email
MONDAY_IDEMPOTENCY = {}                  # monday.com: (token, Idempotency-Key) -> first answer of a mutation
# API versions monday.com serves in October 2026 (maintenance, current, release candidate and older stable ones)
VERSIONS = {'2025-04', '2025-07', '2025-10', '2026-01', '2026-04', '2026-07', '2026-10', '2027-01'}
STATE = {'token': TOKEN, 'budget': 10_000_000, 'next_item': 7000000001, 'versions': set(VERSIONS)}
FAULT = {}


def reset_faults():
    FAULT.clear()
    FAULT.update(monday_http=[], low_budget=None, unauthorized=set(), hidden=set(), log_fail=None, slack_status=None, resend_lose=False,
                 resend_refuse=None, monday_lose=None)
    STATE['token'] = TOKEN
    STATE['budget'] = 10_000_000
    STATE['versions'] = set(VERSIONS)


reset_faults()

VALUE_TYPES = {'email': 'EmailValue', 'phone': 'PhoneValue', 'text': 'TextValue', 'status': 'StatusValue', 'people': 'PeopleValue',
               'date': 'DateValue', 'numbers': 'NumbersValue', 'long_text': 'LongTextValue', 'mirror': 'MirrorValue',
               'board_relation': 'BoardRelationValue', 'dependency': 'DependencyValue'}


def column_value(col, raw, item_name):
    """One ColumnValue object, shaped like monday.com returns it for that column type."""
    cid, ctype = col['id'], col['type']
    empty = raw in ('', None)
    cv = {'id': cid, 'type': ctype, 'is_leaf': True, 'column': col, 'text': '' if empty else raw, 'value': None}
    if ctype == 'email':
        # the plain text of an email column is "label - email", which is why the workflow reads the email field
        cv.update(email=None if empty else raw.strip(), label=None if empty else item_name,
                  text='' if empty else f'{item_name} - {raw.strip()}',
                  value=None if empty else json.dumps({'email': raw.strip(), 'text': item_name}))
    elif ctype == 'phone':
        cv.update(phone=None if empty else raw, country_short_name=None if empty else 'US',
                  value=None if empty else json.dumps({'phone': raw, 'countryShortName': 'US'}))
    elif ctype == 'status':
        cv.update(label=None if empty else raw, index=None if empty else 1, is_done=False,
                  value=None if empty else json.dumps({'index': 1, 'post_id': None}))
    elif ctype == 'people':
        people = [] if empty else [{'id': '4001', 'kind': 'person'}]
        cv.update(persons_and_teams=people, value=None if empty else json.dumps({'personsAndTeams': [{'id': 4001, 'kind': 'person'}]}))
    elif ctype == 'date':
        d = raw.get('date') if isinstance(raw, dict) else raw
        cv.update(date=d or None, text=d or '', value=None if not d else json.dumps({'date': d}))
    elif ctype == 'numbers':
        cv.update(number=None if empty else float(raw), value=None if empty else json.dumps(str(raw)))
    elif ctype in ('mirror', 'board_relation', 'dependency'):
        # these return null for text and value; the content is only in display_value
        cv.update(text=None, value=None, display_value='' if empty else raw)
    elif ctype == 'formula':
        # formula columns return an empty text and a null value; the result is only in a rate limited display_value
        cv.update(text='', value=None)
    else:
        cv['value'] = None if empty else json.dumps(raw)
    return cv


def monday_error(message, code, status=200, **data):
    return GraphQLError(message, extensions={'code': code, 'status_code': status, 'error_data': data})


def check_limit(limit):
    if limit is None or limit < 1 or limit > 500:
        raise monday_error('limit must be between 1 and 500', 'InvalidArgumentException', limit=limit)


def page_of(b, offset, limit, ctx):
    items = b['items'][offset:offset + limit]
    nxt = offset + len(items)
    cursor = None
    if nxt < len(b['items']):
        cursor = base64.b64encode(f"{len(CURSORS) + 1},{b['id']},{nxt},{uuid.uuid4().hex[:8]}".encode()).decode()
        CURSORS[cursor] = (b['id'], nxt)
    ctx['rec']['pages'].append({'board': b['id'], 'offset': offset, 'count': len(items), 'cursor': cursor})
    return {'cursor': cursor, 'items': items}


def r_boards(_, info, ids=None, **kw):
    out = []
    for bid in ids or []:
        if bid in FAULT['unauthorized']:
            raise monday_error('User unauthorized to perform action', 'UserUnauthorizedException', 403)
        if bid in BOARDS and bid not in FAULT['hidden'] and bid != LOG_BOARD:
            out.append(BOARDS[bid])
    return out


def r_items_page(b, info, limit=25, cursor=None, query_params=None, **kw):
    check_limit(limit)
    if cursor and query_params:
        raise monday_error("You can't use query_params and cursor in the same request", 'InvalidArgumentException')
    if cursor:
        return r_next_items_page(None, info, cursor=cursor, limit=limit)
    return page_of(b, 0, limit, info.context)


def r_next_items_page(_, info, cursor, limit):
    check_limit(limit)
    if cursor not in CURSORS:
        raise monday_error('The cursor is invalid or has expired', 'CursorException')
    bid, offset = CURSORS[cursor]
    return page_of(BOARDS[bid], offset, limit, info.context)


def r_columns(b, info, ids=None, **kw):
    return [c for c in b['columns'] if ids is None or c['id'] in ids]


def r_column_values(it, info, ids=None, **kw):
    b = BOARDS[it['url'].split('/boards/')[1].split('/')[0]]
    return [column_value(c, it['cells'].get(c['id'], ''), it['name']) for c in b['columns'] if ids is None or c['id'] in ids]


def r_complexity(_, info):
    ctx = info.context
    cost = ctx['cost']
    before = STATE['budget']
    if FAULT['low_budget'] and FAULT['low_budget'](ctx['rec']):
        before = cost + 5
    after = before - cost
    STATE['budget'] = max(after, 0) if not FAULT['low_budget'] else 10_000_000
    return {'before': before, 'after': after, 'query': cost, 'reset_in_x_seconds': 30}


def r_items_by_values(_, info, board_id, limit, columns=None, cursor=None, **kw):
    check_limit(limit)
    b = BOARDS.get(board_id)
    if not b:
        raise monday_error('Board not found', 'InvalidBoardIdException', board_id=board_id)
    found = []
    for it in b['items']:
        ok = True
        for rule in columns or []:
            col = next((c for c in b['columns'] if c['id'] == rule['column_id']), None)
            if not col:
                raise monday_error(f"Column {rule['column_id']} not found", 'InvalidColumnIdException', column_id=rule['column_id'])
            wanted = [str(v).lower() for v in rule['column_values'] if v is not None]
            if str(it['cells'].get(col['id'], '')).lower() not in wanted:
                ok = False
        if ok:
            found.append(it)
    return {'cursor': None, 'items': found[:limit]}


def parse_values(b, raw):
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise monday_error('column_values is not valid JSON', 'ColumnValueException')
    if not isinstance(values, dict):
        raise monday_error('column_values must be a JSON object', 'ColumnValueException')
    cols = {c['id']: c for c in b['columns']}
    for cid, v in values.items():
        col = cols.get(cid)
        if not col:
            raise monday_error("This column ID doesn't exist for the board", 'ColumnValueException', column_id=cid)
        t = col['type']
        bad = (t == 'text' and not isinstance(v, str)) \
            or (t == 'numbers' and not (v is None or (isinstance(v, str) and re.fullmatch(r'-?\d+(\.\d+)?|', v)))) \
            or (t == 'date' and not (isinstance(v, dict) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(v.get('date', '')))))
        if bad:
            raise monday_error(f'invalid value, please check our API documentation for the correct data structure for {t} columns',
                               'ColumnValueException', column_id=cid, column_type=t, column_value=v)
    return values


def apply_values(b, cells, values):
    """Writes column values the way monday.com does: for a numbers column, null clears it and an empty string becomes 0."""
    types = {c['id']: c['type'] for c in b['columns']}
    for cid, v in values.items():
        if types[cid] == 'numbers' and v is None:
            cells.pop(cid, None)
        elif types[cid] == 'numbers' and v == '':
            cells[cid] = '0'
        else:
            cells[cid] = v
    return cells


def r_create_item(_, info, board_id, item_name, group_id=None, column_values=None, **kw):
    b = BOARDS.get(board_id)
    if not b:
        raise monday_error('Board not found', 'InvalidBoardIdException', board_id=board_id)
    if group_id is not None and group_id not in b['groups']:
        raise monday_error('Group not found', 'InvalidGroupIdException', group_id=group_id)
    values = parse_values(b, column_values or '{}')
    if FAULT['log_fail'] and FAULT['log_fail'] in item_name:
        raise monday_error("This column ID doesn't exist for the board", 'ColumnValueException', column_id='numbers_score')
    with LOCK:
        iid = STATE['next_item']
        STATE['next_item'] += 1
    it = item(board_id, iid, item_name, NOW, NOW, apply_values(b, {}, values))
    it['group'] = group_id or b['groups'][0]
    b['items'].append(it)
    return it


def r_change_values(_, info, board_id, column_values, item_id=None, **kw):
    b = BOARDS.get(board_id)
    if not b:
        raise monday_error('Board not found', 'InvalidBoardIdException', board_id=board_id)
    it = next((i for i in b['items'] if i['id'] == str(item_id)), None)
    if not it:
        raise monday_error('Item not found', 'ResourceNotFoundException', item_id=item_id)
    values = parse_values(b, column_values)
    apply_values(b, it['cells'], values)
    it.setdefault('updates', 0)
    it['updates'] += 1
    return it


Q, M = SCHEMA.query_type, SCHEMA.mutation_type
Q.fields['boards'].resolve = r_boards
Q.fields['next_items_page'].resolve = r_next_items_page
Q.fields['items_page_by_column_values'].resolve = r_items_by_values
Q.fields['complexity'].resolve = r_complexity
M.fields['create_item'].resolve = r_create_item
M.fields['change_multiple_column_values'].resolve = r_change_values
SCHEMA.type_map['Board'].fields['items_page'].resolve = r_items_page
SCHEMA.type_map['Board'].fields['columns'].resolve = r_columns
SCHEMA.type_map['Item'].fields['column_values'].resolve = r_column_values
SCHEMA.type_map['ColumnValue'].resolve_type = lambda obj, info, t: VALUE_TYPES.get(obj['type'], 'TextValue')


def blocks_problem(blocks):
    """Slack Block Kit limits that make chat.postMessage and incoming webhooks answer invalid_blocks."""
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 50:
        return 'blocks must be a list of 1 to 50 blocks'
    for b in blocks:
        t = b.get('type')
        if t == 'header':
            txt = b.get('text') or {}
            if txt.get('type') != 'plain_text' or not 1 <= len(txt.get('text', '')) <= 150:
                return 'header needs plain_text of 1 to 150 characters'
        elif t == 'section':
            txt = b.get('text') or {}
            if txt.get('type') not in ('mrkdwn', 'plain_text') or not 1 <= len(txt.get('text', '')) <= 3000:
                return 'section text must be 1 to 3000 characters'
        elif t == 'context':
            els = b.get('elements') or []
            if not 1 <= len(els) <= 10 or any(e.get('type') not in ('mrkdwn', 'plain_text', 'image') or not e.get('text') for e in els):
                return 'context needs 1 to 10 text elements'
        elif t != 'divider':
            return f'unknown block type {t}'
    return None


EMAIL_RE = re.compile(r'^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]{2,}$')


class QuietServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server bind the same port without an error, and requests then land on
    # either one. Bind the port exclusively, so a port that is already taken stops the test instead.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        pass                                       # a dropped connection is on purpose in one scenario


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj, headers=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html' if isinstance(obj, str) else 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.rstrip('/')
        rec = {'service': path.split('/')[1] if path.count('/') else '', 'path': path, 'headers': headers, 'raw': raw.decode('utf-8', 'replace'),
               'at': time.time(), 'rejected': None, 'pages': []}
        LOG.append(rec)
        if path == '/monday/v2':
            return self.monday(rec, headers, raw)
        if path.startswith('/slack/'):
            return self.slack(rec, headers, raw)
        if path == '/resend/emails':
            return self.resend(rec, headers, raw)
        return self._send(404, {'message': 'not mocked'})

    # monday.com GraphQL API
    def monday(self, rec, headers, raw):
        def reject(why, code, body):
            rec['rejected'] = why
            return self._send(code, body)

        if headers.get('authorization') != STATE['token']:
            return reject('auth', 401, {'errors': [{'message': 'Not Authenticated', 'extensions': {'code': 'UNAUTHENTICATED', 'status_code': 401}}]})
        version = headers.get('api-version')
        if version is not None and version not in STATE['versions']:
            # monday.com answers an unknown or retired version with HTTP 200 and InvalidVersionException
            return reject('version', 200, {'errors': [{'message': 'The requested API version is invalid',
                                                       'extensions': {'code': 'InvalidVersionException', 'status_code': 200}}],
                                           'account_id': 12345678})
        if not headers.get('content-type', '').startswith('application/json'):
            return reject('content type', 400, {'errors': [{'message': 'Content-Type must be application/json'}]})
        try:
            body = json.loads(raw)
        except ValueError:
            return reject('json', 400, {'errors': [{'message': 'Invalid JSON body'}]})
        query, variables = body.get('query'), body.get('variables')
        if not isinstance(query, str) or not query.strip():
            return reject('no query', 400, {'errors': [{'message': 'No query string was present'}]})
        if variables is None:
            variables = {}
        if not isinstance(variables, dict):
            return reject('variables', 400, {'errors': [{'message': 'Variables must be a JSON object'}]})
        try:
            doc = parse(query)
        except GraphQLError as e:
            return reject('parse', 200, {'errors': [{'message': 'Parse error on ' + e.message, 'locations': [l._asdict() for l in (e.locations or [])]}]})
        op = next((d for d in doc.definitions if isinstance(d, OperationDefinitionNode)), None)
        rec.update(op=op.name.value if op and op.name else '', kind=op.operation.value if op else '', variables=variables, query=query)
        problems = validate(SCHEMA, doc)
        if problems:
            rec['problems'] = [p.message for p in problems]
            return reject('validation', 200, {'errors': [p.formatted for p in problems], 'account_id': 12345678})
        # Idempotency-Key works on mutations only (queries ignore it). For 30 minutes a retry with the same key gets the
        # first answer back with Idempotency-Replayed: true, and a retry while the first is still running gets 409.
        key = headers.get('idempotency-key') if rec['kind'] == 'mutation' else None
        slot = (STATE['token'], key)
        if key is not None:
            rec['idempotency_key'] = key
            with LOCK:
                cached = MONDAY_IDEMPOTENCY.get(slot)
            if cached == 'running':
                rec['status'] = 409
                return self._send(409, {'errors': [{'message': 'A request with this idempotency key is currently being processed',
                                                    'extensions': {'code': 'IDEMPOTENCY_CONFLICT'}}]}, {'Retry-After': '1'})
            if cached:
                rec.update(replayed=True, status=cached['status'], answer=cached['answer'])
                return self._send(cached['status'], cached['answer'], {'Idempotency-Replayed': 'true'})
        for fault in FAULT['monday_http']:
            if fault['times'] != 0 and fault['when'](rec):
                fault['times'] -= 1
                rec['faulted'] = fault['status']
                if fault['status'] == 'drop':          # the connection dies before any answer
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return None
                return self._send(fault['status'], fault['body'], fault.get('headers'))
        cost = 100 + 12 * int(variables.get('limit') or 1) * (len(variables.get('columnIds') or []) + 1)
        if key is not None:
            MONDAY_IDEMPOTENCY[slot] = 'running'
        try:
            with LOCK:
                result = execute_sync(SCHEMA, doc, variable_values=variables, context_value={'rec': rec, 'cost': cost})
        finally:
            if MONDAY_IDEMPOTENCY.get(slot) == 'running':
                del MONDAY_IDEMPOTENCY[slot]
        errors = result.errors or []
        # errors without a monday.com code come from graphql-core itself: variables of the wrong type or missing
        if errors and result.data is None and not all((e.extensions or {}).get('code') for e in errors):
            rec['rejected'] = 'variables'
        answer = {'data': result.data, 'account_id': 12345678, 'extensions': {'request_id': str(uuid.uuid4())}}
        if errors:
            answer['errors'] = [e.formatted for e in errors]
        rec['answer'] = answer
        # monday.com answers most errors with HTTP 200, but some with their own status (UserUnauthorizedException is 403)
        status = max([200] + [int((e.extensions or {}).get('status_code') or 200) for e in errors])
        rec['status'] = status
        if key is not None and status < 500:
            MONDAY_IDEMPOTENCY[slot] = {'status': status, 'answer': answer}
        if FAULT['monday_lose'] and FAULT['monday_lose'](rec):
            FAULT['monday_lose'] = None                # the mutation ran, but the answer never reaches n8n
            rec['faulted'] = 'answer lost'
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return None
        return self._send(status, answer)

    # Slack incoming webhook
    def slack(self, rec, headers, raw):
        if rec['path'] not in ('/slack/report', '/slack/alerts'):
            return self._send(404, 'no_service')
        if rec['path'] == '/slack/report' and FAULT['slack_status']:
            rec['faulted'] = FAULT['slack_status']
            return self._send(FAULT['slack_status'], 'rollup_error')
        try:
            body = json.loads(raw)
            assert headers.get('content-type', '').startswith('application/json')
        except (ValueError, AssertionError):
            rec['rejected'] = 'payload'
            return self._send(400, 'invalid_payload')
        if not isinstance(body.get('text'), str) or not body['text'].strip():
            rec['rejected'] = 'no text'
            return self._send(400, 'no_text')
        if 'blocks' in body and blocks_problem(body['blocks']):
            rec['rejected'] = blocks_problem(body['blocks'])
            return self._send(400, 'invalid_blocks')
        rec['body'] = body
        return self._send(200, 'ok')

    # Resend send email API
    def resend(self, rec, headers, raw):
        def fail(code, name, message):
            rec['rejected'] = name
            return self._send(code, {'statusCode': code, 'message': message, 'name': name})

        if not headers.get('user-agent'):                # Resend refuses requests without a User-Agent (error 1010)
            rec['rejected'] = 'no user agent'
            return self._send(403, 'error code: 1010')
        if not headers.get('authorization'):
            return fail(401, 'missing_api_key', 'Missing API key in the authorization header.')
        if headers['authorization'] != 'Bearer re_mock_key':
            return fail(403, 'validation_error', 'API key is invalid')
        try:
            body = json.loads(raw)
            assert headers.get('content-type', '').startswith('application/json') and isinstance(body, dict)
        except (ValueError, AssertionError):
            return fail(400, 'validation_error', 'Invalid JSON body')
        for field in ('from', 'to', 'subject'):
            if not body.get(field):
                return fail(422, 'missing_required_field', f'Missing `{field}` field.')
        to = body['to'] if isinstance(body['to'], list) else [body['to']]
        if not 1 <= len(to) <= 50 or not all(isinstance(t, str) and EMAIL_RE.match(t) for t in to):
            return fail(422, 'validation_error', 'Invalid `to` field. The email address needs to follow the `email@example.com` format.')
        sender = re.fullmatch(r'(?:[^<>]+ <([^<>]+)>|([^<>\s]+))', body['from'])
        if not sender or not EMAIL_RE.match(sender.group(1) or sender.group(2)):
            return fail(422, 'validation_error', 'Invalid `from` field. Use `email@example.com` or `Name <email@example.com>`.')
        if not (body.get('html') or body.get('text')):
            return fail(422, 'validation_error', 'Missing `html` or `text` field.')
        key = headers.get('idempotency-key')
        if key is not None and not 1 <= len(key) <= 256:
            return fail(400, 'invalid_idempotency_key', 'The key must be between 1-256 chars.')
        rec['body'] = body
        if FAULT['resend_refuse']:                       # for example a sending domain that is not verified
            return fail(403, 'validation_error', FAULT['resend_refuse'])
        with LOCK:
            if key in IDEMPOTENCY:
                if IDEMPOTENCY[key]['body'] != body:
                    return fail(409, 'invalid_idempotent_request', 'Same idempotency key used with a different request payload.')
                rec['replayed'] = True
                return self._send(200, {'id': IDEMPOTENCY[key]['id']})
            email_id = str(uuid.uuid4())
            EMAILS.append({'id': email_id, 'key': key, 'body': body})
            if key:
                IDEMPOTENCY[key] = {'id': email_id, 'body': body}
        if FAULT['resend_lose']:
            FAULT['resend_lose'] = False          # the email went out, but the answer is lost on the way back
            rec['faulted'] = 500
            return self._send(500, {'statusCode': 500, 'message': 'Internal server error', 'name': 'application_error'})
        return self._send(200, {'id': email_id})


# ------------------------------------------------------------------ n8n helpers
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(path_name, env=None):
    """A workflow with every $env value replaced by a test value and the API hosts pointed at the mocks."""
    values = {**ENV, **(env or {})}
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(values.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = path_name
    return wf


def import_workflow(wf, error_workflow_id=None):
    settings = dict(wf.get('settings') or {})
    if error_workflow_id:
        settings['errorWorkflow'] = error_workflow_id
    return api('POST', '/api/v1/workflows', {'name': NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': settings})['id']


class Run:
    """One finished execution: where it ended, and what each node produced."""

    def __init__(self, ex, http=None):
        full = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true")
        end = time.time() + 10
        while full.get('status') not in ('success', 'error', 'crashed', 'canceled') and time.time() < end:
            time.sleep(0.5)                 # listed as stopped, but the final status is not saved yet
            full = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true")
        self.id, self.status, self.mode, self.http = ex['id'], full.get('status'), full.get('mode'), http
        result = (full.get('data') or {}).get('resultData') or {}
        self.last = result.get('lastNodeExecuted')
        self.error = (result.get('error') or {}).get('message', '')
        self.run_data = result.get('runData') or {}

    def runs(self, node):
        return len(self.run_data.get(node, []))

    def out(self, node, run=0, output=0):
        data = self.run_data[node][run]['data']['main']
        return [i['json'] for i in (data[output] if len(data) > output else []) or []]


class Runner:
    def __init__(self, wid, path=PATH, results=None):
        self.wid, self.path, self.sent, self.mark = wid, path, 0, 0
        self.results = [] if results is None else results

    def wait(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', []) if e.get('stoppedAt')]
            if len(done) >= self.sent:
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def run_now(self, secret=SECRET):
        self.mark = len(LOG)
        headers = {'Content-Type': 'application/json'}
        if secret is not None:
            headers['X-Run-Secret'] = secret
        req = urllib.request.Request(f'{N8N}/webhook/{self.path}', method='POST', data=b'{}', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                http = (r.status, json.loads(r.read().decode() or '{}'))
        except urllib.error.HTTPError as e:
            http = (e.code, json.loads(e.read().decode() or '{}'))
        self.sent += 1
        return Run(self.wait(), http)

    def new(self, service=None, path=None):
        return [r for r in LOG[self.mark:] if (service is None or r['service'] == service) and (path is None or r['path'] == path)]

    def monday(self, op=None):
        return [r for r in self.new('monday') if op is None or r.get('op') == op]

    def alert(self, run, timeout=40):
        """The error workflow's Slack alert for this run. Alerts arrive after the failed run ends, sometimes after the
        next scenario has started, so each one is matched to its run through the execution link inside it."""
        link = f"/executions/{run.id}" if run else None
        end = time.time() + timeout
        while link and time.time() < end:
            hits = [x for x in LOG if x['path'] == '/slack/alerts' and x.get('body')
                    and re.search(re.escape(link) + r'(?!\d)', x['body'].get('text', ''))]
            if hits:
                return hits[0]['body']['text']
            time.sleep(0.3)
        return ''

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))


def section(blocks, needle):
    return next((b['text']['text'] for b in blocks if b.get('type') == 'section' and needle in b['text']['text']), '')


def board_result(run, bid):
    return next(b for b in run.out('Analyze Boards')[0]['boards'] if b['id'] == bid)


def log_rows():
    return [{'id': i['id'], 'name': i['name'], 'group': i.get('group'), **i['cells']} for i in BOARDS[LOG_BOARD]['items']]


def page_ops(r):
    """The page reads of one run: (operation, board, offset, items returned, cursor sent, cursor returned)."""
    out = []
    for q in r.monday():
        if q.get('op') in ('FirstPage', 'NextPage'):
            p = q['pages'][0] if q['pages'] else {}
            out.append((q['op'], p.get('board'), p.get('offset'), p.get('count'), q['variables'].get('cursor'), p.get('cursor'), q.get('faulted')))
    return out


def waits(run):
    """What Check Page decided for every answer that made the workflow pause."""
    out = [run.out('Check Page', i)[0] for i in range(run.runs('Check Page'))]
    return [x for x in out if x.get('route') == 'wait']


def report_blocks(r):
    slack = r.new('slack', '/slack/report')
    return slack[0]['body']['blocks'] if slack and slack[0].get('body') else []


def on_page(board_id, offset):
    return lambda rec: rec.get('op') == 'NextPage' and CURSORS.get(rec['variables'].get('cursor'), (None, None)) == (board_id, offset)


def first_page(board_id):
    return lambda rec: rec.get('op') == 'FirstPage' and rec['variables'].get('boardIds') == [board_id]


# ------------------------------------------------------------------ scenarios
def mock_self_test(r):
    def post(path, body, headers):
        req = urllib.request.Request(f'http://127.0.0.1:{MOCK_PORT}{path}', method='POST', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', **headers})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    auth = {'Authorization': TOKEN, 'API-Version': '2026-07'}
    typo = post('/monday/v2', {'query': 'query { boards(ids: [1]) { id nme } }'}, auth)
    wrong_type = post('/monday/v2', {'query': 'query Q($l: String!) { next_items_page(cursor: "x", limit: $l) { cursor } }', 'variables': {'l': '5'}}, auth)
    string_vars = post('/monday/v2', {'query': '{ complexity { query } }', 'variables': '{}'}, auth)
    no_token = post('/monday/v2', {'query': '{ complexity { query } }'}, {'API-Version': '2026-07'})
    old_version = post('/monday/v2', {'query': '{ complexity { query } }'}, {**auth, 'API-Version': '2019-01'})
    ok = (typo[0] == 200 and "Cannot query field 'nme' on type 'Board'" in typo[1] and wrong_type[0] == 200
          and 'Variable' in wrong_type[1] and string_vars[0] == 400 and no_token[0] == 401
          and old_version[0] == 200 and '"InvalidVersionException"' in old_version[1])
    r.check('Mock self-test', 'mock monday.com rejects what the real API rejects',
            ok, f'typo {typo[0]}, wrong variable type {wrong_type[0]}, string variables {string_vars[0]}, no token {no_token[0]}, '
                f'unknown API version {old_version[0]} InvalidVersionException')
    # monday.com idempotency: the same key on a mutation is answered from the first run, never run twice
    BOARDS['9999999999'] = board('9999999999', 'Self test', [('text', 'Text', 'text')], [])
    try:
        create = {'query': 'mutation M($b: ID!) { create_item(board_id: $b, item_name: "x") { id } }', 'variables': {'b': '9999999999'}}
        first = post('/monday/v2', create, {**auth, 'Idempotency-Key': 'self-test-key'})
        again = post('/monday/v2', create, {**auth, 'Idempotency-Key': 'self-test-key'})
        other = post('/monday/v2', create, {**auth, 'Idempotency-Key': 'self-test-key-2'})
        made = len(BOARDS['9999999999']['items'])
    finally:
        del BOARDS['9999999999']
    r.check('Mock self-test', 'mock monday.com replays a mutation sent again with the same Idempotency-Key',
            first[0] == again[0] == other[0] == 200 and json.loads(first[1])['data'] == json.loads(again[1])['data']
            and json.loads(other[1])['data'] != json.loads(first[1])['data'] and made == 2 and LOG[-2].get('replayed') is True,
            f'3 requests, 2 keys, {made} items created')
    long_section = post('/slack/report', {'text': 'x', 'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'x' * 3001}}]}, {})
    no_subject = post('/resend/emails', {'from': 'a@example.com', 'to': ['b@example.com'], 'html': '<p>x</p>'}, {'Authorization': 'Bearer re_mock_key'})
    # urllib always adds a User-Agent, http.client does not
    conn = http.client.HTTPConnection('127.0.0.1', MOCK_PORT, timeout=10)
    conn.request('POST', '/resend/emails', json.dumps({'from': 'a@example.com', 'to': ['b@example.com'], 'subject': 's', 'html': '<p>x</p>'}),
                 {'Content-Type': 'application/json', 'Authorization': 'Bearer re_mock_key'})
    no_agent = conn.getresponse().status
    conn.close()
    r.check('Mock self-test', 'mock Slack and Resend reject malformed payloads',
            long_section == (400, 'invalid_blocks') and no_subject[0] == 422 and no_agent == 403 and not EMAILS,
            f'Slack {long_section[0]} {long_section[1]}, Resend without subject {no_subject[0]}, without User-Agent {no_agent}')
    del LOG[:]


def run_now_security(r):
    for label, secret in (('wrong secret', 'not-the-secret'), ('no secret header', None)):
        run = r.run_now(secret)
        alert = r.alert(run)
        r.check(f'Run Now, {label}', 'refused with 401, settings not even read, nothing sent, alert raised',
                run.http == (401, {'status': 'unauthorized', 'message': 'Missing or wrong X-Run-Secret header.'})
                and not r.new('monday') and not r.new('slack', '/slack/report') and not r.new('resend') and run.runs('Load Config') == 0
                and run.last == 'Stop: Bad Run Now Secret' and 'Stop: Bad Run Now Secret' in alert,
                f'HTTP {run.http[0]}, ended at {run.last}')


def full_report(r):
    run = r.run_now()
    r.check('Full report', 'Run Now answers 202 once the settings check passes, and the run succeeds',
            run.http == (202, {'status': 'started', 'message': 'The CRM health report is running. It arrives in Slack or email when every board is read.'})
            and run.runs('Respond: Started') == 1 and run.runs('Respond: Bad Config') == 0
            and run.status == 'success' and run.last == 'Any Problems?', f'HTTP {run.http[0]}, {run.status}, ended at {run.last}')
    snapshot = run.out('Load Config')[0]['snapshotDate']
    r.check('Full report', 'snapshot date is today in REPORT_TIMEZONE', snapshot == DATE, snapshot)

    ops = page_ops(r)
    cursors_chain = len(ops) == 5 and ops[1][4] == ops[0][5] and ops[2][4] == ops[1][5] and ops[2][5] is None
    r.check('Full report', 'Sales Pipeline read in 3 pages, each cursor from the page before',
            [o[:4] for o in ops] == [('FirstPage', A, 0, 50), ('NextPage', A, 50, 50), ('NextPage', A, 100, 30), ('FirstPage', B, 0, 12), ('FirstPage', C, 0, 0)]
            and cursors_chain, ' then '.join(f'{o[0]}({o[3]} items)' for o in ops))
    calls = r.monday()
    headers_ok = all(c['headers'].get('authorization') == TOKEN and c['headers'].get('api-version') == '2026-07'
                     and c['headers'].get('content-type', '').startswith('application/json') for c in calls)
    r.check('Full report', 'every monday.com request: token, API-Version 2026-07, valid against the real schema',
            len(calls) == 7 and headers_ok and not [c for c in calls if c['rejected'] or c.get('answer', {}).get('errors')],
            f'{len(calls)} requests: ' + ', '.join(c.get('op', '?') for c in calls))
    first = calls[0]['variables']
    r.check('Full report', 'page size and column IDs sent as GraphQL variables',
            first == {'boardIds': [A], 'columnIds': ['email_mkq1a2b3', 'text_mkq1c4d5', 'text_mkq1e6f7', 'status', 'person'], 'limit': 50},
            json.dumps(first))

    sales = board_result(run, A)
    expected = {'items': 130, 'issueItems': 19, 'duplicates': 5, 'duplicateGroups': 3, 'missingRequired': 5,
                'missingByField': {'Company': 3, 'Email': 2, 'Phone': 2}, 'stale': 5, 'noOwner': 4, 'invalid': 3, 'invalidEmail': 2, 'invalidPhone': 2}
    r.check('Full report', 'Sales Pipeline: exact counts', sales['counts'] == expected, json.dumps(sales['counts']))
    r.check('Full report', 'Sales Pipeline: score 85 (111 clean of 130), grade Fair', sales['score'] == 85 and sales['grade'] == 'Fair',
            f"score {sales['score']}, {sales['grade']}")
    worst = [(o['id'][-3:], o['issues']) for o in sales['offenders']]
    r.check('Full report', 'Sales Pipeline: worst 5 items in the right order with the right issues', worst == [
        ('110', ['duplicate of Jordan Lee', 'missing Company', 'no update in 60 days', 'no owner']),
        ('042', ['missing Phone, Company']), ('043', ['missing Email, Phone']), ('072', ['invalid email', 'invalid phone']), ('054', ['no owner'])],
        ', '.join('#' + w[0] for w in worst) + (f" (#{worst[0][0]}: {', '.join(worst[0][1])})" if worst else ''))
    partners, old = board_result(run, B), board_result(run, C)
    r.check('Full report', 'Partner Leads: 12 items, no issues (mirror column read from display_value), score 100, no owner check',
            partners['status'] == 'ok' and partners['counts']['items'] == 12 and partners['counts']['issueItems'] == 0
            and partners['score'] == 100 and partners['checks']['owner'] is False and partners['offenders'] == [],
            f"{partners['counts']['items']} items, score {partners['score']}")
    r.check('Full report', 'Old Imports: empty board, no score', old['status'] == 'empty' and old['score'] is None and old['counts']['items'] == 0, old['status'])

    slack = r.new('slack', '/slack/report')
    blocks = slack[0]['body']['blocks'] if slack and slack[0].get('body') else []
    sales_text = section(blocks, 'Sales Pipeline')
    r.check('Full report', 'one Slack message with a summary per board and the worst items (names escaped)',
            len(slack) == 1 and blocks[0]['text']['text'] == f'CRM health report, {DATE}'
            and slack[0]['body']['text'] == f'CRM health report {DATE}: Sales Pipeline 85/100, Partner Leads 100/100, Old Imports empty'
            and f':large_yellow_circle: *<https://example-crm.monday.com/boards/{A}|Sales Pipeline>*: score *85/100* (Fair)' in sales_text
            and '130 items, 19 with issues: 5 duplicates, 5 missing required fields, 5 stale, 4 without an owner, 3 with an invalid email or phone.' in sales_text
            and f'• <https://example-crm.monday.com/boards/{A}/pulses/5000000110|Morgan Diaz &amp; Co &lt;VIP&gt;>: duplicate of Jordan Lee, missing Company, no update in 60 days, no owner' in sales_text
            and '12 items, no issues found.' in section(blocks, 'Partner Leads') and 'empty board, nothing to check.' in section(blocks, 'Old Imports'),
            f'{len(slack)} message(s), {len(blocks)} blocks')
    emails = r.new('resend')
    email = emails[0]['body'] if emails and emails[0].get('body') else {}
    r.check('Full report', 'one HTML email to both recipients, names escaped, idempotency key set',
            len(emails) == 1 and email.get('to') == ['ops@example.com', 'sales.lead@example.com'] and email.get('from') == 'CRM Reports <reports@example.com>'
            and email.get('subject') == f'CRM health report {DATE}: lowest score 85/100 (Sales Pipeline)'
            and '<b>85</b> Fair' in email.get('html', '') and 'Morgan Diaz &amp; Co &lt;VIP&gt;' in email['html'] and '<VIP>' not in email['html']
            and emails[0]['headers'].get('idempotency-key') == f'crm-health-report/{r.wid}/{DATE}/{run.id}',
            email.get('subject', 'no email'))

    lookups, saves = r.monday('FindLogRows'), r.monday('SaveHealthLog')
    r.check('Full report', 'health log: one lookup by key, then 3 rows created in one request that carries an Idempotency-Key',
            len(lookups) == 1 and lookups[0]['variables']['columns'] == [{'column_id': 'text_key', 'column_values': [f'{DATE}|{A}', f'{DATE}|{B}', f'{DATE}|{C}']}]
            and len(saves) == 1 and saves[0]['query'].count('create_item(') == 3 and 'change_multiple_column_values' not in saves[0]['query']
            and saves[0]['headers'].get('idempotency-key') == f'crm-health-log/{r.wid}/{DATE}/{run.id}' and not saves[0].get('replayed'),
            f"{len(lookups)} lookup, {len(saves)} save, key {saves[0]['headers'].get('idempotency-key') if saves else None}")
    rows = {row['text_key']: row for row in log_rows()}
    row_a = rows.get(f'{DATE}|{A}', {})
    r.check('Full report', 'health log rows hold the exact numbers',
            len(rows) == 3 and row_a.get('name') == f'Sales Pipeline {DATE}' and row_a.get('group') == 'topics'
            and {k: v for k, v in row_a.items() if k not in ('id', 'name', 'group')} == {
                'text_key': f'{DATE}|{A}', 'date_snap': {'date': DATE}, 'text_board': 'Sales Pipeline', 'numbers_score': '85', 'numbers_items': '130',
                'numbers_issues': '19', 'numbers_dupes': '5', 'numbers_missing': '5', 'numbers_stale': '5', 'numbers_owner': '4', 'numbers_invalid': '3'}
            and 'numbers_score' not in rows.get(f'{DATE}|{C}', {}) and 'numbers_owner' not in rows.get(f'{DATE}|{B}', {}),
            f'{len(rows)} rows, Sales Pipeline score {row_a.get("numbers_score")}')
    return run


def rerun_same_day(r):
    before = {row['text_key']: row['id'] for row in log_rows()}
    run = r.run_now()
    saves = r.monday('SaveHealthLog')
    q = saves[0]['query'] if saves else ''
    after = {row['text_key']: row['id'] for row in log_rows()}
    r.check('Re-run on the same day', 'no new log rows: the 3 existing rows are updated',
            run.status == 'success' and q.count('change_multiple_column_values(') == 3 and 'create_item(' not in q and after == before
            and sorted(v for k, v in saves[0]['variables'].items() if k.startswith('item')) == sorted(before.values()),
            f'{len(after)} rows, {q.count("change_multiple_column_values(")} updates')
    r.check('Re-run on the same day', 'same numbers, a fresh Slack report and a new email key',
            board_result(run, A)['score'] == 85 and len(r.new('slack', '/slack/report')) == 1 and len(r.new('resend')) == 1
            and r.new('resend')[0]['headers'].get('idempotency-key') == f'crm-health-report/{r.wid}/{DATE}/{run.id}',
            'score 85, 1 Slack message, 1 email')


def board_emptied(r):
    """Partner Leads was scored earlier today, then all its items were removed. The re-run must clear the old score."""
    saved = BOARDS[B]['items']
    BOARDS[B]['items'] = []
    try:
        run = r.run_now()
    finally:
        BOARDS[B]['items'] = saved
    saves = r.monday('SaveHealthLog')
    sent = {json.loads(v)['text_key']: json.loads(v) for k, v in (saves[0]['variables'] if saves else {}).items() if k.startswith('values')}
    sent_b = sent.get(f'{DATE}|{B}', {})
    row = {x['text_key']: x for x in log_rows()}.get(f'{DATE}|{B}', {})
    r.check('Board emptied since the last run today', 'its log row is updated to 0 items and the old score is cleared with null (not kept, not 0)',
            run.status == 'success' and board_result(run, B)['status'] == 'empty' and len(saves) == 1
            and 'numbers_score' in sent_b and sent_b['numbers_score'] is None and sent_b.get('numbers_items') == '0'
            and row.get('numbers_items') == '0' and 'numbers_score' not in row and len(log_rows()) == 3
            and 'empty board, nothing to check.' in section(report_blocks(r), 'Partner Leads'),
            f"score sent {sent_b.get('numbers_score', 'not sent')!r}, row score {row.get('numbers_score', 'cleared')}, items {row.get('numbers_items')}")


def rate_limit(r):
    FAULT['monday_http'].append({'when': on_page(A, 50), 'times': 1, 'status': 429, 'headers': {'Retry-After': '30'}, 'body': {'errors': [{
        'message': 'Complexity budget exhausted', 'extensions': {'code': 'COMPLEXITY_BUDGET_EXHAUSTED', 'complexity': 6182,
                                                                 'complexity_budget_left': 100, 'complexity_budget_limit': 5000000,
                                                                 'retry_in_seconds': 30, 'status_code': 429}}]}})
    run = r.run_now()
    ops = page_ops(r)
    w = waits(run)
    waited = w[0] if w else {}
    r.check('Complexity budget exhausted (429)', 'waits the retry_in_seconds asked for (capped by MONDAY_MAX_WAIT_SECONDS), retries the same cursor, counts unchanged',
            run.status == 'success' and run.runs('Wait: Back Off') == 1 and len(w) == 1 and waited.get('askedSeconds') == 30
            and waited.get('waitSource') == 'retry_in_seconds' and waited.get('waitSeconds') == 1
            and len(ops) == 6 and ops[1][6] == 429 and ops[2][4] == ops[1][4] and ops[2][3] == 50
            and board_result(run, A)['counts']['items'] == 130 and board_result(run, A)['score'] == 85,
            f"{len(ops)} page reads, asked {waited.get('askedSeconds')}s ({waited.get('waitSource')}), waited {waited.get('waitSeconds')}s for: {waited.get('waitReason')}")


def low_budget(r):
    FAULT['low_budget'] = first_page(A)
    run = r.run_now()
    page1 = run.out('Check Page', 0)[0]
    r.check('Complexity budget low', 'keeps the page, then pauses for the budget reset before the next page',
            run.status == 'success' and page1.get('route') == 'wait' and page1.get('itemsOnPage') == 50
            and page1.get('waitReason') == 'complexity budget low (5 left, one page costs 3700)'
            and page1.get('askedSeconds') == 31 and page1.get('waitSource') == 'complexity'
            and run.runs('Wait: Back Off') == 1 and not [o for o in page_ops(r) if o[6]] and len(page_ops(r)) == 5
            and board_result(run, A)['counts']['items'] == 130, page1.get('waitReason', ''))


def server_errors_then_ok(r):
    FAULT['monday_http'] += [
        {'when': first_page(B), 'times': 1, 'status': 500, 'body': {'errors': [{'message': 'Internal server error'}]}},
        {'when': first_page(B), 'times': 1, 'status': 423, 'headers': {'Retry-After': '7'},
         'body': {'errors': [{'message': 'Resource is currently locked, please try again later', 'extensions': {'status_code': 423}}]}}]
    run = r.run_now()
    seen = [(x.get('waitReason'), x.get('askedSeconds'), x.get('waitSource')) for x in waits(run)]
    r.check('monday.com 500, then 423 locked', 'pauses each time (10s back off, then the 7s Retry-After), then the report is complete',
            run.status == 'success' and len(r.monday('FirstPage')) == 5 and run.runs('Wait: Back Off') == 2
            and seen == [('HTTP 500, Internal server error', 10, 'backoff'),
                         ('HTTP 423, Resource is currently locked, please try again later', 7, 'Retry-After')]
            and board_result(run, B)['score'] == 100,
            f'{len(r.monday("FirstPage"))} first-page requests, waits {[(s[1], s[2]) for s in seen]}, Partner Leads score {board_result(run, B)["score"]}')


def temporarily_blocked(r):
    FAULT['monday_http'].append({'when': first_page(B), 'times': 1, 'status': 200, 'body': {'data': None, 'errors': [{
        'message': 'API usage is temporarily blocked', 'extensions': {'code': 'API_TEMPORARILY_BLOCKED', 'status_code': 200}}],
        'account_id': 12345678}})
    run = r.run_now()
    w = waits(run)
    partners = board_result(run, B)
    r.check('GraphQL error with HTTP 200, temporary', 'API_TEMPORARILY_BLOCKED is not read as an empty board: pause, retry, report complete',
            run.status == 'success' and run.last == 'Any Problems?' and len(w) == 1
            and w[0].get('waitReason') == 'HTTP 200, API_TEMPORARILY_BLOCKED: API usage is temporarily blocked'
            and len(r.monday('FirstPage')) == 4 and partners['status'] == 'ok' and partners['counts']['items'] == 12 and partners['score'] == 100,
            f"{len(r.monday('FirstPage'))} first-page requests, Partner Leads {partners.get('counts', {}).get('items')} items, "
            f"score {partners.get('score')}, {partners.get('error') or 'no error'}")


def cursor_expired(r):
    before = {row['text_key']: dict(row) for row in log_rows()}
    FAULT['monday_http'].append({'when': on_page(A, 100), 'times': 1, 'status': 200, 'body': {'data': None, 'errors': [{
        'message': 'The cursor is invalid or has expired', 'extensions': {'code': 'CursorException', 'status_code': 200}}],
        'account_id': 12345678}})
    run = r.run_now()
    alert = r.alert(run)
    sales = board_result(run, A)
    ops = page_ops(r)
    saves = r.monday('SaveHealthLog')
    saved_keys = sorted(json.loads(v)['text_key'] for k, v in (saves[0]['variables'] if saves else {}).items() if k.startswith('values'))
    r.check('GraphQL error with HTTP 200, permanent (cursor expired on page 3)', 'board named as not checked; the 100 items already read are not scored; no retry',
            sales['status'] == 'failed' and sales['error'] == 'CursorException: The cursor is invalid or has expired' and sales['score'] is None
            and [o[0] for o in ops] == ['FirstPage', 'NextPage', 'NextPage', 'FirstPage', 'FirstPage'] and ops[2][6] == 200
            and run.runs('Wait: Back Off') == 0 and board_result(run, B)['score'] == 100
            and 'could not be checked. CursorException: The cursor is invalid or has expired' in section(report_blocks(r), 'Sales Pipeline'),
            sales.get('error', ''))
    r.check('GraphQL error with HTTP 200, permanent (cursor expired on page 3)', "the board's log row keeps its last good numbers, run flagged and alert sent",
            saved_keys == sorted([f'{DATE}|{B}', f'{DATE}|{C}']) and {row['text_key']: row for row in log_rows()}[f'{DATE}|{A}'] == before[f'{DATE}|{A}']
            and run.last == 'Stop: Report Problems' and 'Sales Pipeline (CursorException' in run.error and 'Stop: Report Problems' in alert,
            f'log rows saved for {len(saved_keys)} boards, ended at {run.last}')


def board_errors(r):
    FAULT['unauthorized'].add(B)
    FAULT['hidden'].add(C)
    run = r.run_now()
    alert = r.alert(run)
    partners, old = board_result(run, B), board_result(run, C)
    blocks = report_blocks(r)
    saves = r.monday('SaveHealthLog')
    refused = [q for q in r.monday('FirstPage') if q['variables'].get('boardIds') == [B]]
    r.check('Board refused (HTTP 403) or deleted', 'both boards reported as not checked, Sales Pipeline still scored',
            len(refused) == 1 and refused[0].get('status') == 403
            and partners['status'] == 'failed' and partners['error'] == 'HTTP 403: UserUnauthorizedException: User unauthorized to perform action'
            and old['status'] == 'failed' and old['error'] == 'board not found, archived, or the API token has no access to it'
            and board_result(run, A)['score'] == 85 and 'could not be checked. HTTP 403: UserUnauthorizedException' in section(blocks, 'Partner Leads')
            and '2 boards could not be checked' in section(blocks, 'boards checked'), f"{partners['error']} / {old['error']}")
    r.check('Board refused (HTTP 403) or deleted', 'log row only for the board that was checked, run flagged and alert sent',
            len(saves) == 1 and saves[0]['query'].count('change_multiple_column_values(') == 1 and 'create_item(' not in saves[0]['query']
            and run.last == 'Stop: Report Problems' and '2 board(s) could not be checked' in run.error and 'Stop: Report Problems' in alert,
            f'ended at {run.last}')


def wrong_column(r):
    removed = BOARDS[A]['columns'].pop(2)                     # text_mkq1e6f7 (Company) is gone from the board
    try:
        run = r.run_now()
    finally:
        BOARDS[A]['columns'].insert(2, removed)
    sales = board_result(run, A)
    r.check('Column ID no longer on the board', 'board flagged with the missing column ID, nothing paged, others still run',
            sales['status'] == 'failed' and sales['error'] == 'column ID(s) not on this board: text_mkq1e6f7. Fix BOARDS_JSON'
            and [o[0] for o in page_ops(r) if o[1] == A] == ['FirstPage'] and board_result(run, B)['score'] == 100
            and run.last == 'Stop: Report Problems', sales.get('error', ''))


def formula_column(r):
    col = next(c for c in BOARDS[B]['columns'] if c['id'] == 'text_mkq2e5f6')
    col['type'] = 'formula'                                   # "Partner company", a required column, is now a formula
    try:
        run = r.run_now()
    finally:
        col['type'] = 'text'
    partners = board_result(run, B)
    r.check('Formula column as a required field', 'board flagged with the column ID instead of a misleading score, others still run',
            partners['status'] == 'failed' and partners['score'] is None
            and partners['error'] == 'formula column(s) cannot be checked: text_mkq2e5f6. Remove them from BOARDS_JSON'
            and [o[0] for o in page_ops(r) if o[1] == B] == ['FirstPage'] and board_result(run, A)['score'] == 85
            and 'formula column(s) cannot be checked' in section(report_blocks(r), 'Partner Leads') and run.last == 'Stop: Report Problems',
            partners.get('error', ''))


def monday_down(r):
    FAULT['monday_http'].append({'when': lambda rec: True, 'times': -1, 'status': 500, 'body': {'errors': [{'message': 'Internal server error'}]}})
    run = r.run_now()
    alert = r.alert(run)
    r.check('monday.com down (500 every time)', '3 tries, then the run stops with an alert and no half report',
            len(r.monday()) == 3 and run.runs('Wait: Back Off') == 2 and run.last == 'Stop: Monday Failed'
            and 'still failing after 3 tries (HTTP 500' in run.error and not r.new('slack', '/slack/report') and not r.new('resend')
            and 'Stop: Monday Failed' in alert, f'{len(r.monday())} requests, ended at {run.last}')


def bad_token(r):
    STATE['token'] = 'a-different-token'
    run = r.run_now()
    r.check('API token rejected (401)', 'stops at once without retrying, no report',
            len(r.monday()) == 1 and run.runs('Wait: Back Off') == 0 and run.last == 'Stop: Monday Failed' and 'rejected the API token (HTTP 401)' in run.error
            and not r.new('slack', '/slack/report') and not r.new('resend'), f'{len(r.monday())} request, ended at {run.last}')


def daily_limit(r):
    FAULT['monday_http'].append({'when': lambda rec: True, 'times': -1, 'status': 429, 'body': {'errors': [{
        'message': 'Daily limit exceeded', 'extensions': {'code': 'DAILY_LIMIT_EXCEEDED', 'status_code': 429}}]}})
    run = r.run_now()
    r.check('Daily call limit reached', 'stops at once (waiting would not help), no report',
            len(r.monday()) == 1 and run.runs('Wait: Back Off') == 0 and run.last == 'Stop: Monday Failed'
            and 'daily API call limit reached' in run.error and not r.new('slack', '/slack/report') and not r.new('resend'),
            f'{len(r.monday())} request, ended at {run.last}')


def account_wide_errors(r):
    """Errors that hit every board: stop at once with a clear reason instead of a report where no board was checked."""
    STATE['versions'].discard('2026-07')                    # monday.com no longer serves the configured version
    run = r.run_now()
    r.check('API version no longer served (InvalidVersionException, HTTP 200)', 'stops at once with the version to fix, no report',
            len(r.monday()) == 1 and r.monday()[0]['rejected'] == 'version' and run.runs('Wait: Back Off') == 0
            and run.last == 'Stop: Monday Failed' and 'does not accept API version 2026-07' in run.error and 'MONDAY_API_VERSION' in run.error
            and not r.new('slack', '/slack/report') and not r.new('resend'), f'{len(r.monday())} request, ended at {run.last}')
    STATE['versions'] = set(VERSIONS)
    FAULT['monday_http'].append({'when': lambda rec: True, 'times': -1, 'status': 403, 'body': {'errors': [{
        'message': 'The user is unauthorized to use the API', 'extensions': {'code': 'USER_ACCESS_DENIED', 'status_code': 403}}],
        'account_id': 12345678}})
    run = r.run_now()
    alert = r.alert(run)
    r.check('API token user may not use the API (USER_ACCESS_DENIED, HTTP 403)', 'stops at once instead of reporting every board as not checked',
            len(r.monday()) == 1 and run.last == 'Stop: Monday Failed' and "does not let the API token's user use the API (HTTP 403" in run.error
            and not r.new('slack', '/slack/report') and not r.new('resend') and 'Stop: Monday Failed' in alert,
            f'{len(r.monday())} request, ended at {run.last}')


def query_too_complex(r):
    FAULT['monday_http'].append({'when': first_page(A), 'times': -1, 'status': 200, 'body': {'data': None, 'errors': [{
        'message': 'Query has complexity of 5600000, which exceeds max complexity of 5000000',
        'extensions': {'code': 'REQUEST_MAX_COMPLEXITY_EXCEEDED', 'status_code': 200}}], 'account_id': 12345678}})
    run = r.run_now()
    sales = board_result(run, A)
    r.check('One page over the 5M complexity limit (REQUEST_MAX_COMPLEXITY_EXCEEDED)', 'not retried, the board is named with the fix, the others still run',
            len(r.monday('FirstPage')) == 3 and run.runs('Wait: Back Off') == 0 and sales['status'] == 'failed'
            and sales['error'] == 'REQUEST_MAX_COMPLEXITY_EXCEEDED: Query has complexity of 5600000, which exceeds max complexity of 5000000. '
                                  'One page is too expensive, lower MONDAY_PAGE_SIZE (now 50)'
            and board_result(run, B)['score'] == 100 and run.last == 'Stop: Report Problems', sales.get('error', ''))


def monday_unreachable(r):
    FAULT['monday_http'].append({'when': lambda rec: True, 'times': -1, 'status': 'drop'})
    run = r.run_now()
    r.check('monday.com unreachable (connection dropped)', 'the HTTP node retries 3 times, then the run stops with an alert',
            len(r.monday()) == 3 and run.last == 'Stop: Monday Failed' and 'Could not reach monday.com after 3 tries' in run.error
            and not r.new('slack', '/slack/report') and not r.new('resend') and 'Stop: Monday Failed' in r.alert(run),
            f'{len(r.monday())} requests, ended at {run.last}')


def log_lookup_fails(r):
    FAULT['monday_http'].append({'when': lambda rec: rec.get('op') == 'FindLogRows', 'times': -1, 'status': 500,
                                 'body': {'errors': [{'message': 'Internal server error'}]}})
    run = r.run_now()
    overview = section((r.new('slack', '/slack/report') or [{}])[0].get('body', {}).get('blocks', []), 'boards checked')
    r.check('Health log lookup fails (500)', 'retried 3 times, nothing written, the report still goes out and says so',
            len(r.monday('FindLogRows')) == 3 and not r.monday('SaveHealthLog')
            and 'Health log NOT saved: monday.com did not accept the request (HTTP 500: Internal server error).' in overview
            and len(r.new('resend')) == 1 and run.last == 'Stop: Report Problems', overview.split('\n')[-1][:110])


def log_key_column_gone(r):
    col = next(c for c in BOARDS[LOG_BOARD]['columns'] if c['id'] == 'text_key')
    col['id'] = 'text_key_renamed'                                # the Key column was deleted and made again
    try:
        run = r.run_now()
    finally:
        col['id'] = 'text_key'
    lookups = r.monday('FindLogRows')
    overview = section(report_blocks(r), 'boards checked')
    r.check('Health log key column missing (GraphQL error with HTTP 200)', 'lookup not retried, nothing written, the report still goes out and names the column',
            len(lookups) == 1 and lookups[0].get('status') == 200 and not r.monday('SaveHealthLog')
            and 'Health log NOT saved: could not look up existing rows (InvalidColumnIdException: Column text_key not found).' in overview
            and len(r.new('resend')) == 1 and run.last == 'Stop: Report Problems' and 'InvalidColumnIdException' in run.error,
            overview.split('\n')[-1][:120])


def log_save_fails(r):
    FAULT['monday_http'].append({'when': lambda rec: rec.get('op') == 'SaveHealthLog', 'times': -1, 'status': 500,
                                 'body': {'errors': [{'message': 'Internal server error'}]}})
    before = log_rows()
    run = r.run_now()
    overview = section((r.new('slack', '/slack/report') or [{}])[0].get('body', {}).get('blocks', []), 'boards checked')
    saves = r.monday('SaveHealthLog')
    keys = {s['headers'].get('idempotency-key') for s in saves}
    r.check('Health log save fails (500)', 'tried 3 times with one Idempotency-Key, rows untouched, report and email still go out and say so',
            len(saves) == 3 and keys == {f'crm-health-log/{r.wid}/{DATE}/{run.id}'} and len({s['raw'] for s in saves}) == 1 and log_rows() == before
            and 'Health log NOT saved: monday.com did not accept the request (HTTP 500: Internal server error).' in overview
            and len(r.new('resend')) == 1 and 'Health log NOT saved' in r.new('resend')[0]['body']['html']
            and run.last == 'Stop: Report Problems' and 'Health log not fully saved' in run.error,
            f"{len(saves)} save requests, {len(keys)} key, ended at {run.last}")


def log_save_answer_lost(r):
    """First run of a new day (no rows yet): monday.com creates the 3 rows, but the answer is lost on the way back.
    n8n retries the save with the same Idempotency-Key, monday.com answers from the first run, and no row is created twice."""
    BOARDS[LOG_BOARD]['items'] = [i for i in BOARDS[LOG_BOARD]['items'] if not str(i['cells'].get('text_key', '')).startswith(DATE)]
    FAULT['monday_lose'] = lambda rec: rec.get('op') == 'SaveHealthLog'
    run = r.run_now()
    saves = r.monday('SaveHealthLog')
    rows = log_rows()
    footer = (report_blocks(r) or [{}])[-1].get('elements', [{}])[0].get('text', '')
    r.check('Health log save answer lost', 'the retry carries the same key and body, monday.com replays the first answer, 3 rows and no duplicates',
            len(saves) == 2 and saves[0].get('faulted') == 'answer lost' and saves[1].get('replayed') is True
            and saves[0]['headers'].get('idempotency-key') == saves[1]['headers'].get('idempotency-key') == f'crm-health-log/{r.wid}/{DATE}/{run.id}'
            and saves[0]['raw'] == saves[1]['raw'] and saves[0]['query'].count('create_item(') == 3
            and sorted(x['text_key'] for x in rows) == sorted([f'{DATE}|{A}', f'{DATE}|{B}', f'{DATE}|{C}'])
            and run.status == 'success' and 'Health log: 3 new, 0 updated.' in footer,
            f'{len(saves)} save requests, replayed {bool(len(saves) == 2 and saves[1].get("replayed"))}, {len(rows)} rows, run {run.status}')


def log_write_fails_then_recovers(r):
    BOARDS[LOG_BOARD]['items'] = [i for i in BOARDS[LOG_BOARD]['items'] if i['cells'].get('text_key') != f'{DATE}|{B}']
    FAULT['log_fail'] = 'Partner Leads'
    run = r.run_now()
    alert = r.alert(run)
    overview = section((r.new('slack', '/slack/report') or [{}])[0].get('body', {}).get('blocks', []), 'boards checked')
    saves = r.monday('SaveHealthLog')
    shape_ok = len(saves) == 1 and saves[0]['query'].count('change_multiple_column_values(') == 2 and saves[0]['query'].count('create_item(') == 1
    r.check('Health log save fails for one row', 'the other rows are saved, Slack says so, run flagged',
            shape_ok and 'Health log NOT fully saved: Partner Leads: ColumnValueException' in overview and run.last == 'Stop: Report Problems'
            and 'Health log not fully saved' in alert and len(log_rows()) == 2,
            f"save request {'as expected' if shape_ok else 'WRONG'}, {len(log_rows())} rows, ended at {run.last}, "
            f"alert {'received' if alert else 'MISSING'}; Slack: {overview.split(chr(10))[-1][:100]}")
    FAULT['log_fail'] = None
    r.run_now()
    keys = [row['text_key'] for row in log_rows()]
    q = r.monday('SaveHealthLog')[0]['query']
    r.check('Health log save fails for one row', 'the next run creates only the missing row, no duplicates',
            q.count('create_item(') == 1 and q.count('change_multiple_column_values(') == 2 and sorted(keys) == sorted({f'{DATE}|{A}', f'{DATE}|{B}', f'{DATE}|{C}'}),
            f'{len(keys)} rows, keys unique: {len(set(keys)) == len(keys)}')


def slack_down(r):
    FAULT['slack_status'] = 500
    run = r.run_now()
    r.check('Slack down', 'retried 3 times, the email still goes out, run flagged',
            len(r.new('slack', '/slack/report')) == 3 and len(r.new('resend')) == 1 and run.last == 'Stop: Report Problems'
            and 'Slack did not accept the report (HTTP 500: rollup_error)' in run.error,
            f'{len(r.new("slack", "/slack/report"))} Slack tries, ended at {run.last}')


def email_answer_lost(r):
    FAULT['resend_lose'] = True
    before = len(EMAILS)
    run = r.run_now()
    tries = r.new('resend')
    same_key = len(tries) == 2 and tries[0]['headers'].get('idempotency-key') == tries[1]['headers'].get('idempotency-key')
    replayed = len(tries) == 2 and bool(tries[1].get('replayed'))
    r.check('Email answer lost after sending', 'the retry reuses the idempotency key, so only one email goes out',
            len(tries) == 2 and same_key and replayed and len(EMAILS) - before == 1 and run.status == 'success',
            f'{len(tries)} requests, same key {same_key}, replayed {replayed}, {len(EMAILS) - before} email delivered, run {run.status}')


def email_refused(r):
    FAULT['resend_refuse'] = 'The example.com domain is not verified. Please, add and verify your domain on https://resend.com/domains'
    before = len(EMAILS)
    run = r.run_now()
    alert = r.alert(run)
    tries = r.new('resend')
    keys = {x['headers'].get('idempotency-key') for x in tries}
    r.check('Email refused (sending domain not verified)', 'tried 3 times with one idempotency key, nothing sent, the Slack report still goes out, run flagged and alert sent',
            len(tries) == 3 and keys == {f'crm-health-report/{r.wid}/{DATE}/{run.id}'} and len(EMAILS) == before and len(r.new('slack', '/slack/report')) == 1
            and run.last == 'Stop: Report Problems' and 'Email did not accept the report (HTTP 403: The example.com domain is not verified.' in run.error
            and 'Email did not accept the report' in alert,
            f'{len(tries)} tries, {len(keys)} key, {len(EMAILS) - before} emails sent, ended at {run.last}')


def email_only_page_limit(r, error_wid):
    """Email only (no Slack, no log board), and MONDAY_MAX_PAGES=2, so the 3 page Sales Pipeline hits the limit."""
    path = PATH + '-email-only'
    wid = import_workflow(test_copy(path, {'SLACK_WEBHOOK_URL': '', 'HEALTH_LOG_BOARD_ID': '', 'HEALTH_LOG_COLUMNS': '',
                                           'MONDAY_MAX_PAGES': '2'}), error_wid)
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        m = Runner(wid, path, r.results)
        run = m.run_now()
        alert = m.alert(run)
        sales = board_result(run, A)
        emails = m.new('resend')
        email = emails[0]['body'] if emails and emails[0].get('body') else {}
        r.check('Email only, page limit reached', 'Sales Pipeline stops at MONDAY_MAX_PAGES and is named as not checked, never half scored',
                [o[:4] for o in page_ops(m)] == [('FirstPage', A, 0, 50), ('NextPage', A, 50, 50), ('FirstPage', B, 0, 12), ('FirstPage', C, 0, 0)]
                and sales['status'] == 'failed' and sales['score'] is None
                and sales['error'] == 'stopped after 2 pages (MONDAY_MAX_PAGES); the board has more items' and board_result(run, B)['score'] == 100,
                sales.get('error', ''))
        # The test's error workflow posts to the mock. A real email only setup has no Slack URL for alerts (see the README)
        r.check('Email only, page limit reached', 'no Slack or log calls, one email that names the board, run flagged so the error workflow fires',
                not m.new('slack', '/slack/report') and run.runs('Slack: Post Report') == 0 and not m.monday('FindLogRows') and len(emails) == 1
                and email.get('subject') == f'CRM health report {DATE}: lowest score 100/100 (Partner Leads), 1 board not checked'
                # the key holds this copy's own workflow ID, so two n8n workflows never share a key
                and emails[0]['headers'].get('idempotency-key') == f'crm-health-report/{wid}/{DATE}/{run.id}' and wid != r.wid
                and 'Could not be checked: stopped after 2 pages (MONDAY_MAX_PAGES); the board has more items' in email.get('html', '')
                and run.last == 'Stop: Report Problems' and 'Stop: Report Problems' in alert,
                email.get('subject', 'no email'))
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')


def run_now_bad_config(r, error_wid):
    """Run Now on a setup with a broken BOARDS_JSON and no monday.com token: the caller gets the list of what to fix."""
    path = PATH + '-bad-config'
    wid = import_workflow(test_copy(path, {'MONDAY_API_TOKEN': '', 'BOARDS_JSON': '[{"id": "123", "required_columns": "email"'}), error_wid)
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        m = Runner(wid, path, r.results)
        run = m.run_now()
        alert = m.alert(run)
        body = run.http[1] if run.http else {}
        problems = body.get('problems') or []
        r.check('Run Now, broken settings', 'answers 500 with every setting to fix, reads nothing, run stops with an alert',
                run.http[0] == 500 and body.get('status') == 'not started'
                and body.get('message') == 'The CRM health report did not start. Fix these settings first.'
                and len(problems) == 2 and problems[0] == 'MONDAY_API_TOKEN is empty' and problems[1].startswith('BOARDS_JSON is not valid JSON')
                and run.runs('Respond: Started') == 0 and not m.new('monday') and not m.new('slack', '/slack/report') and not m.new('resend')
                and run.last == 'Stop: Bad Config' and 'MONDAY_API_TOKEN is empty; BOARDS_JSON is not valid JSON' in run.error
                and 'Stop: Bad Config' in alert,
                f'HTTP {run.http[0]}, problems: {problems}')
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')


def scheduled_bad_config(r, error_wid):
    wf = test_copy(PATH + '-schedule', {'REPORT_CRON': '*/5 * * * * *', 'BOARDS_JSON': '[{"id": "123", "required_columns": "email"'})
    wid = import_workflow(wf, error_wid)
    mark = len(LOG)
    run, alert = None, ''
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        end, done = time.time() + 30, []
        while time.time() < end and not done:
            done = [e for e in api('GET', f'/api/v1/executions?workflowId={wid}&limit=20').get('data', []) if e.get('stoppedAt')]
            time.sleep(0.5)
        if done:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
            run = Run(min(done, key=lambda e: int(e['id'])))
            r.mark = mark
            alert = r.alert(run)
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')
    r.check('Scheduled run, broken BOARDS_JSON', 'the schedule fires from REPORT_CRON, stops before any API call, alert sent',
            run is not None and run.mode == 'trigger' and run.out('Load Config')[0]['trigger'] == 'schedule' and run.last == 'Stop: Bad Config'
            and run.runs('Respond: Bad Config') == 0
            and 'BOARDS_JSON is not valid JSON' in run.error and not [x for x in LOG[mark:] if x['service'] == 'monday'] and 'Stop: Bad Config' in alert,
            f'mode {run.mode if run else None}, ended at {run.last if run else None}')


def scheduled_report(r, error_wid):
    """The schedule with good settings (Slack only). The cron fires at one set second of the minute, about 8 seconds
    from now, and the copy is switched off after its first run, so exactly one scheduled run happens."""
    second = (datetime.now(timezone.utc).second + 8) % 60
    wf = test_copy(PATH + '-schedule-ok', {'REPORT_CRON': f'{second} * * * * *', 'HEALTH_LOG_BOARD_ID': '', 'HEALTH_LOG_COLUMNS': '',
                                            'REPORT_EMAIL_TO': '', 'RESEND_API_KEY': '', 'REPORT_EMAIL_FROM': ''})
    wid = import_workflow(wf, error_wid)
    mark = len(LOG)
    run = None
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        end, done = time.time() + 80, []
        while time.time() < end and not done:
            done = [e for e in api('GET', f'/api/v1/executions?workflowId={wid}&limit=20').get('data', []) if e.get('stoppedAt')]
            time.sleep(0.5)
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
        if done:
            run = Run(min(done, key=lambda e: int(e['id'])))
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')
    slack = [x for x in LOG[mark:] if x['path'] == '/slack/report' and x.get('body')]
    footer = slack[0]['body']['blocks'][-1]['elements'][0]['text'] if slack else ''
    r.check('Scheduled run, good settings', 'the schedule fires from REPORT_CRON and runs the whole report, with no Run Now answer involved',
            run is not None and run.mode == 'trigger' and run.status == 'success' and run.last == 'Any Problems?'
            and run.out('Load Config')[0]['trigger'] == 'schedule' and run.runs('Respond: Started') == 0
            and run.runs('Monday: Fetch Page') == 5 and board_result(run, A)['score'] == 85
            and len(slack) == 1 and footer.endswith('Run by schedule.'),
            f"mode {run.mode if run else None}, ended at {run.last if run else None}, {len(slack)} Slack report, footer: {footer[-25:]}")


def minimal_setup(r, error_wid):
    """Slack only: no health log board and no email configured."""
    path = PATH + '-minimal'
    wid = import_workflow(test_copy(path, {'HEALTH_LOG_BOARD_ID': '', 'HEALTH_LOG_COLUMNS': '', 'REPORT_EMAIL_TO': '',
                                           'RESEND_API_KEY': '', 'REPORT_EMAIL_FROM': ''}), error_wid)
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        m = Runner(wid, path, r.results)
        run = m.run_now()
        slack = m.new('slack', '/slack/report')
        footer = slack[0]['body']['blocks'][-1]['elements'][0]['text'] if slack and slack[0].get('body') else ''
        r.check('Slack only (no log board, no email)', 'one Slack report, no log calls, no email, run succeeds',
                run.status == 'success' and run.last == 'Any Problems?' and len(slack) == 1 and not m.monday('FindLogRows')
                and not m.monday('SaveHealthLog') and not m.new('resend') and 'Health log' not in footer and run.runs('Email: Send Report') == 0,
                f'{len(slack)} Slack message, {len(m.new("resend"))} emails, footer: {footer[-40:]}')
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')


def run_now_secret_unset(r, error_wid):
    """RUN_NOW_SECRET left empty: Run Now must refuse every call. The IF node compares loosely, so a missing or empty
    X-Run-Secret header equals an empty secret, and only the "secret is set" condition stops the call."""
    path = PATH + '-no-secret'
    wid = import_workflow(test_copy(path, {'RUN_NOW_SECRET': ''}), error_wid)
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        m = Runner(wid, path, r.results)
        for label, secret in (('empty X-Run-Secret header', ''), ('no header', None)):
            run = m.run_now(secret)
            got = (run.out('Run Now')[0].get('headers') or {}).get('x-run-secret') if run.runs('Run Now') else 'not run'
            r.check(f'Run Now, RUN_NOW_SECRET not set, {label}', 'refused with 401, settings not even read, nothing sent',
                    run.http == (401, {'status': 'unauthorized', 'message': 'Missing or wrong X-Run-Secret header.'})
                    and got == secret and run.runs('Load Config') == 0 and not m.new('monday') and not m.new('slack', '/slack/report')
                    and not m.new('resend') and run.last == 'Stop: Bad Run Now Secret',
                    f'header n8n received {got!r}, HTTP {run.http[0]}, ended at {run.last}')
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')


def big_slack_report(r, error_wid):
    """Slack only, 50 boards and TOP_OFFENDERS=25 with long item names. Slack refuses a whole message over 50 blocks or with
    a section over 3,000 characters, so the report must shorten itself and still name every board."""
    path = PATH + '-big'
    extra = [{'id': str(3000000000 + n), 'name': f'Region {n:02d}', 'required_columns': ['email'], 'email_column': 'email',
              'status_column': 'status', 'owner_column': 'person', 'done_labels': ['Done']} for n in range(1, 49)]
    missing = {'id': '3999999999', 'name': 'Missing Board', 'required_columns': ['email'], 'email_column': 'email'}
    for b in extra:
        BOARDS[b['id']] = board(b['id'], b['name'], COLS_C, [item(b['id'], int(b['id']) * 10 + 1, f"{b['name']} contact", NOW - timedelta(days=100),
                                                              NOW - timedelta(days=2), {'email': f"contact@{b['name'].lower().replace(' ', '')}.example.com",
                                                                                        'status': 'Active', 'person': 'Sam Lee'})])
    names = {it['id']: it['name'] for it in BOARDS[A]['items']}
    for it in BOARDS[A]['items']:
        it['name'] = (it['name'] + ' of Northwind Regional Property and Facilities Management Holdings Group ' * 3)[:200].strip()
    wid = None
    try:
        wid = import_workflow(test_copy(path, {'BOARDS_JSON': json.dumps([BOARDS_CONFIG[0], *extra, missing]), 'TOP_OFFENDERS': '25',
                                               'HEALTH_LOG_BOARD_ID': '', 'HEALTH_LOG_COLUMNS': '', 'REPORT_EMAIL_TO': '', 'RESEND_API_KEY': '',
                                               'REPORT_EMAIL_FROM': ''}), error_wid)
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        m = Runner(wid, path, r.results)
        run = m.run_now()
        slack = m.new('slack', '/slack/report')
        body = slack[0].get('body') if slack else None
        blocks = body['blocks'] if body else []
        sections = [b['text']['text'] for b in blocks if b.get('type') == 'section']
        shown = [s for s in sections if re.match(r':[a-z_]+: \*', s)]
        note = section(blocks, 'more boards are not shown here')
        r.check('Big report, Slack only (50 boards)', 'Slack accepts it: exactly 50 blocks, 46 boards shown, the other 4 named with their score',
                len(slack) == 1 and body is not None and slack[0]['rejected'] is None and len(blocks) == 50
                and not [b for b in blocks if b['type'] == 'divider'] and len(shown) == 46
                and bool(shown) and '|Region 45>*' in shown[-1] and not [s for s in shown if 'Region 46' in s]
                and note == ('4 more boards are not shown here, because Slack allows 50 blocks in one message: '
                             'Region 46 100/100, Region 47 100/100, Region 48 100/100, Missing Board not checked.')
                and bool(sections) and '*49 of 50 boards checked*' in sections[0] and run.last == 'Stop: Report Problems',
                f"{len(slack)} message, {len(blocks)} blocks, {len(shown)} boards shown, note: {note[:60]}")
        sales_text = section(blocks, 'Sales Pipeline')
        listed = sales_text.count('\n• ')
        offenders = board_result(run, A)['offenders'] if run.runs('Analyze Boards') else []
        reads = page_ops(m)
        r.check('Big report, Slack only (50 boards)', 'a section over the limit drops worst items and says how many: no section over 3,000 characters',
                len(offenders) == 19 and 0 < listed < 19 and sales_text.endswith(f'\nand {19 - listed} more') and len(sales_text) <= 2865
                and sales_text.startswith(':large_yellow_circle:') and max([len(s) for s in sections] or [0]) <= 3000
                and len(reads) == 52 and not [x for x in m.new('monday') if x['rejected']] and not m.monday('FindLogRows') and not m.new('resend'),
                f'{len(offenders)} worst items found, {listed} listed, section {len(sales_text)} characters, {len(reads)} page reads')
    finally:
        for b in extra:
            BOARDS.pop(b['id'], None)
        for it in BOARDS[A]['items']:
            it['name'] = names[it['id']]
        if wid:
            try:
                api('POST', f'/api/v1/workflows/{wid}/deactivate')
            finally:
                api('DELETE', f'/api/v1/workflows/{wid}')


def guarded(r, scenario, *args):
    """Runs one scenario. If it crashes (for example a node never ran), that is a FAIL, and the next scenario still runs."""
    reset_faults()
    try:
        scenario(r, *args)
    except Exception as e:  # noqa: BLE001
        r.check(scenario.__name__, 'scenario ran to the end', False, f'{type(e).__name__}: {e}')
    finally:
        reset_faults()


def run_all(r, error_wid):
    mock_self_test(r)
    run_now_security(r)
    guarded(r, full_report)
    for scenario in (rerun_same_day, board_emptied, rate_limit, low_budget, server_errors_then_ok, temporarily_blocked, board_errors,
                     cursor_expired, wrong_column, formula_column, query_too_complex, monday_down, bad_token, daily_limit, account_wide_errors,
                     monday_unreachable, log_lookup_fails, log_key_column_gone, log_save_fails, log_write_fails_then_recovers,
                     log_save_answer_lost, slack_down, email_answer_lost, email_refused):
        guarded(r, scenario)
    keys = [row['text_key'] for row in log_rows()]
    r.check('After every run', f'the health log still has exactly one row per board for {DATE}',
            sorted(keys) == sorted([f'{DATE}|{A}', f'{DATE}|{B}', f'{DATE}|{C}']), f'{len(keys)} rows after {r.sent} runs')
    sent = [x for x in LOG if x['service'] == 'monday']
    on_purpose = sorted(x['rejected'] for x in sent if x['rejected'] in ('auth', 'version'))
    malformed = [(x.get('op'), x['rejected'], x.get('problems')) for x in sent if x['rejected'] not in (None, 'auth', 'version')]
    r.check('After every run', 'no monday.com request was ever rejected as malformed (schema, variables, headers)',
            not malformed and on_purpose == ['auth', 'version'],
            f'{len(sent)} requests, {len(malformed)} malformed, 2 refused on purpose (wrong token, retired API version)' if not malformed else str(malformed[:3]))
    guarded(r, minimal_setup, error_wid)
    guarded(r, big_slack_report, error_wid)
    guarded(r, run_now_secret_unset, error_wid)
    guarded(r, email_only_page_limit, error_wid)
    guarded(r, run_now_bad_config, error_wid)
    guarded(r, scheduled_report, error_wid)
    guarded(r, scheduled_bad_config, error_wid)


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    try:
        server = QuietServer(('127.0.0.1', MOCK_PORT), Mock)
    except OSError as e:
        raise SystemExit(f'Port {MOCK_PORT} is already in use, so the mock APIs cannot start ({e}).')
    threading.Thread(target=server.serve_forever, daemon=True).start()
    error_wf = json.loads(ERROR_WORKFLOW.read_text(encoding='utf-8'))
    error_wf['nodes'] = [{**n, 'parameters': json.loads(json.dumps(n['parameters']).replace('$env.SLACK_WEBHOOK_URL', json.dumps(MOCK + '/slack/alerts').replace('"', "'")))} for n in error_wf['nodes']]
    error_wid = import_workflow(error_wf)
    wid = None
    r = Runner(None)
    try:
        api('POST', f'/api/v1/workflows/{error_wid}/activate')   # n8n 2 only calls an error workflow that is active
        wid = import_workflow(test_copy(PATH), error_wid)
        r.wid = wid
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(2)
        run_all(r, error_wid)
    finally:
        try:
            if wid:
                try:
                    api('POST', f'/api/v1/workflows/{wid}/deactivate')
                finally:
                    api('DELETE', f'/api/v1/workflows/{wid}')
        finally:
            try:
                api('POST', f'/api/v1/workflows/{error_wid}/deactivate')
            finally:
                api('DELETE', f'/api/v1/workflows/{error_wid}')
                server.shutdown()
            if os.environ.get('E2E_DUMP'):
                Path(os.environ['E2E_DUMP']).write_text(json.dumps({
                    'monday': [{k: x.get(k) for k in ('op', 'kind', 'query', 'variables')} for x in LOG if x['service'] == 'monday' and x.get('op')],
                    'slack': [{'path': x['path'], 'body': x.get('body')} for x in LOG if x['service'] == 'slack'],
                    'email': [{'headers': {k: v for k, v in x['headers'].items() if k == 'idempotency-key'}, 'body': x.get('body')}
                              for x in LOG if x['service'] == 'resend'],
                    'log_rows': log_rows()}, indent=1), encoding='utf-8')

    passed = sum(ok for *_, ok, _ in r.results)
    last = None
    for scenario, label, ok, detail in r.results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(r.results)} checks passed')
    sys.exit(0 if passed == len(r.results) else 1)


if __name__ == '__main__':
    main()
