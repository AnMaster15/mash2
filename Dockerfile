FROM python:3.9-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5100
CMD ["flask", "run", "--host=0.0.0.0", "--port=5100"]
