from asyncio import sleep
from typing import Generator, TypeVar, ParamSpec, Callable

from httpx import Auth, Request, Response
from loguru import logger
from pyrogram.errors import FloodWait


class BearerAuth(Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: Request) -> Generator[Request, Response, None]:
        request.headers["Authorization"] = f"bearer {self._token}"
        yield request


T = TypeVar("T")
P = ParamSpec("P")


async def flood_wait(func: Callable[P, T], *args, **kwargs) -> T | None:
    attempts = 5
    for i in range(attempts):
        try:
            return await func(*args, **kwargs)
        except FloodWait as e:
            logger.warning(f"Got FloodWait of {e.value} seconds, waiting...")
            if i == (attempts - 1):
                raise
            await sleep(e.value + 1)
