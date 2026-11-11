FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ./
# The `ea` package is bind-mounted at /app/exec-assist rather than copied, so
# edits to the shared engine take effect on a container restart instead of a
# rebuild. See docker-compose.yml.
CMD ["python", "server.py"]
