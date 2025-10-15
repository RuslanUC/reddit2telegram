from asyncio import new_event_loop, sleep
from io import BytesIO
from os import environ
from urllib.parse import urlparse

from httpx import AsyncClient
from loguru import logger
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto, InputMedia, InputMediaVideo

from reddit_api import RedditClient, RedditPostMediaVideo, RedditPostMediaImage, RedditPostMedia, RedditPost
from state import State
from utils import flood_wait

input_media_item_cls: dict[type[RedditPostMedia], type[InputMedia]] = {
    RedditPostMediaImage: InputMediaPhoto,
    RedditPostMediaVideo: InputMediaVideo,
}


async def _send_one_post(bot: Client, channel_id: int, post: RedditPost, media_files: list[BytesIO]) -> None:
    caption = f"{post.title}\n\n[Post link]({post.url})"

    if len(post.media) == 1:
        media = post.media[0]
        if isinstance(media, RedditPostMediaImage):
            await flood_wait(
                bot.send_photo,
                chat_id=channel_id,
                photo=media_files[0],
                caption=caption,
            )
        elif isinstance(media, RedditPostMediaVideo):
            await flood_wait(
                bot.send_video,
                chat_id=channel_id,
                video=media_files[0],
                caption=caption,
                width=media.width,
                height=media.height,
                duration=media.duration,
            )
    elif len(post.media) > 1:
        media = [
            input_media_item_cls[type(post.media[idx])](file, caption=caption if idx == 0 else "")
            for idx, file in enumerate(media_files)
        ]

        await flood_wait(
            bot.send_media_group,
            chat_id=channel_id,
            media=media,
        )


async def _process_post(bot: Client, post: RedditPost, channel_id: int, log_chat_id: int) -> None:
    logger.debug(f"Post: {post!r}")

    if not post.media or len(post.media) > 10:
        logger.info(f"Skipping post {post.id} ({post.title!r}): {len(post.media)=}")
        return

    logger.info(f"Sending post {post.id} ({post.title!r})")

    media_files: list[BytesIO] = []
    for idx, media in enumerate(post.media):
        photo = BytesIO()
        async with AsyncClient() as cl:
            async with cl.stream("GET", media.url) as resp:
                async for chunk in resp.aiter_bytes(1024 * 64):
                    photo.write(chunk)

        name = urlparse(media.url).path.split("/")[-1]
        if not name:
            name = f"{post.fullname}_{idx}.jpg"
        setattr(photo, "name", name)
        media_files.append(photo)

    try:
        await _send_one_post(bot, channel_id, post, media_files)
    except Exception as e:
        logger.opt(exception=e).error("Failed to send post to telegram")
        await bot.send_message(
            log_chat_id,
            (
                f"Failed to send post to the channel, error: {e}\n"
                f"Post link: {post.url}"
            )
        )


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

    no_posts_count = 30 if environ.get("FORCE_REFETCH_LATEST") == "1" else 0

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
                await _process_post(bot, post, channel_id, log_chat_id)

            if posts:
                no_posts_count = 0
                state.reddit_last_seen_id = posts[-1].fullname
                logger.info(f"Sent ~{len(posts)} posts, last seen id is {state.reddit_last_seen_id!r}")
            else:
                logger.info("No new upvoted posts available")
                no_posts_count += 1
                if no_posts_count >= 30:
                    logger.info(
                        "No new posts were available for the past 30 requests, trying to re-fetch latest upvoted post"
                    )
                    try:
                        post = await reddit.refetch_upvoted_maybe(reddit_username, state.reddit_last_seen_id)
                    except Exception as e:
                        logger.opt(exception=e).error("Failed to refetch latest upvoted post")
                        await bot.send_message(
                            log_chat_id, "Failed to refetch latest upvoted post, check logs for exact error"
                        )
                        no_posts_count -= 5
                        continue

                    if post is not None:
                        state.reddit_last_seen_id = post.fullname
                        await _process_post(bot, post, channel_id, log_chat_id)

                    no_posts_count = 0

            reddit.save_tokens(state)
            state.dump(state_file)

            await sleep(60)


if __name__ == "__main__":
    new_event_loop().run_until_complete(main())
