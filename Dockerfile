FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.9,<2" paho-mqtt pyyaml
COPY app/ /app/
VOLUME /data
EXPOSE 8271
ENV BAMBU_MCP_CACHE=/data/metacache.json
CMD ["python", "server.py"]
