package graphql

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/sirupsen/logrus"
	"github.com/sleetsid972/shopify-payment-api/internal/models"
	"github.com/sleetsid972/shopify-payment-api/internal/network"
)

// Executor executes GraphQL queries
type Executor struct {
	client *network.Client
	logger *logrus.Logger
}

// NewExecutor creates a new GraphQL executor
func NewExecutor(client *network.Client, logger *logrus.Logger) *Executor {
	return &Executor{
		client: client,
		logger: logger,
	}
}

// Execute executes a GraphQL query/mutation
func (e *Executor) Execute(ctx context.Context, url string, query string, variables map[string]interface{}, headers map[string]string) (*models.GraphQLResponse, error) {
	// Build request body
	requestBody := map[string]interface{}{
		"query":     query,
		"variables": variables,
	}

	bodyBytes, err := json.Marshal(requestBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	// Create request
	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(bodyBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	// Set headers
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	for key, value := range headers {
		req.Header.Set(key, value)
	}

	// Execute request
	resp, err := e.client.Do(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	// Read response
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	// Check status code
	if resp.StatusCode != http.StatusOK {
		e.logger.Warnf("GraphQL request returned status %d: %s", resp.StatusCode, string(respBody))
		return nil, fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	// Parse response
	var graphqlResp models.GraphQLResponse
	if err := json.Unmarshal(respBody, &graphqlResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	// Check for GraphQL errors
	if len(graphqlResp.Errors) > 0 {
		e.logger.Warnf("GraphQL returned errors: %+v", graphqlResp.Errors)
	}

	return &graphqlResp, nil
}

// ExecuteWithRetry executes a GraphQL query with retry logic
func (e *Executor) ExecuteWithRetry(ctx context.Context, url string, query string, variables map[string]interface{}, headers map[string]string, maxRetries int) (*models.GraphQLResponse, error) {
	var lastErr error

	for attempt := 0; attempt < maxRetries; attempt++ {
		resp, err := e.Execute(ctx, url, query, variables, headers)
		if err == nil {
			return resp, nil
		}

		lastErr = err
		e.logger.Warnf("GraphQL request attempt %d/%d failed: %v", attempt+1, maxRetries, err)

		// Don't retry on context cancellation
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}

	return nil, fmt.Errorf("all retry attempts failed: %w", lastErr)
}
