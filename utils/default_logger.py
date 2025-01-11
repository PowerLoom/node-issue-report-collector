import sys

from loguru import logger

# {extra} field can be used to pass extra parameters to the logger using .bind()
FORMAT = '{time:MMMM D, YYYY > HH:mm:ss!UTC} | {level} | {message}| {extra}'

logger.remove(0)
logger.add(sys.stdout, level='DEBUG', format=FORMAT)
logger.add(sys.stderr, level='WARNING', format=FORMAT)
logger.add(sys.stderr, level='ERROR', format=FORMAT)

# logging to files in /logs
# with file rotation
logger.add('logs/error.log', level='ERROR', format=FORMAT, rotation='5 MB')
logger.add('logs/warning.log', level='WARNING', format=FORMAT, rotation='5 MB')
logger.add('logs/info.log', level='INFO', format=FORMAT, rotation='5 MB')
logger.add('logs/debug.log', level='DEBUG', format=FORMAT, rotation='5 MB')
logger.add('logs/critical.log', level='CRITICAL', format=FORMAT, rotation='5 MB')
