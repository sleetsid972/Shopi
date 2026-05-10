package graphql

import "fmt"

// Note: These GraphQL queries are based on the working Python implementation (Autoshopify_FIXED.py)
// They use sessionInput instead of attemptToken to match Shopify's actual API

// QUERY_PROPOSAL is the GraphQL query for negotiating proposal
// This matches the Python version's QUERY_PROPOSAL_SHIPPING
const QUERY_PROPOSAL = `
query Proposal(
  $sessionInput: SessionTokenInput!
  $queueToken: String
  $buyerIdentity: BuyerIdentityTermInput
  $delivery: DeliveryTermsInput
  $discounts: DiscountTermsInput
  $payment: PaymentTermInput
  $merchandise: MerchandiseTermInput
  $taxes: TaxTermInput
) {
  session(sessionInput: $sessionInput) {
    negotiate(
      input: {
        purchaseProposal: {
          buyerIdentity: $buyerIdentity
          delivery: $delivery
          discounts: $discounts
          payment: $payment
          merchandise: $merchandise
          taxes: $taxes
        }
        queueToken: $queueToken
      }
    ) {
      __typename
      result {
        ... on NegotiationResultAvailable {
          sellerProposal {
            delivery {
              ... on FilledDeliveryTerms {
                deliveryLines {
                  availableDeliveryStrategies {
                    ... on CompleteDeliveryStrategy {
                      handle
                      title
                      description
                      methodType
                      amount {
                        ... on MoneyValueConstraint {
                          value {
                            amount
                            currencyCode
                          }
                        }
                      }
                    }
                  }
                  targetMerchandise {
                    ... on FilledMerchandiseLineTargetCollection {
                      linesV2 {
                        ... on MerchandiseLine {
                          stableId
                        }
                      }
                    }
                  }
                }
              }
            }
            payment {
              ... on FilledPaymentTerms {
                availablePaymentLines {
                  paymentMethod {
                    ... on PaymentProvider {
                      paymentMethodIdentifier
                      name
                      extensibilityDisplayName
                    }
                  }
                }
              }
            }
            runningTotal {
              ... on MoneyValueConstraint {
                value {
                  amount
                  currencyCode
                }
              }
            }
            tax {
              ... on FilledTaxTerms {
                totalTaxAmount {
                  ... on MoneyValueConstraint {
                    value {
                      amount
                      currencyCode
                    }
                  }
                }
              }
            }
          }
        }
      }
      errors {
        code
        localizedMessage
        nonLocalizedMessage
      }
    }
  }
}
`

// MUTATION_SUBMIT is the GraphQL mutation for submitting payment
const MUTATION_SUBMIT = `
mutation SubmitForCompletion(
  $sessionInput: SessionTokenInput!
  $queueToken: String
  $buyerIdentity: BuyerIdentityTermInput
  $delivery: DeliveryTermsInput
  $discounts: DiscountTermsInput
  $payment: PaymentTermInput
  $merchandise: MerchandiseTermInput
  $taxes: TaxTermInput
) {
  session(sessionInput: $sessionInput) {
    submit(
      input: {
        purchaseProposal: {
          buyerIdentity: $buyerIdentity
          delivery: $delivery
          discounts: $discounts
          payment: $payment
          merchandise: $merchandise
          taxes: $taxes
        }
        queueToken: $queueToken
      }
    ) {
      __typename
      ... on SubmitSuccess {
        result {
          token
          orderId
          checkoutCompleteUrl
        }
      }
      ... on SubmitPending {
        pollDelay
        receipt {
          id
        }
      }
      ... on SubmitFailed {
        errors {
          code
          localizedMessage
          nonLocalizedMessage
        }
      }
      ... on SubmitAlreadyAccepted {
        receipt {
          token
        }
      }
      ... on SubmitThrottled {
        pollAfter
      }
    }
  }
}
`

// QUERY_POLL is the GraphQL query for polling receipt status
const QUERY_POLL = `
query PollForReceipt($receiptId: ID!, $sessionToken: String!) {
  receipt(id: $receiptId, sessionToken: $sessionToken) {
    __typename
    ... on ProcessingReceipt {
      pollDelay
    }
    ... on FailedReceipt {
      processingError {
        code
        localizedMessage
      }
    }
    ... on SuccessfulReceipt {
      token
      orderId
    }
  }
}
`

// VariablesBuilder helps build GraphQL variables
type VariablesBuilder struct {
	SessionToken  string
	QueueToken    string
	MerchandiseID string
	StableID      string
	Currency      string
	Subtotal      string
	PaymentID     string
	PaymentToken  string
	Address       AddressData
}

// AddressData contains address information
type AddressData struct {
	FirstName   string
	LastName    string
	Address1    string
	Address2    string
	City        string
	State       string
	PostalCode  string
	CountryCode string
	Phone       string
}

// BuildProposalVariables builds variables for proposal query
// Matches Python implementation exactly (Autoshopify (1) (4).py lines 420-490)
func (b *VariablesBuilder) BuildProposalVariables(includePayment bool) map[string]interface{} {
	// Derive variant ID from merchandise ID if needed
	variantID := b.MerchandiseID

	stableID := b.StableID
	if stableID == "" {
		stableID = "1"
	}

	variables := map[string]interface{}{
		"sessionInput": map[string]interface{}{
			"sessionToken": b.SessionToken,
		},
		"queueToken": b.QueueToken,
		"discounts": map[string]interface{}{
			"lines":                      []interface{}{},
			"acceptUnexpectedDiscounts": true,
		},
		"buyerIdentity": map[string]interface{}{
			"customer": map[string]interface{}{
				"presentmentCurrency": b.Currency,
				"countryCode":        b.Address.CountryCode,
			},
			"email":            "",
			"emailChanged":     false,
			"phoneCountryCode": b.Address.CountryCode,
			"shopPayOptInPhone": map[string]interface{}{
				"number": b.Address.Phone,
			},
			"acceptsEmailMarketing":     false,
			"acceptsSmsMarketing":       false,
			"languageCode":             "EN",
			"usesSameAddressForBilling": true,
		},
		"delivery": map[string]interface{}{
			"deliveryLines": []map[string]interface{}{
				{
					"destination": map[string]interface{}{
						"partialStreetAddress": map[string]interface{}{
							"address1":    b.Address.Address1,
							"address2":    b.Address.Address2,
							"city":        b.Address.City,
							"countryCode": b.Address.CountryCode,
							"postalCode":  b.Address.PostalCode,
							"firstName":   b.Address.FirstName,
							"lastName":    b.Address.LastName,
							"zoneCode":    b.Address.State,
							"phone":       b.Address.Phone,
						},
					},
					"selectedDeliveryStrategy": map[string]interface{}{
						"deliveryStrategyMatchingConditions": map[string]interface{}{
							"estimatedTimeInTransit": map[string]interface{}{"any": true},
							"shipments":              map[string]interface{}{"any": true},
						},
						"options": map[string]interface{}{},
					},
					"targetMerchandiseLines": map[string]interface{}{"any": true},
					"deliveryMethodTypes":    []string{"SHIPPING"},
					"expectedTotalPrice":     map[string]interface{}{"any": true},
					"destinationChanged":     true,
				},
			},
			"noDeliveryRequired":              []interface{}{},
			"useProgressiveRates":             false,
			"prefetchShippingRatesStrategy":   nil,
			"supportsSplitShipping":           true,
		},
		"deliveryExpectations": map[string]interface{}{
			"deliveryExpectationLines": []interface{}{},
		},
		"merchandise": map[string]interface{}{
			"merchandiseLines": []map[string]interface{}{
				{
					"stableId": stableID,
					"merchandise": map[string]interface{}{
						"productVariantReference": map[string]interface{}{
							"id":                 fmt.Sprintf("gid://shopify/ProductVariantMerchandise/%s", b.MerchandiseID),
							"variantId":          fmt.Sprintf("gid://shopify/ProductVariant/%s", variantID),
							"properties":         []interface{}{},
							"sellingPlanId":      nil,
							"sellingPlanDigest":  nil,
						},
					},
					"quantity": map[string]interface{}{
						"items": map[string]interface{}{
							"value": 1,
						},
					},
					"expectedTotalPrice": map[string]interface{}{
						"value": map[string]interface{}{
							"amount":       b.Subtotal,
							"currencyCode": b.Currency,
						},
					},
					"lineComponentsSource": nil,
					"lineComponents":       []interface{}{},
				},
			},
		},
		"payment": map[string]interface{}{
			"totalAmount":  map[string]interface{}{"any": true},
			"paymentLines": []interface{}{},
			"billingAddress": map[string]interface{}{
				"streetAddress": map[string]interface{}{
					"address1":    "",
					"city":        "",
					"countryCode": b.Address.CountryCode,
					"lastName":    "",
					"zoneCode":    "ENG",
					"phone":       "",
				},
			},
		},
		"taxes": map[string]interface{}{},
	}

	// Add payment method if included (for submit mutation)
	if includePayment && b.PaymentID != "" {
		variables["payment"] = map[string]interface{}{
			"totalAmount": map[string]interface{}{
				"value": map[string]interface{}{
					"amount":       b.Subtotal,
					"currencyCode": b.Currency,
				},
			},
			"paymentLines": []map[string]interface{}{
				{
					"paymentMethodIdentifier": b.PaymentID,
					"amount": map[string]interface{}{
						"shopMoney": map[string]interface{}{
							"amount":       b.Subtotal,
							"currencyCode": b.Currency,
						},
					},
					"directPaymentMethod": map[string]interface{}{
						"vaultToken": b.PaymentToken,
					},
				},
			},
			"billingAddress": map[string]interface{}{
				"streetAddress": map[string]interface{}{
					"address1":    b.Address.Address1,
					"address2":    b.Address.Address2,
					"city":        b.Address.City,
					"countryCode": b.Address.CountryCode,
					"postalCode":  b.Address.PostalCode,
					"firstName":   b.Address.FirstName,
					"lastName":    b.Address.LastName,
					"zoneCode":    b.Address.State,
					"phone":       b.Address.Phone,
				},
			},
		}
	}

	return variables
}

// BuildSubmitVariables builds variables for submit mutation
// Matches Python implementation (Autoshopify (1) (4).py lines 633-657)
func (b *VariablesBuilder) BuildSubmitVariables(deliveryStrategy string, stableID string) map[string]interface{} {
	variables := b.BuildProposalVariables(true)

	if stableID == "" {
		stableID = "1"
	}

	// Update delivery strategy with the selected one (different structure for submit)
	if deliveryLines, ok := variables["delivery"].(map[string]interface{})["deliveryLines"].([]map[string]interface{}); ok && len(deliveryLines) > 0 {
		// Change from deliveryStrategyMatchingConditions to deliveryStrategyByHandle
		deliveryLines[0]["selectedDeliveryStrategy"] = map[string]interface{}{
			"deliveryStrategyByHandle": map[string]interface{}{
				"handle":             deliveryStrategy,
				"customDeliveryRate": false,
			},
			"options": map[string]interface{}{},
		}

		// Change from {any: true} to specific lines structure
		deliveryLines[0]["targetMerchandiseLines"] = map[string]interface{}{
			"lines": []map[string]interface{}{
				{
					"stableId": stableID,
				},
			},
		}

		// expectedTotalPrice should have actual value, not {any: true}
		// Note: In Python this is set to shipping_amount, we'll keep {any: true} for now
		// as we don't have shipping amount yet in this context

		// Change destinationChanged to false for submit
		deliveryLines[0]["destinationChanged"] = false
	}

	return variables
}

// formatAmount formats a float amount to string
func formatAmount(amount float64) string {
	return fmt.Sprintf("%.2f", amount)
}
