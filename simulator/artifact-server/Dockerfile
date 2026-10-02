FROM python:3.12-slim
WORKDIR /app
COPY artifact-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ota.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. ota.proto
COPY artifact-server/server.py .
CMD ["python", "server.py"]
