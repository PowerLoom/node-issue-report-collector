from typing import Any
from typing import Optional

from fastapi_pagination.api import apply_items_transformer
from fastapi_pagination.api import create_page
from fastapi_pagination.bases import AbstractParams
from fastapi_pagination.types import AdditionalData
from fastapi_pagination.types import ItemsTransformer
from fastapi_pagination.utils import verify_params
from redis import asyncio as aioredis


async def paginate_zset(
    redis_conn: aioredis.Redis,
    func: Any,
    args: dict,
    logger,
    params: Optional[AbstractParams] = None,
    *,
    transformer: Optional[ItemsTransformer] = None,
    additional_data: Optional[AdditionalData] = None,
) -> Any:

    logger.debug(f'paginate_zset: func: {func}, args: {args}')

    params, raw_params = verify_params(params, 'limit-offset')

    total = await redis_conn.zcount(
        name=args['name'],
        max=args['max'],
        min=args['min'],
    )

    args['start'] = raw_params.offset
    args['num'] = raw_params.limit

    items = await func(**args)

    t_items = apply_items_transformer(items, transformer)

    return create_page(
        t_items,
        total,
        params,
        **(additional_data or {}),
    )
