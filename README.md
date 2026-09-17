# Identity Reconciliation Service

A FastAPI service that consolidates contact details belonging to the same
person across multiple orders, exposed as a single `POST /identify` endpoint.

Architecture and trade-offs are documented in
[SYSTEM_DESIGN.md](./SYSTEM_DESIGN.md).

---

## Contents

```
.
├── main.py                        # FastAPI app, endpoints, reconciliation logic
├── models.py                      # SQLAlchemy models, engine, session
├── requirements.txt
├── Dockerfile                     # multi-stage, non-root, health-checked
├── .dockerignore
├── k8s-manifest.yaml              # Namespace, Deployment, Service, HPA, Ingress, PDB
├── .github/workflows/deploy.yml   # test → build → push → scan
├── CHANGELOG.md
├── README.md
└── SYSTEM_DESIGN.md
```

---

## The API

### `POST /identify`

At least one of the two fields must be present.

```json
{ "email": "doc@zamazon.com", "phoneNumber": "9876543210" }
```

Always returns `200` on success:

```json
{
  "contact": {
    "primaryContactId": 1,
    "emails": ["doc@zamazon.com", "chandra@zamazon.com"],
    "phoneNumbers": ["9876543210"],
    "secondaryContactIds": [2]
  }
}
```

The primary's own email and phone lead their respective lists.

### `GET /health`

```json
{ "status": "ok" }
```

### Errors

Every failure returns the same shape with a `400` or `500` status — never a
field-by-field validation dump. See §3 of SYSTEM_DESIGN.md for why.

```json
{ "status": "rejected", "message": "Transmission could not be processed.", "reference": "a3f91c04b2d7" }
```

The `reference` maps to a full explanation in the server log.

---

## Run locally (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Interactive docs: <http://localhost:8000/docs>

```bash
curl -X POST http://localhost:8000/identify \
  -H "Content-Type: application/json" \
  -d '{"email":"doc@zamazon.com","phoneNumber":"9876543210"}'
```

The SQLite file `contacts.db` is created in the working directory on first
start. Delete it to reset state.

---

## Run with Docker

```bash
docker build -t identity-service:1.0.0 .

docker run --rm -p 8000:8000 \
  -v identity-data:/data \
  --name identity \
  identity-service:1.0.0
```

The named volume keeps the database across container restarts; without it the
data disappears when the container is removed.

Check the container's own health status:

```bash
docker inspect --format '{{.State.Health.Status}}' identity
```

It reports `starting` for the first ~10s, then `healthy`.

### Environment variables

| Variable | Default (in image) | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:////data/contacts.db` | Database connection string |
| `APP_PORT` | `8000` | Listen port |
| `LOG_LEVEL` | `INFO` | Log verbosity |

Secrets are never baked into the image. Locally, pass an env file
(`docker run --env-file .env ...`) and keep `.env` out of git; in Kubernetes,
use a `Secret`.

---

## Deploy to Kubernetes (Minikube)

### 1. Start the cluster and enable the add-ons

```bash
minikube start --cpus=2 --memory=4096
minikube addons enable ingress        # NGINX ingress controller
minikube addons enable metrics-server # required — the HPA cannot scale without it
```

### 2. Make the image available

Either build straight into Minikube's daemon:

```bash
eval $(minikube docker-env)
docker build -t identity-service:1.0.0 .
```

…and set `imagePullPolicy: Never` in the manifest, **or** pull from Docker Hub
by replacing the placeholder:

```bash
sed -i 's|DOCKERHUB_USERNAME|your-dockerhub-username|g' k8s-manifest.yaml
```

### 3. Apply

```bash
kubectl apply -f k8s-manifest.yaml
kubectl get all -n identity-v1
kubectl rollout status deployment/identity-service -n identity-v1
```

### 4. Route to it

```bash
echo "$(minikube ip) identity.local" | sudo tee -a /etc/hosts

curl -X POST http://identity.local/v1/identify \
  -H "Content-Type: application/json" \
  -d '{"email":"doc@zamazon.com","phoneNumber":"9876543210"}'
```

The Ingress strips the `/v1` prefix, so the pod sees `/identify`.

### 5. Watch the autoscaler

```bash
kubectl get hpa -n identity-v1 -w
```

Generate load in another terminal:

```bash
kubectl run -n identity-v1 load --rm -it --image=busybox --restart=Never -- \
  sh -c 'while true; do wget -q -O- http://identity-service/health; done'
```

Replicas should climb past 2 once average CPU crosses 70% of the **request**
(100m), then settle back after the 300s scale-down window.

> **Important:** the manifest runs multiple replicas against a per-pod SQLite
> file, so clusters will diverge between pods. This is a known, deliberate
> limitation of the exercise — §5 of SYSTEM_DESIGN.md explains it and the fix.
> For a coherent demo, set `replicas: 1` and delete the HPA, or point
> `DATABASE_URL` at a shared Postgres.

### Deploying multiple versions

Copy the manifest per version and change three things — namespace, image tag,
and Ingress path:

```bash
sed -e 's/identity-v1/identity-v1-1/g' \
    -e 's|identity-service:1.0.0|identity-service:1.1.0|' \
    -e 's|/v1(/|/v1.1(/|' \
    k8s-manifest.yaml > k8s-manifest-v1.1.yaml

kubectl apply -f k8s-manifest-v1.1.yaml
```

`/v1`, `/v1.1` and `/v2` then route to their own namespaces through the same
Ingress controller.

---

## CI/CD

`.github/workflows/deploy.yml` runs on pushes to `main` and on `v*.*.*` tags.

| Stage | What it does |
|---|---|
| `test` | Installs deps, runs pytest. Nothing is pushed if this fails. |
| `build-and-push` | Buildx multi-arch build, pushes to Docker Hub, Trivy scans for HIGH/CRITICAL CVEs. |

Pull requests build the image but never push.

### Required repository secrets

`Settings → Secrets and variables → Actions`:

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | A Docker Hub **access token** (not your password) |

### Cutting a release

```bash
git tag -a v1.1.0 -m "Add /products/search endpoint"
git push origin v1.1.0
```

That produces `:1.1.0`, `:1.1`, `:1` and `:latest` on Docker Hub. Image tags
are derived from the git ref, so they cannot drift out of sync with it.

---

## Tests

```bash
pip install pytest httpx
pytest -q
```

Coverage targets the four state transitions: new primary, exact repeat (no
writes), new secondary, and the two-cluster merge including re-parenting of
the absorbed cluster's secondaries.

---

## Logging and monitoring

Application logs go to stdout, which is what container runtimes expect:

```bash
kubectl logs -n identity-v1 -l app=identity-service -f
docker logs -f identity
```

Each rejected request logs its `reference` id alongside the real cause, so a
user-reported reference resolves to an exact log line.

Resource usage:

```bash
kubectl top pods -n identity-v1
kubectl describe hpa identity-service -n identity-v1
```

In production this would be shipped to a log aggregator (Loki, CloudWatch)
with Prometheus scraping `/metrics` — see §8 of SYSTEM_DESIGN.md.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| HPA shows `<unknown>/70%` | metrics-server missing or requests undefined | `minikube addons enable metrics-server`, wait ~60s |
| `ImagePullBackOff` | Placeholder username, or image not in Minikube's daemon | Replace `DOCKERHUB_USERNAME`, or `eval $(minikube docker-env)` and rebuild |
| Ingress 404 | Prefix not stripped | Confirm the `rewrite-target` annotation and that you're calling `/v1/identify` |
| `identity.local` unreachable | Missing hosts entry | Re-run the `/etc/hosts` step; `minikube ip` changes between restarts |
| Pod `CrashLoopBackOff`, permission denied on `/data` | Volume not writable by uid 10001 | `fsGroup: 10001` must be present in the pod `securityContext` |
