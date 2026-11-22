import datetime as dt
import time
from typing import Callable

from bson import ObjectId
from pymongo import ASCENDING

PER_HOUR = 5
HOUR_S = 3600


def stamp(at: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(at, dt.timezone.utc)


class MailTrials:
    def __init__(self, db, clock: Callable[[], float] = time.time):
        self.col = db['mail_trials']
        self.clock = clock

    def ensure_indexes(self) -> None:
        self.col.create_index([('at', ASCENDING)], expireAfterSeconds=2 * HOUR_S)
        self.col.create_index([('who', ASCENDING), ('at', ASCENDING)])

    def reserve(self, who: str) -> bool:
        now = self.clock()
        row = {'_id': ObjectId(), 'who': who.lower(), 'at': stamp(now)}
        self.col.insert_one(row)
        if self.col.count_documents({'who': row['who'], 'at': {'$gt': stamp(now - HOUR_S)}}) > PER_HOUR:
            self.col.delete_one({'_id': row['_id']})
            return False
        return True
