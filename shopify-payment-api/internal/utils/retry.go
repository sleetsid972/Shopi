package utils

import (
	"context"
	"fmt"
	"time"
)

// RetryConfig defines retry behavior
type RetryConfig struct {
	MaxAttempts  int
	InitialDelay time.Duration
	MaxDelay     time.Duration
	Multiplier   float64
}

// DefaultRetryConfig returns a default retry configuration
func DefaultRetryConfig() *RetryConfig {
	return &RetryConfig{
		MaxAttempts:  3,
		InitialDelay: 1 * time.Second,
		MaxDelay:     10 * time.Second,
		Multiplier:   2.0,
	}
}

// Retry executes a function with exponential backoff retry
func Retry(ctx context.Context, config *RetryConfig, fn func() error) error {
	if config == nil {
		config = DefaultRetryConfig()
	}

	var lastErr error
	delay := config.InitialDelay

	for attempt := 0; attempt < config.MaxAttempts; attempt++ {
		// Try the function
		err := fn()
		if err == nil {
			return nil
		}

		lastErr = err

		// Check if context is cancelled
		if ctx.Err() != nil {
			return fmt.Errorf("retry cancelled: %w", ctx.Err())
		}

		// Don't sleep after last attempt
		if attempt == config.MaxAttempts-1 {
			break
		}

		// Sleep with exponential backoff
		select {
		case <-ctx.Done():
			return fmt.Errorf("retry cancelled during backoff: %w", ctx.Err())
		case <-time.After(delay):
			// Calculate next delay
			delay = time.Duration(float64(delay) * config.Multiplier)
			if delay > config.MaxDelay {
				delay = config.MaxDelay
			}
		}
	}

	return fmt.Errorf("all retry attempts failed: %w", lastErr)
}

// RetryWithResult executes a function with retry and returns a result
func RetryWithResult[T any](ctx context.Context, config *RetryConfig, fn func() (T, error)) (T, error) {
	if config == nil {
		config = DefaultRetryConfig()
	}

	var lastErr error
	var result T
	delay := config.InitialDelay

	for attempt := 0; attempt < config.MaxAttempts; attempt++ {
		// Try the function
		res, err := fn()
		if err == nil {
			return res, nil
		}

		lastErr = err
		result = res

		// Check if context is cancelled
		if ctx.Err() != nil {
			return result, fmt.Errorf("retry cancelled: %w", ctx.Err())
		}

		// Don't sleep after last attempt
		if attempt == config.MaxAttempts-1 {
			break
		}

		// Sleep with exponential backoff
		select {
		case <-ctx.Done():
			return result, fmt.Errorf("retry cancelled during backoff: %w", ctx.Err())
		case <-time.After(delay):
			// Calculate next delay
			delay = time.Duration(float64(delay) * config.Multiplier)
			if delay > config.MaxDelay {
				delay = config.MaxDelay
			}
		}
	}

	return result, fmt.Errorf("all retry attempts failed: %w", lastErr)
}
