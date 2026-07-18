# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

COPY --chown=65534:65534 --chmod=0555 \
  scripts/verify_real_dagster_callback_server.py \
  /opt/auris/verify_real_dagster_callback_server.py

USER 65534:65534
ENTRYPOINT ["python", "/opt/auris/verify_real_dagster_callback_server.py"]
