import time
from datetime import datetime
from datetime import timedelta
from json.decoder import JSONDecodeError

import pydantic
from async_limits import parse_many
from fastapi import Depends
from fastapi import Request
from fastapi.responses import JSONResponse
from redis import asyncio as aioredis

from auth.utils.data_models import AuthCheck
from auth.utils.data_models import RateLimitAuthCheck
from auth.utils.data_models import UserStatusEnum
from data_models import SnapshotterMetadata
from helpers import redis_keys
from settings.conf import settings as consensus_settings
from utils.rate_limiter import generic_rate_limiter


def incr_success_calls_count(
        request: Request,
        rate_limit_auth_dep: RateLimitAuthCheck,
):
    request.app.state.auth[rate_limit_auth_dep.owner.alias].callsCount += 1


def incr_throttled_calls_count(
        request: Request,
        rate_limit_auth_dep: RateLimitAuthCheck,
):
    request.app.state.auth[rate_limit_auth_dep.owner.alias].throttledCount += 1


def inject_rate_limit_fail_response(rate_limit_auth_check_dependency: RateLimitAuthCheck) -> JSONResponse:
    if rate_limit_auth_check_dependency.authorized:
        response_body = {
            'error': {
                'details': f'Rate limit exceeded: {rate_limit_auth_check_dependency.violated_limit}. '
                           'Check response body and headers for more details on backoff.',
                'data': {
                    'rate_violated': str(rate_limit_auth_check_dependency.violated_limit),
                    'retry_after': rate_limit_auth_check_dependency.retry_after,
                    'violating_domain': rate_limit_auth_check_dependency.current_limit,
                },
            },
        }
        response_headers = {
            'Retry-After': (datetime.now() + timedelta(rate_limit_auth_check_dependency.retry_after)).isoformat(),
        }
        response_status = 429
    else:
        response_headers = dict()
        response_body = {
            'error': {
                'details': rate_limit_auth_check_dependency.reason,
            },
        }
        if 'cache error' in rate_limit_auth_check_dependency.reason:
            response_status = 500
        else:  # return 401 for unauthorized access for every other reason
            response_status = 401
    return JSONResponse(content=response_body, status_code=response_status, headers=response_headers)


async def auth_check(
        request: Request,
) -> AuthCheck:
    auth_redis_conn: aioredis.Redis = request.app.state.writer_redis_pool
    # public access. create owner based on IP address
    if 'CF-Connecting-IP' in request.headers:
        user_ip = request.headers['CF-Connecting-IP']
    elif 'X-Forwarded-For' in request.headers:
        proxy_data = request.headers['X-Forwarded-For']
        ip_list = proxy_data.split(',')
        user_ip = ip_list[0]  # first address in list is User IP
    else:
        user_ip = request.client.host  # For local development

    # If user is not in cache, get from redis and add to cache
    if user_ip not in request.app.state.auth:
        ip_user_dets_b = await auth_redis_conn.get(redis_keys.get_snapshotter_info_key(alias=user_ip))
        # if user is not in redis, create a new one
        if not ip_user_dets_b:
            public_owner = SnapshotterMetadata(
                alias=user_ip,
                name=user_ip,
                email=user_ip,
                rate_limit=consensus_settings.rate_limit,
                active=UserStatusEnum.active,
                callsCount=0,
                throttledCount=0,
                next_reset_at=int(time.time()) + 86400,
            )
            await auth_redis_conn.set(redis_keys.get_snapshotter_info_key(alias=user_ip), public_owner.json())
        else:
            public_owner = SnapshotterMetadata.parse_raw(ip_user_dets_b)

        request.app.state.auth[user_ip] = public_owner

    else:
        public_owner = request.app.state.auth[user_ip]

    return AuthCheck(
        authorized=public_owner.active == UserStatusEnum.active,
        api_key='public',
        reason='',
        owner=public_owner,
    )


async def rate_limit_auth_check(
        request: Request,
        auth_check_dep: AuthCheck = Depends(auth_check),
) -> RateLimitAuthCheck:
    if auth_check_dep.authorized:
        auth_redis_conn: aioredis.Redis = request.app.state.writer_redis_pool
        try:
            passed, retry_after, violated_limit = await generic_rate_limiter(
                parsed_limits=parse_many(auth_check_dep.owner.rate_limit),
                key_bits=[
                    auth_check_dep.owner.name,
                ],
                redis_conn=auth_redis_conn,
                rate_limit_lua_script_shas=request.app.state.rate_limit_lua_script_shas,
            )
        except:
            auth_check_dep.authorized = False
            auth_check_dep.reason = 'internal cache error'
            return RateLimitAuthCheck(
                **auth_check_dep.dict(),
                rate_limit_passed=False,
                retry_after=1,
                violated_limit='',
                current_limit=auth_check_dep.owner.rate_limit,
            )
        else:
            ret = RateLimitAuthCheck(
                **auth_check_dep.dict(),
                rate_limit_passed=passed,
                retry_after=retry_after,
                violated_limit=violated_limit,
                current_limit=auth_check_dep.owner.rate_limit,
            )

            if passed:
                incr_success_calls_count(request, ret)
            else:
                incr_throttled_calls_count(request, ret)
            return ret
        finally:
            if auth_check_dep.owner.next_reset_at <= int(time.time()):
                owner_updated_obj = auth_check_dep.owner.copy(deep=True)
                owner_updated_obj.callsCount = 0
                owner_updated_obj.throttledCount = 0
                owner_updated_obj.next_reset_at = int(time.time()) + 86400
                request.app.state.auth[auth_check_dep.owner.alias] = owner_updated_obj
    else:
        return RateLimitAuthCheck(
            **auth_check_dep.dict(),
            rate_limit_passed=False,
            retry_after=1,
            violated_limit='',
            current_limit='',
        )
