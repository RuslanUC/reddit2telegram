import platform
from asyncio import new_event_loop
from io import BytesIO
from os import environ, urandom
from time import time
from typing import Generator
from urllib.parse import urlparse

from httpx import AsyncClient, Auth, Request, Response
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto


class BearerAuth(Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: Request) -> Generator[Request, Response, None]:
        request.headers["Authorization"] = f"bearer {self._token}"
        yield request


class RedditPost:
    def __init__(self, id_: str, fullname: str, subreddit: str, title: str, images: list[str]) -> None:
        self.id = id_
        self.fullname = fullname
        self.subreddit = subreddit
        self.title = title
        self.images = images

    @property
    def url(self) -> str:
        return f"https://www.reddit.com/r/{self.subreddit}/comments/{self.id}/"


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
        state = urandom(16).hex()
        return (
            f"https://www.reddit.com/api/v1/authorize"
            f"?client_id={self._client_id}"
            f"&response_type=code"
            f"&state={state}"
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

            if resp.status_code != 200:
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

                if resp.status_code != 200:
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
            if upvoted_resp.status_code != 200:
                raise RuntimeError(f"Failed to get upvoted posts: error code {upvoted_resp.status_code}, body: {upvoted_resp.json()}")

            upvoted_body = upvoted_resp.json()
            posts = upvoted_body["data"]["children"]

            for post in reversed(posts):
                if post["kind"] != "t3":
                    continue
                post = post["data"]

                images = []
                if post.get("is_gallery"):
                    if "gallery_data" not in post \
                            or not isinstance(post["gallery_data"], dict) \
                            or "items" not in post["gallery_data"] \
                            or not isinstance(post["gallery_data"]["items"], list) \
                            or "media_metadata" not in post \
                            or not (isinstance(post["media_metadata"], dict)):
                        continue

                    for item in post["gallery_data"]["items"]:
                        if "media_id" not in item:
                            continue

                        metadata = post["media_metadata"][item["media_id"]]
                        if "s" not in metadata or not isinstance(metadata["s"], dict) \
                                or "u" not in metadata["s"] or not isinstance(metadata["s"]["u"], str) \
                                or not metadata["s"]["u"]:
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
                        continue

                    images.append(post["preview"]["images"][0]["source"]["url"])

                if not images or len(images) > 10:
                    continue

                result.append(RedditPost(
                    id_=post["id"],
                    fullname=post["name"],
                    subreddit=post["subreddit"],
                    title=post["title"],
                    images=images,
                ))

        return result

async def main() -> None:
    reddit_username = environ["REDDIT_USERNAME"]
    reddit_last_known_id = None
    reddit = RedditClient(
        client_id=environ["REDDIT_API_ID"],
        client_secret=environ["REDDIT_API_SECRET"],
        dev_username=environ["REDDIT_API_USERNAME"],
        access_token=environ.get("REDDIT_API_ACCESS_TOKEN"),
        refresh_token=environ.get("REDDIT_API_REFRESH_TOKEN"),
    )

    channel_id = int(environ["CHANNEL_ID"])
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
        posts = await reddit.get_upvoted(reddit_username, reddit_last_known_id)
        for post in posts[-2:]:
            print(f"Post {post.id}:")
            print(f"  id={post.id!r}")
            print(f"  fullname={post.fullname!r}")
            print(f"  subreddit={post.subreddit!r}")
            print(f"  title={post.title!r}")
            print(f"  url={post.url!r}")
            print(f"  images={post.images!r}")

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
                await bot.send_photo(
                    chat_id=channel_id,
                    photo=image_files[0],
                    caption=caption,
                )
            elif len(post.images) > 1:
                media = [
                    InputMediaPhoto(file, caption=caption if idx == 0 else "")
                    for idx, file in enumerate(image_files)
                ]

                await bot.send_media_group(
                    chat_id=channel_id,
                    media=media,
                )




if __name__ == "__main__":
    new_event_loop().run_until_complete(main())
