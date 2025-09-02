import platform
from asyncio import new_event_loop, sleep
from io import BytesIO
from os import environ, urandom
from time import time
from typing import Generator, TypeVar, ParamSpec, Callable
from urllib.parse import urlparse

from httpx import AsyncClient, Auth, Request, Response
from loguru import logger
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaPhoto


class BearerAuth(Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: Request) -> Generator[Request, Response, None]:
        request.headers["Authorization"] = f"bearer {self._token}"
        yield request


class RedditPost:
    __slots__ = ("id", "fullname", "subreddit", "title", "images")

    def __init__(self, id_: str, fullname: str, subreddit: str, title: str, images: list[str]) -> None:
        self.id = id_
        self.fullname = fullname
        self.subreddit = subreddit
        self.title = title
        self.images = images

    @property
    def url(self) -> str:
        return f"https://www.reddit.com/r/{self.subreddit}/comments/{self.id}/"

    def __repr__(self) -> str:
        slots = ", ".join(f"{slot}={getattr(self, slot)!r}" for slot in self.__slots__)
        return f"{self.__class__.__name__}({slots}, url={self.url!r})"


class RedditClient:
    _redirect_uri = "http://127.0.0.1:8080"

    def __init__(
            self, client_id: str, client_secret: str, dev_username: str, access_token: str | None = None,
            refresh_token: str | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._dev_username = dev_username
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._user_agent = f"{platform.system().lower()}/red2tg/0.1.0 (by /u/{self._dev_username})"
        self._expires_at: int = 0

    def make_auth_url(self) -> str:
        return (
            f"https://www.reddit.com/api/v1/authorize"
            f"?client_id={self._client_id}"
            f"&response_type=code"
            f"&state={urandom(16).hex()}"
            f"&redirect_uri={self._redirect_uri}"
            f"&duration=permanent"
            f"&scope=history"
        )

    def need_oauth(self) -> bool:
        return not self._refresh_token

    def _parse_token_response(self, base_time: int, data: dict) -> None:
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._expires_at = base_time + data["expires_in"]

    async def exchange_oauth_code(self, code: str) -> tuple[str, str]:
        async with AsyncClient() as cl:
            base_token_time = int(time()) - 1

            resp = await cl.post(
                "https://www.reddit.com/api/v1/access_token",
                headers={"User-Agent": self._user_agent},
                content=f"grant_type=authorization_code&code={code}&redirect_uri={self._redirect_uri}",
                auth=(self._client_id, self._client_secret),
            )

            logger.trace(f"Got reddit oauth code exchange response: status={resp.status_code}")
            if resp.status_code != 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = "<not json>"
                logger.debug(f"OAuth code exchange response body: {body}")

                raise RuntimeError(f"Failed to get access token: error code {resp.status_code}")

            creds_resp = resp.json()
            self._parse_token_response(base_token_time, creds_resp)

        return self._access_token, self._refresh_token

    async def _get_access_token(self) -> str:
        if self._access_token is None or time() > self._expires_at:
            async with AsyncClient() as cl:
                base_token_time = int(time()) - 1

                resp = await cl.post(
                    "https://www.reddit.com/api/v1/access_token",
                    headers={"User-Agent": self._user_agent},
                    content=f"grant_type=refresh_token&refresh_token={self._refresh_token}",
                    auth=(self._client_id, self._client_secret),
                )

                logger.trace(f"Got reddit token refresh response: status={resp.status_code}")
                if resp.status_code != 200:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = "<not json>"
                    logger.debug(f"OAuth token refreshe response body: {body}")

                    raise RuntimeError(f"Failed to get access token: error code {resp.status_code}")

                creds_resp = resp.json()
                self._parse_token_response(base_token_time, creds_resp)

        return self._access_token

    async def get_upvoted(self, username: str, last_known_id: str | None) -> list[RedditPost]:
        params = {"sort": "new", "t": "day", "limit": 100, "raw_json": "1"}
        if last_known_id is not None:
            params["before"] = last_known_id

        result = []

        async with AsyncClient() as cl:
            upvoted_resp = await cl.get(
                f"https://oauth.reddit.com/user/{username}/upvoted",
                auth=BearerAuth(await self._get_access_token()),
                headers={"User-Agent": self._user_agent},
                params=params,
            )
            logger.trace(f"Got reddit /upvoted response: status={upvoted_resp.status_code}")
            if upvoted_resp.status_code != 200:
                try:
                    body = upvoted_resp.json()
                except ValueError:
                    body = "<not json>"
                logger.debug(f"OAuth token refreshe response body: {body}")

                raise RuntimeError(f"Failed to get upvoted posts: error code {upvoted_resp.status_code}")

            upvoted_body = upvoted_resp.json()
            posts = upvoted_body["data"]["children"]

            logger.debug(f"Got {len(posts)} before processing")

            for post in reversed(posts):
                if post["kind"] != "t3":
                    continue
                post = post["data"]

                images = []
                if post.get("is_gallery"):
                    if "gallery_data" not in post \
                            or not isinstance(post["gallery_data"], dict) \
                            or "items" not in post["gallery_data"] \
                            or not isinstance(post["gallery_data"]["items"], list):
                        logger.info(f"Post {post['id']} has invalid \"gallery_data\" field: {post['gallery_data']}")
                        continue

                    if "media_metadata" not in post \
                            or not (isinstance(post["media_metadata"], dict)):
                        logger.info(f"Post {post['id']} has invalid \"media_metadata\" field: {post['media_metadata']}")
                        continue

                    for item in post["gallery_data"]["items"]:
                        if "media_id" not in item:
                            logger.info(f"Post {post['id']}: invalid item: {item}")
                            continue

                        metadata = post["media_metadata"][item["media_id"]]
                        if "s" not in metadata or not isinstance(metadata["s"], dict) \
                                or "u" not in metadata["s"] or not isinstance(metadata["s"]["u"], str) \
                                or not metadata["s"]["u"]:
                            logger.info(f"Post {post['id']}: invalid metadata: {metadata}")
                            continue

                        images.append(metadata["s"]["u"])
                elif post.get("preview"):
                    if not isinstance(post["preview"], dict) \
                            or "images" not in post["preview"] \
                            or not isinstance(post["preview"]["images"], list) \
                            or not post["preview"]["images"] \
                            or not isinstance(post["preview"]["images"][0], dict) \
                            or "source" not in post["preview"]["images"][0] \
                            or not isinstance(post["preview"]["images"][0]["source"], dict) \
                            or "url" not in post["preview"]["images"][0]["source"] \
                            or not isinstance(post["preview"]["images"][0]["source"]["url"], str) \
                            or not post["preview"]["images"][0]["source"]["url"]:
                        logger.info(f"Post {post['id']} has invalid \"preview\" field: {post['preview']}")
                        continue

                    images.append(post["preview"]["images"][0]["source"]["url"])

                if not images or len(images) > 10:
                    logger.info(f"Post {post['id']} does not have any images or has more than 10 images")
                    continue

                result.append(RedditPost(
                    id_=post["id"],
                    fullname=post["name"],
                    subreddit=post["subreddit"],
                    title=post["title"],
                    images=images,
                ))

        logger.debug(f"Got {len(result)} after processing")

        return result


T = TypeVar("T")
P = ParamSpec("P")


async def _flood_wait(func: Callable[P, T], *args, **kwargs) -> T | None:
    attempts = 5
    for i in range(attempts):
        try:
            return await func(*args, **kwargs)
        except FloodWait as e:
            logger.warning(f"Got FloodWait of {e.value} seconds, waiting...")
            if i == (attempts - 1):
                raise
            await sleep(e.value + 1)


async def main() -> None:
    reddit_username = environ["REDDIT_USERNAME"]
    reddit = RedditClient(
        client_id=environ["REDDIT_API_ID"],
        client_secret=environ["REDDIT_API_SECRET"],
        dev_username=environ["REDDIT_API_USERNAME"],
        access_token=environ.get("REDDIT_API_ACCESS_TOKEN"),
        refresh_token=environ.get("REDDIT_API_REFRESH_TOKEN"),
    )

    channel_id = int(environ["CHANNEL_ID"])
    log_chat_id = int(environ["LOG_CHAT_ID"])
    bot = Client(
        name="reddit2telegram",
        api_id=int(environ["TG_API_ID"]),
        api_hash=environ["TG_API_HASH"],
        bot_token=environ["BOT_TOKEN"],
        no_updates=True,
        parse_mode=ParseMode.MARKDOWN,
    )

    if reddit.need_oauth():
        print(reddit.make_auth_url())
        code = input("Code: ").strip()
        access_token, refresh_token = await reddit.exchange_oauth_code(code)
        print(f"Access token: {access_token}")
        print(f"Refresh token: {refresh_token}")

    async with bot:
        reddit_last_known_id = environ.get("REDDIT_LAST_KNOWN_ID")

        while True:
            try:
                posts = await reddit.get_upvoted(reddit_username, reddit_last_known_id)
            except Exception as e:
                logger.opt(exception=e).error("Failed to get upvoted posts")
                await bot.send_message(log_chat_id, "Failed to get upvoted posts, check logs for exact error")
                await sleep(60 * 5)
                continue

            for post in posts:
                logger.debug(f"Post: {post!r}")

                if not post.images or len(post.images) > 10:
                    logger.info(f"Skipping post {post.id} ({post.title!r}): {len(post.images)=}")
                    continue

                logger.info(f"Sending post {post.id} ({post.title!r})")

                image_files: list[BytesIO] = []
                for idx, image in enumerate(post.images):
                    photo = BytesIO()
                    async with AsyncClient() as cl:
                        async with cl.stream("GET", image) as resp:
                            async for chunk in resp.aiter_bytes(1024 * 64):
                                photo.write(chunk)

                    name = urlparse(image).path.split("/")[-1]
                    if not name:
                        name = f"{post.fullname}_{idx}.jpg"
                    setattr(photo, "name", name)
                    image_files.append(photo)

                caption = f"{post.title}\n\n[Post link]({post.url})"

                if len(post.images) == 1:
                    await _flood_wait(
                        bot.send_photo,
                        chat_id=channel_id,
                        photo=image_files[0],
                        caption=caption,
                    )
                elif len(post.images) > 1:
                    media = [
                        InputMediaPhoto(file, caption=caption if idx == 0 else "")
                        for idx, file in enumerate(image_files)
                    ]

                    await _flood_wait(
                        bot.send_media_group,
                        chat_id=channel_id,
                        media=media,
                    )

            if posts:
                reddit_last_known_id = posts[-1].fullname
                logger.info(f"Sent ~{len(posts)} posts, last seen id is {reddit_last_known_id!r}")
            else:
                logger.info("No new upvoted posts available")

            await sleep(60)


if __name__ == "__main__":
    new_event_loop().run_until_complete(main())
