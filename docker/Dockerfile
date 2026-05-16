ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG ANSIBLE_CORE_VERSION=2.19.3
ARG HASHIVAULT_MODULE_VERSION=5.4.0
ARG HELM_VERSION=v4.2.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/') && \
    curl -sLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/v1.35.0/bin/linux/${ARCH}/kubectl" && \
    chmod +x /usr/local/bin/kubectl

RUN ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/') && \
    curl -sLo /tmp/helm.tar.gz \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" && \
    tar xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${ARCH} && \
    chmod +x /usr/local/bin/helm

RUN pip install --no-cache-dir \
    "ansible-core==${ANSIBLE_CORE_VERSION}" \
    "ansible-modules-hashivault==${HASHIVAULT_MODULE_VERSION}" \
    hvac \
    pytest \
    pytest-mock \
    pytest-cov

RUN ansible-galaxy collection install kubernetes.core

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["pytest", "tests/", "-v"]
