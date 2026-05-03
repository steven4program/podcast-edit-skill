#!/bin/bash
#
# Aliyun FunASR transcription script (for podcast editing).
# Usage: bash aliyun_funasr_transcribe.sh <audio-url> <speaker-count>
#

set -e

# Auto-load .env if DASHSCOPE_API_KEY is not set.
# Tries SKILL_DIR-relative .env, then user home, then current dir.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_CANDIDATES=(
  "${SKILL_DIR:-}/.env"
  "$SCRIPT_DIR/../../.env"
  "$HOME/podcast-edit-skill/.env"
  "$PWD/.env"
)
if [ -z "$DASHSCOPE_API_KEY" ]; then
  for env_file in "${ENV_CANDIDATES[@]}"; do
    if [ -f "$env_file" ]; then
      export $(grep -v '^#' "$env_file" | grep -v '^$' | xargs)
      break
    fi
  done
fi

if [ -z "$1" ]; then
    echo "❌ Error: please provide an audio URL"
    echo ""
    echo "Usage: bash aliyun_funasr_transcribe.sh <audio-url> <speaker-count>"
    echo "Example: bash aliyun_funasr_transcribe.sh \"https://example.com/audio.mp3\" 3"
    exit 1
fi

AUDIO_URL="$1"
SPEAKER_COUNT="${2:-2}"

if [ -z "$DASHSCOPE_API_KEY" ]; then
    echo "❌ Error: DASHSCOPE_API_KEY env var is not set"
    echo ""
    echo "Set it via:"
    echo "  export DASHSCOPE_API_KEY='your-api-key'"
    echo ""
    echo "Or place it in <repo-root>/.env"
    exit 1
fi

API_KEY="$DASHSCOPE_API_KEY"

echo "🎤 Submitting Aliyun FunASR transcription task"
echo "   audio URL:    $AUDIO_URL"
echo "   speaker count: $SPEAKER_COUNT"
echo ""

RESPONSE=$(curl -s -X POST "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-DashScope-Async: enable" \
  -d '{
    "model": "fun-asr",
    "input": {
      "file_urls": ["'"$AUDIO_URL"'"]
    },
    "parameters": {
      "diarization_enabled": true,
      "speaker_count": '$SPEAKER_COUNT',
      "channel_id": [0]
    }
  }')

TASK_ID=$(echo "$RESPONSE" | grep -o '"task_id":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TASK_ID" ]; then
  echo "❌ Submission failed"
  echo "$RESPONSE"
  exit 1
fi

echo "✅ Task submitted"
echo "   task ID: $TASK_ID"
echo ""
echo "⏳ Waiting for transcription to finish (typically 3–15 min)..."

ATTEMPT=0
MAX_ATTEMPTS=300  # max 25 minutes

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  sleep 5
  ATTEMPT=$((ATTEMPT + 1))

  QUERY_RESPONSE=$(curl -s -X GET "https://dashscope.aliyuncs.com/api/v1/tasks/$TASK_ID" \
    -H "Authorization: Bearer $API_KEY")

  STATUS=$(echo "$QUERY_RESPONSE" | grep -o '"task_status":"[^"]*"' | cut -d'"' -f4)

  if [ "$STATUS" = "SUCCEEDED" ]; then
    echo ""
    echo "✅ Transcription complete!"

    echo "$QUERY_RESPONSE" > aliyun_funasr_result.json
    echo "   API response saved: aliyun_funasr_result.json"

    TRANSCRIPTION_URL=$(echo "$QUERY_RESPONSE" | grep -o '"transcription_url":"[^"]*"' | cut -d'"' -f4)

    if [ -n "$TRANSCRIPTION_URL" ]; then
      echo "   Downloading transcription content..."
      curl -s "$TRANSCRIPTION_URL" > aliyun_funasr_transcription.json
      echo "   Transcription saved: aliyun_funasr_transcription.json"

      SENTENCE_COUNT=$(grep -o '"sentence_id"' aliyun_funasr_transcription.json | wc -l | tr -d ' ')
      echo ""
      echo "📊 Transcription stats:"
      echo "   Total sentences: $SENTENCE_COUNT"

      node << 'EOF'
const data = require('./aliyun_funasr_transcription.json');
const sentences = data.transcripts[0].sentences;
const speakers = {};
sentences.forEach(s => {
  speakers[s.speaker_id] = (speakers[s.speaker_id] || 0) + 1;
});
console.log('   Speaker distribution:');
Object.keys(speakers).sort().forEach(spk => {
  const count = speakers[spk];
  const pct = (count / sentences.length * 100).toFixed(1);
  console.log(`     Speaker ${spk}: ${count} sentences (${pct}%)`);
});
EOF
    fi

    exit 0

  elif [ "$STATUS" = "FAILED" ]; then
    echo ""
    echo "❌ Transcription failed"
    echo "$QUERY_RESPONSE"
    exit 1
  else
    if [ $((ATTEMPT % 12)) -eq 0 ]; then
      echo "   Processing... ($((ATTEMPT * 5))s elapsed) status: $STATUS"
    else
      echo -n "."
    fi
  fi
done

echo ""
echo "❌ Timed out (waited $((MAX_ATTEMPTS * 5))s)"
exit 1
