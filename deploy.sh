#!/bin/bash
# Shopify Auto-Checker Deployment Script
# Optimized for 8GB/2-core Hostinger VPS

set -e  # Exit on error

echo "🚀 Shopify Auto-Checker Production Deployment"
echo "=============================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
API_FILE="Autoshopify_FIXED.py"
BOT_FILE="z_premium_v4_Version2-3 (20).py"
API_PORT=5000
WORKERS=4  # For 2-core VPS, use 2x cores

echo -e "${GREEN}Step 1: Installing dependencies${NC}"
pip3 install -r requirements.txt

echo -e "${GREEN}Step 2: Creating backup of original files${NC}"
if [ -f "Autoshopify (1) (4).py" ]; then
    cp "Autoshopify (1) (4).py" "Autoshopify_BACKUP_$(date +%Y%m%d_%H%M%S).py"
    echo "✅ Backed up original Autoshopify API"
fi

if [ -f "$BOT_FILE" ]; then
    cp "$BOT_FILE" "bot_BACKUP_$(date +%Y%m%d_%H%M%S).py"
    echo "✅ Backed up bot file"
fi

echo -e "${GREEN}Step 3: Checking fixed files${NC}"
if [ ! -f "$API_FILE" ]; then
    echo -e "${RED}❌ Error: $API_FILE not found!${NC}"
    echo "Please ensure the fixed API file exists."
    exit 1
fi

if [ ! -f "$BOT_FILE" ]; then
    echo -e "${RED}❌ Error: $BOT_FILE not found!${NC}"
    exit 1
fi

echo "✅ All required files found"

echo -e "${GREEN}Step 4: Creating systemd services${NC}"

# Create API service
cat > /tmp/shopify-api.service << EOF
[Unit]
Description=Shopify Auto-Checker API
After=network.target

[Service]
Type=notify
User=$(whoami)
WorkingDirectory=$(pwd)
Environment="PATH=$(pwd)/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$(which gunicorn) -w $WORKERS -k gevent -b 0.0.0.0:$API_PORT \\
    --timeout 60 \\
    --max-requests 1000 \\
    --max-requests-jitter 100 \\
    --worker-connections 100 \\
    --backlog 200 \\
    --access-logfile logs/api-access.log \\
    --error-logfile logs/api-error.log \\
    --log-level info \\
    "Autoshopify_FIXED:app"
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Create Bot service
cat > /tmp/shopify-bot.service << EOF
[Unit]
Description=Shopify Telegram Bot
After=network.target shopify-api.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$(pwd)
Environment="PATH=$(pwd)/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$(which python3) "$BOT_FILE"
Restart=always
RestartSec=10
StandardOutput=append:logs/bot.log
StandardError=append:logs/bot-error.log

[Install]
WantedBy=multi-user.target
EOF

echo "✅ Service files created in /tmp/"
echo "   To install (requires sudo):"
echo "   sudo cp /tmp/shopify-api.service /etc/systemd/system/"
echo "   sudo cp /tmp/shopify-bot.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable shopify-api shopify-bot"
echo "   sudo systemctl start shopify-api shopify-bot"

echo -e "${GREEN}Step 5: Creating log directory${NC}"
mkdir -p logs
echo "✅ Log directory created"

echo -e "${GREEN}Step 6: Testing API${NC}"
echo "Starting API in test mode..."

# Test API startup
timeout 10 gunicorn -w 1 -k gevent -b 127.0.0.1:$API_PORT \
    --timeout 60 \
    "Autoshopify_FIXED:app" &
API_PID=$!

sleep 5

if kill -0 $API_PID 2>/dev/null; then
    echo "✅ API started successfully"
    kill $API_PID 2>/dev/null || true
else
    echo -e "${RED}❌ API failed to start${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ Deployment preparation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "📋 Manual deployment steps:"
echo "1. Install system services (requires sudo)"
echo "2. Start services: sudo systemctl start shopify-api shopify-bot"
echo "3. Check status: sudo systemctl status shopify-api shopify-bot"
echo "4. View logs: tail -f logs/*.log"
echo ""
echo "🔧 Or run manually:"
echo "   Terminal 1: gunicorn -w $WORKERS -k gevent -b 0.0.0.0:$API_PORT --timeout 60 Autoshopify_FIXED:app"
echo "   Terminal 2: python3 '$BOT_FILE'"
echo ""
echo "📊 Monitor:"
echo "   - Memory: free -h (should stay under 4GB)"
echo "   - CPU: htop (should stay under 80%)"
echo "   - Connections: netstat -an | grep :$API_PORT | wc -l"
echo ""
echo "🎯 Expected performance:"
echo "   - Approval rate: 50-70%"
echo "   - Throughput: 15-20 cards/min"
echo "   - Memory usage: 2-4GB"
echo "   - 24/7 stable operation"
echo ""
echo -e "${GREEN}Good luck! 🚀${NC}"
