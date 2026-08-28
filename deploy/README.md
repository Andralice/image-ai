# Deployment notes

## Files
- `docker-compose.yml` (repo root) — gpt-image-2-web + Right Code async proxy chain
- `rightcode-proxy.service` — systemd unit for the async proxy (Python, listens on 127.0.0.1:8090)
- `cargo-config.toml` (repo root) — injects rsproxy.cn mirror inside the Docker build (this host can't reach crates.io directly)
- `data/config.example.json` — copy to `data/config.json` and edit; real config.json must stay uncommitted

## Bring-up
1. `cp .env.example .env` (or write your own; never commit the real .env)
2. `cp data/config.example.json data/config.json` (edit if needed)
3. `docker compose build && docker compose up -d`
4. `sudo cp deploy/rightcode-proxy.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now rightcode-proxy`
5. UFW: `sudo ufw allow 8787/tcp`
6. Open `http://<host>:8787/` — enter token if `GPT_IMAGE_2_WEB_TOKEN` is set

## Why the proxy?
The Right Code (rightapi.ai) platform exposes gpt-image-2 via an **async** API:
`POST /draw/v1/images/generations` with `async: true` returns a `task_id`, then you poll
`GET /v1/tasks/{task_id}`. The async_proxy.py wraps this and presents the standard
synchronous OpenAI shape that gpt-image-2-web expects.

## Notes
- Rust build is done with `cargo build` (debug) instead of `--release` to fit this 3.6 GB / 4 core host in ~5 min.
- Docker `network_mode: host` so the container can hit `http://127.0.0.1:8090` (the proxy) directly.
- `GPT_IMAGE_2_WEB_ALLOWED_HOSTS` must include any external hostname or IP you access the UI from, otherwise the server enforces loopback-only Host.
