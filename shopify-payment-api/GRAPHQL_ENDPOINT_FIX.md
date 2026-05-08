# GraphQL Endpoint Fix

## Problem

The Go implementation was using the wrong GraphQL endpoint URL, causing all GraphQL queries to fail with errors like:

```
Field 'session' doesn't exist on type 'QueryRoot'
SessionTokenInput isn't a defined input type
```

Session token extraction was working correctly (status 200, HTML received), but the GraphQL API was rejecting the queries.

## Root Cause

The Go implementation was using:
```go
graphqlURL := fmt.Sprintf("%s/api/graphql", task.SiteURL)
```

But the Python implementation (Autoshopify_FIXED.py) uses:
```python
graphql_url = f'https://{urlparse(ourl).netloc}/checkouts/unstable/graphql'
```

The `/api/graphql` endpoint is a different, more limited API that doesn't support the full checkout negotiation schema with `session(sessionInput: ...)` structure.

The `/checkouts/unstable/graphql` endpoint is the correct endpoint that supports:
- `session(sessionInput: $sessionInput)` query
- Full checkout negotiation with `SessionTokenInput`
- All the proposal terms (BuyerIdentityTermInput, DeliveryTermsInput, etc.)

## Solution

Changed both GraphQL endpoint constructions in `internal/api/processor.go`:

**Before:**
```go
graphqlURL := fmt.Sprintf("%s/api/graphql", task.SiteURL)
```

**After:**
```go
graphqlURL := fmt.Sprintf("%s/checkouts/unstable/graphql", task.SiteURL)
```

## Files Modified

- `internal/api/processor.go` (lines 342 and 463)
  - Updated `executeProposals` function
  - Updated `submitPayment` function

## Testing

After rebuild:
```bash
cd /home/runner/work/Shopi/Shopi/shopify-payment-api
go build -o shopify-api ./cmd/api
./shopify-api
```

Test with curl:
```bash
curl -X POST http://127.0.0.1:8080/shopify/check \
  -H "Content-Type: application/json" \
  -d '{
    "site_url": "https://yallsweettea.com",
    "card": {
      "number": "4111111111111111",
      "month": "12",
      "year": "2028",
      "cvv": "123"
    }
  }'
```

Expected: GraphQL queries should now succeed without schema errors.

## Related Issues

This fix complements the previous fixes:
1. Cookie jar fix (REDIRECT_FIX.md) - Fixed HTTP redirect handling
2. GraphQL structure fix (SESSION_TOKEN_FIX.md) - Fixed query schema structure
3. **This fix** - Fixed API endpoint URL

All three were necessary to fully resolve the session token extraction and payment processing issues.
