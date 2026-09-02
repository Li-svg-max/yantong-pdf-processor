const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = __dirname;
const read = (name) => fs.readFileSync(path.join(root, name), "utf8");
const dockerfile = read("Dockerfile");
const entrypoint = read("docker-entrypoint.sh");
const readme = read("README.md");

assert(dockerfile.includes("ARG CLI_PROXY_VERSION=7.2.144"));
assert(dockerfile.includes("CLIProxyAPI_${CLI_PROXY_VERSION}_linux_amd64.tar.gz"));
assert(dockerfile.includes("sha256sum -c -"));
assert(dockerfile.includes("EXPOSE 8080"));
assert(dockerfile.includes("ca-certificates"));
assert(entrypoint.includes('host: "0.0.0.0"'));
assert(entrypoint.includes("CLI_PROXY_API_KEY is required"));
assert(entrypoint.includes("UPSTREAM_BASE_URL must use HTTPS"));
assert(entrypoint.includes("disable-control-panel: true"));
assert(entrypoint.includes("CLI_PROXY_AUTH_JSON_B64"));
assert(entrypoint.includes("CLI_PROXY_AUTH_VERSION"));
assert(entrypoint.includes("codex-oauth.json"));
assert(entrypoint.includes("chmod 0600"));
assert(readme.includes("YANTONG_AI_PROVIDER_URL"));
assert(readme.includes("CLI_PROXY_AUTH_JSON_B64"));
assert(fs.existsSync(path.join(root, ".env.example")));
assert(fs.existsSync(path.join(root, "Copy-CodexOAuthSecret.ps1")));

console.log("CLIProxyAPI CloudBase deployment files verified.");
