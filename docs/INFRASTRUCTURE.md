# Infrastructure — Hermes Arcade

## Servers

| Name | IP | OS | Specs | Role | Access |
|------|----|----|-------|------|--------|
| pilarovds | 176.108.249.180 | Ubuntu 22.04 | 8GB RAM, 4 cores | ArcadeDB + Redis + Hermes | `ssh pilarovds@176.108.249.180` |

## Services / Containers

### hermes-db-arcadedb-1 (ArcadeDB)
- **Image**: `arcadedata/arcadedb:latest` (26.7.2-SNAPSHOT)
- **Ports**: `0.0.0.0:2480->2480` (HTTP API), `0.0.0.0:5432->5432` (PG wire)
- **JVM**: `-server -Xms2G -Xmx2G`
- **Database**: `hermes` (created via `defaultDatabases=hermes[root:hermes123:admin]`)
- **Password**: `-Darcadedb.server.rootPassword=hermes123` в JAVA_OPTS
- **Plugins**: `Postgres:com.arcadedb.postgres.PostgresProtocolPlugin`
- **Health**: `curl http://176.108.249.180:2480/api/v1/command/hermes` + Basic Auth

### hermes-redis (Redis)
- **Image**: `redis:7-alpine`
- **Port**: `0.0.0.0:6379` (`--network host`, `--protected-mode no`)
- **Restart**: `docker restart hermes-redis`
- **Note**: Порт 6379 закрыт облачным провайдером извне, работает локально

### Hermes Processes

| Process | Port | Bind | Status | Start command |
|---------|------|------|--------|---------------|
| Gateway (API Server) | 9119 | 0.0.0.0 | ✅ | `cd ~/hermes-arcade && .venv/bin/python -m hermes_cli.main gateway run` |
| Dashboard (Web UI) | 9118 | 127.0.0.1 | ⚠️ (auth required for 0.0.0.0) | `cd ~/hermes-arcade && .venv/bin/python -m hermes_cli.main dashboard --host 127.0.0.1 --port 9118` |
| Bridge (simple API) | 9119 | 0.0.0.0 |备用 | `.venv/bin/python /tmp/hermes_bridge.py` |

### Hermes Config

- **Config**: `~/.hermes/config.yaml`
- **Env**: `~/.hermes/.env` (OPENAI_API_KEY, DEEPSEEK_API_KEY, API_SERVER_KEY, OPENROUTER_API_KEY)
- **Key API key**: `hermes-arcade-key-2026` (для Gateway API)
- **Venom**: `~/hermes-arcade/.venv/` (Python 3.11)

## Connectivity (last check: 07.07.2026)

| Port | Protocol | Service | Local (127.0.0.1) | External (176.108.249.180) | Security Group |
|------|----------|---------|---------------------|----------------------------|----------------|
| 22 | SSH | Admin | ✅ | ✅ | OPEN |
| 2480 | HTTP | ArcadeDB REST API | ✅ | ✅ | OPEN |
| 5432 | TCP | ArcadeDB PG Wire | ✅ | ✅ | OPEN |
| 6379 | TCP | Redis | ✅ | ❌ (provider block) | OPEN in SG, blocked by provider |
| 9118 | HTTP | Hermes Dashboard | ✅ 127.0.0.1 | ❌ (127.0.0.1 bind) | NOT OPEN |
| 9119 | HTTP | Hermes Gateway/API | ✅ | ✅ (when running) | NOT OPEN |

## SSH Commands Reference

```bash
# ArcadeDB
ssh pilarovds@176.108.249.180 "docker logs --tail 20 hermes-db-arcadedb-1"
ssh pilarovds@176.108.249.180 "docker restart hermes-db-arcadedb-1"

# Redis
ssh pilarovds@176.108.249.180 "docker restart hermes-redis"

# Hermes Gateway
ssh pilarovds@176.108.249.180 "fuser -k 9119/tcp; cd ~/hermes-arcade && nohup .venv/bin/python -m hermes_cli.main gateway run > /tmp/hermes-gw.log 2>&1 &"

# Tests
ssh pilarovds@176.108.249.180 "cd ~/hermes-arcade && ARCADEDB_TEST_HOST=127.0.0.1 ARCADEDB_TEST_PASSWORD=hermes123 PYTHONPATH=. ~/.local/bin/pytest tests/test_arcadedb_*.py tests/e2e/ -v"

# Deploy
cd ~/hermes-arcade && git push && ssh pilarovds@176.108.249.180 "cd ~/hermes-arcade && git pull"
```

## Known Issues

| Date | Issue | Status |
|------|-------|--------|
| 07.07 | Hermes Gateway routes `deepseek-chat` through OpenRouter, ignoring custom `providers.openai` | OPEN — use bridge.py or get OpenRouter key |
| 07.07 | Dashboard cannot bind to 0.0.0.0 without auth provider | OPEN — use SSH tunnel |
| 07.07 | Redis port 6379 blocked by cloud provider externally | OPEN — works locally |
| 07.07 | PG vector.neighbors NPE via PG protocol, works via HTTP | Documented in ARCADE_QUIRKS.md |

## Services / Containers

### hermes-db-arcadedb-1
- **Image**: `arcadedata/arcadedb:26.7.1`
- **Ports**: `0.0.0.0:2480->2480/tcp`, `0.0.0.0:5432->5432/tcp`, `9998-9999/tcp` (JMX)
- **JVM**: `-server -Xms2G -Xmx2G`
- **Plugins**: `PostgresProtocolPlugin` (PG wire на 5432)
- **Password**: через `-Darcadedb.server.rootPassword=...` в JAVA_OPTS (НЕ env var!)
- **Database**: `hermes`
- **Health check**: `docker exec hermes-db-arcadedb-1 ...` (нет curl)
- **Logs**: `docker logs --tail 50 hermes-db-arcadedb-1`

### Process (ps aux)
```
PID 1: java -Darcadedb.server.rootPassword=... 
  -Darcadedb.server.plugins=Postgres:...
  -server -Xms2G -Xmx2G
  -Dcom.sun.management.jmxremote.port=9999
  -cp /home/arcadedb/lib/* com.arcadedb.server.ArcadeDBServer
```

## Connectivity (last check: 06.07.2026)

| Port | Protocol | Service | Internal (docker exec) | External | Workaround |
|------|----------|---------|------------------------|----------|------------|
| 2480 | HTTP | ArcadeDB REST API | ✅ 200 OK `[::1]:2480` | ❌ IPv4 disconnect | **SSH tunnel**: `ssh -L 2480:[::1]:2480 pilarovds@176.108.249.180` |
| 5432 | PG Wire | ArcadeDB Postgres | not tested yet | ❌ no banner | **SSH tunnel**: `ssh -L 5432:[::1]:5432 pilarovds@176.108.249.180` |
| 22 | SSH | Admin | — | ✅ | — |

**Root cause**: ArcadeDB 26.7.1 binds to `:::` (IPv6 all-interfaces) by default.
IPv6 `:::` should accept IPv4 mapped addresses, but Docker bridge iptables
mapping appears broken on this host. Container-internal connections work fine.

**Workaround for tests**: Establish SSH tunnels before running pytest:
```
ssh -f -N -L 2480:[::1]:2480 -L 5432:[::1]:5432 pilarovds@176.108.249.180
```
Then set env vars pointing to localhost:
```
$env:ARCADEDB_TEST_HOST = "localhost"
$env:ARCADEDB_TEST_PASSWORD = "hermes123"
```

## Credentials (references only)

| Purpose | How to obtain |
|---------|---------------|
| ArcadeDB root password | `.env` → `ARCADEDB_PASSWORD`; Docker JAVA_OPTS |
| SSH key | `~/.ssh/id_ed25519` → `pilarovds@176.108.249.180` |
| ArcadeDB user | default: `root` |

## SSH Commands Reference

```bash
# Статус контейнера
ssh pilarovds@176.108.249.180 "docker ps --format '{{.Names}} {{.Status}}'"

# Логи (последние 30 строк)
ssh pilarovds@176.108.249.180 "docker logs --tail 30 hermes-db-arcadedb-1"

# Процессы внутри контейнера
ssh pilarovds@176.108.249.180 "docker exec hermes-db-arcadedb-1 ps aux"

# Порты внутри контейнера
ssh pilarovds@176.108.249.180 "docker exec hermes-db-arcadedb-1 netstat -tlnp"

# Перезапуск
ssh pilarovds@176.108.249.180 "docker restart hermes-db-arcadedb-1"
```

## Incidents / Known Issues

| Date | What | Resolution |
|------|------|------------|
| 06.07.2026 | HTTP 2480: недоступен извне (container `:::2480` IPv6, Docker bridge iptables проблема) | SSH tunnel: `ssh -N -L 2480:[::1]:2480 -L 5432:[::1]:5432 pilarovds@...` |
| 06.07.2026 | PG 5432: `password authentication failed for user "root"` — PBKDF2 hash в `server-users.jsonl` сгенерирован НЕ для `hermes123` (переданного через JAVA_OPTS). SCRAM-SHA-256 требует соответствия хеша. | Нужно: сбросить пароль root через HTTP API или пересоздать hash |
| 06.07.2026 | HTTP INSERT с inline 1024-float вектором не парсится в 26.7.1 | Использовать `_vec_to_bytes()` (INT8 encoding) или PG для векторных INSERT |
| 06.07.2026 | SSH tunnel падает при закрытии родительского shell | `Start-Process -WindowStyle Hidden` для detached SSH процесса |
