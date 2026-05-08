.PHONY: build run test clean docker-build docker-run

# Build variables
BINARY_NAME=shopify-api
DOCKER_IMAGE=shopify-payment-api
VERSION?=latest

# Go parameters
GOCMD=go
GOBUILD=$(GOCMD) build
GOTEST=$(GOCMD) test
GOGET=$(GOCMD) get
GOMOD=$(GOCMD) mod

# Build the application
build:
	@echo "Building..."
	$(GOBUILD) -o bin/$(BINARY_NAME) -v cmd/api/main.go

# Run the application
run: build
	@echo "Running..."
	./bin/$(BINARY_NAME)

# Run tests
test:
	@echo "Testing..."
	$(GOTEST) -v -race -coverprofile=coverage.out ./...

# Test coverage
coverage: test
	$(GOCMD) tool cover -html=coverage.out

# Clean build artifacts
clean:
	@echo "Cleaning..."
	rm -rf bin/
	rm -f coverage.out

# Download dependencies
deps:
	@echo "Downloading dependencies..."
	$(GOMOD) download
	$(GOMOD) tidy

# Build optimized production binary
build-prod:
	@echo "Building production binary..."
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GOBUILD) \
		-ldflags='-w -s -extldflags "-static"' \
		-a -installsuffix cgo \
		-o bin/$(BINARY_NAME) \
		cmd/api/main.go

# Build Docker image
docker-build:
	@echo "Building Docker image..."
	docker build -t $(DOCKER_IMAGE):$(VERSION) .

# Run Docker container
docker-run:
	@echo "Running Docker container..."
	docker run -p 8080:8080 \
		-e WORKERS=20 \
		-e LOG_LEVEL=info \
		--name $(BINARY_NAME) \
		$(DOCKER_IMAGE):$(VERSION)

# Stop Docker container
docker-stop:
	docker stop $(BINARY_NAME)
	docker rm $(BINARY_NAME)

# Lint code
lint:
	@echo "Linting..."
	golangci-lint run --timeout=5m

# Format code
fmt:
	@echo "Formatting..."
	$(GOCMD) fmt ./...

# Run locally with hot reload (requires air)
dev:
	air

# Install development tools
tools:
	go install github.com/cosmtrek/air@latest
	go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest

# Help
help:
	@echo "Available targets:"
	@echo "  build       - Build the application"
	@echo "  run         - Build and run the application"
	@echo "  test        - Run tests"
	@echo "  coverage    - Generate test coverage report"
	@echo "  clean       - Clean build artifacts"
	@echo "  deps        - Download dependencies"
	@echo "  build-prod  - Build optimized production binary"
	@echo "  docker-build- Build Docker image"
	@echo "  docker-run  - Run Docker container"
	@echo "  lint        - Lint code"
	@echo "  fmt         - Format code"
	@echo "  dev         - Run with hot reload"
	@echo "  tools       - Install development tools"
