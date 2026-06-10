FROM debian:bookworm-slim AS whisper-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --depth 1 --branch v1.8.4 \
    https://github.com/ggml-org/whisper.cpp.git

WORKDIR /build/whisper.cpp
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release
RUN cmake --build build --config Release -j$(nproc)

RUN bash models/download-ggml-model.sh base.en

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    rtl-sdr \
    librtlsdr-dev \
    libusb-1.0-0 \
    sox \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=whisper-builder /build/whisper.cpp/build/bin/whisper-cli /usr/local/bin/
COPY --from=whisper-builder /build/whisper.cpp/models/ggml-base.en.bin /models/

WORKDIR /app

RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner.py api_server.py channels.json ./
COPY static/ ./static/

ENV WHISPER_MODEL=base.en
ENV WHISPER_MODEL_PATH=/models/ggml-base.en.bin

EXPOSE 8000

CMD ["python3", "api_server.py"]
