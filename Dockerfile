FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static static
USER 1000:1000
ENV DATA_DIR=/tmp/data PORT=8080
EXPOSE 8080
CMD ["python", "-I", "app.py"]
