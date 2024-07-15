import asyncio
import datetime
import json
import time
import uuid
from functools import wraps
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from httpx import AsyncClient, AsyncHTTPTransport, Limits, Timeout
import redis
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_pagination import add_pagination
from fastapi_pagination.links import Page
from pydantic import Field
from redis import asyncio as aioredis
from web3 import Web3
from urllib.parse import urljoin
from auth.utils.data_models import RateLimitAuthCheck
from auth.utils.data_models import UserStatusEnum
from auth.utils.helpers import inject_rate_limit_fail_response
from auth.utils.helpers import rate_limit_auth_check
from data_models import AccountIdentifier, SequencerTotalRewardsResponse
from data_models import GenericTxnIssue
from data_models import Message
from data_models import SnapshotterIdentifier
from data_models import SnapshotterIssue
from data_models import SnapshotterPing
from data_models import SnapshotterPingResponse
from helpers.redis_keys import get_cached_pings_set, get_generic_txn_issues_reported_key
from helpers.redis_keys import get_snapshotter_issues_reported_key
from helpers.redis_keys import get_snapshotters_status_zset
from settings.conf import settings
from utils.default_logger import logger
from utils.paginator import paginate_zset
from utils.rate_limiter import load_rate_limiter_scripts
from utils.redis_conn import RedisPool

service_logger = logger.bind(
    service='PowerLoom|OnChainConsensus|ServiceEntry',
)


def acquire_bounded_semaphore(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        sem: asyncio.BoundedSemaphore = kwargs['semaphore']
        await sem.acquire()
        result = None
        try:
            result = await fn(*args, **kwargs)
        except Exception as e:
            service_logger.opt(exception=True).error(
                f'Error in {fn.__name__}: {e}',
            )
            pass
        finally:
            sem.release()
            return result

    return wrapped


def parse_snapshotter_issue(issue: str, snapshotter_id_masked: str) -> SnapshotterIssue:
    issue_parsed = SnapshotterIssue(**json.loads(issue))
    issue_parsed.instanceID = snapshotter_id_masked
    return issue_parsed


# setup CORS origins stuff
origins = ['*']

redis_lock = redis.Redis()

app = FastAPI()
app.logger = service_logger

Page = Page.with_custom_options(
    size=Field(10, ge=1, le=30),
)
add_pagination(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.middleware('http')
async def request_middleware(request: Request, call_next: Any) -> Optional[Dict]:
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    with service_logger.contextualize(request_id=request_id):
        service_logger.info('Request started for: {}', request.url)
        try:
            response = await call_next(request)

        except Exception as ex:
            service_logger.opt(exception=True).error(f'Request failed: {ex}')

            response = JSONResponse(
                content={
                    'info':
                        {
                            'success': False,
                            'response': 'Internal Server Error',
                        },
                    'request_id': request_id,
                }, status_code=500,
            )

        finally:
            response.headers['X-Request-ID'] = request_id
            service_logger.info('Request ended')
            return response


@app.on_event('startup')
async def startup_boilerplate():
    app.state.aioredis_pool = RedisPool(writer_redis_conf=settings.redis)
    await app.state.aioredis_pool.populate()
    app.state.reader_redis_pool = app.state.aioredis_pool.reader_redis_pool
    app.state.writer_redis_pool = app.state.aioredis_pool.writer_redis_pool
    app.state.rate_limit_lua_script_shas = await load_rate_limiter_scripts(app.state.writer_redis_pool)
    app.state.auth = dict()
    app.state.snapshotter_aliases = dict()
    app.state.async_transport = AsyncHTTPTransport(
        limits=Limits(
            max_connections=200,
            max_keepalive_connections=50,
            keepalive_expiry=None,
        ),
    )
    app.state.httpx_client = AsyncClient(
        timeout=Timeout(10.0, connect=5.0),
        follow_redirects=False,
        transport=app.state.async_transport,
    )


@app.post('/reportIssue')
async def report_issue(
        request: Request,
        req_parsed: SnapshotterIssue,
        response: Response,
        rate_limit_auth_dep: RateLimitAuthCheck = Depends(
            rate_limit_auth_check,
        ),
):
    """
    Report issue from a snapshotter
    """
    if not (
            rate_limit_auth_dep.rate_limit_passed and
            rate_limit_auth_dep.authorized and
            rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)

    time_of_reporting = int(time.time())
    req_parsed.timeOfReporting = str(time_of_reporting)
    try:
        req_parsed.instanceID = Web3.to_checksum_address(req_parsed.instanceID)
    except ValueError:
        return JSONResponse(status_code=400, content={'message': 'Invalid instanceID.'})

    await request.app.state.writer_redis_pool.zadd(
        name=get_snapshotter_issues_reported_key(
            snapshotter_id=req_parsed.instanceID,
        ),
        mapping={json.dumps(req_parsed.dict()): time_of_reporting},
    )

    # pruning expired items
    await request.app.state.writer_redis_pool.zremrangebyscore(
        get_snapshotter_issues_reported_key(
            snapshotter_id=req_parsed.instanceID,
        ), 0,
        int(time.time()) - (2 * 24 * 60 * 60),
    )

    return JSONResponse(status_code=200, content={'message': 'Reported Issue.'})


# report issues from epoch generator or force consensus
@app.post('/reportGenericTxnIssue')
async def report_generic_txn_issue(
        request: Request,
        req_parsed: GenericTxnIssue,
        response: Response,
        rate_limit_auth_dep: RateLimitAuthCheck = Depends(
            rate_limit_auth_check,
        ),
):
    """
    Report issue from Epoch Generator or Force Consensus
    """
    if not (
            rate_limit_auth_dep.rate_limit_passed and
            rate_limit_auth_dep.authorized and
            rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)

    reporting_address = req_parsed.accountAddress
    try:
        reporting_address = Web3.to_checksum_address(reporting_address)
    except ValueError:
        return JSONResponse(status_code=400, content={'message': 'Invalid accountAddress.'})

    time_of_reporting = int(time.time())

    await request.app.state.writer_redis_pool.zadd(
        name=get_generic_txn_issues_reported_key(
            account_address=reporting_address,
        ),
        mapping={json.dumps(req_parsed.dict()): time_of_reporting},
    )

    # pruning expired items
    await request.app.state.writer_redis_pool.zremrangebyscore(
        get_generic_txn_issues_reported_key(
            account_address=reporting_address,
        ), 0,
        int(time.time()) - (7 * 24 * 60 * 60),
    )

    return JSONResponse(status_code=200, content={'message': 'Reported Issue.'})


@app.post('/ping')
async def ping(
        request: Request,
        req_parsed: SnapshotterPing,
        response: Response,
        rate_limit_auth_dep: RateLimitAuthCheck = Depends(
            rate_limit_auth_check,
        ),
):
    """
    Ping from a snapshotter, helps in determining active/inactive snapshotters
    """
    if not (
            rate_limit_auth_dep.rate_limit_passed and
            rate_limit_auth_dep.authorized and
            rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)

    try:
        req_parsed.instanceID = Web3.to_checksum_address(req_parsed.instanceID)
    except ValueError:
        return JSONResponse(status_code=400, content={'message': 'Invalid instanceID.'})

    # add/update instanceID to zset with current time as ping time

    time = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

    await request.app.state.writer_redis_pool.zadd(
        name=get_snapshotters_status_zset(),
        mapping={req_parsed.instanceID + ':' + str(req_parsed.slotId): time},
        
    )
    await request.app.state.writer_redis_pool.set(
        'lastPing:' + req_parsed.instanceID + ':' + str(req_parsed.slotId), time
    )

    return JSONResponse(
        status_code=200,
        content={'message': 'Ping Successful!'},
    )

@app.get('/pingActivity/{address}/{slot_id}')
async def return_ping_activity_state(
    request: Request,
    address: str,
    slot_id: int,
    response: Response,
    rate_limit_auth_dep: RateLimitAuthCheck = Depends(
        rate_limit_auth_check,
        ),
):
    if not (
        rate_limit_auth_dep.rate_limit_passed and
        rate_limit_auth_dep.authorized and
        rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)
    
    try:
        address = Web3.to_checksum_address(address)
    except ValueError:
        return JSONResponse(status_code=400, content={'message': 'Invalid instanceID.'})
    
    key = 'lastPing:' + address + ':' + str(slot_id)
    # get last 20 pings
    # check if cached pings exist
    cache_ = await request.app.state.writer_redis_pool.get(
        get_cached_pings_set(address)
    )
    if cache_:
        service_logger.debug('Using cached pings: {}', cache_)
        timestamps = json.loads(cache_.decode())
        lastPing = timestamps[-1]
    else:
        ping_zset = await request.app.state.writer_redis_pool.zrange(
            name=get_snapshotters_status_zset(),
            start=0,
            end=-1,
            withscores=True,
        )
        timestamps = [int(x[1]) for x in ping_zset if x[0].decode().startswith(address)]
        # filter latest 20 pings
        timestamps_last_20 = sorted(timestamps, reverse=True)[:20]
        service_logger.debug('Filtered pings from live zset for address {}: {}', address, timestamps_last_20)
        if timestamps:
            lastPing = timestamps[-1]
            # cache the last 20 pings
            await request.app.state.writer_redis_pool.set(
                get_cached_pings_set(address),
                json.dumps(timestamps_last_20),
                ex=60,
            )
        lastPing = await request.app.state.writer_redis_pool.get(
            key
        )
        if lastPing is not None:
            lastPing = int(lastPing.decode('utf-8'))
        else:
            lastPing = 0
    time = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    if time - lastPing > settings.ping_activity_threshold:
        intermediate_activity_status = False
    else:
        intermediate_activity_status = True
    return JSONResponse(status_code=200, content={'pingActivity': intermediate_activity_status, 'lastPings': timestamps})
    
@app.get('/activity/{address}/{slot_id}')
async def return_activity_state(
    request: Request,
    address: str,
    slot_id: int,
    response: Response,
    rate_limit_auth_dep: RateLimitAuthCheck = Depends(
        rate_limit_auth_check,
        ),
):
    if not (
        rate_limit_auth_dep.rate_limit_passed and
        rate_limit_auth_dep.authorized and
        rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)
    
    try:
        address = Web3.to_checksum_address(address)
    except ValueError:
        return JSONResponse(status_code=400, content={'message': 'Invalid instanceID.'})
    
    key = 'lastPing:' + address + ':' + str(slot_id)
    lastPing = await request.app.state.writer_redis_pool.get(
        key
    )
    if lastPing is not None:
        lastPing = int(lastPing.decode('utf-8'))
    else:
        lastPing = 0
    time = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    if time - lastPing > settings.ping_activity_threshold:
        intermediate_activity_status = False
    else:
        intermediate_activity_status = True
    # call the sequencer API to get the activity status
    total_rewards_response_obj = await request.app.state.httpx_client.post(
        url=urljoin(settings.sequencer_url, '/getTotalRewards'),
        json={
            'slot_id': slot_id,
            'token': settings.sequencer_query_token,
        },
    )
    service_logger.debug('Response from sequencer API for total rewards for slot {}: {}', slot_id, total_rewards_response_obj.text)
    try:
        total_rewards_response = total_rewards_response_obj.json()
    except json.JSONDecodeError:
        service_logger.error('Error decoding response from sequencer API into dict object: {}', total_rewards_response_obj.text)
        return JSONResponse(status_code=500, content={'message': 'Internal Server Error.'})
    try:
        total_rewards_response_parse = SequencerTotalRewardsResponse.parse_obj(total_rewards_response)
    except ValueError:
        service_logger.error('Error parsing response from sequencer API for total rewards for slot {}: {}', slot_id, total_rewards_response)
        return JSONResponse(status_code=500, content={'message': 'Internal Server Error.'})
    if total_rewards_response_parse.info.response > 0:
        rewards_status = True
    else:
        rewards_status = False
    return JSONResponse(status_code=200, content={
        'overallActivity': intermediate_activity_status and rewards_status,
        'pingActivity': intermediate_activity_status,
        'rewardsActivity': rewards_status,
    }
    )



@app.get('/lastPing/{address}/{slot_id}')
async def get_last_ping(
    request: Request,
    address: str,
    slot_id: int,
    response: Response,
    rate_limit_auth_dep: RateLimitAuthCheck = Depends(
        rate_limit_auth_check,
        ),
    ):
    if not (
        rate_limit_auth_dep.rate_limit_passed and
        rate_limit_auth_dep.authorized and
        rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)
    
    try:
        address = Web3.to_checksum_address(address)
    except ValueError:
        return JSONResponse(status_code=400, content={'message': 'Invalid instanceID.'})
    
    key = 'lastPing:' + address + ':' + str(slot_id)
    lastPing = await request.app.state.writer_redis_pool.get(
        key
    )
    if lastPing is not None:
        lastPing = int(lastPing.decode('utf-8'))
    else:
        lastPing = 0
    return JSONResponse(status_code=200, content={'lastPing': lastPing})



@app.post(
    '/metrics/activeSnapshotters/{time_window}',
    response_model=List[SnapshotterPingResponse],
    responses={404: {'model': Message}},
)
async def get_snapshotters_status_post(
    time_window: int,
    request: Request,
    response: Response,
    rate_limit_auth_dep: RateLimitAuthCheck = Depends(
        rate_limit_auth_check,
    ),
):
    """
    Get snapshotters which submitted ping in time window
    """
    if not (
        rate_limit_auth_dep.rate_limit_passed and
        rate_limit_auth_dep.authorized and
        rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)
    redis_conn: aioredis.Redis = request.app.state.reader_redis_pool

    # get snapshotters which submitted ping in time window
    active_snapshotters = await redis_conn.zrevrangebyscore(
        name=get_snapshotters_status_zset(),
        max=int(time.time()),
        min=int(time.time()) - time_window,
        withscores=True,
    )

    snapshotters_status = []
    for snapshotter, ping_time in active_snapshotters:
        snapshotter_info = snapshotter.decode().split(':')
        snapshotters_status.append(
            SnapshotterPingResponse(
                instanceID=snapshotter_info[0], slotId=int(snapshotter_info[1]), timeOfReporting=int(ping_time),
            ),
        )
    return snapshotters_status


@app.post(
    '/metrics/inactiveSnapshotters/{time_window}',
    response_model=List[SnapshotterPingResponse],
    responses={404: {'model': Message}},
)
async def get_inactive_snapshotters_status_post(
    time_window: int,
    request: Request,
    response: Response,
    rate_limit_auth_dep: RateLimitAuthCheck = Depends(
        rate_limit_auth_check,
    ),
):
    """
    Get snapshotters which did not submit ping in time window
    """
    if not (
        rate_limit_auth_dep.rate_limit_passed and
        rate_limit_auth_dep.authorized and
        rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)

    redis_conn: aioredis.Redis = request.app.state.reader_redis_pool

    # get snapshotters who did not submit ping in time window
    inactive_snapshotters = await redis_conn.zrevrangebyscore(
        name=get_snapshotters_status_zset(),
        max=int(time.time()) - time_window,
        min=0,
        withscores=True,
    )

    snapshotters_status = []
    for snapshotter, ping_time in inactive_snapshotters:
        snapshotters_status.append(
            SnapshotterPingResponse(
                instanceID=snapshotter.decode(), timeOfReporting=int(ping_time),
            ),
        )
    return snapshotters_status


@app.post(
    '/metrics/issues/{time_window}',
    response_model=Page[SnapshotterIssue],
    responses={404: {'model': Message}},
)
async def get_snapshotter_issues_post(
    time_window: int,
    request: Request,
    req_parsed: SnapshotterIdentifier,
    response: Response,
    rate_limit_auth_dep: RateLimitAuthCheck = Depends(
        rate_limit_auth_check,
    ),
) -> Page[SnapshotterIssue]:

    """
    Get issues reported by a snapshotter in time window
    """
    if not (
        rate_limit_auth_dep.rate_limit_passed and
        rate_limit_auth_dep.authorized and
        rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)
    redis_conn: aioredis.Redis = request.app.state.reader_redis_pool

    snapshotter_id = req_parsed.instanceId
    # create a masked version of snapshotter_id
    snapshotter_id_masked = snapshotter_id[:6] + '*********************' + snapshotter_id[-6:]

    args = {
        'name': get_snapshotter_issues_reported_key(
            snapshotter_id=snapshotter_id,
        ),
        'max': int(time.time()),
        'min': int(time.time()) - time_window,
    }

    return await paginate_zset(
        redis_conn=redis_conn,
        func=redis_conn.zrevrangebyscore,
        args=args,
        logger=service_logger,
        transformer=lambda items: [
            parse_snapshotter_issue(
                issue, 
                snapshotter_id_masked=snapshotter_id_masked,
            ) for issue in items
        ],
    )


@app.post(
    '/metrics/genericTxnIssues/{time_window}',
    response_model=List[GenericTxnIssue],
    responses={404: {'model': Message}},
)
async def get_generic_txn_issues_post(
        time_window: int,
        request: Request,
        req_parsed: AccountIdentifier,
        response: Response,
        rate_limit_auth_dep: RateLimitAuthCheck = Depends(
            rate_limit_auth_check,
        ),
):
    """
    Get generic txn issues reported by a snapshotter in time window
    """
    if not (
            rate_limit_auth_dep.rate_limit_passed and
            rate_limit_auth_dep.authorized and
            rate_limit_auth_dep.owner.active == UserStatusEnum.active
    ):
        return inject_rate_limit_fail_response(rate_limit_auth_dep)
    redis_conn: aioredis.Redis = request.app.state.reader_redis_pool

    account_address = req_parsed.accountAddress

    account_address_masked = account_address[:6] + '*********************' + account_address[-6:]

    issues = await redis_conn.zrevrangebyscore(
        name=get_generic_txn_issues_reported_key(
            account_address=account_address,
        ),
        max=int(time.time()),
        min=int(time.time()) - time_window,
        withscores=False,
    )

    issues_reports = []
    for issue in issues:
        issue_parsed = GenericTxnIssue(**json.loads(issue))
        issue_parsed.accountAddress = account_address_masked
        issues_reports.append(issue_parsed)
    return issues_reports
