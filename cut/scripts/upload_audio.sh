#!/bin/bash
#
# Multi-service fallback uploader.
# Tries free file-hosting services in order until one succeeds.
#

set -e

if [ -z "$1" ]; then
    echo "Usage: bash upload_audio.sh <audio-file>"
    exit 1
fi

AUDIO_FILE="$1"

if [ ! -f "$AUDIO_FILE" ]; then
    echo "❌ Error: file not found: $AUDIO_FILE"
    exit 1
fi

FILE_SIZE=$(du -h "$AUDIO_FILE" | cut -f1)
echo "📤 Preparing to upload"
echo "   file: $AUDIO_FILE"
echo "   size: $FILE_SIZE"
echo ""

# Try 1: uguu.se (fast, 48-hour retention, max 100MB)
echo "🔄 [1/5] Trying uguu.se (48h retention)..."
RESPONSE=$(curl -s --max-time 120 -F "files[]=@$AUDIO_FILE" https://uguu.se/upload.php 2>&1 || echo "")
URL=$(echo "$RESPONSE" | grep -o '"url":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")
if [[ "$URL" =~ ^https?:// ]]; then
    echo "✅ Upload succeeded!"
    echo "$URL"
    echo "$URL" > audio_url.txt
    echo ""
    echo "   URL saved to: audio_url.txt"
    exit 0
fi
echo "   ❌ Failed"
echo ""

# Try 2: 0x0.st (large files, 48h retention)
echo "🔄 [2/5] Trying 0x0.st (48h retention)..."
RESPONSE=$(curl -s -F "file=@$AUDIO_FILE" https://0x0.st 2>&1 || echo "")
if [[ "$RESPONSE" =~ ^https?:// ]]; then
    echo "✅ Upload succeeded!"
    echo "$RESPONSE"
    echo "$RESPONSE" > audio_url.txt
    echo ""
    echo "   URL saved to: audio_url.txt"
    exit 0
fi
echo "   ❌ Failed"
echo ""

# Try 3: file.io (single-use download)
echo "🔄 [3/5] Trying file.io (single-use)..."
RESPONSE=$(curl -s -F "file=@$AUDIO_FILE" https://file.io 2>&1 || echo "")
URL=$(echo "$RESPONSE" | grep -o '"link":"[^"]*"' | cut -d'"' -f4 || echo "")
if [[ "$URL" =~ ^https?:// ]]; then
    echo "✅ Upload succeeded!"
    echo "$URL"
    echo "$URL" > audio_url.txt
    echo ""
    echo "   URL saved to: audio_url.txt"
    echo "   ⚠️  Note: this link can only be downloaded once"
    exit 0
fi
echo "   ❌ Failed"
echo ""

# Try 4: tmpfiles.org (24h retention)
echo "🔄 [4/5] Trying tmpfiles.org (24h retention)..."
RESPONSE=$(curl -s -F "file=@$AUDIO_FILE" https://tmpfiles.org/api/v1/upload 2>&1 || echo "")
URL=$(echo "$RESPONSE" | grep -o '"url":"[^"]*"' | cut -d'"' -f4 | sed 's/tmpfiles.org\//tmpfiles.org\/dl\//' || echo "")
if [[ "$URL" =~ ^https?:// ]]; then
    echo "✅ Upload succeeded!"
    echo "$URL"
    echo "$URL" > audio_url.txt
    echo ""
    echo "   URL saved to: audio_url.txt"
    exit 0
fi
echo "   ❌ Failed"
echo ""

# Try 5: catbox.moe (permanent, max 200MB)
echo "🔄 [5/5] Trying catbox.moe (permanent)..."
RESPONSE=$(curl -s -F "reqtype=fileupload" -F "fileToUpload=@$AUDIO_FILE" https://catbox.moe/user/api.php 2>&1 || echo "")
if [[ "$RESPONSE" =~ ^https?://.*catbox.moe.* ]]; then
    echo "✅ Upload succeeded!"
    echo "$RESPONSE"
    echo "$RESPONSE" > audio_url.txt
    echo ""
    echo "   URL saved to: audio_url.txt"
    exit 0
fi
echo "   ❌ Failed"
echo ""

echo "❌ All upload services failed"
echo ""
echo "💡 Alternatives:"
echo "   1. Configure Aliyun OSS (see SKILL.md)"
echo "   2. Expose a local file with ngrok"
echo "   3. Upload manually to a cloud drive and use the share link"
echo ""
exit 1
