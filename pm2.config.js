// this means if app restart {MAX_RESTART} times in 1 min then it stops

const { readFileSync } = require('fs');

const NODE_ENV = process.env.NODE_ENV || 'development';

const MAX_RESTART = 10;
const MIN_UPTIME = 60000;


module.exports = {
  apps : [
    {
      name   : "node-info-collector-service",
      script: `poetry run python -m gunicorn_main_launcher`,
      max_restarts: MAX_RESTART,
      min_uptime: MIN_UPTIME,
      kill_timeout : 3000,
      env: {
        NODE_ENV: NODE_ENV,
      },
    }
  ]
}
