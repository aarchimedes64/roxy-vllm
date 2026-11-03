FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Container always listens internally on 7777; host port mapping is
# handled by start.sh.
EXPOSE 7777

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 7777"]
