# Session Token Extraction Fix for Shopify Frontend Changes

## Problem

Session token extraction was failing with error: `"failed to extract session token"`

This occurred because Shopify changed their frontend HTML structure, and the existing patterns in `ParseCheckoutPage` were no longer matching the new format.

## Root Cause

The original code only had 5 simple extraction patterns that looked for specific strings like:
- `name="serialized-sessionToken" content="&quot;...&quot;"`
- `"serializedSessionToken":"..."`
- `data-session-token="..."`
- `"sessionToken":"..."`

When Shopify updated their checkout page HTML structure, none of these patterns matched anymore.

## Solution

Enhanced the session token extraction with **9 comprehensive patterns** including:

1. **Meta tag with escaped quotes** (original)
2. **Meta tag without escaped quotes** (original)
3. **JSON with escaped quotes** (original)
4. **Data attribute** (original)
5. **Simple sessionToken in JSON** (original)
6. **Unescaped HTML extraction** (NEW) - Try patterns on HTML with entities decoded
7. **Regex-based JSON extraction** (NEW) - Match `sessionToken` in any JSON context with flexible quotes
8. **serializedSessionToken regex** (NEW) - More flexible matching for the serialized variant
9. **window.Shopify globals** (NEW) - Extract from JavaScript window objects

### Key Improvements

- **HTML entity unescaping**: Converts `&quot;` → `"`, `&amp;` → `&`, etc. before pattern matching
- **Regex-based extraction**: More flexible than simple string matching
- **Token length validation**: Only matches tokens with 50+ characters (typical session token length)
- **Multiple quote styles**: Handles both `"` and `'` quotes in JSON
- **Whitespace tolerance**: Regex patterns handle variable spacing

## Files Modified

- `/home/runner/work/Shopi/Shopi/shopify-payment-api/internal/parser/response.go`
  - Lines 263-326: Enhanced `ParseCheckoutPage` function with 9 extraction patterns

## Testing

After rebuilding:
```bash
cd ~/Shopi/shopify-payment-api  # or wherever your repo is
go build -o shopify-api ./cmd/api
./shopify-api
```

Test with:
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

**Expected**: Session token extraction succeeds, no more "failed to extract session token" errors.

## Why This Works

The enhanced extraction logic:
1. First tries all the original simple patterns (for backward compatibility)
2. Then uses HTML entity decoding to handle escaped content
3. Finally applies flexible regex patterns that can match various JSON/HTML formats
4. Has 9 fallback layers, so if Shopify changes format again, there's a higher chance one pattern will match

This makes the extraction much more robust against future Shopify frontend changes.

## Verification

Check logs for:
- ✅ `Session token from HTML: AAE...` (should show actual token, not empty)
- ✅ `Final session token: AAE...` (should show same token)
- ✅ No error: `failed to extract session token`
- ✅ GraphQL proposals execute successfully
