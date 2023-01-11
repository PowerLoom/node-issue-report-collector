from data_models import (
    SnapshotSubmission, SubmissionResponse, PeerRegistrationRequest, SubmissionAcceptanceStatus, SnapshotBase,
    EpochConsensusStatus, Snapshotters, Epoch, EpochData, EpochDataPage, Submission, SubmissionStatus, Message, EpochInfo,
    EpochStatus, EpochDetails, SnapshotterIssue
)
from typing import List, Optional, Any, Dict
from fastapi.responses import JSONResponse
from settings.conf import settings
from helpers.state import submission_delayed, register_submission, check_consensus, check_submissions_consensus
from helpers.redis_keys import *
from fastapi import FastAPI, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from functools import wraps
from utils.redis_conn import RedisPool
from pydantic import ValidationError
import sys
import json
import redis
import time
import uuid
import asyncio
from loguru import logger


FORMAT = '{time:MMMM D, YYYY > HH:mm:ss!UTC} | {level} | {message}| {extra}'

logger.remove(0)
logger.add(sys.stdout, level='DEBUG', format=FORMAT)
logger.add(sys.stderr, level='WARNING', format=FORMAT)
logger.add(sys.stderr, level='ERROR', format=FORMAT)

service_logger = logger.bind(service='consensus_service')


def acquire_bounded_semaphore(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        sem: asyncio.BoundedSemaphore = kwargs['semaphore']
        await sem.acquire()
        result = None
        try:
            result = await fn(*args, **kwargs)
        except Exception as e:
            service_logger.opt(exception=True).error(f'Error in {fn.__name__}: {e}')
            pass
        finally:
            sem.release()
            return result
    return wrapped


# setup CORS origins stuff
origins = ["*"]

redis_lock = redis.Redis()

app = FastAPI()
app.logger = service_logger

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

@app.middleware('http')
async def request_middleware(request: Request, call_next: Any) -> Optional[Dict]:
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    with service_logger.contextualize(request_id=request_id):
        service_logger.info('Request started')
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
    app.aioredis_pool = RedisPool(writer_redis_conf=settings.redis)
    await app.aioredis_pool.populate()

    app.reader_redis_pool = app.aioredis_pool.reader_redis_pool
    app.writer_redis_pool = app.aioredis_pool.writer_redis_pool


@app.post('/registerProjectPeer')
async def register_peer_against_project(
        request: Request,
        response: Response
):
    req_json = await request.json()
    try:
        req_parsed: PeerRegistrationRequest = PeerRegistrationRequest.parse_obj(req_json)
    except ValidationError:
        service_logger.opt(exception=True).error('Bad request in register peer: {}', req_json)
        response.status_code = 400
        return {}
    await request.app.writer_redis_pool.sadd(
        get_project_registered_peers_set_key(req_parsed.projectID),
        req_parsed.instanceID
    )


@app.post('/submitSnapshot')
async def submit_snapshot(
        request: Request,
        response: Response
):
    cur_ts = int(time.time())
    req_json = await request.json()
    try:
        req_parsed = SnapshotSubmission.parse_obj(req_json)
    except ValidationError:
        service_logger.opt(exception=True).error('Bad request in submit snapshot: {}', req_json)
        response.status_code = 400
        return {}
    service_logger.debug('Snapshot for submission: {}', req_json)
    # get last accepted epoch?
    if await submission_delayed(
        project_id=req_parsed.projectID,
        epoch_end=req_parsed.epoch.end,
        auto_init_schedule=True,
        redis_conn=request.app.writer_redis_pool
    ):
        response_obj = SubmissionResponse(status=SubmissionAcceptanceStatus.accepted, delayedSubmission=True)
    else:
        response_obj = SubmissionResponse(status=SubmissionAcceptanceStatus.accepted, delayedSubmission=False)
    consensus_status, finalized_cid = await register_submission(req_parsed, cur_ts, request.app.writer_redis_pool)
    # if consensus achieved, set the key
    if finalized_cid:
        await request.app.writer_redis_pool.sadd(get_project_finalized_epochs_key(req_parsed.projectID), req_parsed.epoch.end)

    response_obj.status = consensus_status
    response_obj.finalizedSnapshotCID = finalized_cid
    response.body = response_obj
    return response_obj.dict()


@app.post('/checkForSnapshotConfirmation')
async def check_submission_status(
        request: Request,
        response: Response
):
    req_json = await request.json()
    try:
        req_parsed = SnapshotSubmission.parse_obj(req_json)
    except ValidationError:
        service_logger.opt(exception=True).error('Bad request in check submission status: {}', req_json)
        response.status_code = 400
        return {}
    status, finalized_cid = await check_submissions_consensus(
        submission=req_parsed, redis_conn=request.app.writer_redis_pool
    )
    if status == SubmissionAcceptanceStatus.notsubmitted:
        response.status_code = 400
        return SubmissionResponse(status=status, delayedSubmission=False, finalizedSnapshotCID=None).dict()
    else:
        return SubmissionResponse(
            status=status,
            delayedSubmission=await submission_delayed(
                req_parsed.projectID,
                epoch_end=req_parsed.epoch.end,
                auto_init_schedule=False,
                redis_conn=request.app.writer_redis_pool
            ),
            finalizedSnapshotCID=finalized_cid
        ).dict()


@app.post('/reportIssue')
async def report_issue(
        request: Request,
        response: Response
):
    req_json = await request.json()
    try:
        req_parsed = SnapshotterIssue.parse_obj(req_json)
    except ValidationError:
        service_logger.opt(exception=True).error('Bad request in report issue: {}', req_json)
        return JSONResponse(status_code=400, content={"message": f"Validation Error, invalid Data."})

    # Updating time of reporting to avoid manual incorrect time manipulation
    req_parsed.timeOfReporting= int(time.time())
    await request.app.writer_redis_pool.zadd(
        name=get_snapshotter_issues_reported_key(snapshotter_id=req_parsed.instanceID), 
        mapping={json.dumps(req_parsed.dict()): req_parsed.timeOfReporting})

    # pruning expired items
    request.app.writer_redis_pool.zremrangebyscore(
        get_snapshotter_issues_reported_key(snapshotter_id=req_parsed.instanceID), 0, int(time.time()) - (7*24*60*60)
        )

    return JSONResponse(status_code=200, content={"message": f"Reported Issue."})


@app.get("/epochDetails", response_model=EpochDetails, responses={404: {"model": Message}})
async def epoch_details(request: Request, response: Response, epoch: int = Query(default=0, gte=0)):
    if epoch == 0:
        epoch = int(await app.reader_redis_pool.get(get_epoch_generator_last_epoch()))
    
    epoch_release_time = await app.reader_redis_pool.zscore(
        get_epoch_generator_epoch_history(),
        json.dumps({"begin":epoch-settings.chain.epoch.height+1,"end":epoch})
    )
    
    if not epoch_release_time:
        return JSONResponse(status_code=404, content={"message": f"No epoch found with Epoch End Time {epoch}"})

    epoch_release_time = int(epoch_release_time)

    project_keys = []
    finalized_projects_count = 0
    projectID_pattern = "projectID:*:centralizedConsensus:peers"
    async for project_id in request.app.reader_redis_pool.scan_iter(match=projectID_pattern):
        project_id = project_id.decode("utf-8").split(":")[1]
        project_keys.append(project_id)
        if await request.app.reader_redis_pool.sismember(get_project_finalized_epochs_key(project_id), epoch):
            finalized_projects_count += 1

    total_projects = len(project_keys)

    if finalized_projects_count == total_projects:
        epoch_status = EpochStatus.finalized
    else:
        epoch_status = EpochStatus.in_progress

    return EpochDetails(
        epochEndHeight = epoch,
        releaseTime = epoch_release_time,
        status = epoch_status,
        totalProjects = total_projects,
        projectsFinalized = finalized_projects_count
    )


@app.post('/epochStatus')
async def epoch_status(
        request: Request,
        response: Response
):
    req_json = await request.json()
    try:
        req_parsed = SnapshotBase.parse_obj(req_json)
    except ValidationError:
        service_logger.opt(exception=True).error('Bad request in epoch status: {}', req_json)
        response.status_code = 400
        return {}
    status, finalized_cid = await check_submissions_consensus(
        submission=req_parsed, redis_conn=request.app.writer_redis_pool, epoch_consensus_check=True
    )
    if status != SubmissionAcceptanceStatus.finalized:
        status = EpochConsensusStatus.no_consensus
    else:
        status = EpochConsensusStatus.consensus_achieved
    return SubmissionResponse(status=status, delayedSubmission=False, finalizedSnapshotCID=finalized_cid).dict()


# List of projects tracked/registered '/metrics/projects' .
# Response will be the list of projectIDs that are being tracked for consensus.
@app.get("/metrics/projects", responses={404: {"model": Message}})
async def get_projects(request: Request, response: Response):
    """
    Returns a list of project IDs that are being tracked for consensus.
    """
    projects = []

    projectID_pattern = "projectID:*:centralizedConsensus:peers"
    async for project_id in request.app.reader_redis_pool.scan_iter(match=projectID_pattern, count=100):
        projects.append(project_id.decode("utf-8").split(":")[1])

    return projects


# List of snapshotters registered for a project '/metrics/{projectid}/snapshotters'.
# Response will be the list of instance-IDs of the snapshotters that are participanting in consensus for this project.
@app.get("/metrics/{project_id}/snapshotters", response_model=Snapshotters, responses={404: {"model": Message}})
async def get_snapshotters(project_id: str, request: Request, response: Response):
    """
    Returns a list of instance-IDs of snapshotters that are participating in consensus for the given project.
    """
    snapshotters = await request.app.reader_redis_pool.smembers(
        get_project_registered_peers_set_key(project_id)
    )
    # NOTE: Ideal way is to check if project exists first and then get the snapshotters.
    # But right now fetching project list is expensive. So we are doing it this way.
    if not snapshotters:
        return JSONResponse(status_code=404, content={"message": f"Either the project is not registered or there are no snapshotters for project {project_id}"})

    return Snapshotters(projectId=project_id, snapshotters=snapshotters)


@acquire_bounded_semaphore
async def bound_check_consensus(project_id:str, epoch_end:int, redis_pool:RedisPool, semaphore: asyncio.BoundedSemaphore) -> SubmissionAcceptanceStatus:
    """Check consensus in a bounded way. Will run N paralell threads at once max."""
    consensus_status = await check_consensus(project_id, epoch_end, redis_pool)
    return consensus_status


# List of epochs submitted per project '/metrics/{projectid}/epochs' .
# Response will be the list of epochs whose state is currently available in consensus service.
@app.get("/metrics/{project_id}/epochs", response_model=EpochDataPage, responses={404: {"model": Message}})
async def get_epochs(project_id: str, request: Request,
        response: Response, page: int = Query(default=1, gte=0), limit: int = Query(default=100, lte=100)):
    """
    Returns a list of epochs whose state is currently available in the consensus service for the given project.
    """
    epoch_keys = []
    epoch_pattern = f"projectID:{project_id}:[0-9]*:centralizedConsensus:epochSubmissions"
    async for epoch_key in request.app.reader_redis_pool.scan_iter(match=epoch_pattern, count=500):
        epoch_keys.append(epoch_key)

    if not epoch_keys:
        return JSONResponse(status_code=404, content={"message": f"No epochs found for project {project_id}. Either project is not valid or was just added."})

    epoch_ends = sorted(list(set([eval(key.decode('utf-8').split(':')[2]) for key in epoch_keys])), reverse=True)
    if (page-1)*limit < len(epoch_ends):
        epoch_ends_data = epoch_ends[(page-1)*limit:page*limit]
    else:
        epoch_ends_data = []
    semaphore = asyncio.BoundedSemaphore(25)
    epochs = []
    epoch_status_tasks = [bound_check_consensus(project_id, epoch_end, request.app.reader_redis_pool, semaphore=semaphore) for epoch_end in epoch_ends_data]
    epoch_status = await asyncio.gather(*epoch_status_tasks)

    for i in range(len(epoch_ends_data)):
        finalized = False
        if epoch_status[i][0] == SubmissionAcceptanceStatus.finalized:
            finalized = True
        epochs.append(Epoch(sourcechainEndheight=epoch_ends_data[i], finalized=finalized))

    data = EpochData(projectId=project_id, epochs=epochs)

    return {
     "total": len(epoch_ends),
     "next_page": None if page*limit >= len(epoch_ends) else f"/metrics/{project_id}/epochs?page={page+1}&limit={limit}",
     "prev_page": None if page == 1 else f"/metrics/{project_id}/epochs?page={page-1}&limit={limit}",
     "data": data
    }


@app.get("/metrics/{snapshotter_id}/issues", response_model=List[SnapshotterIssue], responses={404: {"model": Message}})
async def get_snapshotter_issues(snapshotter_id: str, request: Request,
        response: Response):

    issues_with_scores = await request.app.reader_redis_pool.zrevrange(get_snapshotter_issues_reported_key(snapshotter_id), 0, -1, withscores=True)
    issues = []
    for issue in issues_with_scores:
        issues.append(SnapshotterIssue(**json.loads(issue[0])))

    return issues

# Submission details for an epoch '/metrics/{projectid}/{epoch}/submissionStatus' .
# This shall include whether consensus has been achieved along with final snapshotCID.
# The details of snapshot submissions snapshotterID and submissionTime along with snapshot submitted.
@app.get(
    "/metrics/{project_id}/{epoch}/submissionStatus",
    response_model=List[Submission], responses={404: {"model": Message}})
async def get_submission_status(project_id: str, epoch: str, request: Request,
        response: Response):
    """
    Returns the submission details for the given project and epoch, including whether consensus has been achieved and the final snapshot CID.
    Also includes the details of snapshot submissions, such as snapshotter ID and submission time.
    """

    submission_schedule = await request.app.reader_redis_pool.get(get_epoch_submission_schedule_key(project_id, epoch))
    if not submission_schedule:
        return JSONResponse(status_code=404, content={"message": f"Submission schedule for projectID {project_id} and epoch {epoch} not found"})
    submission_schedule = json.loads(submission_schedule)

    submission_data = await request.app.reader_redis_pool.hgetall(
        get_epoch_submissions_htable_key(project_id, epoch)
    )

    if not submission_data:
        return JSONResponse(status_code=404, content={"message": f"Project with projectID {project_id} and epoch {epoch} not found"})

    submissions = []
    for k, v in submission_data.items():
        k, v = k.decode("utf-8"), json.loads(v)
        if v["submittedTS"] < submission_schedule["end"]:
            submission_status = SubmissionStatus.within_schedule
        else:
            submission_status = SubmissionStatus.delayed

        submission = Submission(
            snapshotterInstanceID = k,
            submittedTS = v["submittedTS"],
            snapshotCID = v["snapshotCID"],
            submissionStatus = submission_status)
        submissions.append(submission)

    return submissions


@app.get("/currentEpoch", response_model=EpochInfo, responses={404: {"model": Message}})
async def get_current_epoch(request: Request,
        response: Response):
    """
    Returns the current epoch information.

    Returns:
        dict: A dictionary with the following keys:
            "chain-id" (int): The chain ID.
            "epochStartBlockHeight" (int): The epoch start block height.
            "epochEndBlockHeight" (int): The epoch end block height.
    """
    # Get the current epoch end block height from Redis
    epoch_end_block_height = await app.writer_redis_pool.get(get_epoch_generator_last_epoch())

    if epoch_end_block_height is None:
        return JSONResponse(status_code=404, content={"message": "Epoch not found! Make sure the system ticker is running."})
    epoch_end_block_height = int(epoch_end_block_height.decode("utf-8"))
    # Calculate the epoch start block height using the epoch length from the configuration
    epoch_start_block_height = epoch_end_block_height - settings.chain.epoch.height + 1

    # Return the current epoch information as a JSON response
    return {
        "chainId": settings.chain.chain_id,
        "epochStartBlockHeight": epoch_start_block_height,
        "epochEndBlockHeight": epoch_end_block_height
    }
