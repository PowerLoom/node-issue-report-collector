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

import redis
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis import asyncio as aioredis
from web3 import Web3

from auth.utils.data_models import RateLimitAuthCheck
from auth.utils.data_models import UserStatusEnum
from auth.utils.helpers import inject_rate_limit_fail_response
from auth.utils.helpers import rate_limit_auth_check
from data_models import AccountIdentifier
from data_models import GenericTxnIssue
from data_models import Message
from data_models import SnapshotterIdentifier
from data_models import SnapshotterIssue
from data_models import SnapshotterPing
from data_models import SnapshotterPingResponse
from helpers.redis_keys import get_generic_txn_issues_reported_key
from helpers.redis_keys import get_snapshotter_issues_reported_key
from helpers.redis_keys import get_snapshotters_status_zset
from settings.conf import settings
from utils.default_logger import logger
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


# setup CORS origins stuff
origins = ['*']

redis_lock = redis.Redis()

app = FastAPI()
app.logger = service_logger

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
        int(time.time()) - (7 * 24 * 60 * 60),
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
    response_model=List[SnapshotterIssue],
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
):

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

    issues = await redis_conn.zrevrangebyscore(
        name=get_snapshotter_issues_reported_key(
            snapshotter_id=snapshotter_id,
        ),
        max=int(time.time()),
        min=int(time.time()) - time_window,
        withscores=False,
    )

    issues_reports = []
    for issue in issues:
        issue_parsed = SnapshotterIssue(**json.loads(issue))
        issue_parsed.instanceID = snapshotter_id_masked
        issues_reports.append(issue_parsed)
    return issues_reports


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
