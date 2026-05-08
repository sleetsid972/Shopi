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
func (b *VariablesBuilder) BuildProposalVariables(includePayment bool) map[string]interface{} {
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
			"email": "",
			"shopPayOptInPhone": map[string]interface{}{
				"number": b.Address.Phone,
			},
		},
		"delivery": map[string]interface{}{
			"deliveryLines": []map[string]interface{}{
				{
					"destination": map[string]interface{}{
						"oneTimeUse": true,
						"streetAddress": map[string]interface{}{
							"firstName":   b.Address.FirstName,
							"lastName":    b.Address.LastName,
							"address1":    b.Address.Address1,
							"address2":    b.Address.Address2,
							"city":        b.Address.City,
							"countryCode": b.Address.CountryCode,
							"zoneCode":    b.Address.State,
							"postalCode":  b.Address.PostalCode,
							"phone":       b.Address.Phone,
						},
					},
					"targetMerchandise": map[string]interface{}{
						"lines": []map[string]interface{}{
							{
								"merchandiseId": "gid://shopify/ProductVariantMerchandise/" + b.MerchandiseID,
								"quantity": map[string]interface{}{
									"items": 1,
								},
							},
						},
					},
				},
			},
		},
		"merchandise": map[string]interface{}{
			"lines": []map[string]interface{}{
				{
					"merchandiseId": "gid://shopify/ProductVariantMerchandise/" + b.MerchandiseID,
					"quantity": map[string]interface{}{
						"items": 1,
					},
				},
			},
		},
		"taxes": map[string]interface{}{},
	}

	// Add payment method if included
	if includePayment && b.PaymentID != "" {
		variables["payment"] = map[string]interface{}{
			"lines": []map[string]interface{}{
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
				"firstName":   b.Address.FirstName,
				"lastName":    b.Address.LastName,
				"address1":    b.Address.Address1,
				"address2":    b.Address.Address2,
				"city":        b.Address.City,
				"countryCode": b.Address.CountryCode,
				"zoneCode":    b.Address.State,
				"postalCode":  b.Address.PostalCode,
				"phone":       b.Address.Phone,
			},
		}
	}

	return variables
}

// BuildSubmitVariables builds variables for submit mutation
func (b *VariablesBuilder) BuildSubmitVariables(deliveryStrategy string, stableID string) map[string]interface{} {
	variables := b.BuildProposalVariables(true)

	// Update delivery strategy with the selected one
	if deliveryLines, ok := variables["delivery"].(map[string]interface{})["deliveryLines"].([]map[string]interface{}); ok && len(deliveryLines) > 0 {
		deliveryLines[0]["selectedDeliveryStrategy"] = map[string]interface{}{
			"handle": deliveryStrategy,
		}
		// Update target merchandise with stable ID
		if stableID != "" {
			deliveryLines[0]["targetMerchandise"] = map[string]interface{}{
				"lines": []map[string]interface{}{
					{
						"stableId": stableID,
					},
				},
			}
		}
	}

	return variables
}

// formatAmount formats a float amount to string
func formatAmount(amount float64) string {
	return fmt.Sprintf("%.2f", amount)
}
