# CRITICAL FIX: TAX_NEW_TAX_MUST_BE_ACCEPTED Error

## The Problem

The Go implementation was getting `TAX_NEW_TAX_MUST_BE_ACCEPTED` errors and treating them as fatal errors, stopping the payment flow.

**Error Message:**
```
"Proposal failed: failed to parse first proposal: negotiation failed:
TAX_NEW_TAX_MUST_BE_ACCEPTED - Taxes associated to your order have changed.
Review and try again."
```

## Root Cause Analysis

### What We Thought Was Wrong

Initially, we believed the issue was that we needed a complex two-step proposal flow with:
- First proposal to get shipping options
- Parse checkpoint/changeset tokens
- Build modified variables for second proposal
- Execute second proposal with updated fields

This led to implementing `BuildDeliveryProposalVariables()` with complex logic to update delivery strategies, tax amounts, etc.

### The Actual Problem

After analyzing the Python implementation more carefully, we found:

**Python Code (lines 509-514):**
```python
for i in range(2):
    response, resp_text, captcha_solved = await make_graphql_request_with_captcha_handling(
        session, graphql_url, params, headers, json_data, checkout_url, max_retries=1
    )
    if i == 0:
        await asyncio.sleep(3)
```

Python simply:
1. **Runs the proposal query TWICE** with the **SAME variables**
2. **Sleeps 3 seconds** after the first one
3. **Does NOT** check for TAX_NEW_TAX_MUST_BE_ACCEPTED
4. **Does NOT** modify variables between proposals
5. **Does NOT** check for checkpoint/changeset tokens

The TAX_NEW_TAX_MUST_BE_ACCEPTED error is **NOT a fatal error** - it's just Shopify's way of saying "run the proposal again to confirm the tax changes". Python handles this by simply running the proposal twice unconditionally.

## The Fix

### 1. Parser Fix (internal/parser/response.go:93-105)

**Changed from:**
```go
// Check for errors
if errors, ok := negotiate["errors"].([]interface{}); ok && len(errors) > 0 {
    if firstError, ok := errors[0].(map[string]interface{}); ok {
        code := GetString(firstError, "code")
        message := GetString(firstError, "localizedMessage")
        return nil, fmt.Errorf("negotiation failed: %s - %s", code, message)
    }
    return nil, fmt.Errorf("negotiation failed with unknown error")
}
```

**Changed to:**
```go
// Check for errors (but ignore TAX_NEW_TAX_MUST_BE_ACCEPTED as it's handled by two-proposal flow)
if errors, ok := negotiate["errors"].([]interface{}); ok && len(errors) > 0 {
    if firstError, ok := errors[0].(map[string]interface{}); ok {
        code := GetString(firstError, "code")
        message := GetString(firstError, "localizedMessage")
        // TAX_NEW_TAX_MUST_BE_ACCEPTED is not a fatal error - it signals need for second proposal
        // The two-step proposal flow handles this automatically
        if code != "TAX_NEW_TAX_MUST_BE_ACCEPTED" {
            return nil, fmt.Errorf("negotiation failed: %s - %s", code, message)
        }
        // For TAX_NEW_TAX_MUST_BE_ACCEPTED, continue parsing to extract tokens for second proposal
    }
}
```

**Why:** TAX_NEW_TAX_MUST_BE_ACCEPTED is not a fatal error. It's just a signal that needs to be ignored. The parser should continue and extract the proposal data.

### 2. Processor Simplification (internal/api/processor.go:498-560)

**Changed from:**
```go
// Execute first proposal
p.Logger.Info("Executing first proposal (shipping)...")
variables := builder.BuildProposalVariables(false)
resp, err := graphqlExecutor.Execute(...)

// Parse first proposal
proposalData, err := p.Parser.ParseProposalResponse(resp.Data)

// Check if checkpoint/changeset tokens exist
if proposalData.CheckpointData != "" || len(proposalData.ChangesetTokens) > 0 {
    p.Logger.Info("Executing second proposal for tax acceptance...")
    time.Sleep(3 * time.Second)

    // Update builder with tokens and data from first proposal
    builder.CheckpointData = proposalData.CheckpointData
    builder.QueueToken = proposalData.QueueToken
    builder.ChangesetTokens = proposalData.ChangesetTokens
    builder.DeliveryStrategy = proposalData.DeliveryStrategy
    builder.ShippingAmount = proposalData.ShippingAmount
    builder.TaxAmount = proposalData.TaxAmount

    // Build DIFFERENT variables for second proposal
    variables = builder.BuildDeliveryProposalVariables()

    // Execute second proposal with modified variables
    resp, err = graphqlExecutor.Execute(...)
    proposalData, err = p.Parser.ParseProposalResponse(resp.Data)
}
```

**Changed to:**
```go
// Build proposal variables once (same variables used for both proposals like Python)
variables := builder.BuildProposalVariables(false)

var proposalData *parser.ProposalData
var resp *models.GraphQLResponse

// Execute proposal twice (matches Python: for i in range(2))
// This handles TAX_NEW_TAX_MUST_BE_ACCEPTED by running proposal again with same variables
for i := 0; i < 2; i++ {
    proposalNum := i + 1
    p.Logger.Infof("Executing proposal %d/2...", proposalNum)

    var err error
    resp, err = graphqlExecutor.Execute(ctx, graphqlURL, graphql.QUERY_PROPOSAL, variables, headers)
    if err != nil {
        return nil, fmt.Errorf("proposal %d failed: %w", proposalNum, err)
    }

    // Parse proposal response
    proposalData, err = p.Parser.ParseProposalResponse(resp.Data)
    if err != nil {
        return nil, fmt.Errorf("failed to parse proposal %d: %w", proposalNum, err)
    }

    // Sleep 3 seconds after first proposal (matches Python: if i == 0: await asyncio.sleep(3))
    if i == 0 {
        time.Sleep(3 * time.Second)
    }
}

p.Logger.Info("Both proposals completed successfully")
```

**Why:**
- **Simpler**: No conditional logic, no token checking, no variable modification
- **Matches Python exactly**: Same loop structure, same sleep timing
- **Same variables**: Uses identical variables for both proposals
- **Unconditional**: Always runs twice, regardless of response

## Key Insights

### What TAX_NEW_TAX_MUST_BE_ACCEPTED Really Means

This error code is Shopify's way of implementing a two-phase commit for tax calculations:

1. **First Proposal**: Calculate taxes based on shipping address
2. **User Confirmation**: User needs to "review and try again" (accept tax changes)
3. **Second Proposal**: Confirm the tax calculations

Python handles this by **running the proposal twice with the same variables**. The second run serves as the "confirmation" that the user has reviewed and accepted the tax changes.

### Why The Complex Approach Failed

Our complex approach with `BuildDeliveryProposalVariables()` was:
- **Over-engineered**: Tried to modify too many fields
- **Unnecessary**: Python doesn't modify anything between proposals
- **Error-prone**: More code = more places for bugs
- **Wrong assumption**: Assumed we needed to update variables based on first response

The simple truth: **Just run it twice with the same input.**

## Comparison: Before vs After

| Aspect | Before (Complex) | After (Simple) |
|--------|------------------|----------------|
| **Lines of Code** | ~100 lines | ~40 lines |
| **Variable Building** | Build once, modify, build again | Build once, use twice |
| **Conditional Logic** | Check tokens, conditional execution | Unconditional loop |
| **Error Handling** | Fatal on TAX_NEW_TAX_MUST_BE_ACCEPTED | Ignore TAX_NEW_TAX_MUST_BE_ACCEPTED |
| **Python Match** | ❌ Different approach | ✅ Exact match |
| **Complexity** | High | Low |
| **Maintainability** | Difficult | Easy |

## Testing

To verify this fix works:

```bash
cd /opt/Shopi/shopify-payment-api
git pull
make build
./bin/shopify-api

# In another terminal:
curl -X POST http://localhost:8080/shopify/check \
  -H "Content-Type: application/json" \
  -d '{
    "card": {
      "number": "4242424242424242",
      "month": "12",
      "year": "2025",
      "cvv": "123"
    },
    "site_url": "https://yallsweettea.com/products/sweet-tea-12-pack"
  }'
```

**Expected behavior:**
- Proposal 1/2 executes
- 3 second sleep
- Proposal 2/2 executes
- Both complete successfully
- NO TAX_NEW_TAX_MUST_BE_ACCEPTED error
- Payment flow continues to card vaulting and submission

## Logs to Verify Fix

Look for these log messages:

```json
{"level":"info","msg":"Executing proposal 1/2..."}
{"level":"info","msg":"Proposal 1 completed. CheckpointData: true, QueueToken: true, ChangesetTokens: 2"}
[3 second pause]
{"level":"info","msg":"Executing proposal 2/2..."}
{"level":"info","msg":"Proposal 2 completed. CheckpointData: true, QueueToken: true, ChangesetTokens: 2"}
{"level":"info","msg":"Both proposals completed successfully"}
```

If you see these logs without errors, the fix is working correctly.

## Summary

**The mistake:** We were treating TAX_NEW_TAX_MUST_BE_ACCEPTED as a fatal error and trying to build complex logic to handle it.

**The solution:**
1. Ignore TAX_NEW_TAX_MUST_BE_ACCEPTED in the parser (it's not fatal)
2. Run the proposal twice with the same variables (unconditionally)
3. Sleep 3 seconds between them
4. Match Python's simple `for i in range(2)` loop exactly

**Result:** Simple, maintainable code that exactly matches the working Python implementation.

---

**Commit:** e608cf2
**Date:** 2026-05-11
**Files Changed:**
- `internal/parser/response.go` - Ignore TAX_NEW_TAX_MUST_BE_ACCEPTED
- `internal/api/processor.go` - Simplified two-proposal loop
