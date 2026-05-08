package models

import "time"

// PaymentStatus represents the classification of a payment attempt
type PaymentStatus string

const (
	// Success statuses
	StatusCharged            PaymentStatus = "CHARGED"
	StatusApproved           PaymentStatus = "APPROVED"

	// Decline statuses with card validation
	StatusDeclined           PaymentStatus = "DECLINED"
	StatusCVVMismatch        PaymentStatus = "CVV_MISMATCH"
	StatusInsufficientFunds  PaymentStatus = "INSUFFICIENT_FUNDS"
	StatusInvalidCard        PaymentStatus = "INVALID_CARD"
	StatusExpiredCard        PaymentStatus = "EXPIRED_CARD"

	// Gateway/Risk rejections
	StatusGatewayRejection   PaymentStatus = "GATEWAY_REJECTION"
	StatusRiskRejection      PaymentStatus = "RISK_REJECTION"
	StatusDuplicate          PaymentStatus = "DUPLICATE"
	Status3DSRequired        PaymentStatus = "3DS_REQUIRED"

	// Retryable failures
	StatusRetryable          PaymentStatus = "RETRYABLE"
	StatusTimeout            PaymentStatus = "TIMEOUT"
	StatusThrottled          PaymentStatus = "THROTTLED"

	// System failures
	StatusError              PaymentStatus = "ERROR"
	StatusUnknown            PaymentStatus = "UNKNOWN"
)

// PaymentRequest represents an incoming payment check request
type PaymentRequest struct {
	Card      CardData `json:"card" binding:"required"`
	SiteURL   string   `json:"site_url" binding:"required,url"`
	ProxyURL  string   `json:"proxy_url,omitempty"`
	RequestID string   `json:"request_id,omitempty"`
}

// CardData contains card information
type CardData struct {
	Number string `json:"number" binding:"required,len=13|len=16|len=19"`
	Month  string `json:"month" binding:"required,len=2"`
	Year   string `json:"year" binding:"required,len=4"`
	CVV    string `json:"cvv" binding:"required,len=3|len=4"`
}

// PaymentResponse represents the API response
type PaymentResponse struct {
	Status    PaymentStatus `json:"status"`
	Message   string        `json:"message"`
	Gateway   string        `json:"gateway,omitempty"`
	Amount    string        `json:"amount,omitempty"`
	Currency  string        `json:"currency,omitempty"`
	RequestID string        `json:"request_id,omitempty"`
	Duration  int64         `json:"duration_ms,omitempty"`
	Timestamp time.Time     `json:"timestamp"`
}

// CheckoutSession contains session data for checkout flow
type CheckoutSession struct {
	SessionToken     string
	CheckoutURL      string
	CheckpointData   string
	QueueToken       string
	VariantID        string
	TotalPrice       string
	Currency         string
	DeliveryStrategy string
	PaymentToken     string
}

// GraphQLResponse represents a parsed GraphQL response
type GraphQLResponse struct {
	Data   map[string]interface{} `json:"data"`
	Errors []GraphQLError         `json:"errors,omitempty"`
}

// GraphQLError represents a GraphQL error
type GraphQLError struct {
	Message    string                 `json:"message"`
	Extensions map[string]interface{} `json:"extensions,omitempty"`
	Path       []interface{}          `json:"path,omitempty"`
}

// ClassificationResult contains detailed classification information
type ClassificationResult struct {
	Status     PaymentStatus
	Confidence float64
	Reason     string
	Code       string
	Retryable  bool
	Details    map[string]interface{}
}
