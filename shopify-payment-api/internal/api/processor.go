package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/sirupsen/logrus"
	"github.com/sleetsid972/shopify-payment-api/internal/classifier"
	"github.com/sleetsid972/shopify-payment-api/internal/graphql"
	"github.com/sleetsid972/shopify-payment-api/internal/models"
	"github.com/sleetsid972/shopify-payment-api/internal/network"
	"github.com/sleetsid972/shopify-payment-api/internal/parser"
	"github.com/sleetsid972/shopify-payment-api/internal/utils"
	"github.com/sleetsid972/shopify-payment-api/internal/workers"
)

// PaymentProcessor processes payment checks
type PaymentProcessor struct {
	Client       *network.Client
	Classifier   *classifier.Classifier
	Logger       *logrus.Logger
	GraphQL      *graphql.Executor
	Parser       *parser.Parser
	AddrGen      *utils.AddressGenerator
}

// NewPaymentProcessor creates a new payment processor
func NewPaymentProcessor(client *network.Client, classifier *classifier.Classifier, logger *logrus.Logger) *PaymentProcessor {
	return &PaymentProcessor{
		Client:     client,
		Classifier: classifier,
		Logger:     logger,
		GraphQL:    graphql.NewExecutor(client, logger),
		Parser:     parser.NewParser(logger),
		AddrGen:    utils.NewAddressGenerator(logger),
	}
}

// Process processes a payment check task
func (p *PaymentProcessor) Process(ctx context.Context, task *workers.Task) (*models.PaymentResponse, error) {
	startTime := time.Now()

	// Create fresh cookie jar for this task to prevent cookie leakage
	// This matches Python's isolated aiohttp.ClientSession per request
	checkoutClient, err := p.Client.CloneWithFreshCookieJar()
	if err != nil {
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Failed to create isolated client: %v", err),
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 1: Fetch first product from shop
	p.Logger.Infof("Task %s: Fetching product from %s", task.ID, task.SiteURL)
	variantID, err := p.fetchFirstProduct(ctx, task.SiteURL)
	if err != nil {
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Failed to fetch product: %v", err),
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 2: Add to cart and navigate to checkout
	p.Logger.Infof("Task %s: Creating checkout session", task.ID)
	checkoutData, err := p.createCheckout(ctx, checkoutClient, task.SiteURL, variantID)
	if err != nil {
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Failed to create checkout: %v", err),
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 3: Execute GraphQL proposal queries
	p.Logger.Infof("Task %s: Executing GraphQL proposals", task.ID)
	proposalData, err := p.executeProposals(ctx, checkoutClient, task, checkoutData)
	if err != nil {
		// Safe defaults if proposalData is nil
		gateway := ""
		amount := "0.00"
		currency := "USD"
		if proposalData != nil {
			gateway = proposalData.Gateway
			amount = fmt.Sprintf("%.2f", proposalData.TotalAmount)
			currency = proposalData.Currency
		}
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Proposal failed: %v", err),
			Gateway:   gateway,
			Amount:    amount,
			Currency:  currency,
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 4: Vault credit card
	p.Logger.Infof("Task %s: Vaulting card", task.ID)
	paymentToken, err := p.vaultCard(ctx, checkoutClient, task, checkoutData)
	if err != nil {
		// Safe defaults if proposalData is nil
		gateway := ""
		amount := "0.00"
		currency := "USD"
		if proposalData != nil {
			gateway = proposalData.Gateway
			amount = fmt.Sprintf("%.2f", proposalData.TotalAmount)
			currency = proposalData.Currency
		}
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Card vault failed: %v", err),
			Gateway:   gateway,
			Amount:    amount,
			Currency:  currency,
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 5: Submit payment
	p.Logger.Infof("Task %s: Submitting payment", task.ID)
	submitData, err := p.submitPayment(ctx, checkoutClient, task, checkoutData, proposalData, paymentToken)
	if err != nil {
		// Safe defaults if proposalData is nil
		gateway := ""
		amount := "0.00"
		currency := "USD"
		if proposalData != nil {
			gateway = proposalData.Gateway
			amount = fmt.Sprintf("%.2f", proposalData.TotalAmount)
			currency = proposalData.Currency
		}
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Submit failed: %v", err),
			Gateway:   gateway,
			Amount:    amount,
			Currency:  currency,
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 6: Handle pending (poll if needed)
	if submitData.ResultType == "SubmitPending" && submitData.PollURL != "" {
		p.Logger.Infof("Task %s: Payment pending, polling...", task.ID)
		submitData, err = p.pollPaymentStatus(ctx, checkoutClient, submitData.PollURL, checkoutData.SessionToken)
		if err != nil {
			// Safe defaults if proposalData is nil
			gateway := ""
			amount := "0.00"
			currency := "USD"
			if proposalData != nil {
				gateway = proposalData.Gateway
				amount = fmt.Sprintf("%.2f", proposalData.TotalAmount)
				currency = proposalData.Currency
			}
			return &models.PaymentResponse{
				Status:    models.StatusError,
				Message:   fmt.Sprintf("Poll failed: %v", err),
				Gateway:   gateway,
				Amount:    amount,
				Currency:  currency,
				Timestamp: time.Now(),
				Duration:  time.Since(startTime).Milliseconds(),
			}, nil
		}
	}

	// Step 7: Classify payment result
	classResult := p.Classifier.Classify(&classifier.ResponseData{
		ResultType:       submitData.ResultType,
		ErrorCode:        submitData.ErrorCode,
		ErrorMessage:     submitData.Message,
		LocalizedMessage: submitData.Message,
		RawResponse:      submitData.ErrorCode + " " + submitData.Message,
	})

	p.Logger.Infof("Task %s: Classification: %s (confidence: %.2f)", task.ID, classResult.Status, classResult.Confidence)

	return &models.PaymentResponse{
		Status:    classResult.Status,
		Message:   submitData.Message,
		Gateway:   proposalData.Gateway,
		Amount:    fmt.Sprintf("%.2f", proposalData.TotalAmount),
		Currency:  proposalData.Currency,
		Timestamp: time.Now(),
		Duration:  time.Since(startTime).Milliseconds(),
	}, nil
}

// ProductsResponse represents the JSON response from /products.json
type ProductsResponse struct {
	Products []Product `json:"products"`
}

// Product represents a Shopify product
type Product struct {
	Handle   string    `json:"handle"`
	Variants []Variant `json:"variants"`
}

// Variant represents a product variant
type Variant struct {
	ID        int64   `json:"id"`
	Available bool    `json:"available"`
	Price     string  `json:"price"`
}

// fetchFirstProduct fetches the cheapest available product variant ID
// Matches Python fetch_products function behavior
func (p *PaymentProcessor) fetchFirstProduct(ctx context.Context, siteURL string) (string, error) {
	productsURL := fmt.Sprintf("%s/products.json?limit=1", siteURL)

	req, err := http.NewRequestWithContext(ctx, "GET", productsURL, nil)
	if err != nil {
		return "", err
	}

	req.Header.Set("Accept", "application/json")

	resp, err := p.Client.Do(ctx, req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("products endpoint returned status %d", resp.StatusCode)
	}

	// Unmarshal JSON response
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}

	var productsResp ProductsResponse
	if err := json.Unmarshal(body, &productsResp); err != nil {
		return "", fmt.Errorf("failed to parse products JSON: %w", err)
	}

	if len(productsResp.Products) == 0 {
		return "", fmt.Errorf("no products found")
	}

	// Find the cheapest available variant
	minPrice := float64(999999999) // Use a large number instead of infinity
	var cheapestVariantID string

	for _, product := range productsResp.Products {
		if len(product.Variants) == 0 {
			continue
		}

		for _, variant := range product.Variants {
			// Skip unavailable variants (available must be explicitly true)
			if !variant.Available {
				continue
			}

			// Parse price
			price, err := parsePrice(variant.Price)
			if err != nil {
				p.Logger.Warnf("Failed to parse variant price '%s': %v", variant.Price, err)
				continue
			}

			// Track cheapest variant
			if price < minPrice {
				minPrice = price
				cheapestVariantID = fmt.Sprintf("%d", variant.ID)
			}
		}
	}

	if cheapestVariantID == "" {
		return "", fmt.Errorf("no valid products")
	}

	p.Logger.Infof("Found cheapest available variant: ID=%s, Price=%.2f", cheapestVariantID, minPrice)
	return cheapestVariantID, nil
}

// parsePrice parses a price string to float64
func parsePrice(priceStr string) (float64, error) {
	// Remove commas from price string
	priceStr = strings.ReplaceAll(priceStr, ",", "")

	price, err := strconv.ParseFloat(priceStr, 64)
	if err != nil {
		return 0, err
	}

	return price, nil
}

// createCheckout creates a checkout session
func (p *PaymentProcessor) createCheckout(ctx context.Context, client *network.Client, siteURL, variantID string) (*parser.CheckoutData, error) {
	// Add to cart
	cartURL := fmt.Sprintf("%s/cart/add.js", siteURL)
	cartData := url.Values{}
	cartData.Set("id", variantID)
	cartData.Set("quantity", "1")

	req, err := http.NewRequestWithContext(ctx, "POST", cartURL, strings.NewReader(cartData.Encode()))
	if err != nil {
		return nil, err
	}

	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(ctx, req)
	if err != nil {
		return nil, err
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("cart add failed with status %d", resp.StatusCode)
	}

	// Navigate to checkout
	checkoutURL := fmt.Sprintf("%s/checkout/", siteURL)
	req, err = http.NewRequestWithContext(ctx, "POST", checkoutURL, nil)
	if err != nil {
		return nil, err
	}

	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")

	resp, err = client.Do(ctx, req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	// Read checkout page
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	html := string(body)

	// Debug logging
	p.Logger.Infof("Checkout URL: %s", resp.Request.URL.String())
	p.Logger.Infof("Response status: %d", resp.StatusCode)
	p.Logger.Infof("Response headers: %v", resp.Header)
	p.Logger.Infof("HTML length: %d bytes", len(html))

	// Log first 500 chars of HTML for debugging
	if len(html) > 500 {
		p.Logger.Debugf("HTML start: %s...", html[:500])
	}

	// Check for login requirement
	if strings.Contains(strings.ToLower(html), "login") && strings.Contains(resp.Request.URL.Path, "account") {
		return nil, fmt.Errorf("site requires login")
	}

	// Extract session token from response header
	sessionToken := resp.Header.Get("X-Checkout-One-Session-Token")
	if sessionToken == "" {
		sessionToken = resp.Header.Get("x-checkout-one-session-token")
	}

	p.Logger.Infof("Session token from header: %s", sessionToken)

	// Parse checkout page
	checkoutData, err := p.Parser.ParseCheckoutPage(html)
	if err != nil {
		return nil, err
	}

	p.Logger.Infof("Session token from HTML: %s", checkoutData.SessionToken)

	// Use header session token if available
	if sessionToken != "" {
		checkoutData.SessionToken = sessionToken
	}

	if checkoutData.SessionToken == "" {
		// Log snippet for debugging
		htmlSnippet := html
		if len(html) > 1000 {
			htmlSnippet = html[:1000]
		}
		p.Logger.Errorf("Failed to extract session token. HTML snippet: %s", htmlSnippet)
		return nil, fmt.Errorf("failed to extract session token")
	}

	p.Logger.Infof("Final session token: %s", checkoutData.SessionToken)

	// Extract attempt token from URL
	attemptToken := ""
	urlPath := resp.Request.URL.Path
	re := regexp.MustCompile(`/checkouts/cn/([^/?]+)`)
	matches := re.FindStringSubmatch(urlPath)
	if len(matches) > 1 {
		attemptToken = matches[1]
	} else {
		// Fallback: extract from URL segments
		segments := strings.Split(strings.Trim(urlPath, "/"), "/")
		if len(segments) > 0 {
			lastSegment := segments[len(segments)-1]
			attemptToken = strings.Split(lastSegment, "?")[0]
		}
	}

	if attemptToken == "" {
		return nil, fmt.Errorf("failed to extract attempt token")
	}

	// Store attempt token in checkout data
	checkoutData.AttemptToken = attemptToken

	return checkoutData, nil
}

// executeProposals executes the GraphQL proposal queries
func (p *PaymentProcessor) executeProposals(ctx context.Context, client *network.Client, task *workers.Task, checkoutData *parser.CheckoutData) (*parser.ProposalData, error) {
	// Generate address
	addr := p.AddrGen.Generate("US")

	// Generate email from name
	email := fmt.Sprintf("%s.%s@example.com", strings.ToLower(addr.FirstName), strings.ToLower(addr.LastName))
	if addr.FirstName == "" || addr.LastName == "" {
		email = "customer@example.com"
	}

	// Build GraphQL variables
	builder := &graphql.VariablesBuilder{
		SessionToken:  checkoutData.SessionToken, // Use the actual session token
		QueueToken:    checkoutData.QueueToken,
		MerchandiseID: checkoutData.MerchandiseID,
		StableID:      checkoutData.StableID,
		Currency:      checkoutData.Currency,
		Subtotal:      checkoutData.Subtotal,
		Email:         email,
		Address: graphql.AddressData{
			FirstName:   addr.FirstName,
			LastName:    addr.LastName,
			Address1:    addr.Address1,
			Address2:    addr.Address2,
			City:        addr.City,
			State:       addr.State,
			PostalCode:  addr.PostalCode,
			CountryCode: addr.CountryCode,
			Phone:       addr.Phone,
		},
	}

	// Build headers - use /checkouts/unstable/graphql endpoint (matches Python implementation)
	graphqlURL := fmt.Sprintf("%s/checkouts/unstable/graphql", task.SiteURL)
	headers := map[string]string{
		"Accept":                       "application/json",
		"Content-Type":                 "application/json",
		"X-Shopify-Checkout-Version":   "1",
		"X-Checkout-One-Session-Token": checkoutData.SessionToken,
		"shopify-checkout-client":      "checkout-web/1.0",
		"shopify-checkout-source":      fmt.Sprintf(`id="%s", type="cn"`, checkoutData.AttemptToken),
		"sec-fetch-dest":               "empty",
		"sec-fetch-mode":               "cors",
		"sec-fetch-site":               "same-origin",
	}

	// Add build-related headers if available
	if checkoutData.BuildID != "" {
		headers["x-checkout-web-build-id"] = checkoutData.BuildID
		headers["x-checkout-web-deploy-stage"] = "production"
		headers["x-checkout-web-server-handling"] = "fast"
		headers["x-checkout-web-server-rendering"] = "yes"
	}

	// Add source token header if available
	if checkoutData.SourceToken != "" {
		headers["x-checkout-web-source-id"] = checkoutData.SourceToken
	}

	// Execute shipping proposal (first proposal)
	p.Logger.Info("Executing first proposal (shipping)...")
	variables := builder.BuildProposalVariables(false)

	// Log variables payload at debug level
	if variablesJSON, err := json.Marshal(variables); err == nil {
		p.Logger.Debugf("First proposal variables payload: %s", string(variablesJSON))
	}

	// Log delivery phone specifically
	p.Logger.Infof("Delivery phone in proposal: %s", addr.Phone)

	// Create GraphQL executor with isolated client for this task
	graphqlExecutor := graphql.NewExecutor(client, p.Logger)
	resp, err := graphqlExecutor.Execute(ctx, graphqlURL, graphql.QUERY_PROPOSAL, variables, headers)
	if err != nil {
		return nil, fmt.Errorf("first proposal failed: %w", err)
	}

	// Check for GraphQL errors or null data before parsing
	if len(resp.Errors) > 0 {
		errorMessages := make([]string, len(resp.Errors))
		for i, e := range resp.Errors {
			errorMessages[i] = e.Message
		}
		return nil, fmt.Errorf("first proposal GraphQL errors: %v", errorMessages)
	}
	if resp.Data == nil {
		return nil, fmt.Errorf("first proposal response has null data field")
	}

	// Parse first proposal response
	proposalData, err := p.Parser.ParseProposalResponse(resp.Data)
	if err != nil {
		return nil, fmt.Errorf("failed to parse first proposal: %w", err)
	}

	p.Logger.Infof("First proposal completed. CheckpointData: %v, QueueToken: %v, ChangesetTokens: %v",
		proposalData.CheckpointData != "", proposalData.QueueToken != "", len(proposalData.ChangesetTokens))

	// Sleep for 3 seconds before second proposal (matches Python implementation)
	time.Sleep(3 * time.Second)

	// Execute second proposal (delivery selection) - this accepts tax terms
	p.Logger.Info("Executing second proposal (delivery selection)...")

	// Update builder with checkpoint and queue tokens from first proposal
	builder.CheckpointData = proposalData.CheckpointData
	builder.QueueToken = proposalData.QueueToken
	builder.ChangesetTokens = proposalData.ChangesetTokens

	// Use same variables for second proposal (Python does this in a loop with same data)
	variables = builder.BuildProposalVariables(false)
	resp, err = graphqlExecutor.Execute(ctx, graphqlURL, graphql.QUERY_PROPOSAL, variables, headers)
	if err != nil {
		return nil, fmt.Errorf("second proposal failed: %w", err)
	}

	// Check for GraphQL errors or null data
	if len(resp.Errors) > 0 {
		errorMessages := make([]string, len(resp.Errors))
		for i, e := range resp.Errors {
			errorMessages[i] = e.Message
		}
		return nil, fmt.Errorf("second proposal GraphQL errors: %v", errorMessages)
	}
	if resp.Data == nil {
		return nil, fmt.Errorf("second proposal response has null data field")
	}

	// Parse second proposal response (this should have accepted tax terms)
	proposalData, err = p.Parser.ParseProposalResponse(resp.Data)
	if err != nil {
		return nil, fmt.Errorf("failed to parse second proposal: %w", err)
	}

	p.Logger.Info("Second proposal completed successfully")

	// Update builder with payment info
	builder.PaymentID = proposalData.PaymentID
	builder.StableID = proposalData.StableID

	return proposalData, nil
}

// vaultCard vaults the credit card and returns payment token
func (p *PaymentProcessor) vaultCard(ctx context.Context, client *network.Client, task *workers.Task, checkoutData *parser.CheckoutData) (string, error) {
	// Generate address
	addr := p.AddrGen.Generate("US")

	// Build vault payload
	payload := map[string]interface{}{
		"credit_card": map[string]interface{}{
			"number":             task.Card.Number,
			"month":              task.Card.Month,
			"year":               task.Card.Year,
			"verification_value": task.Card.CVV,
			"start_month":        nil,
			"start_year":         nil,
			"issue_number":       "",
			"name":               fmt.Sprintf("%s %s", addr.FirstName, addr.LastName),
		},
		"payment_session_scope": strings.TrimPrefix(task.SiteURL, "https://"),
	}

	// Vault URL
	vaultURL := "https://deposit.shopifycs.com/sessions"

	// Build request
	bodyBytes, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, "POST", vaultURL, bytes.NewReader(bodyBytes))
	if err != nil {
		return "", err
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Origin", "https://checkout.pci.shopifyinc.com")
	if checkoutData.IdentSignature != "" {
		req.Header.Set("shopify-identification-signature", checkoutData.IdentSignature)
	}

	resp, err := client.Do(ctx, req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("vault failed with status %d: %s", resp.StatusCode, string(body))
	}

	// Parse response
	var vaultResp map[string]interface{}
	if err := json.Unmarshal(body, &vaultResp); err != nil {
		return "", fmt.Errorf("failed to parse vault response: %w", err)
	}

	// Extract session ID
	sessionID := parser.GetString(vaultResp, "id")
	if sessionID == "" {
		return "", fmt.Errorf("no session ID in vault response")
	}

	return sessionID, nil
}

// submitPayment submits the payment
func (p *PaymentProcessor) submitPayment(ctx context.Context, client *network.Client, task *workers.Task, checkoutData *parser.CheckoutData, proposalData *parser.ProposalData, paymentToken string) (*parser.SubmitData, error) {
	// Generate address
	addr := p.AddrGen.Generate("US")

	// Build GraphQL variables
	builder := &graphql.VariablesBuilder{
		SessionToken:  checkoutData.SessionToken, // Use actual session token
		QueueToken:    checkoutData.QueueToken,
		MerchandiseID: checkoutData.MerchandiseID,
		StableID:      proposalData.StableID,
		Currency:      proposalData.Currency,
		Subtotal:      checkoutData.Subtotal,
		PaymentID:     proposalData.PaymentID,
		PaymentToken:  paymentToken,
		Address: graphql.AddressData{
			FirstName:   addr.FirstName,
			LastName:    addr.LastName,
			Address1:    addr.Address1,
			Address2:    addr.Address2,
			City:        addr.City,
			State:       addr.State,
			PostalCode:  addr.PostalCode,
			CountryCode: addr.CountryCode,
			Phone:       addr.Phone,
		},
	}

	// Build headers - use /checkouts/unstable/graphql endpoint (matches Python implementation)
	graphqlURL := fmt.Sprintf("%s/checkouts/unstable/graphql", task.SiteURL)
	headers := map[string]string{
		"Accept":                       "application/json",
		"Content-Type":                 "application/json",
		"X-Shopify-Checkout-Version":   "1",
		"X-Checkout-One-Session-Token": checkoutData.SessionToken,
		"shopify-checkout-client":      "checkout-web/1.0",
		"shopify-checkout-source":      fmt.Sprintf(`id="%s", type="cn"`, checkoutData.AttemptToken),
		"sec-fetch-dest":               "empty",
		"sec-fetch-mode":               "cors",
		"sec-fetch-site":               "same-origin",
	}

	// Add build-related headers if available
	if checkoutData.BuildID != "" {
		headers["x-checkout-web-build-id"] = checkoutData.BuildID
		headers["x-checkout-web-deploy-stage"] = "production"
		headers["x-checkout-web-server-handling"] = "fast"
		headers["x-checkout-web-server-rendering"] = "yes"
	}

	// Add source token header if available
	if checkoutData.SourceToken != "" {
		headers["x-checkout-web-source-id"] = checkoutData.SourceToken
	}

	// Execute submit mutation - use the new signature
	variables := builder.BuildSubmitVariables(proposalData.DeliveryStrategy, proposalData.StableID)

	// Create GraphQL executor with isolated client for this task
	graphqlExecutor := graphql.NewExecutor(client, p.Logger)
	resp, err := graphqlExecutor.Execute(ctx, graphqlURL, graphql.MUTATION_SUBMIT, variables, headers)
	if err != nil {
		return nil, fmt.Errorf("submit mutation failed: %w", err)
	}

	// Check for GraphQL errors or null data before parsing
	if len(resp.Errors) > 0 {
		errorMessages := make([]string, len(resp.Errors))
		for i, e := range resp.Errors {
			errorMessages[i] = e.Message
		}
		return nil, fmt.Errorf("GraphQL errors: %v", errorMessages)
	}
	if resp.Data == nil {
		return nil, fmt.Errorf("GraphQL response has null data field")
	}

	// Parse submit response
	submitData, err := p.Parser.ParseSubmitResponse(resp.Data)
	if err != nil {
		return nil, fmt.Errorf("failed to parse submit response: %w", err)
	}

	return submitData, nil
}

// pollPaymentStatus polls the payment status URL
func (p *PaymentProcessor) pollPaymentStatus(ctx context.Context, client *network.Client, pollURL, sessionToken string) (*parser.SubmitData, error) {
	maxAttempts := 3
	delay := 2 * time.Second

	for attempt := 0; attempt < maxAttempts; attempt++ {
		time.Sleep(delay)

		req, err := http.NewRequestWithContext(ctx, "GET", pollURL, nil)
		if err != nil {
			return nil, err
		}

		req.Header.Set("X-Checkout-One-Session-Token", sessionToken)

		resp, err := client.Do(ctx, req)
		if err != nil {
			continue
		}

		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		if resp.StatusCode == http.StatusOK {
			var data map[string]interface{}
			if err := json.Unmarshal(body, &data); err == nil {
				submitData, err := p.Parser.ParseSubmitResponse(data)
				if err == nil && submitData.ResultType != "SubmitPending" {
					return submitData, nil
				}
			}
		}
	}

	return &parser.SubmitData{
		ResultType: "SubmitFailed",
		Success:    false,
		Message:    "Polling timeout",
	}, nil
}
