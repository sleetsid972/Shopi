package parser

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/sirupsen/logrus"
)

// Parser parses Shopify responses
type Parser struct {
	logger *logrus.Logger
}

// NewParser creates a new parser
func NewParser(logger *logrus.Logger) *Parser {
	return &Parser{
		logger: logger,
	}
}

// ProposalData contains parsed proposal response data
type ProposalData struct {
	DeliveryStrategy string
	ShippingAmount   float64
	TaxAmount        float64
	TotalAmount      float64
	PaymentID        string
	Gateway          string
	Currency         string
	StableID         string
}

// SubmitData contains parsed submit response data
type SubmitData struct {
	ResultType string
	Success    bool
	ErrorCode  string
	Message    string
	Token      string
	OrderID    string
	PollURL    string
	PollDelay  int
}

// CheckoutData contains parsed checkout page data
type CheckoutData struct {
	SessionToken   string
	QueueToken     string
	StableID       string
	MerchandiseID  string
	Currency       string
	Subtotal       string
	BuildID        string
	SourceToken    string
	IdentSignature string
}

// ParseProposalResponse parses the proposal query response
func (p *Parser) ParseProposalResponse(data map[string]interface{}) (*ProposalData, error) {
	result := &ProposalData{
		Currency: "USD",
	}

	// Navigate to proposal data
	proposalInterface, ok := data["proposal"]
	if !ok {
		return nil, fmt.Errorf("no proposal field in response")
	}

	proposal, ok := proposalInterface.(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid proposal format")
	}

	// Check for ProposalFailure
	if errors, ok := proposal["proposalErrors"].([]interface{}); ok && len(errors) > 0 {
		if firstError, ok := errors[0].(map[string]interface{}); ok {
			code := GetString(firstError, "code")
			message := GetString(firstError, "localizedMessage")
			return nil, fmt.Errorf("proposal failed: %s - %s", code, message)
		}
		return nil, fmt.Errorf("proposal failed with unknown error")
	}

	// Extract proposal data
	proposalData, ok := proposal["proposal"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("no proposal data found")
	}

	// Extract running total
	if runningTotal, ok := proposalData["runningTotal"].(map[string]interface{}); ok {
		if value, ok := runningTotal["value"].(map[string]interface{}); ok {
			result.TotalAmount = GetFloat(value, "amount")
			if currency := GetString(value, "currencyCode"); currency != "" {
				result.Currency = currency
			}
		}
	}

	// Extract delivery information
	if delivery, ok := proposalData["delivery"].(map[string]interface{}); ok {
		if deliveryLines, ok := delivery["deliveryLines"].([]interface{}); ok && len(deliveryLines) > 0 {
			if line, ok := deliveryLines[0].(map[string]interface{}); ok {
				// Extract delivery strategies
				if strategies, ok := line["availableDeliveryStrategies"].([]interface{}); ok && len(strategies) > 0 {
					if strategy, ok := strategies[0].(map[string]interface{}); ok {
						result.DeliveryStrategy = GetString(strategy, "handle")
						if price, ok := strategy["price"].(map[string]interface{}); ok {
							if value, ok := price["value"].(map[string]interface{}); ok {
								result.ShippingAmount = GetFloat(value, "amount")
							}
						}
					}
				}

				// Extract merchandise info
				if target, ok := line["targetMerchandise"].(map[string]interface{}); ok {
					id := GetString(target, "id")
					if id != "" {
						// Extract ID from gid://shopify/ProductVariantSnapshot/...
						parts := strings.Split(id, "/")
						if len(parts) > 0 {
							result.StableID = parts[len(parts)-1]
						}
					}
				}
			}
		}
	}

	// Extract payment information
	if payment, ok := proposalData["payment"].(map[string]interface{}); ok {
		if paymentLines, ok := payment["availablePaymentLines"].([]interface{}); ok && len(paymentLines) > 0 {
			if line, ok := paymentLines[0].(map[string]interface{}); ok {
				if method, ok := line["paymentMethod"].(map[string]interface{}); ok {
					result.PaymentID = GetString(method, "paymentMethodIdentifier")
					result.Gateway = GetString(method, "extensibilityDisplayName")
					if result.Gateway == "" {
						result.Gateway = GetString(method, "name")
					}
				}
			}
		}
	}

	// Extract tax information
	if tax, ok := proposalData["tax"].(map[string]interface{}); ok {
		if totalTax, ok := tax["totalTaxAmount"].(map[string]interface{}); ok {
			if value, ok := totalTax["value"].(map[string]interface{}); ok {
				result.TaxAmount = GetFloat(value, "amount")
			}
		}
	}

	return result, nil
}

// ParseSubmitResponse parses the submit mutation response
func (p *Parser) ParseSubmitResponse(data map[string]interface{}) (*SubmitData, error) {
	result := &SubmitData{}

	// Navigate to submit data
	submitInterface, ok := data["submit"]
	if !ok {
		return nil, fmt.Errorf("no submit field in response")
	}

	submit, ok := submitInterface.(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid submit format")
	}

	// Check result type
	if typename, ok := submit["__typename"].(string); ok {
		result.ResultType = typename
	}

	// Handle different result types
	switch result.ResultType {
	case "SubmitSuccess":
		result.Success = true
		if resultData, ok := submit["result"].(map[string]interface{}); ok {
			result.Token = GetString(resultData, "token")
			result.OrderID = GetString(resultData, "orderId")
			result.Message = "Payment successful"
		}

	case "SubmitPending":
		result.Success = false
		result.PollURL = GetString(submit, "pollUrl")
		result.PollDelay = GetInt(submit, "pollDelay")
		result.Message = "Payment pending"

	case "SubmitRejected", "SubmitFailed":
		result.Success = false
		if errors, ok := submit["errors"].([]interface{}); ok && len(errors) > 0 {
			if firstError, ok := errors[0].(map[string]interface{}); ok {
				result.ErrorCode = GetString(firstError, "code")
				result.Message = GetString(firstError, "localizedMessage")
				if result.Message == "" {
					result.Message = GetString(firstError, "nonLocalizedMessage")
				}
			}
		}
		if result.Message == "" {
			result.Message = "Payment rejected"
		}

	case "SubmitAlreadyAccepted":
		result.Success = true
		if receipt, ok := submit["receipt"].(map[string]interface{}); ok {
			result.Token = GetString(receipt, "token")
		}
		result.Message = "Payment already accepted"

	case "SubmitThrottled":
		result.Success = false
		result.Message = "Throttled - too many requests"

	default:
		return nil, fmt.Errorf("unknown submit result type: %s", result.ResultType)
	}

	return result, nil
}

// ParseCheckoutPage parses the checkout HTML page
func (p *Parser) ParseCheckoutPage(html string) (*CheckoutData, error) {
	result := &CheckoutData{
		Currency: "USD",
	}

	// Extract session token (multiple patterns)
	result.SessionToken = ExtractBetween(html, "name=\"serialized-sessionToken\" content=\"&quot;", "&quot;")
	if result.SessionToken == "" {
		result.SessionToken = ExtractBetween(html, "name=\"serialized-sessionToken\" content=\"", "\"")
	}
	if result.SessionToken == "" {
		result.SessionToken = ExtractBetween(html, "\"serializedSessionToken\":\"", "\"")
	}
	if result.SessionToken == "" {
		result.SessionToken = ExtractBetween(html, "data-session-token=\"", "\"")
	}
	if result.SessionToken == "" {
		result.SessionToken = ExtractBetween(html, "\"sessionToken\":\"", "\"")
	}

	// Extract queue token
	result.QueueToken = ExtractBetween(html, "queueToken&quot;:&quot;", "&quot;")
	if result.QueueToken == "" {
		result.QueueToken = ExtractBetween(html, "\"queueToken\":\"", "\"")
	}

	// Extract stable ID
	result.StableID = ExtractBetween(html, "stableId&quot;:&quot;", "&quot;")
	if result.StableID == "" {
		result.StableID = ExtractBetween(html, "\"stableId\":\"", "\"")
	}

	// Extract merchandise ID
	result.MerchandiseID = ExtractBetween(html, "ProductVariantMerchandise/", "&quot;")
	if result.MerchandiseID == "" {
		result.MerchandiseID = ExtractBetween(html, "ProductVariantMerchandise/", "&q")
	}
	if result.MerchandiseID == "" {
		result.MerchandiseID = ExtractBetween(html, "\"merchandiseId\":\"gid://shopify/ProductVariantMerchandise/", "\"")
	}

	// Extract currency
	if strings.Contains(html, "currencyCode&quot;:&quot;") {
		result.Currency = ExtractBetween(html, "currencyCode&quot;:&quot;", "&quot;")
	} else if strings.Contains(html, "\"currencyCode\":\"") {
		result.Currency = ExtractBetween(html, "\"currencyCode\":\"", "\"")
	}
	if result.Currency == "" {
		result.Currency = "USD"
	}

	// Extract subtotal
	result.Subtotal = ExtractBetween(html, "subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;", "&quot;")
	if result.Subtotal == "" {
		result.Subtotal = ExtractBetween(html, "\"subtotalBeforeTaxesAndShipping\":{\"value\":{\"amount\":\"", "\"")
	}
	if result.Subtotal == "" {
		// Try to extract from price field
		re := regexp.MustCompile(`"price":\s*"([\d.]+)"`)
		if match := re.FindStringSubmatch(html); len(match) > 1 {
			result.Subtotal = match[1]
		}
	}
	if result.Subtotal == "" {
		result.Subtotal = "0.01"
	}

	// Extract build ID (commitSha)
	unescaped := strings.ReplaceAll(html, "&quot;", "\"")
	unescaped = strings.ReplaceAll(unescaped, "&amp;", "&")
	unescaped = strings.ReplaceAll(unescaped, "&#39;", "'")

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

	return result, nil
}

// ExtractBetween extracts text between start and end markers
func ExtractBetween(text, start, end string) string {
	startIdx := strings.Index(text, start)
	if startIdx == -1 {
		return ""
	}
	startIdx += len(start)

	endIdx := strings.Index(text[startIdx:], end)
	if endIdx == -1 {
		return ""
	}

	return text[startIdx : startIdx+endIdx]
}

// GetString safely extracts a string from a map
func GetString(data map[string]interface{}, key string) string {
	if val, ok := data[key]; ok {
		if str, ok := val.(string); ok {
			return str
		}
	}
	return ""
}

// GetFloat safely extracts a float from a map
func GetFloat(data map[string]interface{}, key string) float64 {
	if val, ok := data[key]; ok {
		switch v := val.(type) {
		case float64:
			return v
		case string:
			if f, err := strconv.ParseFloat(v, 64); err == nil {
				return f
			}
		case int:
			return float64(v)
		}
	}
	return 0.0
}

// GetInt safely extracts an int from a map
func GetInt(data map[string]interface{}, key string) int {
	if val, ok := data[key]; ok {
		switch v := val.(type) {
		case int:
			return v
		case float64:
			return int(v)
		case string:
			if i, err := strconv.Atoi(v); err == nil {
				return i
			}
		}
	}
	return 0
}
