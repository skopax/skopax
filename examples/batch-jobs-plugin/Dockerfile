ARG BASE_IMAGE=ghcr.io/skopax/skopax
ARG SKOPAX_VERSION=0.4.5

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY skopax_batch_jobs/ ./skopax_batch_jobs/
RUN pip install --no-cache-dir --root-user-action=ignore --no-deps --target /plugins .

FROM ${BASE_IMAGE}:${SKOPAX_VERSION}

COPY --from=build /plugins /usr/local/lib/python3.15/site-packages/
