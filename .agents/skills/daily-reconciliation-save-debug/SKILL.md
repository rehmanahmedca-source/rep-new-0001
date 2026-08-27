---
name: daily-reconciliation-save-debug
description: Diagnose and fix row-level save errors in the Daily Reconciliations board, especially when values update but the action column shows Error. Use when debugging AJAX form submissions, CSRF retries, invalid request URLs, or form fields that shadow native DOM properties.
---

# Daily Reconciliation Save Debugging

Use this skill for the per-account `Actual Counted` save flow in the Cash Flow
application.

## Project locations

- Board markup and client-side save logic: `templates/_daily_recon_board.html`
- Daily reconciliation endpoint: `app/blueprints/reports/cash.py`
- Reconciliation persistence and calculations: `app/services/cash_day_recon.py`
- Global CSRF handling: `app/hooks.py`
- Reconciliation tests:
  - `tests/test_cash_day_reconciliation_page.py`
  - `tests/test_cash_flow_reconciliation_chain.py`
- Application request log: `instance/logs/errorlog.txt`

## First diagnose the request, not just the displayed row

When the board shows a red `Error`, establish which layer failed:

1. **Persistence/service layer** — did the counted amount save?
2. **HTTP layer** — did the browser call `/daily_reconciliation`?
3. **Response layer** — did the endpoint return the expected JSON?
4. **UI layer** — did the JavaScript update the row as successful?

A row that shows a new counted amount and a recalculated difference may still have
had a failed AJAX request, especially if a native form submission or page reload
also occurred.

## Investigation workflow

### 1. Read the latest logs after reproducing

Refresh workflow/browser logs, then inspect the newest request lines. Search for:

```bash
rg -n -i "daily_reconciliation|RadioNodeList|CSRF|404|500" \
  instance/logs/errorlog.txt /tmp/logs
```

Interpretation:

- `POST /daily_reconciliation 200` usually indicates the JSON save path reached
  the endpoint successfully.
- `POST /daily_reconciliation 302` indicates a normal form redirect or a
  server-side failure path that needs further inspection.
- `POST /[object RadioNodeList] 404` indicates a client-side URL construction
  problem, not a reconciliation calculation problem.
- CSRF rejection means inspect token freshness and retry handling separately.

### 2. Inspect the form and save script together

Search for form URL and submit handling:

```bash
rg -n -i "form\.action|form\.getAttribute|fetch\(|FormData|submitter|name=\"action\"" \
  templates/_daily_recon_board.html templates/layout.html static
```

Pay special attention to forms containing controls named:

- `action`
- `method`
- `target`
- `name`

These names can shadow native form properties in browser JavaScript.

### 3. Recognize the `RadioNodeList` collision

If logs contain:

```text
/[object RadioNodeList]
```

look for this pattern:

```html
<form action="/daily_reconciliation">
  <input name="action" value="save_count">
</form>
```

combined with:

```javascript
fetch(form.action, options);
```

The form control named `action` can make `form.action` resolve to a named
control/collection instead of the URL string.

### 4. Apply the safe URL fix

Use the actual HTML attribute for both fetch and URL parsing:

```javascript
const formAction = form.getAttribute('action') || window.location.href;
const url = new URL(formAction, window.location.origin);
const response = await fetch(formAction, options);
```

Do not solve this by removing the hidden `action` field unless the server and
submit-button contract are changed together. The attribute lookup is the smaller,
backward-compatible fix.

### 5. Preserve action selection independently

`FormData(form)` does not always include the clicked submit button. Use
`SubmitEvent.submitter` when available, then explicitly set the action:

```javascript
const submitter = event && event.submitter;
const data = new FormData(form);
if (submitter && submitter.name) {
  data.set(submitter.name, submitter.value);
}
if (!data.get('action')) {
  data.set('action', 'save_count');
}
```

Never use `document.activeElement` as a substitute for the submitter. Pressing
Enter or clicking elsewhere can make it identify the wrong element or no action
at all.

### 6. Keep CSRF handling separate

If the server explicitly rejects CSRF:

1. Fetch the current reconciliation page/token.
2. Update the form's hidden token.
3. Preserve the entered amount.
4. Retry once.
5. Report a real error if the retry fails.

Do not disable server-side CSRF validation to hide the problem. A CSRF failure and
an invalid fetch URL are different failures and should be diagnosed independently.

## Tests to add or run

Run the focused regression tests:

```bash
pytest -q tests/test_cash_day_reconciliation_page.py \
  tests/test_cash_flow_reconciliation_chain.py
```

For this specific bug, the page/template test should assert:

```python
assert "form.getAttribute('action')" in body
assert "fetch(form.action" not in body
```

Also compile changed Python modules:

```bash
python -m py_compile app/blueprints/reports/cash.py \
  app/services/cash_day_recon.py
```

Restart the application workflow after template, JavaScript, Python, or run
configuration changes. Then inspect fresh logs; old `/[object RadioNodeList]`
entries do not prove the restarted application is still failing.

## Completion checklist

- [ ] Reproduced the error or inspected the exact failing request.
- [ ] Checked the HTTP status and response body.
- [ ] Distinguished CSRF failure from invalid URL failure.
- [ ] Checked for named form controls shadowing native form properties.
- [ ] Used `getAttribute('action')` for the request URL.
- [ ] Preserved submitter/action handling.
- [ ] Added a regression assertion.
- [ ] Ran focused tests and Python compilation.
- [ ] Restarted the workflow and checked fresh logs/browser errors.
