#!/usr/bin/env node
/**
 * User preferences management module.
 *
 * Provides CRUD operations for per-user preferences.
 * Other agent scripts require this module to read/write user preferences.
 *
 * API:
 *   getCurrentUser()                → userId string
 *   getUserConfigPath(userId)       → absolute path
 *   createUser(userId)              → void (clones default/)
 *   loadPreferences(userId)         → parsed YAML object
 *   savePreferences(userId, obj)    → void
 *   loadPostProduction(userId)      → parsed YAML object
 *   savePostProduction(userId, obj) → void
 *   loadPodcastProfile(userId)      → parsed YAML object
 *   savePodcastProfile(userId, obj) → void
 *   loadEditingRules(userId)        → merged rules (base + user overrides)
 *   saveEditingRule(userId, name, obj) → void
 *   loadLearningHistory(userId)     → JSON object
 *   appendLearningEvent(userId, event) → void
 *   loadEpisodeHistory(userId)      → JSON object
 *   appendEpisode(userId, episode)  → void
 *   listUsers()                     → string[]
 *   userExists(userId)              → boolean
 *
 * CLI:
 *   node user_manager.js [command] [args]
 *   node user_manager.js list
 *   node user_manager.js create <userId>
 *   node user_manager.js check <userId>
 */

const fs = require('fs');
const path = require('path');
const yaml = require('js-yaml');

// --- Path constants ---

const SKILL_DIR = path.resolve(__dirname, '..');
const CONFIG_DIR = path.join(SKILL_DIR, 'user-prefs');
const DEFAULT_DIR = path.join(CONFIG_DIR, 'default');
const BASE_RULES_DIR = path.join(SKILL_DIR, 'editing-rules');

// --- Helpers ---

function readYaml(filePath) {
  if (!fs.existsSync(filePath)) return null;
  const content = fs.readFileSync(filePath, 'utf8');
  return yaml.load(content);
}

function writeYaml(filePath, obj) {
  const content = yaml.dump(obj, {
    indent: 2,
    lineWidth: 120,
    noRefs: true,
    sortKeys: false,
    quotingType: '"',
    forceQuotes: false
  });
  fs.writeFileSync(filePath, content, 'utf8');
}

function readJson(filePath) {
  if (!fs.existsSync(filePath)) return null;
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function writeJson(filePath, obj) {
  fs.writeFileSync(filePath, JSON.stringify(obj, null, 2), 'utf8');
}

function copyDirRecursive(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDirRecursive(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

// --- Core API ---

/**
 * Get current user id.
 * Priority: env var PODCAST_EDIT_USER > "default"
 */
function getCurrentUser() {
  return process.env.PODCAST_EDIT_USER || process.env.PODCASTCUT_USER || 'default';
}

/**
 * Get absolute path to a user's preference directory.
 */
function getUserConfigPath(userId) {
  return path.join(CONFIG_DIR, userId || getCurrentUser());
}

/**
 * Check whether a user exists.
 */
function userExists(userId) {
  return fs.existsSync(getUserConfigPath(userId));
}

/**
 * List all users (excluding the "default" template).
 */
function listUsers() {
  if (!fs.existsSync(CONFIG_DIR)) return [];
  return fs.readdirSync(CONFIG_DIR, { withFileTypes: true })
    .filter(d => d.isDirectory() && d.name !== 'default')
    .map(d => d.name);
}

/**
 * Create a new user by cloning default/.
 */
function createUser(userId) {
  if (!userId || userId === 'default') {
    throw new Error('userId cannot be empty or "default"');
  }
  const userDir = getUserConfigPath(userId);
  if (fs.existsSync(userDir)) {
    throw new Error(`user "${userId}" already exists: ${userDir}`);
  }
  copyDirRecursive(DEFAULT_DIR, userDir);

  // Update metadata
  const prefs = loadPreferences(userId);
  if (prefs && prefs.meta) {
    prefs.meta.created_at = new Date().toISOString().slice(0, 10);
    prefs.meta.last_updated = new Date().toISOString().slice(0, 10);
    savePreferences(userId, prefs);
  }
  return userDir;
}

// --- Preferences read/write ---

function loadPreferences(userId) {
  const filePath = path.join(getUserConfigPath(userId), 'preferences.yaml');
  return readYaml(filePath);
}

function savePreferences(userId, obj) {
  const filePath = path.join(getUserConfigPath(userId), 'preferences.yaml');
  if (obj.meta) {
    obj.meta.last_updated = new Date().toISOString().slice(0, 10);
  }
  writeYaml(filePath, obj);
}

function loadPostProduction(userId) {
  const filePath = path.join(getUserConfigPath(userId), 'post_production.yaml');
  return readYaml(filePath);
}

function savePostProduction(userId, obj) {
  const filePath = path.join(getUserConfigPath(userId), 'post_production.yaml');
  if (obj.meta) {
    obj.meta.last_updated = new Date().toISOString().slice(0, 10);
  }
  writeYaml(filePath, obj);
}

function loadPodcastProfile(userId) {
  const filePath = path.join(getUserConfigPath(userId), 'podcast_profile.yaml');
  return readYaml(filePath);
}

function savePodcastProfile(userId, obj) {
  const filePath = path.join(getUserConfigPath(userId), 'podcast_profile.yaml');
  writeYaml(filePath, obj);
}

// --- Editing rules (two-tier merge) ---

/**
 * Load a user's editing rules (base rules + user overrides merged).
 *
 * Returns: { base_rules_dir, user_overrides: { [ruleName]: yamlObj }, has_overrides }
 *
 * Merge logic:
 * - Base rules live in editing-rules/ (markdown files), read directly by Claude.
 * - User overrides live in editing_rules/ (YAML files) under the user's prefs dir.
 * - When an override exists, its value wins.
 */
function loadEditingRules(userId) {
  const userRulesDir = path.join(getUserConfigPath(userId), 'editing_rules');
  const overrides = {};

  if (fs.existsSync(userRulesDir)) {
    for (const file of fs.readdirSync(userRulesDir)) {
      if (file.endsWith('.yaml') || file.endsWith('.yml')) {
        const name = path.basename(file, path.extname(file));
        overrides[name] = readYaml(path.join(userRulesDir, file));
      }
    }
  }

  return {
    base_rules_dir: BASE_RULES_DIR,
    user_overrides: overrides,
    has_overrides: Object.keys(overrides).length > 0
  };
}

/**
 * Save a single editing-rule override.
 */
function saveEditingRule(userId, ruleName, ruleObj) {
  const userRulesDir = path.join(getUserConfigPath(userId), 'editing_rules');
  fs.mkdirSync(userRulesDir, { recursive: true });
  const filePath = path.join(userRulesDir, `${ruleName}.yaml`);
  writeYaml(filePath, ruleObj);
}

// --- History ---

function loadLearningHistory(userId) {
  const filePath = path.join(getUserConfigPath(userId), 'learning_history.json');
  return readJson(filePath) || { version: '1.0', learning_events: [], preference_evolution: {} };
}

function appendLearningEvent(userId, event) {
  const history = loadLearningHistory(userId);
  event.date = event.date || new Date().toISOString().slice(0, 10);
  history.learning_events.push(event);
  const filePath = path.join(getUserConfigPath(userId), 'learning_history.json');
  writeJson(filePath, history);
}

function loadEpisodeHistory(userId) {
  const filePath = path.join(getUserConfigPath(userId), 'episode_history.json');
  return readJson(filePath) || { version: '1.0', episodes: [] };
}

function appendEpisode(userId, episode) {
  const history = loadEpisodeHistory(userId);
  episode.date = episode.date || new Date().toISOString().slice(0, 10);
  history.episodes.push(episode);
  const filePath = path.join(getUserConfigPath(userId), 'episode_history.json');
  writeJson(filePath, history);
}

// --- CLI ---

function printUsage() {
  console.log(`Usage: node user_manager.js <command> [args]

Commands:
  list                    list all users
  create <userId>         create a new user (clones default/)
  check [userId]          check a user's preference status
  prefs [userId]          print preferences as JSON
  rules [userId]          print editing-rules summary`);
}

if (require.main === module) {
  const [,, command, ...args] = process.argv;

  switch (command) {
    case 'list': {
      const users = listUsers();
      console.log(`Registered users (${users.length}):`, users.length ? users.join(', ') : '(none, only default)');
      break;
    }
    case 'create': {
      const userId = args[0];
      if (!userId) { console.error('missing userId'); process.exit(1); }
      const dir = createUser(userId);
      console.log(`✅ user "${userId}" created at: ${dir}`);
      break;
    }
    case 'check': {
      const userId = args[0] || getCurrentUser();
      if (!userExists(userId)) {
        console.log(JSON.stringify({ exists: false, needsOnboarding: true, userId }));
        process.exit(1);
      }
      const prefs = loadPreferences(userId);
      const isConfigured = prefs
        && prefs.audience && prefs.audience !== ''
        && prefs.purpose && prefs.purpose !== ''
        && prefs.duration && prefs.duration.target_minutes > 0;
      console.log(JSON.stringify({
        exists: true,
        needsOnboarding: !isConfigured,
        isConfigured,
        userId,
        configPath: getUserConfigPath(userId)
      }, null, 2));
      process.exit(isConfigured ? 0 : 1);
      break;
    }
    case 'prefs': {
      const userId = args[0] || getCurrentUser();
      console.log(JSON.stringify(loadPreferences(userId), null, 2));
      break;
    }
    case 'rules': {
      const userId = args[0] || getCurrentUser();
      const rules = loadEditingRules(userId);
      console.log(`Base rules dir: ${rules.base_rules_dir}`);
      console.log(`User overrides: ${rules.has_overrides ? Object.keys(rules.user_overrides).join(', ') : '(none)'}`);
      break;
    }
    default:
      printUsage();
      process.exit(command ? 1 : 0);
  }
}

// --- Exports ---

module.exports = {
  getCurrentUser,
  getUserConfigPath,
  userExists,
  listUsers,
  createUser,
  loadPreferences,
  savePreferences,
  loadPostProduction,
  savePostProduction,
  loadPodcastProfile,
  savePodcastProfile,
  loadEditingRules,
  saveEditingRule,
  loadLearningHistory,
  appendLearningEvent,
  loadEpisodeHistory,
  appendEpisode,
  // Path constants for use by other scripts
  SKILL_DIR,
  CONFIG_DIR,
  DEFAULT_DIR,
  BASE_RULES_DIR
};
