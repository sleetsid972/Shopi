# GraphQL Variable Construction Fix

## Problem

The Go implementation was crashing during the first shipping proposal (`Proposal` query) because the GraphQL variable structure did not match Shopify's schema requirements. The Python reference implementation (`Autoshopify (1) (4).py`) was working correctly.

## Root Causes

### 1. Delivery Variables Structure Issues

**Problem**: Missing required fields in `delivery.deliveryLines`:
- Missing `selectedDeliveryStrategy` with proper structure
- Missing `deliveryMethodTypes: ["SHIPPING"]`
- Missing `expectedTotalPrice: {any: true}`
- Missing `destinationChanged` boolean
- Missing `noDeliveryRequired` array
- Using `streetAddress` instead of `partialStreetAddress`

**Python Structure (lines 420-445)**:
```python
'delivery': {
    'deliveryLines': [{
        'destination': {
            'partialStreetAddress': {  # NOT streetAddress
                'address1': street,
                # ... other fields
            }
        },
        'selectedDeliveryStrategy': {
            'deliveryStrategyMatchingConditions': {  # For first proposal
                'estimatedTimeInTransit': {'any': True},
                'shipments': {'any': True}
            },
            'options': {}
        },
        'targetMerchandiseLines': {'any': True},  # For first proposal
        'deliveryMethodTypes': ['SHIPPING'],
        'expectedTotalPrice': {'any': True},
        'destinationChanged': True
    }],
    'noDeliveryRequired': [],
    'useProgressiveRates': False,
    'prefetchShippingRatesStrategy': None,
    'supportsSplitShipping': True
}
```

### 2. Merchandise Variables Structure Issues

**Problem**: Using flat `variantId` field instead of nested `productVariantReference` object:
- Missing `productVariantReference` wrapper
- Missing `properties`, `sellingPlanId`, `sellingPlanDigest`
- Missing `expectedTotalPrice`, `lineComponentsSource`, `lineComponents`

**Python Structure (lines 448-463)**:
```python
'merchandise': {
    'merchandiseLines': [{
        'stableId': stableId or '1',
        'merchandise': {
            'productVariantReference': {  # Required wrapper
                'id': f'gid://shopify/ProductVariantMerchandise/{merch}',
                'variantId': f'gid://shopify/ProductVariant/{variant_id}',
                'properties': [],
                'sellingPlanId': None,
                'sellingPlanDigest': None
            }
        },
        'quantity': {'items': {'value': 1}},
        'expectedTotalPrice': {
            'value': {'amount': subtotal, 'currencyCode': currency}
        },
        'lineComponentsSource': None,
        'lineComponents': []
    }]
}
```

### 3. BuyerIdentity Incomplete

**Problem**: Only had minimal fields, missing customer info and preferences

**Python Structure (lines 475-489)**:
```python
'buyerIdentity': {
    'customer': {
        'presentmentCurrency': currency,
        'countryCode': country_code
    },
    'email': email,
    'emailChanged': False,
    'phoneCountryCode': country_code,
    'shopPayOptInPhone': {'number': phone},
    'acceptsEmailMarketing': False,
    'acceptsSmsMarketing': False,
    'languageCode': 'EN',
    'usesSameAddressForBilling': True
}
```

### 4. Submit Mutation Differences

**Problem**: Submit mutation needs different structure than proposal:
- Use `deliveryStrategyByHandle` instead of `deliveryStrategyMatchingConditions`
- Use specific `targetMerchandiseLines.lines` array instead of `{any: true}`
- Set `destinationChanged: false` instead of `true`

**Python Structure (lines 633-646)**:
```python
# For submit mutation
json_data['variables']['delivery']['deliveryLines'][0]['selectedDeliveryStrategy'] = {
    'deliveryStrategyByHandle': {  # Different from proposal
        'handle': delivery_strategy,
        'customDeliveryRate': False
    },
    'options': {}
}
json_data['variables']['delivery']['deliveryLines'][0]['targetMerchandiseLines'] = {
    'lines': [{'stableId': stableId or '1'}]  # Not {any: true}
}
json_data['variables']['delivery']['deliveryLines'][0]['destinationChanged'] = False
```

## Solution Applied

### File: `internal/graphql/queries.go`

#### BuildProposalVariables Function (Lines 224-383)

**Changes Made**:

1. **Delivery Structure**:
   - Changed `destination.streetAddress` → `destination.partialStreetAddress`
   - Added `selectedDeliveryStrategy` with `deliveryStrategyMatchingConditions`
   - Added `targetMerchandiseLines: {any: true}` for first call
   - Added `deliveryMethodTypes: ["SHIPPING"]`
   - Added `expectedTotalPrice: {any: true}`
   - Added `destinationChanged: true`
   - Added `noDeliveryRequired: []`
   - Added `useProgressiveRates: false`
   - Added `prefetchShippingRatesStrategy: nil`
   - Added `supportsSplitShipping: true`

2. **Merchandise Structure**:
   - Wrapped variant in `productVariantReference` object:
     ```go
     "merchandise": map[string]interface{}{
         "productVariantReference": map[string]interface{}{
             "id":                fmt.Sprintf("gid://shopify/ProductVariantMerchandise/%s", b.MerchandiseID),
             "variantId":         fmt.Sprintf("gid://shopify/ProductVariant/%s", variantID),
             "properties":        []interface{}{},
             "sellingPlanId":     nil,
             "sellingPlanDigest": nil,
         },
     }
     ```
   - Added `expectedTotalPrice` with value object
   - Added `lineComponentsSource: nil`
   - Added `lineComponents: []`

3. **BuyerIdentity Structure**:
   - Added `customer` object with `presentmentCurrency` and `countryCode`
   - Added `emailChanged: false`
   - Added `phoneCountryCode`
   - Added `acceptsEmailMarketing: false`
   - Added `acceptsSmsMarketing: false`
   - Added `languageCode: "EN"`
   - Added `usesSameAddressForBilling: true`

4. **Other Additions**:
   - Added `deliveryExpectations` with empty `deliveryExpectationLines`
   - Updated `payment` structure for both proposal and submit cases

#### BuildSubmitVariables Function (Lines 385-423)

**Changes Made**:

1. Changed `selectedDeliveryStrategy` structure:
   ```go
   // Before: Simple handle string
   "selectedDeliveryStrategy": map[string]interface{}{
       "handle": deliveryStrategy,
   }

   // After: Nested with deliveryStrategyByHandle
   "selectedDeliveryStrategy": map[string]interface{}{
       "deliveryStrategyByHandle": map[string]interface{}{
           "handle":             deliveryStrategy,
           "customDeliveryRate": false,
       },
       "options": map[string]interface{}{},
   }
   ```

2. Changed `targetMerchandiseLines` from `{any: true}` to specific lines:
   ```go
   "targetMerchandiseLines": map[string]interface{}{
       "lines": []map[string]interface{}{
           {"stableId": stableID},
       },
   }
   ```

3. Set `destinationChanged: false` for submit (was `true` in proposal)

## Before vs After Comparison

### Before (Wrong)

```go
// Delivery - WRONG
"delivery": map[string]interface{}{
    "deliveryLines": []map[string]interface{}{
        {
            "destination": map[string]interface{}{
                "streetAddress": map[string]interface{}{...},  // Wrong key
            },
            "targetMerchandiseLines": map[string]interface{}{
                "lines": []map[string]interface{}{
                    {"stableId": b.StableID},
                },
            },
            // Missing: selectedDeliveryStrategy, deliveryMethodTypes,
            // expectedTotalPrice, destinationChanged
        },
    },
    // Missing: noDeliveryRequired, useProgressiveRates, etc.
}

// Merchandise - WRONG
"merchandise": map[string]interface{}{
    "merchandiseLines": []map[string]interface{}{
        {
            "merchandise": map[string]interface{}{
                "variantId": b.MerchandiseID,  // Wrong structure
            },
            "quantity": {...},
            // Missing: expectedTotalPrice, lineComponentsSource, lineComponents
        },
    },
}
```

### After (Correct)

```go
// Delivery - CORRECT
"delivery": map[string]interface{}{
    "deliveryLines": []map[string]interface{}{
        {
            "destination": map[string]interface{}{
                "partialStreetAddress": map[string]interface{}{...},  // Correct
            },
            "selectedDeliveryStrategy": map[string]interface{}{
                "deliveryStrategyMatchingConditions": map[string]interface{}{
                    "estimatedTimeInTransit": map[string]interface{}{"any": true},
                    "shipments":              map[string]interface{}{"any": true},
                },
                "options": map[string]interface{}{},
            },
            "targetMerchandiseLines": map[string]interface{}{"any": true},
            "deliveryMethodTypes":    []string{"SHIPPING"},
            "expectedTotalPrice":     map[string]interface{}{"any": true},
            "destinationChanged":     true,
        },
    },
    "noDeliveryRequired":              []interface{}{},
    "useProgressiveRates":             false,
    "prefetchShippingRatesStrategy":   nil,
    "supportsSplitShipping":           true,
}

// Merchandise - CORRECT
"merchandise": map[string]interface{}{
    "merchandiseLines": []map[string]interface{}{
        {
            "stableId": stableID,
            "merchandise": map[string]interface{}{
                "productVariantReference": map[string]interface{}{
                    "id":                fmt.Sprintf("gid://shopify/ProductVariantMerchandise/%s", b.MerchandiseID),
                    "variantId":         fmt.Sprintf("gid://shopify/ProductVariant/%s", variantID),
                    "properties":        []interface{}{},
                    "sellingPlanId":     nil,
                    "sellingPlanDigest": nil,
                },
            },
            "quantity": {...},
            "expectedTotalPrice": map[string]interface{}{
                "value": map[string]interface{}{
                    "amount":       b.Subtotal,
                    "currencyCode": b.Currency,
                },
            },
            "lineComponentsSource": nil,
            "lineComponents":       []interface{}{},
        },
    },
}
```

## Testing

Build the updated code:
```bash
cd /home/runner/work/Shopi/Shopi/shopify-payment-api
go build -o shopify-api ./cmd/api
./shopify-api
```

Test with a Shopify store:
```bash
curl -X POST http://127.0.0.1:8080/shopify/check \
  -H "Content-Type: application/json" \
  -d '{
    "site_url": "https://example.myshopify.com",
    "card": {
      "number": "4111111111111111",
      "month": "12",
      "year": "2028",
      "cvv": "123"
    }
  }'
```

## Expected Results

With these fixes:
1. ✅ First proposal query should succeed without schema errors
2. ✅ Delivery strategy negotiation should work
3. ✅ Merchandise lines should be accepted
4. ✅ Submit mutation should succeed with proper delivery strategy
5. ✅ Complete checkout flow should work identically to Python implementation

## Key Learnings

1. **Shopify uses different structures for proposal vs submit**:
   - Proposal: `deliveryStrategyMatchingConditions` + `{any: true}`
   - Submit: `deliveryStrategyByHandle` + specific values

2. **Field names matter**:
   - `partialStreetAddress` vs `streetAddress`
   - `productVariantReference` wrapper required

3. **Required fields are not always obvious**:
   - `noDeliveryRequired: []` is required (empty array, not null)
   - `deliveryMethodTypes: ["SHIPPING"]` must be present
   - Many boolean flags need explicit values

4. **Match Python exactly**: The working Python implementation is the source of truth for the exact structure Shopify expects.
