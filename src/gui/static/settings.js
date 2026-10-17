/* Settings: providers, API keys, and which model a session uses (DR-036).
 *
 * Pure renderers over the `/api/providers` payload, like inspector.js: they
 * return markup, and `app.js` owns the requests and the event handling. A key
 * is only ever typed into a password field and sent to the server; what comes
 * back, and what is drawn here, is its masked tail.
 */

/* Where to get a key, per hosted provider. Plain text, not a link: the page
 * itself reaches nothing on the network (DR-029). */
const KEY_SOURCES = {
  "ollama-cloud": "ollama.com/settings/keys",
};

function providerTitle(name) {
  const titles = {
    ollama: "Ollama",
    "ollama-cloud": "Ollama Cloud",
    "lm-studio": "LM Studio",
    vllm: "vLLM",
    "llama-cpp": "llama.cpp",
    "openai-compatible": "OpenAI-compatible",
  };
  return titles[name] || humanize(name);
}

function connectionState(connection) {
  if (connection.requires_api_key && !connection.has_key) {
    return { tone: "idle", text: "needs a key" };
  }
  if (!connection.reachable) {
    // A local runtime that is not running is a fact about this machine, not
    // a fault; a hosted one that does not answer is worth flagging.
    return connection.is_local ? { tone: "idle", text: "not running here" } : { tone: "bad", text: "not reachable" };
  }
  if (!connection.models.length) return { tone: "work", text: "running, no models" };
  return { tone: "good", text: `${connection.models.length} model${connection.models.length === 1 ? "" : "s"}` };
}

function settingsMarkup(payload, current) {
  if (!payload) return `<p class="panel-note">Looking for providers…</p>`;
  const fallback = payload.default || {};
  const defaultLine = fallback.model
    ? `New sessions start on <strong>${escapeHtml(fallback.model)}</strong> via ${escapeHtml(providerTitle(fallback.provider))}.`
    : "No default saved: new sessions use whatever AutoSE finds running on this machine.";
  // What can be used first: the one in use, then working ones, then ones a
  // key would unlock, then runtimes that are simply not running here.
  const rank = (connection) =>
    current && connection.provider === current.provider
      ? 0
      : connection.usable
        ? 1
        : connection.requires_api_key && !connection.has_key
          ? 2
          : 3;
  const ordered = [...(payload.providers || [])].sort((a, b) => rank(a) - rank(b));
  const cards = ordered.map((connection) => providerCardMarkup(connection, current)).join("");
  return (
    `<p class="panel-note">${defaultLine} Keys are stored only in <span class="mono">${escapeHtml(payload.credentials_file || "")}</span>, readable by you alone, and never sent back to this page.</p>` +
    `<div class="provider-list">${cards}</div>`
  );
}

function providerCardMarkup(connection, current) {
  const name = connection.provider;
  const state = connectionState(connection);
  const inUse = current && current.provider === name;
  const keyRow =
    connection.requires_api_key || connection.has_key || name === "openai-compatible"
      ? `<div class="provider-key">` +
        (connection.has_key
          ? `<span class="key-chip">key ${escapeHtml(connection.masked_key)}${connection.key_source && connection.key_source !== "stored" ? ` · from ${escapeHtml(connection.key_source)}` : ""}</span>`
          : `<span class="key-chip missing">no key</span>`) +
        `<input type="password" autocomplete="off" spellcheck="false" placeholder="${connection.has_key ? "Replace key" : "Paste API key"}" data-key-input="${escapeHtml(name)}" />` +
        `<button type="button" class="ghost-button small" data-provider-action="save_key" data-provider="${escapeHtml(name)}">Save key</button>` +
        (connection.has_key && connection.key_source === "stored"
          ? `<button type="button" class="ghost-button small" data-provider-action="delete_key" data-provider="${escapeHtml(name)}">Remove</button>`
          : "") +
        (KEY_SOURCES[name] ? `<div class="provider-hint">Create a key at <span class="mono">${escapeHtml(KEY_SOURCES[name])}</span></div>` : "") +
        `</div>`
      : "";
  const models = connection.models || [];
  const modelRow = models.length
    ? `<div class="provider-models">` +
      `<select data-model-input="${escapeHtml(name)}">` +
      models
        .map(
          (model) =>
            `<option value="${escapeHtml(model.name)}"${inUse && current.model === model.name ? " selected" : ""}>${escapeHtml(model.name)}${model.parameter_size ? ` · ${escapeHtml(model.parameter_size)}` : ""}${model.supports_tools === false ? " · no tools" : ""}</option>`
        )
        .join("") +
      `</select>` +
      `<button type="button" class="primary-button small" data-provider-action="use" data-provider="${escapeHtml(name)}">Use in this session</button>` +
      `<button type="button" class="ghost-button small" data-provider-action="set_default" data-provider="${escapeHtml(name)}">Make default</button>` +
      `</div>`
    : "";
  return (
    `<div class="provider-card${inUse ? " in-use" : ""}">` +
    `<div class="provider-head"><span class="provider-name">${escapeHtml(providerTitle(name))}</span>` +
    `<span class="provider-kind">${connection.is_local ? "on this machine" : "hosted"}</span>` +
    (inUse ? `<span class="choice-tag now">in use</span>` : "") +
    `<span class="provider-state tone-${state.tone}">${escapeHtml(state.text)}</span></div>` +
    `<div class="provider-desc">${escapeHtml(connection.description || "")}</div>` +
    `<div class="provider-url mono">${escapeHtml(connection.base_url || "")}</div>` +
    // A local runtime's connection-refused error says nothing the state does not.
    (connection.error &&
    !(connection.is_local && !connection.reachable) &&
    !(connection.requires_api_key && !connection.has_key)
      ? `<div class="provider-error">${escapeHtml(connection.error)}</div>`
      : "") +
    keyRow +
    modelRow +
    `</div>`
  );
}

/* The header picker: every usable provider's models, grouped (DR-036). The
 * value carries both halves, because a model name alone is ambiguous across
 * providers. */
function modelPickerMarkup(payload, current) {
  const groups = ((payload && payload.providers) || []).filter((connection) => connection.models.length);
  if (!groups.length) {
    return current && current.model
      ? `<option value="${escapeHtml(`${current.provider}|${current.model}`)}" selected>${escapeHtml(current.model)}</option>`
      : `<option value="">no models</option>`;
  }
  return groups
    .map(
      (connection) =>
        `<optgroup label="${escapeHtml(providerTitle(connection.provider))}">` +
        connection.models
          .map((model) => {
            const value = `${connection.provider}|${model.name}`;
            const selected = current && current.provider === connection.provider && current.model === model.name;
            return `<option value="${escapeHtml(value)}"${selected ? " selected" : ""}>${escapeHtml(model.name)}</option>`;
          })
          .join("") +
        `</optgroup>`
    )
    .join("");
}
