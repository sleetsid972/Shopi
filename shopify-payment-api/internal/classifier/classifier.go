package classifier

import (
	"strings"

	"github.com/sleetsid972/shopify-payment-api/internal/models"
)

// Classifier handles payment response classification
type Classifier struct {
	rules []ClassificationRule
}

// ClassificationRule defines a classification rule
type ClassificationRule struct {
	Name       string
	Priority   int
	Matcher    func(data *ResponseData) bool
	Status     models.PaymentStatus
	Confidence float64
	Retryable  bool
}

// ResponseData contains parsed response information
type ResponseData struct {
	ResultType       string
	ErrorCode        string
	ErrorMessage     string
	LocalizedMessage string
	RawResponse      string
	StatusCode       int
	Headers          map[string]string
}

// NewClassifier creates a new payment classifier
func NewClassifier() *Classifier {
	c := &Classifier{}
	c.initializeRules()
	return c
}

// Classify determines the payment status from response data
func (c *Classifier) Classify(data *ResponseData) *models.ClassificationResult {
	// Multi-layer detection with priority ordering
	for _, rule := range c.rules {
		if rule.Matcher(data) {
			return &models.ClassificationResult{
				Status:     rule.Status,
				Confidence: rule.Confidence,
				Reason:     rule.Name,
				Code:       data.ErrorCode,
				Retryable:  rule.Retryable,
				Details: map[string]interface{}{
					"result_type": data.ResultType,
					"error_code":  data.ErrorCode,
				},
			}
		}
	}

	// Fallback to unknown
	return &models.ClassificationResult{
		Status:     models.StatusUnknown,
		Confidence: 0.0,
		Reason:     "No matching classification rule",
		Code:       data.ErrorCode,
		Retryable:  false,
	}
}

// initializeRules sets up classification rules in priority order
func (c *Classifier) initializeRules() {
	c.rules = []ClassificationRule{
		// ============ SUCCESS CASES ============
		{
			Name:       "SubmitSuccess",
			Priority:   1,
			Status:     models.StatusCharged,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				return data.ResultType == "SubmitSuccess"
			},
		},
		{
			Name:       "OrderPlaced",
			Priority:   2,
			Status:     models.StatusCharged,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.RawResponse)
				return strings.Contains(upper, "ORDER_PLACED") ||
					strings.Contains(upper, "PROCESSED_RECEIPT")
			},
		},

		// ============ APPROVED (Valid Card, Declined for Other Reasons) ============
		{
			Name:       "InsufficientFunds",
			Priority:   3,
			Status:     models.StatusInsufficientFunds,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.ErrorCode)
				return strings.Contains(upper, "INSUFFICIENT_FUNDS")
			},
		},
		{
			Name:       "CVVMismatch",
			Priority:   4,
			Status:     models.StatusCVVMismatch,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.ErrorCode)
				return strings.Contains(upper, "INCORRECT_CVC") ||
					strings.Contains(upper, "INCORRECT_CVV") ||
					strings.Contains(upper, "INVALID_CVC") ||
					strings.Contains(upper, "INVALID_CVV")
			},
		},
		{
			Name:       "InvalidCardNumber",
			Priority:   5,
			Status:     models.StatusInvalidCard,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.ErrorCode)
				return strings.Contains(upper, "INCORRECT_NUMBER") ||
					strings.Contains(upper, "INVALID_NUMBER")
			},
		},
		{
			Name:       "ExpiredCard",
			Priority:   6,
			Status:     models.StatusExpiredCard,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.ErrorCode)
				return strings.Contains(upper, "EXPIRED") ||
					strings.Contains(upper, "INVALID_EXPIRY")
			},
		},
		{
			Name:       "CardDeclined",
			Priority:   7,
			Status:     models.StatusApproved,
			Confidence: 0.9,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.ErrorCode)
				return strings.Contains(upper, "CARD_DECLINED")
			},
		},

		// ============ GATEWAY/RISK REJECTIONS ============
		{
			Name:       "3DSRequired",
			Priority:   8,
			Status:     models.Status3DSRequired,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.RawResponse)
				return strings.Contains(upper, "3D_SECURE") ||
					strings.Contains(upper, "AUTHENTICATION_REQUIRED")
			},
		},
		{
			Name:       "DuplicateTransaction",
			Priority:   9,
			Status:     models.StatusDuplicate,
			Confidence: 1.0,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.ErrorCode)
				return strings.Contains(upper, "DUPLICATE")
			},
		},
		{
			Name:       "RiskRejection",
			Priority:   10,
			Status:     models.StatusRiskRejection,
			Confidence: 0.9,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.RawResponse)
				return strings.Contains(upper, "RISK") ||
					strings.Contains(upper, "FRAUD")
			},
		},

		// ============ RETRYABLE FAILURES ============
		{
			Name:       "Throttled",
			Priority:   11,
			Status:     models.StatusThrottled,
			Confidence: 1.0,
			Retryable:  true,
			Matcher: func(data *ResponseData) bool {
				return data.ResultType == "Throttled" ||
					data.StatusCode == 429 ||
					strings.Contains(strings.ToUpper(data.RawResponse), "RATE_LIMIT")
			},
		},
		{
			Name:       "Timeout",
			Priority:   12,
			Status:     models.StatusTimeout,
			Confidence: 1.0,
			Retryable:  true,
			Matcher: func(data *ResponseData) bool {
				return data.StatusCode == 504 ||
					data.StatusCode == 408 ||
					strings.Contains(strings.ToUpper(data.ErrorMessage), "TIMEOUT")
			},
		},
		{
			Name:       "ServerError",
			Priority:   13,
			Status:     models.StatusRetryable,
			Confidence: 0.8,
			Retryable:  true,
			Matcher: func(data *ResponseData) bool {
				return data.StatusCode >= 500 && data.StatusCode < 600
			},
		},

		// ============ GENERIC DECLINES ============
		{
			Name:       "GenericError",
			Priority:   14,
			Status:     models.StatusDeclined,
			Confidence: 0.7,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				upper := strings.ToUpper(data.ErrorCode)
				return strings.Contains(upper, "GENERIC_ERROR") ||
					strings.Contains(upper, "PAYMENT_FAILED")
			},
		},
		{
			Name:       "SubmitRejected",
			Priority:   15,
			Status:     models.StatusDeclined,
			Confidence: 0.6,
			Retryable:  false,
			Matcher: func(data *ResponseData) bool {
				return data.ResultType == "SubmitRejected"
			},
		},
	}
}
