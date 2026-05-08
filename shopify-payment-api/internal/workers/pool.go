package workers

import (
	"context"
	"sync"
	"time"

	"github.com/sirupsen/logrus"
	"github.com/sleetsid972/shopify-payment-api/internal/models"
)

// Pool is a bounded worker pool for processing payment requests
type Pool struct {
	workers    int
	taskQueue  chan *Task
	resultChan chan *models.PaymentResponse
	wg         sync.WaitGroup
	ctx        context.Context
	cancel     context.CancelFunc
	logger     *logrus.Logger
	processor  TaskProcessor
}

// Task represents a payment processing task
type Task struct {
	ID        string
	Card      models.CardData
	SiteURL   string
	ProxyURL  string
	Timestamp time.Time
	Attempt   int
}

// TaskProcessor defines the interface for processing tasks
type TaskProcessor interface {
	Process(ctx context.Context, task *Task) (*models.PaymentResponse, error)
}

// PoolConfig contains worker pool configuration
type PoolConfig struct {
	Workers       int
	QueueSize     int
	ResultBufSize int
	Logger        *logrus.Logger
	Processor     TaskProcessor
}

// NewPool creates a new worker pool
func NewPool(config *PoolConfig) *Pool {
	ctx, cancel := context.WithCancel(context.Background())

	if config.Workers <= 0 {
		config.Workers = 10
	}
	if config.QueueSize <= 0 {
		config.QueueSize = 1000
	}
	if config.ResultBufSize <= 0 {
		config.ResultBufSize = 100
	}
	if config.Logger == nil {
		config.Logger = logrus.New()
	}

	pool := &Pool{
		workers:    config.Workers,
		taskQueue:  make(chan *Task, config.QueueSize),
		resultChan: make(chan *models.PaymentResponse, config.ResultBufSize),
		ctx:        ctx,
		cancel:     cancel,
		logger:     config.Logger,
		processor:  config.Processor,
	}

	return pool
}

// Start launches the worker pool
func (p *Pool) Start() {
	p.logger.Infof("Starting worker pool with %d workers", p.workers)

	for i := 0; i < p.workers; i++ {
		p.wg.Add(1)
		go p.worker(i)
	}
}

// worker processes tasks from the queue
func (p *Pool) worker(id int) {
	defer p.wg.Done()

	p.logger.Debugf("Worker %d started", id)

	for {
		select {
		case <-p.ctx.Done():
			p.logger.Debugf("Worker %d shutting down", id)
			return

		case task, ok := <-p.taskQueue:
			if !ok {
				p.logger.Debugf("Worker %d: task queue closed", id)
				return
			}

			p.processTask(id, task)
		}
	}
}

// processTask processes a single task with timeout and recovery
func (p *Pool) processTask(workerID int, task *Task) {
	defer func() {
		if r := recover(); r != nil {
			p.logger.Errorf("Worker %d: panic recovered: %v", workerID, r)
			// Send error response
			errorResp := &models.PaymentResponse{
				Status:    models.StatusError,
				Message:   "Internal server error",
				RequestID: task.ID,
				Timestamp: time.Now(),
			}
			select {
			case p.resultChan <- errorResp:
			default:
				p.logger.Warn("Result channel full, dropping error response")
			}
		}
	}()

	// Create context with timeout
	ctx, cancel := context.WithTimeout(p.ctx, 45*time.Second)
	defer cancel()

	startTime := time.Now()

	// Process the task
	result, err := p.processor.Process(ctx, task)

	duration := time.Since(startTime).Milliseconds()

	if err != nil {
		p.logger.Errorf("Worker %d: task processing failed: %v", workerID, err)
		result = &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   err.Error(),
			RequestID: task.ID,
			Duration:  duration,
			Timestamp: time.Now(),
		}
	} else {
		result.Duration = duration
		result.Timestamp = time.Now()
	}

	// Send result
	select {
	case p.resultChan <- result:
		p.logger.Debugf("Worker %d: task completed in %dms", workerID, duration)
	case <-ctx.Done():
		p.logger.Warn("Worker context cancelled while sending result")
	default:
		p.logger.Warn("Result channel full, dropping response")
	}
}

// Submit adds a task to the queue
func (p *Pool) Submit(req *models.PaymentRequest) error {
	task := &Task{
		ID:        req.RequestID,
		Card:      req.Card,
		SiteURL:   req.SiteURL,
		ProxyURL:  req.ProxyURL,
		Timestamp: time.Now(),
		Attempt:   0,
	}

	select {
	case p.taskQueue <- task:
		return nil
	case <-p.ctx.Done():
		return context.Canceled
	default:
		return ErrQueueFull
	}
}

// Workers returns the number of workers
func (p *Pool) Workers() int {
	return p.workers
}

// Results returns the result channel
func (p *Pool) Results() <-chan *models.PaymentResponse {
	return p.resultChan
}

// Shutdown gracefully shuts down the worker pool
func (p *Pool) Shutdown(timeout time.Duration) error {
	p.logger.Info("Shutting down worker pool...")

	// Stop accepting new tasks
	close(p.taskQueue)

	// Cancel context
	p.cancel()

	// Wait for workers with timeout
	done := make(chan struct{})
	go func() {
		p.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		p.logger.Info("All workers stopped gracefully")
		close(p.resultChan)
		return nil
	case <-time.After(timeout):
		p.logger.Warn("Shutdown timeout exceeded")
		return ErrShutdownTimeout
	}
}

// Errors
var (
	ErrQueueFull        = &PoolError{Message: "task queue is full"}
	ErrShutdownTimeout  = &PoolError{Message: "shutdown timeout exceeded"}
)

// PoolError represents a worker pool error
type PoolError struct {
	Message string
}

func (e *PoolError) Error() string {
	return e.Message
}
