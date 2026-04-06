FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
COPY vk_bot/ vk_bot/
COPY core/ core/
COPY web/ web/
COPY start_all.py .
COPY telegram-bot/ telegram-bot/

RUN mkdir -p data/service_accounts telegram-bot/data

EXPOSE 8080

CMD ["python", "start_all.py"]
