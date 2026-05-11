package utils

import (
	"fmt"
	"math/rand"
	"time"
)

// Address represents a physical address
type Address struct {
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

// AddressGenerator generates realistic addresses for testing
type AddressGenerator struct {
	rand *rand.Rand
}

// NewAddressGenerator creates a new address generator
func NewAddressGenerator() *AddressGenerator {
	return &AddressGenerator{
		rand: rand.New(rand.NewSource(time.Now().UnixNano())),
	}
}

// US address data
var usAddresses = []struct {
	city    string
	state   string
	zipBase string
}{
	{"New York", "NY", "10001"},
	{"Los Angeles", "CA", "90001"},
	{"Chicago", "IL", "60601"},
	{"Houston", "TX", "77001"},
	{"Phoenix", "AZ", "85001"},
	{"Philadelphia", "PA", "19019"},
	{"San Antonio", "TX", "78201"},
	{"San Diego", "CA", "92101"},
	{"Dallas", "TX", "75201"},
	{"San Jose", "CA", "95101"},
	{"Austin", "TX", "78701"},
	{"Jacksonville", "FL", "32099"},
	{"Fort Worth", "TX", "76101"},
	{"Columbus", "OH", "43004"},
	{"Indianapolis", "IN", "46201"},
	{"Charlotte", "NC", "28201"},
	{"Seattle", "WA", "98101"},
	{"Denver", "CO", "80201"},
	{"Boston", "MA", "02101"},
	{"Portland", "OR", "97201"},
}

var streets = []string{
	"Main St", "Oak Ave", "Maple Dr", "Pine Ln", "Cedar Blvd",
	"Elm St", "Washington Ave", "Park Pl", "Broadway", "Market St",
	"River Rd", "Lake Dr", "Hill St", "Valley View", "Forest Ave",
	"Spring St", "Summer Ln", "Winter Dr", "Autumn Way", "Church St",
}

var firstNames = []string{
	"John", "Jane", "Michael", "Sarah", "David", "Emily", "Robert", "Lisa",
	"William", "Mary", "James", "Patricia", "Thomas", "Jennifer", "Charles", "Linda",
	"Daniel", "Elizabeth", "Matthew", "Susan", "Joseph", "Jessica", "Christopher", "Karen",
}

var lastNames = []string{
	"Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
	"Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas",
	"Taylor", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White", "Harris",
}

// Generate generates a random address for a given country
func (g *AddressGenerator) Generate(countryCode string) *Address {
	switch countryCode {
	case "US":
		return g.generateUS()
	default:
		return g.generateUS() // Default to US
	}
}

// generateUS generates a random US address
func (g *AddressGenerator) generateUS() *Address {
	// Select random location
	loc := usAddresses[g.rand.Intn(len(usAddresses))]

	// Generate street address
	streetNum := 100 + g.rand.Intn(9900)
	street := streets[g.rand.Intn(len(streets))]
	address1 := fmt.Sprintf("%d %s", streetNum, street)

	// Optional apartment number
	address2 := ""
	if g.rand.Float32() < 0.3 { // 30% chance of having apt number
		address2 = fmt.Sprintf("Apt %d", 1+g.rand.Intn(999))
	}

	// Generate names
	firstName := firstNames[g.rand.Intn(len(firstNames))]
	lastName := lastNames[g.rand.Intn(len(lastNames))]

	// Generate phone (US format - plain 10 digits for Shopify compatibility)
	areaCode := 200 + g.rand.Intn(800)
	exchange := 200 + g.rand.Intn(800)
	subscriber := g.rand.Intn(10000)
	phone := fmt.Sprintf("%03d%03d%04d", areaCode, exchange, subscriber)

	return &Address{
		FirstName:   firstName,
		LastName:    lastName,
		Address1:    address1,
		Address2:    address2,
		City:        loc.city,
		State:       loc.state,
		PostalCode:  loc.zipBase,
		CountryCode: "US",
		Phone:       phone,
	}
}
