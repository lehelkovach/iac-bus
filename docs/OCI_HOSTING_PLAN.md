# OCI Hosting Plan

How the IAC Bus runs as a service on Oracle Cloud Infrastructure: the topology,
the release process, and the operational rules that follow from how the bus is
built today.

`OCI_DEPLOYMENT.md` stays as the short "get a VM up" guide. This document is the
plan for running it as something a swarm can depend on.

Companion documents:

- `docs/SWARM_DEV_PLAN.md` - the product work each hosting phase depends on
- `docs/SWARM_TESTING_PLAN.md` - the gates every release passes

## 1. Current state

Verified against the repository:

| Aspect | Today |
| --- | --- |
| Compute | One Ubuntu VM, default shape `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB) |
| Provisioning | `scripts/provision-oci-dev-vm.py` using `OCI_*` secrets |
| Process | systemd `iac-bus.service` running `python server.py`, the Werkzeug development server |
| Dev process | `iac-bus-dev.service` running `watchmedo auto-restart` for hot reload |
| User | `root` |
| Exposure | Plain HTTP on port 8091, public IP, no TLS |
| Auth | One shared static bearer token, `BUS_API_TOKEN` |
| State | Entirely in process memory |
| Backup | None, because there is nothing durable to back up |
| Monitoring | `journalctl` on the box |

It works, and for a two-agent handoff it is proportionate. The gaps below are
what stand between that and hosting a swarm.

## 2. Operating rules that follow from the design

These are not style preferences. Each one is a property of the current code and
was confirmed by measurement.

### Rule 1: exactly one worker process

All state lives in module-level globals guarded by a `threading.Lock`. Running
more than one process gives each its own queues, cursors, and jobs.

Measured against `gunicorn -w 4`: a job created on one connection was invisible
to 19 of 20 fresh connections, and readers on new connections saw either all 20
broadcast messages or none, depending on which process accepted them.
Connection keep-alive pins a client to one process, so a casual smoke test with
a single session passes while the deployment is silently broken.

Because this is easy to get wrong and hard to notice, `scripts/bus_conformance.py`
includes a required check that opens fresh connections and fails the release if
they disagree.

The supported production process model until phase S6 of the swarm plan:

```bash
gunicorn --workers 1 --threads 32 --bind 127.0.0.1:8091 \
         --timeout 120 --graceful-timeout 30 server:app
```

Verified against this exact command: all 16 conformance checks pass and all
three harness scenarios run clean. Threads give concurrency for I/O-bound
handlers, one process keeps state coherent, and `--timeout 120` leaves room for
the long-poll work in swarm phase S2. Werkzeug's development server, which the
current systemd unit runs, should not serve production traffic.

### Rule 2: a restart is data loss

Restarting the service discards every pending task, every lease, and every
orchestration job. Three consequences:

- **The dev VM's hot reload is a queue-wiping trigger.** `watchmedo` restarts on
  any `*.py`, `*.json`, `*.md`, `*.yaml` change, so an unrelated docs edit
  destroys in-flight work. Acceptable for a scratch environment, never for one a
  swarm depends on.
- **`deploy.sh` deletes `/opt/iac-bus/*` before restarting.** Any deploy is a
  full coordination reset.
- **Unattended upgrades and reboots do the same thing,** without warning.

Until swarm phase S3 delivers durable state, deploys must be scheduled against a
drained swarm. After S3, this rule relaxes to a graceful drain.

### Rule 3: capacity is a fixed shared budget

Throughput is flat at roughly 330 operations/second regardless of how many
members connect, because every request serializes on one lock. Adding members
adds latency, not capacity, and per-request cost also grows with retained
messages. Full figures are in `docs/SWARM_DEV_PLAN.md` section 2.

For hosting this means **a larger shape buys very little.** Scale by reducing
load (longer poll intervals, shorter retention) rather than by upsizing, and
treat "we need more throughput" as a trigger for swarm phase S6, not a
procurement decision.

## 3. Target topology

```
                    Internet
                        |
              [ DNS: bus.<domain> ]
                        |
        +---------------v----------------+
        |  OCI Load Balancer (H4) / or   |
        |  Caddy on the instance (H2)    |   TLS termination, HTTP -> HTTPS
        +---------------+----------------+
                        | 127.0.0.1:8091
        +---------------v----------------+
        |  Compute instance, private     |
        |  subnet                        |
        |   - gunicorn, 1 worker         |
        |   - systemd, non-root user     |
        |   - SQLite state (after S3)    |
        +---------------+----------------+
                        |
        +---------------v----------------+
        |  Object Storage: state backups |
        |  Vault: BUS_API_TOKEN          |
        |  Monitoring: metrics + alarms  |
        +--------------------------------+
```

Network layout:

| Component | Setting |
| --- | --- |
| VCN | One per environment, non-overlapping CIDRs |
| Public subnet | Load balancer or NAT only |
| Private subnet | The bus instance; no public IP after H2 |
| NSG: ingress 443 | From the internet, to the load balancer only |
| NSG: ingress 8091 | From the load balancer NSG only, never `0.0.0.0/0` |
| NSG: ingress 22 | From a bastion or the operator CIDR only |
| NSG: egress | Restricted to what the bus needs, which is almost nothing |

The single most important change from today: **port 8091 stops being reachable
from the internet.** It currently carries unencrypted bearer tokens on a public
IP, which means the swarm's shared credential is on the wire in plaintext.

## 4. Environments

| Environment | Purpose | Lifetime | Data |
| --- | --- | --- | --- |
| `dev` | Hot-reload development, breaking changes welcome | Disposable | Throwaway |
| `staging` | Release gate; identical config to prod | Long-lived | Synthetic swarm traffic |
| `prod` | Real swarms | Long-lived | Real coordination state |

Rules: staging and prod run the same process model and the same shape; only dev
uses hot reload; every prod release passes staging first; no member ever points
at more than one environment.

Naming: `iac-bus-<env>` for instances, `bus-<env>.<domain>` for DNS,
`BUS_API_TOKEN_<ENV>` for secrets.

## 5. Phases

### H0 - Release gating (available now)

**Goal.** Never deploy a bus that fails the contract.

**Deliverables.**
- `scripts/bus_conformance.py` runs after every deploy; a failure blocks or
  rolls back.
- `scripts/swarm_harness.py` runs a smoke swarm against the deployed URL.
- CI workflow `.github/workflows/swarm-tests.yml` gates merges.

**Exit criteria.** A deploy that violates the single-process rule, or breaks
leasing, fails automatically rather than being discovered by an agent.

**Depends on.** Swarm phase S0. Done.

### H1 - Hardened single instance

**Goal.** The current architecture, run properly.

**Deliverables.**
- **Process model.** Replace `python server.py` with the gunicorn command in
  Rule 1. Keep one process.
- **Least privilege.** Dedicated `iac-bus` service user, not root. Add systemd
  sandboxing: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
  `PrivateTmp`, `RestrictAddressFamilies=AF_INET AF_INET6`, and a `ReadWritePaths`
  entry for the state directory only.
- **TLS.** Caddy on the instance terminating 443 with an automatic certificate,
  proxying to `127.0.0.1:8091`. Bind gunicorn to loopback so the plain port is
  unreachable from outside.
- **Firewall.** NSG plus host firewall; 8091 loopback-only.
- **Secrets.** `BUS_API_TOKEN` from OCI Vault, read at start, never in the repo
  or in a workflow log. Document a rotation procedure, since one shared token
  means rotation is a swarm-wide event until swarm phase S6.
- **Config.** Set `BUS_MAX_MESSAGES` and `BUS_RETENTION_SECONDS` from the
  sizing formula in section 7 rather than leaving defaults.
- **Unattended upgrades.** Disable automatic reboots; patch on a schedule with a
  drained swarm, per Rule 2.

**Exit criteria.** Conformance passes over HTTPS; port 8091 unreachable from
outside the host; service runs unprivileged; a `curl` to the plain HTTP port
fails.

**Depends on.** Nothing. This is the highest-value hosting work available and it
can start immediately.

### H2 - Survivable service

**Goal.** A restart or patch stops being an incident.

**Deliverables.**
- Adopt swarm phase S3 durable state; put the SQLite file on a block volume
  separate from the boot volume.
- Nightly state backup to Object Storage with lifecycle expiry, and a restore
  runbook that is actually exercised.
- Graceful drain on shutdown: stop accepting claims, let leases finish or expire,
  flush state, then exit. `--graceful-timeout` on gunicorn plus a systemd
  `ExecStop` hook.
- Move the instance to the private subnet behind the load balancer.
- Block volume backup policy for point-in-time recovery.

**Exit criteria.** A planned restart during an active swarm loses no tasks,
proven by the harness restart scenario in the testing plan. A restore drill
rebuilds state from a backup.

**Depends on.** Swarm phase S3.

### H3 - Observable service

**Goal.** Answer "is the swarm healthy" without SSH.

**Deliverables.**
- Publish swarm phase S5 metrics to OCI Monitoring as custom metrics: queue
  depth, oldest pending age, claim rate, member count, request latency.
- Forward journald to OCI Logging with structured JSON, retained 30 days.
- Alarms, with the reasoning for each threshold recorded:
  - oldest pending task age > 5 minutes (work is stuck, not slow)
  - claim p95 > 250 ms (the shared budget is saturated; see Rule 3)
  - member count drops by more than half within 5 minutes (mass agent failure)
  - health check failing for 2 consecutive minutes (page)
  - certificate expiring within 14 days
- A dashboard covering queue depth, member count, latency, and error rate.

**Exit criteria.** A deliberately stalled queue is detected and diagnosed from
the dashboard alone during a game-day exercise.

**Depends on.** Swarm phase S5.

### H4 - Scale-out and zero-downtime

**Goal.** Remove the single-instance ceiling.

**Deliverables.**
- OCI Load Balancer in front of two or more instances, health check on `/health`.
- Shared state backend from swarm phase S6, so instances agree.
- Blue/green or rolling deploys with conformance run against the new instance
  before it takes traffic.
- Multi-AD placement for availability.

**Exit criteria.** Two instances serving one queue deliver every task exactly
once under the harness flood scenario, and a deploy completes with no failed
member requests.

**Depends on.** Swarm phase S6. **Do not start earlier:** putting a load
balancer in front of the current bus multiplies the state-sharding failure
proven in Rule 1 across instances instead of processes.

## 6. Release process

Every change to a hosted environment follows the same path:

1. **CI on the pull request.** Unit, schema, and swarm tests, plus the harness in
   `--embedded` mode. Merge is blocked on green.
2. **Deploy to staging.** `scripts/deploy-dev-vm.sh` against the staging host.
3. **Verify staging.** `bus_conformance.py --slow`, then `swarm_harness.py all`.
   Any required check or invariant violation stops the release.
4. **Drain prod.** Announce on the `ops` channel, wait for in-flight leases, and
   confirm queue depth is zero. Until swarm phase S3, this step is mandatory
   because of Rule 2.
5. **Deploy prod,** then immediately run conformance against it.
6. **Smoke the swarm.** A short harness flood with a small task count.
7. **Roll back** on any failure: previous release directory, restart, re-verify.

The verification commands, which are the same in every environment:

```bash
python3 scripts/bus_conformance.py --bus-url "$BUS_URL" --token "$BUS_API_TOKEN" --slow
python3 scripts/swarm_harness.py all --bus-url "$BUS_URL" --token "$BUS_API_TOKEN" \
    --workers 8 --tasks 200
```

Both exit non-zero on failure, so they drop straight into a pipeline.

## 7. Sizing

**Shape.** The bus is bound by a single lock, so extra OCPUs mostly idle. Start
with `VM.Standard.A1.Flex` at 2 OCPU / 12 GB, which is within the Always Free
allowance and leaves room for TLS termination and long-poll threads. The
default `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB) is adequate for the bus process
alone but leaves nothing for Caddy, backups, and monitoring agents.

**Memory.** Messages are held in memory, so plan
`BUS_MAX_MESSAGES * average_message_size * ~2`. At 20,000 messages of 2 KB that
is roughly 80 MB, comfortable on any of these shapes.

**Retention.** Choose the buffer from load rather than leaving the default:

```
BUS_MAX_MESSAGES  >=  (peak in-flight tasks)
                    + (members * messages_per_member_per_minute
                       * BUS_RETENTION_SECONDS / 60)
                    * 1.5 safety factor
```

Keep the result under about 20,000 so per-request cost stays near 4 ms, and
prefer lowering `BUS_RETENTION_SECONDS` over raising the cap. Undersizing this
causes the silent task loss described in the swarm plan as gap G1; the default
of 500 is far too small for any real swarm.

**Cost.** A single Always Free A1.Flex instance, one block volume, and Object
Storage backups fit within or near the Always Free allowance. Load balancing in
phase H4 is the first materially billable component.

## 8. Runbooks

**Deploy.** Follow section 6. Never skip the drain until swarm phase S3 lands.

**Rollback.** Restore the previous release directory, restart the service,
re-run conformance. Coordination state is lost either way today, so tell the
swarm to resynchronise on the `ops` channel.

**Rotate the token.** Update Vault, restart the bus, then update every member.
Because the token is shared, this is a coordinated swarm-wide change; swarm
phase S6 replaces it with per-agent credentials that rotate individually.

**Swarm is stalled.** Check queue depth and oldest pending age (phase H3), then
confirm whether leases are expiring without acks, which means members are dying
mid-task. Look for `429` or eviction if depth is at the cap. Verify the
single-process rule still holds by running conformance, since an accidental
multi-worker deploy presents exactly as "some agents see no work".

**Suspected message loss.** Compare tasks posted against tasks acked using the
harness flood scenario in a staging environment with production settings. If the
buffer is undersized, this reproduces immediately.

**Instance lost.** Provision with `scripts/provision-oci-dev-vm.py`, deploy,
restore state from the most recent backup (after H2), re-run conformance, then
announce recovery on `ops`.

## 9. Risk register

| Risk | Severity | Mitigation | Phase |
| --- | --- | --- | --- |
| Tokens in plaintext on a public IP | High | TLS, private subnet | H1 |
| Multi-worker deploy shards state silently | High | Conformance check, documented process model | H0, done |
| Restart discards in-flight work | High | Drain procedure now, durable state later | H2 |
| Undersized buffer drops tasks silently | High | Sizing formula, backpressure | H1, S3 |
| Service runs as root | Medium | Service user, systemd sandboxing | H1 |
| Shared token cannot be revoked per agent | Medium | Per-agent credentials | S6 |
| Single instance is a single point of failure | Medium | Load-balanced multi-instance | H4 |
| No backups | Medium | Object Storage backups, restore drills | H2 |
| Throughput ceiling reached | Low today | Reduce load; scale out later | S6, H4 |

## 10. Prerequisites

Provisioning is currently blocked on the secrets listed in `docs/AGENT_TASKS.md`
(B-001). To proceed with phase H1 the following must be available to CI:

- `OCI_TENANCY_OCID`, `OCI_USER_OCID`, `OCI_FINGERPRINT`, `OCI_REGION`
- `OCI_COMPARTMENT_OCID`, `OCI_SUBNET_OCID`, `OCI_IMAGE_OCID`
- `OCI_SSH_PUBLIC_KEY`, and `OCI_PRIVATE_KEY` or `OCI_PRIVATE_KEY_B64`
- `KSG_DEV_VM_HOST`, `KSG_DEV_VM_USER`, `KSG_DEV_VM_KEY`
- `BUS_API_TOKEN`, ideally sourced from Vault rather than repository secrets

Two decisions are also needed before H1: the DNS name for each environment, and
whether TLS terminates on the instance (Caddy, simplest) or at an OCI Load
Balancer (needed anyway at H4).
