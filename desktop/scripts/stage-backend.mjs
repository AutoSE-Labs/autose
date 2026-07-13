// Stages the Python backend into desktop/src-tauri/backend so Tauri can bundle
// it as a resource. Run automatically by beforeBuildCommand (cwd = desktop/).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "..", "..");
const dest = path.join(scriptDir, "..", "src-tauri", "backend");

const copyEntries = ["pyproject.toml", "uv.lock", "README.md", "autose.py", "src", "code"];

const copyFilter = (src) => {
  const name = path.basename(src);
  return name !== "__pycache__" && !name.endsWith(".pyc");
};

fs.rmSync(dest, { recursive: true, force: true });
fs.mkdirSync(dest, { recursive: true });

for (const entry of copyEntries) {
  const from = path.join(repoRoot, entry);
  if (!fs.existsSync(from)) {
    console.error(`stage-backend: missing required entry ${from}`);
    process.exit(1);
  }
  fs.cpSync(from, path.join(dest, entry), { recursive: true, filter: copyFilter });
}

// Ship a blank config template, never the repo config (it may hold private endpoints).
fs.mkdirSync(path.join(dest, "profiles"), { recursive: true });
fs.writeFileSync(
  path.join(dest, "profiles", "config.yaml"),
  [
    "inference:",
    "  provider: openai",
    '  base_url: ""',
    '  api_key: ""',
    '  model: ""',
    "  context_limit: 262144",
    "",
  ].join("\n"),
);

console.log(`stage-backend: staged backend into ${dest}`);
