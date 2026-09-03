# CLIProxyAPI CloudBase Service

This is a CloudBase container deployment for `CLIProxyAPI` v7.2.144. It downloads the official Linux amd64 release during image build and verifies its SHA-256 before it is used.

It never packages OAuth credentials in the image or Git repository. For private testing, one Codex OAuth JSON file can be injected at deploy time through the encrypted `CLI_PROXY_AUTH_JSON_B64` environment variable. Do not use a personal ChatGPT Plus credential for a public or multi-user service without confirming that the account and service terms permit it.

## CloudBase setup

1. Push this directory to a Git repository. The parent repository already ignores `services/CLIProxyAPI*/`, so this CloudBase deployment directory is intentionally excluded from the current app repository unless you force-add it or use a separate repository.
2. In CloudBase, open the environment and choose `云托管` -> `新建服务`.
3. Use service name `cli-proxy-api`, deploy from the repository branch, and set the source directory to this directory if CloudBase asks for it.
4. Set container port to `8080`, then configure the environment variables listed in `.env.example`.
5. Mount a persistent volume at `/data` so the generated config and OAuth state survive a container replacement.
6. Deploy. In the service details page, enable `HTTP 访问服务` / public HTTPS access. CloudBase then shows an address similar to:

   ```text
   https://cli-proxy-api-xxxx.ap-shanghai.app.tcloudbase.com
   ```

7. Verify the service with:

   ```text
   https://YOUR_SERVICE_DOMAIN/v1/models
   ```

   Send `Authorization: Bearer <CLI_PROXY_API_KEY>` with the request. Do not put the key in a browser URL.

8. Only after the endpoint returns the configured model, set the WeChat `aiApi` cloud function environment variables:

   ```text
   YANTONG_AI_PROVIDER_URL=https://YOUR_SERVICE_DOMAIN/v1
   YANTONG_AI_PROVIDER_KEY=<CLI_PROXY_API_KEY>
   YANTONG_AI_PROVIDER_MODEL=<UPSTREAM_MODEL_ALIAS>
   YANTONG_AI_TIMEOUT_MS=25000
   ```

## Required CloudBase variables

| Name | Purpose |
| --- | --- |
| `CLI_PROXY_API_KEY` | Key accepted by this proxy. Generate a new random value; do not reuse the previously exposed local key. |
| `CLI_PROXY_AUTH_JSON_B64` | Optional sensitive environment variable containing the Base64 text of one local Codex OAuth JSON file. Base64 is encoding, not encryption. The container restores it as `/data/auth/codex-oauth.json`; never commit this value. |
| `CLI_PROXY_AUTH_VERSION` | Version marker for the injected credential, e.g. `2026-09-02-1`. Change it only when replacing the OAuth JSON; otherwise refreshed tokens in `/data/auth` are preserved. |
| `CLI_PROXY_OUTBOUND_PROXY` | Optional global outbound proxy (`http`, `https`, `socks5`, or `socks5h`). Required if the container region cannot connect to `chatgpt.com`; treat credentials in the URL as a secret. |
| `UPSTREAM_BASE_URL` | HTTPS base URL supplied by your authorized OpenAI-compatible provider. |
| `UPSTREAM_API_KEY` | Provider-issued API key. |
| `UPSTREAM_MODEL` | Upstream model identifier. It must support image input for question recognition. |
| `UPSTREAM_MODEL_ALIAS` | Optional alias returned by `/v1/models`, e.g. `gpt-5.4`. |

## Injecting the local Codex login for private testing

The cloud container cannot read the `auth` directory on your computer. To inject the existing login without uploading a token file to GitHub:

1. Stop the local CLIProxyAPI service before copying its credential. In PowerShell, run the included helper. It copies the encoded credential directly to the clipboard without printing it:

   ```powershell
   cd "C:\Users\16950\Desktop\研通\services\cli-proxy-cloud"
   .\Copy-CodexOAuthSecret.ps1
   ```

   The helper locates the sibling `CLIProxyAPI-v7.2.144\auth` directory automatically, so it is not affected by Chinese Windows path encoding. If your local CLIProxyAPI is stored elsewhere, pass its `auth` directory explicitly with `-AuthDirectory`.

2. Do not send the clipboard value in chat, paste it into Git, or put it in a screenshot.
3. In CloudBase, open `云托管 -> cli-proxy-api -> 配置 -> 环境变量`, add `CLI_PROXY_AUTH_JSON_B64`, paste the clipboard value, and set `CLI_PROXY_AUTH_VERSION` to a unique value such as `2026-09-02-1`. Mark the variable as sensitive/encrypted if the console offers that option. Keep `CLI_PROXY_API_KEY` configured. Remove all three `UPSTREAM_*` variables for this OAuth mode.
4. Make sure `/data` is mounted as a persistent volume, then redeploy/restart the service.
5. Test `/v1/models` with the proxy key. A Codex model should be listed. If it is listed, set the mini-program `aiApi` variables to the cloud service URL and the same proxy key.

The access token may expire and the refresh token may stop working. If the service later reports an expired or invalid account, repeat the local login, generate a new Base64 value, and change `CLI_PROXY_AUTH_VERSION` before redeploying. Do not expose the JSON file.

### Mainland China deployment note

`GET /v1/models` only proves that the OAuth file was loaded; it does not prove that the container can reach the model. Always send one small `/v1/chat/completions` request. If it fails with `chatgpt.com ... connect: connection refused`, the deployment region has no usable route to the upstream service. Either set `CLI_PROXY_OUTBOUND_PROXY` to a stable, authorized outbound proxy or deploy this private proxy service in an overseas region that can reach the upstream. A localhost proxy on the developer computer is not reachable from CloudBase.

## Local build check

```powershell
docker build -t yantong-cli-proxy .
docker run --rm -p 8080:8080 --env-file .env yantong-cli-proxy
```

The management panel is deliberately disabled. Do not expose an unauthenticated management UI on a public service.

## Push commands (PowerShell)

From this directory, use the Git executable installed on your computer. If `git` is not recognized, use its full path, for example `D:\Git\cmd\git.exe`. Create a separate repository for this service or force-add only this directory to an authorized private repository; do not add `.env`, `auth`, `config.yaml`, OAuth JSON files, or provider keys.
