FROM alpine:3.23

ARG KIND_VERSION=v0.31.0
ARG KUBECTL_VERSION=v1.35.0

RUN apk add --no-cache curl bash

RUN ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/') \
    && curl -sLo /usr/local/bin/kind \
        "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}" \
    && chmod +x /usr/local/bin/kind \
    && curl -sLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

RUN apk add --no-cache docker-cli

COPY kind-cluster.yaml /kind-cluster.yaml
COPY setup.sh /setup.sh
RUN chmod +x /setup.sh

ENTRYPOINT ["/setup.sh"]
