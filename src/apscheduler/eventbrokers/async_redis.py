from __future__ import annotations

import anyio
import attrs
import tenacity
from redis import ConnectionError
from redis.asyncio import Redis, RedisCluster
from redis.asyncio.client import PubSub
from redis.asyncio.connection import ConnectionPool

from .. import RetrySettings
from .._events import Event
from ..abc import Serializer
from ..serializers.json import JSONSerializer
from .async_local import LocalAsyncEventBroker
from .base import DistributedEventBrokerMixin


@attrs.define(eq=False)
class AsyncRedisEventBroker(LocalAsyncEventBroker, DistributedEventBrokerMixin):
    """
    An event broker that uses a Redis server to broadcast events.

    Requires the redis_ library to be installed.

    .. _redis: https://pypi.org/project/redis/

    :param client: an asynchronous Redis client
    :param serializer: the serializer used to (de)serialize events for transport
    :param channel: channel on which to send the messages
    :param retry_settings: Tenacity settings for retrying operations in case of a
        broker connectivity problem
    :param stop_check_interval: interval on which the channel listener should check if
        it
        values mean slower reaction time but less CPU use)
    """

    client: Redis | RedisCluster
    serializer: Serializer = attrs.field(factory=JSONSerializer)
    channel: str = attrs.field(kw_only=True, default="apscheduler")
    retry_settings: RetrySettings = attrs.field(default=RetrySettings())
    stop_check_interval: float = attrs.field(kw_only=True, default=1)
    _stopped: bool = attrs.field(init=False, default=True)

    @classmethod
    def from_url(cls, url: str, **kwargs) -> AsyncRedisEventBroker:
        """
        Create a new event broker from a URL.

        :param url: a Redis URL (```redis://...```)
        :param kwargs: keyword arguments to pass to the initializer of this class
        :return: the newly created event broker

        """
        pool = ConnectionPool.from_url(url)
        client = Redis(connection_pool=pool)
        return cls(client, **kwargs)

    def _retry(self) -> tenacity.AsyncRetrying:
        def after_attempt(retry_state: tenacity.RetryCallState) -> None:
            self._logger.warning(
                f"{self.__class__.__name__}: connection failure "
                f"(attempt {retry_state.attempt_number}): "
                f"{retry_state.outcome.exception()}",
            )

        return tenacity.AsyncRetrying(
            stop=self.retry_settings.stop,
            wait=self.retry_settings.wait,
            retry=tenacity.retry_if_exception_type(ConnectionError),
            after=after_attempt,
            sleep=anyio.sleep,
            reraise=True,
        )

    async def start(self) -> None:
        await super().start()
        pubsub = self.client.pubsub()
        try:
            await pubsub.subscribe(self.channel)
        except Exception:
            await self.stop(force=True)
            raise

        self._stopped = False
        self._task_group.start_soon(
            self._listen_messages, pubsub, name="Redis subscriber"
        )

    async def stop(self, *, force: bool = False) -> None:
        self._stopped = True
        await super().stop(force=force)

    async def _listen_messages(self, pubsub: PubSub) -> None:
        while not self._stopped:
            try:
                async for attempt in self._retry():
                    with attempt:
                        msg = await pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=self.stop_check_interval,
                        )

                if msg and isinstance(msg["data"], bytes):
                    event = self.reconstitute_event(msg["data"])
                    if event is not None:
                        await self.publish_local(event)
            except Exception:
                self._logger.exception(f"{self.__class__.__name__} listener crashed")
                await pubsub.close()
                raise

    async def publish(self, event: Event) -> None:
        notification = self.generate_notification(event)
        async for attempt in self._retry():
            with attempt:
                await self.client.publish(self.channel, notification)
