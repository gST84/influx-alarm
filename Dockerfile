FROM python:3.12-slim

# sox converts Piper's speech to the 8 kHz format Asterisk plays.
RUN apt-get update && apt-get install -y --no-install-recommends sox curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir piper-tts==1.8.0 tzdata

# Belgian Dutch voices for the phone messages.
ARG VOICES="nathalie/medium/nl_BE-nathalie-medium rdh/medium/nl_BE-rdh-medium"
RUN mkdir -p /opt/voices && cd /opt/voices && for v in $VOICES; do \
      n=$(basename "$v"); \
      curl -fsSL -o "$n.onnx" "https://huggingface.co/rhasspy/piper-voices/resolve/main/nl/nl_BE/$v.onnx" && \
      curl -fsSL -o "$n.onnx.json" "https://huggingface.co/rhasspy/piper-voices/resolve/main/nl/nl_BE/$v.onnx.json"; \
    done

WORKDIR /app
COPY alarm ./alarm
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/alarm.db SOUNDS_DIR=/data/sounds VOICES_DIR=/opt/voices PORT=8080
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://localhost:8080/api/me >/dev/null || exit 1
CMD ["python", "-m", "alarm"]
