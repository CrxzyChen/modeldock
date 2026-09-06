# Fixed Wan 2.1 derivative. The build must use an already verified runtime-v1
# parent and an offline wheelhouse; no package resolution occurs during build.
FROM runtime_parent
USER 0:0
COPY locked/ /offline/wan21-wheels/
COPY requirements.txt /offline/wan21-requirements.txt
RUN /opt/python/bin/python3.11 -m pip install --no-index --no-deps --require-hashes \
    --find-links=/offline/wan21-wheels -r /offline/wan21-requirements.txt
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
USER 1000:1000
WORKDIR /opt/mediacenter
RUN /opt/python/bin/python3.11 -m pip check && /opt/python/bin/python3.11 -B -c \
    "import mediacenter.video_worker_cli,mediacenter.adapters.wan21,imageio_ffmpeg"
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.video_worker_cli"]
