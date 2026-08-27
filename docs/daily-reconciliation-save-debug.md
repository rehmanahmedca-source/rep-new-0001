# Daily Reconciliation Save Error: Discovery and Fix

## Summary

The **Cash Flow → Daily Reconciliations** board showed a red `Error` after saving an
`Actual Counted` amount. The amount appeared in the board and the difference changed
to `0 (RECONCILED)`, which made the failure confusing: the server-side save was
working, but the browser-side save request was not completing correctly.

The final cause was a JavaScript/HTML naming collision:

- Each reconciliation form has a hidden input named `action`.
- In browsers, named form controls can become properties of the form element.
- Therefore `form.action` resolved to the input collection (`RadioNodeList`) instead
  of the form's URL.
- `fetch(form.action, ...)` converted that object to the URL path
  `/[object RadioNodeList]`.
- The server returned `404`, and the row's JavaScript marked the save as `Error`.

The fix was to read the HTML attribute directly:

```javascript
const formAction = form.getAttribute('action') || window.location.href;
await fetch(formAction, options);
```

The same safe lookup was used when refreshing the CSRF token.

## User-visible symptom

The reported state was:

- Reconciliation day `2026-08-27` was unlocked.
- Expected total: `Rs. 50,000`.
- Counted total: `Rs. 50,000`.
- Entered amount: `7777777777777`.
- Difference: `0 (RECONCILED)`.
- Action column: `Error`.

This combination was an important clue. If the displayed counted value and
difference had updated, the database/service calculation path was likely succeeding.
The investigation therefore needed to separate:

1. Whether the server saved the amount.
2. Whether the browser sent the intended request.
3. Whether the browser interpreted the response as success.

## Discovery process

### 1. Start from the actual user-visible path

The investigation focused on the per-account `Actual Counted` save flow in
`templates/_daily_recon_board.html`, not the older physical-cash reconciliation
flow. The relevant server path is the `save_count` action handled by the daily
reconciliation endpoint.

The board JavaScript had these responsibilities:

- intercept the form submit;
- preserve the clicked button's `action` value;
- send the form with `fetch`;
- handle CSRF/session-token errors;
- update the row from the JSON response.

### 2. Check server and browser logs after a fresh reproduction

The application request log contained repeated requests like:

```text
POST /[object RadioNodeList] 404
```

This was more specific than a generic save failure. It showed that the browser was
constructing an invalid URL before the request reached the reconciliation endpoint.

The logs also showed requests to:

```text
POST /daily_reconciliation 302
```

Those normal form submissions explained why some values could still be saved or
displayed after a page reload, but they did not explain the repeated row-level
`Error`. The `404` requests were the decisive evidence for the client-side failure.

### 3. Search for the invalid URL and its source

The search was narrowed to the exact error and then to form URL handling:

```bash
rg -n -i "RadioNodeList|form\.action|\.action\s*=|elements\[|submit\(" \
  static templates app blueprints
```

The reconciliation template contained both:

```html
<input type="hidden" name="action" value="save_count">
```

and JavaScript using:

```javascript
fetch(form.action, ...)
```

The same form also has a submit button with `name="action"`. That made the native
`form.action` property unsafe to use as the request URL.

### 4. Confirm the browser naming collision

HTML forms expose named controls as properties. A control named `action` can shadow
the form's built-in `action` URL property. When the value is represented as a
collection, JavaScript string conversion produces:

```text
[object RadioNodeList]
```

That exactly matched the request path in the server log, connecting the HTML field,
the JavaScript expression, and the observed `404`.

## Why the earlier CSRF fix did not fully solve it

The application also had a real CSRF/session-token problem. A prior change added a
recovery path that fetches a fresh reconciliation page/token and retries once when
the server reports a CSRF error. Server-side CSRF enforcement correctly remained
enabled.

That change addressed requests rejected by CSRF validation. It did not address the
separate problem where the browser never used the intended endpoint URL at all.
The new `/[object RadioNodeList]` evidence distinguished the two failures.

## Fix implemented

In `templates/_daily_recon_board.html`, both URL reads were changed from the DOM
property to the explicit HTML attribute.

### Before

```javascript
const url = new URL(form.action, window.location.origin);
const res = await fetch(form.action, options);
```

### After

```javascript
const formAction = form.getAttribute('action') || window.location.href;
const url = new URL(formAction, window.location.origin);

const formAction = form.getAttribute('action') || window.location.href;
const res = await fetch(formAction, options);
```

The fallback keeps the code usable if a form is created without an explicit
`action` attribute.

The hidden `action` field was not renamed because the server and submit-button
behavior already depend on it. Reading the attribute is the smaller and safer fix.

## Regression protection

The reconciliation page test now verifies that:

- the JavaScript uses `form.getAttribute('action')`;
- it no longer contains `fetch(form.action`.

This protects against reintroducing the exact naming-collision bug during future
template edits.

## Verification

The following checks passed after the fix:

```bash
pytest -q tests/test_cash_day_reconciliation_page.py \
  tests/test_cash_flow_reconciliation_chain.py
```

Result:

```text
11 passed
```

Python compilation also passed:

```bash
python -m py_compile app/blueprints/reports/cash.py \
  app/services/cash_day_recon.py
```

The Flask workflow was restarted successfully. Fresh workflow and browser logs
showed no new `/[object RadioNodeList]` requests or browser errors after restart.

## General lesson

When a form save appears to update some UI data but ends in an error:

1. Inspect the actual network request URL and status.
2. Do not assume that a successful-looking page value proves the AJAX request
   succeeded.
3. Search for invalid URL stringification such as `[object HTML...]` or
   `[object RadioNodeList]`.
4. Check whether a form control name shadows a native form property.
5. Use `getAttribute('action')` when the form contains a control named `action`.
