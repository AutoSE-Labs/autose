use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::thread;
use tauri::{AppHandle, Emitter, Manager};

const STAGED_VERSION_FILE: &str = ".autose-staged-version";
// Sentinel error returned by run_autose when the user stopped the task; the
// frontend matches on it to render a "stopped" report instead of a failure.
const STOP_SENTINEL: &str = "__AUTOSE_STOPPED__";
const BACKEND_ENTRIES: [&str; 7] = [
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "autose.py",
    "src",
    "code",
    "profiles",
];

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TaskRequest {
    prompt: String,
    mode: String,
    workspace: String,
    auto_approve: bool,
}

#[derive(Debug, Serialize)]
struct TaskResponse {
    payload: Value,
    stderr: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct SavedSession {
    id: String,
    task: String,
    status: String,
    summary: String,
    created_at: String,
}

#[derive(Debug, Deserialize)]
struct StreamRecord {
    #[serde(rename = "type")]
    kind: String,
    event: Option<Value>,
    payload: Option<Value>,
}

#[derive(Debug, Serialize)]
struct BootstrapStatus {
    state: String,
    detail: String,
}

#[derive(Default)]
struct RunningTask {
    child: Mutex<Option<Child>>,
    stop_requested: AtomicBool,
}

#[derive(Debug, Serialize, Deserialize)]
struct InferenceSettings {
    #[serde(default = "default_provider")]
    provider: String,
    #[serde(default)]
    base_url: String,
    #[serde(default)]
    api_key: String,
    #[serde(default)]
    model: String,
    #[serde(default = "default_context_limit")]
    context_limit: u64,
}

fn default_provider() -> String {
    "openai".to_string()
}

fn default_context_limit() -> u64 {
    262144
}

impl Default for InferenceSettings {
    fn default() -> Self {
        InferenceSettings {
            provider: default_provider(),
            base_url: String::new(),
            api_key: String::new(),
            model: String::new(),
            context_limit: default_context_limit(),
        }
    }
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct ConfigDocument {
    #[serde(default)]
    inference: InferenceSettings,
    #[serde(flatten)]
    extra: HashMap<String, serde_yaml::Value>,
}

fn is_dev() -> bool {
    cfg!(debug_assertions)
}

fn dev_repo_root() -> Result<PathBuf, String> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(|desktop| desktop.parent())
        .map(PathBuf::from)
        .ok_or_else(|| "Could not resolve AutoSE repository root.".to_string())
}

fn app_home() -> Result<PathBuf, String> {
    if cfg!(windows) {
        env::var_os("LOCALAPPDATA")
            .map(|base| PathBuf::from(base).join("AutoSE"))
            .ok_or_else(|| "LOCALAPPDATA is not set.".to_string())
    } else {
        env::var_os("HOME")
            .map(|home| {
                PathBuf::from(home)
                    .join(".local")
                    .join("share")
                    .join("autose")
            })
            .ok_or_else(|| "HOME is not set.".to_string())
    }
}

fn backend_dir() -> Result<PathBuf, String> {
    if is_dev() {
        dev_repo_root()
    } else {
        // Must not be `app_home()/backend`: the NSIS per-user install dir defaults
        // to %LOCALAPPDATA%\AutoSE, so that path would collide with the bundled
        // `backend` resource next to the exe.
        app_home().map(|home| home.join("runtime").join("backend"))
    }
}

fn config_path() -> Result<PathBuf, String> {
    // Must mirror the lookup in code/logic/main.py so the CLI and desktop app
    // read the same file.
    if cfg!(windows) {
        env::var_os("APPDATA")
            .map(|base| PathBuf::from(base).join("AutoSE").join("config.yaml"))
            .ok_or_else(|| "APPDATA is not set.".to_string())
    } else {
        env::var_os("HOME")
            .map(|home| {
                PathBuf::from(home)
                    .join(".config")
                    .join("autose")
                    .join("config.yaml")
            })
            .ok_or_else(|| "HOME is not set.".to_string())
    }
}

fn exe_name(program: &str) -> String {
    if cfg!(windows) {
        format!("{program}.exe")
    } else {
        program.to_string()
    }
}

fn hide_console(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
}

fn is_executable(path: &Path) -> bool {
    path.is_file()
}

fn find_on_path(program: &str) -> Option<PathBuf> {
    let file_name = exe_name(program);
    let path_var = env::var_os("PATH")?;
    env::split_paths(&path_var)
        .map(|dir| dir.join(&file_name))
        .find(|candidate| is_executable(candidate))
}

fn home_dir() -> Option<PathBuf> {
    if cfg!(windows) {
        env::var_os("USERPROFILE").map(PathBuf::from)
    } else {
        env::var_os("HOME").map(PathBuf::from)
    }
}

fn managed_uv_path() -> Option<PathBuf> {
    app_home()
        .ok()
        .map(|home| home.join("uv").join(exe_name("uv")))
}

fn uv_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Some(path) = find_on_path("uv") {
        candidates.push(path);
    }

    if let Some(home) = home_dir() {
        candidates.push(home.join(".local").join("bin").join(exe_name("uv")));
        candidates.push(home.join(".cargo").join("bin").join(exe_name("uv")));
    }

    if let Some(managed) = managed_uv_path() {
        candidates.push(managed);
    }

    candidates
}

fn find_uv() -> Option<PathBuf> {
    uv_candidates()
        .into_iter()
        .find(|candidate| is_executable(candidate))
}

fn venv_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    }
}

fn python_candidates(root: &Path) -> Vec<PathBuf> {
    let mut candidates = vec![venv_python(root)];

    if let Some(path) = find_on_path("python3") {
        candidates.push(path);
    }
    if let Some(path) = find_on_path("python") {
        candidates.push(path);
    }

    candidates
}

fn apply_common_env(command: &mut Command) {
    if let Ok(config) = config_path() {
        if config.is_file() {
            command.env("AUTOSE_CONFIG", config.as_os_str());
        }
    }
}

fn build_autose_command(
    root: &Path,
    workspace: &Path,
    mode: &str,
    prompt: &str,
    auto_approve: bool,
) -> Result<Command, String> {
    if let Some(uv_path) = find_uv() {
        let mut command = Command::new(uv_path);
        command
            .current_dir(root)
            .arg("run")
            .arg("autose")
            .arg("--events")
            .arg("--mode")
            .arg(mode)
            .arg("--workspace")
            .arg(workspace);

        if auto_approve {
            command.arg("--yes");
        }

        command.arg(prompt);
        command.stdout(Stdio::piped()).stderr(Stdio::piped());
        apply_common_env(&mut command);
        hide_console(&mut command);
        return Ok(command);
    }

    let autose_py = root.join("autose.py");
    for python_path in python_candidates(root) {
        if !is_executable(&python_path) {
            continue;
        }

        let mut command = Command::new(python_path);
        command
            .current_dir(root)
            .arg(autose_py.as_os_str())
            .arg("--events")
            .arg("--mode")
            .arg(mode)
            .arg("--workspace")
            .arg(workspace);

        if auto_approve {
            command.arg("--yes");
        }

        command.arg(prompt);
        command.stdout(Stdio::piped()).stderr(Stdio::piped());
        apply_common_env(&mut command);
        hide_console(&mut command);
        return Ok(command);
    }

    Err(
        "AutoSE could not find a runnable environment. Restart the app to run first-time setup, or install `uv` manually."
            .to_string(),
    )
}

fn emit_setup(app: &AppHandle, stage: &str, message: &str) {
    let _ = app.emit(
        "autose-setup",
        json!({ "stage": stage, "message": message }),
    );
}

#[tauri::command]
fn backend_status() -> Result<BootstrapStatus, String> {
    if is_dev() {
        return Ok(BootstrapStatus {
            state: "dev".to_string(),
            detail: "Development build uses the repository checkout directly.".to_string(),
        });
    }

    let backend = backend_dir()?;
    let marker = backend.join(STAGED_VERSION_FILE);
    let staged_version = fs::read_to_string(&marker).ok();
    let app_version = env!("CARGO_PKG_VERSION");

    if staged_version.as_deref().map(str::trim) != Some(app_version)
        || !backend.join("pyproject.toml").is_file()
    {
        return Ok(BootstrapStatus {
            state: "needs_setup".to_string(),
            detail: "The AutoSE runtime has not been set up for this version yet.".to_string(),
        });
    }
    if !venv_python(&backend).is_file() {
        return Ok(BootstrapStatus {
            state: "needs_setup".to_string(),
            detail: "The Python environment is missing.".to_string(),
        });
    }
    if find_uv().is_none() {
        return Ok(BootstrapStatus {
            state: "needs_setup".to_string(),
            detail: "The uv package manager is missing.".to_string(),
        });
    }

    Ok(BootstrapStatus {
        state: "ready".to_string(),
        detail: String::new(),
    })
}

#[tauri::command]
async fn bootstrap_backend(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || bootstrap_blocking(app))
        .await
        .map_err(|err| format!("Failed to join setup worker: {err}"))?
}

fn bootstrap_blocking(app: AppHandle) -> Result<(), String> {
    if is_dev() {
        return Ok(());
    }

    let backend = backend_dir()?;
    let app_version = env!("CARGO_PKG_VERSION");
    let marker = backend.join(STAGED_VERSION_FILE);
    let staged_version = fs::read_to_string(&marker).ok();

    if staged_version.as_deref().map(str::trim) != Some(app_version)
        || !backend.join("pyproject.toml").is_file()
    {
        emit_setup(&app, "copy", "Installing the AutoSE runtime files...");
        stage_backend_files(&app, &backend)?;
        fs::write(&marker, app_version)
            .map_err(|err| format!("Failed to record the staged version: {err}"))?;
    }

    let uv_path = match find_uv() {
        Some(path) => path,
        None => {
            emit_setup(&app, "uv", "Downloading the uv package manager...");
            install_uv()?;
            find_uv().ok_or_else(|| {
                "uv was installed but could not be located afterwards.".to_string()
            })?
        }
    };

    emit_setup(&app, "sync", "Preparing the Python environment (first run only)...");
    run_uv_sync(&app, &uv_path, &backend)?;

    let config = config_path()?;
    if !config.is_file() {
        emit_setup(&app, "config", "Creating the default configuration...");
        if let Some(parent) = config.parent() {
            fs::create_dir_all(parent)
                .map_err(|err| format!("Failed to create the config directory: {err}"))?;
        }
        let template = backend.join("profiles").join("config.yaml");
        fs::copy(&template, &config)
            .map_err(|err| format!("Failed to create the default config: {err}"))?;
    }

    emit_setup(&app, "done", "Setup complete.");
    Ok(())
}

fn stage_backend_files(app: &AppHandle, backend: &Path) -> Result<(), String> {
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|err| format!("Failed to resolve the app resource directory: {err}"))?
        .join("backend");
    if !bundled.is_dir() {
        return Err(format!(
            "Bundled runtime files are missing at {}.",
            bundled.display()
        ));
    }
    if bundled == *backend {
        return Err(
            "The runtime directory would overwrite the bundled app files. Reinstall AutoSE."
                .to_string(),
        );
    }

    let missing: Vec<&str> = BACKEND_ENTRIES
        .iter()
        .copied()
        .filter(|entry| !bundled.join(entry).exists())
        .collect();
    if !missing.is_empty() {
        return Err(format!(
            "The app bundle is incomplete (missing: {}). Reinstall AutoSE.",
            missing.join(", ")
        ));
    }

    fs::create_dir_all(backend)
        .map_err(|err| format!("Failed to create the runtime directory: {err}"))?;

    // Replace only the staged source entries; leave .venv and any session data alone.
    for entry in BACKEND_ENTRIES {
        let target = backend.join(entry);
        if target.is_dir() {
            fs::remove_dir_all(&target)
                .map_err(|err| format!("Failed to clear {}: {err}", target.display()))?;
        } else if target.is_file() {
            fs::remove_file(&target)
                .map_err(|err| format!("Failed to clear {}: {err}", target.display()))?;
        }

        let source = bundled.join(entry);
        if source.is_dir() {
            copy_dir_recursive(&source, &target)?;
        } else {
            fs::copy(&source, &target)
                .map_err(|err| format!("Failed to copy {}: {err}", source.display()))?;
        }
    }

    Ok(())
}

fn copy_dir_recursive(from: &Path, to: &Path) -> Result<(), String> {
    fs::create_dir_all(to).map_err(|err| format!("Failed to create {}: {err}", to.display()))?;
    let entries =
        fs::read_dir(from).map_err(|err| format!("Failed to read {}: {err}", from.display()))?;
    for entry in entries {
        let entry = entry.map_err(|err| format!("Failed to read {}: {err}", from.display()))?;
        let source = entry.path();
        let target = to.join(entry.file_name());
        if source.is_dir() {
            copy_dir_recursive(&source, &target)?;
        } else {
            fs::copy(&source, &target)
                .map_err(|err| format!("Failed to copy {}: {err}", source.display()))?;
        }
    }
    Ok(())
}

fn install_uv() -> Result<(), String> {
    let install_dir = app_home()?.join("uv");
    fs::create_dir_all(&install_dir)
        .map_err(|err| format!("Failed to create the uv install directory: {err}"))?;

    let mut command = if cfg!(windows) {
        let mut command = Command::new("powershell");
        command.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "irm https://astral.sh/uv/install.ps1 | iex",
        ]);
        command
    } else {
        let mut command = Command::new("sh");
        command.args(["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]);
        command
    };

    command
        .env("UV_INSTALL_DIR", install_dir.as_os_str())
        .env("UV_NO_MODIFY_PATH", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    hide_console(&mut command);

    let output = command
        .output()
        .map_err(|err| format!("Failed to run the uv installer: {err}"))?;
    if !output.status.success() {
        return Err(format!(
            "The uv installer failed. Check your internet connection and try again.\n{}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

fn run_uv_sync(app: &AppHandle, uv_path: &Path, backend: &Path) -> Result<(), String> {
    let mut command = Command::new(uv_path);
    command
        .current_dir(backend)
        .args(["sync", "--frozen"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    hide_console(&mut command);

    let mut child = command
        .spawn()
        .map_err(|err| format!("Failed to start uv sync: {err}"))?;

    let mut stderr_lines = Vec::new();
    if let Some(stderr) = child.stderr.take() {
        // uv reports progress on stderr; surface it in the setup screen.
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let trimmed = line.trim().to_string();
            if !trimmed.is_empty() {
                emit_setup(app, "sync", &trimmed);
                stderr_lines.push(trimmed);
            }
        }
    }

    let status = child
        .wait()
        .map_err(|err| format!("Failed to wait for uv sync: {err}"))?;
    if !status.success() {
        return Err(format!(
            "Preparing the Python environment failed. Check your internet connection and try again.\n{}",
            stderr_lines.join("\n")
        ));
    }
    Ok(())
}

fn read_config_document() -> Result<ConfigDocument, String> {
    let config = config_path()?;
    if !config.is_file() {
        return Ok(ConfigDocument::default());
    }
    let content = fs::read_to_string(&config)
        .map_err(|err| format!("Failed to read {}: {err}", config.display()))?;
    serde_yaml::from_str(&content)
        .map_err(|err| format!("Failed to parse {}: {err}", config.display()))
}

#[tauri::command]
fn get_settings() -> Result<InferenceSettings, String> {
    Ok(read_config_document()?.inference)
}

#[tauri::command]
fn save_settings(settings: InferenceSettings) -> Result<(), String> {
    let mut document = read_config_document()?;
    document.inference = settings;

    let config = config_path()?;
    if let Some(parent) = config.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("Failed to create the config directory: {err}"))?;
    }
    let content = serde_yaml::to_string(&document)
        .map_err(|err| format!("Failed to serialize the configuration: {err}"))?;
    fs::write(&config, content)
        .map_err(|err| format!("Failed to write {}: {err}", config.display()))?;
    Ok(())
}

#[tauri::command]
fn default_workspace() -> Result<String, String> {
    if is_dev() {
        return dev_repo_root().map(|path| path.to_string_lossy().to_string());
    }
    home_dir()
        .map(|path| path.to_string_lossy().to_string())
        .ok_or_else(|| "Could not resolve the home directory.".to_string())
}

#[tauri::command]
fn list_saved_sessions() -> Result<Vec<SavedSession>, String> {
    let sessions_dir = backend_dir()?.join(".autose").join("sessions");
    if !sessions_dir.is_dir() {
        return Ok(Vec::new());
    }

    let mut sessions = Vec::new();
    for entry in fs::read_dir(&sessions_dir)
        .map_err(|err| format!("Failed to read sessions directory: {err}"))?
    {
        let entry = entry.map_err(|err| format!("Failed to read session entry: {err}"))?;
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }

        let content = fs::read_to_string(&path)
            .map_err(|err| format!("Failed to read session {}: {err}", path.display()))?;
        let value: Value = serde_json::from_str(&content)
            .map_err(|err| format!("Failed to parse session {}: {err}", path.display()))?;

        let fallback_id = path
            .file_stem()
            .and_then(|name| name.to_str())
            .unwrap_or("session")
            .to_string();
        let id = value
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or(&fallback_id)
            .to_string();
        let created_at = value
            .get("created_at")
            .and_then(Value::as_str)
            .or_else(|| value.get("updated_at").and_then(Value::as_str))
            .unwrap_or("")
            .to_string();
        let task = first_history_content(&value, "user")
            .or_else(|| first_command(&value))
            .unwrap_or_else(|| "Untitled chat".to_string());
        let summary = last_history_content(&value, "assistant").unwrap_or_else(|| task.clone());

        sessions.push(SavedSession {
            id,
            task,
            status: "completed".to_string(),
            summary,
            created_at,
        });
    }

    sessions.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    sessions.truncate(50);
    Ok(sessions)
}

#[tauri::command]
fn delete_saved_session(id: String) -> Result<(), String> {
    let sessions_dir = backend_dir()?.join(".autose").join("sessions");
    if !sessions_dir.is_dir() {
        return Ok(());
    }

    for entry in fs::read_dir(&sessions_dir)
        .map_err(|err| format!("Failed to read sessions directory: {err}"))?
    {
        let entry = entry.map_err(|err| format!("Failed to read session entry: {err}"))?;
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }

        // Match either the file name or the id stored inside, mirroring how
        // list_saved_sessions derives ids.
        let stem_matches = path
            .file_stem()
            .and_then(|name| name.to_str())
            .is_some_and(|stem| stem == id);
        let id_matches = || {
            fs::read_to_string(&path)
                .ok()
                .and_then(|content| serde_json::from_str::<Value>(&content).ok())
                .and_then(|value| value.get("id").and_then(Value::as_str).map(ToOwned::to_owned))
                .is_some_and(|session_id| session_id == id)
        };
        if stem_matches || id_matches() {
            fs::remove_file(&path)
                .map_err(|err| format!("Failed to delete session {}: {err}", path.display()))?;
        }
    }
    Ok(())
}

fn first_history_content(value: &Value, role: &str) -> Option<String> {
    value
        .get("history")?
        .as_array()?
        .iter()
        .find(|item| item.get("role").and_then(Value::as_str) == Some(role))
        .and_then(|item| item.get("content").and_then(Value::as_str))
        .map(str::trim)
        .filter(|content| !content.is_empty())
        .map(ToOwned::to_owned)
}

fn last_history_content(value: &Value, role: &str) -> Option<String> {
    value
        .get("history")?
        .as_array()?
        .iter()
        .rev()
        .find(|item| item.get("role").and_then(Value::as_str) == Some(role))
        .and_then(|item| item.get("content").and_then(Value::as_str))
        .map(str::trim)
        .filter(|content| !content.is_empty())
        .map(ToOwned::to_owned)
}

fn first_command(value: &Value) -> Option<String> {
    value
        .get("command_history")?
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .map(str::trim)
        .find(|command| !command.is_empty() && !command.starts_with('/'))
        .map(ToOwned::to_owned)
}

#[tauri::command]
fn stop_autose(state: tauri::State<RunningTask>) -> Result<(), String> {
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "The task state is unavailable.".to_string())?;
    if let Some(child) = guard.as_mut() {
        state.stop_requested.store(true, Ordering::SeqCst);
        kill_process_tree(child)?;
    }
    Ok(())
}

fn kill_process_tree(child: &mut Child) -> Result<(), String> {
    #[cfg(windows)]
    {
        // `uv run` wraps the real python process; taskkill /T takes down the
        // whole tree instead of orphaning the grandchild.
        let mut command = Command::new("taskkill");
        command.args(["/PID", &child.id().to_string(), "/T", "/F"]);
        hide_console(&mut command);
        if command
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
        {
            return Ok(());
        }
    }
    child
        .kill()
        .map_err(|err| format!("Failed to stop AutoSE: {err}"))
}

#[tauri::command]
async fn run_autose(app: AppHandle, request: TaskRequest) -> Result<TaskResponse, String> {
    tauri::async_runtime::spawn_blocking(move || run_autose_blocking(app, request))
        .await
        .map_err(|err| format!("Failed to join AutoSE worker: {err}"))?
}

fn run_autose_blocking(app: AppHandle, request: TaskRequest) -> Result<TaskResponse, String> {
    let prompt = request.prompt.trim();
    if prompt.is_empty() {
        return Err("Task prompt is required.".to_string());
    }

    let mode = match request.mode.as_str() {
        "auto" | "lite" | "standard" => request.mode.as_str(),
        _ => return Err(format!("Unsupported mode: {}", request.mode)),
    };

    let root = backend_dir()?;
    let workspace = if request.workspace.trim().is_empty() {
        root.clone()
    } else {
        PathBuf::from(request.workspace.trim())
    };

    let mut command = build_autose_command(&root, &workspace, mode, prompt, request.auto_approve)?;

    let mut child = command
        .spawn()
        .map_err(|err| format!("Failed to start AutoSE CLI: {err}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Failed to capture AutoSE stdout.".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Failed to capture AutoSE stderr.".to_string())?;

    let state = app.state::<RunningTask>();
    state.stop_requested.store(false, Ordering::SeqCst);
    {
        let mut guard = state
            .child
            .lock()
            .map_err(|_| "The task state is unavailable.".to_string())?;
        if guard.is_some() {
            let _ = child.kill();
            return Err("A task is already running.".to_string());
        }
        *guard = Some(child);
    }

    let stderr_reader = thread::spawn(move || -> Result<String, String> {
        let mut stderr_text = String::new();
        for line in BufReader::new(stderr).lines() {
            let line = line.map_err(|err| format!("Failed to read AutoSE stderr: {err}"))?;
            stderr_text.push_str(&line);
            stderr_text.push('\n');
        }
        Ok(stderr_text)
    });

    let mut final_payload: Option<Value> = None;
    let mut invalid_lines: Vec<String> = Vec::new();
    // Collected instead of returned early so the child always gets reaped and
    // cleared from the shared slot below.
    let mut read_error: Option<String> = None;

    for line in BufReader::new(stdout).lines() {
        let line = match line {
            Ok(line) => line,
            Err(err) => {
                read_error = Some(format!("Failed to read AutoSE stdout: {err}"));
                break;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<StreamRecord>(&line) {
            Ok(record) if record.kind == "event" => {
                if let Some(event) = record.event {
                    if let Err(err) = app.emit("autose-event", event) {
                        read_error = Some(format!("Failed to emit AutoSE event: {err}"));
                        break;
                    }
                }
            }
            Ok(record) if record.kind == "session" => {
                final_payload = record.payload;
            }
            Ok(_) => invalid_lines.push(line),
            Err(_) => invalid_lines.push(line),
        }
    }

    let mut child = state
        .child
        .lock()
        .map_err(|_| "The task state is unavailable.".to_string())?
        .take()
        .ok_or_else(|| "Lost track of the AutoSE process.".to_string())?;
    let status = child
        .wait()
        .map_err(|err| format!("Failed to wait for AutoSE CLI: {err}"))?;
    let stderr_text = stderr_reader
        .join()
        .map_err(|_| "Failed to join AutoSE stderr reader.".to_string())??;
    if state.stop_requested.swap(false, Ordering::SeqCst) {
        return Err(STOP_SENTINEL.to_string());
    }
    if let Some(err) = read_error {
        return Err(err);
    }
    if !status.success() {
        return Err(format!(
            "AutoSE exited with status {}.\n{}",
            status,
            stderr_text.trim()
        ));
    }

    if !invalid_lines.is_empty() {
        return Err(format!(
            "AutoSE returned non-JSONL output before the final session:\n{}",
            invalid_lines.join("\n")
        ));
    }

    let payload = final_payload
        .ok_or_else(|| "AutoSE finished without emitting a final session payload.".to_string())?;

    Ok(TaskResponse {
        payload,
        stderr: stderr_text,
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(RunningTask::default())
        .invoke_handler(tauri::generate_handler![
            backend_status,
            bootstrap_backend,
            get_settings,
            save_settings,
            default_workspace,
            list_saved_sessions,
            delete_saved_session,
            run_autose,
            stop_autose
        ])
        .run(tauri::generate_context!())
        .expect("error while running AutoSE desktop app");
}

fn main() {
    run();
}
