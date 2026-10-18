/* Schedules and watchers (DR-047).
 *
 * Pure renderers over the `/api/automation` payload, like settings.js: they
 * return markup, and `app.js` owns the requests and the event handling.
 * A schedule runs a prompt on a cadence; a watcher runs a command the person
 * typed and starts a prompt when its output changes or it starts failing.
 */

function formatUntil(timestamp) {
  if (!timestamp) return "";
  const minutes = Math.round((Number(timestamp) * 1000 - Date.now()) / 60000);
  if (minutes < 1) return "due now";
  if (minutes < 60) return `in ${minutes}m`;
  const then = new Date(Number(timestamp) * 1000);
  const time = then.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (minutes < 1440) return `at ${time}`;
  if (minutes < 7 * 1440) return `${then.toLocaleDateString([], { weekday: "short" })} ${time}`;
  return `${then.toLocaleDateString()} ${time}`;
}

function tierOptionsMarkup(tiers, chosen) {
  const options = [{ value: "", label: "Auto" }, ...tiers.map((tier) => ({ value: tier, label: humanize(tier) }))];
  return options
    .map(
      (option) =>
        `<option value="${escapeHtml(option.value)}"${option.value === (chosen || "") ? " selected" : ""}>${escapeHtml(option.label)}</option>`
    )
    .join("");
}

function automationMarkup(payload, view, tiers) {
  if (!payload) return `<p class="panel-note">Reading schedules and watchers…</p>`;
  const approvals =
    payload.unattended_approvals === "auto"
      ? `<p class="callout warn">This dashboard was started with <span class="mono">--yes</span>, so shell and network calls in these runs go ahead without asking (${escapeHtml(payload.authorized_by)}).</p>`
      : `<p class="panel-note">Each run opens its own session. Nobody is there to answer, so its shell and network calls are refused, and the run says so. Start the dashboard with <span class="mono">--yes</span> to let them through.</p>`;
  const stopped = payload.running
    ? ""
    : `<p class="callout bad">Nothing is acting on these: the automation runner is not running.</p>`;
  const problems = (payload.problems || []).map((problem) => `<p class="callout bad">${escapeHtml(problem)}</p>`).join("");
  return (
    approvals +
    stopped +
    problems +
    sectionMarkup("Schedules", "schedule", payload.schedules || [], view, tiers) +
    sectionMarkup("Watchers", "watcher", payload.watchers || [], view, tiers) +
    `<p class="panel-note panel-foot">Kept in <span class="mono">${escapeHtml(payload.file || "")}</span>.</p>`
  );
}

function sectionMarkup(title, kind, items, view, tiers) {
  const editing = view.editing && view.editing.kind === kind ? view.editing : null;
  const empty =
    kind === "schedule"
      ? "No schedules. A schedule runs a prompt every so many minutes, or on a cron expression."
      : "No watchers. A watcher runs a command you write, such as a CI status check, and starts a prompt when its output changes or it starts failing.";
  const cards = items.map((item) =>
    editing && editing.id === item.id ? formMarkup(kind, item, tiers) : (kind === "schedule" ? scheduleCard : watcherCard)(item)
  );
  const adding = editing && !editing.id ? formMarkup(kind, null, tiers) : "";
  return (
    `<div class="auto-section"><div class="auto-section-head"><h3>${escapeHtml(title)}</h3>` +
    (adding || !cards.length ? "" : `<button type="button" class="ghost-button small" data-auto-new="${kind}">Add ${kind}</button>`) +
    `</div>` +
    (cards.length || adding
      ? cards.join("") + adding
      : `<div class="empty-box"><p>${escapeHtml(empty)}</p><button type="button" class="ghost-button small" data-auto-new="${kind}">Add ${kind}</button></div>`) +
    `</div>`
  );
}

function lastMarkup(item) {
  const parts = [];
  if (item.last_run_at || item.last_triggered_at) {
    parts.push(`last ran ${formatWhen(item.last_run_at || item.last_triggered_at)}`);
  }
  if (item.last_outcome) parts.push(item.last_outcome);
  return parts.join(" · ");
}

function scheduleCard(item) {
  const next = item.enabled ? formatUntil(item.next_run_at) : "off";
  return (
    `<div class="auto-card${item.enabled ? "" : " off"}">` +
    `<div class="auto-head"><span class="auto-name">${escapeHtml(item.name)}</span>` +
    `<span class="auto-when">${escapeHtml(item.every_text)} · ${escapeHtml(next)}</span></div>` +
    `<div class="auto-prompt">${escapeHtml(item.prompt)}</div>` +
    `<div class="auto-meta">${escapeHtml([item.tier ? humanize(item.tier) : "auto tier", lastMarkup(item)].filter(Boolean).join(" · "))}</div>` +
    actionsMarkup("schedule", item, `<button type="button" class="ghost-button small" data-auto-action="run_now" data-id="${escapeHtml(item.id)}">Run now</button>`) +
    `</div>`
  );
}

function watcherCard(item) {
  const next = !item.enabled ? "off" : !item.trusted ? "waiting for you" : formatUntil(item.next_check_at);
  const trigger = item.trigger === "failure" ? "when it starts failing" : "when its output changes";
  const lastCheck = item.last_checked_at
    ? `checked ${formatWhen(item.last_checked_at)}${item.last_exit_code === null || item.last_exit_code === undefined ? "" : `, exit ${item.last_exit_code}`}`
    : "not checked yet";
  const trust = item.trusted
    ? ""
    : `<div class="auto-trust">This command came with the repository's files and has not been saved on this machine, so it will not run. Read it, then allow it.` +
      `<button type="button" class="primary-button small" data-auto-action="trust" data-id="${escapeHtml(item.id)}">Allow this command</button></div>`;
  return (
    `<div class="auto-card${item.enabled ? "" : " off"}${item.trusted ? "" : " untrusted"}">` +
    `<div class="auto-head"><span class="auto-name">${escapeHtml(item.name)}</span>` +
    `<span class="auto-when">every ${item.every_minutes}m · ${escapeHtml(next)}</span></div>` +
    `<div class="auto-command mono">$ ${escapeHtml(item.command)}</div>` +
    `<div class="auto-prompt">${escapeHtml(trigger)}: ${escapeHtml(item.prompt_template)}</div>` +
    `<div class="auto-meta">${escapeHtml([item.tier ? humanize(item.tier) : "auto tier", lastCheck, lastMarkup(item), item.pending ? "a run is waiting for room" : ""].filter(Boolean).join(" · "))}</div>` +
    trust +
    actionsMarkup("watcher", item, item.trusted ? `<button type="button" class="ghost-button small" data-auto-action="check_now" data-id="${escapeHtml(item.id)}">Check now</button>` : "") +
    `</div>`
  );
}

function actionsMarkup(kind, item, extra) {
  const id = escapeHtml(item.id);
  return (
    `<div class="approval-actions">` +
    `<button type="button" class="ghost-button small" data-auto-action="delete_${kind}" data-id="${id}">Delete</button>` +
    `<button type="button" class="ghost-button small" data-auto-toggle="${kind}" data-id="${id}" data-enabled="${item.enabled}">${item.enabled ? "Pause" : "Resume"}</button>` +
    `<button type="button" class="ghost-button small" data-auto-edit="${kind}" data-id="${id}">Edit</button>` +
    extra +
    `</div>`
  );
}

function formMarkup(kind, item, tiers) {
  const value = (key, fallback = "") => escapeHtml(item && item[key] !== undefined && item[key] !== null ? item[key] : fallback);
  const id = item ? escapeHtml(item.id) : "";
  const fields =
    kind === "schedule"
      ? `<label>Name<input data-field="name" value="${value("name")}" placeholder="Nightly dependency check" /></label>` +
        `<label>Every<input data-field="every" value="${value("every", 1440)}" placeholder="minutes, or a cron line like 0 9 * * 1-5" /></label>` +
        `<label class="wide">Prompt<textarea data-field="prompt" rows="3" placeholder="What the run should do">${value("prompt")}</textarea></label>`
      : `<label>Name<input data-field="name" value="${value("name")}" placeholder="CI on main" /></label>` +
        `<label>Every (minutes)<input data-field="every_minutes" type="number" min="1" value="${value("every_minutes", 10)}" /></label>` +
        `<label class="wide">Command<input class="mono" data-field="command" value="${value("command")}" placeholder="gh run list --branch main --limit 1 --json conclusion" spellcheck="false" /></label>` +
        `<label>Start a run<select data-field="trigger">` +
        `<option value="change"${item && item.trigger === "failure" ? "" : " selected"}>when its output changes</option>` +
        `<option value="failure"${item && item.trigger === "failure" ? " selected" : ""}>when it starts failing</option></select></label>` +
        `<label class="wide">Prompt<textarea data-field="prompt_template" rows="3" placeholder="The CI run changed: {output}. Find out why and fix it.">${value("prompt_template")}</textarea>` +
        `<span class="provider-hint"><span class="mono">{output}</span> and <span class="mono">{exit_code}</span> are filled in from the check. The command runs in this workspace without asking each time, because you wrote it here.</span></label>`;
  return (
    `<div class="auto-card auto-form" data-auto-form="${kind}" data-id="${id}">` +
    `<div class="auto-fields">${fields}<label>Tier<select data-field="tier">${tierOptionsMarkup(tiers, item && item.tier)}</select></label></div>` +
    `<div class="approval-actions">` +
    `<button type="button" class="ghost-button small" data-auto-cancel="1">Cancel</button>` +
    `<button type="button" class="primary-button small" data-auto-save="${kind}">${item ? "Save" : `Add ${kind}`}</button>` +
    `</div></div>`
  );
}

/* The form's values as the API takes them. `every` stays text when it is a
 * cron line and becomes a number when it is only digits. */
function automationFormValues(form) {
  const values = {};
  form.querySelectorAll("[data-field]").forEach((field) => {
    values[field.dataset.field] = field.value.trim();
  });
  if (values.every !== undefined && /^\d+$/.test(values.every)) values.every = Number(values.every);
  if (values.every_minutes !== undefined) values.every_minutes = Number(values.every_minutes);
  return values;
}

/* One line for the toast when the runner did something on its own. */
function automationFrameSummary(frame) {
  const lines = [];
  (frame.ran || []).forEach((item) => lines.push(`${item.type === "watcher" ? "Watcher" : "Schedule"} "${item.name}" started a run.`));
  (frame.skipped || []).forEach((item) => lines.push(`"${item.name}" was skipped: ${item.reason}.`));
  return lines.join(" ");
}
