# System Design

This document records the decisions behind the Identity Reconciliation service
and, where a decision was a deliberate trade-off for the scope of this
exercise, says so plainly.

---

## 1. The problem, restated

Requests arrive carrying an email, a phone number, or both. Any two contacts
that share either identifier belong to the same person. The service must
return the full consolidated view of that person on every request, and must
survive the case where a single request reveals that two groups it previously
believed were different people are in fact one.

That is a **disjoint-set (union-find)** problem. Each request is either a
lookup, an insert, or a union. Framing it that way is what makes the edge case
tractable rather than a pile of special cases.

---

## 2. Data model: flattened star topology

The obvious model is a linked list — each contact points at whatever it was
linked to. It is also wrong, and it fails in a way that does not show up until
the third or fourth request in a chain.

Instead, the schema maintains a **star** invariant:

```
        primary (linkPrecedence = "primary", linkedId = NULL)
       /   |   \
      s1   s2   s3        (linkPrecedence = "secondary", linkedId = primary.id)
```

**Every secondary points directly at the primary. Depth is always exactly one.**
There are no chains, so there is no traversal.

### Why this matters

| | Chain / tree model | Flattened star |
|---|---|---|
| Resolve a cluster | Recursive CTE or N queries | `WHERE id = P OR linkedId = P` — one indexed query |
| Cost | O(depth) round trips | O(1) round trips |
| Failure mode | A broken link silently orphans a subtree | Invariant is checkable in one query |

### The invariant is load-bearing during merges

When a request bridges two clusters, the naive fix is to demote the younger
primary and move on. That leaves this:

```
   older_primary          younger (now secondary, linkedId = older) 
                              ↑
                            s4, s5   ← still pointing at "younger"
```

`s4` and `s5` are now at depth 2. The next `WHERE id = P OR linkedId = P`
query does not see them, so half the person's contact details disappear from
the response — and nothing errors. This is the single most common bug in
implementations of this problem.

`_merge_clusters()` therefore re-parents the absorbed cluster's children
**before** demoting its root, in one bulk `UPDATE` rather than a row-per-loop:

```sql
UPDATE contacts SET linkedId = :survivor WHERE linkedId = :absorbed;
```

One statement per absorbed cluster, regardless of how many members it has.

### Choosing the survivor

Ordering is `created_at ASC, id ASC`. The spec says the older contact stays
primary; the `id` tiebreak exists because two rows created inside the same
clock tick — routine when a test suite hammers the endpoint — would otherwise
pick a non-deterministic winner and make the test suite flaky.

### Indexing

| Index | Serves |
|---|---|
| `email`, `phoneNumber` | the initial match lookup (the `OR` is index-friendly on both branches) |
| `linkedId` | cluster expansion |
| `(linkPrecedence, createdAt)` | survivor selection and reporting |

Without the first two, every `/identify` call is a full table scan.

### Soft deletes

`deletedAt` is never written by the service, but every read path filters on
it. This means a future retention or erasure job can tombstone rows without
corrupting the clusters that reference them.

---

## 3. Opaque error handling

Every failure — validation, HTTP, or unhandled exception — returns the same
flat body:

```json
{ "status": "rejected", "message": "Transmission could not be processed.", "reference": "a3f91c04b2d7" }
```

The real cause is written to the server log under that reference id.

This satisfies the assignment's "misdirect potential threats" bonus, but it is
not a gimmick — it is standard practice, for three concrete reasons:

1. **No schema disclosure.** FastAPI's default `422` response enumerates every
   field, its expected type, and exactly which one failed. That is a free map
   of the API for anyone probing it. Returning `400` with a fixed body gives
   an attacker no gradient to climb.
2. **No oracle.** If malformed-email and malformed-phone produced
   distinguishable errors, the endpoint becomes a probe for which identifiers
   the system considers valid.
3. **No stack traces in production.** The catch-all handler guarantees an ORM
   or driver exception can never leak a query, a table name, or a file path to
   a caller.

The reference id is the deliberate escape hatch: support can resolve any user
report to an exact log line without the response itself carrying detail.

**Trade-off:** this makes the API less pleasant for legitimate integrators
debugging their payloads. In a real product the honest resolution is
environment-dependent verbosity — detailed errors in staging, opaque in
production — not opacity everywhere.

---

## 4. Concurrency

`reconcile()` is a read-modify-write across several rows. Two simultaneous
requests carrying the same new identifier can both observe "no match" and both
create a primary, splitting one person into two clusters.

**Current:** a process-wide `threading.Lock` around the transaction. SQLite
serialises writers at the file level anyway, so this costs nothing real and
makes behaviour deterministic under test.

**Correct at scale:** run the transaction at `SERIALIZABLE` isolation on
Postgres, or take a row lock (`SELECT ... FOR UPDATE`) on the surviving
primary before merging, plus a unique partial index on `(email)` and
`(phoneNumber)` as a backstop. Retry on serialisation failure.

The lock is a single-process guarantee. It does **not** hold across replicas —
which brings us to the real limitation.

---

## 5. State: the honest caveat

The service uses SQLite on a local file. The Kubernetes manifest runs 2
replicas and an HPA that scales to 10.

**These are incompatible, and knowingly so.** Each pod mounts its own
`emptyDir` and therefore its own database. Two requests from the same person
landing on different pods produce two unrelated clusters, and the data is gone
when the pod is replaced.

SQLite was specified for the application task; the deployment task asks for
horizontal autoscaling. Rather than paper over the conflict, the shape of the
fix is:

1. Replace SQLite with managed Postgres (RDS, Cloud SQL). One line —
   `DATABASE_URL` — because SQLAlchemy abstracts the dialect.
2. Move the in-process lock to database-level isolation (§4).
3. Add Alembic migrations. `create_all()` is fine for a demo and unacceptable
   once a schema change has to be reversible.

Only then does replica count above 1 mean anything.

For a genuinely single-replica demo, the interim fix is a `StatefulSet` with a
`PersistentVolumeClaim`, `replicas: 1`, and no HPA.

---

## 6. Container and cluster decisions

**Multi-stage build.** The builder stage carries `build-essential` and pip's
machinery; the runtime stage receives only the resolved virtualenv. Smaller
image, and no compiler left in the runtime for an attacker to use.

**Non-root, read-only root filesystem, all capabilities dropped.** Root in a
container is still root against the host kernel if anything escapes the
namespace. `/tmp` and `/data` are mounted as writable `emptyDir` volumes
because the root filesystem is read-only.

**Health check via `python -c`.** The slim image ships neither `curl` nor
`wget`, and installing one purely for a probe adds attack surface for no gain.

**Three probes, not one.**
- *Liveness* — process is wedged, restart it.
- *Readiness* — pull a busy pod from the Service endpoints instead of killing it.
- *Startup* — protect a slow first boot from being killed by the liveness probe.

Collapsing these into one probe means a momentarily slow pod gets restarted
instead of drained, which converts a latency blip into an outage.

**Requests and limits.** The HPA measures utilisation against the CPU
*request*, not the limit. A Deployment with no requests gives the HPA no
denominator and it silently refuses to scale. Requests are set well below
limits so pods can burst.

**Asymmetric HPA behaviour.** Scale up fast (30s window), scale down slowly
(300s window). Symmetric windows cause replica thrashing on bursty traffic.

**Namespace per version.** `identity-v1`, `identity-v1-1`, `identity-v2` are
fully isolated — separate ConfigMaps, quotas and RBAC — which makes it
possible to run all three concurrently and route between them at the Ingress.

**Ingress rewrite.** The app serves `/identify` at the root and knows nothing
about versions. `rewrite-target: /$2` strips the `/v1` prefix at the edge, so
the identical image works behind any version path. Version routing is an
infrastructure concern, not an application one.

**PodDisruptionBudget.** Keeps at least one pod serving through node drains
and cluster upgrades.

---

## 7. CI/CD

Tests gate the image build; an image is never pushed from code that fails its
own suite. Pull requests build the Dockerfile (proving it still works) but
never push — a fork PR must not be able to publish to the registry.

Tags are derived from the git ref by `docker/metadata-action`, so
`git tag v1.1.0 && git push --tags` produces `:1.1.0`, `:1.1`, `:1` and
`:latest`. The image tag and the semver tag cannot drift apart, because one
is generated from the other.

Trivy scans for `CRITICAL`/`HIGH` CVEs and fails the job on a finding, so a
known-vulnerable base image cannot reach the cluster.

---

## 8. What is deliberately not here

| Missing | Why | What production needs |
|---|---|---|
| Alembic migrations | `create_all()` suffices for a demo | Versioned, reversible schema changes |
| AuthN / AuthZ | Out of scope | API keys or mTLS; `/identify` exposes PII |
| Rate limiting | Out of scope | The endpoint is an enumeration oracle without it |
| Structured logging / tracing | Out of scope | JSON logs, OpenTelemetry, request ids end to end |
| TLS | Needs a real domain | cert-manager + Let's Encrypt at the Ingress |
| Terraform | Minikube is local | IaC for the cluster itself |

Listing these is the point: a system design document that claims no
limitations is not a design document.
