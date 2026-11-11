"""Kafka, and the only place this codebase admits Kafka exists.

`services/publisher.py` defines a `Publisher` as something that takes a batch
of outbox rows and reports on each one. This package is the implementation
that does it with Kafka, and nothing outside it imports `confluent_kafka`.

The point of that boundary is the dispatcher: it holds a `Publisher`, reads
back `TransientDeliveryError` or `PermanentDeliveryError`, and has no table of
Kafka error codes, no notion of a broker, and nothing to change on the day a
second transport is added.
"""

from smartcourse.infra.kafka.publisher import KafkaPublisher

__all__ = ["KafkaPublisher"]
