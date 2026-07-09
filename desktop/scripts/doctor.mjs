import { spawnSync } from "node:child_process";

const checks = [
  {
    name: "cargo",
    command: "cargo",
    args: ["--version"],
    hint: "Install Rust from https://rustup.rs/ or make sure cargo is on PATH.",
  },
  {
    name: "pkg-config",
    command: "pkg-config",
    args: ["--version"],
    hint: "Install pkg-config.",
  },
  {
    name: "dbus-1",
    command: "pkg-config",
    args: ["--exists", "dbus-1"],
    hint: "Install libdbus-1-dev.",
  },
  {
    name: "webkit2gtk-4.1",
    command: "pkg-config",
    args: ["--exists", "webkit2gtk-4.1"],
    hint: "Install libwebkit2gtk-4.1-dev.",
  },
  {
    name: "javascriptcoregtk-4.1",
    command: "pkg-config",
    args: ["--exists", "javascriptcoregtk-4.1"],
    hint: "Install libjavascriptcoregtk-4.1-dev.",
  },
  {
    name: "ayatana-appindicator3-0.1",
    command: "pkg-config",
    args: ["--exists", "ayatana-appindicator3-0.1"],
    hint: "Install libayatana-appindicator3-dev.",
  },
  {
    name: "librsvg-2.0",
    command: "pkg-config",
    args: ["--exists", "librsvg-2.0"],
    hint: "Install librsvg2-dev.",
  },
  {
    name: "xdo",
    command: "pkg-config",
    args: ["--exists", "xdo"],
    hint: "Install libxdo-dev.",
  },
  {
    name: "openssl",
    command: "pkg-config",
    args: ["--exists", "openssl"],
    hint: "Install libssl-dev.",
  },
];

let failed = false;

for (const check of checks) {
  const result = spawnSync(check.command, check.args, {
    encoding: "utf-8",
    stdio: "pipe",
  });
  const ok = result.status === 0;
  const marker = ok ? "ok" : "missing";
  console.log(`${marker.padEnd(7)} ${check.name}`);
  if (!ok) {
    failed = true;
    console.log(`        ${check.hint}`);
  }
}

if (failed) {
  console.log("");
  console.log("Ubuntu setup:");
  console.log(
    "  sudo apt install pkg-config libdbus-1-dev libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev libssl-dev",
  );
  process.exit(1);
}
