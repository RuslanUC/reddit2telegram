from asyncio import new_event_loop, sleep
from io import BytesIO
from os import environ
from urllib.parse import urlparse

from httpx import AsyncClient
from loguru import logger
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto

from reddit_api import RedditClient
from state import State
from utils import flood_wait


async def main() -> None:
    state_file = environ.get("STATE_FILE", "reddit2telegram.state")

    try:
        state = State.load(state_file)
        logger.info("Loaded state from file")
    except Exception as e:
        logger.opt(exception=e).warning("Failed to load state")
        state = State(
            environ.get("REDDIT_LAST_KNOWN_ID"),
            environ.get("REDDIT_API_ACCESS_TOKEN"),
            environ.get("REDDIT_API_REFRESH_TOKEN"),
        )

    reddit_username = environ["REDDIT_USERNAME"]
    reddit = RedditClient(
        client_id=environ["REDDIT_API_ID"],
        client_secret=environ["REDDIT_API_SECRET"],
        dev_username=environ["REDDIT_API_USERNAME"],
        access_token=state.access_token,
        refresh_token=state.refresh_token,
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
        reddit.save_tokens(state)
        state.dump(state_file)
        print(f"Access token: {access_token}")
        print(f"Refresh token: {refresh_token}")

    async with bot:
        while True:
            try:
                posts = await reddit.get_upvoted(reddit_username, state.reddit_last_seen_id)
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
                    await flood_wait(
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

                    await flood_wait(
                        bot.send_media_group,
                        chat_id=channel_id,
                        media=media,
                    )

            if posts:
                state.reddit_last_seen_id = posts[-1].fullname
                logger.info(f"Sent ~{len(posts)} posts, last seen id is {state.reddit_last_seen_id!r}")
            else:
                logger.info("No new upvoted posts available")

            reddit.save_tokens(state)
            state.dump(state_file)

            await sleep(60)


if __name__ == "__main__":
    new_event_loop().run_until_complete(main())
