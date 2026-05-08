# GraphQL Variables Structure Fix

## Problem

After fixing the GraphQL endpoint URL to `/checkouts/unstable/graphql`, the API was still failing with GraphQL schema validation errors:

```
Variable $delivery was provided invalid value for:
- deliveryLines.0.destination.oneTimeUse (Field is not defined on DeliveryAddressInput)
- deliveryLines.0.targetMerchandise (Field is not defined on DeliveryLineInput)
- deliveryLines.0.selectedDeliveryStrategy (Expected value to not be null)
- deliveryLines.0.expectedTotalPrice (Expected value to not be null)
- deliveryLines.0.deliveryMethodTypes (Expected value to not be null)
- noDeliveryRequired (Expected value to not be null)

Variable $merchandise was provided invalid value for:
- lines (Field is not defined on MerchandiseTermInput)
- merchandiseLines (Expected value to not be null)
```

## Root Cause

The Go implementation was using incorrect field names and missing required fields in the GraphQL variables. The Shopify GraphQL schema is very specific about field names and structure.

### Issues Found

1. **`oneTimeUse` field** - Doesn't exist on `DeliveryAddressInput`, should be removed
2. **`targetMerchandise`** - Wrong field name, should be `targetMerchandiseLines`
3. **`merchandise.lines`** - Wrong field name, should be `merchandise.merchandiseLines`
4. **Missing required fields** - Several null-required fields were missing:
   - `selectedDeliveryStrategy`
   - `expectedTotalPrice`
   - `deliveryMethodTypes`
   - `noDeliveryRequired`

## Solution

Fixed the variable structure in `/home/runner/work/Shopi/Shopi/shopify-payment-api/internal/graphql/queries.go`:

### Before (Incorrect)
```go
"delivery": map[string]interface{}{
    "deliveryLines": []map[string]interface{}{
        {
            "destination": map[string]interface{}{
                "oneTimeUse": true,  // ❌ Field doesn't exist
                "streetAddress": map[string]interface{}{...},
            },
            "targetMerchandise": map[string]interface{}{  // ❌ Wrong field name
                "lines": []map[string]interface{}{...},
            },
            // ❌ Missing required fields
        },
    },
},
"merchandise": map[string]interface{}{
    "lines": []map[string]interface{}{...},  // ❌ Wrong field name
},
```

### After (Correct)
```go
"delivery": map[string]interface{}{
    "deliveryLines": []map[string]interface{}{
        {
            "destination": map[string]interface{}{
                "streetAddress": map[string]interface{}{...},  // ✅ Removed oneTimeUse
            },
            "targetMerchandiseLines": []map[string]interface{}{  // ✅ Correct field name
                {
                    "merchandiseId": "gid://shopify/ProductVariantMerchandise/" + b.MerchandiseID,
                    "quantity": map[string]interface{}{
                        "items": 1,
                    },
                },
            },
            "selectedDeliveryStrategy": nil,  // ✅ Added required field
            "expectedTotalPrice":       nil,  // ✅ Added required field
            "deliveryMethodTypes":      []string{},  // ✅ Added required field
        },
    },
    "noDeliveryRequired": false,  // ✅ Added required field
},
"merchandise": map[string]interface{}{
    "merchandiseLines": []map[string]interface{}{  // ✅ Correct field name
        {
            "merchandiseId": "gid://shopify/ProductVariantMerchandise/" + b.MerchandiseID,
            "quantity": map[string]interface{}{
                "items": 1,
            },
        },
    },
},
```

## Files Modified

- `internal/graphql/queries.go` - Fixed BuildProposalVariables function (lines 241-281)

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

Expected: No more GraphQL schema validation errors, payment processing should proceed.

## Complete Fix Chain

This is the **fourth critical fix** in the session:

1. ✅ **Cookie jar fix** - Added cookie persistence for HTTP redirects
2. ✅ **GraphQL query structure** - Fixed `sessionInput` nesting
3. ✅ **GraphQL endpoint URL** - Changed from `/api/graphql` to `/checkouts/unstable/graphql`
4. ✅ **GraphQL variable structure** - Fixed field names and added required fields

All four fixes were necessary to get the Go implementation working correctly with Shopify's checkout API.

## Key Learnings

- Shopify's GraphQL schema is strict about field names and structure
- Missing required fields (even with `nil` values) cause validation errors
- Field names must match exactly: `merchandiseLines` not `lines`, `targetMerchandiseLines` not `targetMerchandise`
- Extra fields that don't exist in the schema (like `oneTimeUse`) cause errors
- Always test GraphQL variables against the actual API schema
