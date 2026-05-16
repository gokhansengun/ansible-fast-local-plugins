ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG ANSIBLE_CORE_VERSION=2.19.3
ARG HASHIVAULT_MODULE_VERSION=5.4.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/') && \
    curl -sLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/v1.35.0/bin/linux/${ARCH}/kubectl" && \
    chmod +x /usr/local/bin/kubectl

RUN pip install --no-cache-dir \
    "ansible-core==${ANSIBLE_CORE_VERSION}" \
    "ansible-modules-hashivault==${HASHIVAULT_MODULE_VERSION}" \
    hvac \
    pytest \
    pytest-mock \
    pytest-cov

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["pytest", "tests/", "-v"]
