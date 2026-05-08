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
		AddrGen:    utils.NewAddressGenerator(),
	}
}

// Process processes a payment check task
func (p *PaymentProcessor) Process(ctx context.Context, task *workers.Task) (*models.PaymentResponse, error) {
	startTime := time.Now()

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
	checkoutData, err := p.createCheckout(ctx, task.SiteURL, variantID)
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
	proposalData, err := p.executeProposals(ctx, task, checkoutData)
	if err != nil {
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Proposal failed: %v", err),
			Gateway:   proposalData.Gateway,
			Amount:    fmt.Sprintf("%.2f", proposalData.TotalAmount),
			Currency:  proposalData.Currency,
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 4: Vault credit card
	p.Logger.Infof("Task %s: Vaulting card", task.ID)
	paymentToken, err := p.vaultCard(ctx, task, checkoutData)
	if err != nil {
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Card vault failed: %v", err),
			Gateway:   proposalData.Gateway,
			Amount:    fmt.Sprintf("%.2f", proposalData.TotalAmount),
			Currency:  proposalData.Currency,
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 5: Submit payment
	p.Logger.Infof("Task %s: Submitting payment", task.ID)
	submitData, err := p.submitPayment(ctx, task, checkoutData, proposalData, paymentToken)
	if err != nil {
		return &models.PaymentResponse{
			Status:    models.StatusError,
			Message:   fmt.Sprintf("Submit failed: %v", err),
			Gateway:   proposalData.Gateway,
			Amount:    fmt.Sprintf("%.2f", proposalData.TotalAmount),
			Currency:  proposalData.Currency,
			Timestamp: time.Now(),
			Duration:  time.Since(startTime).Milliseconds(),
		}, nil
	}

	// Step 6: Handle pending (poll if needed)
	if submitData.ResultType == "SubmitPending" && submitData.PollURL != "" {
		p.Logger.Infof("Task %s: Payment pending, polling...", task.ID)
		submitData, err = p.pollPaymentStatus(ctx, submitData.PollURL, checkoutData.SessionToken)
		if err != nil {
			return &models.PaymentResponse{
				Status:    models.StatusError,
				Message:   fmt.Sprintf("Poll failed: %v", err),
				Gateway:   proposalData.Gateway,
				Amount:    fmt.Sprintf("%.2f", proposalData.TotalAmount),
				Currency:  proposalData.Currency,
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

// fetchFirstProduct fetches the first available product variant
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

	// Parse response to extract variant ID
	body, _ := io.ReadAll(resp.Body)
	re := regexp.MustCompile(`"variants":\s*\[\s*\{\s*"id":\s*(\d+)`)
	matches := re.FindStringSubmatch(string(body))
	if len(matches) < 2 {
		return "", fmt.Errorf("no variants found")
	}

	return matches[1], nil
}

// createCheckout creates a checkout session
func (p *PaymentProcessor) createCheckout(ctx context.Context, siteURL, variantID string) (*parser.CheckoutData, error) {
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

	resp, err := p.Client.Do(ctx, req)
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

	resp, err = p.Client.Do(ctx, req)
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

	// Check for login requirement
	if strings.Contains(strings.ToLower(html), "login") && strings.Contains(resp.Request.URL.Path, "account") {
		return nil, fmt.Errorf("site requires login")
	}

	// Extract session token from response header
	sessionToken := resp.Header.Get("X-Checkout-One-Session-Token")
	if sessionToken == "" {
		sessionToken = resp.Header.Get("x-checkout-one-session-token")
	}

	// Parse checkout page
	checkoutData, err := p.Parser.ParseCheckoutPage(html)
	if err != nil {
		return nil, err
	}

	// Use header session token if available
	if sessionToken != "" {
		checkoutData.SessionToken = sessionToken
	}

	if checkoutData.SessionToken == "" {
		return nil, fmt.Errorf("failed to extract session token")
	}

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

	// Store attempt token in checkout data (we'll add this field)
	checkoutData.SessionToken = checkoutData.SessionToken
	// Note: We should add AttemptToken to CheckoutData struct

	return checkoutData, nil
}

// executeProposals executes the GraphQL proposal queries
func (p *PaymentProcessor) executeProposals(ctx context.Context, task *workers.Task, checkoutData *parser.CheckoutData) (*parser.ProposalData, error) {
	// Generate address
	addr := p.AddrGen.Generate("US")

	// Build GraphQL variables
	builder := &graphql.VariablesBuilder{
		AttemptToken:  checkoutData.SessionToken, // This should be attempt token
		MerchandiseID: checkoutData.MerchandiseID,
		StableID:      checkoutData.StableID,
		Currency:      checkoutData.Currency,
		Subtotal:      checkoutData.Subtotal,
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

	// Build headers
	graphqlURL := fmt.Sprintf("%s/api/graphql", task.SiteURL)
	headers := map[string]string{
		"Accept":                       "application/json",
		"Content-Type":                 "application/json",
		"X-Shopify-Checkout-Version":   "1",
		"X-Checkout-One-Session-Token": checkoutData.SessionToken,
	}

	// Execute shipping proposal
	variables := builder.BuildProposalVariables(false)
	resp, err := p.GraphQL.Execute(ctx, graphqlURL, graphql.QUERY_PROPOSAL_SHIPPING, variables, headers)
	if err != nil {
		return nil, fmt.Errorf("shipping proposal failed: %w", err)
	}

	// Parse proposal response
	proposalData, err := p.Parser.ParseProposalResponse(resp.Data)
	if err != nil {
		return nil, fmt.Errorf("failed to parse proposal: %w", err)
	}

	// Update builder with payment info
	builder.PaymentID = proposalData.PaymentID
	builder.StableID = proposalData.StableID

	return proposalData, nil
}

// vaultCard vaults the credit card and returns payment token
func (p *PaymentProcessor) vaultCard(ctx context.Context, task *workers.Task, checkoutData *parser.CheckoutData) (string, error) {
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

	resp, err := p.Client.Do(ctx, req)
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
func (p *PaymentProcessor) submitPayment(ctx context.Context, task *workers.Task, checkoutData *parser.CheckoutData, proposalData *parser.ProposalData, paymentToken string) (*parser.SubmitData, error) {
	// Generate address
	addr := p.AddrGen.Generate("US")

	// Build GraphQL variables
	builder := &graphql.VariablesBuilder{
		AttemptToken:  checkoutData.SessionToken,
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

	// Build headers
	graphqlURL := fmt.Sprintf("%s/api/graphql", task.SiteURL)
	headers := map[string]string{
		"Accept":                       "application/json",
		"Content-Type":                 "application/json",
		"X-Shopify-Checkout-Version":   "1",
		"X-Checkout-One-Session-Token": checkoutData.SessionToken,
	}

	// Execute submit mutation
	variables := builder.BuildSubmitVariables(proposalData.DeliveryStrategy, proposalData.ShippingAmount, proposalData.TaxAmount)
	resp, err := p.GraphQL.Execute(ctx, graphqlURL, graphql.MUTATION_SUBMIT_PAYMENT, variables, headers)
	if err != nil {
		return nil, fmt.Errorf("submit mutation failed: %w", err)
	}

	// Parse submit response
	submitData, err := p.Parser.ParseSubmitResponse(resp.Data)
	if err != nil {
		return nil, fmt.Errorf("failed to parse submit response: %w", err)
	}

	return submitData, nil
}

// pollPaymentStatus polls the payment status URL
func (p *PaymentProcessor) pollPaymentStatus(ctx context.Context, pollURL, sessionToken string) (*parser.SubmitData, error) {
	maxAttempts := 3
	delay := 2 * time.Second

	for attempt := 0; attempt < maxAttempts; attempt++ {
		time.Sleep(delay)

		req, err := http.NewRequestWithContext(ctx, "GET", pollURL, nil)
		if err != nil {
			return nil, err
		}

		req.Header.Set("X-Checkout-One-Session-Token", sessionToken)

		resp, err := p.Client.Do(ctx, req)
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
