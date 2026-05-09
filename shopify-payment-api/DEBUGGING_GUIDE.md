# Session Token Extraction Debugging Guide

## Problem
Session token extraction is failing despite implementing 9 different extraction patterns.

## New Diagnostic Features

I've added comprehensive debugging to help diagnose exactly why extraction is failing:

### 1. HTML Response Saving
The checkout page HTML is now saved to `/tmp/shopify_checkout_debug.html` every time a checkout is created.

### 2. Context Logging
If the keywords `sessionToken` or `serialized-sessionToken` are found in the HTML, the code will print:
- 200 characters before the keyword
- 300 characters after the keyword
- This shows the exact context and format

### 3. Detailed Error Messages
When all 9 patterns fail, you'll see:
```
[ERROR] All 9 session token extraction patterns failed!
[ERROR] HTML saved to: /tmp/shopify_checkout_debug.html
[ERROR] HTML length: XXXX bytes
[ERROR] Contains 'sessionToken': true/false
[ERROR] Contains 'serialized-sessionToken': true/false
```

### 4. Success Logging
When extraction succeeds:
```
[SUCCESS] Session token extracted: AAE...
```

## How to Use

### Step 1: Rebuild and Run
```bash
cd /home/runner/work/Shopi/Shopi/shopify-payment-api
go build -o shopify-api ./cmd/api
./shopify-api
```

### Step 2: Test with a Request
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

### Step 3: Check the Logs
Look at the server logs for:
- `[DEBUG] HTML snippet around 'sessionToken':` - Shows exact format
- `[ERROR] All 9 session token extraction patterns failed!` - Confirms failure
- `[SUCCESS] Session token extracted` - Shows success

### Step 4: Examine the Saved HTML
```bash
cat /tmp/shopify_checkout_debug.html | grep -A5 -B5 "sessionToken"
```

Or view the full file:
```bash
less /tmp/shopify_checkout_debug.html
```

## What to Look For

### Case 1: "sessionToken" keyword NOT found
If the logs show `Contains 'sessionToken': false`, then Shopify has completely changed how they deliver the token. Possible reasons:
- Token is now delivered via JavaScript only (client-side rendering)
- Token is in a different field name
- Token requires a different API endpoint or authentication

### Case 2: "sessionToken" keyword IS found
If the logs show the HTML snippet with context, you can see:
- The exact format Shopify is using
- Whether it's in a meta tag, JSON, or JavaScript
- How it's escaped or encoded

Then we can write a new extraction pattern to match that specific format.

## Next Steps

Once you run the test and have the debug output, share:
1. The `[DEBUG]` log snippets (if any)
2. The `[ERROR]` messages
3. OR a snippet from `/tmp/shopify_checkout_debug.html` around any "sessionToken" or "serialized" keywords

With that information, I can write the exact extraction pattern needed.

## Current Extraction Patterns

For reference, here are the 9 patterns currently implemented:

1. `name="serialized-sessionToken" content="&quot;XXX&quot;"`
2. `name="serialized-sessionToken" content="XXX"`
3. `"serializedSessionToken":"XXX"`
4. `data-session-token="XXX"`
5. `"sessionToken":"XXX"`
6. Unescaped HTML: `"sessionToken":"XXX"`
7. Regex: `["']sessionToken["']\s*:\s*["']([A-Za-z0-9_\-]{50,})["']`
8. Regex: `["']serializedSessionToken["']\s*:\s*["']([A-Za-z0-9_\-]{50,})["']`
9. Regex: `window\.Shopify[^{]*\{[^}]*sessionToken["']\s*:\s*["']([A-Za-z0-9_\-]{50,})["']`

If none of these match, we need to see the actual format to add pattern #10.
