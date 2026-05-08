package api

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/sirupsen/logrus"
	"github.com/sleetsid972/shopify-payment-api/internal/models"
	"github.com/sleetsid972/shopify-payment-api/internal/workers"
)

// Server represents the HTTP API server
type Server struct {
	Pool      *workers.Pool
	Processor *PaymentProcessor
	Logger    *logrus.Logger
	StartTime time.Time
}

// NewServer creates a new API server instance
func NewServer(pool *workers.Pool, processor *PaymentProcessor, logger *logrus.Logger) *Server {
	return &Server{
		Pool:      pool,
		Processor: processor,
		Logger:    logger,
		StartTime: time.Now(),
	}
}

// HandlePaymentCheck handles payment check requests
func (s *Server) HandlePaymentCheck(c *gin.Context) {
	startTime := time.Now()

	// Parse request
	var req models.PaymentRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		s.Logger.Warnf("Invalid request: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{
			"status":  false,
			"message": "Invalid request format",
			"error":   err.Error(),
		})
		return
	}

	// Generate request ID if not provided
	if req.RequestID == "" {
		req.RequestID = generateRequestID()
	}

	s.Logger.Infof("Processing payment check: request_id=%s site=%s", req.RequestID, req.SiteURL)

	// Create task
	task := &workers.Task{
		ID:        req.RequestID,
		Card:      req.Card,
		SiteURL:   req.SiteURL,
		ProxyURL:  req.ProxyURL,
		Timestamp: time.Now(),
	}

	// Submit to worker pool
	ctx, cancel := context.WithTimeout(c.Request.Context(), 60*time.Second)
	defer cancel()

	result, err := s.Processor.Process(ctx, task)
	if err != nil {
		s.Logger.Errorf("Processing failed: request_id=%s error=%v", req.RequestID, err)
		c.JSON(http.StatusOK, gin.H{
			"status":      false,
			"message":     err.Error(),
			"request_id":  req.RequestID,
			"duration_ms": time.Since(startTime).Milliseconds(),
			"timestamp":   time.Now(),
		})
		return
	}

	// Calculate duration
	result.Duration = time.Since(startTime).Milliseconds()
	result.RequestID = req.RequestID
	result.Timestamp = time.Now()

	s.Logger.Infof("Processing completed: request_id=%s status=%s duration=%dms",
		req.RequestID, result.Status, result.Duration)

	// Return response
	c.JSON(http.StatusOK, gin.H{
		"status":      isApprovalStatus(result.Status),
		"message":     result.Message,
		"gateway":     result.Gateway,
		"amount":      result.Amount,
		"currency":    result.Currency,
		"request_id":  result.RequestID,
		"duration_ms": result.Duration,
		"timestamp":   result.Timestamp,
	})
}

// HandleHealth handles health check requests
func (s *Server) HandleHealth(c *gin.Context) {
	uptime := time.Since(s.StartTime)

	c.JSON(http.StatusOK, gin.H{
		"status":  "healthy",
		"uptime":  uptime.String(),
		"workers": s.Pool.Workers(),
		"timestamp": time.Now(),
	})
}

// HandleMetrics handles metrics requests
func (s *Server) HandleMetrics(c *gin.Context) {
	// TODO: Implement Prometheus metrics
	c.JSON(http.StatusOK, gin.H{
		"workers_active": s.Pool.Workers(),
		"uptime_seconds": time.Since(s.StartTime).Seconds(),
	})
}

// isApprovalStatus determines if a status is considered an approval
func isApprovalStatus(status models.PaymentStatus) bool {
	approvalStatuses := []models.PaymentStatus{
		models.StatusCharged,
		models.StatusApproved,
		models.StatusInsufficientFunds,
		models.StatusCVVMismatch,
		models.StatusInvalidCard,
		models.StatusExpiredCard,
	}

	for _, s := range approvalStatuses {
		if status == s {
			return true
		}
	}
	return false
}

// generateRequestID generates a unique request ID
func generateRequestID() string {
	return time.Now().Format("20060102150405") + "-" + randomString(8)
}

// randomString generates a random string of specified length
func randomString(length int) string {
	const charset = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, length)
	for i := range b {
		b[i] = charset[time.Now().UnixNano()%int64(len(charset))]
	}
	return string(b)
}
