package graphql

import "fmt"

// QUERY_PROPOSAL_SHIPPING is the initial GraphQL query for shipping proposal
const QUERY_PROPOSAL_SHIPPING = `
query proposal(
  $attemptToken: String!
  $buyerIdentity: BuyerIdentityInput
  $delivery: DeliveryInput
  $localization: LocalizationInput
  $payment: PaymentInput
  $taxes: TaxesInput
) {
  proposal(
    attemptToken: $attemptToken
    buyerIdentity: $buyerIdentity
    delivery: $delivery
    localization: $localization
    payment: $payment
    taxes: $taxes
  ) {
    ... on ProposalSuccess {
      proposal {
        runningTotal {
          value {
            amount
            currencyCode
          }
        }
        delivery {
          deliveryLines {
            targetMerchandise {
              ... on ProductVariantSnapshot {
                id
              }
            }
            selectedDeliveryStrategy {
              handle
            }
            availableDeliveryStrategies {
              handle
              description
              price {
                value {
                  amount
                  currencyCode
                }
              }
              methodType
            }
          }
        }
        payment {
          availablePaymentLines {
            paymentMethod {
              name
              paymentMethodIdentifier
              extensibilityDisplayName
            }
          }
        }
        tax {
          ... on FilledTaxTerms {
            totalTaxAmount {
              value {
                amount
                currencyCode
              }
            }
          }
        }
      }
    }
    ... on ProposalFailure {
      proposalErrors {
        code
        localizedMessage
        nonLocalizedMessage
        key
      }
    }
  }
}
`

// QUERY_PROPOSAL_DELIVERY is the GraphQL query for delivery proposal
const QUERY_PROPOSAL_DELIVERY = `
query proposal(
  $attemptToken: String!
  $buyerIdentity: BuyerIdentityInput
  $delivery: DeliveryInput
  $localization: LocalizationInput
  $payment: PaymentInput
  $taxes: TaxesInput
) {
  proposal(
    attemptToken: $attemptToken
    buyerIdentity: $buyerIdentity
    delivery: $delivery
    localization: $localization
    payment: $payment
    taxes: $taxes
  ) {
    ... on ProposalSuccess {
      proposal {
        runningTotal {
          value {
            amount
            currencyCode
          }
        }
      }
    }
    ... on ProposalFailure {
      proposalErrors {
        code
        localizedMessage
        nonLocalizedMessage
      }
    }
  }
}
`

// MUTATION_SUBMIT_PAYMENT is the GraphQL mutation for submitting payment
const MUTATION_SUBMIT_PAYMENT = `
mutation submit(
  $attemptToken: String!
  $buyerIdentity: BuyerIdentityInput
  $delivery: DeliveryInput
  $localization: LocalizationInput
  $payment: PaymentInput
  $taxes: TaxesInput
) {
  submit(
    attemptToken: $attemptToken
    buyerIdentity: $buyerIdentity
    delivery: $delivery
    localization: $localization
    payment: $payment
    taxes: $taxes
  ) {
    ... on SubmitSuccess {
      result {
        token
        orderId
        checkoutCompleteUrl
      }
    }
    ... on SubmitPending {
      pollDelay
      pollUrl
    }
    ... on SubmitThrottled {
      throttle {
        currentlyAvailable
        restoreRate
      }
    }
    ... on SubmitRejected {
      errors {
        code
        localizedMessage
        nonLocalizedMessage
        key
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
  }
}
`

// VariablesBuilder helps build GraphQL variables
type VariablesBuilder struct {
	AttemptToken  string
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
		"attemptToken": b.AttemptToken,
		"buyerIdentity": map[string]interface{}{
			"shopPayOptInPhone": map[string]interface{}{
				"number": b.Address.Phone,
			},
		},
		"delivery": map[string]interface{}{
			"deliveryLines": []map[string]interface{}{
				{
					"targetMerchandise": map[string]interface{}{
						"merchandiseId": "gid://shopify/ProductVariantMerchandise/" + b.MerchandiseID,
					},
					"destinationAddress": map[string]interface{}{
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
					"expectedTotalPrice": map[string]interface{}{
						"value": map[string]interface{}{
							"amount":       "0",
							"currencyCode": b.Currency,
						},
					},
				},
			},
		},
		"localization": map[string]interface{}{
			"country": b.Address.CountryCode,
		},
		"payment": map[string]interface{}{
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
		},
		"taxes": map[string]interface{}{
			"proposedTotalAmount": map[string]interface{}{
				"value": map[string]interface{}{
					"amount":       "0",
					"currencyCode": b.Currency,
				},
			},
		},
	}

	// Add payment method if included
	if includePayment && b.PaymentID != "" {
		variables["payment"].(map[string]interface{})["paymentLines"] = []map[string]interface{}{
			{
				"paymentMethod": map[string]interface{}{
					"paymentMethodIdentifier": b.PaymentID,
				},
				"directPaymentMethod": map[string]interface{}{
					"sessionId":    b.PaymentToken,
					"cardBrand":    "",
					"lastFourOnly": "",
				},
			},
		}
	}

	return variables
}

// BuildSubmitVariables builds variables for submit mutation
func (b *VariablesBuilder) BuildSubmitVariables(deliveryStrategy string, shippingAmount, taxAmount float64) map[string]interface{} {
	variables := b.BuildProposalVariables(true)

	// Update delivery strategy
	if deliveryLines, ok := variables["delivery"].(map[string]interface{})["deliveryLines"].([]map[string]interface{}); ok && len(deliveryLines) > 0 {
		deliveryLines[0]["selectedDeliveryStrategy"] = map[string]interface{}{
			"deliveryStrategyByHandle": map[string]interface{}{
				"handle":            deliveryStrategy,
				"customDeliveryRate": false,
			},
			"options": map[string]interface{}{},
		}
		deliveryLines[0]["targetMerchandiseLines"] = map[string]interface{}{
			"lines": []map[string]interface{}{
				{"stableId": b.StableID},
			},
		}
		deliveryLines[0]["expectedTotalPrice"] = map[string]interface{}{
			"value": map[string]interface{}{
				"amount":       formatAmount(shippingAmount),
				"currencyCode": b.Currency,
			},
		}
		deliveryLines[0]["destinationChanged"] = false
	}

	// Update tax amount
	if taxes, ok := variables["taxes"].(map[string]interface{}); ok {
		taxes["proposedTotalAmount"] = map[string]interface{}{
			"value": map[string]interface{}{
				"amount":       formatAmount(taxAmount),
				"currencyCode": b.Currency,
			},
		}
	}

	return variables
}

// formatAmount formats a float amount to string
func formatAmount(amount float64) string {
	return fmt.Sprintf("%.2f", amount)
}
