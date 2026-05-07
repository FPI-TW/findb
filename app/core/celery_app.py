import os

from celery import Celery

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

celery_app = Celery("findb_worker", broker=RABBITMQ_URL, include=["app.workers.tasks"])

# 移除 result_backend 配置
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
)
