FROM mongo:7.0
# iproute2 provides `tc` (network latency injection); iputils-ping is used to verify the injected RTT.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iputils-ping \
    && rm -rf /var/lib/apt/lists/*
