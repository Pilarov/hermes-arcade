# Infrastructure — Hermes Arcade

## Servers

| Name | IP | OS | Role | Access |
|------|----|----|------|--------|
| pilarovds | 176.108.249.180 | Linux | ArcadeDB host | `ssh pilarovds@176.108.249.180` |

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
