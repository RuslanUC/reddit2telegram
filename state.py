from __future__ import annotations

import shelve


class State:
    VERSION = 1

    __slots__ = ("reddit_last_seen_id", "access_token", "refresh_token")

    def __init__(self, reddit_last_seen_id: str | None, access_token: str | None, refresh_token: str | None) -> None:
        self.reddit_last_seen_id = reddit_last_seen_id
        self.access_token = access_token
        self.refresh_token = refresh_token

    @classmethod
    def load(cls, file_path: str) -> State:
        with shelve.open(file_path) as db:
            version = db.get("_version", 0)
            if version >= 1:
                return State(db["reddit_last_seen_id"], db["access_token"], db["refresh_token"])

        raise ValueError("Invalid (empty?) state")

    def dump(self, file_path: str) -> None:
        with shelve.open(file_path) as db:
            db.clear()
            db["_version"] = self.VERSION
            db["reddit_last_seen_id"] = self.reddit_last_seen_id
            db["access_token"] = self.access_token
            db["refresh_token"] = self.refresh_token
