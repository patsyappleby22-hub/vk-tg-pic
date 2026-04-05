FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
COPY vk_bot/ vk_bot/
COPY core/ core/
COPY start_all.py .

RUN mkdir -p data/service_accounts

CMD ["python", "start_all.py"]
