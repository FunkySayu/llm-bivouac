FROM python:3.14.10-slim-trixie AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.14.10-slim-trixie
# Compose mounts the proxy-state volume here; a new volume inherits this owner.
RUN install -d -o 990 -g 990 -m 0700 /state
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 990:990
EXPOSE 8000 9100
CMD ["llm-bivouac-proxy"]
