#!/usr/bin/env node
/**
 * Podcast link parser.
 *
 * Extracts podcast metadata from a Xiaoyuzhou or Apple Podcasts link
 * and writes the result into the user's podcast_profile.yaml.
 *
 * Usage:
 *   node parse_podcast_link.js <url> [userId]
 *
 * Supported link formats:
 *   - Xiaoyuzhou:     https://www.xiaoyuzhoufm.com/podcast/xxx
 *   - Apple Podcasts: https://podcasts.apple.com/xx/podcast/xxx/idNNN
 */

const https = require('https');
const http = require('http');
const UserManager = require('./user_manager');

function detectPlatform(url) {
  if (/xiaoyuzhoufm\.com/.test(url)) return 'xiaoyuzhou';
  if (/podcasts\.apple\.com/.test(url)) return 'apple';
  if (/itunes\.apple\.com/.test(url)) return 'apple';
  return null;
}

function fetchUrl(url) {
  return new Promise((resolve, reject) => {
    const client = url.startsWith('https') ? https : http;
    const req = client.get(url, { headers: { 'User-Agent': 'Mozilla/5.0' } }, (res) => {
      // Follow redirects
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return fetchUrl(res.headers.location).then(resolve).catch(reject);
      }
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(data));
    });
    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('Request timeout')); });
  });
}

async function parseXiaoyuzhou(url) {
  const html = await fetchUrl(url);

  const result = {
    link: url,
    platform: 'xiaoyuzhou',
    name: '',
    description: '',
    theme: [],
    audience: '',
    style: ''
  };

  const titleMatch = html.match(/<meta\s+property="og:title"\s+content="([^"]+)"/i)
    || html.match(/<title>([^<]+)<\/title>/i);
  if (titleMatch) result.name = titleMatch[1].trim();

  const descMatch = html.match(/<meta\s+property="og:description"\s+content="([^"]+)"/i)
    || html.match(/<meta\s+name="description"\s+content="([^"]+)"/i);
  if (descMatch) result.description = descMatch[1].trim();

  // Try Next.js __NEXT_DATA__ for richer info
  const nextDataMatch = html.match(/<script id="__NEXT_DATA__"[^>]*>(.+?)<\/script>/s);
  if (nextDataMatch) {
    try {
      const nextData = JSON.parse(nextDataMatch[1]);
      const podcast = nextData?.props?.pageProps?.podcast
        || nextData?.props?.pageProps?.podcastData;
      if (podcast) {
        result.name = podcast.title || result.name;
        result.description = podcast.description || result.description;
        if (podcast.episodeCount) result.episodes_analyzed = 0;
      }
    } catch (e) {
      // JSON parse failure — fall back to og: meta
    }
  }

  return result;
}

async function parseApplePodcasts(url) {
  const idMatch = url.match(/id(\d+)/);
  if (!idMatch) throw new Error('Cannot extract ID from Apple Podcasts URL');

  const podcastId = idMatch[1];

  const apiUrl = `https://itunes.apple.com/lookup?id=${podcastId}&entity=podcast`;
  const response = await fetchUrl(apiUrl);
  const data = JSON.parse(response);

  if (!data.results || data.results.length === 0) {
    throw new Error(`Apple Podcasts: no podcast found for id ${podcastId}`);
  }

  const podcast = data.results[0];

  return {
    link: url,
    platform: 'apple',
    name: podcast.collectionName || podcast.trackName || '',
    description: podcast.description || '',
    theme: podcast.genres || [],
    audience: '',
    style: '',
    _raw: {
      artist: podcast.artistName,
      feedUrl: podcast.feedUrl,
      trackCount: podcast.trackCount,
      artworkUrl: podcast.artworkUrl600 || podcast.artworkUrl100
    }
  };
}

async function parsePodcastLink(url, userId) {
  const platform = detectPlatform(url);
  if (!platform) {
    throw new Error(`Unsupported link format: ${url}\nSupported: Xiaoyuzhou (xiaoyuzhoufm.com), Apple Podcasts (podcasts.apple.com)`);
  }

  let profile;
  if (platform === 'xiaoyuzhou') {
    profile = await parseXiaoyuzhou(url);
  } else if (platform === 'apple') {
    profile = await parseApplePodcasts(url);
  }

  const rawData = profile._raw;
  delete profile._raw;

  profile.episodes_analyzed = profile.episodes_analyzed || 0;

  // Persist to user prefs
  if (userId) {
    UserManager.savePodcastProfile(userId, profile);
    console.error(`✅ Saved to ${UserManager.getUserConfigPath(userId)}/podcast_profile.yaml`);
  }

  return { profile, rawData };
}

async function main() {
  const url = process.argv[2];
  const userId = process.argv[3] || UserManager.getCurrentUser();

  if (!url) {
    console.log(`Usage: node parse_podcast_link.js <url> [userId]

Supported:
  - Xiaoyuzhou:     https://www.xiaoyuzhoufm.com/podcast/xxx
  - Apple Podcasts: https://podcasts.apple.com/xx/podcast/xxx/idNNN

Example:
  node parse_podcast_link.js "https://www.xiaoyuzhoufm.com/podcast/abc123" alice`);
    process.exit(1);
  }

  try {
    const { profile, rawData } = await parsePodcastLink(url, userId);
    console.log(JSON.stringify(profile, null, 2));
    if (rawData) {
      console.error('\nAdditional info (not written to YAML):');
      console.error(JSON.stringify(rawData, null, 2));
    }
  } catch (error) {
    console.error(`❌ Parse failed: ${error.message}`);
    process.exit(1);
  }
}

if (require.main === module) {
  main();
}

module.exports = { parsePodcastLink, detectPlatform };
