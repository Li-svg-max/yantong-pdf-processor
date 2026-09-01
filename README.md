# CLIProxyAPI CloudBase Service

This is a CloudBase container deployment for `CLIProxyAPI` v7.2.144. It downloads the official Linux amd64 release during image build and verifies its SHA-256 before it is used.

It intentionally does **not** package any ChatGPT Plus/Codex OAuth credentials. For a cloud service, configure an API provider that explicitly allows programmatic and server-side use in the chosen deployment region.

## CloudBase setup

1. Push this directory to a Git repository. The parent repository already ignores `services/CLIProxyAPI*/`, so this CloudBase deployment directory is intentionally excluded from the current app repository unless you force-add it or use a separate repository.
2. In CloudBase, open the environment and choose `云托管` -> `新建服务`.
3. Use service name `cli-proxy-api`, deploy from the repository branch, and set the source directory to this directory if CloudBase asks for it.
4. Set container port to `8080`, then configure the environment variables listed in `.env.example`.
5. Mount a persistent volume at `/data` if credentials or local state must survive a container replacement. This deployment does not need OAuth files.
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
| `UPSTREAM_BASE_URL` | HTTPS base URL supplied by your authorized OpenAI-compatible provider. |
| `UPSTREAM_API_KEY` | Provider-issued API key. |
| `UPSTREAM_MODEL` | Upstream model identifier. It must support image input for question recognition. |
| `UPSTREAM_MODEL_ALIAS` | Optional alias returned by `/v1/models`, e.g. `gpt-5.4`. |

## Local build check

```powershell
docker build -t yantong-cli-proxy .
docker run --rm -p 8080:8080 --env-file .env yantong-cli-proxy
```

The management panel is deliberately disabled. Do not expose an unauthenticated management UI on a public service.

## Push commands (PowerShell)

From this directory, use the Git executable installed on your computer. If `git` is not recognized, use its full path, for example `D:\Git\cmd\git.exe`. Create a separate repository for this service or force-add only this directory to an authorized private repository; do not add `.env`, `auth`, `config.yaml`, OAuth JSON files, or provider keys.
