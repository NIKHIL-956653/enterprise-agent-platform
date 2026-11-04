# Deploying the pilot — runbook

From a bare Ubuntu VPS to three colleges answering questions over HTTPS. Steps are in the
order to do them; each one says what "done" looks like before you move on. Nothing here is a
hostname, a key or a password: every such value lives in `.env.production` on the VPS, which
is gitignored, and is written as `<DOMAIN>`, `<VPS_IP>` and so on below.

What you end up with, all on one machine via `deploy/compose.prod.yml`:

| Service | Image | Reachable from |
|---|---|---|
| `caddy` | `deploy/caddy.Dockerfile` (Caddy 2 + the built `web/`) | the internet, :80 and :443 |
| `api` | `Dockerfile` (uvicorn) | compose network only, via Caddy at `/v1`, `/livez`, `/readyz`, `/docs` |
| `worker` | same image, `python -m eap.worker` | nothing - it pulls work from Temporal |
| `postgres` | `pgvector/pgvector:pg16` | compose network only |
| `redis` | `redis:7-alpine` | compose network only |
| `temporal` | `temporalio/auto-setup:1.24.2` | compose network only |
| `temporal-ui` | `temporalio/ui:2.26.2` | nothing, unless you publish it to localhost and SSH-tunnel |

Caddy serves the React app as static files and proxies `/v1` to the API on the **same
origin**, so the browser never makes a cross-origin request. `CORS_ALLOW_ORIGINS` still has to
name `https://<DOMAIN>`: the API refuses to boot in `prod` with the localhost default (EAP-51),
and that is the one origin it should allow.

Sizing: a 2 vCPU / 4 GB VPS is comfortable for twenty students. Postgres, Temporal and the
two Python processes idle at well under 1.5 GB together; the model calls happen elsewhere.

---

## 1. Provision

On the provider's console: Ubuntu 24.04 LTS, 2 vCPU, 4 GB RAM, 40 GB disk, your SSH key.
Then, as root over SSH, make a user, a firewall and Docker:

```bash
adduser --disabled-password --gecos "" eap
usermod -aG sudo eap
rsync --archive --chown=eap:eap ~/.ssh /home/eap
```

```bash
apt-get update && apt-get -y upgrade
apt-get -y install ufw git curl
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
ufw --force enable
```

```bash
apt-get -y install docker.io docker-compose-v2
usermod -aG docker eap
```

Optional but recommended on 4 GB: a 2 GB swap file so a build or a Temporal restart cannot
take the box down.

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

**Done when:** you can `ssh eap@<VPS_IP>`, `docker ps` works without sudo, and
`sudo ufw status` shows 22, 80 and 443 only. Disable root SSH login after this.

## 2. DNS

At your DNS provider, an `A` record for `<DOMAIN>` pointing at `<VPS_IP>` (and an `AAAA` if
the VPS has IPv6; if it has an IPv6 address but you do not add the record, Caddy still
validates over IPv4). Keep the TTL short (300 s) until the pilot is stable.

**Done when:** `dig +short <DOMAIN>` on your laptop prints `<VPS_IP>`. Do not run step 5
before this is true: Caddy asks Let's Encrypt for a certificate at first start, and a failed
validation counts against the rate limit.

## 3. Clone

As `eap`:

```bash
sudo mkdir -p /opt/eap && sudo chown eap:eap /opt/eap
git clone <REPO_URL> /opt/eap          # the repository's HTTPS clone URL
cd /opt/eap && git checkout v1.1.0   # the pilot release - a frozen build; main moves on
mkdir -p demo-docs/engg demo-docs/mba demo-docs/bba
```

The pilot runs **v1.1.0** and nothing else. `main` keeps moving - the Arabic work (EAP-L5
onwards) lands there during the pilot - and must never be deployed to this box mid-flight.
A fix the pilot needs is a new tag, `v1.1.1`, cut on purpose: see "Ship a fix" under
Day-to-day. `git pull` has no place on the pilot box.

`demo-docs/` is bind-mounted into the API container for the PDFs, the student rosters and
the credentials files the seed writes. It is gitignored. Create it now, as `eap`, so Docker
does not create it as root.

**Done when:** `git describe --tags --exact-match` prints `v1.1.0`, and `ls /opt/eap/deploy`
shows `compose.prod.yml`, `Caddyfile`, `caddy.Dockerfile`.

## 4. Env

```bash
cd /opt/eap
cp .env.production.example .env.production
chmod 600 .env.production
```

Open `.env.production` and fill **every** line. The example names each variable and says
what it is for; the ones people get wrong:

- `DOMAIN` - the bare name, no scheme: `pilot.example.edu`, not `https://...`.
- `CORS_ALLOW_ORIGINS` - a JSON list with the scheme: `["https://pilot.example.edu"]`.
  Anything containing `localhost` or `127.0.0.1` makes the API refuse to start in `prod`.
- `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `JWT_SECRET` - three different random strings
  (`openssl rand -base64 48` each). `APP_DB_PASSWORD` becomes the `eap_app` role's password
  at the first migration (EAP-38); changing it later is `ALTER ROLE` plus this file.
- `GOOGLE_API_KEY` - required even if `LLM_PROVIDER=anthropic`: embeddings are Gemini.
- `DEMO_PASSWORD` - the admins' password. Unset, the seed uses a published dev default.

Do **not** set `DATABASE_URL`, `APP_DATABASE_URL`, `REDIS_URL` or `TEMPORAL_ADDRESS`; the
compose file builds them from the service names and the two passwords.

**Done when:** `grep -c '=$' .env.production` prints `0` for the required lines (the
per-tenant `TENANT_*` lines may stay commented out), and the file is mode `600`.

## 5. Up

```bash
cd /opt/eap
docker compose --env-file .env.production -f deploy/compose.prod.yml up -d --build
```

The first build takes a few minutes (Python dependencies, `npm ci`, the Vite build). Caddy
obtains the certificate on its first start; the API waits for Postgres and Redis to pass
their health checks; the worker waits for the API to be healthy.

```bash
docker compose --env-file .env.production -f deploy/compose.prod.yml ps
docker compose --env-file .env.production -f deploy/compose.prod.yml logs -f caddy api
```

**Done when:** every service is `running` (api `healthy`) and the Caddy log shows
`certificate obtained successfully` for `<DOMAIN>`. If the API restarts in a loop, read its
log: a `prod` settings check failing (JWT, CORS, Google key) prints the exact field.

Every later command below uses the same prefix; alias it:

```bash
alias dc='docker compose --env-file /opt/eap/.env.production -f /opt/eap/deploy/compose.prod.yml'
```

## 6. Migrate

```bash
dc run --rm api alembic upgrade head
dc run --rm api alembic check
```

The first run creates the `eap_app` role with `APP_DB_PASSWORD` (migration 0005), enables
pgvector and pgcrypto, and builds every table with RLS + FORCE. `alembic check` must print
`No new upgrade operations detected.`

**Done when:** `dc exec postgres psql -U eap -d eap -c '\dt'` lists `tenants`, `users`,
`agents`, `agent_runs`, `documents`, `document_chunks`, `audit_log`, `feedback`, and
`dc restart api worker` leaves both healthy (they could not connect as `eap_app` before this).

## 7. Seed

Tenants, admins and agents (idempotent):

```bash
dc run --rm api python scripts/seed_demo.py
```

This prints the logins and each college's daily token budget (5,000,000) and never a
password. Then the students: put each college's roster at
`demo-docs/<slug>/students.csv` (header `name,email`) and run

```bash
dc run --rm --user "$(id -u):$(id -g)" api python scripts/seed_students.py
```

`--user` makes the files it writes yours rather than the container user's. It creates one
`member` at clearance 1 per row and writes `demo-docs/<slug>/credentials.txt`
(`email<TAB>name<TAB>password`, mode 600) - the **only** place those passwords exist. Hand
the lines to students yourself; re-running changes nothing unless you pass `--rotate`.

**Done when:** `dc run --rm api python scripts/ops_report.py --days 1` lists `acme`,
`engg`, `mba`, `bba` as columns, and `wc -l demo-docs/*/credentials.txt` matches the rosters.

## 8. Ingest

Course PDFs go in `demo-docs/<slug>/`, faculty-only material in a separate folder. The
script logs in as each college's admin and posts through the API, so RLS checks every write:

```bash
dc run --rm api python scripts/ingest_pdfs.py --api-url http://api:8000 --dry-run    # extract and report only
dc run --rm api python scripts/ingest_pdfs.py --api-url http://api:8000              # classification 0 (public)
dc run --rm api python scripts/ingest_pdfs.py --api-url http://api:8000 --tenant engg --classification 3 --layout "demo-docs/faculty/{tenant}/*.pdf"
```

Scanned PDFs with no text are skipped and named; long files are posted in parts. The file
name is the `external_id`, so re-running replaces a document rather than duplicating it.
This step spends embedding tokens; the dry run first is not optional on a real library.

**Done when:** every file has a line saying `ingested` with a chunk count, and in the UI
(next step) the Documents tab for each college lists its own files only.

## 9. Verify

```bash
curl -s https://<DOMAIN>/readyz
```

must print `{"status":"ready","checks":{"postgres":"ok","redis":"ok"}}` with HTTP 200. A 503
names the dependency that is down. Then, in a browser:

1. Open `https://<DOMAIN>`, log in as `admin@engg.edu` / `$DEMO_PASSWORD`, tenant `engg`.
2. Agents → **Answer from documents** → ask something a syllabus answers. You get an answer
   with citations; the thumbs appear under it.
3. Ask something nothing covers. You get the refusal, not an invention.
4. Log out; log in as a student from `credentials.txt`. Documents shows public and internal
   files only; a question about faculty-only material is refused the same way as an
   unknown topic - and the Ops tab is not in their navigation.
5. Back as the admin: **Ops** shows today's runs, the latency, the refusal rate and your
   thumbs.

Then from the VPS, the operator's view across all colleges:

```bash
dc run --rm api python scripts/ops_report.py --days 7
```

**Done when:** all five checks pass for one college, and a second college's admin sees none
of the first college's documents or runs.

---

## Backups

One nightly `pg_dump` of the `eap` database, kept 14 days. As `eap`, `crontab -e`:

```bash
30 2 * * * mkdir -p /var/backups/eap && docker compose --env-file /opt/eap/.env.production -f /opt/eap/deploy/compose.prod.yml exec -T postgres pg_dump -U eap -Fc eap > /var/backups/eap/eap-$(date +\%F).dump && find /var/backups/eap -name 'eap-*.dump' -mtime +14 -delete
```

02:30 server time; set the VPS clock to UTC and that is 08:00 IST, before classes. Copy the
folder off the machine as well (`rsync` to your laptop or object storage) - a backup on the
disk it protects is a convenience, not a backup. Temporal's own `temporal` and
`temporal_visibility` databases are deliberately not dumped: they hold workflow history, which
is operational state, not the record; after a restore, in-flight runs show as failed and the
`agent_runs` rows say so.

`pg_dump -Fc` is the custom format, so a restore can be selective and runs through
`pg_restore`.

### Restore

Into a fresh or emptied database. Stop the writers first so nothing lands mid-restore:

```bash
dc stop api worker
```

```bash
dc exec -T postgres pg_restore -U eap -d eap --clean --if-exists --no-owner < /var/backups/eap/eap-<DATE>.dump
```

```bash
dc run --rm api alembic upgrade head && dc start api worker && curl -s https://<DOMAIN>/readyz
```

`--clean --if-exists` drops and recreates each object before loading it. `alembic upgrade
head` afterwards is a no-op when the dump is current and the right move when it predates a
migration. The `eap_app` role is cluster-level, not in the dump: on a brand-new Postgres
volume run step 6 before the restore so the role exists, then restore.

Test the restore once before the pilot starts, on the VPS, with a dump you just took.
A backup that has never been restored is a hope.

## Day-to-day

- **Logs:** `dc logs -f api worker` (JSON lines; every line carries a `request_id`).
- **Ship a fix during the pilot:** never `git pull`, and never deploy `main`. Cut the fix on
  a branch from `v1.1.0` (or cherry-pick a reviewed commit from main), get CI green, then tag
  it deliberately - `git tag -a v1.1.1 -m "pilot fix: <what>"`, `git push origin v1.1.1` -
  and on the box: `git fetch --tags && git checkout v1.1.1`, `dc up -d --build`,
  `dc run --rm api alembic upgrade head`. Caddy and Postgres keep their volumes; the API,
  worker and Caddy images are rebuilt. The next fix is `v1.1.2`; the pilot box only ever
  moves between tags you chose.
- **Rotate the app DB password:** `dc exec postgres psql -U eap -d eap -c "ALTER ROLE eap_app PASSWORD '<new>'"`,
  edit `APP_DB_PASSWORD`, `dc up -d api worker`.
- **A college's budget:** `dc exec postgres psql -U eap -d eap -c "UPDATE tenants SET daily_token_budget = <n> WHERE slug = '<slug>'"`.
  0 means no cap. The students see a 429 with the reset time once it is spent.
- **Temporal UI:** uncomment the `ports` line on `temporal-ui`, `dc up -d temporal-ui`, then
  `ssh -L 8233:localhost:8233 eap@<VPS_IP>` and open `http://localhost:8233`. It has no
  login of its own; never publish it to `0.0.0.0`.

## What is logged, and what to tell the colleges

The platform keeps an **audit trail per tenant** (`audit_log`, append-only, hash-chained).
For every question a student asks it records the question, the chunks retrieved and their
scores, the full prompt sent to the model and the model's full completion, with a redaction
pass for emails, phone numbers and card numbers before anything is written. A tenant's
admin can read every event of every run of their own college on the run detail page; the
operator can read all of it in the database. The model provider (Google or Anthropic) also
receives the prompt, under its own terms.

Students' thumbs and comments are stored against their account (`feedback`). Accounts are
email-identified; there is no name column, only the roster file on the VPS.

Tell each college's admin, before the first login, in writing: questions are recorded with
the asker's account and are visible to the college's admin; they are sent to a third-party
model provider; they are kept for the pilot and deleted at its end (decide the date now);
and students should not type personal details into the question box. Put the same sentence
on the login page or the roster hand-out so the students hear it too.
