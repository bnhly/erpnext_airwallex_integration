# Diagnostic Probes

A single whitelisted helper in `bank_integration/airwallex/utils.py` lets an
Accounts Manager spot-check Airwallex API responses from the browser without
needing a code push.

## `airwallex_api_get` — generic GET

Calls any read endpoint via `AirwallexBase` and returns the raw JSON. Useful
for verifying response shape on a new endpoint before wiring it into the
sync.

```
/api/method/bank_integration.airwallex.utils.airwallex_api_get
    ?endpoint=issuing/transactions/<id>

/api/method/bank_integration.airwallex.utils.airwallex_api_get
    ?endpoint=spend/expenses&params_json={"page_size":5}

/api/method/bank_integration.airwallex.utils.airwallex_api_get
    ?endpoint=transfers/<id>&client_index=1
```

Parameters:

- `endpoint` (required) — Airwallex path after `/api/v1/`, e.g.
  `issuing/transactions/<id>`. No leading slash needed.
- `params_json` (optional) — JSON-encoded query params, e.g.
  `{"page_size":5,"from_created_at":"2026-05-01T00:00:00Z"}`.
- `client_index` (optional, default `0`) — which Airwallex Client row on
  Bank Integration Setting to authenticate as.

Errors come back as `{"error": true, "status_code": 4xx, "message": "..."}`
rather than raising.

## Security

Role-gated to Accounts Manager. The Airwallex API key has broad read access
across the tenant (financial transactions, beneficiaries, card transaction
details), so arbitrary endpoint calls are a privileged operation and must
not be opened up to other roles.
