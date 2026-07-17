"""Celery application and RabbitMQ topology for normalization delivery."""

from celery import Celery
from kombu import Exchange, Queue

from app.config import get_settings

settings = get_settings()

normalization_exchange = Exchange(
    settings.NORMALIZATION_EXCHANGE,
    type="direct",
    durable=True,
)
normalization_queue = Queue(
    settings.NORMALIZATION_QUEUE,
    exchange=normalization_exchange,
    routing_key="normalize",
    durable=True,
    queue_arguments={
        "x-queue-type": "classic",
        "x-max-length": 100_000,
        "x-overflow": "reject-publish",
        "x-consumer-timeout": settings.NORMALIZATION_CONSUMER_TIMEOUT_MS,
        "x-dead-letter-exchange": settings.NORMALIZATION_EXCHANGE,
        "x-dead-letter-routing-key": "dead",
    },
)
dead_letter_queue = Queue(
    settings.NORMALIZATION_DLQ,
    exchange=normalization_exchange,
    routing_key="dead",
    durable=True,
    queue_arguments={"x-queue-type": "classic"},
)

celery_app = Celery(
    "findb",
    broker=settings.CELERY_BROKER_URL,
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_default_queue=settings.NORMALIZATION_QUEUE,
    task_default_exchange=settings.NORMALIZATION_EXCHANGE,
    task_default_exchange_type="direct",
    task_default_routing_key="normalize",
    task_default_delivery_mode="persistent",
    task_queues=(normalization_queue, dead_letter_queue),
    task_routes={
        "app.workers.tasks.normalize_run": {
            "queue": settings.NORMALIZATION_QUEUE,
            "routing_key": "normalize",
        }
    },
    broker_transport_options={"confirm_publish": True},
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_ignore_result=True,
    task_soft_time_limit=settings.NORMALIZATION_TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.NORMALIZATION_TASK_TIME_LIMIT,
    imports=("app.workers.heartbeat",),
)


def declare_topology() -> None:
    """Idempotently declare the complete version-controlled broker topology."""
    with celery_app.connection_for_write() as connection:
        connection.ensure_connection(max_retries=3)
        channel = connection.channel()
        try:
            normalization_exchange.declare(channel=channel)
            normalization_queue.declare(channel=channel)
            dead_letter_queue.declare(channel=channel)
        finally:
            channel.close()
