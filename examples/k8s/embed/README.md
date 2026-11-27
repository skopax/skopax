# Embedding service for Search by meaning

These manifests run the embedding service that Skopax's Search by meaning needs: Hugging Face text-embeddings-inference (Apache-2.0) serving the model `intfloat/multilingual-e5-small` (MIT) from a volume. Nothing is downloaded when the service starts, and the service needs no network at all once the model is on the volume. See [Search by meaning](../../../docs/operations.md#search-by-meaning) for what the feature does, its sizing and its privacy limits.

## Files

| File | What it is |
|---|---|
| `secret.yaml` | The API key, as a placeholder. Replace the value before you apply it. |
| `pvc.yaml` | A 1 GiB volume for the model (the model is about 465 MB). |
| `model-fetch-job.yaml` | Downloads the 8 model files from a pinned revision, checks the sha256 of each and fails on any mismatch. |
| `deployment.yaml` | The service: 1 CPU, 1.25 GiB requested and 1.5 GiB limit, non-root, read-only root file system. |
| `service.yaml` | Port 80, forwarded to the container's 8080. |
| `networkpolicy.yaml` | Ingress only from the Skopax pods; no egress at all. |
| `networkpolicy-metrics.yaml` | Optional: lets a Prometheus read `/metrics`. |

Apply everything into the namespace of Skopax. The policies select the Skopax pods by the label `app.kubernetes.io/name: skopax`; change that label in `networkpolicy.yaml` if your Skopax pods carry another.

## Apply order

1. Make the key and apply the Secret. Either edit `secret.yaml` and replace the `replace-me` placeholder with a key made by a command such as `openssl rand -hex 32`, or create it from the command line instead of applying the file:

        kubectl create secret generic skopax-embed --from-literal=api-key="$(openssl rand -hex 32)"

2. Apply the volume and the network policy: `kubectl apply -f pvc.yaml -f networkpolicy.yaml`.
3. Run the fetch Job and wait for it: `kubectl apply -f model-fetch-job.yaml`, then `kubectl wait --for=condition=complete job/skopax-embed-model-fetch --timeout=30m`. The Job needs to reach `huggingface.co` over https, and its pod is not covered by the embed policy. If your cluster denies egress by default, allow that Job's pod for the time it runs. It prints `ok <file>` for each file, and it stops with `sha256 mismatch` if a file is not the pinned one.
4. Apply the service and the Deployment: `kubectl apply -f service.yaml -f deployment.yaml`. The volume can be mounted by one node at a time, so the Deployment waits for the Job to finish first.
5. Point Skopax at it: `SKOPAX_EMBED_URL=http://skopax-embed` (the Service listens on port 80) and `SKOPAX_EMBED_API_KEY` from the same Secret. Then turn on Search by meaning in Admin > Features.

The Job image `docker.io/library/alpine:3.21` is pinned by digest and is only a download tool; mirror it in your own registry if your policy asks for that.

## Sizing

- One service pod: CPU limit 1, memory request 1.25 GiB and limit 1.5 GiB. The service is killed for lack of memory at 1 GiB while it starts, so do not lower the limit.
- On one cloud vCPU expect about 4 to 8 passages a second at 200 tokens and 2 to 3.5 at 400 tokens; a search question takes about 15 to 30 ms. The first fill of a large install takes hours; see the sizing notes in the operations page.
- One replica is enough. More replicas only help the first fill, and the volume then has to be shared read-only.

## Architectures

The Deployment pins the amd64 image:

    ghcr.io/huggingface/text-embeddings-inference:cpu-1.10.0@sha256:f0a4bb206b9d4877746b5b9f638e138fe683eeb9787c14d541a12c993b562d86

There is no multi-architecture tag. On arm64 nodes use this one instead:

    ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.10.0@sha256:446c683eef5bbbdf1eb9899966b8e198921f671e92ee3fe3d7a5f6d770ea0a26

Do not use the 1.9 releases: they write the API key to the log.

## Why the network policy is required

The service's `--api-key` (the `API_KEY` variable here) only protects the embedding routes. `/health`, `/metrics`, `/docs`, `/api-doc/openapi.json` and `/` answer without a key. The policy keeps those closed to everything except the Skopax pods, and it also lets the embed pod open no connection of its own. A cluster whose network plug-in does not enforce NetworkPolicy gives no such protection: use one that does, or keep the service on a network that only Skopax reaches.

## The model must match Skopax

The service names itself `intfloat/multilingual-e5-small` (`--served-model-name`), and Skopax refuses an answer from a service that names another model or returns vectors of another length. If you serve another model, set `SKOPAX_EMBED_MODEL` to the same name, adjust the prefixes if the model needs other ones, and expect the whole index to be rebuilt. The files in the Job and the sha256 values belong to the pinned revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`; for another model, change both.
