# Python Implementation Matching - Complete Guide

## Overview

This document describes all changes made to match the working Python implementation (`Autoshopify (1) (4).py`) that successfully handles modern Shopify checkout flows.

## Problem Analysis

The Python Flask API successfully extracts session tokens and processes payments, while the Go implementation was failing with "session token not fetch" errors. The root causes were:

1. **Missing SSL Configuration**: Go was verifying certificates, Python used `ssl=False`
2. **Missing Modern Headers**: Critical Shopify headers were not being sent
3. **Incomplete Token Extraction**: While Go had extraction patterns, headers weren't properly utilized

## Changes Made

### 1. HTTP Client Configuration (`internal/network/client.go`)

**Changed SSL verification to match Python:**

```go
TLSClientConfig: &tls.Config{
    MinVersion:         tls.VersionTLS12,
    InsecureSkipVerify: true, // Match Python's ssl=False
}
```

This matches Python's:
```python
connector = aiohttp.TCPConnector(ssl=False)
```

### 2. Session Token Extraction (`internal/parser/response.go`)

**Already implemented - 9 fallback patterns matching Python's 6:**

Python patterns (from lines 336-349):
1. Header: `X-Checkout-One-Session-Token`
2. `name="serialized-sessionToken" content="&quot;...&quot;`
3. `name="serialized-sessionToken" content="..."`
4. `"serializedSessionToken":"..."`
5. `data-session-token="..."`
6. `"sessionToken":"..."`

Go implementation includes all of these PLUS:
7. Unescaped HTML extraction
8. Regex-based JSON extraction
9. Window.Shopify global extraction

### 3. Build and Source Token Extraction (`internal/parser/response.go`)

**Already implemented - matching Python lines 374-389:**

```go
// Extract build ID (commitSha)
re := regexp.MustCompile(`"commitSha"\s*:\s*"([a-f0-9]{40})"`)
if match := re.FindStringSubmatch(unescaped); len(match) > 1 {
    result.BuildID = match[1]
}

// Extract source token
result.SourceToken = ExtractBetween(html, "name=\"serialized-sourceToken\" content=\"", "\"")
result.SourceToken = strings.ReplaceAll(result.SourceToken, "&quot;", "")
result.SourceToken = strings.Trim(result.SourceToken, "\"")

// Extract identification signature
re = regexp.MustCompile(`checkoutCardsinkCallerIdentificationSignature":"([^"]+)"`)
if match := re.FindStringSubmatch(unescaped); len(match) > 1 {
    result.IdentSignature = match[1]
}
```

### 4. Modern Shopify Headers (`internal/api/processor.go`)

**Added all Python headers (lines 394-409) to both `executeProposals` and `submitPayment`:**

```go
headers := map[string]string{
    "Accept":                       "application/json",
    "Content-Type":                 "application/json",
    "X-Shopify-Checkout-Version":   "1",
    "X-Checkout-One-Session-Token": checkoutData.SessionToken,
    "shopify-checkout-client":      "checkout-web/1.0",
    "shopify-checkout-source":      fmt.Sprintf(`id="%s", type="cn"`, checkoutData.AttemptToken),
    "sec-fetch-dest":               "empty",
    "sec-fetch-mode":               "cors",
    "sec-fetch-site":               "same-origin",
}

// Add build-related headers if available (Python lines 403-407)
if checkoutData.BuildID != "" {
    headers["x-checkout-web-build-id"] = checkoutData.BuildID
    headers["x-checkout-web-deploy-stage"] = "production"
    headers["x-checkout-web-server-handling"] = "fast"
    headers["x-checkout-web-server-rendering"] = "yes"
}

// Add source token header if available (Python lines 408-409)
if checkoutData.SourceToken != "" {
    headers["x-checkout-web-source-id"] = checkoutData.SourceToken
}
```

### 5. Attempt Token Extraction (`internal/api/processor.go`)

**Already implemented - matching Python lines 333-334:**

```go
// Extract attempt token from URL
attemptToken := ""
urlPath := resp.Request.URL.Path
re := regexp.MustCompile(`/checkouts/cn/([^/?]+)`)
matches := re.FindStringSubmatch(urlPath)
if len(matches) > 1 {
    attemptToken = matches[1]
} else {
    // Fallback: extract from URL segments
    segments := strings.Split(strings.Trim(urlPath, "/"), "/")
    if len(segments) > 0 {
        lastSegment := segments[len(segments)-1]
        attemptToken = strings.Split(lastSegment, "?")[0]
    }
}
```

### 6. Cookie Jar and Redirect Handling (`internal/network/client.go`)

**Already implemented:**

```go
// Create cookie jar for maintaining cookies across requests
jar, err := cookiejar.New(nil)
if err != nil {
    return nil, err
}

client := &http.Client{
    Transport: transport,
    Timeout:   45 * time.Second,
    Jar:       jar, // Cookie jar for automatic cookie handling
    CheckRedirect: func(req *http.Request, via []*http.Request) error {
        // Allow up to 10 redirects (Shopify can have multiple)
        if len(via) >= 10 {
            return http.ErrUseLastResponse
        }
        return nil
    },
}
```

This matches Python's session behavior:
```python
async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
    response = await session.post(url=checkout, allow_redirects=True, ...)
```

### 7. GraphQL Endpoint (`internal/api/processor.go`)

**Already using correct endpoint:**

```go
graphqlURL := fmt.Sprintf("%s/checkouts/unstable/graphql", task.SiteURL)
```

Matches Python line 507:
```python
graphql_url = f'https://{urlparse(ourl).netloc}/checkouts/unstable/graphql'
```

### 8. PCI Vault Headers (`internal/api/processor.go`)

**Already implemented - matching Python vault request:**

```go
req.Header.Set("Content-Type", "application/json")
req.Header.Set("Accept", "application/json")
req.Header.Set("Origin", "https://checkout.pci.shopifyinc.com")
if checkoutData.IdentSignature != "" {
    req.Header.Set("shopify-identification-signature", checkoutData.IdentSignature)
}
```

## Verification Checklist

### ✅ Already Implemented
- [x] Cookie jar for session persistence
- [x] Redirect following (up to 10 redirects)
- [x] Session token extraction (6+ patterns)
- [x] Header-based token extraction (`X-Checkout-One-Session-Token`)
- [x] Attempt token regex extraction from URL
- [x] Build ID extraction (`commitSha`)
- [x] Source token extraction
- [x] Identification signature extraction
- [x] Correct GraphQL endpoint (`/checkouts/unstable/graphql`)
- [x] PCI vault identification signature header

### ✅ Newly Added
- [x] SSL verification disabled (`InsecureSkipVerify: true`)
- [x] `shopify-checkout-client: checkout-web/1.0`
- [x] `shopify-checkout-source: id="attempt_token", type="cn"`
- [x] Build-related headers (build-id, deploy-stage, etc.)
- [x] Source token header (x-checkout-web-source-id)
- [x] sec-fetch headers (dest, mode, site)

## Testing

To test the implementation:

```bash
cd /home/runner/work/Shopi/Shopi/shopify-payment-api
go build -o shopify-api ./cmd/api
./shopify-api
```

Test with a known working Shopify store:

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

## Key Differences from Previous Implementation

### Before
- SSL verification enabled (caused issues with some stores)
- Missing modern Shopify headers
- Headers not matching browser/Python behavior

### After
- SSL verification disabled (matches Python)
- All modern Shopify headers included
- Headers exactly match working Python implementation
- Both proposal and submit requests use identical header sets

## Implementation Notes

1. **Single HTTP Client**: The Go implementation reuses a single HTTP client with cookie jar throughout the entire flow (add to cart → checkout → GraphQL → poll), matching Python's session behavior.

2. **Header Consistency**: All GraphQL requests (both proposal and submit) now include the same comprehensive header set that the Python implementation uses.

3. **Fallback Patterns**: The Go implementation actually has MORE fallback patterns for token extraction than Python, increasing reliability.

4. **Debug Logging**: The Go implementation includes extensive debug logging that saves HTML to `/tmp/shopify_checkout_debug.html` for troubleshooting.

## Expected Behavior

With these changes, the Go implementation should:
1. Successfully extract session tokens from modern Shopify stores
2. Pass GraphQL authentication checks
3. Complete the entire checkout flow without "session token not fetch" errors
4. Match the success rate of the working Python implementation

## Troubleshooting

If session token extraction still fails:

1. Check `/tmp/shopify_checkout_debug.html` to see the actual HTML received
2. Review logs for `[DEBUG] HTML snippet around 'sessionToken'` messages
3. Verify the `X-Checkout-One-Session-Token` header is present in responses
4. Confirm all headers are being sent (check with network debugging)

The extensive logging should reveal exactly what format Shopify is using for any store that still fails.
