def get_epoch_generator_last_epoch():
    return 'epochGenerator:lastEpoch'


def get_epoch_generator_epoch_history():
    return 'epochGenerator:epochHistory'


def get_snapshotter_issues_reported_key(snapshotter_id):
    return f'snapshotterData:{snapshotter_id}:issuesReported'


def get_generic_txn_issues_reported_key(account_address):
    return f'genericTxnData:{account_address}:issuesReported'

# sorted set

def get_cached_pings_set(instance_id):
    return f'instance:{instance_id}:cachedPings'


def get_snapshotters_status_zset():
    return 'SnapshotterStatus'


# string key:value pair that holds jsonified metadata on UUID peers
def get_snapshotter_info_key(alias):
    return f'snapshotterInfo:{alias}'


rpc_json_rpc_calls = (
    'rpc:jsonRpc:calls'
)

rpc_get_event_logs_calls = (
    'rpc:eventLogsCount:calls'
)

rpc_web3_calls = (
    'rpc:web3:calls'
)

rpc_blocknumber_calls = (
    'rpc:blocknumber:calls'
)

rpc_get_transaction_receipt_calls = (
    'rpc:transactionReceipt:calls'
)

event_detector_last_processed_block = 'SystemEventDetector:lastProcessedBlock'
