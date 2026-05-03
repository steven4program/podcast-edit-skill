#!/usr/bin/env node
/**
 * Check whether a user has completed onboarding.
 *
 * Usage:
 *   node check_preferences.js [userId]
 *
 * Output: JSON-formatted result.
 * Exit code: 0 = configured, 1 = needs onboarding.
 */

const UserManager = require('./user_manager');

function checkUser(userId) {
  if (!UserManager.userExists(userId)) {
    return {
      exists: false,
      needsOnboarding: true,
      format: 'yaml',
      userId,
      reason: `user "${userId}" does not exist`
    };
  }

  const prefs = UserManager.loadPreferences(userId);
  if (!prefs) {
    return {
      exists: true,
      needsOnboarding: true,
      format: 'yaml',
      userId,
      reason: 'preferences.yaml could not be read'
    };
  }

  const checks = {
    hasAudience: !!prefs.audience && prefs.audience !== '',
    hasPurpose: !!prefs.purpose && prefs.purpose !== '',
    hasTargetDuration: !!(prefs.duration && prefs.duration.target_minutes > 0),
    hasContentAnalysis: !!(prefs.content_analysis && prefs.content_analysis.enabled),
    hasTechOptimization: !!(prefs.technical && prefs.technical.enabled)
  };

  const isConfigured = checks.hasAudience && checks.hasPurpose && checks.hasTargetDuration;
  const rules = UserManager.loadEditingRules(userId);

  return {
    exists: true,
    needsOnboarding: !isConfigured,
    isConfigured,
    format: 'yaml',
    userId,
    configPath: UserManager.getUserConfigPath(userId),
    hasEditingOverrides: rules.has_overrides,
    checks,
    reason: isConfigured ? 'preferences configured' : 'preferences incomplete'
  };
}

const userId = process.argv[2] || UserManager.getCurrentUser();
const result = checkUser(userId);

console.log(JSON.stringify(result, null, 2));
process.exit(result.needsOnboarding ? 1 : 0);
