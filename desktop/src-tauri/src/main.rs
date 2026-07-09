use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::thread;
use tauri::{AppHandle, Emitter};

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

fn repo_root() -> Result<PathBuf, String> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(|desktop| desktop.parent())
        .map(PathBuf::from)
        .ok_or_else(|| "Could not resolve AutoSE repository root.".to_string())
}

fn is_executable(path: &Path) -> bool {
    path.is_file()
}

fn find_on_path(program: &str) -> Option<PathBuf> {
    let path_var = env::var_os("PATH")?;
    env::split_paths(&path_var)
        .map(|dir| dir.join(program))
        .find(|candidate| is_executable(candidate))
}

fn uv_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Some(path) = find_on_path("uv") {
        candidates.push(path);
    }

    if let Some(home) = env::var_os("HOME") {
        let home = PathBuf::from(home);
        candidates.push(home.join(".local").join("bin").join("uv"));
        candidates.push(home.join(".cargo").join("bin").join("uv"));
    }

    candidates
}

fn python_candidates(root: &Path) -> Vec<PathBuf> {
    let mut candidates = vec![root.join(".venv").join("bin").join("python")];

    if let Some(path) = find_on_path("python3") {
        candidates.push(path);
    }
    if let Some(path) = find_on_path("python") {
        candidates.push(path);
    }

    candidates
}

fn build_autose_command(
    root: &Path,
    workspace: &Path,
    mode: &str,
    prompt: &str,
    auto_approve: bool,
) -> Result<Command, String> {
    for uv_path in uv_candidates() {
        if !is_executable(&uv_path) {
            continue;
        }

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
        return Ok(command);
    }

    Err(
        "AutoSE could not find a runnable environment. Install `uv`, or create a project virtualenv and install the Python dependencies."
            .to_string(),
    )
}

#[tauri::command]
fn default_workspace() -> Result<String, String> {
    repo_root().map(|path| path.to_string_lossy().to_string())
}

#[tauri::command]
fn list_saved_sessions() -> Result<Vec<SavedSession>, String> {
    let sessions_dir = repo_root()?.join(".autose").join("sessions");
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

    let root = repo_root()?;
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

    for line in BufReader::new(stdout).lines() {
        let line = line.map_err(|err| format!("Failed to read AutoSE stdout: {err}"))?;
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<StreamRecord>(&line) {
            Ok(record) if record.kind == "event" => {
                if let Some(event) = record.event {
                    app.emit("autose-event", event)
                        .map_err(|err| format!("Failed to emit AutoSE event: {err}"))?;
                }
            }
            Ok(record) if record.kind == "session" => {
                final_payload = record.payload;
            }
            Ok(_) => invalid_lines.push(line),
            Err(_) => invalid_lines.push(line),
        }
    }

    let status = child
        .wait()
        .map_err(|err| format!("Failed to wait for AutoSE CLI: {err}"))?;
    let stderr_text = stderr_reader
        .join()
        .map_err(|_| "Failed to join AutoSE stderr reader.".to_string())??;
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
        .invoke_handler(tauri::generate_handler![
            default_workspace,
            list_saved_sessions,
            run_autose
        ])
        .run(tauri::generate_context!())
        .expect("error while running AutoSE desktop app");
}

fn main() {
    run();
}
