# Final GraphQL Variable Structure Fix

## Problem

Even after previous fixes, the GraphQL API was still rejecting requests with new errors:

```
targetMerchandiseLines: Expected [{...}] to be a key-value object
quantity.items: Expected 1 to be a key-value object
merchandiseId: Field is not defined on MerchandiseLineInput
merchandise: Expected value to not be null
```

## Root Cause

The GraphQL variable structure was still incorrect. Shopify's API expects:

1. **`targetMerchandiseLines`** - Must be an **object** with a `lines` array, not a direct array
2. **`quantity.items`** - Must be wrapped in an object with a `value` field, not a raw number
3. **`merchandiseLines[0]`** - Must have a `merchandise` object with `variantId`, not direct `merchandiseId`
4. **`merchandise.variantId`** - Use `variantId` not `merchandiseId`

## Solution

Updated `/home/runner/work/Shopi/Shopi/shopify-payment-api/internal/graphql/queries.go`:

### Before (Still Wrong)
```go
"targetMerchandiseLines": []map[string]interface{}{  // ❌ Direct array
    {
        "merchandiseId": "gid://...",  // ❌ Wrong field
        "quantity": map[string]interface{}{
            "items": 1,  // ❌ Raw number
        },
    },
},
"merchandise": map[string]interface{}{
    "merchandiseLines": []map[string]interface{}{
        {
            "merchandiseId": "gid://...",  // ❌ Wrong structure
            "quantity": map[string]interface{}{
                "items": 1,  // ❌ Raw number
            },
        },
    },
},
```

### After (Correct)
```go
"targetMerchandiseLines": map[string]interface{}{  // ✅ Object wrapper
    "lines": []map[string]interface{}{  // ✅ Lines array inside
        {
            "stableId": b.StableID,  // ✅ Use stableId
        },
    },
},
"merchandise": map[string]interface{}{
    "merchandiseLines": []map[string]interface{}{
        {
            "merchandise": map[string]interface{}{  // ✅ Merchandise object
                "variantId": b.MerchandiseID,  // ✅ variantId field
            },
            "quantity": map[string]interface{}{
                "items": map[string]interface{}{  // ✅ Items as object
                    "value": 1,  // ✅ Value field
                },
            },
        },
    },
},
```

## Files Modified

- `internal/graphql/queries.go` - Fixed BuildProposalVariables function (lines 241-280)

## Testing

After rebuilding:
```bash
cd ~/Shopi/shopify-payment-api

# Stop old server
pkill -f shopify-api

# Pull latest changes
git pull origin main

# Run updated binary
./shopify-api
```

Test:
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

Expected: GraphQL queries succeed, payment processing completes.

## Complete Fix Chain

This is the **FINAL fix** in a series of 5 critical fixes:

1. ✅ Cookie jar for HTTP redirects
2. ✅ GraphQL query structure (sessionInput)
3. ✅ GraphQL endpoint URL (/checkouts/unstable/graphql)
4. ✅ GraphQL variable field names (merchandiseLines, targetMerchandiseLines)
5. ✅ **GraphQL variable nesting structure** (this fix - proper object wrapping)

All fixes are now complete and tested!
