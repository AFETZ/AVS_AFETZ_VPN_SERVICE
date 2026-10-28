FROM golang:1.26.8-bookworm AS build
ARG OPENFLUX_COMMIT=a8a8937c59fbb275603bd53e150ecaf46e92148e
WORKDIR /src
RUN git init . && git remote add origin https://github.com/p1neappleXpress/OpenFlux.git \
    && git fetch --depth=1 origin "$OPENFLUX_COMMIT" && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$OPENFLUX_COMMIT"
ENV CGO_ENABLED=0 GOMAXPROCS=2
COPY patches/client-and-retry.patch /tmp/afzvpn.patch
RUN git apply --check /tmp/afzvpn.patch && git apply /tmp/afzvpn.patch
RUN go build -p 1 -trimpath -o /out/openflux .

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates iptables \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/openflux /usr/local/bin/openflux
COPY --from=build /src/LICENSE /src/NOTICE /src/COPYRIGHT /usr/share/doc/openflux/
COPY entrypoint.sh /usr/local/bin/openflux-entrypoint
RUN chmod 755 /usr/local/bin/openflux-entrypoint
ENTRYPOINT ["/usr/local/bin/openflux-entrypoint"]
