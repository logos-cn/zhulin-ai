const WRITER_GENERATION_EFFECT_KEY = "bamboo_writer_generation_effect_enabled";

function readBooleanPreference(key, defaultValue = true) {
  const raw = window.localStorage.getItem(key);
  if (raw === null) return defaultValue;
  return !["0", "false", "off", "no"].includes(String(raw).trim().toLowerCase());
}

export function isWriterGenerationEffectEnabled() {
  return readBooleanPreference(WRITER_GENERATION_EFFECT_KEY, true);
}

export function setWriterGenerationEffectEnabled(enabled) {
  window.localStorage.setItem(WRITER_GENERATION_EFFECT_KEY, enabled ? "1" : "0");
  return enabled;
}
