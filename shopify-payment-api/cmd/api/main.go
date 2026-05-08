package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/sirupsen/logrus"
	"github.com/sleetsid972/shopify-payment-api/internal/api"
	"github.com/sleetsid972/shopify-payment-api/internal/classifier"
	"github.com/sleetsid972/shopify-payment-api/internal/middleware"
	"github.com/sleetsid972/shopify-payment-api/internal/network"
	"github.com/sleetsid972/shopify-payment-api/internal/workers"
)

func main() {
	// Initialize logger
	logger := logrus.New()
	logger.SetFormatter(&logrus.JSONFormatter{})
	logger.SetLevel(logrus.InfoLevel)

	logger.Info("Starting Shopify Payment API...")

	// Initialize HTTP client
	httpClient, err := network.NewClient(network.DefaultConfig())
	if err != nil {
		logger.Fatalf("Failed to create HTTP client: %v", err)
	}
	defer httpClient.Close()

	// Initialize payment classifier
	paymentClassifier := classifier.NewClassifier()

	// Initialize payment processor
	processor := api.NewPaymentProcessor(httpClient, paymentClassifier, logger)

	// Initialize worker pool
	poolConfig := &workers.PoolConfig{
		Workers:       20, // Configurable based on VPS resources
		QueueSize:     1000,
		ResultBufSize: 100,
		Logger:        logger,
		Processor:     processor,
	}

	workerPool := workers.NewPool(poolConfig)
	workerPool.Start()

	// Initialize HTTP server with middleware
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(middleware.RecoveryMiddleware(logger))
	router.Use(middleware.LoggingMiddleware(logger))
	router.Use(middleware.CORSMiddleware())
	router.Use(middleware.TimeoutMiddleware(60 * time.Second))

	// Initialize API server
	apiServer := api.NewServer(workerPool, processor, logger)

	// Register routes
	router.POST("/shopify/check", apiServer.HandlePaymentCheck)
	router.GET("/health", apiServer.HandleHealth)
	router.GET("/metrics", apiServer.HandleMetrics)

	// Create HTTP server
	srv := &http.Server{
		Addr:         ":8080",
		Handler:      router,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	// Start server in goroutine
	go func() {
		logger.Infof("HTTP server listening on %s", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("HTTP server error: %v", err)
		}
	}()

	// Wait for interrupt signal
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	logger.Info("Shutting down server...")

	// Graceful shutdown with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// Shutdown worker pool
	if err := workerPool.Shutdown(20 * time.Second); err != nil {
		logger.Errorf("Worker pool shutdown error: %v", err)
	}

	// Shutdown HTTP server
	if err := srv.Shutdown(ctx); err != nil {
		logger.Errorf("HTTP server shutdown error: %v", err)
	}

	logger.Info("Server stopped")
}
