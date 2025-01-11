#!/bin/bash

# Create logs directory if it doesn't exist
mkdir -p logs
chmod 744 logs

# Build the Docker image
docker build -t powerloom-onchain-consensus .
