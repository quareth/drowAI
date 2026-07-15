import time
from functools import wraps
from typing import Dict, List
from fastapi import HTTPException, status

class InMemoryRateLimiter:
    def __init__(self):
        self.calls: Dict[str, List[float]] = {}

    def is_allowed(self, key: str, max_calls: int, window: int) -> bool:
        now = time.time()
        call_times = self.calls.get(key, [])
        call_times = [t for t in call_times if now - t < window]
        if len(call_times) >= max_calls:
            self.calls[key] = call_times
            return False
        call_times.append(now)
        self.calls[key] = call_times
        return True

rate_limiter = InMemoryRateLimiter()


def rate_limit(max_calls: int, window: int):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            user_id = None
            if 'current_user' in kwargs and hasattr(kwargs['current_user'], 'id'):
                user_id = kwargs['current_user'].id
            key = f"{func.__name__}:{user_id}"
            if not rate_limiter.is_allowed(key, max_calls, window):
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")
            return await func(*args, **kwargs)
        return wrapper
    return decorator
