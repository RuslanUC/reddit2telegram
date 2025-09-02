import platform
from os import urandom
from time import time

from httpx import AsyncClient
from loguru import logger

from state import State
from utils import BearerAuth


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

    def save_tokens(self, state: State) -> None:
        state.access_token = self._access_token
        state.refresh_token = self._refresh_token

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