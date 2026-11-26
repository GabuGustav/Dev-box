# Deployment security

## Local development

1. Copy `.env.example` to `.env`.
2. Generate a unique URL-safe database password and executor token:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Use separate generated values for `DB_PASSWORD` and `EXECUTOR_SERVICE_TOKEN`. Keep `.env` out of version control.
3. Keep `API_BIND_ADDRESS=127.0.0.1` for local-only access.
4. Start with `docker compose up --build`.

PostgreSQL is intentionally not published to the host. The API reaches it over the Compose service network. The API is published on host loopback only by default.

## External deployment

**Do not expose the API directly over plain HTTP.** Terminate TLS at a maintained reverse proxy or load balancer and configure the proxy to forward to the API over a trusted network. If the proxy runs on the deployment host, the default loopback binding allows it to reach `127.0.0.1:8000`.

If the proxy is containerized, place it and the API on a dedicated private Compose network and do not publish the API port on a public host interface. Configure the proxy's public listener for HTTPS, redirect HTTP to HTTPS, use a valid certificate, set strict TLS versions, and forward only the headers required by the application. Do not trust client-supplied forwarded headers unless the proxy is the only route to the API and the proxy overwrites those headers.

Set `ENVIRONMENT=production` and provide:
- A unique `DB_PASSWORD` (URL-safe characters; the Compose interpolation forms the internal PostgreSQL URL).
- A unique `EXECUTOR_SERVICE_TOKEN` of at least 32 characters.
- A PostgreSQL `DATABASE_URL` when running the API outside the supplied Compose environment.
- A securely generated `SNAPSHOT_ENCRYPTION_KEY` and protected backup of that key.

The application rejects production configuration with a missing/placeholder executor token, SQLite database, or known placeholder database credentials. Compose requires `DB_PASSWORD` to be set. The example values are placeholders and must never be used in a deployed environment.

## Network exposure checklist

- [ ] PostgreSQL has no published host port and is reachable only by authorized internal services.
- [ ] API traffic from clients is HTTPS-only.
- [ ] API port is not publicly reachable except through the TLS proxy.
- [ ] Worker port and Docker socket are not exposed to clients or the public network.
- [ ] `.env`, database credentials, executor token, and snapshot key are protected and backed up appropriately.
- [ ] Firewall rules allow only required ingress and egress.
- [ ] Logs and reverse-proxy configuration do not record authorization headers or secrets.
