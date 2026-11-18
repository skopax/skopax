FROM gcr.io/distroless/cc-debian13:latest@sha256:809d04afa1be033c441f76bb0051878c342fb76d0ea236bc6dc94b655fa0fbd6 AS base

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build

SHELL ["/bin/bash", "-eu", "-o", "pipefail", "-c"]
COPY --from=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 /uv /bin/uv
COPY --from=base /var/lib/dpkg/status.d/ /base-status/
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --inexact \
 && python3 -m pip uninstall -y --root-user-action=ignore pip \
 && rm /usr/local/lib/python3.15/lib-dynload/readline.cpython-*.so \
       /usr/local/lib/python3.15/lib-dynload/_gdbm.cpython-*.so \
       /usr/local/lib/python3.15/lib-dynload/_dbm.cpython-*.so \
 && mkdir -p /deps/var/lib/dpkg/status.d /build/sums /notices \
 && find /usr/local/lib/python3.15/site-packages -path '*.libs/*' -type f | sort > /build/bundled \
 && { find /usr/local -name '*.so*' -type f -exec ldd {} + 2>/dev/null || true; } \
    | awk '$2 == "=>" && $3 ~ /^\/(usr\/)?lib\// {print $3}' | sed 's#^/lib/#/usr/lib/#' | sort -u > /build/linked \
 && held='/(ld-linux[^/]*|libc|libm|libpthread|libdl|librt|libutil|libresolv|libgcc_s|libstdc\+\+|libssl|libcrypto|libz)\.so' \
 && { grep -E "$held" /build/linked || true; } > /build/held \
 && { grep -Ev "$held" /build/linked || true; } > /build/libs \
 && while read -r lib; do \
      pkg="$(dpkg -S "$lib" | head -n 1 | cut -d: -f1)" || exit 1; \
      [[ "$pkg" =~ ^[a-z0-9][a-z0-9.+-]+$ ]] || exit 1; \
      echo "$lib $pkg" >> /build/libs.txt || exit 1; \
      have="$(dpkg-query -W -f='${Version}' "$pkg")" || exit 1; \
      shipped="$(test ! -e "/base-status/$pkg" || sed -n 's/^Version: //p' "/base-status/$pkg")" || exit 1; \
      [ ! -e "/base-status/$pkg" ] || [ "$shipped" = "$have" ] || exit 1; \
      real="$(readlink -f "$lib")" || exit 1; \
      [ ! -e "/base-status/$pkg" ] || awk -v real="${real#/}" '$2 == real {found = 1} END {exit !found}' "/base-status/$pkg.md5sums" || exit 1; \
      [ ! -e "/base-status/$pkg" ] || continue; \
      cp --parents -L "$lib" /deps || exit 1; \
      mkdir -p "/deps/usr/share/doc/$pkg" || exit 1; \
      cp "/usr/share/doc/$pkg/copyright" "/deps/usr/share/doc/$pkg/" || exit 1; \
      owner="$(dpkg-query -W -f='${binary:Package}' "$pkg")" || exit 1; \
      dpkg-query -s "$pkg" | grep -E '^(Package|Status|Priority|Section|Installed-Size|Maintainer|Architecture|Multi-Arch|Source|Version|Depends|Description|Homepage):' > "/deps/var/lib/dpkg/status.d/$pkg" || exit 1; \
      awk -v real="${real#/}" -v lib="${lib#/}" -v doc="usr/share/doc/$pkg/copyright" '$2 == real {print $1 "  " lib} $2 == real {found = 1} $2 == doc {print} END {exit !found}' "/var/lib/dpkg/info/$owner.md5sums" >> "/build/sums/$pkg" || exit 1; \
      sort -u -k 2 "/build/sums/$pkg" > "/deps/var/lib/dpkg/status.d/$pkg.md5sums" || exit 1; \
    done < /build/libs \
 && test -s /build/libs.txt \
 && while read -r held_lib; do \
      pkg="$(dpkg -S "$held_lib" | head -n 1 | cut -d: -f1)" || exit 1; \
      [[ "$pkg" =~ ^[a-z0-9][a-z0-9.+-]+$ ]] || exit 1; \
      test -e "/base-status/$pkg" || exit 1; \
      real="$(readlink -f "$held_lib")" || exit 1; \
      awk -v real="${real#/}" '$2 == real {found = 1} END {exit !found}' "/base-status/$pkg.md5sums" || exit 1; \
    done < /build/held \
 && test -s /build/held
COPY tools/ ./tools/
RUN python3 -m tools.licences check --site /usr/local/lib/python3.15/site-packages --out /notices \
 && python3 -m tools.licences debian --libs /build/libs.txt --out /notices \
 && python3 -m tools.licences bundled --site /usr/local/lib/python3.15/site-packages --list /build/bundled --out /notices

FROM base

ARG SKOPAX_VERSION=0.1.0
ARG COMMIT_ID=dev
LABEL org.opencontainers.image.title="Skopax" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/skopax/skopax" \
      org.opencontainers.image.url="https://skopax.io" \
      org.opencontainers.image.version="${SKOPAX_VERSION}" \
      org.opencontainers.image.revision="${COMMIT_ID}"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COMMIT_ID=${COMMIT_ID} \
    PATH=/usr/local/bin:/usr/bin:/bin \
    LD_LIBRARY_PATH=/usr/local/lib \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    HOME=/app

COPY --from=build /usr/local /usr/local
COPY --from=build /deps/ /
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/doc/skopax/
COPY --from=build /notices/ /usr/share/doc/skopax/

WORKDIR /app
COPY skopax/ ./skopax/

USER 1001

EXPOSE 8080

HEALTHCHECK --start-period=20s CMD ["/usr/local/bin/python3", "-c", "import http.client as h; c = h.HTTPConnection('127.0.0.1', 8080, timeout=4); c.request('GET', '/actuator/health/liveness'); raise SystemExit(c.getresponse().status >= 500)"]

CMD ["/usr/local/bin/python3", "-m", "uvicorn", "skopax.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--no-server-header"]
